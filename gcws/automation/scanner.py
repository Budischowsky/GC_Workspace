"""Looking at a watched folder without touching it: batch folders, runs and whether a run is
finished (only ``os.scandir`` / ``stat``; nothing is opened for writing or moved).

A run is ready when its files did not change over ``stable_scans`` looks, it is older than
``min_age`` and there is a sign that the acquisition ended: Agilent's ``checksum.xml``, the next
run of the sequence has started, the sequence log says completed, or the folder has been quiet.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: file written last into an Agilent .D folder
MARKERS = ("checksum.xml",)


@dataclass
class RunObs:
    path: Path
    stem: str                   # casefolded stem ("07_..._a")
    name: str                   # file / folder name
    fingerprint: str
    mtime: float                # newest file time
    marker: bool
    busy: bool = False          # a file could not be read (still written)
    birth: Optional[float] = None   # when the run folder / file was made here (a copy put in again is newer)


def _is_run(entry: os.DirEntry) -> bool:
    name = entry.name.lower()
    try:
        if name.endswith(".d") and entry.is_dir():
            return True
        return name.endswith(".qgd") and entry.is_file()
    except OSError:
        return False


def folder_key(folder) -> str:
    """How the journal names a folder (case and separators do not matter)."""
    return os.path.normcase(os.path.abspath(str(folder)))


def birth(path) -> Optional[float]:
    """When ``path`` was made where it is (a folder copied in again is newer; one moved keeps its time)."""
    try:
        return getattr(os.stat(path), "st_birthtime", None)
    except OSError:
        return None


def _subfolders(folder: Path) -> list[Path]:
    out = []
    with os.scandir(folder) as it:
        for e in it:
            try:
                if e.is_dir() and not e.name.lower().endswith(".d") and not e.name.startswith("."):
                    out.append(Path(e.path))
            except OSError:
                continue
    return out


def _has_runs(folder: Path) -> bool:
    with os.scandir(folder) as it:
        return any(_is_run(e) for e in it)


def batch_folders(root, depth: int = 1, pattern: str = "*", ignore_older_days: float = 0,
                  now: Optional[float] = None, *, known: Optional[set] = None, skipped: Optional[list] = None,
                  seen: Optional[set] = None) -> list[Path]:
    """The batch folders below ``root``: those ``depth`` levels down (0 = ``root`` itself) that hold
    runs and, above them, any folder with runs lying loose in it (runs dropped straight into the
    watched folder are a batch of their own).

    ``pattern`` (``;``-separated globs) filters the names of the folders ``depth`` levels down.
    Folders unchanged for more than ``ignore_older_days`` are skipped (0 = never); with ``known``
    (journal folder keys) only those the journal knows already - a folder never seen before is new
    data, however old its files are. ``skipped`` collects ``(folder, why)`` of the folders with runs
    (or too old to look into) that are left out: "old" or "name". ``seen`` collects the keys of every
    folder listed, and of the folders whose listing worked as ``"listed:" + key``."""
    root = Path(root)
    now = time.time() if now is None else now
    depth = max(0, int(depth))
    levels = [[root]]
    for _ in range(depth):
        nxt = []
        for folder in levels[-1]:
            try:
                nxt += _subfolders(folder)
            except OSError:
                continue
            if seen is not None:
                seen.add("listed:" + folder_key(folder))
        levels.append(nxt)
    if seen is not None:
        seen.update(folder_key(f) for level in levels for f in level)
    pats = [p.strip() for p in (pattern or "*").split(";") if p.strip()] or ["*"]
    out = []
    for n, level in enumerate(levels):
        for folder in sorted(level):
            try:
                if n == depth and not any(fnmatch.fnmatch(folder.name.casefold(), p.casefold()) for p in pats):
                    if skipped is not None and _has_runs(folder):
                        skipped.append((folder, "name"))
                    continue
                if ignore_older_days and (known is None or folder_key(folder) in known) and \
                        now - folder.stat().st_mtime > ignore_older_days * 86400:
                    if skipped is not None and (n == depth or _has_runs(folder)):
                        skipped.append((folder, "old"))
                    continue
                if _has_runs(folder):
                    out.append(folder)
            except OSError:
                continue
    return out


def _walk(path: Path):
    stack = [path]
    while stack:
        d = stack.pop()
        with os.scandir(d) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                else:
                    yield e


def fingerprint(path: Path) -> tuple[str, float, bool, bool]:
    """``(hash of names, sizes and times, newest time, marker present, busy)`` of a run."""
    h = hashlib.sha1()
    newest, marker, busy = 0.0, False, False
    try:
        if path.is_file():
            st = path.stat()
            h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
            return h.hexdigest()[:16], st.st_mtime, True, False
        entries = []
        for e in _walk(path):
            try:
                st = e.stat()
            except OSError:
                busy = True
                continue
            rel = os.path.relpath(e.path, path)
            entries.append(f"{rel.lower()}:{st.st_size}:{st.st_mtime_ns}")
            newest = max(newest, st.st_mtime)
            if e.name.lower() in MARKERS:
                marker = True
        for line in sorted(entries):
            h.update(line.encode("utf-8", "replace"))
    except PermissionError:
        busy = True
    except OSError:
        busy = True
    return h.hexdigest()[:16], newest, marker, busy


def other_files(folder, limit: int = 40) -> list[str]:
    """The names of what lies in a batch folder beside the runs (sequence log, method, ...)."""
    try:
        with os.scandir(folder) as it:
            names = sorted((e.name for e in it if not _is_run(e)), key=str.casefold)
    except OSError:
        return []
    return names[:limit] + ([f"... {len(names) - limit} more"] if len(names) > limit else [])


def observe(folder) -> list[RunObs]:
    """Every run in the batch ``folder`` (finished or not)."""
    out = []
    try:
        with os.scandir(folder) as it:
            entries = [e for e in it if _is_run(e)]
    except OSError:
        return out
    for e in sorted(entries, key=lambda e: e.name.casefold()):
        p = Path(e.path)
        fp, newest, marker, busy = fingerprint(p)
        out.append(RunObs(p, p.stem.casefold(), p.name, fp, newest, marker, busy, birth(p)))
    return out


@dataclass
class Readiness:
    stable_scans: int = 2
    min_age_s: float = 120.0
    quiet_s: float = 1800.0


def readiness(prev: Optional[dict], obs: RunObs, now: float, cfg: Readiness, *, successor_started: bool,
              seq_finished: bool, folder_quiet: bool) -> tuple[str, int]:
    """``(state, stable count)`` of a run: "acquiring" | "ready".

    ``prev`` is the run's journal row from the last look (None the first time)."""
    if obs.busy:
        return "acquiring", 0
    same = prev is not None and prev.get("fingerprint") == obs.fingerprint
    count = (int(prev.get("stable_count") or 0) + 1) if same else 1
    old_enough = now - (obs.mtime or now) >= cfg.min_age_s
    ended = obs.marker or successor_started or seq_finished or folder_quiet
    ready = count >= max(1, cfg.stable_scans) and old_enough and ended
    return ("ready" if ready else "acquiring"), count

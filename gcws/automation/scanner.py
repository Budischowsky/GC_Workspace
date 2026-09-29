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


def _is_run(entry: os.DirEntry) -> bool:
    name = entry.name.lower()
    try:
        if name.endswith(".d") and entry.is_dir():
            return True
        return name.endswith(".qgd") and entry.is_file()
    except OSError:
        return False


def batch_folders(root, depth: int = 1, pattern: str = "*", ignore_older_days: float = 0,
                  now: Optional[float] = None) -> list[Path]:
    """Folders ``depth`` levels below ``root`` (0 = ``root`` itself) that hold runs.

    ``pattern`` (``;``-separated globs) filters the batch folder names; folders unchanged for
    more than ``ignore_older_days`` are skipped (0 = never)."""
    root = Path(root)
    now = time.time() if now is None else now
    level = [root]
    for _ in range(max(0, int(depth))):
        nxt = []
        for folder in level:
            try:
                with os.scandir(folder) as it:
                    nxt += [Path(e.path) for e in it if e.is_dir() and not e.name.lower().endswith(".d")
                            and not e.name.startswith(".")]
            except OSError:
                continue
        level = nxt
    pats = [p.strip() for p in (pattern or "*").split(";") if p.strip()] or ["*"]
    out = []
    for folder in sorted(level):
        if not any(fnmatch.fnmatch(folder.name.casefold(), p.casefold()) for p in pats):
            continue
        try:
            if ignore_older_days and now - folder.stat().st_mtime > ignore_older_days * 86400:
                continue
            with os.scandir(folder) as it:
                if any(_is_run(e) for e in it):
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
        out.append(RunObs(p, p.stem.casefold(), p.name, fp, newest, marker, busy))
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

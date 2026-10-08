"""The local copy: runs are copied from the (slow, network) watched folder to a folder on this PC
and processed from there.

The watcher copies every finished run of a batch folder, and the files beside the runs (the
sequence log), in a separate process (``python -m gcws --copy-runs <spec.json>``), so that a
slow network never holds the watcher up. The copy keeps the layout below the watched folder
(X:\\GC\\2610_A -> C:\\GC Data\\2610_A) and the files' times. Nothing is ever deleted: files that
are already there unchanged are skipped, files added to the copy stay. The watched folder is
only read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import time
import traceback
from pathlib import Path

from gcws.automation import scanner as SC

PART = ".gcwscopy"                    # a file being copied; renamed when it is complete
TIMEOUT_S = 1800                      # a copy process that takes longer is stopped (and tried again)

log = logging.getLogger("gcws.copy")


def local_batch(local_root, source_root, batch_folder) -> Path:
    """Where the copy of ``batch_folder`` (a folder below ``source_root``) goes."""
    try:
        rel = Path(batch_folder).relative_to(Path(source_root))
    except ValueError:
        rel = Path(Path(batch_folder).name)
    return Path(local_root) / rel


def _long(path) -> str:
    """``path`` usable beyond 260 characters on Windows."""
    p = os.path.abspath(str(path))
    if os.name != "nt" or len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


def _same(src: os.stat_result, dst: Path) -> bool:
    try:
        st = os.stat(_long(dst))
    except OSError:
        return False
    return st.st_size == src.st_size and abs(st.st_mtime - src.st_mtime) < 2.0


def _writable(path: Path) -> None:
    """Raw data are often read-only; a copy of them must be replaceable."""
    try:
        os.chmod(_long(path), stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def _copy_file(src: Path, dst: Path, st: os.stat_result) -> int:
    """Copies one file under a temporary name and renames it when complete; returns its size."""
    os.makedirs(_long(dst.parent), exist_ok=True)
    tmp = dst.with_name(dst.name + PART)
    if os.path.exists(_long(tmp)):
        _writable(tmp)
        os.remove(_long(tmp))
    shutil.copy2(_long(src), _long(tmp))
    if os.stat(_long(tmp)).st_size != st.st_size:
        raise OSError(f"incomplete copy of {src}")
    if os.path.exists(_long(dst)):
        _writable(dst)
    os.replace(_long(tmp), _long(dst))
    return st.st_size


def copy_tree(src, dst) -> tuple[int, int]:
    """Copies what of ``src`` (a run folder or file) is missing in ``dst`` or differs from it (size or
    time); returns (files copied, bytes). Files only in ``dst`` stay."""
    src, dst = Path(src), Path(dst)
    if src.is_file():
        st = os.stat(_long(src))
        return (0, 0) if _same(st, dst) else (1, _copy_file(src, dst, st))
    if not src.is_dir():
        raise FileNotFoundError(f"not found: {src}")
    os.makedirs(_long(dst), exist_ok=True)
    files = size = 0
    for e in SC._walk(src):
        if e.name.endswith(PART):
            continue
        st = e.stat()
        target = dst / os.path.relpath(e.path, src)
        if _same(st, target):
            continue
        size += _copy_file(Path(e.path), target, st)
        files += 1
    return files, size


def extra_files(folder) -> list[os.DirEntry]:
    """The files beside the runs of a batch folder (sequence log, sequence file ...); no folders."""
    try:
        with os.scandir(folder) as it:
            return sorted((e for e in it if e.is_file() and not SC._is_run(e) and not e.name.startswith(".")
                           and not e.name.endswith(PART)), key=lambda e: e.name.casefold())
    except OSError:
        return []


def extras_signature(folder) -> str:
    """Changes when a file beside the runs is added or changed (``""``: there is none)."""
    h = hashlib.sha1()
    found = False
    for e in extra_files(folder):
        try:
            st = e.stat()
        except OSError:
            continue
        h.update(f"{e.name.casefold()}:{st.st_size}:{st.st_mtime_ns};".encode("utf-8", "replace"))
        found = True
    return h.hexdigest()[:16] if found else ""


def copy_extras(src, dst) -> tuple[int, int]:
    files = size = 0
    for e in extra_files(src):
        n, b = copy_tree(Path(e.path), Path(dst) / e.name)
        files, size = files + n, size + b
    return files, size


def run_copy(spec: dict) -> dict:
    """One copy pass of a batch folder (the copy process): ``spec`` from the watcher, the result for it.

    ``spec``: src, dst (batch folders), runs [{stem, name, fingerprint}], extras (bool). A run counts
    as copied only when it did not change meanwhile (its fingerprint is the one the watcher saw)."""
    t0 = time.time()
    src, dst = Path(spec["src"]), Path(spec["dst"])
    out = {"copied": {}, "failed": {}, "files": 0, "bytes": 0, "extras": None, "extras_error": ""}
    for r in spec.get("runs") or []:
        log.info("copying %s", r["name"])
        try:
            n, b = copy_tree(src / r["name"], dst / r["name"])
            fp = SC.fingerprint(src / r["name"])[0]
        except OSError as exc:
            out["failed"][r["stem"]] = str(exc)
            log.error("%s: %s", r["name"], exc)
            continue
        if r.get("fingerprint") and fp != r["fingerprint"]:
            out["failed"][r["stem"]] = "the run changed while it was copied"
            continue
        out["copied"][r["stem"]] = fp
        out["files"] += n
        out["bytes"] += b
    if spec.get("extras", True):
        sig = extras_signature(src)
        try:
            n, b = copy_extras(src, dst)
            out["extras"] = sig
            out["files"] += n
            out["bytes"] += b
        except OSError as exc:
            out["extras_error"] = str(exc)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def ensure_runs(src, dst, names, progress=log.info) -> list[str]:
    """Copies the runs ``names`` that are missing in ``dst``, or only partly there (a copy stopped
    halfway), from ``src`` (a job that finds its local copy deleted); returns the names copied. A run
    copied completely, or one the watched folder cannot be reached for, is used as it is."""
    src, dst = Path(src), Path(dst)
    done = []
    for name in names:
        there = (dst / name).exists()
        if there and not (src / name).exists():
            continue                                   # e.g. the network drive without the VPN
        if not there:
            progress(f"copying {name} from the watched folder")
        try:
            if copy_tree(src / name, dst / name)[0] or not there:
                done.append(name)
        except OSError as exc:
            log.warning("could not copy %s: %s", name, exc)
    if done:
        try:
            copy_extras(src, dst)
        except OSError as exc:
            log.warning("could not copy the files beside the runs: %s", exc)
    return done


def main(argv) -> None:
    """``--copy-runs <spec.json>``: the copy process; writes ``result.json`` beside the spec, never returns."""
    code = 0
    try:
        spec_path = Path(argv[2])
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        logging.basicConfig(filename=str(spec_path.parent / "copy.log"), level=logging.INFO, force=True,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        result = run_copy(spec)
        tmp = spec_path.parent / "result.json.tmp"
        tmp.write_text(json.dumps(result), encoding="utf-8")
        os.replace(tmp, spec_path.parent / "result.json")
    except Exception:  # noqa: BLE001 - the watcher sees the missing result
        log.error(traceback.format_exc())
        traceback.print_exc()
        code = 2
    logging.shutdown()
    sys.stdout.flush() if sys.stdout else None
    os._exit(code)

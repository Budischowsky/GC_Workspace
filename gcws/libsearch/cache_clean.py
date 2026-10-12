"""Old library indexes in ``DATA/libcache/.cache`` are removed.

An index folder is named by the fingerprint of its library files (path, size, time), so a changed or
edited library, and every other copy of the code (the NIST reader's own file is part of a NIST
library's fingerprint), builds a new one of up to 250 MB; the old ones were never removed. When an
engine has opened its libraries, the folders it uses are marked as used now, and the index folders
not used for ``GRACE_DAYS`` (a library disabled for a while, another copy of the code) are removed.
A folder another process still has open cannot be renamed on Windows: it stays until a later load."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import msp_cache
import spectral_index

GRACE_DAYS = 7

#: index folders (``spectral_index``) and parsed MSP files (``msp_cache``); nothing else is touched
_ENTRY = re.compile(r"(?:[0-9a-f]{64}|msp-[0-9a-f]{32})(?:\.removing-\d+)?")


def index_folder(root, files) -> Path:
    """The folder ``spectral_index.open_index(root, files, ...)`` keeps the index of ``files`` in."""
    fingerprint = hashlib.sha256(json.dumps(
        [(spectral_index._cache_name(p, root), p.stat().st_size, p.stat().st_mtime_ns) for p in map(Path, files)]
        + [spectral_index.CACHE_VERSION]).encode()).hexdigest()
    return Path(root) / ".cache" / fingerprint


def used_folders(root, native_files: list, msp_files: list) -> set[Path]:
    """The cache folders of an engine over the native libraries' files and the MSP files."""
    out = set()
    for files in native_files:
        out.add(index_folder(root, files))
    for path in map(Path, msp_files):
        out.add(index_folder(root, [path]))
        out.add(msp_cache._folder(root, path))
    return out


def clean(root, used: set[Path], grace_days: float = GRACE_DAYS) -> list[str]:
    """Mark ``used`` as used now and remove the other cache folders not used for ``grace_days``;
    returns the names removed."""
    cache = Path(root) / ".cache"
    used = {p.name for p in used}
    limit = time.time() - grace_days * 86400
    removed = []
    try:
        entries = [e for e in cache.iterdir() if e.is_dir() and _ENTRY.fullmatch(e.name)]
    except OSError:
        return removed
    for entry in entries:
        try:
            if entry.name in used:
                os.utime(entry)
                continue
            if ".removing-" not in entry.name:
                if entry.stat().st_mtime > limit:
                    continue
                # refused while a process has a file of it open: it stays whole
                target = entry.with_name(f"{entry.name}.removing-{os.getpid()}")
                os.rename(entry, target)
                entry = target
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name.partition(".removing-")[0])
    return removed

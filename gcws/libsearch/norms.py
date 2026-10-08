"""The reference norms of the library prefilter, resumable from one search range to the next.

The vendored ``Engine.shard_norms(index, minimum, maximum)`` sums, per reference spectrum, the
squared prefilter weights ``I * (m/100)^2`` of its peaks with ``minimum <= m <= maximum``: one
``bincount`` over the whole posting range of the inverted index, again for every new range. A
search without a fixed m/z range takes the range from the spectrum, so nearly every search of
consensus spectra paid that pass (0.4 s for NIST05a.L).

``bincount`` adds each reference's terms in posting order, that is by ascending m/z. Adding mass
by mass (``out[rows] += terms``; a reference has at most one posting per mass) performs the same
float64 additions in the same order, so the sums are the same bit for bit - and the running sum
after ``maximum`` is the start for any larger ``maximum`` with the same ``minimum``. Searches taken
in ascending range order therefore need about one pass per lower bound.

The snapshots of the large (memory-mapped) libraries are also kept on disk, in ``.norms/<the
library index's fingerprint>`` below the library cache: every automation job is a process of its
own and searches the same few ranges (the acquisition range; consensus spectra start at m/z 39,
41, 43, ...), so a later process resumes from the nearest stored snapshot instead of starting at
zero. The files are the float64 arrays themselves (``np.save``), so a loaded snapshot is the
computed one bit for bit. The least recently used files go once the store exceeds ``budget``.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np

#: bytes of norm snapshots kept on disk (all libraries together)
DISK_BUDGET = 400 * 2 ** 20


def _fold_mass(out: np.ndarray, shard: dict, m: int) -> None:
    """Add the terms of mass ``m`` to the running sums ``out`` (the vendored arithmetic)."""
    a, b = int(shard['pointers'][m]), int(shard['pointers'][m + 1])
    if b > a:
        f = np.float64(m) / 100
        out[shard['rows'][a:b]] += np.asarray(shard['intensities'][a:b], dtype=np.float64) * (f * f)


def _fingerprint(shard: dict) -> Optional[str]:
    """The name of a memory-mapped shard's index folder (the library files' fingerprint)."""
    name = getattr(shard.get('rows'), 'filename', None)
    return Path(name).parent.name if name else None


class DiskNorms:
    """Norm snapshots on disk: ``<root>/<fingerprint>/<minimum>_<maximum>.npy``."""

    def __init__(self, root: Path, budget: int = DISK_BUDGET):
        self.root = Path(root)
        self.budget = budget

    def _folder(self, fingerprint: str) -> Path:
        return self.root / fingerprint

    def maxima(self, fingerprint: str, minimum: int) -> list[int]:
        """The stored maxima of ``minimum``."""
        out = []
        try:
            with os.scandir(self._folder(fingerprint)) as entries:
                for e in entries:
                    lo, _, rest = e.name.partition('_')
                    if lo == str(minimum) and rest.endswith('.npy') and rest[:-4].isdigit():
                        out.append(int(rest[:-4]))
        except OSError:
            pass
        return out

    def load(self, fingerprint: str, minimum: int, maximum: int, count: int) -> Optional[np.ndarray]:
        path = self._folder(fingerprint) / f'{minimum}_{maximum}.npy'
        try:
            out = np.load(path, allow_pickle=False)
        except OSError:
            return None
        except ValueError:
            out = None
        if out is None or out.dtype != np.float64 or out.shape != (count,):
            # unreadable, or of an index rebuilt in place for another count: removed, so that
            # ``save`` stores the sums computed now instead of keeping the useless file
            try:
                path.unlink()
            except OSError:
                pass
            return None
        try:
            os.utime(path)                       # recently used: pruned last
        except OSError:
            pass
        return out

    def save(self, fingerprint: str, minimum: int, maximum: int, values: np.ndarray) -> None:
        folder = self._folder(fingerprint)
        path = folder / f'{minimum}_{maximum}.npy'
        if path.exists():                        # another process stored the same sums
            return
        tmp = folder / f'{minimum}_{maximum}.{os.getpid()}.{threading.get_ident()}.tmp'
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with open(tmp, 'wb') as fh:
                np.save(fh, values, allow_pickle=False)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return
        self.prune()

    def prune(self) -> None:
        """Remove the least recently used snapshots beyond the budget (files in use elsewhere stay)."""
        files = []
        try:
            for folder in self.root.iterdir():
                if folder.is_dir():
                    for p in folder.glob('*.npy'):
                        st = p.stat()
                        files.append((st.st_mtime_ns, st.st_size, p))
        except OSError:
            return
        total = sum(size for _t, size, _p in files)
        for _t, size, p in sorted(files):
            if total <= self.budget:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass


class NormFolds:
    """Snapshots of the running sums, keyed ``(shard index, minimum, maximum)``, at most ``limit``
    in memory; with ``disk``, those of memory-mapped shards are also kept there."""

    def __init__(self, limit: int = 64, disk: Optional[DiskNorms] = None):
        self.limit = limit
        self.disk = disk
        self.snapshots: "OrderedDict[tuple[int, int, int], np.ndarray]" = OrderedDict()
        self._lock = threading.Lock()
        self._shard_locks: dict[int, threading.Lock] = {}

    def clear(self) -> None:
        with self._lock:
            self.snapshots.clear()

    def get(self, shard: dict, index: int, minimum: int, maximum: int) -> np.ndarray:
        key = (index, minimum, maximum)
        with self._lock:
            lock = self._shard_locks.setdefault(index, threading.Lock())
        with lock:
            with self._lock:
                if key in self.snapshots:
                    self.snapshots.move_to_end(key)
                    return self.snapshots[key]
                below = [k[2] for k in self.snapshots if k[0] == index and k[1] == minimum and k[2] < maximum]
                start = max(below) if below else None
                out = self.snapshots[(index, minimum, start)].copy() if start is not None else None
            fingerprint = _fingerprint(shard) if self.disk is not None else None
            if fingerprint is not None:
                stored = [m for m in self.disk.maxima(fingerprint, minimum)
                          if m <= maximum and (start is None or m > start)]
                for m in sorted(stored, reverse=True):          # the nearest one that reads back
                    loaded = self.disk.load(fingerprint, minimum, m, int(shard['count']))
                    if loaded is not None:
                        out, start = loaded, m
                        break
            if out is None:
                out, first = np.zeros(int(shard['count'])), minimum
            else:
                first = start + 1
            for m in range(first, maximum + 1):
                _fold_mass(out, shard, m)
            if fingerprint is not None and first <= maximum:
                self.disk.save(fingerprint, minimum, maximum, out)
            with self._lock:
                self.snapshots[key] = out
                while len(self.snapshots) > self.limit:
                    self.snapshots.popitem(last=False)
            return out

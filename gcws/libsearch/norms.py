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
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np


def _fold_mass(out: np.ndarray, shard: dict, m: int) -> None:
    """Add the terms of mass ``m`` to the running sums ``out`` (the vendored arithmetic)."""
    a, b = int(shard['pointers'][m]), int(shard['pointers'][m + 1])
    if b > a:
        f = np.float64(m) / 100
        out[shard['rows'][a:b]] += np.asarray(shard['intensities'][a:b], dtype=np.float64) * (f * f)


class NormFolds:
    """Snapshots of the running sums, keyed ``(shard index, minimum, maximum)``, at most ``limit``."""

    def __init__(self, limit: int = 64):
        self.limit = limit
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
            if out is None:
                out, first = np.zeros(int(shard['count'])), minimum
            else:
                first = start + 1
            for m in range(first, maximum + 1):
                _fold_mass(out, shard, m)
            with self._lock:
                self.snapshots[key] = out
                while len(self.snapshots) > self.limit:
                    self.snapshots.popitem(last=False)
            return out

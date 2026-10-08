"""The PBM peak statistics of the loaded libraries, without a pass over every library per process.

The vendored ``Engine.statistics`` walks every m/z of every shard (a NumPy call per m/z, about
1 s for 1.8 million spectra) in every process that searches - each automation job pays it again.
The statistics are sums of whole-number counts per shard: ``occurrences[m]`` (peaks of at least
``MIN_ABUNDANCE`` at m/z m) and ``abundance[a]`` (such peaks whose value floors to a). Here each
shard's counts are computed once and kept beside the shard's index
(``gcws_pbm_counts.npz`` in its cache folder, which is named by the library files' fingerprint),
so later processes read them. Whole numbers below 2^53 add exactly in float64 in any order, so
:class:`PeakStatistics` gets the same arrays as the vendored loop.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np

import gcws.libsearch  # noqa: F401  (vendor on sys.path)
from pbm import MIN_ABUNDANCE, PeakStatistics

FILE = "gcws_pbm_counts.npz"


def _folder(shard: dict) -> Optional[Path]:
    """The cache folder of a memory-mapped shard (small shards are read into memory: no file)."""
    name = getattr(shard.get("intensities"), "filename", None)
    return Path(name).parent if name else None


def compute(shard: dict) -> tuple[np.ndarray, np.ndarray]:
    """(occurrences per m/z, abundance counts per percent 0-100) of one shard, as int64 (the
    vendored loop, one m/z at a time)."""
    intensities, pointers = shard["intensities"], shard["pointers"]
    occurrences = np.zeros(len(pointers) - 1, np.int64)
    abundance = np.zeros(101, np.int64)
    for m in np.flatnonzero(np.diff(pointers)):
        values = np.asarray(intensities[pointers[m]:pointers[m + 1]], dtype=np.float64)
        values = values[values >= MIN_ABUNDANCE]
        occurrences[m] += len(values)
        abundance += np.bincount(np.minimum(values.astype(np.int64), 100), minlength=101)
    return occurrences, abundance


def counts(shard: dict) -> tuple[np.ndarray, np.ndarray]:
    """``compute(shard)``, read from / written to the shard's cache folder when it has one."""
    folder = _folder(shard)
    path = folder / FILE if folder is not None else None
    if path is not None:
        try:
            with np.load(path, allow_pickle=False) as saved:
                occ, abd = saved["occurrences"], saved["abundance"]
            if occ.shape == (len(shard["pointers"]) - 1,) and abd.shape == (101,):
                return occ, abd
        except (OSError, ValueError, KeyError):
            pass
    occ, abd = compute(shard)
    if path is not None:
        tmp = path.with_name(f"{FILE}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as fh:
                np.savez(fh, occurrences=occ, abundance=abd)
            os.replace(tmp, path)                  # another process may write the same file
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
    return occ, abd


def statistics(shards: list, count: int) -> PeakStatistics:
    """``Engine.statistics()`` of ``shards`` (``count`` spectra in all)."""
    occurrences, abundance = np.zeros(10001), np.zeros(101)
    for shard in shards:
        occ, abd = counts(shard)
        occurrences[:len(occ)] += occ
        abundance += abd
    return PeakStatistics(count, occurrences, abundance)

"""Spectral similarity of two EI spectra, as mzmine computes it for GC-EI data.

Ported from mzmine (MIT, Copyright (c) 2004-2025 The mzmine Development Team),
``util/scans/similarity``:

* ``Weights`` -- each intensity is taken as ``I ** intensity * mz ** mz``;
  ``NIST_GC`` (0.6, 3) is what mzmine's GC-EI workflow uses.
* ``ScanAlignment.align`` + ``HandleUnmatchedSignalOptions.KEEP_ALL_AND_MATCH_TO_ZERO``
  -- signals of one spectrum without a partner in the other stay in with 0 on
  the other side, so they lower the score. Spectra here are nominal mass, so
  two signals match when their rounded m/z is equal.
* ``WeightedCosineSpectralSimilarity`` -- the cosine of the weighted vectors
  (not squared, unlike the NIST match factor).
* ``CompositeCosineSpectralSimilarity`` --
  ``(queryN * cos + overlap * relative) / (queryN + overlap)`` where
  ``relative`` compares the intensity ratios of neighbouring matched signals.
  As in mzmine, ``relative`` is divided by the overlap, not by the number of
  neighbour pairs: two identical spectra with ``n`` common signals score
  ``1 - 1/(2n)`` (0.9 for 5 signals, 0.99 for 50), which keeps a match on very
  few ions below a match on many.

``library`` and ``query`` follow mzmine's argument order; in its GC aligner the
row being added is the library and the aligned (base) row the query.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from gcws.ms.similarity import as_arrays


@dataclass(frozen=True)
class Weights:
    name: str
    intensity: float
    mz: float

    def apply(self, mz, intensity):
        return np.power(intensity, self.intensity) * np.power(mz, self.mz)


NONE = Weights("NONE", 1.0, 0.0)
SQRT = Weights("SQRT", 0.5, 0.0)
MASSBANK = Weights("MassBank", 0.5, 2.0)
NIST_GC = Weights("NIST (GC)", 0.6, 3.0)
NIST11 = Weights("NIST11 (LC)", 0.53, 1.3)
WEIGHTS = {w.name: w for w in (NONE, SQRT, MASSBANK, NIST_GC, NIST11)}


@dataclass(frozen=True)
class Similarity:
    score: float        # the similarity (composite or weighted cosine), 0..1
    cosine: float       # the weighted cosine alone
    overlap: int        # signals present in both spectra
    relative: float     # the neighbour-ratio factor (composite only; 0 otherwise)


def nominal(spec) -> tuple[np.ndarray, np.ndarray]:
    """``(mz, intensity)`` with one entry per nominal mass (duplicates summed), sorted by m/z."""
    mz, ab = as_arrays(spec)
    if mz.size == 0:
        return mz, ab
    masses, inverse = np.unique(mz, return_inverse=True)
    acc = np.zeros(masses.size)
    np.add.at(acc, inverse, ab)
    return masses, acc


def aligned(library, query) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Both spectra on the union of their masses (0 where a signal is missing):
    mzmine's alignment with ``KEEP_ALL_AND_MATCH_TO_ZERO``."""
    ml, il = nominal(library)
    mq, iq = nominal(query)
    masses = np.union1d(ml, mq)
    vl = np.zeros(masses.size)
    vq = np.zeros(masses.size)
    vl[np.searchsorted(masses, ml)] = il
    vq[np.searchsorted(masses, mq)] = iq
    return masses, vl, vq


def _cosine(masses, vl, vq, weights: Weights) -> float:
    wl = weights.apply(masses.astype(float), vl)
    wq = weights.apply(masses.astype(float), vq)
    nl, nq = float(np.linalg.norm(wl)), float(np.linalg.norm(wq))
    if nl <= 0 or nq <= 0:
        return 0.0
    return float(np.dot(wl, wq) / (nl * nq))


def relative_neighbour_factor(masses, vl, vq) -> float:
    """mzmine ``calcRelativeNeighbourFactor``: over the matched signals sorted by m/z, the sum of
    ``min(rL, rQ) / max(rL, rQ)`` of consecutive intensity ratios, divided by the overlap."""
    both = (vl > 0) & (vq > 0)
    overlap = int(both.sum())
    if overlap == 0:
        return math.nan
    il, iq = vl[both], vq[both]          # already in m/z order
    factor = 0.0
    for i in range(1, overlap):
        rl = il[i] / il[i - 1]
        rq = iq[i] / iq[i - 1]
        factor += min(rl, rq) / max(rl, rq)
    return factor / overlap


def weighted_cosine(library, query, weights: Weights = NIST_GC, min_match: int = 0,
                    min_cos: float = 0.0) -> Optional[Similarity]:
    """mzmine ``WeightedCosineSpectralSimilarity``: None below ``min_match`` common signals or
    below ``min_cos``."""
    masses, vl, vq = aligned(library, query)
    overlap = int(((vl > 0) & (vq > 0)).sum())
    if masses.size == 0 or overlap < min_match:
        return None
    cos = _cosine(masses, vl, vq, weights)
    if cos < min_cos:
        return None
    return Similarity(cos, cos, overlap, 0.0)


def composite_cosine(library, query, weights: Weights = NIST_GC, min_match: int = 0,
                     min_cos: float = 0.0) -> Optional[Similarity]:
    """mzmine ``CompositeCosineSpectralSimilarity.getSimilarity``: None below ``min_match`` common
    signals, with no common signal (mzmine's 0/0 fails the test) or below ``min_cos``."""
    masses, vl, vq = aligned(library, query)
    query_n = int((vq > 0).sum())
    overlap = int(((vl > 0) & (vq > 0)).sum())
    if overlap < min_match or overlap == 0:
        return None
    relative = relative_neighbour_factor(masses, vl, vq)
    cos = _cosine(masses, vl, vq, weights)
    composite = (query_n * cos + overlap * relative) / (query_n + overlap)
    if not composite >= min_cos:
        return None
    return Similarity(float(composite), cos, overlap, float(relative))


def score(library, query, weights: Weights = NIST_GC) -> float:
    """The composite cosine as a number (0 when the spectra share no signal)."""
    s = composite_cosine(library, query, weights)
    return s.score if s is not None else 0.0

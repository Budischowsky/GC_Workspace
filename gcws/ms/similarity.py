"""Spectral similarity of nominal-mass spectra.

``cosine`` is the weighted dot product used by NIST/AMDIS style searches:
each intensity is taken as ``m/z ** mz_pow * I ** int_pow`` before the
normalised dot product. ``match_factor`` is the same number on the familiar
0-999 scale. Spectra may be given as ``{mz: abundance}`` dicts, ``(mz, ab)``
array pairs, lists of ``(mz, ab)`` pairs or objects with ``mz``/``ab``.
"""
from __future__ import annotations

import numpy as np


def as_arrays(spec) -> tuple[np.ndarray, np.ndarray]:
    if spec is None:
        return np.zeros(0, np.int64), np.zeros(0)
    if hasattr(spec, "mz") and hasattr(spec, "ab"):
        mz, ab = spec.mz, spec.ab
    elif isinstance(spec, dict):
        mz, ab = list(spec.keys()), list(spec.values())
    elif isinstance(spec, tuple) and len(spec) == 2 and np.ndim(spec[0]) == 1:
        mz, ab = spec
    else:
        pairs = list(spec)
        mz = [p[0] for p in pairs]
        ab = [p[1] for p in pairs]
    mz = np.rint(np.asarray(mz, dtype=float)).astype(np.int64)
    ab = np.asarray(ab, dtype=float)
    keep = ab > 0
    return mz[keep], ab[keep]


def _aligned(a, b, mz_pow: float, int_pow: float, min_mz: int | None):
    ma, aa = as_arrays(a)
    mb, ab = as_arrays(b)
    if min_mz is not None:
        ka, kb = ma >= min_mz, mb >= min_mz
        ma, aa, mb, ab = ma[ka], aa[ka], mb[kb], ab[kb]
    masses = np.union1d(ma, mb)
    if masses.size == 0:
        return None, None
    va = np.zeros(masses.size)
    vb = np.zeros(masses.size)
    va[np.searchsorted(masses, ma)] = aa
    vb[np.searchsorted(masses, mb)] = ab
    w = masses.astype(float) ** mz_pow
    return w * va ** int_pow, w * vb ** int_pow


def cosine(a, b, mz_pow: float = 1.0, int_pow: float = 0.5, min_mz: int | None = None) -> float:
    """Weighted cosine similarity 0..1 (0 when either spectrum is empty)."""
    va, vb = _aligned(a, b, mz_pow, int_pow, min_mz)
    if va is None:
        return 0.0
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na <= 0 or nb <= 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def match_factor(a, b, **kw) -> float:
    """Similarity on the 0-999 scale of library searches."""
    return 999.0 * cosine(a, b, **kw) ** 2

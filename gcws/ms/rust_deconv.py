"""Rust kernels of the array deconvolution (``rust/gcws_rust``), chosen by ``GCWS_RUST_DECONV``.

Unset, every part is used whenever the extension is built; ``off`` (or ``python``) keeps the
Python code. Otherwise a ``+``-separated list of:

* ``ions``  - ``deconv_fast._perceive_ions`` after smoothing: maxima, prominence gates, flanking
  minima, sub-scan apex, half-height width and score of every ion peak of a window.
* ``links`` - the Pearson r of ``deconv_fast._links`` (seed against candidate over the seed's
  slice), with numpy's pairwise row sums and its ``einsum`` summation order.
* ``components`` - all of ``deconv_fast._perceive_components``: the seed blocks, their links
  (as ``links``) and ``_merge_groups``.

Every part reproduces the numpy results bit for bit, so the components, spectra and the probe's
borderline decisions are unchanged. The joint purification (``nnls_columns``) stays in numpy:
its LAPACK solves decide the reported areas to the last digit.
"""
from __future__ import annotations

import os

import numpy as np

import gc_deconv as legacy

from gcws.ms import deconv_fast as F

try:
    import gcws_rust as _rust
except ImportError:  # the extension is not built
    _rust = None
if _rust is not None and not hasattr(_rust, "perceive_ions"):
    _rust = None

#: the parts used when ``GCWS_RUST_DECONV`` is not set
DEFAULT = "ions+components"

_installed: dict = {}


def parts() -> set:
    if _rust is None:
        return set()
    value = os.environ.get("GCWS_RUST_DECONV", DEFAULT).strip().lower()
    if value in ("off", "python", "0", "no"):
        return set()
    return {p for p in value.replace(",", "+").split("+") if p}


def install() -> set:
    """Switch the chosen parts into ``deconv_fast`` (idempotent); returns them."""
    chosen = parts()
    _installed.setdefault("ions", F._perceive_ions)
    _installed.setdefault("links", F._links)
    _installed.setdefault("components", F._perceive_components)
    F._perceive_ions = _perceive_ions if "ions" in chosen else _installed["ions"]
    F._links = _links if "links" in chosen else _installed["links"]
    F._perceive_components = _perceive_components if "components" in chosen else _installed["components"]
    return chosen


def _perceive_ions(x, mzs, sigma, nonzero, params):
    """``deconv_fast._perceive_ions`` with the perception after smoothing in Rust."""
    cols = np.flatnonzero((nonzero >= legacy.MIN_ION_SCANS)
                          & ~(x.max(axis=0) < params.noise_factor * sigma))
    if cols.size == 0:
        return None
    s = F._smooth(x, cols)
    sig = np.ascontiguousarray(np.asarray(sigma, dtype=float)[cols])
    row, apex, apex_sub, base, width, s_n, score, left, right = _rust.perceive_ions(
        np.ascontiguousarray(s, dtype=float), sig, float(params.noise_factor))
    if row.size == 0:
        return None
    return F._Peaks(smooth=s, row=row, mz=mzs[cols][row], apex=apex, apex_sub=apex_sub, base=base,
                    width_half=width, s_n=s_n, score=score, left=left, right=right)


def _perceive_components(p, n: int, params) -> list[list[int]]:
    """``deconv_fast._perceive_components`` in Rust."""
    smooth = p.smooth
    if smooth.shape[1] != n or smooth.dtype != np.float64 or not smooth.flags.c_contiguous:
        return _installed["components"](p, n, params)
    i64 = lambda a: np.ascontiguousarray(a, dtype=np.int64)  # noqa: E731
    f64 = lambda a: np.ascontiguousarray(a, dtype=np.float64)  # noqa: E731
    return _rust.perceive_components(smooth, i64(p.row), i64(p.mz), i64(p.apex), f64(p.apex_sub),
                                     f64(p.width_half), f64(p.score), int(n), float(params.apex_tolerance),
                                     float(params.shape_r), int(params.min_ions), int(legacy.CORR_MIN_HALF_WIDTH),
                                     float(legacy.MIN_SEPARATION_SCANS), int(legacy.MAX_COMPONENTS))


def _links(seeds, p, params, rank, used, by_apex, first, last, lo, hi, n) -> dict[int, list[int]]:
    """``deconv_fast._links`` with the Pearson r of the pairs in Rust."""
    smooth = p.smooth
    if smooth.shape[1] != n or smooth.dtype != np.float64 or not smooth.flags.c_contiguous:
        return _installed["links"](seeds, p, params, rank, used, by_apex, first, last, lo, hi, n)
    count = last[seeds] - first[seeds]
    total = int(count.sum())
    if total == 0:
        return {}
    seed = np.repeat(seeds, count)
    pos = np.arange(total) - np.repeat(np.cumsum(count) - count, count) + np.repeat(first[seeds], count)
    cand = by_apex[pos]
    ok = (rank[cand] > rank[seed]) & ~used[cand]
    seed, cand = seed[ok], cand[ok]
    ok = ~(np.abs(p.apex_sub[cand] - p.apex_sub[seed]) > params.apex_tolerance)
    seed, cand = seed[ok], cand[ok]
    if seed.size == 0:
        return {}
    i64 = lambda a: np.ascontiguousarray(a, dtype=np.int64)  # noqa: E731
    r = _rust.link_r(smooth, i64(p.row[seed]), i64(p.row[cand]), i64(lo[seed]), i64(hi[seed]))
    keep = ~(r < params.shape_r)
    seed, cand = seed[keep], cand[keep]
    o = np.lexsort((rank[cand], seed))
    out: dict[int, list[int]] = {}
    for k, j in zip(seed[o].tolist(), cand[o].tolist()):
        out.setdefault(k, []).append(j)
    return out


install()

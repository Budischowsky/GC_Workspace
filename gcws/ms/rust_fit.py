"""The component fit's grid search in Rust (``rust/gcws_rust``), chosen by ``GCWS_RUST_FIT``.

Unset (or ``on``), :func:`fit_trace_uncached` replaces ``component_fit.fit_trace_uncached``
whenever the extension is built; ``off`` (or ``python``) keeps the Python code.

The grid search evaluates a few hundred (shift, width factor) points per fit, each a design
matrix of PCHIP curves and an NNLS. In Rust the points run in parallel and the NNLS is the NIAS
engine's Lawson-Hanson with a Jacobi SVD for the pseudo-inverse instead of LAPACK's: the
residuals agree with numpy's to about 1e-15 relative. Everything else - the coarse and fine
grids, the first-minimum rule, the other minima, the tie rule and the final fit at the chosen
point (``component_fit.solve``) - is the Python code, so the result is the Python result unless
two grid points tie to the last digits.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

import numpy as np

from gcws.ms import component_fit as CF

try:
    import gcws_rust as _rust
except ImportError:  # the extension is not built
    _rust = None
if _rust is not None and not hasattr(_rust, "grid_residuals"):
    _rust = None

_installed: dict = {}


def enabled() -> bool:
    if _rust is None:
        return False
    return os.environ.get("GCWS_RUST_FIT", "on").strip().lower() not in ("off", "python", "0", "no")


def install() -> bool:
    """Switch the Rust grid search in or out (idempotent); True when it is in."""
    _installed.setdefault("fit_trace_uncached", CF.fit_trace_uncached)
    on = enabled()
    CF.fit_trace_uncached = fit_trace_uncached if on else _installed["fit_trace_uncached"]
    return on


def _shape_arrays(shapes: Sequence[CF.Shape]) -> list:
    f64 = lambda a: np.ascontiguousarray(a, dtype=np.float64)  # noqa: E731
    return [(float(s.rt), f64(s.t), f64(s.y), f64(s.d)) for s in shapes]


def fit_trace_uncached(t, y, shapes: Sequence[CF.Shape], shift0: float, scan_dt: Optional[float] = None,
                       mask: Optional[np.ndarray] = None) -> CF.TraceFit:
    """``component_fit.fit_trace_uncached`` with the grid points' residuals from Rust."""
    if not shapes:
        raise ValueError("no component shapes to fit")
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    use = np.ones(t.size, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    tu, yu = np.ascontiguousarray(t[use]), np.ascontiguousarray(y[use])
    dt = scan_dt or min(s.scan_dt for s in shapes)
    arrays = _shape_arrays(shapes)

    def residuals(pairs, summed):
        # the width factor as component_fit._curves takes it: float(np.exp(log_k))
        shifts = [float(shift) for shift, _log_k in pairs]
        stretches = [float(np.exp(log_k)) for _shift, log_k in pairs]
        return _rust.grid_residuals(arrays, tu, yu, shifts, stretches, summed)

    def ss_many(pairs):
        return residuals(pairs, False)

    best, profile = CF._search(ss_many, shift0, dt)
    found = [CF._refine(ss_many, best, dt)]
    if len(shapes) > 1:
        found += [CF._refine(ss_many, start, dt) for start in CF._other_minima(profile, best, yu)]
    fits = [CF.solve(t, y, shapes, shift, float(np.exp(log_k)), use) for _ss, shift, log_k in found]
    top = max(f.r2 for f in fits)
    tied = [f for f in fits if f.r2 >= top - CF.ALIGN_TIE_R2]
    if len(tied) == 1:
        return tied[0]

    def ms_many(pairs):
        return residuals(pairs, True)

    ms_shift = CF._refine(ms_many, CF._search(ms_many, shift0, dt)[0], dt)[1]
    return min(tied, key=lambda f: abs(f.shift - ms_shift))


install()

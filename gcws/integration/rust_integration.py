"""Rust kernels for the integrator (``rust/gcws_rust``), chosen by ``GCWS_RUST_INTEGRATION``.

Unset, every part is used whenever the extension is built; ``off`` (or ``python``) keeps the
Python code. Otherwise the value is a ``+``-separated list of parts:

* ``detect``  - the slope detector's state machine (``detector.detect``).
* ``width``   - the loop of ``autoparams.measure_width`` over the local maxima.
* ``measure`` - ``measure.raw_area`` and ``measure.shape`` for straight-line and horizontal
  baselines (exponential skims keep the Python code, whose ``exp`` comes from numpy).
* ``index``   - ``WorkSignal.idx`` (the sample nearest to a time).

``noise.estimate`` stays in Python: its line fits come from LAPACK (``np.polyfit``).

Every part reproduces the Python results bit for bit (numpy's ``interp`` and pairwise ``sum``
are repeated operation by operation). Without the extension nothing changes.
"""
from __future__ import annotations

import os

import numpy as np

try:
    import gcws_rust as _rust
except ImportError:  # the extension is not built
    _rust = None
if _rust is not None and not hasattr(_rust, "width_candidates"):
    _rust = None                       # an older build with the search kernels only

#: the parts used when ``GCWS_RUST_INTEGRATION`` is not set
DEFAULT = "detect+width+measure+index"

_installed: dict = {}


def parts() -> set:
    if _rust is None:
        return set()
    value = os.environ.get("GCWS_RUST_INTEGRATION", DEFAULT).strip().lower()
    if value in ("off", "python", "0", "no"):
        return set()
    return {p for p in value.replace(",", "+").split("+") if p}


def install() -> set:
    """Switch the chosen parts into the integrator's modules (idempotent); returns them."""
    from gcws.integration import autoparams, detector, engine, measure, work
    chosen = parts()
    if not _installed:
        _installed.update(detect=detector.detect, width=autoparams.measure_width,
                          raw_area=measure.raw_area, shape=measure.shape, idx=work.WorkSignal.idx)
    work.WorkSignal.idx = _idx if "index" in chosen else _installed["idx"]
    engine.detect = detector.detect = _detect if "detect" in chosen else _installed["detect"]
    autoparams.measure_width = _measure_width if "width" in chosen else _installed["width"]
    measure.raw_area = _raw_area if "measure" in chosen else _installed["raw_area"]
    measure.shape = _shape if "measure" in chosen else _installed["shape"]
    return chosen


# -- detect ---------------------------------------------------------------------

def _detect(ys, d1, slope, on, n_up, n_dn, at_base=None):
    from gcws.integration.detector import Seg
    f64 = lambda a: np.ascontiguousarray(a, dtype=np.float64)  # noqa: E731
    clusters = _rust.detect(f64(ys), f64(d1), f64(np.broadcast_to(slope, np.shape(ys))),
                            np.ascontiguousarray(on, dtype=bool),
                            np.ascontiguousarray(n_up, dtype=np.int64), np.ascontiguousarray(n_dn, dtype=np.int64),
                            None if at_base is None else np.ascontiguousarray(at_base, dtype=bool))
    return [[Seg(*s) for s in cl] for cl in clusters]


# -- WorkSignal.idx ---------------------------------------------------------------

def _idx(self, t: float) -> int:
    rt = self.rt
    if rt.dtype != np.float64 or not rt.flags.c_contiguous or rt.size == 0 or t != t:  # NaN: numpy's order
        return _installed["idx"](self, t)
    return _rust.nearest_index(rt, float(t))


# -- measure_width --------------------------------------------------------------

def _measure_width(rt, y, noise_sigma, t_from=None) -> float:
    """``autoparams.measure_width`` with its loop over the local maxima in Rust."""
    from gcws.signal import savgol
    ys = savgol.smooth(y, 5, 2)
    mask = np.ones(rt.size, bool) if t_from is None else rt >= t_from
    i = np.flatnonzero(mask)
    if i.size < 20:
        return float(np.median(np.diff(rt))) * 10
    seg = np.ascontiguousarray(ys[i[0]:], dtype=np.float64)
    step = float(np.median(np.diff(rt)))
    win = max(10, int(round(0.3 / step)))
    # (no local maximum gives the same step * 10 as no candidate)
    cands = _rust.width_candidates(seg, win, 20 * noise_sigma, step)
    if not cands:
        return step * 10
    cands.sort(reverse=True)
    top = cands[: max(3, len(cands) // 5)]
    return float(np.median([w for _, w in top]))


# -- measure --------------------------------------------------------------------

def _line(base):
    if base.kind not in ("line", "hold"):
        return None
    return base.kind, float(base.t0), float(base.y0), float(base.t1), float(base.y1)


def _arrays(sig):
    rt, y = sig.rt, sig.y
    if (rt.dtype == np.float64 and y.dtype == np.float64 and rt.flags.c_contiguous
            and y.flags.c_contiguous and rt.size >= 1):
        return rt, y
    return None


def _raw_area(sig, t0: float, t1: float, base, negative=False) -> float:
    line, arrays = _line(base), _arrays(sig)
    if line is None or arrays is None or t0 != t0 or t1 != t1:
        return _installed["raw_area"](sig, t0, t1, base, negative)
    return _rust.raw_area(*arrays, float(t0), float(t1), *line, bool(negative))


def _shape(sig, t0: float, t1: float, base, negative=False) -> dict:
    line, arrays = _line(base), _arrays(sig)
    if line is None or arrays is None or t0 != t0 or t1 != t1:
        return _installed["shape"](sig, t0, t1, base, negative)
    h, w50, w5, sym, asym = _rust.shape(*arrays, float(t0), float(t1), *line, bool(negative))
    return {"height": h, "width50": w50, "width5": w5, "symmetry": sym, "asymmetry": asym}

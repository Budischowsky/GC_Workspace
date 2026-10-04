"""Workspace adapter for the NIAS MS deconvolution engine.

Windows are computed by :mod:`gcws.ms.deconv_fast`, which finds the same components
as the vendored algorithm a whole window at a time. The highest sensitivity level
combines broad and narrow windows and scans a range more densely so weak
neighbouring components can be perceived.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields
from typing import Callable, Optional

import numpy as np
import gc_deconv as legacy

from gcws.ms import deconv_fast

ENGINE_VERSION = "nias-1"


@dataclass
class DeconvSettings:
    window: float = 0.30
    noise_factor: float = 3.0
    shape_r: float = 0.90
    min_ions: int = 3
    apex_tol: float = 0.5

    @classmethod
    def from_dict(cls, d: dict | None) -> "DeconvSettings":
        # Unmarked settings belong to the superseded engine. Start with NIAS
        # defaults rather than carrying its more permissive grouping forward.
        if not d or d.get("engine") != ENGINE_VERSION:
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        return {**asdict(self), "engine": ENGINE_VERSION}

    def params(self):
        return legacy.DeconvParams(window=self.window, noise_factor=self.noise_factor,
                                   shape_r=self.shape_r, min_ions=self.min_ions,
                                   apex_tolerance=self.apex_tol)


PRESETS = {
    "Resolution high": {"apex_tol": 0.3},
    "Resolution medium": {"apex_tol": 0.5},
    "Resolution low": {"apex_tol": 1.0},
    "Sensitivity high": {"noise_factor": 2.0, "min_ions": 3},
    "Sensitivity medium": {"noise_factor": 3.0, "min_ions": 3},
    "Sensitivity low": {"noise_factor": 5.0, "min_ions": 5},
    "Shape strict": {"shape_r": 0.95},
    "Shape medium": {"shape_r": 0.90},
    "Shape loose": {"shape_r": 0.85},
}

# The integration method's five-position control tunes MS perception. Keeping
# level 3 at the original parameters preserves existing saved method behavior.
LEVEL_PARAMS = {
    1: (5.0, 0.95, 5, 0.3),
    2: (4.0, 0.93, 4, 0.4),
    3: (3.0, 0.90, 3, 0.5),
    4: (2.0, 0.85, 2, 1.0),
    5: (1.5, 0.80, 2, 1.2),
}


def settings_for_level(base: DeconvSettings, level: int) -> DeconvSettings:
    """MS settings for a method's detection level, retaining its window size."""
    level = max(1, min(5, int(level)))
    if level == 3:
        return base
    noise, shape, ions, tolerance = LEVEL_PARAMS[level]
    return DeconvSettings(window=base.window, noise_factor=noise, shape_r=shape,
                          min_ions=ions, apex_tol=tolerance)


@dataclass
class Component(legacy.Component):
    def spectrum_dict(self) -> dict[int, float]:
        return {int(m): float(v) for m, v in self.spectrum}


@dataclass
class DeconvResult:
    components: list
    t0: float
    t1: float
    rt: np.ndarray = field(default_factory=lambda: np.empty(0))
    tic: np.ndarray = field(default_factory=lambda: np.empty(0))
    settings: DeconvSettings = field(default_factory=DeconvSettings)
    elapsed: float = 0.0


class _DataMSAdapter:
    """Expose unmodified MSMatrix scan values through the NIAS reader API."""
    def __init__(self, ms):
        self.ms = ms
        self.rt = ms.rt

    def spectrum(self, i):
        mz, ab = self.ms.scan(i)
        return list(zip(mz.tolist(), ab.tolist()))


def _scan_step(ms) -> float:
    return float(np.median(np.diff(ms.rt))) if ms.n_scans > 1 else 0.0075


def _window_components(ms, rt: float, settings: DeconvSettings, dt: float | None = None) -> list:
    """The components of the window around ``rt`` (``dt``: the run's scan step, if known)."""
    converted = [Component(**vars(c)) for c in deconv_fast.deconvolute(ms, rt, settings.params())]
    if settings.noise_factor <= 1.5 and settings.window >= 0.24:
        # A narrow second view resolves small peaks which disappear into the
        # baseline of a broader ion window. Keep the primary window's spectrum
        # where both views describe the same compound.
        from gcws.ms.similarity import cosine
        narrower = legacy.DeconvParams(window=settings.window * 2 / 3,
                                       noise_factor=settings.noise_factor,
                                       shape_r=settings.shape_r, min_ions=settings.min_ions,
                                       apex_tolerance=settings.apex_tol)
        dt = _scan_step(ms) if dt is None else dt
        for item in deconv_fast.deconvolute(ms, rt, narrower):
            candidate = Component(**vars(item))
            if not any(abs(old.rt - candidate.rt) < legacy.MIN_SEPARATION_SCANS * dt
                       and (old.model_mz == candidate.model_mz or
                            cosine(old.spectrum_dict(), candidate.spectrum_dict()) > 0.9)
                       for old in converted):
                converted.append(candidate)
        converted.sort(key=lambda c: (c.rt, -c.area, c.model_mz))
    return converted


def deconvolute_window(ms, rt: float, settings: DeconvSettings | None = None) -> DeconvResult:
    started = time.perf_counter()
    settings = settings or DeconvSettings()
    if settings.window <= 0:
        raise ValueError("Deconvolution window must be positive")
    converted = _window_components(ms, rt, settings)
    scans = np.flatnonzero(np.abs(ms.rt - rt) <= settings.window)
    axis = ms.rt[scans]
    return DeconvResult(converted, float(axis[0]) if axis.size else rt - settings.window,
                        float(axis[-1]) if axis.size else rt + settings.window,
                        rt=axis, tic=ms.tic()[scans], settings=settings,
                        elapsed=time.perf_counter() - started)


def deconvolute_range(ms, t0: float, t1: float, settings: DeconvSettings | None = None,
                      progress: Optional[Callable[[str], None]] = None,
                      cancel: Optional[Callable[[], bool]] = None) -> list[Component]:
    """Combine NIAS windows over a range, retaining each window's central half."""
    from gcws.ms.similarity import cosine
    settings = settings or DeconvSettings()
    step = settings.window / 2 if settings.noise_factor <= 1.5 else settings.window
    if step <= 0:
        raise ValueError("Deconvolution window must be positive")
    centers = np.arange(t0 + step / 2, t1 + step / 2, step)
    found = []
    dt = _scan_step(ms)
    for k, center in enumerate(centers):
        if cancel is not None and cancel():
            break
        found.extend(c for c in _window_components(ms, float(center), settings, dt)
                     if abs(c.rt - center) <= step / 2 and t0 <= c.rt <= t1)
        if progress is not None and k % 5 == 0:
            progress(f"deconvolution {100 * (k + 1) / len(centers):.0f} %")
    out = []
    for comp in sorted(found, key=lambda c: (c.rt, -c.area, c.model_mz)):
        duplicate = next((i for i in range(len(out) - 1, -1, -1)
                          if abs(out[i].rt - comp.rt) < legacy.MIN_SEPARATION_SCANS * dt
                          and (settings.noise_factor <= 1.5 or out[i].model_mz == comp.model_mz or
                               cosine(out[i].spectrum_dict(), comp.spectrum_dict()) > 0.9)), None)
        if duplicate is None:
            out.append(comp)
        elif comp.area > out[duplicate].area:
            out[duplicate] = comp
    return sorted(out, key=lambda c: (c.rt, -c.area, c.model_mz))


def component_for_peak(components: list[Component], t0: float, t1: float, apex: float | None = None):
    """Largest NIAS component inside the peak; never borrow a neighbour."""
    inside = [c for c in components if t0 <= c.rt <= t1]
    if inside:
        return max(inside, key=lambda c: c.area)
    return None


def allocated_component(ms, peak):
    """The original MS component attached to an area-allocated FID/TIC fragment."""
    data = (getattr(peak, "extra", None) or {}).get("deconv_component")
    if not data or not data.get("spectrum"):
        return None
    return Component(rt=data["rt"], apex_scan=ms.scan_at_rt(data["rt"]), model_mz=data["model_mz"],
                     spectrum=data["spectrum"], area=data["area"], purity=data["purity"],
                     n_ions=len(data["spectrum"]), s_n=0.0)

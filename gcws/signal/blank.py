"""Blank subtraction on the chromatogram level.

``subtract(sample, blanks, options, mode)`` returns a new trace
``sample - scale * blank`` on the sample's time grid, the blank first aligned
in time (cross-correlation) and several blanks averaged. Two modes:

* ``peaks`` -- only the blank's *peaks* are removed: the blank's own SNIP
  baseline is taken off before subtracting, so the sample's baseline (and
  a different detector offset) stays untouched. The default for FID.
* ``full`` -- the whole blank trace is subtracted, which also removes a
  rising column-bleed background. The default for MS traces.

The derived trace is integrated like any other signal ("FID - Blank").
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import numpy as np

from gcws.core.model import Signal
from gcws.signal.align import Alignment, peak_shift, xcorr_shift
from gcws.signal.envelope import envelope


@dataclass
class BlankOptions:
    source: str = "blank"            # blank | blank_istd | both
    mode_fid: str = "peaks"          # peaks | full
    mode_ms: str = "full"
    align: str = "auto"              # auto | off
    max_shift: float = 0.05          # min, search range of the alignment
    scale: float = 1.0
    clip: bool = True                # never below the sample's own baseline (no dips where the blank is larger)
    ratio_limit: float = 3.0         # peak level: sample/blank area below this = blank level
    spectral_min: float = 0.7        # peak level: spectral cosine needed for an MS blank match
    rt_tol: float | None = None      # peak level: None = NIAS blank RT tolerance (FID) / 0.03 min (MS)
    env_window: float = 0.5          # min, SNIP window of the blank's baseline ("peaks" mode)

    @classmethod
    def from_dict(cls, d: dict | None) -> "BlankOptions":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})

    def to_dict(self) -> dict:
        return asdict(self)


def align(sample: Signal, blank: Signal, opts: BlankOptions, t_from: float | None = None,
          apexes: tuple | None = None) -> tuple[float, float, str]:
    """``(shift, quality, method)``: matched peak apexes when available, else cross-correlation."""
    if opts.align != "auto":
        return 0.0, 1.0, "none"
    if apexes is not None:
        s_ap, b_ap = apexes
        if t_from is not None:
            s_ap = [t for t in s_ap if t >= t_from]
            b_ap = [t for t in b_ap if t >= t_from]
        found = peak_shift(s_ap, b_ap, opts.max_shift)
        if found is not None:
            shift, n = found
            return shift, min(1.0, n / 10.0), f"{n} matched peaks"
    lo = t_from if t_from is not None else float(sample.rt[0])
    shift, quality = xcorr_shift(sample.rt, sample.y, blank.rt, blank.y, lo, float(sample.rt[-1]), opts.max_shift)
    if quality < 0.3:                         # an unreliable correlation must not move the blank
        return 0.0, quality, "no alignment (weak correlation)"
    return shift, quality, "cross-correlation"


def subtract(sample: Signal, blanks: list[tuple], opts: BlankOptions, mode: str, key: str,
             t_from: float | None = None, sample_apexes=None) -> tuple[Signal, list[Alignment]]:
    """``blanks``: ``[(name, signal, blank apexes or None)]`` of the same kind as ``sample``."""
    rt = np.asarray(sample.rt, float)
    y = np.asarray(sample.y, float)
    aligned = []
    aligns: list[Alignment] = []
    for name, b, b_apexes in blanks:
        apexes = (sample_apexes, b_apexes) if sample_apexes is not None and b_apexes is not None else None
        shift, quality, method = align(sample, b, opts, t_from, apexes)
        # outside the blank's time range nothing is subtracted (NaN)
        aligned.append(np.interp(rt - shift, b.rt, b.y, left=np.nan, right=np.nan))
        aligns.append(Alignment(shift, quality, method, name))
    if not aligned:
        return sample, []
    # smooth the blank a little: its noise must not be added to the sample
    dt = float(np.median(np.diff(rt))) if rt.size > 1 else 1.0
    win = int(max(3, round(0.01 / dt))) | 1
    if win >= 5:
        from gcws.signal import savgol
        aligned = [np.where(np.isnan(a), np.nan, savgol.smooth(np.nan_to_num(a, nan=float(np.nanmedian(a))), win, 2))
                   for a in aligned]
    stack = np.vstack(aligned)
    valid = ~np.isnan(stack)
    count = valid.sum(axis=0)
    yb = np.where(count > 0, np.nansum(np.where(valid, stack, 0.0), axis=0) / np.maximum(count, 1), np.nan)
    missing = np.isnan(yb)
    if mode == "peaks":
        filled = np.where(missing, np.interp(rt, rt[~missing], yb[~missing]) if (~missing).any() else 0.0, yb)
        part = filled - envelope(rt, filled, opts.env_window)
        part[missing] = 0.0
        out = y - opts.scale * part
        if opts.clip:
            out = np.maximum(out, envelope(rt, y, opts.env_window))
    else:
        part = np.where(missing, 0.0, yb)
        out = y - opts.scale * part
        if opts.clip:
            out = np.maximum(out, 0.0)
    names = ", ".join(a.blank for a in aligns)
    shifts = ", ".join(f"{a.shift:+.3f}" for a in aligns)
    label = f"{key} ({names}; shift {shifts} min)"
    return Signal(key, rt, out, source=sample.source, label=label, y_unit=sample.y_unit), aligns

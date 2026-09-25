"""Timed integration events compiled into per-point parameter arrays."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gcws.integration.method import EventKind as E
from gcws.integration.method import IntegrationMethod


@dataclass
class Timeline:
    on: np.ndarray
    slope_mult: np.ndarray
    threshold: np.ndarray
    peak_width: np.ndarray
    area_reject: np.ndarray
    height_reject: np.ndarray
    min_sn: np.ndarray
    shoulders: np.ndarray          # object: off|drop|tangent
    skim_mode: np.ndarray          # object
    tail_ratio: np.ndarray
    front_ratio: np.ndarray
    valley_ratio: np.ndarray
    valley_mode: np.ndarray        # bool
    hold: np.ndarray               # bool
    negative: np.ndarray           # bool
    solvent: np.ndarray            # bool
    area_sum: list[tuple[float, float]] = field(default_factory=list)
    hold_starts: list[float] = field(default_factory=list)
    baseline_now: list[float] = field(default_factory=list)
    next_valley: list[float] = field(default_factory=list)
    split: list[float] = field(default_factory=list)
    backward: list[float] = field(default_factory=list)


def _ranges(events, on_kind, off_kind, t_end) -> list[tuple[float, float]]:
    out, start = [], None
    for e in events:
        if e.kind == on_kind and start is None:
            start = e.time
        elif e.kind == off_kind and start is not None:
            out.append((start, e.time))
            start = None
    if start is not None:
        out.append((start, t_end))
    return out


def compile_timeline(rt: np.ndarray, method: IntegrationMethod, resolved) -> Timeline:
    n = rt.size
    t_end = float(rt[-1]) + 1.0 if n else 1.0
    ev = method.events()

    def filled(v, dtype=float):
        return np.full(n, v, dtype=dtype)

    tl = Timeline(
        on=filled(True, bool),
        slope_mult=filled(resolved.slope_mult),
        threshold=filled(resolved.threshold),
        peak_width=filled(resolved.peak_width),
        area_reject=filled(method.area_reject),
        height_reject=filled(method.height_reject),
        min_sn=filled(method.min_sn),
        shoulders=np.full(n, method.shoulders, dtype=object),
        skim_mode=np.full(n, method.skim_mode, dtype=object),
        tail_ratio=filled(method.tail_skim_ratio),
        front_ratio=filled(method.front_skim_ratio),
        valley_ratio=filled(method.skim_valley_ratio),
        valley_mode=filled(method.baseline_mode == "valley", bool),
        hold=filled(False, bool),
        negative=filled(bool(method.negative_peaks), bool),
        solvent=filled(False, bool),
    )
    value_target = {
        E.SLOPE_SENSITIVITY: tl.slope_mult, E.THRESHOLD: tl.threshold,
        E.PEAK_WIDTH: tl.peak_width, E.AREA_REJECT: tl.area_reject,
        E.HEIGHT_REJECT: tl.height_reject, E.MIN_SN: tl.min_sn,
        E.TAIL_SKIM_RATIO: tl.tail_ratio, E.FRONT_SKIM_RATIO: tl.front_ratio,
        E.SKIM_VALLEY_RATIO: tl.valley_ratio,
    }
    for e in ev:
        i = int(np.searchsorted(rt, e.time))
        if e.kind in value_target and e.value is not None:
            try:
                value_target[e.kind][i:] = float(e.value)
            except (TypeError, ValueError):
                pass
        elif e.kind == E.SHOULDERS:
            tl.shoulders[i:] = str(e.value or "off")
        elif e.kind == E.SKIM_MODE:
            tl.skim_mode[i:] = str(e.value or "none")
        elif e.kind == E.BASELINE_NOW:
            tl.baseline_now.append(e.time)
        elif e.kind == E.BASELINE_NEXT_VALLEY:
            tl.next_valley.append(e.time)
        elif e.kind == E.SPLIT_PEAK:
            tl.split.append(e.time)
        elif e.kind == E.BASELINE_BACKWARD:
            tl.backward.append(e.time)

    def mask(on_kind, off_kind, arr, value=True):
        for a, b in _ranges(ev, on_kind, off_kind, t_end):
            arr[(rt >= a) & (rt < b)] = value

    mask(E.INTEGRATOR_OFF, E.INTEGRATOR_ON, tl.on, False)
    mask(E.BASELINE_VALLEYS_ON, E.BASELINE_VALLEYS_OFF, tl.valley_mode, True)
    mask(E.BASELINE_HOLD_ON, E.BASELINE_HOLD_OFF, tl.hold, True)
    mask(E.NEGATIVE_ON, E.NEGATIVE_OFF, tl.negative, True)
    mask(E.SOLVENT_ON, E.SOLVENT_OFF, tl.solvent, True)
    tl.area_sum = _ranges(ev, E.AREA_SUM_ON, E.AREA_SUM_OFF, t_end)
    tl.hold_starts = [a for a, _ in _ranges(ev, E.BASELINE_HOLD_ON, E.BASELINE_HOLD_OFF, t_end)]
    return tl

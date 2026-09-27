"""Replayable area allocation for deconvolution splits.

A deconvolution split cuts the selected FID/TIC peak into one fragment per
component and allocates the parent's measured area in the ratio of the
component *weights*, conserving its total exactly (the original NIAS
``SplitByComponents`` contract). The versioned payload lives in the manual
event's option field.

* Version 3 (:mod:`gcws.ms.peak_split`): the weights are the component areas
  fitted to the trace itself (``basis: "fit"``), or the MS component areas
  (``basis: "ms"``) when the fit is not trusted. The cut points are the
  crossings of the fitted curves, and each component keeps its elution profile
  and the fit's time shift and width factor, so the chromatogram can draw the
  modeled curve of every fragment.
* Versions 1 and 2 (older projects) replay unchanged: MS component areas,
  midpoint cuts, no profile.
"""
from __future__ import annotations

import json
import math

import numpy as np

from gcws.core.events import ManualEvent, ManualKind as K

PREFIX = "deconvolution:"
VERSION = 3
BASES = ("fit", "ms")


def _number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid deconvolution {label}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Invalid deconvolution {label}")
    return number


def _profile(points) -> list:
    out = []
    try:
        for t, y in points:
            t, y = _number(t, "profile time"), _number(y, "profile value")
            if y < 0 or (out and t <= out[-1][0]):
                raise ValueError("Invalid deconvolution profile")
            out.append([t, y])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid deconvolution profile") from exc
    if len(out) < 2 or not any(y > 0 for _t, y in out):
        raise ValueError("Invalid deconvolution profile")
    return out


def _component(item, version: int = VERSION) -> dict:
    get = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
    rt = _number(get("rt"), "component RT")
    area = _number(get("area"), "component area")
    mass = _number(get("model_mz"), "model ion")
    purity = _number(get("purity"), "component purity")
    if area <= 0:
        raise ValueError("Every deconvolution component must have a positive area")
    if mass <= 0 or not mass.is_integer():
        raise ValueError("Invalid deconvolution model ion")
    if not 0 <= purity <= 1:
        raise ValueError("Deconvolution component purity must be between 0 and 1")
    result = {"rt": rt, "model_mz": int(mass), "purity": purity, "area": area}
    spectrum = get("spectrum")
    if spectrum is not None:
        result["spectrum"] = []
        try:
            for mz, abundance in spectrum:
                mz = _number(mz, "spectrum mass")
                abundance = _number(abundance, "spectrum abundance")
                if mz <= 0 or abundance < 0:
                    raise ValueError("Invalid deconvolution spectrum")
                result["spectrum"].append([mz, abundance])
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid deconvolution spectrum") from exc
    if version >= 3:
        weight = _number(get("weight", area), "component weight")
        if weight <= 0:
            raise ValueError("Every deconvolution component must have a positive weight")
        result["weight"] = weight
        if get("profile") is not None:
            result["profile"] = _profile(get("profile"))
    return result


def _fit(value) -> dict | None:
    if value is None:
        return None
    try:
        fit = {"shift": _number(value["shift"], "fit shift"), "stretch": _number(value["stretch"], "fit width"),
               "r2": _number(value["r2"], "fit R2")}
    except (KeyError, TypeError) as exc:
        raise ValueError("Invalid deconvolution fit") from exc
    if fit["stretch"] <= 0:
        raise ValueError("Invalid deconvolution fit")
    return fit


def _validate(event: ManualEvent, payload) -> dict:
    if not isinstance(payload, dict) or payload.get("version") not in (1, 2, VERSION):
        raise ValueError("Unsupported deconvolution split version")
    version = payload["version"]
    start = _number(event.t0, "peak start")
    end = _number(event.t1, "peak end")
    apex = _number(event.ref_rt, "peak apex")
    delay = _number(payload.get("delay", 0), "detector delay")
    if not start < end or not start <= apex <= end:
        raise ValueError("Invalid deconvolution peak boundaries")
    try:
        components = [_component(item, version) for item in payload["components"]]
        points = [_number(point, "split point") for point in payload["points"]]
    except (KeyError, TypeError) as exc:
        raise ValueError("Invalid deconvolution split payload") from exc
    if len(components) < 2 or len(points) != len(components) - 1:
        raise ValueError("At least two components are required for a deconvolution split")
    basis = payload.get("basis", "ms") if version >= 3 else "ms"
    if basis not in BASES:
        raise ValueError("Invalid deconvolution split basis")
    fit = _fit(payload.get("fit")) if version >= 3 else None
    if basis == "fit" and fit is None:
        raise ValueError("A fitted deconvolution split needs its fit")
    if version < 3:
        for component in components:
            component["weight"] = component["area"]
    bounds = [start, *points, end]
    if any(left >= right for left, right in zip(bounds, bounds[1:])):
        raise ValueError("Deconvolution split points must be strictly inside the peak in RT order")
    mapping = fit["shift"] if basis == "fit" else delay
    times = [component["rt"] + mapping for component in components]
    if any(left >= right for left, right in zip(times, times[1:])):
        raise ValueError("Deconvolution components must have distinct RTs in increasing order")
    if not all(left <= rt <= right for left, rt, right in zip(bounds, times, bounds[1:])):
        raise ValueError("Each deconvolution component must lie inside its split fragment")
    signal = payload.get("signal_key", "FID")
    from gcws.core.keys import base_key
    if not isinstance(signal, str) or base_key(signal) not in ("FID", "TIC"):
        raise ValueError("Proportional deconvolution splitting supports FID and TIC only")
    return {"version": version, "delay": delay, "points": points, "components": components,
            "signal_key": signal, "basis": basis, "fit": fit}


def create_event(peak, components, delay: float = 0, signal_key: str = "FID", *, weights=None,
                 points=None, fit: dict | None = None, basis: str = "ms", profiles=None) -> ManualEvent:
    """Create one atomic split event; component RTs stay in MS minutes.

    ``delay`` maps those RTs to the signal's time frame (zero for an MS trace).
    ``weights`` default to the MS component areas and ``points`` (cut times in the
    signal's frame) to the midpoints between the mapped component RTs; ``fit``
    (``{shift, stretch, r2}``) and ``basis`` come from :mod:`gcws.ms.peak_split`.
    ``profiles`` are the components' elution profiles (``[[rt, y], ...]``); by
    default they are taken from the components themselves. Components must
    belong to the selected peak; their order does not matter.
    """
    from gcws.ms.component_fit import Shape
    delay = _number(delay, "detector delay")
    items = list(components)
    weights = [item_area(item) for item in items] if weights is None else list(weights)
    if profiles is None:
        profiles = []
        for item in items:
            shape = Shape.of(item)
            profiles.append(shape.points() if shape is not None else None)
    if not len(items) == len(weights) == len(profiles):
        raise ValueError("One weight and one profile per deconvolution component")
    entries = []
    for item, weight, profile in zip(items, weights, profiles):
        entry = _component(item, VERSION)
        entry["weight"] = _number(weight, "component weight")
        if profile is not None:
            entry["profile"] = profile
        entry = _component(entry, VERSION)
        entries.append(entry)
    entries.sort(key=lambda item: item["rt"])
    if points is None:
        points = [(left["rt"] + (right["rt"] - left["rt"]) / 2) + delay for left, right in zip(entries, entries[1:])]
    payload = {"version": VERSION, "delay": delay, "signal_key": signal_key, "basis": basis,
               "fit": None if fit is None else {k: float(fit[k]) for k in ("shift", "stretch", "r2")},
               "points": [float(p) for p in points], "components": entries}
    comment = ("deconvolution: area allocated from the fitted " if basis == "fit"
               else "deconvolution: area allocated from MS component proportions")
    if basis == "fit":
        from gcws.core.keys import base_key
        comment += f"{base_key(signal_key)} signal"
    event = ManualEvent(K.SPLIT, float(peak.start), float(peak.end), ref_rt=float(peak.apex_rt), comment=comment)
    payload = _validate(event, payload)
    return event.with_(option=PREFIX + json.dumps(payload, separators=(",", ":"), allow_nan=False))


def item_area(item) -> float:
    return item.get("area") if isinstance(item, dict) else getattr(item, "area", None)


def decode(event: ManualEvent) -> dict | None:
    """Return a validated allocation, or ``None`` for an ordinary manual event.

    A recognized but damaged payload raises ``ValueError`` so replay reports
    an unresolved event instead of silently applying a different operation.
    """
    if event.kind != K.SPLIT or not event.option.startswith(PREFIX):
        return None
    try:
        payload = json.loads(event.option[len(PREFIX):])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid deconvolution split payload") from exc
    return _validate(event, payload)


def share_exactly(total: float, weights) -> list[float]:
    """Allocate area by positive weights, conserving the total exactly.

    Integer multiples of ``ulp(total)`` sum exactly with ordinary, compensated
    and pairwise floating-point summation. The last component receives the
    remaining units. This implements the original NIAS conservation contract
    without importing its UI and data-layer dependencies.
    """
    total = _number(total, "parent area")
    weights = [_number(weight, "component area") for weight in weights]
    if not weights or any(weight <= 0 for weight in weights):
        raise ValueError("Every deconvolution component must have a positive area")
    if total == 0:
        return [0.0] * len(weights)
    largest = max(weights)
    scaled = [weight / largest for weight in weights]
    scale = math.fsum(scaled)
    quantum = math.ulp(abs(total))
    units = int(round(abs(total) / quantum))
    remaining = units
    parts = []
    for weight in scaled[:-1]:
        part = min(remaining, max(0, round(units * (weight / scale))))
        parts.append(math.copysign(part * quantum, total))
        remaining -= part
    parts.append(math.copysign(remaining * quantum, total))
    return parts


def fragment_curve(peak, t) -> np.ndarray | None:
    """The modeled curve of a deconvoluted fragment above its baseline, on ``t``.

    Scaled so that its area over the parent span equals the fragment's allocated
    (raw) area: the curve drawn is exactly the area reported. None for fragments
    without an elution profile (older projects) or with a non-positive area.
    """
    from gcws.ms.component_fit import Shape, curve
    dc = (getattr(peak, "extra", None) or {}).get("deconv_component") or {}
    span = dc.get("parent_span")
    shape = Shape.of(dc) if dc.get("profile") else None
    if shape is None or not span or not peak.area_raw > 0:
        return None
    fit = dc.get("fit") or {}
    shift = float(fit.get("shift", dc.get("delay", 0.0)))
    stretch = float(fit.get("stretch", 1.0))
    lo, hi = float(span[0]), float(span[1])
    dense = np.linspace(lo, hi, 801)
    norm = float(np.trapezoid(curve(shape, dense, shift, stretch), dense * 60.0))
    if not norm > 0:
        return None
    t = np.asarray(t, dtype=float)
    out = curve(shape, t, shift, stretch) * (peak.area_raw / norm)
    out[(t < lo) | (t > hi)] = 0.0
    return out

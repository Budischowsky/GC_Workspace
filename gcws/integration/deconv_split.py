"""Replayable MS-weighted area allocation for deconvolution splits.

Ordinary drop lines integrate each fragment directly. A deconvolution split
keeps those boundaries but allocates the parent area in the ratio of the MS
component areas, as the original NIAS ``SplitByComponents`` operation did.
The versioned payload lives in the existing manual-event option field.
"""
from __future__ import annotations

import json
import math

from gcws.core.events import ManualEvent, ManualKind as K

PREFIX = "deconvolution:"
VERSION = 2


def _number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid deconvolution {label}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Invalid deconvolution {label}")
    return number


def _component(item) -> dict:
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
    return result


def _validate(event: ManualEvent, payload) -> dict:
    if not isinstance(payload, dict) or payload.get("version") not in (1, VERSION):
        raise ValueError("Unsupported deconvolution split version")
    start = _number(event.t0, "peak start")
    end = _number(event.t1, "peak end")
    apex = _number(event.ref_rt, "peak apex")
    delay = _number(payload.get("delay", 0), "detector delay")
    if not start < end or not start <= apex <= end:
        raise ValueError("Invalid deconvolution peak boundaries")
    try:
        components = [_component(item) for item in payload["components"]]
        points = [_number(point, "split point") for point in payload["points"]]
    except (KeyError, TypeError) as exc:
        raise ValueError("Invalid deconvolution split payload") from exc
    if len(components) < 2 or len(points) != len(components) - 1:
        raise ValueError("At least two components are required for a deconvolution split")
    bounds = [start, *points, end]
    if any(left >= right for left, right in zip(bounds, bounds[1:])):
        raise ValueError("Deconvolution split points must be strictly inside the peak in RT order")
    times = [component["rt"] + delay for component in components]
    if any(left >= right for left, right in zip(times, times[1:])):
        raise ValueError("Deconvolution components must have distinct RTs in increasing order")
    if not all(left <= rt <= right for left, rt, right in zip(bounds, times, bounds[1:])):
        raise ValueError("Each deconvolution component must lie inside its split fragment")
    signal = payload.get("signal_key", "FID")
    from gcws.core.keys import base_key
    if not isinstance(signal, str) or base_key(signal) not in ("FID", "TIC"):
        raise ValueError("Proportional deconvolution splitting supports FID and TIC only")
    return {"version": payload["version"], "delay": delay, "points": points,
            "components": components, "signal_key": signal}


def create_event(peak, components, delay: float = 0, signal_key: str = "FID") -> ManualEvent:
    """Create one atomic split event; component RTs stay in MS minutes.

    ``delay`` maps those RTs to the selected signal's time frame (zero for an
    MS trace). Components must belong to the selected peak; their order does
    not matter. The cut geometry matches the existing midpoint drop lines.
    """
    delay = _number(delay, "detector delay")
    components = sorted((_component(item) for item in components), key=lambda item: item["rt"])
    payload = {"version": VERSION, "delay": delay, "signal_key": signal_key, "components": components,
               "points": [(left["rt"] + (right["rt"] - left["rt"]) / 2) + delay
                          for left, right in zip(components, components[1:])]}
    event = ManualEvent(K.SPLIT, float(peak.start), float(peak.end), ref_rt=float(peak.apex_rt),
                        comment="deconvolution: area allocated from MS component proportions")
    payload = _validate(event, payload)
    return event.with_(option=PREFIX + json.dumps(payload, separators=(",", ":"), allow_nan=False))


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
    """Allocate area by positive MS weights, conserving the total exactly.

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

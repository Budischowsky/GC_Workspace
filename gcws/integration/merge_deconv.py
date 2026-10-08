"""Merge deconvoluted peaks: the deconvolution split of a peak reversed.

A fragment of the analyst's *Split by deconvolution* comes from one SPLIT event (its fragments'
ids are ``<event uid>:<i>``): that event is removed. A fragment of the automatic deconvolution
split (:mod:`gcws.integration.auto_deconv`) is not stored as an event: its parent gets a *Keep
unsplit* marker. Either way the parent peak is integrated whole again, and the undo of the
manual events brings the split back.
"""
from __future__ import annotations

from types import SimpleNamespace

from gcws.integration import auto_deconv as AD


def fragments(peaks, lo: float, hi: float) -> list:
    """The deconvoluted fragments among ``peaks`` with their apex in [lo, hi] (or, for a click,
    lo == hi, the fragment holding that time)."""
    lo, hi = sorted((lo, hi))
    out = []
    for p in peaks or []:
        if not (getattr(p, "extra", None) or {}).get("deconv_component"):
            continue
        if (lo <= p.apex_rt <= hi) if hi > lo else (p.start <= lo <= p.end):
            out.append(p)
    return out


def _split_uid(peak) -> str:
    """The uid of the split a fragment comes from ("" for a peak that is no fragment)."""
    dc = (getattr(peak, "extra", None) or {}).get("deconv_component") or {}
    return str(dc.get("id", "")).rsplit(":", 1)[0]


def _parent_span(fragment, peaks) -> tuple[float, float, float]:
    """``(start, end, apex)`` of the peak the fragment was split from."""
    dc = fragment.extra["deconv_component"]
    uid = _split_uid(fragment)
    same = [p for p in peaks if _split_uid(p) == uid] if uid else [fragment]
    span = dc.get("parent_span")
    lo, hi = (float(span[0]), float(span[1])) if span else (min(p.start for p in same), max(p.end for p in same))
    apex = max(same, key=lambda p: p.height).apex_rt if same else fragment.apex_rt
    return lo, hi, float(apex)


def unsplit(events: list, peaks: list, lo: float, hi: float, auto_split: bool,
            presplit: list | None = None) -> tuple[list, list[float], list[str]]:
    """The manual ``events`` of a signal with the deconvolution split of every fragment with its apex
    in [lo, hi] reversed, the apex of every peak merged again and the problems (one text each).
    ``auto_split``: the method splits automatically (then a removed split of the analyst also gets a
    *Keep unsplit* marker, or the automatic split would take its place). ``presplit`` are the peaks before the automatic split
    (``RunState.presplit``), whose bounds the marker takes when found."""
    out = list(events)
    merged, problems, seen = [], [], set()
    kept = AD.kept_spans(out)
    for f in fragments(peaks, lo, hi):
        uid = _split_uid(f)
        if uid in seen:
            continue
        seen.add(uid)
        start, end, apex = _parent_span(f, peaks)
        parent = next((p for p in presplit or [] if abs(p.start - start) < 1e-6 and abs(p.end - end) < 1e-6),
                      SimpleNamespace(start=start, end=end, apex_rt=apex))
        auto = uid.startswith(AD.AUTO_UID + "-")
        if not auto:
            own = [e for e in out if e.uid == uid]
            if not own:
                problems.append(f"{apex:.3f} min: its deconvolution split was not found")
                continue
            out = [e for e in out if e.uid != uid]
        if (auto or auto_split) and not any(t0 - 1e-6 <= parent.apex_rt <= t1 + 1e-6 for t0, t1, _a in kept):
            out.append(AD.keep_marker(parent, "merged deconvoluted peaks: no automatic deconvolution split"))
            kept = AD.kept_spans(out)
        merged.append(float(parent.apex_rt))
    return out, merged, problems

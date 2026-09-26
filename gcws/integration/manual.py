"""Applying manual integration events to an automatic result."""
from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Optional

import numpy as np

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.model import Baseline
from gcws.integration import measure as MS
from gcws.integration import skim as SK
from gcws.integration.work import WP, WorkSignal, apex_of, line_between


def effective_events(events: Iterable[ManualEvent]) -> list[ManualEvent]:
    """Enabled events in order; a RESET_RANGE cancels earlier events it overlaps."""
    out: list[ManualEvent] = []
    for e in events:
        if not e.enabled:
            continue
        if e.kind == K.RESET_RANGE:
            lo, hi = sorted((e.t0, e.t1 if e.t1 is not None else e.t0))
            keep = []
            for prev in out:
                a = min(prev.t0, prev.t1 if prev.t1 is not None else prev.t0)
                b = max(prev.t0, prev.t1 if prev.t1 is not None else prev.t0)
                if prev.ref_rt is not None:
                    a, b = min(a, prev.ref_rt), max(b, prev.ref_rt)
                if b < lo or a > hi:
                    keep.append(prev)
            out = keep
            continue
        out.append(e)
    return out


def _all(peaks: list[WP]) -> list[WP]:
    out = []
    for p in peaks:
        out.extend(p.children)
        out.append(p)
    return out


def _containing(peaks: list[WP], t: float) -> Optional[WP]:
    for p in peaks:                               # riders first: they are smaller
        for c in p.children:
            if c.t0 <= t <= c.t1:
                return c
    for p in peaks:
        if p.t0 <= t <= p.t1:
            return p
    return None


def _nearest(peaks: list[WP], ref: float, tol: float) -> Optional[WP]:
    best = None
    for p in _all(peaks):
        d = abs(p.ta - ref)
        if d <= tol and (best is None or d < abs(best.ta - ref)):
            best = p
    return best


def _remove(peaks: list[WP], p: WP) -> None:
    if p.parent is not None:
        _clear_allocation(p.parent)
        if p in p.parent.children:
            p.parent.children.remove(p)
        p.parent = None
    elif p in peaks:
        peaks.remove(p)
        for c in p.children:          # riders of a deleted parent become peaks of their own
            c.parent = None
            peaks.append(c)
        p.children = []


def _mark(p: WP, origin: str | None = None) -> None:
    p.add_flag("M")
    p.manual = True
    if origin:
        p.origin = origin


def _refresh_apex(sig: WorkSignal, p: WP) -> None:
    _clear_allocation(p)
    p.ta = apex_of(sig, p.t0, p.t1, p.base, p.negative)


def _clear_allocation(p: WP) -> None:
    """A later geometry edit replaces the modeled area with a trace integral."""
    p.allocated_area_raw = None
    p.area_allocation = None
    p.deconv_component = {}
    if p.origin == "deconvoluted":
        p.origin = "manual"


def _split_by_components(peaks, sig, event, payload):
    from gcws.integration.deconv_split import share_exactly
    # Bind to the selected peak's bounds, so a skimmed rider cannot steal the split.
    p = min((p for p in _all(peaks) if abs(p.t0 - event.t0) < 1e-8
             and abs(p.t1 - event.t1) < 1e-8),
            key=lambda p: abs(p.ta - event.ref_rt), default=None)
    if p is None:
        return "the deconvolution peak boundaries changed; run deconvolution again"
    bounds = [p.t0, *payload["points"], p.t1]
    for lo, hi in zip(bounds, bounds[1:]):
        if np.searchsorted(sig.rt, hi, side="right") - np.searchsorted(sig.rt, lo) < 3:
            return "a deconvolution fragment would contain fewer than three measured points"
    total = p.allocated_area_raw
    if total is None:
        total = MS.raw_area(sig, p.t0, p.t1, p.base, p.negative)
        total -= sum(MS.raw_area(sig, c.t0, c.t1, c.base, c.negative) for c in p.children)
    if not np.isfinite(total) or total <= 0:
        return "the original peak area must be positive for a deconvolution split"
    components = payload["components"]
    weights = [c["area"] for c in components]
    areas = share_exactly(total, weights)
    fractions = share_exactly(1.0, weights)
    fragments = []
    for i, (lo, hi, component, area, fraction) in enumerate(
            zip(bounds, bounds[1:], components, areas, fractions)):
        frag = WP(lo, hi, p.ta, replace(p.base), ts=p.ts if i == 0 else "V",
                  te=p.te if i == len(components) - 1 else "V", flags=p.flags,
                  negative=p.negative, parent=p.parent, cluster=p.cluster)
        _refresh_apex(sig, frag)
        _mark(frag, "deconvoluted")
        frag.allocated_area_raw = area
        frag.area_allocation = (total, tuple(weights), i)
        frag.deconv_component = {**component, "weight": fraction}
        fragments.append(frag)
    # Commit only after every validation and allocation succeeded.
    for child in p.children:
        i = min(int(np.searchsorted(payload["points"], child.ta)), len(fragments) - 1)
        child.parent = fragments[i]
        fragments[i].children.append(child)
    container = p.parent.children if p.parent is not None else peaks
    index = next(i for i, item in enumerate(container) if item is p)
    container[index:index + 1] = fragments
    return ""


def apply(peaks: list[WP], sig: WorkSignal, events: Iterable[ManualEvent],
          width: float) -> tuple[list[WP], list[tuple[str, str]]]:
    peaks = list(peaks)
    unresolved: list[tuple[str, str]] = []
    tol = max(0.5 * width, 0.02)
    for e in effective_events(events):
        try:
            msg = _apply_one(peaks, sig, e, tol, width)
        except Exception as exc:  # noqa: BLE001 - an event must never break integration
            msg = f"error: {exc}"
        if msg:
            unresolved.append((e.uid, msg))
    peaks.sort(key=lambda p: p.ta)
    return peaks, unresolved


def _clip_around(peaks: list[WP], t0: float, t1: float) -> None:
    """Remove peaks with apex inside [t0, t1]; clip ones reaching into it."""
    for p in list(peaks):
        if t0 <= p.ta <= t1:
            _remove(peaks, p)
    for p in peaks:
        if p.t0 < t0 < p.t1:
            _clear_allocation(p)
            p.t1 = t0
        if p.t0 < t1 < p.t1:
            _clear_allocation(p)
            p.t0 = t1


def _apply_one(peaks: list[WP], sig: WorkSignal, e: ManualEvent, tol: float, width: float) -> str:
    k = e.kind
    if k == K.SPLIT:
        from gcws.integration.deconv_split import decode
        payload = decode(e)
        if payload is not None:
            return _split_by_components(peaks, sig, e, payload)
        p = _containing(peaks, e.t0)
        if p is None or not (p.t0 < e.t0 < p.t1):
            return "no peak at the split time"
        q = WP(e.t0, p.t1, p.ta, replace(p.base), ts="V", te=p.te, flags=p.flags,
               negative=p.negative, parent=p.parent)
        p.t1, p.te = e.t0, "V"
        for c in list(p.children):
            if c.t0 >= e.t0:
                p.children.remove(c)
                c.parent = q
                q.children.append(c)
        if p.parent is not None:
            p.parent.children.append(q)
        else:
            peaks.append(q)
        _refresh_apex(sig, p)
        _refresh_apex(sig, q)
        _mark(p, "split")
        _mark(q, "split")
        return ""

    if k == K.DELETE:
        if e.t1 is None:
            p = _containing(peaks, e.t0)
            if p is None:
                return "no peak at this time"
            _remove(peaks, p)
            return ""
        lo, hi = sorted((e.t0, e.t1))
        hits = [p for p in _all(peaks) if lo <= p.ta <= hi]
        if not hits:
            return "no peak in the range"
        for p in hits:
            _remove(peaks, p)
        return ""

    if k in (K.ADD_PEAK, K.NEGATIVE_PEAK):
        lo, hi = sorted((e.t0, e.t1 if e.t1 is not None else e.t0))
        if hi - lo <= 0:
            return "empty range"
        y0, y1 = (e.y0, e.y1) if e.t0 <= (e.t1 or e.t0) else (e.y1, e.y0)
        _clip_around(peaks, lo, hi)
        base = line_between(sig, lo, hi, y0, y1)
        p = WP(lo, hi, (lo + hi) / 2, base, negative=(k == K.NEGATIVE_PEAK))
        if p.negative:
            p.add_flag("N")
        _refresh_apex(sig, p)
        _mark(p, "added")
        peaks.append(p)
        return ""

    if k == K.DRAW_BASELINE:
        lo, hi = sorted((e.t0, e.t1 if e.t1 is not None else e.t0))
        if hi - lo <= 0:
            return "empty range"
        y0, y1 = (e.y0, e.y1) if e.t0 <= (e.t1 or e.t0) else (e.y1, e.y0)
        base = line_between(sig, lo, hi, y0, y1)
        inside = sorted((p for p in peaks if lo <= p.ta <= hi), key=lambda p: p.ta)
        for p in peaks:
            if p in inside:
                continue
            if p.t0 < lo < p.t1:
                _clear_allocation(p)
                p.t1 = lo
            if p.t0 < hi < p.t1:
                _clear_allocation(p)
                p.t0 = hi
        if not inside:
            p = WP(lo, hi, (lo + hi) / 2, base)
            _refresh_apex(sig, p)
            _mark(p, "manual")
            peaks.append(p)
            return ""
        inside[0].t0, inside[0].ts = lo, "B"
        inside[-1].t1, inside[-1].te = hi, "B"
        for p in inside:
            p.t0 = max(p.t0, lo)
            p.t1 = min(p.t1, hi)
            p.base = replace(base)
            _refresh_apex(sig, p)
            _mark(p, "manual")
        return ""

    if k in (K.MOVE_START, K.MOVE_END):
        ref = e.ref_rt if e.ref_rt is not None else e.t0
        p = _nearest(peaks, ref, max(tol, 3 * width))
        if p is None:
            return "peak to move not found"
        t = e.t0
        if k == K.MOVE_START:
            if t >= p.t1:
                return "start after end"
            old = p.t0
            if e.option == "shared":
                for q in _all(peaks):
                    if q is not p and abs(q.t1 - old) < 1e-6:
                        q.t1 = t
                        _refresh_apex(sig, q)
            if abs(p.base.t0 - old) < 1e-6 and p.base.kind == "line":
                p.base = Baseline("line", t, sig.at(t) if e.y0 is None else e.y0,
                                  p.base.t1, p.base.y1)
            p.t0 = t
        else:
            if t <= p.t0:
                return "end before start"
            old = p.t1
            if e.option == "shared":
                for q in _all(peaks):
                    if q is not p and abs(q.t0 - old) < 1e-6:
                        q.t0 = t
                        _refresh_apex(sig, q)
            if abs(p.base.t1 - old) < 1e-6 and p.base.kind == "line":
                p.base = Baseline("line", p.base.t0, p.base.y0, t,
                                  sig.at(t) if e.y0 is None else e.y0)
            p.t1 = t
        _refresh_apex(sig, p)
        if p.parent is not None:
            _clear_allocation(p.parent)
        _mark(p)
        return ""

    if k == K.MERGE:
        lo, hi = sorted((e.t0, e.t1 if e.t1 is not None else e.t0))
        hits = sorted((p for p in peaks if lo <= p.ta <= hi), key=lambda p: p.ta)
        if len(hits) < 2:
            return "fewer than two peaks in the range"
        first, last = hits[0], hits[-1]
        if all(h.base == first.base for h in hits):
            base = replace(first.base)
        else:
            b0 = float(first.base.eval([first.t0])[0])
            b1 = float(last.base.eval([last.t1])[0])
            base = Baseline("line", first.t0, b0, last.t1, b1)
        m = WP(first.t0, last.t1, first.ta, base, ts=first.ts, te=last.te)
        for h in hits:
            m.children.extend(h.children)
            peaks.remove(h)
        for c in m.children:
            c.parent = m
        _refresh_apex(sig, m)
        _mark(m, "merged")
        peaks.append(m)
        return ""

    if k == K.SKIM:
        parent = _nearest(peaks, e.t0, max(tol, 3 * width))
        child = _nearest(peaks, e.t1 if e.t1 is not None else e.t0, max(tol, 3 * width))
        if parent is None or child is None or parent is child:
            return "parent or rider peak not found"
        if child.parent is not None:
            return "peak is already skimmed"
        if child in peaks:
            peaks.remove(child)
        if child.ta > parent.ta:
            res = None
            if e.option == "exponential":
                ex = SK.exponential_tail(sig, parent, child.t0, child.ta, child.t1, width)
                if ex:
                    res = (ex[0], ex[1])
                    child.add_flag("X")
            if res is None:
                res = SK.tangent_tail(sig, child.t0, child.ta, child.t1)
                child.add_flag("T")
            if res is None:
                peaks.append(child)
                return "skim not possible"
            child.t1, child.base = res
            parent.t1 = max(parent.t1, child.t1)
        else:
            res = SK.tangent_front(sig, child.t1, child.ta, child.t0)
            if res is None:
                peaks.append(child)
                return "skim not possible"
            child.t0, child.base = res
            child.add_flag("T")
            parent.t0 = min(parent.t0, child.t0)
        child.parent = parent
        parent.children.append(child)
        _clear_allocation(parent)
        _clear_allocation(child)
        _mark(child, "skim")
        return ""

    return f"unsupported event {k}"

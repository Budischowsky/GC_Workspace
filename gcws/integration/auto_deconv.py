"""Automatic deconvolution split of a whole run (an integration stage after the integrator).

With ``IntegrationMethod.deconv_split == "auto"`` every integrated peak of the FID (or TIC)
that holds more than one deconvoluted MS component is split into one fragment per component,
exactly like the analyst's *Split by deconvolution* (:mod:`gcws.ms.peak_split`): the components'
elution profiles are fitted to the trace, the parent's area is allocated by the fitted areas
(total kept exactly), and below the method's R² limit by the MS component proportions.

The split events are not stored: :func:`plan_run` makes them from the integration (after the
analyst's manual events), the whole-run deconvolution and the method, and the workspace replays
them after the manual events. Each event's uid is derived from its parent peak and its
components, so a fragment keeps its identity (and its identification) over re-integrations.

Gates, taken from mzmine's GC spectral deconvolution (``featdet_spectraldeconvolutiongc``,
MIT, Copyright (c) 2004-2025 The mzmine Development Team), decide which components become a
fragment of their own; the others leave their signal to their neighbours:

* the model ion is not one of the excluded m/z (column bleed, mzmine "Exclude m/z values");
* the component's fitted curve correlates with what the trace shows of it (Pearson r >= the
  method's ``deconv_min_r``, mzmine's "minimum shape similarity" 0.8);
* two components with nearly the same spectrum (cosine > 0.9) within two scans are one
  compound, as the range deconvolution merges them.

Hidden components: a component whose apex lies in no integrated peak, but whose elution reaches
into a neighbouring peak (a small compound in the tail or front of a large, often overloaded,
peak; the integrator ends the large peak before it), extends that peak over its elution
(:func:`adoptions`, replayed as automatic *Move end/start* events before the split). Such an
adopted component, like any component that elutes apart from the peak's main compound
(:mod:`gcws.ms.peak_split`), becomes a fragment when its area passes the method's area reject
(instead of the minimum share of the peak, which is meant for negligible parts of the peak's own
compound), and its shape is compared with the trace where it carries a visible part of the fitted
signal. The extension is kept only where the extended peak's split is fitted to the trace and splits off
an adopted component; elsewhere the peak keeps its integrated bounds and its own plan.

Peaks that are left alone: negative and solvent peaks, peaks already split by deconvolution
(the analyst's split wins), peaks with a *Keep unsplit* marker and peaks inside a
*Deconvolution split off* range of the timed events.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from gcws.core.events import ManualEvent, ManualKind as K

#: option of the analyst's *Keep unsplit* marker (a SPLIT event that only blocks the automatic split)
KEEP_OPTION = "deconvolution-off"
#: prefix of the uid of an automatic split event
AUTO_UID = "auto"
#: two components closer than this many MS scans with a cosine above SAME_COSINE are one compound
SAME_SCANS = 2.0
SAME_COSINE = 0.9
#: note of a split (or a refusal) that rests on the closer look at one peak
CLOSER = "closer look"
#: an elution profile reaches as far as it carries this share of its maximum
SPAN_LEVEL = 0.05
#: a hidden component needs at least this MS S/N whatever the level (the default level's limit): it
#: makes a peak where the integrator saw none, so it has to stand out on its own
ADOPT_MIN_SN = 20.0
#: a hidden component whose estimated area (the host peak's area in the proportion of the MS
#: component areas) is below this share of the method's area reject is not taken in: it could not pass
#: the area limit (the margin allows for detector responses that differ from the MS)
ESTIMATE_SHARE = 0.25
#: an extended peak ends (starts) at the trace's lowest point within this many elution spans of
#: the hidden component after (before) it: a drop line on the large peak's tail would cut away most
#: of the compound
VALLEY_SPANS = 1.0
#: the shape of a component apart from the main compound is compared where it is this share of
#: the fitted signal
VISIBLE_SHARE = 0.2


@dataclass
class RunPlan:
    """The automatic splits of one run and signal, and what was left alone (for notes)."""
    events: list = field(default_factory=list)          # ManualEvent, RT order
    plans: list = field(default_factory=list)           # SplitPlan per event
    skipped: list = field(default_factory=list)         # (apex, reason) of peaks with >= 2 components
    components: int = 0
    extensions: list = field(default_factory=list)      # Extension of the split peaks, replayed first

    @property
    def ms_basis(self) -> int:
        return sum(1 for p in self.plans if p.basis == "ms")

    @property
    def extension_events(self) -> list:
        return [e for x in self.extensions for e in x.events]


@dataclass
class Extension:
    """An integrated peak extended over components that had no peak of their own."""
    apex: float                                         # the peak's apex (trace time)
    start: float                                        # extended bounds (trace time)
    end: float
    events: list = field(default_factory=list)          # automatic MOVE_START / MOVE_END
    components: list = field(default_factory=list)      # the adopted components (MS time)
    host: tuple = ()                                    # the peak's integrated (start, end)

    def holds(self, peak) -> bool:
        return abs(peak.start - self.start) < 1e-9 and abs(peak.end - self.end) < 1e-9

    def extends(self, peak) -> bool:
        return bool(self.host) and abs(peak.start - self.host[0]) < 1e-9 and abs(peak.end - self.host[1]) < 1e-9


def enabled(method) -> bool:
    return getattr(method, "deconv_split", "off") == "auto"


def is_auto(event: ManualEvent) -> bool:
    return event.uid.startswith(AUTO_UID + "-")


def keep_marker(peak, comment: str = "") -> ManualEvent:
    """The analyst's *Keep unsplit* marker for ``peak`` (replayed as a no-op)."""
    return ManualEvent(K.SPLIT, float(peak.start), float(peak.end), ref_rt=float(peak.apex_rt),
                       option=KEEP_OPTION, comment=comment or "keep unsplit: no automatic deconvolution split")


def kept_spans(events) -> list[tuple[float, float, float]]:
    """``(t0, t1, apex)`` of the enabled *Keep unsplit* markers (not those a later *Reset range*
    discards)."""
    from gcws.integration.manual import effective_events
    return [(float(e.t0), float(e.t1 if e.t1 is not None else e.t0),
             float(e.ref_rt if e.ref_rt is not None else e.t0))
            for e in effective_events(events) if e.kind == K.SPLIT and e.option == KEEP_OPTION]


def limits_of(method):
    from gcws.integration.method import deconv_value
    from gcws.ms.peak_split import Limits
    return Limits(min_sn=deconv_value(method, "deconv_min_sn"),
                  min_share=deconv_value(method, "deconv_min_share"),
                  fit_r2=deconv_value(method, "deconv_fit_r2"),
                  min_area=float(getattr(method, "area_reject", 0.0) or 0.0))


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _uid(key: str, peak, components) -> str:
    h = hashlib.sha1(f"{key}|{peak.start:.5f}|{peak.end:.5f}".encode())
    for c in components:
        h.update(f"|{float(_get(c, 'rt')):.4f}:{int(_get(c, 'model_mz'))}".encode())
    return f"{AUTO_UID}-{h.hexdigest()[:10]}"


def shape_r(plan, index: int) -> float:
    """Pearson r of candidate ``index``'s fitted curve with the trace minus the other candidates'
    curves, over the points where the curve carries signal (>= 5 % of its maximum).

    A component apart from the main compound may instead be compared where it is a visible part of
    both the fitted and the measured signal (VISIBLE_SHARE), whichever agrees better: on the tail of
    a peak a hundred times larger, the large peak's model error outweighs the small compound's whole
    curve where that curve rises."""
    fit = plan.first
    if fit is None or index >= len(fit.curves):
        return math.nan
    own = fit.curves[index]
    if not own.max() > 0:
        return 0.0
    rest = plan.y - (fit.total - own)
    use = (own >= 0.05 * own.max()) & plan.mask
    if int(use.sum()) < 4:
        return math.nan
    from gcws.features.pseudo import pearson
    r = pearson(own[use], rest[use])
    if plan.candidates[index].apart:
        visible = use & (own >= VISIBLE_SHARE * np.maximum(fit.total, plan.y))
        if int(visible.sum()) >= 4:
            r = max(r, pearson(own[visible], rest[visible]))
    return r


def _same_compound(a, b, scan_dt: float) -> bool:
    from gcws.ms.similarity import cosine
    if abs(float(_get(a, "rt")) - float(_get(b, "rt"))) > SAME_SCANS * scan_dt:
        return False
    sa, sb = _get(a, "spectrum"), _get(b, "spectrum")
    if not sa or not sb:
        return False
    return cosine(sa, sb) > SAME_COSINE


def gate(plan, method, scan_dt: float) -> tuple[list[int], list[str]]:
    """The candidates of ``plan`` that become fragments, and why the others do not."""
    from gcws.integration.method import deconv_value
    exclude = {int(m) for m in (getattr(method, "deconv_exclude_mz", None) or [])}
    min_r = deconv_value(method, "deconv_min_r")
    keep, notes = [], []
    for i in plan.checked:
        c = plan.candidates[i].component
        rt = float(_get(c, "rt"))
        if int(_get(c, "model_mz", 0) or 0) in exclude:
            notes.append(f"{rt:.3f}: model m/z {int(_get(c, 'model_mz'))} excluded")
            continue
        r = shape_r(plan, i)
        if min_r > 0 and math.isfinite(r) and r < min_r:
            notes.append(f"{rt:.3f}: shape r {r:.2f} < {min_r:.2f}")
            continue
        keep.append(i)
    out: list[int] = []
    for i in keep:
        if out and _same_compound(plan.candidates[out[-1]].component, plan.candidates[i].component, scan_dt):
            j = out[-1]
            larger = i if float(_get(plan.candidates[i].component, "area", 0) or 0) > \
                float(_get(plan.candidates[j].component, "area", 0) or 0) else j
            notes.append(f"{float(_get(plan.candidates[i].component, 'rt')):.3f}: same spectrum as "
                         f"{float(_get(plan.candidates[j].component, 'rt')):.3f}")
            out[-1] = larger
            continue
        out.append(i)
    return out, notes


def _in_ranges(t: float, ranges) -> bool:
    return any(a <= t < b for a, b in ranges)


def suspect(signal, peak, key: str, delay: float, cands, riders, method) -> str:
    """Why a peak with fewer than two whole-run components deserves a closer look ("" = it does
    not): the method asks for it, and the trace shows a shoulder or one component does not
    explain it (fit R² below the method's limit)."""
    if not getattr(method, "deconv_probe", False):
        return ""
    from gcws.core.keys import base_key
    from gcws.ms.deconv_probe import trace_shoulders
    from gcws.ms.peak_split import plan_split, trace_window
    if base_key(key) not in ("FID", "TIC") or getattr(peak, "negative", False) or not peak.area > 0:
        return ""                                    # plan_split refuses these before any fit
    if not riders:
        # the shoulder test needs the trace only: the fit below is made when it finds none
        t, y, _mask = trace_window(signal, peak, riders)
        if t.size < 3:
            return ""
        level = max(1, min(5, int(getattr(method, "deconv_level", 3))))
        depth, prominence = ((0.04, 0.02) if level == 5 else
                             (0.06, 0.03) if level == 4 else (0.08, 0.04))
        found = trace_shoulders(t, y, depth, prominence)
        if found:
            return "shoulder at " + ", ".join(f"{t:.3f}" for t in found)
    plan = plan_split(signal, peak, key, delay, cands, riders=riders)
    if plan.t.size < 3:
        return ""
    from gcws.integration.method import deconv_value
    limit = deconv_value(method, "deconv_probe_r2")
    if plan.first is not None and plan.first.r2 < limit:
        return f"one component explains the trace with R² {plan.first.r2:.3f} < {limit:.3f}"
    return ""


def _span(component, shift: float) -> Optional[tuple[float, float]]:
    """Where ``component``'s elution profile carries SPAN_LEVEL of its maximum (trace time)."""
    t, y = _get(component, "profile_rt"), _get(component, "profile_y")
    if t is None or y is None or len(t) < 2 or len(t) != len(y):
        return None
    t, y = np.asarray(t, dtype=float), np.asarray(y, dtype=float)
    if not y.max() > 0:
        return None
    on = np.flatnonzero(y >= SPAN_LEVEL * y.max())
    return float(t[on[0]]) + shift, float(t[on[-1]]) + shift


def _may_pass(component, host, components, key: str, delay: float, min_area: float) -> bool:
    """Whether the hidden ``component`` could pass the area limit as a fragment of ``host``: its area
    estimated from the host's area in the proportion of the MS component areas (the host's own
    components and it) reaches ESTIMATE_SHARE of ``min_area``. The trace fits of an extended peak are
    the expensive part; most weak hidden components of a sensitive level fail here."""
    from gcws.ms.peak_split import candidates_in
    if not min_area > 0:
        return True
    own = sum(max(float(_get(c, "area", 0) or 0), 0.0) for c in candidates_in(components, host, key, delay))
    area = max(float(_get(component, "area", 0) or 0), 0.0)
    if not own > 0:
        return True
    return float(host.area) * area / (own + area) >= ESTIMATE_SHARE * min_area


def _valley(signal, t0: float, t1: float) -> float:
    """Time of the trace's lowest point in [t0, t1] (``t0`` without a point in the range)."""
    rt = np.asarray(signal.rt, dtype=float)
    inside = np.flatnonzero((rt >= t0) & (rt <= t1))
    if inside.size == 0:
        return float(t0)
    return float(rt[inside[int(np.argmin(np.asarray(signal.y, dtype=float)[inside]))]])


def _event_uid(key: str, peak, kind: str, t: float, comps) -> str:
    h = hashlib.sha1(f"{key}|{kind}|{peak.start:.5f}|{peak.end:.5f}|{t:.5f}".encode())
    for c in comps:
        h.update(f"|{float(_get(c, 'rt')):.4f}:{int(_get(c, 'model_mz'))}".encode())
    return f"{AUTO_UID}-{h.hexdigest()[:10]}"


def adoptions(signal, result, key: str, delay: float, components, method, events=()) -> list[Extension]:
    """The peaks of ``result`` to extend over components that have no peak of their own.

    A component is hidden when its apex (MS time + the detector delay) lies in no integrated peak.
    It is taken in by the neighbouring peak its elution reaches into (above SPAN_LEVEL of its
    profile; the one it reaches deeper into when both do), when it passes the method's S/N limit
    (at least ADOPT_MIN_SN) and its estimated area could pass the area limit (:func:`_may_pass`),
    its model ion is not excluded and neither the analyst's *Keep unsplit* marker nor a
    *Deconvolution split off* range lies on that peak. The peak is extended past the end (start)
    of the component's elution to the trace's lowest point within VALLEY_SPANS of its elution span,
    never into the next peak."""
    from gcws.core.events import ManualEvent, ManualKind as K
    from gcws.core.keys import is_fid
    from gcws.integration.method import deconv_value
    if result is None or not getattr(result, "peaks", None):
        return []
    shift = delay if is_fid(key) else 0.0
    exclude = {int(m) for m in (getattr(method, "deconv_exclude_mz", None) or [])}
    min_sn = max(deconv_value(method, "deconv_min_sn"), ADOPT_MIN_SN)
    min_area = limits_of(method).min_area
    off = method.deconv_off_ranges()
    kept = kept_spans(events)
    peaks = result.peaks
    tops = sorted((p for p in peaks if p.parent is None), key=lambda p: p.start)
    t_first = float(signal.rt[0]) if len(signal.rt) else -math.inf
    t_last = float(signal.rt[-1]) if len(signal.rt) else math.inf

    def eligible(p) -> bool:
        return (not p.negative and not p.is_solvent and p.area > 0 and not (p.extra or {}).get("deconv_component")
                and not _in_ranges(p.apex_rt, off)
                and not any(t0 - 1e-6 <= p.apex_rt <= t1 + 1e-6 or p.start - 1e-6 <= apex <= p.end + 1e-6
                            for t0, t1, apex in kept))

    plans: dict[int, dict] = {}
    for c in sorted(components or [], key=lambda c: float(_get(c, "rt"))):
        t = float(_get(c, "rt")) + shift
        if any(p.start <= t <= p.end for p in peaks) or _in_ranges(t, off):
            continue
        sn = _get(c, "s_n")
        if int(_get(c, "model_mz", 0) or 0) in exclude or (sn is not None and float(sn) < min_sn):
            continue
        span = _span(c, shift)
        if span is None:
            continue
        a, b = span
        left = max((i for i, p in enumerate(tops) if p.end <= t), key=lambda i: tops[i].end, default=None)
        right = min((i for i, p in enumerate(tops) if p.start >= t), key=lambda i: tops[i].start, default=None)
        reach = []
        if left is not None and tops[left].end > a:
            reach.append((tops[left].end - a, left, "end"))
        if right is not None and b > tops[right].start:
            reach.append((b - tops[right].start, right, "start"))
        if not reach:
            continue
        _depth, i, side = max(reach)
        host = tops[i]
        if not eligible(host) or not _may_pass(c, host, components, key, delay, min_area):
            continue
        if side == "end":
            limit = min((p.start for p in tops if p.start >= host.end and p is not host), default=t_last)
            new = _valley(signal, min(b, limit), min(b + VALLEY_SPANS * (b - a), limit, t_last))
            if new <= t:
                continue
            entry = plans.setdefault(i, {"start": host.start, "end": host.end, "comps": []})
            entry["end"] = max(entry["end"], new)
        else:
            limit = max((p.end for p in tops if p.end <= host.start and p is not host), default=t_first)
            new = _valley(signal, max(a - VALLEY_SPANS * (b - a), limit, t_first), max(a, limit))
            if new >= t:
                continue
            entry = plans.setdefault(i, {"start": host.start, "end": host.end, "comps": []})
            entry["start"] = min(entry["start"], new)
        entry["comps"].append(c)
    order = sorted(plans)
    for i, j in zip(order, order[1:]):             # two peaks extended into the same gap meet at its valley
        first, second = plans[i], plans[j]
        if first["end"] > second["start"]:
            cut = _valley(signal, second["start"], first["end"])
            first["end"], second["start"] = max(cut, tops[i].end), min(cut, tops[j].start)
    for i in order:
        entry = plans[i]
        entry["comps"] = [c for c in entry["comps"] if entry["start"] < float(_get(c, "rt")) + shift < entry["end"]]
        if not entry["comps"]:
            del plans[i]
    out = []
    for i, entry in sorted(plans.items()):
        host, comps = tops[i], entry["comps"]
        names = ", ".join(f"{float(_get(c, 'rt')):.3f} (m/z {int(_get(c, 'model_mz'))})" for c in comps)
        note = f"automatic deconvolution split: peak extended over the hidden component {names}"
        ext = Extension(float(host.apex_rt), float(entry["start"]), float(entry["end"]), [], comps,
                        (float(host.start), float(host.end)))
        if entry["end"] > host.end:
            ext.events.append(ManualEvent(K.MOVE_END, ext.end, ref_rt=host.apex_rt, comment=note, user="automatic",
                                          uid=_event_uid(key, host, "end", ext.end, comps)))
        if entry["start"] < host.start:
            ext.events.append(ManualEvent(K.MOVE_START, ext.start, ref_rt=host.apex_rt, comment=note,
                                          user="automatic", uid=_event_uid(key, host, "start", ext.start, comps)))
        out.append(ext)
    return out


def plan_peaks(signal, result, key: str, delay: float, components, method, events=(),
               scan_dt: Optional[float] = None, probe=None, adopted=(), only=None) -> RunPlan:
    """The automatic split events of ``result`` (integrated ``signal`` of ``key``).

    ``components`` are the whole-run deconvolution components (MS time), ``events`` the
    analyst's manual events of the signal (for the *Keep unsplit* markers). ``probe(peak)``
    returns the components of a closer look at one peak (:mod:`gcws.ms.deconv_probe`, MS time);
    it is asked for peaks with fewer than two whole-run components that :func:`suspect` flags.
    ``adopted`` are the components :func:`adoptions` extended a peak of ``result`` over; ``only(peak)``
    limits the planning to some peaks."""
    from gcws.ms.peak_split import candidates_in, plan_split, replan
    out = RunPlan(components=len(components or []))
    if result is None:
        return out
    if scan_dt is None:
        scan_dt = 0.0075
        for c in components:
            prt = _get(c, "profile_rt")
            if prt is not None and len(prt) > 1:
                scan_dt = float(np.median(np.diff(np.asarray(prt, dtype=float))))
                break
    limits = limits_of(method)
    off = method.deconv_off_ranges()
    kept = kept_spans(events)
    peaks = result.peaks
    for index, peak in enumerate(peaks):
        if peak.negative or peak.is_solvent or not peak.area > 0 or (peak.extra or {}).get("deconv_component"):
            continue
        if only is not None and not only(peak):
            continue
        if peak.parent is not None:          # a skimmed rider: split with its own parent's rules only
            continue
        riders = [(p.start, p.end) for p in peaks if p.parent == index]
        cands = candidates_in(components, peak, key, delay)
        if _in_ranges(peak.apex_rt, off):
            left = "deconvolution split switched off here"
        elif any(t0 - 1e-6 <= peak.apex_rt <= t1 + 1e-6 or peak.start - 1e-6 <= apex <= peak.end + 1e-6
                 for t0, t1, apex in kept):
            left = "kept unsplit by the analyst"
        else:
            left = ""
        closer = False
        if len(cands) < 2:
            why = suspect(signal, peak, key, delay, cands, riders, method) if probe is not None and not left else ""
            if not why:
                continue
            found = candidates_in(probe(peak), peak, key, delay)
            if len(found) < 2:
                if cands:                    # a peak without any MS component is not worth a note
                    out.skipped.append((peak.apex_rt, f"{why}; the {CLOSER} found {len(found)} component"
                                        + ("" if len(found) == 1 else "s")))
                continue
            cands, closer = found, True
        if left:
            out.skipped.append((peak.apex_rt, left))
            continue
        plan = plan_split(signal, peak, key, delay, cands, riders=riders, limits=limits, adopted=adopted)
        if plan.t.size < 3:
            out.skipped.append((peak.apex_rt, plan.problem))
            continue
        chosen, notes = gate(plan, method, scan_dt)
        if chosen != plan.checked:
            plan = replan(plan, chosen)
        if not plan.ok:
            if len(chosen) >= 2 or closer:
                out.skipped.append((peak.apex_rt, (CLOSER + ": " if closer else "") + plan.problem))
            continue
        event = plan.event()
        comps = [plan.candidates[i].component for i in plan.checked]
        how = "fitted to the trace" if plan.basis == "fit" else "MS component proportions"
        comment = f"automatic deconvolution split ({how}; {plan.summary()}"
        comment += f"; {CLOSER})" if closer else ")"
        if notes:
            comment += "; not split off: " + ", ".join(notes)
        out.events.append(event.with_(uid=_uid(key, peak, comps), comment=comment, user="automatic"))
        out.plans.append(plan)
    return out


def settings_for_method(ws, method):
    from gcws.ms import deconv as D, deconv_cache as DC
    return D.settings_for_level(DC.settings_of(ws), getattr(method, "deconv_level", 3))


def components_for(ws, st, settings=None, compute: bool = True):
    """The whole-run components of ``st`` after the TIC solvent cut (cached), or None when they are
    not available (no MS, or not computed and ``compute`` is False)."""
    from gcws.ms import deconv_cache as DC
    if st.run.ms is None or not getattr(st.run.ms, "n_scans", 0):
        return None
    settings = settings or DC.settings_of(ws)
    comps = DC.whole_run(st, settings)
    if comps is None:
        if not compute:
            return None
        comps = DC.compute_whole_run(st, settings, t_min=ws.solvent_cut(st, "TIC"))
        DC.store_whole_run(st, settings, comps)
    cut = ws.solvent_cut(st, "TIC")
    return [c for c in comps if cut is None or c.rt >= cut]


def plan_run(ws, st, key: str, result, method, compute: bool = True) -> Optional[RunPlan]:
    """:func:`plan_peaks` for a run of the workspace; None when the components are not available."""
    if not enabled(method):
        return RunPlan()
    signal = st.run.signal(key)
    settings = settings_for_method(ws, method)
    comps = components_for(ws, st, settings=settings, compute=compute)
    if comps is None or signal is None:
        return None
    from gcws.ms import deconv_cache as DC
    from gcws.ms.spectra import ms_times
    def probe(peak):
        return DC.probe(st, *ms_times(peak, key, st.delay_value), settings)
    plan = plan_peaks(signal, result, key, st.delay_value, comps, method, st.events(key), probe=probe)
    extensions = adoptions(signal, result, key, st.delay_value, comps, method, st.events(key))
    if not extensions:
        return plan
    from gcws.integration.engine import integrate
    extended = integrate(signal, method, list(st.events(key)) + [e for x in extensions for e in x.events],
                         t_min=ws.solvent_cut(st, key))
    wider = plan_peaks(signal, extended, key, st.delay_value, comps, method, st.events(key), probe=probe,
                       adopted=[c for x in extensions for c in x.components],
                       only=lambda peak: any(x.holds(peak) for x in extensions))
    return merge_extended(plan, wider, extensions)


def merge_extended(plan: RunPlan, wider: RunPlan, extensions: list) -> RunPlan:
    """``plan`` (the integrated peaks) with the split of each extended peak of ``wider`` that is
    fitted to the trace and splits off an adopted component; that peak's own plan is replaced (its
    extension kept), every other peak keeps its plan."""
    good = {}
    for event, split in zip(wider.events, wider.plans):
        x = next((x for x in extensions if x.holds(split.peak)), None)
        if x is not None and split.basis == "fit" and any(split.candidates[i].adopted for i in split.checked):
            good[id(x)] = (x, event, split)
    if not good:
        return plan
    out = RunPlan(components=plan.components)
    pairs = [(e, s) for e, s in zip(plan.events, plan.plans)
             if not any(x.extends(s.peak) for x, _e, _s in good.values())]
    pairs += [(e, s) for _x, e, s in good.values()]
    pairs.sort(key=lambda pair: pair[1].peak.start)
    out.events = [e for e, _s in pairs]
    out.plans = [s for _e, s in pairs]
    apexes = {round(x.apex, 6) for x, _e, _s in good.values()}
    out.skipped = [(a, why) for a, why in plan.skipped if round(a, 6) not in apexes]
    out.extensions = sorted((x for x, _e, _s in good.values()), key=lambda x: x.start)
    return out

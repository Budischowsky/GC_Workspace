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


@dataclass
class RunPlan:
    """The automatic splits of one run and signal, and what was left alone (for notes)."""
    events: list = field(default_factory=list)          # ManualEvent, RT order
    plans: list = field(default_factory=list)           # SplitPlan per event
    skipped: list = field(default_factory=list)         # (apex, reason) of peaks with >= 2 components
    components: int = 0

    @property
    def ms_basis(self) -> int:
        return sum(1 for p in self.plans if p.basis == "ms")


def enabled(method) -> bool:
    return getattr(method, "deconv_split", "off") == "auto"


def is_auto(event: ManualEvent) -> bool:
    return event.uid.startswith(AUTO_UID + "-")


def keep_marker(peak, comment: str = "") -> ManualEvent:
    """The analyst's *Keep unsplit* marker for ``peak`` (replayed as a no-op)."""
    return ManualEvent(K.SPLIT, float(peak.start), float(peak.end), ref_rt=float(peak.apex_rt),
                       option=KEEP_OPTION, comment=comment or "keep unsplit: no automatic deconvolution split")


def kept_spans(events) -> list[tuple[float, float, float]]:
    """``(t0, t1, apex)`` of the enabled *Keep unsplit* markers."""
    return [(float(e.t0), float(e.t1 if e.t1 is not None else e.t0),
             float(e.ref_rt if e.ref_rt is not None else e.t0))
            for e in events if e.enabled and e.kind == K.SPLIT and e.option == KEEP_OPTION]


def limits_of(method):
    from gcws.integration.method import deconv_value
    from gcws.ms.peak_split import Limits
    return Limits(min_sn=deconv_value(method, "deconv_min_sn"),
                  min_share=deconv_value(method, "deconv_min_share"),
                  fit_r2=deconv_value(method, "deconv_fit_r2"))


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _uid(key: str, peak, components) -> str:
    h = hashlib.sha1(f"{key}|{peak.start:.5f}|{peak.end:.5f}".encode())
    for c in components:
        h.update(f"|{float(_get(c, 'rt')):.4f}:{int(_get(c, 'model_mz'))}".encode())
    return f"{AUTO_UID}-{h.hexdigest()[:10]}"


def shape_r(plan, index: int) -> float:
    """Pearson r of candidate ``index``'s fitted curve with the trace minus the other candidates'
    curves, over the points where the curve carries signal (>= 5 % of its maximum)."""
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
    return pearson(own[use], rest[use])


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
    from gcws.ms.deconv_probe import trace_shoulders
    from gcws.ms.peak_split import plan_split
    plan = plan_split(signal, peak, key, delay, cands, riders=riders)
    if plan.t.size < 3:
        return ""
    if not riders:
        level = max(1, min(5, int(getattr(method, "deconv_level", 3))))
        depth, prominence = ((0.04, 0.02) if level == 5 else
                             (0.06, 0.03) if level == 4 else (0.08, 0.04))
        found = trace_shoulders(plan.t, plan.y, depth, prominence)
        if found:
            return "shoulder at " + ", ".join(f"{t:.3f}" for t in found)
    from gcws.integration.method import deconv_value
    limit = deconv_value(method, "deconv_probe_r2")
    if plan.first is not None and plan.first.r2 < limit:
        return f"one component explains the trace with R² {plan.first.r2:.3f} < {limit:.3f}"
    return ""


def plan_peaks(signal, result, key: str, delay: float, components, method, events=(),
               scan_dt: Optional[float] = None, probe=None) -> RunPlan:
    """The automatic split events of ``result`` (integrated ``signal`` of ``key``).

    ``components`` are the whole-run deconvolution components (MS time), ``events`` the
    analyst's manual events of the signal (for the *Keep unsplit* markers). ``probe(peak)``
    returns the components of a closer look at one peak (:mod:`gcws.ms.deconv_probe`, MS time);
    it is asked for peaks with fewer than two whole-run components that :func:`suspect` flags."""
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
        plan = plan_split(signal, peak, key, delay, cands, riders=riders, limits=limits)
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
    return plan_peaks(signal, result, key, st.delay_value, comps, method, st.events(key), probe=probe)

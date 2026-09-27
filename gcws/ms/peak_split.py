"""Plan the split of one FID or TIC peak into its deconvoluted components.

The plan is headless and cheap (a few milliseconds): the deconvolution window
shows it as a preview and :meth:`SplitPlan.event` turns it into the replayable
manual event of :mod:`gcws.integration.deconv_split`.

1. The candidates are the components whose apex (MS time + detector delay)
   lies inside the integrated peak.
2. Their MS elution shapes are fitted to the trace above the peak's baseline
   (:mod:`gcws.ms.component_fit`). Components with a low S/N or almost no
   share of the fitted signal are listed, but not checked by default.
3. The checked components are fitted again. A good fit (R^2 >= FIT_MIN_R2,
   no two curves alike) makes the fitted areas the weights: the relative areas
   then come from the trace itself (FID response, not MS response). Otherwise
   the weights are the MS component areas, and the plan says why.
4. The parent's area is allocated by those weights, exactly conserving its
   total; the cuts sit where neighbouring fitted curves cross.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from gcws.core.keys import base_key, is_fid
from gcws.ms import component_fit as F


@dataclass
class Candidate:
    component: object
    shape: Optional[F.Shape]
    suggested: bool = True
    reason: str = ""            # why the component is not suggested
    share: float = math.nan     # share of the fit over all candidates


@dataclass
class SplitPlan:
    key: str
    delay: float
    peak: object
    candidates: list[Candidate]
    checked: list[int]                      # indices into ``candidates`` (RT order)
    t: np.ndarray = field(default_factory=lambda: np.empty(0))   # trace window
    y: np.ndarray = field(default_factory=lambda: np.empty(0))   # trace - baseline
    mask: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=bool))
    first: Optional[F.TraceFit] = None      # fit of all candidates (default check, shares)
    fit: Optional[F.TraceFit] = None        # fit of the checked components
    basis: str = "ms"                       # "fit" | "ms"
    basis_note: str = ""
    weights: list[float] = field(default_factory=list)
    shares: list[float] = field(default_factory=list)
    areas: list[float] = field(default_factory=list)
    points: list[float] = field(default_factory=list)
    problem: str = ""                       # why the peak cannot be split ("" = it can)

    @property
    def ok(self) -> bool:
        return not self.problem

    @property
    def signal_name(self) -> str:
        return base_key(self.key)

    @property
    def shift0(self) -> float:
        """The detector offset that maps MS time to the trace (0 for an MS trace)."""
        return self.delay if is_fid(self.key) else 0.0

    def share_of(self, index: int) -> Optional[float]:
        """Allocated share of candidate ``index`` (None when it is not checked)."""
        if index in self.checked and len(self.shares) == len(self.checked):
            return self.shares[self.checked.index(index)]
        return None

    def area_of(self, index: int) -> Optional[float]:
        if index in self.checked and len(self.areas) == len(self.checked):
            return self.areas[self.checked.index(index)]
        return None

    def summary(self) -> str:
        if self.fit is not None:
            text = f"Fit to {self.signal_name}: R² {self.fit.r2:.3f}"
        else:
            text = "No trace fit"
        text += f" · {len(self.checked)} of {len(self.candidates)} components used"
        if self.basis == "ms" and self.checked:
            text += " · areas from MS component proportions"
            if self.basis_note:
                text += f" ({self.basis_note})"
        return text

    def event(self):
        """The replayable split event (raises ``ValueError`` if the plan is not splittable)."""
        from gcws.integration.deconv_split import create_event
        if self.problem:
            raise ValueError(self.problem)
        comps = [self.candidates[i] for i in self.checked]
        fit = None
        if self.fit is not None:
            fit = {"shift": self.fit.shift, "stretch": self.fit.stretch, "r2": self.fit.r2}
        return create_event(self.peak, [c.component for c in comps], self.shift0, signal_key=self.key,
                            weights=self.weights, points=self.points, fit=fit, basis=self.basis,
                            profiles=[c.shape.points() if c.shape is not None else None for c in comps])


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def trace_window(signal, peak, riders: Sequence[tuple[float, float]] = ()):
    """``(t, y - baseline, mask)`` of the peak; ``mask`` drops skimmed riders."""
    sl = signal.window(peak.start, peak.end)
    t = np.asarray(signal.rt[sl], dtype=float)
    y = np.asarray(signal.y[sl], dtype=float) - peak.baseline.eval(t)
    mask = np.ones(t.size, dtype=bool)
    for lo, hi in riders:
        mask &= ~((t >= lo) & (t <= hi))
    return t, y, mask


def candidates_in(components, peak, key: str, delay: float) -> list:
    """Components whose apex, mapped to the trace's time, lies inside the peak (RT order)."""
    shift = delay if is_fid(key) else 0.0
    return sorted((c for c in components if peak.start <= float(_get(c, "rt")) + shift <= peak.end),
                  key=lambda c: float(_get(c, "rt")))


def _weak_reason(component, share: float) -> str:
    sn = _get(component, "s_n")
    if sn is not None and math.isfinite(float(sn)) and 0 < float(sn) < F.SUGGEST_MIN_SN:
        return f"S/N {float(sn):.0f} < {F.SUGGEST_MIN_SN:.0f}"
    if math.isfinite(share) and share < F.SUGGEST_MIN_SHARE:
        return f"{100 * share:.1f} % of the signal"
    return ""


def plan_split(signal, peak, key: str, delay: float, components, checked: Optional[Sequence[int]] = None,
               riders: Sequence[tuple[float, float]] = ()) -> SplitPlan:
    """Plan the split of ``peak`` on ``signal`` (the trace of ``key``).

    ``components`` may be all components of the deconvolution window; only those
    inside the peak become candidates. ``checked`` are candidate indices; None
    checks the suggested ones.
    """
    delay = float(delay)
    shift0 = delay if is_fid(key) else 0.0
    cands = [Candidate(c, F.Shape.of(c)) for c in candidates_in(components, peak, key, delay)]
    plan = SplitPlan(key, delay, peak, cands, [])
    if base_key(key) not in ("FID", "TIC"):
        plan.problem = "Splitting by components supports FID and TIC peaks only."
        return plan
    if getattr(peak, "negative", False):
        plan.problem = "A negative peak cannot be split by components."
        return plan
    if not peak.area > 0:
        plan.problem = "The original peak area must be positive for a split."
        return plan
    plan.t, plan.y, plan.mask = trace_window(signal, peak, riders)
    if plan.t.size < 3:
        plan.problem = "The peak has too few measured points."
        return plan
    shapes_ok = bool(cands) and all(c.shape is not None for c in cands)
    if shapes_ok:
        first = plan.first = F.fit_trace(plan.t, plan.y, [c.shape for c in cands], shift0, mask=plan.mask)
        for c, share in zip(cands, first.shares):
            c.share = float(share)
    elif cands:
        areas = np.array([max(float(_get(c.component, "area", 0) or 0), 0.0) for c in cands])
        for c, a in zip(cands, areas):
            c.share = float(a / areas.sum()) if areas.sum() > 0 else math.nan
    for c in cands:
        c.reason = _weak_reason(c.component, c.share)
        c.suggested = not c.reason
    plan.checked = sorted(set(checked)) if checked is not None else [i for i, c in enumerate(cands) if c.suggested]
    plan.checked = [i for i in plan.checked if 0 <= i < len(cands)]
    _allocate(plan, shift0)
    return plan


def replan(plan: SplitPlan, checked: Sequence[int]) -> SplitPlan:
    """``plan`` with other checked candidates: one fit, the same candidates and suggestions."""
    new = SplitPlan(plan.key, plan.delay, plan.peak, plan.candidates,
                    sorted({i for i in checked if 0 <= i < len(plan.candidates)}),
                    t=plan.t, y=plan.y, mask=plan.mask, first=plan.first)
    if plan.t.size < 3:                  # refused before any fit: nothing a selection can change
        new.problem = plan.problem
        return new
    _allocate(new, plan.shift0)
    return new


def _allocate(plan: SplitPlan, shift0: float) -> None:
    from gcws.integration.deconv_split import share_exactly
    cands, peak = plan.candidates, plan.peak
    used = [cands[i] for i in plan.checked]
    if not cands:
        plan.problem = "No deconvoluted component lies inside the peak."
        return
    if len(used) < 2:
        plan.problem = ("One component explains the peak: nothing to split." if used
                        else "Check at least two components.")
    if plan.first is not None and len(used) == len(cands):
        plan.fit = plan.first
    elif used and all(c.shape is not None for c in used):
        plan.fit = F.fit_trace(plan.t, plan.y, [c.shape for c in used], shift0, mask=plan.mask)
    if plan.problem:
        return
    fit = plan.fit
    if fit is None:
        plan.basis_note = "no elution profile"
    elif fit.r2 < F.FIT_MIN_R2:
        plan.basis_note = f"fit R² {fit.r2:.3f} < {F.FIT_MIN_R2:.2f}"
    elif fit.collinear is not None:
        i, j = fit.collinear
        plan.basis_note = (f"components {float(_get(used[i].component, 'rt')):.3f} and "
                           f"{float(_get(used[j].component, 'rt')):.3f} are not resolved in the "
                           f"{plan.signal_name} signal")
    if fit is not None and not plan.basis_note:
        empty = [c for c, a in zip(used, fit.areas) if not a > 0]
        if empty:
            plan.problem = (f"Component {float(_get(empty[0].component, 'rt')):.3f} takes no "
                            f"{plan.signal_name} signal in the fit; uncheck it.")
            return
        plan.basis = "fit"
        plan.weights = [float(a) for a in fit.areas]
        apexes = [float(_get(c.component, "rt")) + fit.shift for c in used]
        plan.points = F.cut_points(plan.t, fit.curves, apexes)
    else:
        plan.basis = "ms"
        plan.weights = [float(_get(c.component, "area", 0) or 0) for c in used]
        if not all(w > 0 for w in plan.weights):
            plan.problem = "Every component needs a positive MS area."
            return
        apexes = [float(_get(c.component, "rt")) + shift0 for c in used]
        plan.points = [(a + b) / 2 for a, b in zip(apexes, apexes[1:])]
    bounds = [peak.start, *plan.points, peak.end]
    for lo, apex, hi in zip(bounds, apexes, bounds[1:]):
        if not lo < hi:
            plan.problem = "The cut points do not lie inside the peak in RT order."
            return
        if not lo <= apex <= hi:
            plan.problem = f"Component {apex:.3f} lies outside its fragment after alignment."
            return
        if int(np.count_nonzero((plan.t >= lo) & (plan.t <= hi))) < 3:
            plan.problem = "A fragment would contain fewer than three measured points."
            return
    plan.shares = share_exactly(1.0, plan.weights)
    plan.areas = share_exactly(peak.area, plan.weights)

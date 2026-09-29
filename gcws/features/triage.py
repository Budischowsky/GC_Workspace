"""The traffic light of a feature: only the exceptions need the analyst.

* **grey** -- not reported anyway: below the reporting limit, or at blank level.
* **green** -- found in every determination, the same substance (similar spectra, or the same
  retention time where the spectra are too weak to compare), one identification, the
  difference within the limit: taken over as it is.
* **yellow** -- made consistent automatically, a quick look is enough: a gap fill, a name taken
  from the consensus of the hit lists, two candidates that cannot be separated, a difference
  between the limit and 1.5 x the limit, spectra only partly similar.
* **red** -- the analyst decides: found in one determination only and not detectable in the
  other (not reported), different spectra at the same retention time, a peak integrated as one
  here and as two there, an ambiguous pairing, a difference above 1.5 x the limit.

``classify`` returns ``(light, verdict text, reasons)``; the verdict texts keep the words the
double-determination panel counts ("Confirmed", "Only in A", "Deviation ...", "Below reporting
limit").
"""
from __future__ import annotations

from typing import Optional

from gcws.features.consensus import SOURCE as CONSENSUS_SOURCE
from gcws.features.model import GAPFILL, NOT_DETECTABLE, Feature, Settings

LEVEL = {"green": "ok", "yellow": "warn", "red": "bad", "grey": "neutral"}


def _below(row: dict, reporting_limit: float) -> bool:
    import gc_duplicate as GD
    return bool(reporting_limit) and GD.below_limit(GD.DuplicateRow(row), reporting_limit)


def classify(feature: Feature, row: dict, limit: float, reporting_limit: float, settings: Settings,
             labels: Optional[list] = None) -> tuple[str, str, list]:
    labels = labels or [m.label for m in feature.members]
    found = feature.found
    reasons: list[str] = []
    ident = feature.identity
    rd = row.get("reldiff")
    missing = [m for m in feature.members if not m.found]
    if _below(row, reporting_limit):
        return "grey", "Below reporting limit", [f"below the reporting limit {reporting_limit:g}"]
    if any(m.peak.blank_level for m in found) and not any(m.peak.istd for m in found):
        return "grey", "Blank level", ["at blank level"]
    # red
    if missing:
        which = "/".join(m.label for m in found)
        for m in missing:
            reasons.append(f"{m.label}: " + (m.note or ("not detectable" if m.origin == NOT_DETECTABLE
                                                        else "not searched")))
        if feature.split:
            reasons.append("integrated as one peak in one determination and as two in the other")
        return "red", f"Only in {which}", reasons
    if feature.mismatch:
        return "red", "Spectra differ", [f"different spectra at the same retention time (similarity "
                                         f"{feature.sim:.2f}): co-elution?"]
    if feature.split:
        reasons.append("integrated as one peak in one determination and as two in the other")
    if feature.ambiguous:
        reasons.append("pairing ambiguous: another peak fits almost as well")
    if rd is not None and rd > 1.5 * limit:
        reasons.insert(0, f"deviation {rd:.0f} % > {1.5 * limit:g} %")
        return "red", f"Deviation {rd:.0f} % > {limit:g} %", reasons
    if reasons:
        return "red", "Check pairing", reasons
    # yellow: (verdict text, reason) in order of importance; the first one names the row
    tagged: list[tuple[str, str]] = []
    gap = [m for m in found if m.origin == GAPFILL]
    for m in gap:
        tagged.append(("Check: gap fill", f"gap fill in {m.label}" + (f": {m.note}" if m.note else "")))
    if rd is not None and rd > limit:
        tagged.append((f"Deviation {rd:.0f} % > {limit:g} %", f"deviation {rd:.0f} % > {limit:g} %"))
    if ident is not None and ident.case == "C":
        tagged.append(("Check: 2 candidates", f"candidates: {ident.name}" + (f" ({ident.basis})" if ident.basis else "")))
    elif ident is not None and ident.case == "B":
        tagged.append(("Check: name by consensus", f"name from the determinations together: {ident.basis}"))
    elif ident is not None and ident.case == "A" and "below" in (ident.basis or ""):
        tagged.append(("Check: name by consensus", f"name confirmed by the other determination ({ident.basis})"))
    elif any(getattr(m.peak.ident, "source", "") == CONSENSUS_SOURCE for m in found):
        tagged.append(("Check: name by consensus", "name set by the double determination (consensus of the hit lists)"))
    if feature.sim is not None and feature.sim < settings.green_sim:
        tagged.append(("Check: spectra", f"spectra only partly similar ({feature.sim:.2f})"))
    if feature.sim is None and len(found) > 1:
        d = max(m.rt_ref for m in found) - min(m.rt_ref for m in found)
        if d > 0.5 * settings.rt_tol:
            tagged.append(("Check: retention time", f"paired by retention time only ({d * 60:.1f} s apart)"))
    bounds = [p for p in feature.proposals if p.kind == "boundary"]
    if bounds:
        tagged.append(("Check: boundaries",
                       "integration boundaries differ; proposal: " + bounds[0].text.split(": ", 1)[-1]))
    if tagged:
        return "yellow", tagged[0][0], [r for _t, r in tagged]
    detail = []
    if rd is not None:
        detail.append(f"difference {rd:.1f} %")
    detail.append(f"similarity {feature.sim:.2f}" if feature.sim is not None else "paired by retention time")
    return "green", "Confirmed", detail

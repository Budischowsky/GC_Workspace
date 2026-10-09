"""The feature table as AutoLib's merged rows (what the reports and the panel read).

Every row has the shape of ``AutoLib.combine_determinations`` -- ``rt, name, cas, mean, c1, c2,
reldiff, status, id_status, review, source1, source2`` plus the N-fold keys of
:func:`gcws.quant.replicates._decorate` -- so the NIAS workbook, its colour rules and the
analyst's edits work unchanged. The numbers are AutoLib's: ``mean`` is the mean of the
determinations' concentrations, ``reldiff = |c1 - c2| / mean x 100``; a substance found in one
determination only has no mean ("Artefact: only determination N", not reported). The pairing,
the name and the review text come from the features; the status vocabulary stays AutoLib's.

Extra keys: ``feature_id``, ``light`` (green/yellow/red/grey), ``verdict`` (text), ``reasons``,
``sim``, ``gapfill`` (labels of gap-filled determinations), ``candidates``, ``edit_key``.
"""
from __future__ import annotations

import statistics
from typing import Optional

from gcws.features import consensus as CO
from gcws.features import triage as TR
from gcws.features.model import GAPFILL, NOT_DETECTABLE, FeatureTable, Settings


def _index(lst: list) -> dict:
    out = {}
    for d in lst or []:
        n = d.get("fid_peak")
        if n is not None:
            out[int(n) - 1] = d
    return out


def _review_de(feature, labels, n) -> list[str]:
    """Review texts for the workbook, in German like AutoLib's."""
    out = []
    for k, m in enumerate(feature.members):
        if m.origin == GAPFILL:
            out.append(f"Bestimmung {k + 1}: Peak durch Lückenfüllung nachintegriert ({m.note})")
        elif m.origin == NOT_DETECTABLE:
            out.append(f"Bestimmung {k + 1}: nicht nachweisbar ({m.note})")
    if feature.mismatch:
        out.append(f"Spektren der Bestimmungen unterschiedlich (Ähnlichkeit {feature.sim:.2f}); Koelution prüfen")
    if feature.split:
        out.append("in einer Bestimmung als ein Peak, in der anderen als zwei Peaks integriert")
    ident = feature.identity
    if ident is not None and ident.case == "C":
        out.append("Identifikation uneindeutig: " + "; ".join(
            f"{c['name']} ({c['score']:.0f})" for c in ident.candidates[:2]))
    if ident is not None and ident.case == "B":
        out.append(f"Identifikation aus beiden Bestimmungen ({ident.basis})")
    return out


def row_of(feature, sources: list, labels: list, required: int) -> dict:
    n = len(sources)
    present = [s for s in sources if s is not None]
    cs = [s["mg_kg"] if s is not None else None for s in sources]
    vals = [c for c in cs if c is not None]
    mean = statistics.mean(vals) if vals else None
    ident = feature.identity
    if feature.mismatch:
        status = "Identification conflict"
    elif len(present) >= required and len(present) >= 2:
        status = "Valid duplicate" if n == 2 and len(present) == 2 else f"Valid replicate ({len(present)}/{n})"
    elif n == 1:
        status = "Einzelbestimmung"
    else:
        which = ", ".join(str(k + 1) for k in range(n) if sources[k] is not None)
        status = f"Artefact: only determination {which}"
        mean = None
    if any(s.get("blank_area") or s.get("blank_istd_area") for s in present):
        status += ", also in Blank"
    reviews = [s["review"] for s in present if s.get("review")]
    if status.startswith("Artefact"):
        which = ", ".join(str(k + 1) for k in range(n) if sources[k] is not None)
        reviews.append(f"Nur in Bestimmung {which} detektiert; als Artefakt bewertet")
    reviews += _review_de(feature, labels, n)
    # name and identification status
    single = present[0] if len(present) == 1 else None
    learned = next((s for s in present if s.get("learned")), None)
    if learned is not None:
        # an approved learned rule (gcws.learn family) named the row: it wins over the consensus name
        name, cas, id_status = learned["name"], "", learned.get("id_status", "")
    elif ident is None or ident.case in ("none",) or not ident.name:
        name = (single or present[0])["name"]
        cas = (single or present[0])["cas"]
        id_status = (single or present[0]).get("id_status", "")
    elif ident.case == "U":
        name, cas = ident.name, (single or present[0])["cas"]
        id_status = (single["id_status"] if single else
                     (present[0]["id_status"] if all(s.get("id_status") == present[0].get("id_status")
                                                     for s in present) else "Manual review"))
    else:
        name, cas = ident.name, ident.cas
        id_status = {CO.ACCEPTED: "Accepted", CO.REVIEW: "Manual review",
                     CO.CONFLICT: "Conflict; manual review"}.get(ident.status, ident.status or "Manual review")
    if len(vals) == 2 and n == 2 and mean:
        reldiff = abs(vals[0] - vals[1]) / mean * 100.0
    elif len(vals) >= 2 and mean:
        reldiff = statistics.stdev(vals) / mean * 100.0
    else:
        reldiff = None
    rt = statistics.mean(s["rt"] for s in present)
    return {"rt": rt, "name": name, "cas": cas or "", "mean": mean,
            "c1": cs[0], "c2": cs[1] if n > 1 else None, "reldiff": reldiff,
            "status": status, "id_status": id_status, "review": "; ".join(dict.fromkeys(reviews)),
            "source1": sources[0], "source2": sources[1] if n > 1 else None}


def rows(table: FeatureTable, engine_lists: list[list], limit: float, reporting_limit: float,
         policy: str = "all", settings: Optional[Settings] = None) -> list[dict]:
    """AutoLib-shaped rows of ``table`` (``engine_lists``: the determinations' engine peaks, in
    ``table.members`` order); sets ``light``/``reasons`` on the features too."""
    from gcws.quant.replicates import _stats, required
    settings = settings or table.settings
    n = len(table.members)
    need = required(policy, n)
    index = [_index(lst) for lst in engine_lists]
    labels = list(table.labels)
    out = []
    for f in table.features:
        sources = []
        for k, m in enumerate(f.members):
            sources.append(index[k].get(m.peak.index) if (m.found and k < len(index)) else None)
        if not any(s is not None for s in sources):
            continue                                  # before the solvent end, or no quantification row
        row = row_of(f, sources, labels, need)
        light, text, reasons = TR.classify(f, row, limit, reporting_limit, settings, labels)
        f.light, f.reasons = light, reasons
        f.rpd = row.get("reldiff")
        row.update(feature_id=f.id, light=light, verdict=text, reasons=list(reasons), sim=f.sim,
                   gapfill=[m.label for m in f.members if m.origin == GAPFILL],
                   candidates=list(f.identity.candidates) if f.identity else [], edit_key=f.id,
                   _sources=sources)
        out.append(row)
    for r in out:                                     # the N-fold keys (replicates._decorate for any N)
        srcs = r.pop("_sources")
        cs = [s["mg_kg"] if s is not None else None for s in srcs]
        _present, _mean, sd, rsd = _stats(cs)
        r.update(cs=cs, sources=srcs, sd=sd, rsd=rsd, n_detected=sum(1 for s in srcs if s is not None), n=n)
    return sorted(out, key=lambda r: r["rt"])

"""Double determination in plain language.

The pairing and the numbers come unchanged from AutoLib (``combine``); the
rules that colour a row are the workbook's own (``gc_duplicate``). This module
only turns them into verdicts an analyst can read at a glance, and translates
the German AutoLib review texts for display (reports keep the originals).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VERDICT_ORDER = {"bad": 0, "warn": 1, "info": 2, "neutral": 3, "ok": 4}

_TRANSLATIONS = [
    (r"Abweichende Identifikationen bei vergleichbarer RT; keine automatische Endidentifikation",
     "different identifications at comparable RT; no automatic final identification"),
    (r"Nur in Bestimmung ([\d, ]+) detektiert; als Artefakt bewertet",
     r"only detected in determination \1; treated as an artefact"),
    (r"ISTD geschützt; keine Blanksubtraktion", "ISTD protected; no blank subtraction"),
    (r"kein passendes Blanksignal", "no matching blank signal"),
    (r"keine MS-Zuordnung; Prüfung", "no MS assignment; check"),
    (r"semiquantitativ; keine Konzentration berechnet", "semi-quantitative; no concentration calculated"),
    (r"subtrahiert \(Maximum aus Blank/Blank\+ISTD\)", "subtracted (maximum of Blank / Blank+ISTD)"),
    (r"auch im Blank", "also in the blank"),
    (r"unsichere Identifikation", "uncertain identification"),
    (r"Manuelle Prüfung", "manual review"),
    (r"Einzelbestimmung", "single determination"),
]


def english(text: str) -> str:
    out = text or ""
    for pat, rep in _TRANSLATIONS:
        out = re.sub(pat, rep, out)
    return out


@dataclass
class Verdict:
    level: str          # ok | warn | bad | info | neutral
    text: str           # short, for the table
    detail: str = ""    # tooltip


@dataclass
class Summary:
    total: int = 0
    confirmed: int = 0
    deviating: int = 0
    only_a: int = 0
    only_b: int = 0
    conflicts: int = 0
    below: int = 0
    no_conc: int = 0
    mean_reldiff: float | None = None
    level: str = "neutral"
    text: str = ""


def _fmt(v, digits=4):
    return "-" if v is None else f"{v:.{digits}g}"


def plain_verdict(row: dict, limit: float, reporting_limit: float = 0.0, labels=("A", "B"), unit: str = "") -> Verdict:
    import gc_duplicate as GD
    dr = GD.DuplicateRow(row)
    status = row.get("status") or ""
    blank = ", also in Blank" in status
    note = " Also found in the blank." if blank else ""
    s1, s2 = row.get("source1"), row.get("source2")
    if status.startswith("Einzelbestimmung"):
        return Verdict("neutral", "Single determination", "Only one determination in this group." + note)
    if status.startswith("Identification conflict"):
        n1 = (s1 or {}).get("name", "?")
        n2 = (s2 or {}).get("name", "?")
        return Verdict("bad", "Identifications differ",
                       f"{labels[0]}: {n1}  /  {labels[1]}: {n2}. Compare both spectra and decide." + note)
    if status.startswith("Artefact"):
        which = labels[0] if s1 is not None and s2 is None else labels[1]
        return Verdict("bad", f"Only in {which}",
                       f"Found in determination {which} only: treated as an artefact and not reported." + note)
    c1, c2 = row.get("c1"), row.get("c2")
    if row.get("mean") is None and (c1 is None or c2 is None):
        return Verdict("info", "Confirmed, no concentration",
                       "Found in both determinations, but no concentration yet (ISTD factor missing)." + note)
    if reporting_limit and GD.below_limit(dr, reporting_limit):
        return Verdict("neutral", "Below reporting limit",
                       f"Mean {_fmt(row.get('mean'))} {unit} is below the reporting limit {reporting_limit:g}." + note)
    rd = row.get("reldiff")
    if GD.exceeds_reldiff(dr, limit, reporting_limit=reporting_limit or 0.0):
        return Verdict("warn", f"Deviation {rd:.0f} % > {limit:g} %",
                       f"{labels[0]} = {_fmt(c1)}, {labels[1]} = {_fmt(c2)} {unit}: check the integration of both "
                       "peaks (baseline, splits) before reporting the mean." + note)
    if (row.get("id_status") or "") != "Accepted":
        return Verdict("warn", "Confirmed, check identification",
                       f"Found in both (difference {rd:.0f} %), but the identification is "
                       f"'{english(row.get('id_status', ''))}'." + note if rd is not None else
                       "Found in both determinations; identification to review." + note)
    return Verdict("ok", "Confirmed" + (" (in blank)" if blank else ""),
                   (f"Difference {rd:.1f} % (limit {limit:g} %)." if rd is not None else "Found in both.") + note)


def summarize(rows: list[dict], verdicts: list[Verdict], limit: float, labels=("A", "B")) -> Summary:
    s = Summary(total=len(rows))
    diffs = []
    for row, v in zip(rows, verdicts):
        if v.text.startswith("Confirmed"):
            s.confirmed += 1
            if row.get("reldiff") is not None:
                diffs.append(row["reldiff"])
            if v.text.startswith("Confirmed, no"):
                s.no_conc += 1
        elif v.text.startswith("Deviation"):
            s.deviating += 1
            if row.get("reldiff") is not None:
                diffs.append(row["reldiff"])
        elif v.text == f"Only in {labels[0]}":
            s.only_a += 1
        elif v.text == f"Only in {labels[1]}":
            s.only_b += 1
        elif v.text.startswith("Identifications"):
            s.conflicts += 1
        elif v.text.startswith("Below"):
            s.below += 1
    s.mean_reldiff = sum(diffs) / len(diffs) if diffs else None
    paired = s.confirmed + s.deviating
    if not rows:
        s.level, s.text = "neutral", "No substances to compare."
    elif s.deviating == 0 and s.conflicts == 0:
        s.level = "ok"
        s.text = f"Double determination consistent: {s.confirmed} of {s.total} substances confirmed in both."
    else:
        s.level = "warn" if s.conflicts == 0 else "bad"
        issues = []
        if s.deviating:
            issues.append(f"{s.deviating} deviate by more than {limit:g} %")
        if s.conflicts:
            issues.append(f"{s.conflicts} with differing identifications")
        s.text = f"{paired} substances in both determinations; " + " and ".join(issues) + ": review before reporting."
    if s.only_a or s.only_b:
        s.text += f" {s.only_a + s.only_b} found in one determination only (artefacts)."
    if s.no_conc:
        s.text += " Some substances have no concentration yet: identify or bind the ISTDs."
    return s


def suggest_partner(ws, run_id: str) -> str | None:
    """The other determination of the same sample (``_A`` <-> ``_B``), if loaded."""
    from gcws.io.sequence import replicate_stem, sample_number
    st = ws.runs.get(run_id)
    if st is None:
        return None
    name = st.run.path.name
    key = (sample_number(name), replicate_stem(name))
    order = ws.ordered_ids_by_injection()
    cands = [s.id for s in ws.states() if s.id != run_id and s.role in ("sample", "standard")
             and (sample_number(s.run.path.name), replicate_stem(s.run.path.name)) == key]
    if not cands:
        return None
    return min(cands, key=lambda i: abs(order.index(i) - order.index(run_id)) if i in order and run_id in order else 0)


def member_problems(ws, members: list[str]) -> list[str]:
    """Why determinations cannot be compared, one line per run (empty list: fine)."""
    from gcws.core.model import FID
    from gcws.io.sequence import ROLE_LABELS
    out = []
    for m in members:
        st = ws.runs.get(m)
        if st is None:
            out.append("a member is no longer loaded")
            continue
        if st.role not in ("sample", "standard"):
            out.append(f"{st.name}: role is {ROLE_LABELS.get(st.role, st.role)} (needs Sample)")
        elif st.run.fid is None:
            out.append(f"{st.name}: no FID signal")
        elif FID not in st.results:
            out.append(f"{st.name}: FID not integrated")
        elif ws.quant_result is not None and ws.quant_result.errors.get(m):
            out.append(f"{st.name}: {ws.quant_result.errors[m]}")
    return out


def compute(ws, members: list[str], policy: str = "all"):
    """``(rows, problems)`` of the replicate merge for ``members`` in the current quantification mode."""
    from gcws.quant.nias_bridge import make_settings
    from gcws.quant.replicates import combine, engine_peaks
    members = [m for m in members if m in ws.runs]
    if ws.quant_result is None or any(m not in ws.quant_result.samples for m in members):
        ws.recompute_quant()                  # the scheduled recomputation may not have run yet
    problems = member_problems(ws, members)
    samples = [ws.nias_sample(m) for m in members]
    if problems or not samples or any(s is None for s in samples):
        return [], problems or ["every determination needs role Sample and an FID integration"]
    mode = ws.quant.get("mode", "nias_mgkg")
    lists = []
    for m, s in zip(members, samples):
        if mode == "nias_mgkg":
            lists.append(engine_peaks(s))
        else:
            qr = ws.quant_result.rows.get(m, {})
            lists.append(engine_peaks(s, value=lambda row, qr=qr: qr.get(row.derived.get("gcws_index"), {}).get("conc")))
    settings = make_settings(ws.quant.get("settings"))
    tol = float(getattr(settings, "rt_tolerance", 0.035) or 0.035)
    return combine(lists, tol, policy), []


def limits(ws) -> tuple[float, float]:
    """(difference limit %, reporting limit) from the quantification settings."""
    from gcws.quant.nias_bridge import make_settings
    s = make_settings(ws.quant.get("settings"))
    limit = float(getattr(s, "duplicate_max_reldiff", 30.0) or 30.0)
    rl = getattr(s, "reporting_limit", None)
    if rl is None:
        import gc_duplicate as GD
        rl = GD.DEFAULT_REPORTING_LIMIT
    rl = float(rl) if ws.quant.get("mode", "nias_mgkg") == "nias_mgkg" else 0.0
    return limit, rl

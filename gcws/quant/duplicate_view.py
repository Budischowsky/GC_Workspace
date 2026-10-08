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
    (r"Bestimmung (\d+): Peak durch Lückenfüllung nachintegriert", r"determination \1: peak integrated by gap filling"),
    (r"Bestimmung (\d+): nicht nachweisbar", r"determination \1: not detectable"),
    (r"Spektren der Bestimmungen unterschiedlich \(Ähnlichkeit ([\d.]+)\); Koelution prüfen",
     r"different spectra in the determinations (similarity \1); check for co-elution"),
    (r"in einer Bestimmung als ein Peak, in der anderen als zwei Peaks integriert",
     "integrated as one peak in one determination and as two in the other"),
    (r"Identifikation uneindeutig", "identification not unique"),
    (r"Identifikation aus beiden Bestimmungen", "identification from both determinations"),
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
    if row.get("light"):
        # the feature double determination has judged the row already (gcws.features.triage)
        from gcws.features.triage import LEVEL
        text = row.get("verdict") or "Confirmed"
        detail = "; ".join(row.get("reasons") or [])
        return Verdict(LEVEL.get(row["light"], "neutral"), text, detail[:1].upper() + detail[1:] if detail else "")
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


#: verdict level -> traffic light, so the classic pairing gets the colours of the feature pairing
LIGHT_OF_LEVEL = {"ok": "green", "warn": "yellow", "info": "yellow", "bad": "red", "neutral": "grey"}


def light_of(row: dict, verdict: Verdict) -> str:
    """The traffic light of a row: the feature pairing's own, else from the verdict's level."""
    return row.get("light") or LIGHT_OF_LEVEL.get(verdict.level, "grey")


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
    from gcws.io.sequence import ROLE_LABELS
    from gcws.quant.service import quant_detector
    key = quant_detector(ws.quant)
    out = []
    for m in members:
        st = ws.runs.get(m)
        if st is None:
            out.append("a member is no longer loaded")
            continue
        if st.role not in ("sample", "standard"):
            out.append(f"{st.name}: role is {ROLE_LABELS.get(st.role, st.role)} (needs Sample)")
        elif st.run.signal(key) is None:
            out.append(f"{st.name}: no {key} signal")
        elif key not in st.results:
            out.append(f"{st.name}: {key} not integrated")
        elif getattr(ws, "deconv_background", False) and ws.split_pending(st, key):
            out.append(f"{st.name}: deconvolution in progress")
        elif ws.quant_result is not None and ws.quant_result.errors.get(m):
            out.append(f"{st.name}: {ws.quant_result.errors[m]}")
    return out


def compute(ws, members: list[str], policy: str = "all"):
    """``(rows, problems)`` of the replicate merge for ``members`` in the current quantification mode."""
    from gcws.quant.nias_bridge import make_settings
    from gcws.quant.replicates import combine, engine_peaks
    members = [m for m in members if m in ws.runs]
    from gcws.quant.service import quant_detector
    key = quant_detector(ws.quant)
    if ws.quant_result is None or any(m not in ws.quant_result.samples
                                      and key in ws.runs[m].results
                                      and ws.runs[m].role in ("sample", "standard")
                                      and m not in ws.quant_result.errors for m in members):
        ws.recompute_quant()                  # the scheduled recomputation may not have run yet
    problems = member_problems(ws, members)
    samples = [ws.nias_sample(m) for m in members]
    if problems or not samples or any(s is None for s in samples):
        return [], problems or [f"every determination needs role Sample and an {key} integration"]
    mode = ws.quant.get("mode", "nias_mgkg")
    lists = []
    for m, s in zip(members, samples):
        if mode == "nias_mgkg":
            lists.append(engine_peaks(s))
        else:
            qr = ws.quant_result.rows.get(m, {})
            lists.append(engine_peaks(s, value=lambda row, qr=qr: qr.get(row.derived.get("gcws_index"), {}).get("conc")))
    settings = make_settings(ws.quant.get("hs" if mode == "hs_screening" else "settings"))
    tol = float(getattr(settings, "rt_tolerance", 0.035) or 0.035)
    table = features_table(ws, members)
    if table is not None:
        from gcws.features import combine as FC
        limit, rl = limits(ws)
        return FC.rows(table, lists, limit, rl, policy), []
    return combine(lists, tol, policy), []


def features_table(ws, members: list[str], table=None):
    """The feature table used for ``members`` (None: AutoLib's pairing -- HS screening, or the
    pairing set to classic). ``table``: one the caller already built (e.g. after applying)."""
    from gcws.features import service as SV
    from gcws.features.model import PAIRING_FEATURES
    if ws.quant.get("mode") == "hs_screening" or len(members) < 2 or SV.pairing(ws) != PAIRING_FEATURES:
        return None
    cache = getattr(ws, "_feature_table", None)
    if table is None:
        if cache is not None and cache.members == list(members) and _fresh(ws, cache):
            return cache
        table = SV.build(ws, members, search=False)
    table.stamp = stamp(ws, table.members, table.key)
    ws._feature_table = table
    return table


def _fresh(ws, table) -> bool:
    """The integrations and identifications the table was built from are still the current ones."""
    return getattr(table, "stamp", None) == stamp(ws, table.members, table.key)


def stamp(ws, members, key) -> tuple:
    out = []
    for m in members:
        st = ws.runs.get(m)
        res = st.results.get(key) if st is not None else None
        items = st.ident_set(key).items if st is not None else []
        out.append((m, res.digest if res is not None else None, id(res),
                    tuple((round(i.apex_rt, 4), i.name, i.cas) for i in items)))
    return tuple(out)


def limits(ws) -> tuple[float, float]:
    """(difference limit %, reporting limit) from the quantification settings."""
    from gcws.quant.nias_bridge import make_settings
    if ws.quant.get("mode") == "hs_screening":
        return float(ws.quant.get("hs", {}).get("duplicate_max_reldiff", 30)), 0.0
    s = make_settings(ws.quant.get("settings"))
    limit = float(getattr(s, "duplicate_max_reldiff", 30.0) or 30.0)
    rl = getattr(s, "reporting_limit", None)
    if rl is None:
        import gc_duplicate as GD
        rl = GD.DEFAULT_REPORTING_LIMIT
    rl = float(rl) if ws.quant.get("mode", "nias_mgkg") == "nias_mgkg" else 0.0
    return limit, rl


def default_limit(ws) -> float:
    """The difference limit (%) a new method starts with."""
    if ws.quant.get("mode") == "hs_screening":
        return 30.0
    import gc_duplicate as GD
    return float(GD.DEFAULT_MAX_RELDIFF)


# -- analyst edits (double determination -> report) ----------------------------------------------
#
# The analyst can change the areas and concentrations of a pair, its mean, add a comment, decide
# whether the substance goes into the report, dismiss one determination as an outlier (the result is
# then the other determination's concentration, no mean) and delete the row (it leaves the list, the
# counts and the report; it can be restored). The edits are kept in the replicate group (``group["edits"]``)
# under the pair's mean RT and found again by RT after a re-integration. Names and CAS are not kept
# here: they become the identification of both peaks.

NUMERIC_EDITS = ("a1", "a2", "c1", "c2", "mean")
#: the other fields of an edit; any of them (or a number) decides a red row
ROW_FLAGS = ("report", "comment", "deleted", "dismiss")


def default_report(row: dict, verdict: Verdict | None = None) -> bool:
    """AutoLib's rule: artefacts (found in one determination only) and substances below the
    reporting limit are not reported; everything else is. The feature pairing's grey rows at
    blank level are not reported either."""
    status = row.get("status") or ""
    if status.startswith("Artefact"):
        return False
    if verdict is not None and verdict.text.startswith(("Below reporting limit", "Blank level")):
        return False
    return True


def edit_key(rt: float) -> str:
    return f"{float(rt):.3f}"


def is_feature_key(key: str) -> bool:
    return str(key).startswith("F-")


def find_edit(edits: dict, rt: float, tol: float, used: set | None = None):
    """Key of the stored edit for the pair at ``rt`` (nearest within ``tol``), or ``None``."""
    best = None
    for k, e in (edits or {}).items():
        if used is not None and k in used:
            continue
        d = abs(float(e.get("rt", k)) - float(rt))
        if d <= tol and (best is None or d < best[0]):
            best = (d, k)
    return best[1] if best else None


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def apply_edits(rows: list[dict], verdicts: list, edits: dict, tol: float) -> list[dict]:
    """``rows`` with the analyst's changes applied. Every returned row carries ``a1``/``a2`` (areas),
    ``report`` (bool), ``comment``, ``deleted``, ``dismissed`` (0, or 1 / 2: the determination the
    analyst dismissed as an outlier), ``edited`` = {field: value before the change} and ``decided`` (a
    red row the analyst has answered). A deleted row is never reported.

    An edited area changes that determination's concentration in proportion (same factor and
    O/V); an edited concentration and the mean are taken as entered; the mean and the difference
    follow edited concentrations unless the mean itself was set. With one determination dismissed the
    result is the other one's concentration (no mean, no difference), unless the mean itself was set."""
    out, used = [], set()
    # a feature row finds its edit by the feature id; everything else (and edits of old projects) by RT
    by_id = {row.get("feature_id") for row in rows if row.get("feature_id") in (edits or {})}
    used |= by_id
    rt_edits = {k: e for k, e in (edits or {}).items() if not is_feature_key(k)}
    for row, v in zip(rows, verdicts):
        r = dict(row)
        s1, s2 = row.get("source1") or {}, row.get("source2") or {}
        r["a1"], r["a2"] = s1.get("area"), s2.get("area")
        r["report"] = default_report(row, v)
        r["comment"] = ""
        r["edited"] = {}
        r["decided"] = False
        r["deleted"] = False
        r["dismissed"] = 0
        if row.get("feature_id") in by_id:
            k = row["feature_id"]
        else:
            k = find_edit(rt_edits, row.get("rt") or 0.0, tol, used) if row.get("rt") is not None else None
        if k is not None:
            used.add(k)
            e = edits[k]
            r["edit_key"] = k
            changed_c = False
            for a, c in (("a1", "c1"), ("a2", "c2")):
                if e.get(a) is not None:
                    old_a, old_c = r[a], r.get(c)
                    r["edited"][a] = old_a
                    r[a] = float(e[a])
                    if c not in e and old_a and old_c is not None:
                        r["edited"].setdefault(c, old_c)
                        r[c] = old_c * r[a] / old_a
                        changed_c = True
            for c in ("c1", "c2"):
                if e.get(c) is not None:
                    r["edited"].setdefault(c, r.get(c))
                    r[c] = float(e[c])
                    changed_c = True
            if e.get("mean") is not None:
                r["edited"]["mean"] = r.get("mean")
                r["mean"] = float(e["mean"])
            elif changed_c:
                r["edited"]["mean"] = r.get("mean")
                r["mean"] = _mean([r.get("c1"), r.get("c2")])
            if changed_c or e.get("mean") is not None:
                c1, c2 = r.get("c1"), r.get("c2")
                m = _mean([c1, c2])
                r["reldiff"] = abs(c1 - c2) / m * 100.0 if (c1 is not None and c2 is not None and m) else None
            if e.get("dismiss") in (1, 2):
                r["dismissed"] = int(e["dismiss"])
                kept = r.get("c2" if r["dismissed"] == 1 else "c1")
                if e.get("mean") is None and kept is not None:
                    r["edited"].setdefault("mean", r.get("mean"))
                    r["mean"], r["reldiff"] = kept, None
            if e.get("report") is not None:
                r["edited"]["report"] = r["report"]
                r["report"] = bool(e["report"])
            if e.get("comment"):
                r["comment"] = str(e["comment"])
            if e.get("deleted"):
                r["deleted"], r["report"] = True, False
            # a red row the analyst has answered (report box, comment, a value or deleted) is decided
            r["decided"] = v.level == "bad" and any(e.get(f) is not None and e.get(f) != ""
                                                    for f in NUMERIC_EDITS + ROW_FLAGS)
        if r["report"] and r.get("mean") is None:
            r["mean"] = _mean([r.get("c1"), r.get("c2")])      # a single determination the analyst keeps
        out.append(r)
    return out


def is_open(row: dict, verdict: Verdict) -> bool:
    """A red row that still waits for the analyst's decision."""
    return verdict.level == "bad" and not row.get("decided")


def edits_key(quant: dict, unit: str) -> str:
    """Where a replicate group keeps the analyst's edits: per unit for HS and the extraction method (an
    edited value belongs to its unit), ``edits`` for the NIAS modes."""
    mode = (quant or {}).get("mode")
    if mode == "hs_screening":
        return "hs_edits:" + unit
    if mode == "extraction":
        return "extraction_edits:" + unit
    return "edits"


def rows_for_report(rows: list[dict], edits: dict, limit: float, reporting_limit: float, tol: float,
                    labels=("A", "B")):
    """``(combined rows to report, numeric overrides by row position)`` for a double determination."""
    verdicts = [plain_verdict(r, limit, reporting_limit, labels) for r in rows]
    edited = apply_edits(rows, verdicts, edits, tol)
    keep, overrides = [], {}
    for base, r in zip(rows, edited):
        if not r["report"]:
            continue
        if any(f in r["edited"] for f in NUMERIC_EDITS) or (r["report"] and not default_report(base)):
            overrides[len(keep)] = {f: r.get(f) for f in NUMERIC_EDITS + ("reldiff", "comment", "dismissed")}
        row = dict(base)
        notes = [r.get("comment") or ""]
        if r.get("dismissed"):
            out, kept = labels[r["dismissed"] - 1], labels[2 - r["dismissed"]]
            notes.insert(0, f"{out} dismissed as an outlier: result = {kept}")
        if any(notes):
            row["review"] = "; ".join(x for x in (base.get("review", ""), *notes) if x)
        keep.append(row)
    return keep, overrides

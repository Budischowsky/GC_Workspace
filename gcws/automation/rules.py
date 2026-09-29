"""Report² rules: when a processed report is accepted automatically and when the analyst must
check it ("Control needed").

A rule looks at the evidence of one processed sample (its determinations, ISTDs, blanks, the
merged substance rows of the report, the report's own summary and warnings) and returns
findings. A finding of level "control" sends the report to *Control needed*; level "info" is
only listed. Without such a finding the report is accepted automatically.
"""
from __future__ import annotations

import copy
import fnmatch
from dataclasses import asdict, dataclass, field
from typing import Optional

from gcws.automation import store

ACCEPTED_AUTO = "accepted_auto"
CONTROL = "control"

#: rule id -> (title, description, enabled by default, default parameters)
RULES = {
    "manual_check": ("Substances to check manually",
                     "Uncertain or unknown identification, identification conflict between the determinations, "
                     "deviating double determination (relative difference above the limit) or artefact (found in "
                     "one determination only) - the NIAS 'Manuell_pruefen' list.",
                     True, {"only_above_limit": True, "artefacts": True}),
    "sml_exceeded": ("SML exceeded", "A reported substance above its specific migration limit.", True, {}),
    "istd_qc": ("Internal standards (QC)",
                "An internal standard not found, its area differing between the determinations (or outside a "
                "fixed area window), no ISTD factor, or quantified with another peak than the automatic ISTD "
                "detection found.",
                True, {"area_diff": True, "max_area_diff_pct": 50.0, "area_window": False, "area_low": 8e6,
                       "area_high": 11e6, "detection": True}),
    "processing_warnings": ("Processing problems",
                            "The report could not be made completely (warnings of the report, quantification "
                            "errors, a missing file).", True, {}),
    "no_sml_above_limit": ("No SML above the reporting limit",
                           "A substance at or above the reporting limit without SML / reference: an assessment is "
                           "required.", False, {}),
    "substance_above": ("Substance above a concentration",
                        "Any substance (or the substances matching a name / CAS pattern) above the concentration.",
                        False, {"mgkg": 0.09, "pattern": ""}),
    "unidentified_over": ("Many unidentified substances",
                          "More unidentified substances (at or above the reporting limit) than allowed.",
                          False, {"max": 5}),
    "no_blank": ("Processed without a blank",
                 "The sample was processed without a blank from its batch (the analyst allowed it).", True, {}),
}


@dataclass
class Rule:
    id: str
    enabled: bool = True
    level: str = CONTROL                    # "control" | "info"
    params: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return RULES.get(self.id, (self.id,))[0]

    def p(self, key: str):
        return self.params.get(key, RULES.get(self.id, ("", "", True, {}))[3].get(key))


@dataclass
class Finding:
    rule: str
    level: str
    text: str
    member: str = ""
    substance: str = ""
    cas: str = ""
    rt: Optional[float] = None
    value: Optional[float] = None


@dataclass
class Evaluation:
    status: str
    findings: list[Finding]

    def to_list(self) -> list[dict]:
        return [asdict(f) for f in self.findings]


def default_rules() -> list[Rule]:
    return [Rule(rid, spec[2], CONTROL, copy.deepcopy(spec[3])) for rid, spec in RULES.items()]


def from_list(data) -> list[Rule]:
    """Rules from JSON; unknown ids are dropped, rules missing from ``data`` get their defaults."""
    if not isinstance(data, list):
        return default_rules()
    by_id = {}
    for d in data:
        if isinstance(d, dict) and d.get("id") in RULES:
            spec = RULES[d["id"]]
            params = dict(copy.deepcopy(spec[3]), **(d.get("params") or {}))
            by_id[d["id"]] = Rule(d["id"], bool(d.get("enabled", spec[2])), str(d.get("level") or CONTROL), params)
    return [by_id.get(r.id, r) for r in default_rules()]


def to_list(rules: list[Rule]) -> list[dict]:
    return [asdict(r) for r in rules]


def load_default_rules() -> list[Rule]:
    return from_list(store.read_json(store.default_rules_path()))


def save_default_rules(rules: list[Rule]) -> None:
    store.atomic_write_json(store.default_rules_path(), to_list(rules))


def rules_for(node_params: dict) -> list[Rule]:
    """The rules of a Report² step (its own, else the saved default rule set)."""
    own = (node_params or {}).get("rules")
    return from_list(own) if isinstance(own, list) else load_default_rules()


# -- evaluation -------------------------------------------------------------------------------

def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _label(row: dict) -> str:
    return str(row.get("name") or "unknown")


def _reported(row: dict, limit: float) -> bool:
    mean = _num(row.get("mean"))
    return mean is not None and mean >= limit


def _is_unidentified(row: dict) -> bool:
    name = str(row.get("name") or "").strip().casefold()
    return not name or name.startswith("unknown") or name.startswith("unbekannt") or \
        str(row.get("id_status") or "").casefold().startswith("unknown")


def _manual_check(rule: Rule, ev: dict) -> list[Finding]:
    out = []
    s = ev.get("settings") or {}
    limit = _num(s.get("reporting_limit")) or 0.01
    max_rd = _num(s.get("duplicate_max_reldiff")) or 30.0
    for row in ev.get("rows") or []:
        status = str(row.get("status") or "")
        artefact = status.casefold().startswith("artefact")
        if artefact and not rule.p("artefacts"):
            continue
        if rule.p("only_above_limit") and not artefact and not _reported(row, limit):
            continue
        reasons = []
        id_status = str(row.get("id_status") or "")
        if id_status and id_status != "Accepted":
            reasons.append(id_status)
        if "conflict" in status.casefold():
            reasons.append("identification conflict")
        if artefact:
            reasons.append(status.split(",")[0])
        rd = _num(row.get("reldiff"))
        if rd is not None and rd > max_rd and _reported(row, limit):
            reasons.append(f"relative difference {rd:.0f} % > {max_rd:.0f} %")
        if row.get("review"):
            reasons.append(str(row["review"]))
        if reasons:
            out.append(Finding(rule.id, rule.level, "; ".join(dict.fromkeys(reasons)), substance=_label(row),
                               cas=str(row.get("cas") or ""), rt=_num(row.get("rt")), value=_num(row.get("mean"))))
    return out


def _sml_exceeded(rule: Rule, ev: dict) -> list[Finding]:
    out, seen = [], set()
    for row in ev.get("rows") or []:
        sml, mean = _num(row.get("sml")), _num(row.get("mean"))
        if sml is not None and mean is not None and sml > 0 and mean > sml:
            seen.add(_label(row).casefold())
            out.append(Finding(rule.id, rule.level, f"{mean:.3g} mg/kg > SML {sml:.3g} mg/kg",
                               substance=_label(row), cas=str(row.get("cas") or ""), rt=_num(row.get("rt")),
                               value=mean))
    for name in (ev.get("summary") or {}).get("sml_exceedances") or []:
        if str(name).casefold() not in seen:
            out.append(Finding(rule.id, rule.level, "SML exceeded (report)", substance=str(name)))
    return out


def _istd_qc(rule: Rule, ev: dict) -> list[Finding]:
    out = []
    for m in ev.get("members") or []:
        name = m.get("name", "")
        stds = m.get("standards") or []
        for s in stds:
            status = str(s.get("status") or "")
            quant = str(s.get("role") or "Quantification") != "QC"
            if status == "Not found" or (quant and s.get("fid_area") in (None, 0)):
                out.append(Finding(rule.id, rule.level, f"{s.get('code', '')} {s.get('name', '')} not found".strip(),
                                   member=name, substance=str(s.get("name") or "")))
        if rule.p("area_window") and stds:
            try:
                import gc_qc
                checks = gc_qc.istd_window(stds, name, low=float(rule.p("area_low")), high=float(rule.p("area_high")))
            except Exception:  # noqa: BLE001 - the QC module is optional here
                checks = []
            for c in checks:
                if c.verdict in ("low", "high"):
                    out.append(Finding(rule.id, rule.level, f"ISTD area {c.verdict}: {c.area:,.0f} "
                                       f"(window {float(rule.p('area_low')):,.0f} - {float(rule.p('area_high')):,.0f})",
                                       member=name, substance=c.name, value=c.area))
        if ev.get("kind") in ("nias", None, "total_extraction") and m.get("mean_factor") in (None, 0)                 and m.get("quantified", True):
            out.append(Finding(rule.id, rule.level, "No ISTD factor", member=name))
        if rule.p("detection"):
            for code, d in (m.get("istd_detection") or {}).items():
                rt, used = d.get("rt"), d.get("used_rt")
                if d.get("applied") is False and rt is not None and used is not None and abs(rt - used) > 0.02:
                    out.append(Finding(rule.id, rule.level, f"{code}: the automatic detection found it at "
                                       f"{rt:.3f} ({d.get('confidence')} confidence), quantified with the peak "
                                       f"at {used:.3f}", member=name, substance=code, rt=used))
    return out


def _istd_spread(rule: Rule, ev: dict) -> list[Finding]:
    """The same standard with very different areas in the determinations of one sample."""
    if not rule.p("area_diff"):
        return []
    limit = float(rule.p("max_area_diff_pct") or 50.0)
    areas: dict = {}
    for m in ev.get("members") or []:
        for s in m.get("standards") or []:
            a = _num(s.get("fid_area"))
            if a and str(s.get("role") or "") != "QC":
                areas.setdefault(s.get("code"), []).append((a, s.get("name") or s.get("code")))
    out = []
    for code, vals in areas.items():
        if len(vals) < 2:
            continue
        lo, hi = min(v for v, _ in vals), max(v for v, _ in vals)
        diff = (hi / lo - 1.0) * 100.0
        if diff > limit:
            out.append(Finding(rule.id, rule.level, f"{code} area differs by {diff:.0f} % between the determinations "
                               f"({lo:,.0f} - {hi:,.0f}; limit {limit:.0f} %)", substance=vals[0][1], value=diff))
    return out


def _processing(rule: Rule, ev: dict) -> list[Finding]:
    out = [Finding(rule.id, rule.level, str(w)) for w in ev.get("warnings") or []]
    out += [Finding(rule.id, rule.level, str(e)) for e in ev.get("errors") or []]
    for m in ev.get("members") or []:
        if m.get("quant_error"):
            out.append(Finding(rule.id, rule.level, str(m["quant_error"]), member=m.get("name", "")))
    return out


def _no_sml(rule: Rule, ev: dict) -> list[Finding]:
    limit = _num((ev.get("settings") or {}).get("reporting_limit")) or 0.01
    out = []
    for row in ev.get("rows") or []:
        if _reported(row, limit) and not _is_unidentified(row) and _num(row.get("sml")) is None \
                and not str(row.get("status") or "").casefold().startswith("artefact"):
            out.append(Finding(rule.id, rule.level, "no SML: assessment required", substance=_label(row),
                               cas=str(row.get("cas") or ""), rt=_num(row.get("rt")), value=_num(row.get("mean"))))
    return out


def _substance_above(rule: Rule, ev: dict) -> list[Finding]:
    limit = _num(rule.p("mgkg"))
    pats = [p.strip().casefold() for p in str(rule.p("pattern") or "").split(";") if p.strip()]
    out = []
    for row in ev.get("rows") or []:
        mean = _num(row.get("mean"))
        if limit is None or mean is None or mean <= limit:
            continue
        if pats and not any(fnmatch.fnmatch(_label(row).casefold(), p) or
                            fnmatch.fnmatch(str(row.get("cas") or "").casefold(), p) for p in pats):
            continue
        out.append(Finding(rule.id, rule.level, f"{mean:.3g} mg/kg > {limit:.3g} mg/kg", substance=_label(row),
                           cas=str(row.get("cas") or ""), rt=_num(row.get("rt")), value=mean))
    return out


def _unidentified(rule: Rule, ev: dict) -> list[Finding]:
    limit = _num((ev.get("settings") or {}).get("reporting_limit")) or 0.01
    n = sum(1 for row in ev.get("rows") or [] if _reported(row, limit) and _is_unidentified(row))
    mx = int(_num(rule.p("max")) or 0)
    return [Finding(rule.id, rule.level, f"{n} unidentified substances (more than {mx})", value=n)] if n > mx else []


def _no_blank(rule: Rule, ev: dict) -> list[Finding]:
    out = []
    for m in ev.get("members") or []:
        if m.get("blank_ok") is False:
            out.append(Finding(rule.id, rule.level, m.get("blank_text") or "no blank from the same batch",
                               member=m.get("name", "")))
    return out


CHECKS = {"manual_check": _manual_check, "sml_exceeded": _sml_exceeded,
          "istd_qc": lambda r, ev: _istd_qc(r, ev) + _istd_spread(r, ev),
          "processing_warnings": _processing, "no_sml_above_limit": _no_sml, "substance_above": _substance_above,
          "unidentified_over": _unidentified, "no_blank": _no_blank}


def evaluate(rules: list[Rule], evidence: dict, auto_accept: bool = True) -> Evaluation:
    """Findings of all enabled rules and the resulting status."""
    findings: list[Finding] = []
    for r in rules:
        if r.enabled and r.id in CHECKS:
            findings += CHECKS[r.id](r, evidence or {})
    control = any(f.level == CONTROL for f in findings)
    if not control and not auto_accept:
        findings.append(Finding("auto_accept", CONTROL, "Automatic acceptance is switched off for this step"))
        control = True
    return Evaluation(CONTROL if control else ACCEPTED_AUTO, findings)

"""The data of a Template report, collected from the workspace (GUI thread / headless workspace).

:func:`collect` merges the determinations exactly as the double determination page does
(``duplicate_view.compute`` with the analyst's edits, the mode's concentration), maps every
determination's source back to its integrated peak and reads the values the template asks for. The
result, :class:`ReportData`, holds plain values only: the table is built and written from it without the
workspace (in a worker thread, in the automation, in the template window's preview)."""
from __future__ import annotations

import copy
import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional

from gcws.report import catalog as C

#: fields every report reads (filters, sorting, sums, footnotes)
BASE_FIELDS = {"rt", "name", "cas", "score", "conc", "istd", "sml", "ref", "footnote"}
#: peak fields read off the integrated peak (``peak_values``) rather than the merged row
_SOURCE_FIELDS = {"rt", "name", "cas", "score", "id_status", "ri", "corr_area", "raw_area"}
ISTD_CODES = {"IS1", "IS2", "IS3", "IS4"}
ISTD_NAMES = {"perdeutero-heptadecane", "benzyl-butyl-phthalate-d4", "di-n-nonyl-phthalate-d4",
              "dibutyl phthalate-3,4,5,6-d4"}


@dataclass
class ReportData:
    mode: str
    quant: dict                       # the parts of ``ws.quant`` units and headers need
    n: int
    labels: list
    names: list
    rows: list                        # merged rows (see collect)
    values: dict                      # header placeholders
    units: list = field(default_factory=list)          # units the mode gives
    report_units: list = field(default_factory=list)   # Conc. 1 / Conc. 2
    method_limit: float = 0.0         # the method's reporting limit (in the mode's Conc. 1 unit)
    features: bool = False
    has_ms: bool = True
    has_cas: bool = True
    determinations: list = field(default_factory=list)  # per determination: label, name, path, calculation
    warnings: list = field(default_factory=list)

    def info(self) -> C.Info:
        return C.Info(mode=self.mode, units=tuple(self.units), detector=self.quant.get("_detector", "FID"),
                      features=self.features, n=self.n, has_ms=self.has_ms, has_cas=self.has_cas)


def _quant_part(quant: dict) -> dict:
    from gcws.quant.service import quant_detector
    q = {k: copy.deepcopy(quant[k]) for k in ("mode", "unit", "hs", "method", "method_samples", "features")
         if k in quant}
    q["_detector"] = quant_detector(quant)
    return q


def _mean(values):
    vals = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(vals) / len(vals) if vals else None


def aggregate(each: list, agg: str, dismissed: int = 0):
    """One value of a determination's list: mean / min of the numbers, first / joined texts. The
    dismissed determination (1-based) does not count."""
    used = [v for k, v in enumerate(each) if dismissed != k + 1]
    if agg == "mean":
        return _mean(used)
    if agg == "min":
        nums = [v for v in used if isinstance(v, (int, float))]
        return min(nums) if nums else None
    texts = [str(v) for v in used if v not in (None, "")]
    if agg == "join":
        return " / ".join(dict.fromkeys(texts))
    return texts[0] if texts else ""


def _peak_index(st, key, src) -> Optional[int]:
    """The integrated peak of a determination's source: its ``gcws_index``, else the nearest apex (0.05 min)."""
    if not src:
        return None
    idx = src.get("gcws_index")
    res = st.results.get(key) if st is not None else None
    if res is None or not res.peaks:
        return None
    if isinstance(idx, int) and 0 <= idx < len(res.peaks):
        return idx
    rt = src.get("rt")
    if rt is None:
        return None
    i = min(range(len(res.peaks)), key=lambda j: abs(res.peaks[j].apex_rt - rt))
    return i if abs(res.peaks[i].apex_rt - rt) <= 0.05 else None


def _source_value(key, src, prow, ws, rid, skey):
    """A determination's value of ``key`` from its engine source (the numbers the merge used) or its peak."""
    from gcws.quant.peak_values import VALUES
    from gcws.quant.duplicate_view import english
    if src is None:
        return None
    if key == "rt":
        return src.get("rt")
    if key in ("name", "cas"):
        return src.get(key) or ""
    if key == "score":
        return src.get("score", src.get("quality"))
    if key == "id_status":
        return english(src.get("id_status") or "")
    if key == "corr_area":
        return src.get("area")
    if key == "raw_area":
        v = src.get("raw_area")
        return v if v is not None else (prow.quant.get("raw_area") if prow is not None else None)
    if key == "ri":
        v = src.get("ri")
        return v if v is not None else (prow.quant.get("ri") if prow is not None else None)
    if prow is None:
        return None
    getter = VALUES.get(key)
    return getter(prow, ws, rid, skey) if getter is not None else None


def header_values(ws, members, group, names, *, method_name="", template_name="") -> dict:
    """The header placeholders' values."""
    from gcws.quant import conversion as CV
    from gcws.quant import migration as MG
    from gcws.quant.service import MODES, mode_unit, quant_detector
    q = ws.quant
    n = len(members)
    meta = {}
    try:
        meta = MG.current(q) if q.get("migration") else {}
    except Exception:  # noqa: BLE001 - the header is still made
        meta = dict(q.get("migration") or {})
    analyst = meta.get("analyst") or ""
    if not analyst:
        from gcws.core.audit import current_user
        analyst = current_user()
    ov = meta.get("ov_ratio")
    try:
        sv = f"{float(ov):g}" if ov not in (None, "") else ""
    except (TypeError, ValueError):
        sv = str(ov)
    units = CV.report_units(q)
    first = ws.runs[members[0]] if members and members[0] in ws.runs else None
    sample = ""
    if first is not None:
        s = ws.nias_sample(members[0]) if hasattr(ws, "nias_sample") else None
        sample = str(((getattr(s, "meta", None) or {}).get("sample")) or first.run.path.stem)
    blanks = [ws.runs[b].name for m in members if m in ws.runs
              for b in ws.runs[m].blanks + ws.runs[m].blanks_istd if b in ws.runs]
    det = "single determination" if n == 1 else "double determination" if n == 2 else f"{n}-fold determination"
    return {
        "sample": sample, "samples": "; ".join(names), "group": (group or {}).get("name") or "; ".join(names),
        "determination": det, "n": str(n), "analyst": analyst, "operator": "",
        "migrate": meta.get("migrate_text") or "", "sv_ratio": sv,
        "date": _dt.date.today().isoformat(), "method": method_name, "quant_method": quant_text(ws, members),
        "mode": MODES.get(q.get("mode", "nias_mgkg"), ""), "unit": mode_unit(q),
        "unit1": units[0] if units else "", "unit2": units[1] if len(units) > 1 else "",
        "detector": quant_detector(q), "blanks": "; ".join(dict.fromkeys(blanks)), "template": template_name,
    }


def quant_text(ws, members) -> str:
    """One line on how the result was quantified."""
    from gcws.quant.service import MODES, quant_detector
    q = ws.quant
    mode = q.get("mode", "nias_mgkg")
    samples = [ws.nias_sample(m) for m in members if m in ws.runs] if hasattr(ws, "nias_sample") else []
    if mode == "extraction":
        try:
            from gcws.quant import units as U
            from gcws.report.quant import method_text
            infos = [(s.meta or {}).get("extraction") for s in samples if s is not None]
            if infos and all(infos):
                m = infos[0]["method"]
                labels = [chr(ord("A") + k) for k in range(len(infos))]
                amounts = [i["basis"].get(U.AMOUNT_BASIS[m["sample_type"]]) for i in infos]
                return method_text(m, samples[0].standards, quant_detector(q), amounts, labels)
        except Exception:  # noqa: BLE001 - fall back to the mode's name
            pass
    if mode == "hs_screening":
        s = samples[0] if samples else None
        if s is not None and (s.meta or {}).get("method"):
            return s.meta["method"]
    return f"{MODES.get(mode, mode)}; {quant_detector(q)}"


def determinations(ws, members, labels) -> list[dict]:
    """Per determination: label, name, source and the calculation of its factor (Calculation sheet)."""
    out = []
    for k, m in enumerate(members):
        st = ws.runs.get(m)
        s = ws.nias_sample(m) if hasattr(ws, "nias_sample") else None
        meta = (getattr(s, "meta", None) or {}) if s is not None else {}
        ext = meta.get("extraction") or {}
        factor = ext.get("factor") if ext else getattr(s, "mean_factor", None)
        text = ext.get("factor_text") if ext else ""
        if not text and meta.get("calculation"):
            text = str(meta["calculation"])
        out.append({"label": labels[k], "name": st.name if st is not None else m,
                    "source": str(st.run.path) if st is not None else "", "factor": factor,
                    "calculation": text or "",
                    "blanks": "; ".join(ws.runs[b].name for b in (st.blanks + st.blanks_istd if st else [])
                                        if b in ws.runs),
                    "standards": [dict(code=d.get("code"), name=d.get("name"), concentration=d.get("concentration"),
                                       role=d.get("role"), rt=d.get("fid_rt", d.get("rt")),
                                       area=d.get("fid_area", d.get("area")), status=d.get("status"))
                                  for d in (getattr(s, "standards", None) or [])]})
    return out


def _flags(name: str, cas: str, istd_codes, main=None) -> dict:
    """What the row rules ask of a merged row: internal standard, no name and no valid CAS, a library's
    "Sum of" row, unidentified. ``main``: the NIAS script (its CAS check)."""
    try:
        cas_ok = main.is_valid_cas_number(main.normalize_cas(cas)) if main is not None else bool(cas)
    except Exception:  # noqa: BLE001
        cas_ok = bool(cas)
    low = (name or "").strip().casefold()
    return {"istd": (name or "").strip().upper() in ISTD_CODES or low in ISTD_NAMES or any(istd_codes),
            "nameless": not low and not cas_ok,
            "library_sum": low.startswith("sum of "),
            "unidentified": not low or low.startswith("unknown")}


def collect(ws, members: list, group: Optional[dict], *, fields=(), method_name: str = "",
            template_name: str = "") -> ReportData:
    """The report data of ``members`` (one run: a single determination) with the values of ``fields``."""
    from gcws.quant import conversion as CV
    from gcws.quant import duplicate_view as DV
    from gcws.quant.nias_bridge import make_settings
    from gcws.quant.peak_values import rows_for
    from gcws.quant.service import quant_detector
    members = [m for m in members if m in ws.runs]
    group = group or {"members": members, "policy": "all"}
    q = ws.quant
    mode = q.get("mode", "nias_mgkg")
    n = len(members)
    labels = CV.labels(ws, members)
    names = [ws.runs[m].name for m in members]
    rows, problems = DV.compute(ws, members, group.get("policy", "all"))
    warnings = list(problems)
    limit, rl = DV.limits(ws)
    policy = group.get("policy", "all")
    if n == 2 and policy == "all":
        verdicts = [DV.plain_verdict(r, limit, rl, tuple(labels[:2]), ws.quant_unit()) for r in rows]
        edits = dict(group.get(DV.edits_key(q, ws.quant_unit())) or {})
        settings = make_settings(q.get("settings"))
        tol = float(getattr(settings, "rt_tolerance", 0.035) or 0.035)
        rows = DV.apply_edits(rows, verdicts, edits, tol)
    else:
        verdicts = [DV.plain_verdict(r, limit, rl, tuple((labels + ["B"])[:2]), ws.quant_unit())
                    if n == 2 else None for r in rows]
        rows = [dict(r, report=DV.default_report(r, v), deleted=False, dismissed=0, comment="", edited={})
                for r, v in zip(rows, verdicts)]
    fields = set(fields) | BASE_FIELDS
    wanted = [C.get(k) for k in fields if C.get(k) is not None]
    skey = quant_detector(q)
    settings = make_settings(q.get("hs" if mode == "hs_screening" else "settings"))
    ratio = CV.ratio_fn(q, settings, members)
    peak_fields = [f for f in wanted if f.per_det and f.group != "Concentration" and f.key not in _SOURCE_FIELDS]
    peak_rows = {m: {r.index: r for r in rows_for(ws, m, skey)} for m in members} if peak_fields else {}
    lookup, main = {}, None
    try:
        from gcws.quant.service import cas_lookup
        from gcws.report.legacy_api import main_script
        lookup = cas_lookup()
        main = main_script()
    except Exception as exc:  # noqa: BLE001 - no SML / Ref.
        warnings.append(f"CASINFO.xlsx not read: {exc}")
    out_rows = []
    for i, (r, v) in enumerate(zip(rows, verdicts)):
        sources = list(r.get("sources") or [r.get("source1"), r.get("source2")][:n])
        sources += [None] * (n - len(sources))
        cs, mean, dismissed = CV.det_values(r, n)
        vals: dict = {}
        prows = []
        for k, m in enumerate(members):
            st = ws.runs[m]
            idx = _peak_index(st, skey, sources[k]) if peak_fields else None
            prows.append(peak_rows.get(m, {}).get(idx) if idx is not None else None)
        for f in wanted:
            if f.key == "conc":
                vals["conc"] = {"each": list(cs) + [None] * (n - len(cs)), "agg": mean}
            elif f.group == "Concentration":
                unit = C.unit_of(f.key, q)
                each, agg = CV.in_unit(r, n, unit, ratio) if unit else ([None] * n, None)
                vals[f.key] = {"each": list(each) + [None] * (n - len(each)), "agg": agg}
            elif f.per_det:
                each = [_source_value(f.key, sources[k], prows[k] if prows else None, ws, members[k], skey)
                        if sources[k] is not None else None for k in range(n)]
                if f.key in ("rt", "name", "cas") and r.get(f.key) not in (None, ""):
                    agg = r.get(f.key)
                elif f.key == "id_status" and r.get("id_status"):
                    agg = DV.english(r["id_status"])
                else:
                    agg = aggregate(each, f.agg, dismissed)
                vals[f.key] = {"each": each, "agg": agg}
        name, cas = r.get("name") or "", r.get("cas") or ""
        match = {}
        if main is not None and lookup and cas:
            try:
                match = main.cas_lookup_match(lookup, cas) or {}
            except Exception:  # noqa: BLE001
                match = {}
        nd = r.get("n_detected")
        notes = v.detail if (v is not None and r.get("light")) else DV.english(r.get("review", ""))
        vals.update({
            "reldiff": r.get("reldiff"), "sd": r.get("sd"), "rsd": r.get("rsd"),
            "found_in": f"{nd}/{n}" if nd is not None else "",
            "verdict": v.text if v is not None else "", "notes": notes, "comment": r.get("comment") or "",
            "outlier": labels[dismissed - 1] if dismissed and dismissed <= n else "",
            "edited": ", ".join(sorted(r.get("edited") or {})), "feature": r.get("feature_id") or "",
            "similarity": r.get("sim"), "sml": match.get("sml"), "ref": match.get("reference") or "",
            "footnote": main.clean_footnote(match.get("footnote")) if main is not None and match else "",
        })
        istd_codes = vals.get("istd", {}).get("each", []) if isinstance(vals.get("istd"), dict) else []
        out_rows.append({
            "index": i, "rt": r.get("rt"), "name": name, "cas": cas, "mean": mean, "dismissed": dismissed,
            "report": bool(r.get("report")) and not r.get("deleted"), "deleted": bool(r.get("deleted")),
            "level": v.level if v is not None else "", "values": vals,
            "flags": _flags(name, cas, [c for c in istd_codes if c], main),
            "slim": {k: r.get(k) for k in ("rt", "name", "cas", "mean", "c1", "c2", "reldiff", "status",
                                           "id_status", "review")},
        })
    has_ms = any(ws.runs[m].run.ms is not None for m in members)
    rl_method = rl
    if mode == "extraction":
        from gcws.quant import extraction as EX
        rl_method = float(EX.of(q).get("reporting_limit") or 0.0)
    return ReportData(mode=mode, quant=_quant_part(q), n=n, labels=labels, names=names, rows=out_rows,
                      values=header_values(ws, members, group, names, method_name=method_name,
                                           template_name=template_name),
                      units=CV.available_units(q), report_units=CV.report_units(q), method_limit=rl_method,
                      features=bool(rows and rows[0].get("feature_id")), has_ms=has_ms,
                      has_cas=bool(lookup), determinations=determinations(ws, members, labels),
                      warnings=warnings)

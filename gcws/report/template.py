"""Report templates: what the Template report shows, chosen by the analyst.

A template names the report's columns (any value of the Peaks / substances panel or the double
determination, see :mod:`gcws.report.catalog`), its header (title, subtitle and label/value lines with
``{placeholders}``), which rows are reported and the extras (sums, footnotes, sheets, Word). Templates are
saved by name in ``<data>/report_templates/<name>.json``; the processing method carries a copy as its
"report_template" section, which lives in ``ws.quant["report_template"]``.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from gcws import paths

FORMAT = "gcws-report-template"
VERSION = 1
#: the workspace / processing-method key of the template the method reports with
QUANT_KEY = "report_template"

#: how a column shows a double (N-fold) determination
VIEWS = {"mean": "Mean", "merged": "Merged (A / B)", "each": "Each determination",
         "each_mean": "Each + Mean"}
SORTS = {"rt": "Retention time", "name": "Name", "conc": "Concentration (highest first)"}
ORIENTATIONS = ("landscape", "portrait")

#: header placeholders -> what they are replaced by
PLACEHOLDERS = {
    "sample": "Sample name(s) of the determinations",
    "group": "Replicate group name",
    "determination": "single / double / N-fold determination",
    "n": "Number of determinations",
    "analyst": "Analyst (migration conditions, else the Windows user)",
    "operator": "Operator (left empty)",
    "migrate": "Migration: simulant | temperature | duration",
    "sv_ratio": "Surface/volume ratio (dm²/kg)",
    "date": "Today's date",
    "method": "Processing method name",
    "quant_method": "Quantification: mode and method text",
    "mode": "Quantification mode",
    "unit": "Unit of the quantification mode",
    "unit1": "Conc. 1 unit of the mode",
    "unit2": "Conc. 2 unit of the mode",
    "detector": "Detector (FID or TIC)",
    "blanks": "Blank runs",
    "template": "Template name",
}


def _col(field, header, decimals=None, view="mean"):
    return {"field": field, "header": header, "decimals": decimals, "view": view}


def default_template() -> dict:
    """An empty template with every setting at its default."""
    return {
        "format": FORMAT, "version": VERSION, "name": "", "comment": "", "created": "", "by": "",
        "columns": [],
        "header": {"title": "GC – Report", "subtitle": "{determination}",
                   "lines": [[{"label": "Sample:", "value": "{sample}"}],
                             [{"label": "Method:", "value": "{quant_method}"}]]},
        "rows": {"only_reported": True, "skip_istd": True, "skip_nameless": False, "skip_library_sums": True,
                 "unidentified": "report", "min_score": None,
                 "limit": {"field": "", "value": None, "use_method": False},
                 "sort": "rt", "category_sums": False, "repeated_sums": False,
                 "empty_text": "No substance to report."},
        "extras": {"orientation": "portrait", "word": True, "sml_bold_field": "", "footnotes": True,
                   "notes": [], "sheets": {"determinations": True, "calculation": False, "audit": True},
                   "audit_page": False, "hide_empty_columns": False, "record_seen": True,
                   "on_method_run": True, "default_report": True, "file_suffix": ""},
    }


def _nias() -> dict:
    t = default_template()
    t.update(name="NIAS", comment="The NIAS-Screening layout (PA 26.007)")
    t["columns"] = [_col("rt", "RT (min)", 2), _col("name", "Name"), _col("cas", "CAS-No."),
                    _col("score", "% match", 0), _col("conc:mg_dm2", "Conc. mg/dm²", 4),
                    _col("conc:mg_kg", "Conc. mg/kg", 3), _col("sml", "SML (mg/kg)"), _col("ref", "Ref.")]
    t["header"] = {"title": "GC-MS/FID – NIAS-Screening –", "subtitle": "PA 26.007, {determination}",
                   "lines": [[{"label": "Sample:", "value": "{sample}"}, {"label": "Analyst:", "value": "{analyst}"}],
                             [{"label": "Migrate:", "value": "{migrate}"}, {"label": "S/V-ratio", "value": "{sv_ratio}"},
                              {"label": "Operator:", "value": "{operator}"}]]}
    t["rows"].update(skip_nameless=True, limit={"field": "conc:mg_kg", "value": 0.01, "use_method": False},
                     category_sums=True, repeated_sums=True, empty_text="No substance above 10 ppb detected.")
    t["extras"].update(orientation="landscape", sml_bold_field="conc:mg_kg", audit_page=True)
    return t


def _six_columns(name: str, title: str, subtitle: str) -> dict:
    t = default_template()
    t.update(name=name, comment=f"The {name} report layout: RT, Name, CAS, Qual, Conc. 1, Conc. 2")
    t["columns"] = [_col("rt", "RT (min)", 3), _col("name", "Name"), _col("cas", "CAS-No."),
                    _col("score", "Qual", 0), _col("report_unit_1", "Conc. 1 [{unit}]"),
                    _col("report_unit_2", "Conc. 2 [{unit}]")]
    t["header"] = {"title": title, "subtitle": subtitle,
                   "lines": [[{"label": "Sample:", "value": "{sample}"}],
                             [{"label": "Method:", "value": "{quant_method}"}]]}
    t["rows"]["limit"] = {"field": "report_unit_1", "value": None, "use_method": True}
    return t


def _hs() -> dict:
    return _six_columns("HS-Screening", "GC-MS – HS-Screening",
                        "HS-Screening; {determination}; Conc. 1 in {unit1}, Conc. 2 in {unit2}")


def _quant() -> dict:
    return _six_columns("Quantification", "GC – Quantification",
                        "{determination}; Conc. 1 in {unit1}, Conc. 2 in {unit2}")


def _empty() -> dict:
    t = default_template()
    t["name"] = "Empty"
    return t


#: built-in starting points: name -> factory
PRESETS = {"NIAS": _nias, "HS-Screening": _hs, "Quantification": _quant, "Empty": _empty}


def preset(name: str) -> dict:
    return normalise(PRESETS[name]())


def starter_for(quant: Optional[dict]) -> str:
    """The preset that fits the quantification mode."""
    mode = (quant or {}).get("mode", "nias_mgkg")
    return {"hs_screening": "HS-Screening", "extraction": "Quantification"}.get(mode, "NIAS")


# -- model -------------------------------------------------------------------------------------

def _merge(base: dict, data: dict) -> dict:
    """``base`` with ``data``'s values where the keys exist in ``base`` (nested dicts merged)."""
    out = copy.deepcopy(base)
    for k, v in (data or {}).items():
        if k not in out:
            continue
        if isinstance(out[k], dict) and isinstance(v, dict) and k not in ("header",):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _decimals(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return max(0, min(8, int(value)))
    except (TypeError, ValueError):
        return None


def _float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise(data: Optional[dict]) -> dict:
    """``data`` with every setting present and valid (unknown settings dropped, defaults filled)."""
    data = _upgrade(copy.deepcopy(data or {}))
    out = _merge(default_template(), data)
    out["format"], out["version"] = FORMAT, VERSION
    for k in ("name", "comment", "created", "by"):
        out[k] = str(out[k] or "")
    cols = []
    for c in out["columns"] if isinstance(out["columns"], list) else []:
        if not isinstance(c, dict) or not str(c.get("field") or "").strip():
            continue
        view = c.get("view") if c.get("view") in VIEWS else "mean"
        cols.append({"field": str(c["field"]).strip(), "header": str(c.get("header") or ""),
                     "decimals": _decimals(c.get("decimals")), "view": view})
    out["columns"] = cols
    h = out["header"] if isinstance(out["header"], dict) else {}
    lines = []
    for line in h.get("lines") or []:
        pairs = [{"label": str(p.get("label") or ""), "value": str(p.get("value") or "")}
                 for p in (line if isinstance(line, list) else []) if isinstance(p, dict)]
        if pairs:
            lines.append(pairs)
    out["header"] = {"title": str(h.get("title") or ""), "subtitle": str(h.get("subtitle") or ""), "lines": lines}
    r = out["rows"]
    for k in ("only_reported", "skip_istd", "skip_nameless", "skip_library_sums", "category_sums", "repeated_sums"):
        r[k] = bool(r[k])
    r["unidentified"] = r["unidentified"] if r["unidentified"] in ("report", "hide") else "report"
    r["min_score"] = _float(r["min_score"])
    lim = r["limit"] if isinstance(r["limit"], dict) else {}
    r["limit"] = {"field": str(lim.get("field") or ""), "value": _float(lim.get("value")),
                  "use_method": bool(lim.get("use_method"))}
    r["sort"] = r["sort"] if r["sort"] in SORTS else "rt"
    r["empty_text"] = str(r["empty_text"] or "")
    e = out["extras"]
    e["orientation"] = e["orientation"] if e["orientation"] in ORIENTATIONS else "portrait"
    for k in ("word", "footnotes", "audit_page", "hide_empty_columns", "record_seen", "on_method_run",
              "default_report"):
        e[k] = bool(e[k])
    e["sml_bold_field"] = str(e["sml_bold_field"] or "")
    e["notes"] = [str(n) for n in (e["notes"] if isinstance(e["notes"], list) else [e["notes"]]) if str(n).strip()]
    e["sheets"] = {k: bool((e["sheets"] or {}).get(k, v)) for k, v in default_template()["extras"]["sheets"].items()}
    e["file_suffix"] = re.sub(r'[<>:"/\\|?*]+', "_", str(e["file_suffix"] or "")).strip(" .")
    return out


def _upgrade(data: dict) -> dict:
    """Older template versions brought to ``VERSION`` (none yet)."""
    return data


def validate(tpl: dict, quant: Optional[dict] = None) -> list[str]:
    """Problems of ``tpl`` (unknown columns, placeholders, columns the mode of ``quant`` cannot fill)."""
    from gcws.report import catalog as C
    out = []
    t = normalise(tpl)
    info = C.Info.of_quant(quant) if quant is not None else None
    for c in t["columns"]:
        f = C.get(c["field"])
        if f is None:
            out.append(f"Unknown column '{c['field']}'")
        elif info is not None:
            ok, why = C.availability(f, info)
            if not ok:
                out.append(f"{f.label}: {why}")
    texts = [t["header"]["title"], t["header"]["subtitle"]] + \
        [p["label"] + " " + p["value"] for line in t["header"]["lines"] for p in line]
    for text in texts:
        for name in re.findall(r"\{(\w+)\}", text):
            if name not in PLACEHOLDERS:
                out.append(f"Unknown placeholder {{{name}}}")
    return list(dict.fromkeys(out))


def differs(a: Optional[dict], b: Optional[dict]) -> bool:
    """Whether two templates differ in what the report shows (not in name, date or author)."""
    def core(t):
        t = normalise(t)
        for k in ("name", "created", "by"):
            t.pop(k, None)
        return t
    return core(a) != core(b)


def fill(text: str, values: dict) -> str:
    """``text`` with its ``{placeholders}`` replaced (one without a value stays as it is)."""
    def one(m):
        v = values.get(m.group(1))
        return m.group(0) if v is None else str(v)
    return re.sub(r"\{(\w+)\}", one, text or "")


# -- the method's copy -------------------------------------------------------------------------

def of(quant: Optional[dict]) -> Optional[dict]:
    """The template the method reports with (``quant["report_template"]``), or None."""
    data = (quant or {}).get(QUANT_KEY)
    return normalise(data) if isinstance(data, dict) else None


def applied(quant: Optional[dict], tpl: Optional[dict]) -> dict:
    """``quant`` with ``tpl`` as the method's template (None removes it)."""
    q = copy.deepcopy(quant or {})
    if tpl is None:
        q.pop(QUANT_KEY, None)
    else:
        q[QUANT_KEY] = normalise(tpl)
    return q


# -- named store -------------------------------------------------------------------------------

def folder() -> Path:
    return paths.DATA / "report_templates"


def _file(name: str) -> Path:
    return folder() / (re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .") + ".json")


def stamped(tpl: dict, name: str) -> dict:
    """``tpl`` under ``name``, dated and signed now."""
    from gcws.core.audit import current_user
    out = normalise(tpl)
    out.update(name=name, created=datetime.now().isoformat(timespec="seconds"), by=current_user())
    return out


def save(tpl: dict) -> Path:
    data = normalise(tpl)
    if not data["name"].strip():
        raise ValueError("A report template needs a name")
    path = _file(data["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{Path(path).name} is not a GC Workspace report template.")
    data.setdefault("name", Path(path).stem)
    return normalise(data)


def names() -> list[str]:
    out = []
    for f in sorted(folder().glob("*.json")) if folder().is_dir() else []:
        try:
            out.append(read(f)["name"])
        except (OSError, ValueError):
            continue
    return sorted(out, key=str.casefold)


def load(name: str) -> dict:
    for f in folder().glob("*.json") if folder().is_dir() else []:
        try:
            data = read(f)
        except (OSError, ValueError):
            continue
        if data["name"] == name:
            return data
    raise KeyError(name)


def delete(name: str) -> bool:
    for f in folder().glob("*.json") if folder().is_dir() else []:
        try:
            if read(f)["name"] == name:
                f.unlink()
                return True
        except (OSError, ValueError):
            continue
    return False

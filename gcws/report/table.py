"""The Template report's table, built from :class:`~gcws.report.template_data.ReportData` and a template
(pure: no workspace, no Qt, no files).

:func:`build` turns each template column into report columns (one, or one per determination, see the
column's view), keeps the rows the template reports, adds the NIAS sum rows, marks values above the SML
in bold and numbers the footnotes. :class:`ReportTable` is plain data: the Excel and Word writers, the
template window's preview and the batch Word document all read the same table."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from gcws.report import catalog as C
from gcws.report import template as TP

PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/compound/"


@dataclass
class Cell:
    value: Any = None
    bold: bool = False
    marker: str = ""          # footnote label ("a") shown as a superscript "(a)"
    link: str = ""            # hyperlink (CAS -> PubChem)


@dataclass
class Column:
    key: str
    header: str
    kind: str = "number"
    decimals: Optional[int] = None
    det: Optional[int] = None  # determination index; None: the result (mean / merged)
    view: str = "mean"
    width: float = 9.0

    @property
    def numeric(self) -> bool:
        return self.kind == "number" and self.view != "merged"


@dataclass
class Row:
    cells: list
    kind: str = "substance"   # substance | sum


@dataclass
class ReportTable:
    title: str = ""
    subtitle: str = ""
    header_lines: list = field(default_factory=list)    # [[(label, value), ...], ...]
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    footnotes: list = field(default_factory=list)       # texts of the markers (a), (b), ...
    notes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    orientation: str = "portrait"
    empty_text: str = ""
    reported: list = field(default_factory=list)        # {"name", "cas", "rt"} for the register
    summary: dict = field(default_factory=dict)
    combined: list = field(default_factory=list)        # slim merged rows (Report²)
    determinations: list = field(default_factory=list)  # Determinations sheet: header row + rows
    calculation: list = field(default_factory=list)     # Calculation sheet: header row + rows
    standards: list = field(default_factory=list)       # Standards rows of the Calculation sheet
    sheets: dict = field(default_factory=dict)
    word: bool = True
    audit_page: bool = False
    template: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReportTable":
        d = dict(d)
        d["columns"] = [Column(**c) for c in d.get("columns") or []]
        d["rows"] = [Row([Cell(**c) for c in r["cells"]], r.get("kind", "substance")) for r in d.get("rows") or []]
        d["header_lines"] = [[tuple(p) for p in line] for line in d.get("header_lines") or []]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def fmt(value, decimals: Optional[int]) -> str:
    """A value as the report prints it."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if decimals is None:
            return f"{value:g}"
        return f"{value:.{decimals}f}"
    return str(value)


def _decimals(f: C.Field, col: dict, data) -> Optional[int]:
    if col.get("decimals") is not None:
        return col["decimals"]
    if f.group == "Concentration" and f.decimals is None:
        from gcws.quant import units as U
        return U.DECIMALS.get(C.unit_of(f.key, data.quant), 4)
    return f.decimals


def expand(tpl: dict, data) -> tuple[list, list]:
    """``(columns, warnings)``: the template's columns for ``data``'s determinations. A column the
    quantification cannot fill is left out with a warning; a double-determination column is left out
    silently in a single determination."""
    cols, warnings = [], []
    info = data.info()
    for c in tpl["columns"]:
        f = C.get(c["field"])
        if f is None:
            warnings.append(f"Unknown column '{c['field']}' left out")
            continue
        ok, why = C.availability(f, info)
        header = C.header_of(f, c.get("header") or "", data.quant)
        if not ok:
            if not (f.needs == "dd" and data.n < 2):
                warnings.append(f"Column '{header}' left out: {why}")
            continue
        dec = _decimals(f, c, data)
        view = c.get("view", "mean") if f.per_det and data.n > 1 else "mean"
        base = dict(key=f.key, kind=f.kind, decimals=dec, width=f.width)
        if view in ("each", "each_mean"):
            for k, lab in enumerate(data.labels):
                cols.append(Column(header=f"{header} {lab}", det=k, view="each", **base))
            if view == "each_mean":
                cols.append(Column(header=f"{header} mean", view="mean", **base))
        else:
            cols.append(Column(header=header, view=view, **base))
    return cols, warnings


def value(row: dict, col: Column, dismissed: int = 0):
    v = row["values"].get(col.key)
    if not isinstance(v, dict):
        return v
    if col.det is not None:
        each = v.get("each") or []
        return each[col.det] if col.det < len(each) else None
    if col.view == "merged":
        parts = []
        for k, x in enumerate(v.get("each") or []):
            text = fmt(x, col.decimals) if col.kind == "number" else ("" if x is None else str(x))
            text = text or "–"
            parts.append(f"({text})" if dismissed == k + 1 else text)
        return " / ".join(parts)
    return v.get("agg")


def agg_of(row: dict, key: str):
    v = row["values"].get(key)
    return v.get("agg") if isinstance(v, dict) else v


def _number(v) -> Optional[float]:
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def limit_of(tpl: dict, data, cols: list) -> tuple[str, Optional[float]]:
    """``(field, value)`` of the template's reporting limit (value None: no limit)."""
    lim = tpl["rows"]["limit"]
    value = data.method_limit if lim.get("use_method") else lim.get("value")
    if not value:
        return "", None
    key = lim.get("field") or next((c.key for c in cols if C.get(c.key).group == "Concentration"), "conc")
    return key, float(value)


def keep(row: dict, tpl: dict, limit: tuple) -> bool:
    r = tpl["rows"]
    flags = row["flags"]
    if row.get("deleted") or (r["only_reported"] and not row.get("report")):
        return False
    if (r["skip_istd"] and flags["istd"]) or (r["skip_nameless"] and flags["nameless"]) \
            or (r["skip_library_sums"] and flags["library_sum"]) \
            or (r["unidentified"] == "hide" and flags["unidentified"]):
        return False
    if r["min_score"] is not None:
        s = _number(agg_of(row, "score"))
        if s is not None and s < r["min_score"]:
            return False
    key, lim = limit
    if lim is not None:
        v = _number(agg_of(row, key))
        if v is None or v < lim:
            return False
    return True


def _sort(rows: list, tpl: dict, cols: list) -> list:
    how = tpl["rows"]["sort"]
    if how == "name":
        return sorted(rows, key=lambda r: (str(r.get("name") or "").casefold(), r.get("rt") or 0.0))
    if how == "conc":
        key = next((c.key for c in cols if C.get(c.key).group == "Concentration"), "conc")
        return sorted(rows, key=lambda r: -(_number(agg_of(r, key)) or 0.0))
    return sorted(rows, key=lambda r: (r.get("rt") is None, r.get("rt") or 0.0))


def _sum_values(items: list, cols: list, printed: bool = False) -> dict:
    """Column index -> sum of the summable columns over ``items``; ``printed``: of the values as printed
    (rounded to the column's decimals), as the NIAS Report adds up a substance found more than once."""
    out = {}
    for i, c in enumerate(cols):
        f = C.get(c.key)
        if not f.summable or c.view == "merged":
            continue
        vals = [_number(value(r, c, r.get("dismissed", 0))) for r in items]
        vals = [round(v, c.decimals) if printed and c.decimals is not None else v for v in vals if v is not None]
        out[i] = sum(vals) if vals else None
    return out


def _main():
    try:
        from gcws.report.legacy_api import main_script
        return main_script()
    except Exception:  # noqa: BLE001 - no sums without the NIAS script
        return None


def nias_sums(rows: list, tpl: dict, cols: list):
    """``(substance rows, sum rows, notes)``: the NIAS category sums (styrene oligomers,
    hydrocarbons, siloxanes, cyclic polyester oligomers) and the sums of substances found more than once."""
    r = tpl["rows"]
    main = _main()
    if main is None or not (r["category_sums"] or r["repeated_sums"]):
        return rows, [], []
    sums, notes = [], []
    keep_rows = rows
    abbreviations: list = []
    counted = False
    if r["category_sums"]:
        groups: dict = {}
        keep_rows = []
        for row in rows:
            cat = main.classify_name(row.get("name") or "")
            if cat:
                groups.setdefault(cat, []).append(row)
                if cat == "cyclic_polyester":
                    for src in (row.get("name"), row.get("cas")):
                        for a in main.extract_monomer_abbreviations(src):
                            if a not in abbreviations:
                                abbreviations.append(a)
            else:
                keep_rows.append(row)
        for cat in ("styrene", "hydrocarbon", "siloxane", "cyclic_polyester"):
            if groups.get(cat):
                label = (main.cyclic_polyester_summary_label(abbreviations) if cat == "cyclic_polyester"
                         else main.SUMMARY_LABELS[cat])
                sums.append({"label": label, "items": groups[cat], "rep": None, "printed": False,
                             "footnote": main.CYCLIC_POLYESTER_FOOTNOTE if cat == "cyclic_polyester" else ""})
                counted = True
    if r["repeated_sums"]:
        items = [dict(name=row.get("name"), cas=row.get("cas"), _row=row) for row in keep_rows]
        remaining, repeated = main.split_repeated_substance_groups(items)
        keep_rows = [it["_row"] for it in remaining]
        for key, group in repeated.items():
            unknown = key[0] == "unknown"
            sums.append({"label": main.repeated_substance_summary_label(key, group),
                         "items": [g["_row"] for g in group], "rep": None if unknown else group[0]["_row"],
                         "printed": True, "footnote": ""})
            counted = True
    if counted:
        notes.append(main.CALCULATION_NOTE)
    if abbreviations and any(s["label"].startswith("Sum of cyclic") for s in sums):
        notes.append(main.cyclic_polyester_abbreviation_note(abbreviations))
    return keep_rows, sums, notes


def _cas_link(cas: str) -> str:
    main = _main()
    if main is None or not cas:
        return ""
    norm = main.normalize_cas(cas)
    if not main.is_valid_cas_number(norm):
        return ""
    from urllib.parse import quote
    return PUBCHEM + quote(norm, safe="")


def build(data, tpl: dict) -> ReportTable:
    """The report table of ``data`` with the template ``tpl``."""
    tpl = TP.normalise(tpl)
    cols, warnings = expand(tpl, data)
    warnings = list(data.warnings) + warnings
    ex = tpl["extras"]
    limit = limit_of(tpl, data, cols)
    if limit[1] is not None and C.get(limit[0]) is not None:
        ok, why = C.availability(C.get(limit[0]), data.info())
        if not ok:
            warnings.append(f"Reporting limit not applied: {why}")
            limit = ("", None)
    rows = [r for r in data.rows if keep(r, tpl, limit)]
    rows = _sort(rows, tpl, cols)
    rows, sums, notes = nias_sums(rows, tpl, cols)
    bold_key = ex["sml_bold_field"]
    if bold_key:
        f = C.get(bold_key)
        if f is None or not C.availability(f, data.info())[0]:
            warnings.append(f"Bold above the SML not applied: {bold_key} is not available")
            bold_key = ""
    footnotes: dict[str, int] = {}

    def marker(text: str) -> str:
        if not ex["footnotes"] or not text:
            return ""
        if text not in footnotes:
            footnotes[text] = len(footnotes)
        return chr(ord("a") + footnotes[text])

    keys = [c.key for c in cols]
    mark_col = keys.index("ref") if "ref" in keys else keys.index("name") if "name" in keys else None
    out_rows = []
    for row in rows:
        cells = []
        sml = _number(row["values"].get("sml"))
        exceeds = False
        if bold_key:
            v = _number(agg_of(row, bold_key))
            exceeds = v is not None and (sml is None or v > sml)
        for c in cols:
            v = value(row, c, row.get("dismissed", 0))
            if c.key == "sml_check":
                v = _sml_check(row, sml)
            cell = Cell(v, bold=exceeds and c.key == bold_key and c.det is None)
            if c.key == "cas":
                cell.link = _cas_link(str(v or ""))
            cells.append(cell)
        m = marker(row["values"].get("footnote") or "")
        if m and mark_col is not None:
            cells[mark_col].marker = m
        out_rows.append(Row(cells))
    for s in sums:
        cells = [Cell(None, bold=True) for _ in cols]
        if "name" in keys:
            cells[keys.index("name")].value = s["label"]
        elif cols:
            cells[0].value = s["label"]
        for i, total in _sum_values(s["items"], cols, s["printed"]).items():
            cells[i].value = total
        rep = s["rep"]
        if rep is not None:
            for i, c in enumerate(cols):
                if c.key in ("sml", "ref"):
                    cells[i].value = rep["values"].get(c.key)
        m = marker(s["footnote"] or (rep["values"].get("footnote") if rep is not None else ""))
        if m and mark_col is not None:
            cells[mark_col].marker = m
        out_rows.append(Row(cells, "sum"))
    if ex["hide_empty_columns"] and out_rows:
        used = [i for i in range(len(cols)) if any(r.cells[i].value not in (None, "") for r in out_rows)]
        cols = [cols[i] for i in used]
        for r in out_rows:
            r.cells = [r.cells[i] for i in used]
    v = data.values
    header = tpl["header"]
    lines = [[(TP.fill(p["label"], v), TP.fill(p["value"], v)) for p in line] for line in header["lines"]]
    exceed = [{"name": r.get("name"), "cas": r.get("cas"), "sml": _number(r["values"].get("sml")),
               "value": _number(agg_of(r, bold_key))} for r in rows
              if bold_key and _number(r["values"].get("sml")) is not None
              and (_number(agg_of(r, bold_key)) or 0.0) > _number(r["values"].get("sml"))]
    return ReportTable(
        title=TP.fill(header["title"], v), subtitle=TP.fill(header["subtitle"], v), header_lines=lines,
        columns=cols, rows=out_rows, footnotes=list(footnotes), notes=notes + list(ex["notes"]),
        warnings=list(dict.fromkeys(warnings)), orientation=ex["orientation"], empty_text=tpl["rows"]["empty_text"],
        reported=[{"name": r.get("name"), "cas": r.get("cas"), "rt": r.get("rt")} for r in rows],
        summary={"sml_exceedances": exceed, "rows": len(rows), "sums": len(sums)},
        combined=[r["slim"] for r in rows], determinations=determination_sheet(data, rows),
        calculation=calculation_sheet(data), standards=standards_sheet(data), sheets=dict(ex["sheets"]),
        word=ex["word"], audit_page=ex["audit_page"], template=tpl["name"])


def _sml_check(row: dict, sml: Optional[float]) -> str:
    v = _number(agg_of(row, "conc:mg_kg"))
    if v is None:
        return ""
    if sml is None:
        return "no SML"
    return "> SML" if v > sml else "≤ SML"


def determination_sheet(data, rows: list) -> list:
    """Header and rows of the Determinations sheet: each determination's result in the report units."""
    units = [u for u in data.report_units if u]
    keys = [k for k, u in (("report_unit_1", units[0] if units else None),
                           ("report_unit_2", units[1] if len(units) > 1 else None)) if u]
    head = ["RT (min)", "Name", "CAS-No."]
    for lab in data.labels:
        head += [f"{lab} [{u}]" for u in units[:len(keys)]] + [f"{lab} outlier"]
    head += ["Result [" + units[0] + "]" if units else "Result", "Relative difference [%]", "Verdict", "Notes"]
    out = [head]
    for r in rows:
        line = [r.get("rt"), r.get("name"), r.get("cas")]
        for k in range(data.n):
            for key in keys:
                v = r["values"].get(key) or {}
                each = v.get("each") or []
                line.append(each[k] if k < len(each) else None)
            line.append("dismissed" if r.get("dismissed") == k + 1 else "")
        first = r["values"].get(keys[0]) if keys else None
        line += [first.get("agg") if isinstance(first, dict) else r.get("mean"), r["values"].get("reldiff"),
                 r["values"].get("verdict"), r["values"].get("notes")]
        out.append(line)
    return out


def calculation_sheet(data) -> list:
    out = [["Determination", "Sample", "Source", "Factor", "Calculation", "Blanks"]]
    for d in data.determinations:
        out.append([d["label"], d["name"], d["source"], d["factor"], d["calculation"], d["blanks"]])
    return out


def standards_sheet(data) -> list:
    out = [["Determination", "Sample", "Code", "Name", "Concentration", "Role", "RT min", "Area", "Status"]]
    for d in data.determinations:
        for s in d["standards"]:
            out.append([d["label"], d["name"], s.get("code"), s.get("name"), s.get("concentration"), s.get("role"),
                        s.get("rt"), s.get("area"), s.get("status")])
    return out

"""Fingerprint-shaped HS report: quantities replace Area %, without renormalization."""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


def literal(cell, value):
    from gcws.core.text import excel_safe
    value = excel_safe(value)
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def generate(job, progress=lambda _: None):
    from gcws.report import service as RS
    from gcws.report.legacy_api import main_script
    from gcws.quant.hs import UNITS
    if not job.samples or any(getattr(s, "mode", None) != "hs_screening" for s in job.samples):
        raise ValueError("Select HS-Screening quantification before creating an HS report")
    units = {s.meta.get("unit") for s in job.samples}
    if len(units) != 1 or not units.issubset(UNITS):
        raise ValueError("HS determinations must have the same result unit")
    if any(not s.mean_factor or any(r.derived.get("status") for r in s.rows) for s in job.samples):
        raise ValueError("Resolve HS standard and sample-normalization errors before reporting")
    unit = next(iter(units))
    combined = RS.combined_rows(job)
    rows = []
    for i, row in enumerate(combined):
        value = job.overrides.get(i, {}).get("mean", row.get("mean"))
        if value is None:
            continue
        sources = row.get("sources", [])
        scores = [s["score"] for s in sources if s and s.get("score") is not None]
        rows.append([row.get("rt"), row.get("name", "Unknown"), row.get("cas", ""),
                     min(scores) if scores else None, value])
    progress("HS-Screening: Fingerprint layout")
    api = main_script()
    layout = api.fingerprint_report_layout({"rtmin": 1}, unit)
    wb = Workbook()
    sh = wb.active
    sh.title = "HS Result"
    sh.sheet_view.showGridLines = False
    title = "GC-MS – HS-Screening"
    subtitle = f"HS-Screening; {unit}"
    for r, text in ((1, title), (2, subtitle)):
        sh.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
        literal(sh.cell(r, 1), text)
    sh["A1"].fill = PatternFill("solid", fgColor="BA0C2F")
    sh["A1"].font = Font(name="Arial", size=10, color="FFFFFF", bold=True)
    sh["A2"].font = Font(name="Arial", size=8, bold=True, italic=True)
    for r, label, text in ((3, "Sample:", "; ".join(job.names)),
                           (4, "Method:", "Headspace; TIC; internal standard response")):
        sh.cell(r, 1, label)
        sh.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        literal(sh.cell(r, 2), text)
    for c, header in enumerate(layout["titles"], 1):
        sh.cell(5, c, header)
    for r, values in enumerate(rows, 6):
        for c, value in enumerate(values, 1):
            literal(sh.cell(r, c), value)
    for cells in sh.iter_rows(min_row=3):
        for cell in cells:
            cell.font = Font(name="Arial", size=8, bold=cell.row == 5)
            cell.alignment = Alignment(vertical="center", horizontal="right" if cell.column >= 4 else "left",
                                       wrap_text=cell.column == 2)
            if cell.row == 5:
                cell.border = Border(bottom=Side(style="thin", color="000000"))
            if cell.row >= 6 and cell.column in (1, 5):
                cell.number_format = "0.0000"
    for c, width in layout["widths"].items():
        sh.column_dimensions[get_column_letter(c)].width = width
    sh.freeze_panes = "A6"
    sh.print_title_rows = "1:5"
    sh.print_area = f"A1:E{max(6, sh.max_row)}"
    sh.page_setup.orientation = "landscape"
    sh.page_setup.fitToWidth = 1
    sh.page_setup.fitToHeight = 0
    sh.sheet_properties.pageSetUpPr.fitToPage = True
    detail = wb.create_sheet("HS calculation")
    detail.append(["Sample", "Source", "Unit", "Sample area dm²", "Sample mass g", "µg per area count",
                   "Calculation", "Blank correction", "Blanks"])
    standards = wb.create_sheet("HS standards")
    standards.append(["Sample", "Code", "Name", "µg/HS", "RT min", "TIC area", "Active", "Status"])
    peaks = wb.create_sheet("HS peak calculation")
    peaks.append(["Sample", "RT min", "Name", "CAS", "Raw TIC area", "Blank area", "Corrected area",
                  "µg/HS", unit, "ISTD"])
    for s in job.samples:
        meta = s.meta
        detail.append([s.name, meta["source"], unit, meta["inputs"].get("area_dm2"), meta["inputs"].get("mass_g"),
                       s.mean_factor, meta["calculation"], meta["blank_correction"], "; ".join(meta["blanks"])])
        for d in s.standards:
            standards.append([s.name, d["code"], d["name"], float(d["concentration"]), d["rt"], d["area"],
                              bool(d.get("quantify", True)), d["status"]])
        for r in s.rows:
            d = r.derived
            peaks.append([s.name, r.rt, r.name, r.cas, d["raw_area"], d["blank_area"], d["corr_area"],
                          d["amount_ug"], d["conc"], d["istd"]])
    audit = wb.create_sheet("Audit")
    audit.append(["Timestamp", "Run", "Action", "Before", "After", "Detail"])
    for record in job.audit:
        audit.append([record.timestamp, record.run, record.action, record.before, record.after, record.detail])
    for sheet in (detail, standards, peaks, audit):
        sheet.freeze_panes = "A2"
        for cells in sheet:
            for cell in cells:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        for c in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(c)].width = 24
    from gcws.core.text import ILLEGAL, excel_safe
    for sheet in wb.worksheets:
        for line in sheet.iter_rows():
            for cell in line:
                if isinstance(cell.value, str) and ILLEGAL.search(cell.value):
                    cell.value = excel_safe(cell.value)
    wb.save(job.target)
    write_word(job.word, title, subtitle, job.names, layout["titles"], rows)
    reported = [{"name": r[1], "cas": r[2], "rt": r[0]} for r in rows]
    warnings = list(getattr(job, "notes", None) or [])
    if job.record_seen:
        error = RS.record_seen(job.kind, reported, job.target, job.sample_key)
        if error:
            warnings.append(error)
    return RS.ReportResult(job.target, job.word, len(rows), warnings=warnings, reported=reported)


def write_word(path, title, subtitle, names, headers, rows):
    """Same five columns, font and width proportions as the Fingerprint Word report."""
    from docx import Document
    from docx.shared import Cm, Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(1.75)
    section.top_margin = section.bottom_margin = Cm(1.5)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Arial", Pt(8)
    normal.paragraph_format.space_after = Pt(0)
    table = doc.add_table(rows=5, cols=5)
    table.autofit = False
    widths = [8, 46, 14, 10, 12]
    for c, width in enumerate(widths):
        table.columns[c].width = Cm(17.5 * width / sum(widths))
    for row in table.rows:
        for c, cell in enumerate(row.cells):
            cell.width = Cm(17.5 * widths[c] / sum(widths))
    for r, text in ((0, title), (1, subtitle)):
        cell = table.cell(r, 0).merge(table.cell(r, 4))
        cell.text = text
    band = OxmlElement("w:shd")
    band.set(qn("w:fill"), "BA0C2F")
    table.cell(0, 0)._tc.get_or_add_tcPr().append(band)
    for run in table.cell(0, 0).paragraphs[0].runs:
        run.bold, run.font.size = True, Pt(10)
        run.font.color.rgb = RGBColor.from_string("FFFFFF")
    for r, label, value in ((2, "Sample:", "; ".join(names)), (3, "Method:", "Headspace; TIC; internal standard response")):
        table.cell(r, 0).text = label
        table.cell(r, 1).merge(table.cell(r, 4)).text = value
    for c, h in enumerate(headers):
        table.cell(4, c).text = h
        borders = OxmlElement("w:tcBorders")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "4")
        borders.append(bottom)
        table.cell(4, c)._tc.get_or_add_tcPr().append(borders)
        for run in table.cell(4, c).paragraphs[0].runs:
            run.bold = True
    for row in table.rows:
        repeat = OxmlElement("w:tblHeader")
        row._tr.get_or_add_trPr().append(repeat)
    for values in rows:
        cells = table.add_row().cells
        for c, value in enumerate(values):
            cells[c].width = Cm(17.5 * widths[c] / sum(widths))
            cells[c].text = "" if value is None else (f"{value:.4f}" if c == 4 else
                                                      f"{value:.2f}" if c == 0 else str(value))
            if c >= 3:
                from docx.enum.text import WD_ALIGN_PARAGRAPH
                cells[c].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        no_split = OxmlElement("w:cantSplit")
        table.rows[-1]._tr.get_or_add_trPr().append(no_split)
    doc.save(path)

"""The batch report: all samples of one batch folder together.

* a Word document with every sample's report one after the other (the NIAS main script's
  ``create_combined_word``, which the NIAS batch processor uses too), optionally as PDF;
* a Report² summary workbook: one row per sample with its status, reviewer and findings.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from gcws.automation import store

STATUS = {"accepted_auto": "No finding (Report²)", "accepted_manual": "Accepted (analyst)",
          "control": "Control needed", "rejected": "Rejected", "not_processed": "Not processed",
          "failed": "Failed", "waiting": "Waiting", "queued": "Queued", "processing": "Processing"}


def batch_stem(name: str) -> str:
    return store.safe_name(name, 60)


def combined_word(xlsx: list[Path], target: Path) -> Path:
    from gcws.report.legacy_api import main_script
    target.parent.mkdir(parents=True, exist_ok=True)
    return Path(main_script().create_combined_word([Path(p) for p in xlsx], target))


def summary_workbook(batch_name: str, entries: list[dict], target: Path) -> Path:
    """``entries``: name, state, reviewer, comment, reviewed, findings [{text, substance, member}], files."""
    from openpyxl import Workbook
    from gcws.core.text import excel_safe
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    sh = wb.active
    sh.title = "Report²"
    sh.append([f"Batch {batch_name}", "", "", f"created {datetime.now().strftime('%Y-%m-%d %H:%M')}"])
    sh["A1"].font = Font(bold=True, size=13)
    sh.append([])
    head = ["Sample", "Status", "Reviewed by", "Reviewed", "Comment", "Findings", "Details", "Report"]
    sh.append(head)
    for c in sh[3]:
        c.font = Font(bold=True)
    fills = {"control": "FFF2CC", "accepted_auto": "E2F0D9", "accepted_manual": "E2F0D9", "rejected": "F8CBAD",
             "not_processed": "F8CBAD", "failed": "F8CBAD"}
    for e in entries:
        findings = e.get("findings") or []
        details = "\n".join(" - ".join(x for x in (f.get("member"), f.get("substance"), f.get("text")) if x)
                            for f in findings)
        sh.append([excel_safe(v) for v in (
            e.get("name", ""), STATUS.get(e.get("state"), e.get("state", "")), e.get("reviewer") or "",
            e.get("reviewed") or "", e.get("comment") or "", len(findings), details, str(e.get("report") or ""))])
        row = sh.max_row
        fill = fills.get(e.get("state"))
        if fill:
            sh.cell(row, 2).fill = PatternFill("solid", fgColor=fill)
        sh.cell(row, 7).alignment = Alignment(wrap_text=True, vertical="top")
    for col, width in zip("ABCDEFGH", (34, 22, 14, 17, 30, 9, 70, 50)):
        sh.column_dimensions[col].width = width
    target.parent.mkdir(parents=True, exist_ok=True)
    wb.save(target)
    return target


def _sheet_title(name: str, used: set) -> str:
    """A worksheet name Excel accepts (31 characters, no []:*?/\\), unique in the workbook."""
    import re
    base = (re.sub(r"[\[\]:*?/\\]+", "_", str(name)).strip("' ") or "Report")[:31]
    title, n = base, 2
    while title.casefold() in used:
        tail = f" ({n})"
        title, n = base[:31 - len(tail)] + tail, n + 1
    used.add(title.casefold())
    return title


def combined_workbook(reports: list[tuple[str, Path]], target: Path) -> Path:
    """One workbook with every sample's Excel report as a sheet of its own (``reports``: (sample name,
    report workbook)): values, formats, merged cells, column widths and row heights of the report's
    first visible sheet. A report that cannot be read is left out."""
    from copy import copy
    from openpyxl import Workbook, load_workbook
    wb = Workbook()
    wb.remove(wb.active)
    used: set = set()
    for name, path in reports:
        try:
            src = load_workbook(path)
        except Exception:  # noqa: BLE001 - damaged or not a workbook: the others are still combined
            continue
        sheet = next((s for s in src.worksheets if s.sheet_state == "visible"), src.worksheets[0])
        out = wb.create_sheet(_sheet_title(name, used))
        for row in sheet.iter_rows():
            for c in row:
                if type(c).__name__ == "MergedCell":
                    continue
                d = out.cell(row=c.row, column=c.column, value=c.value)
                if c.has_style:
                    d.font, d.fill, d.border = copy(c.font), copy(c.fill), copy(c.border)
                    d.alignment, d.protection = copy(c.alignment), copy(c.protection)
                    d.number_format = c.number_format
        for rng in sheet.merged_cells.ranges:
            out.merge_cells(str(rng))
        for key, dim in sheet.column_dimensions.items():
            out.column_dimensions[key].width = dim.width
            out.column_dimensions[key].hidden = dim.hidden
        for idx, dim in sheet.row_dimensions.items():
            if dim.height:
                out.row_dimensions[idx].height = dim.height
        out.sheet_view.showGridLines = sheet.sheet_view.showGridLines
        out.page_setup.orientation = sheet.page_setup.orientation
        out.page_setup.paperSize = sheet.page_setup.paperSize
        out.sheet_properties.pageSetUpPr = copy(sheet.sheet_properties.pageSetUpPr)
        out.page_setup.fitToWidth, out.page_setup.fitToHeight = sheet.page_setup.fitToWidth, \
            sheet.page_setup.fitToHeight
    if not wb.worksheets:
        raise ValueError("no sample report to combine")
    target.parent.mkdir(parents=True, exist_ok=True)
    wb.save(target)
    return target


def report_groups(jobs: list, out_dir: Path, batch_name: str, formats=("batch_docx", "batch_xlsx"),
                  progress=lambda text: None) -> tuple[list[dict], dict, list[str]]:
    """GC Workspace's batch report (worker thread): every sample's report, judged by the Report²
    rules, then all together. ``jobs``: ``[(group, ReportJob, evidence)]`` prepared on the GUI
    thread. Returns ``(entries, batch files, warnings)``."""
    from gcws.automation import pipeline as PL
    from gcws.automation import rules as RU
    from gcws.report import service as RS
    rules = RU.load_default_rules()
    entries, warnings = [], []
    for n, (group, job, ev) in enumerate(jobs, 1):
        progress(f"{n}/{len(jobs)} {group['name']}")
        try:
            res = RS.generate(job)
        except Exception as exc:  # noqa: BLE001 - the other samples are still reported
            warnings.append(f"{group['name']}: {exc}")
            entries.append({"name": group["name"], "state": "failed", "findings": [{"text": str(exc)}]})
            continue
        ev = dict(ev, rows=PL.slim_rows(res), summary=res.summary, warnings=list(res.warnings))
        result = RU.evaluate(rules, ev)
        entries.append({"name": group["name"], "state": result.status, "findings": result.to_list(),
                        "xlsx": str(res.target), "report": str(res.word or res.target)})
    files, more = batch_report(batch_name, entries, out_dir, formats)
    return entries, files, warnings + more


def batch_report(batch_name: str, entries: list[dict], out_dir: Path, formats, *,
                 pdf_timeout: float = 180.0) -> tuple[dict, list[str]]:
    """Write the batch files; returns ``({format: path}, warnings)``.

    ``entries`` are in injection order; the Word document takes each entry's ``xlsx`` report."""
    out_dir = Path(out_dir)
    stem = batch_stem(batch_name)
    files: dict = {}
    warnings: list[str] = []
    formats = set(formats or ())
    xlsx = [Path(e["xlsx"]) for e in entries if e.get("xlsx") and Path(e["xlsx"]).is_file()]
    if formats & {"batch_docx", "batch_pdf"}:
        if xlsx:
            try:
                word = combined_word(xlsx, out_dir / f"{stem}_Batch_Report.docx")
                files["batch_docx"] = str(word)
                if "batch_pdf" in formats:
                    from gcws.automation.pipeline import _pdf
                    pdf, err = _pdf(word, word.with_suffix(".pdf"), pdf_timeout)
                    if pdf is not None:
                        files["batch_pdf"] = str(pdf)
                    else:
                        warnings.append(err)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Batch Word report not created: {exc}")
        else:
            warnings.append("No sample report to combine")
    if "batch_xlsx" in formats:
        files["batch_xlsx"] = str(summary_workbook(batch_name, entries, out_dir / f"{stem}_Report2_Summary.xlsx"))
    return files, warnings

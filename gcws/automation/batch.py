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

STATUS = {"accepted_auto": "Accepted (automatic)", "accepted_manual": "Accepted (analyst)",
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
        sh.append([e.get("name", ""), STATUS.get(e.get("state"), e.get("state", "")), e.get("reviewer") or "",
                   e.get("reviewed") or "", e.get("comment") or "", len(findings), details,
                   str(e.get("report") or "")])
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

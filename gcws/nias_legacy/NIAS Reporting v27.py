#!/usr/bin/env python3
"""
Create GC-MS/FID Excel evaluations and NIAS reports.

Workflow:
1. Convert GC data to a Doppelbestimmung or Fingerprint Excel workbook.
2. Review and edit the generated Excel result manually.
3. Create the final NIAS report from the reviewed workbook.

Report inputs:
- Reviewed Fingerprint workbook (CAS reference optional; Area % report)
- Reviewed Doppelbestimmung workbook (CAS reference required)
- Legacy NIAS workbook with worksheet Auswertung (CAS reference required)

The script creates a new .xlsx file and never overwrites the source workbook.

Usage:
    Double-click the script or run without arguments for graphical file dialogs.
    Alternatively:
    python nias_screening_processor.py NIAS-Screening.xlsm CASINFO.xlsx -o NIAS_Result.xlsx

Dependencies:
    pip install openpyxl python-docx
    Optional for automatic Excel recalculation on Windows: pip install pywin32
"""

from __future__ import annotations

import argparse
import contextlib
from copy import copy
import functools
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import warnings
import statistics
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# openpyxl cannot preserve some Excel-only extensions while reading.
# The source file is opened read-only for values and is never overwritten,
# so these messages are informational and can safely be hidden here.
warnings.filterwarnings(
    "ignore",
    message=r"(Unknown extension|Conditional Formatting extension).*",
    module=r"openpyxl\.worksheet\._reader",
)
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.formatting.rule import FormulaRule
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

SCRIPT_VERSION = "v27.0"
HEADER_ROW = 27
MIN_CONCENTRATION_MG_KG = 0.01
INTERNAL_STANDARDS = {"IS1", "IS2", "IS3", "IS4"}
PUBCHEM_NO_HIT_TEXT = "Kein Treffer gefunden"
PUBCHEM_REQUEST_INTERVAL_SECONDS = 0.22  # stays below PubChem's 5 requests/s limit
PUBCHEM_TIMEOUT_SECONDS = 15

_GC_ENGINE = None


def gc_engine():
    """Load the colocated GC-data engine once for the integrated workflow."""
    global _GC_ENGINE
    if _GC_ENGINE is not None:
        return _GC_ENGINE
    engine_path = Path(__file__).resolve().parent / "AutoLib" / "Geänderten Python-Code herunterladen.py"
    if not engine_path.is_file():
        raise FileNotFoundError(
            "Das integrierte GC-Datenmodul wurde nicht gefunden: " + str(engine_path)
        )
    spec = importlib.util.spec_from_file_location("nias_gc_data_engine", engine_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Das GC-Datenmodul konnte nicht geladen werden.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _install_missing_standard_recovery(module)
    _install_batch_name_recognition(module)
    _GC_ENGINE = module
    return module


# Expected FID retention times used by the existing duplicate workflow.
# Recovery is intentionally restricted to named internal standards and a narrow
# window, so ordinary analyte peaks are never relabelled.
_GC_STANDARD_TARGETS = (
    ("Perdeutero-Heptadecane", 13.444, "000000-00-0", 4),
    ("Dibutyl phthalate-3,4,5,6-d4", 15.967, "093952-11-5", 399),
    ("Benzyl-butyl-phthalate-d4", 18.954, "000000-00-0", 1),
    ("Di-n-nonyl-phthalate-d4", 22.509, "000000-00-0", 2),
)


def _recover_missing_standard_pbm_rows(source: Path, target: Path) -> bool:
    """Add only missing internal-standard PBM rows from unambiguous FID peaks.

    Agilent can export a valid FID peak while omitting its PBM record. The old
    engine discovers standards through PBM names, therefore it incorrectly
    reports the standard as absent. This recovery reads the actual FID section,
    locates a unique peak within 0.08 min of the method target, and creates the
    missing PBM identity record. Existing PBM records are never replaced.
    """
    raw = source.read_bytes()
    encoding = "utf-8-sig"
    try:
        text_value = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        text_value = raw.decode(encoding)
    newline = "\r\n" if "\r\n" in text_value else "\n"
    lines = text_value.splitlines()

    sections = []
    for index, line in enumerate(lines):
        if line.strip().startswith("["):
            sections.append(index)
    sections.append(len(lines))

    fid_rts = []
    pbm_start = None
    pbm_end = None
    existing_text = ""
    row_re = re.compile(r"^\s*\d+=,\s*\d+\s*,\s*([-+0-9.eE]+)\s*,")
    for start, end in zip(sections, sections[1:]):
        title = lines[start].strip().casefold()
        body = lines[start + 1:end]
        if title.startswith("[int ") and "fid" in title:
            for line in body:
                match = row_re.match(line)
                if not match:
                    continue
                try:
                    fid_rts.append(float(match.group(1)))
                except ValueError:
                    pass
        elif title.startswith("[pbm "):
            pbm_start, pbm_end = start, end
            existing_text = "\n".join(body).casefold()

    if pbm_start is None or not fid_rts:
        return False

    additions = []
    used = set()
    for name, target_rt, cas, reference in _GC_STANDARD_TARGETS:
        if name.casefold() in existing_text:
            continue
        candidates = sorted(
            (abs(rt - target_rt), rt) for rt in fid_rts
            if rt not in used and abs(rt - target_rt) <= 0.08
        )
        if len(candidates) != 1:
            continue
        _, fid_rt = candidates[0]
        used.add(fid_rt)
        # Use a high synthetic key outside normal peak numbering. The parser
        # reads the second field as PBM peak and matches by RT/name.
        key = 9000 + len(additions) + 1
        additions.append(
            f'{key}=, {key}, {fid_rt:.4f}, 0.0000,"{name}",{reference},"{cas}",100'
        )

    if not additions:
        return False
    lines[pbm_end:pbm_end] = additions
    output = newline.join(lines)
    if text_value.endswith(("\n", "\r")):
        output += newline
    target.write_text(output, encoding=encoding, newline="")
    return True



#: "Duplicate status" of a result row whose analysis consists of a single
#: determination (spec v2.1 SS V.6). Written by the engine
#: (``AutoLib.SINGLE_DETERMINATION_STATUS``) and by ``gc_export``; repeated here
#: because the engine is loaded lazily and by path.
SINGLE_DETERMINATION_STATUS = "Einzelbestimmung"

#: Headers of the columns that belong to the second determination. For a single
#: determination they stay in place, empty, and are hidden -- the layout is the
#: one layout, and everything that reads by header name keeps working.
SECOND_DETERMINATION_HEADERS = (
    "Area 2", "Concentration 2 [mg/kg]", "Relative difference [%]")


def _duplicate_header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", text(value).casefold().replace("²", "2"))


def _copy_cell_payload(source, target) -> None:
    """Copy a cell without copying worksheet-level objects that can corrupt OOXML."""
    target.value = source.value
    if source.has_style:
        target._style = copy(source._style)
    target.number_format = source.number_format
    target.font = copy(source.font)
    target.fill = copy(source.fill)
    target.border = copy(source.border)
    target.alignment = copy(source.alignment)
    target.protection = copy(source.protection)


def _move_worksheet_column(ws, source_column: int, target_column: int) -> None:
    """Move a column by copying cells, avoiding openpyxl delete/insert corruption."""
    if source_column == target_column or source_column < 1:
        return
    target_column = min(max(1, target_column), ws.max_column)
    saved = []
    for row in range(1, ws.max_row + 1):
        cell = ws.cell(row, source_column)
        saved.append((cell.value, copy(cell._style), copy(cell.alignment),
                      copy(cell.protection), cell.number_format))
    width = ws.column_dimensions[get_column_letter(source_column)].width
    if source_column < target_column:
        column_range = range(source_column, target_column)
        source_offset = 1
    else:
        column_range = range(source_column, target_column, -1)
        source_offset = -1
    for column in column_range:
        for row in range(1, ws.max_row + 1):
            _copy_cell_payload(ws.cell(row, column + source_offset), ws.cell(row, column))
        ws.column_dimensions[get_column_letter(column)].width = ws.column_dimensions[
            get_column_letter(column + source_offset)].width
    for row, (value, style, alignment, protection, number_format) in enumerate(saved, 1):
        cell = ws.cell(row, target_column)
        cell.value, cell._style = value, style
        cell.alignment, cell.protection = alignment, protection
        cell.number_format = number_format
    ws.column_dimensions[get_column_letter(target_column)].width = width


def _insert_worksheet_column(ws, column: int) -> None:
    """Insert one visual column by shifting cell payloads right without delete_cols."""
    old_max = ws.max_column
    for current in range(old_max, column - 1, -1):
        for row in range(1, ws.max_row + 1):
            _copy_cell_payload(ws.cell(row, current), ws.cell(row, current + 1))
        ws.column_dimensions[get_column_letter(current + 1)].width = (
            ws.column_dimensions[get_column_letter(current)].width)
    for row in range(1, ws.max_row + 1):
        cell = ws.cell(row, column)
        cell.value = None
        cell._style = copy(ws.cell(row, column + 1)._style)
        cell.alignment = copy(ws.cell(row, column + 1).alignment)



def _normalize_xlsx_package(path: Path) -> None:
    """Write schema-valid font child order so Excel does not request a repair."""
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ET.register_namespace("", namespace)
    font_order = {
        "name": 0, "charset": 1, "family": 2, "b": 3, "i": 4,
        "strike": 5, "outline": 6, "shadow": 7, "condense": 8,
        "extend": 9, "sz": 10, "color": 11, "u": 12,
        "vertAlign": 13, "scheme": 14,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            payload = source.read(item.filename)
            if item.filename == "xl/styles.xml":
                root = ET.fromstring(payload)
                fonts = root.find(f"{{{namespace}}}fonts")
                if fonts is not None:
                    for font in fonts:
                        children = list(font)
                        children.sort(key=lambda child: font_order.get(
                            child.tag.rsplit("}", 1)[-1], 999))
                        font[:] = children
                payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            target.writestr(item, payload)
    temporary.replace(path)

def _rebuild_worksheet_table(ws) -> None:
    """Re-anchor the sheet's Excel table after columns were moved or inserted.

    openpyxl only regenerates ``tableColumns`` from the header row while that
    list is still empty. A table read back from disk keeps the column names it
    was written with, so after a column was inserted or moved the stored table
    no longer matches the sheet and Excel repairs ``xl/tables/tableN.xml`` when
    the file is opened. Replacing the table with a fresh one restores the match.
    """
    existing = list(ws.tables.values())
    if not existing:
        return
    source = existing[0]
    display_name, style = source.displayName, source.tableStyleInfo
    for name in list(ws.tables):
        del ws.tables[name]
    headers = [text(ws.cell(1, column).value)
               for column in range(1, ws.max_column + 1)]
    if "" in headers or len({header.casefold() for header in headers}) != len(headers):
        # Excel requires unique, non-empty header names. Without them the table
        # would be repaired again, so the sheet stays readable without one.
        return
    rebuilt = Table(
        displayName=display_name,
        ref=f"A1:{get_column_letter(ws.max_column)}{ws.max_row}",
    )
    rebuilt.tableStyleInfo = style
    ws.add_table(rebuilt)


# The engine links every duplicate concentration to its detail sheet, e.g.
# "=Bestimmung_1!G17". That reference carries the row needed for the area link.
_DUPLICATE_DETAIL_REFERENCE = re.compile(
    r"^=\s*(Bestimmung_\d+)!\$?G\$?(\d+)\s*$", re.IGNORECASE)


def _detail_factor_column(workbook, sheet_name: str) -> str:
    """Return the Bestimmung_n column letter holding the quantification factor.

    The column used to be a fixed "T". Adding one audit column to the detail
    sheet shifted it, which would have multiplied every concentration by the
    wrong cell, so the position is resolved from the header row instead.
    """
    if sheet_name in workbook.sheetnames:
        ws = workbook[sheet_name]
        for column in range(1, ws.max_column + 1):
            if normalized_header(ws.cell(1, column).value) == "quantificationfactor":
                return get_column_letter(column)
    return "U"


def _parameter_cell_reference(workbook, aliases: set[str], default_row: int) -> str:
    """Return the absolute Parameter!$B$n reference for one parameter label."""
    row = default_row
    if DUPLICATE_PARAMETER_SHEET in workbook.sheetnames:
        ws = workbook[DUPLICATE_PARAMETER_SHEET]
        for candidate in range(2, ws.max_row + 1):
            if normalized_header(ws.cell(candidate, 1).value) in aliases:
                row = candidate
                break
    return f"'{DUPLICATE_PARAMETER_SHEET}'!$B${row}"


def _is_single_determination_sheet(ws) -> bool:
    """True when the result sheet was written from one determination.

    Read from the data rather than from a flag, so an existing workbook opened
    later is judged the same way as one just written. A sheet with no data rows
    is not a single determination -- it is empty.
    """
    headers = {_duplicate_header_key(ws.cell(1, col).value): col
               for col in range(1, ws.max_column + 1)}
    status_column = headers.get("duplicatestatus")
    if status_column is None or ws.max_row < 2:
        return False
    statuses = [text(ws.cell(row, status_column).value)
                for row in range(2, ws.max_row + 1)]
    statuses = [value for value in statuses if value]
    return bool(statuses) and all(
        value.startswith(SINGLE_DETERMINATION_STATUS) for value in statuses)


def _hide_second_determination_columns(ws) -> None:
    """Hide the second determination's columns on a single-determination sheet.

    A no-op for a Doppelbestimmung. The columns are located by header name
    because their positions change while the sheet is post-processed.
    """
    if not _is_single_determination_sheet(ws):
        return
    wanted = {_duplicate_header_key(name) for name in SECOND_DETERMINATION_HEADERS}
    for column in range(1, ws.max_column + 1):
        # Clear first, then set: the engine hid the columns by letter and the
        # inserted Area columns shifted the contents past those letters, so a
        # stale flag would otherwise hide a first-determination column.
        letter = get_column_letter(column)
        ws.column_dimensions[letter].hidden = (
            _duplicate_header_key(ws.cell(1, column).value) in wanted)


def _save_recalculating(workbook, path: Path) -> None:
    """Save ``workbook`` so Excel recalculates it on open, then close it.

    ``_normalize_xlsx_package`` rewrites the whole zip, so it runs once, after
    the last change -- never between two edits of the same file.
    """
    try:
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        workbook.calculation.calcMode = "auto"
    except Exception:
        pass
    workbook.save(path)
    workbook.close()
    _normalize_xlsx_package(path)


def _postprocess_duplicate_workbook(output_path: Any) -> None:
    """Create a stable review workbook and link areas to concentrations."""
    path = Path(output_path)
    if not path.is_file():
        return
    workbook = load_workbook(path)
    if not _postprocess_duplicate_sheet(workbook):
        workbook.close()
        return
    _save_recalculating(workbook, path)


def finalize_duplicate_workbook(output_path: Any, metadata: dict[str, Any]) -> None:
    """Post-process an engine workbook and write the report metadata, in one save.

    The same two steps as :func:`_postprocess_duplicate_workbook` followed by
    :func:`write_migration_metadata_to_workbook`, in that order, but on one
    loaded workbook: each separate step loaded, saved and re-zipped the whole
    file, which is most of the time a batch spends after the engine.
    """
    metadata = validate_migration_metadata(metadata)
    path = Path(output_path)
    workbook = load_workbook(path)
    try:
        _postprocess_duplicate_sheet(workbook)
        _apply_migration_metadata(workbook, metadata)
    except BaseException:
        workbook.close()
        raise
    _save_recalculating(workbook, path)


#: Output paths collected while :func:`deferred_duplicate_postprocessing` is
#: active; ``None`` outside it, when the engine wrapper post-processes at once.
_DEFERRED_DUPLICATE_OUTPUTS: Optional[list[Path]] = None


@contextlib.contextmanager
def deferred_duplicate_postprocessing():
    """Collect the engine's Doppelbestimmung outputs instead of post-processing.

    For a caller that writes the report metadata into the same files right
    after the engine: it then finishes them with :func:`finish_duplicate_batch`
    (one save per file) instead of two saves per file.
    """
    global _DEFERRED_DUPLICATE_OUTPUTS
    previous = _DEFERRED_DUPLICATE_OUTPUTS
    collected: list[Path] = []
    _DEFERRED_DUPLICATE_OUTPUTS = collected
    try:
        yield collected
    finally:
        _DEFERRED_DUPLICATE_OUTPUTS = previous


def finish_duplicate_batch(info: dict[str, Any], pending: list[Path],
                           metadata_for) -> None:
    """Finish every workbook a deferred ``process_duplicate_batch`` produced.

    ``metadata_for(created_item)`` returns the metadata of one created
    analysis. A file that fails here moves from ``info["created"]`` to
    ``info["failed"]``, as a post-processing error inside the engine call
    always did. A pending file the engine did not report as created is still
    post-processed, so no output is left in its raw engine layout.
    """
    by_path: dict[Path, Any] = {}
    for item in info.get("created", []):
        output = item.get("output") if isinstance(item, dict) else item
        if output:
            by_path[Path(output).resolve()] = item
    finished: set[Path] = set()
    for path in pending:
        key = Path(path).resolve()
        if key in finished:
            continue
        finished.add(key)
        item = by_path.get(key)
        try:
            if item is None:
                _postprocess_duplicate_workbook(path)
            else:
                finalize_duplicate_workbook(path, metadata_for(item))
        except Exception as exc:
            if item is None:
                raise
            # The metadata failed: still give the file its review layout, so a
            # rerun or a manual check does not start from the raw engine sheet.
            try:
                _postprocess_duplicate_workbook(path)
            except Exception:
                pass
            info["created"].remove(item)
            name = item.get("sample") if isinstance(item, dict) else ""
            info.setdefault("failed", []).append(
                {"sample": name or Path(path).name, "error": str(exc)})
    # Reported as created without passing the engine wrapper: metadata only.
    for key, item in by_path.items():
        if key not in finished:
            write_migration_metadata_to_workbook(key, metadata_for(item))


def _postprocess_duplicate_sheet(workbook) -> bool:
    """Apply the review layout to a loaded workbook; ``False`` if it has none."""
    if DUPLICATE_SHEET_NAME not in workbook.sheetnames:
        return False
    ws = workbook[DUPLICATE_SHEET_NAME]

    def header_map() -> dict[str, int]:
        return {_duplicate_header_key(ws.cell(1, col).value): col
                for col in range(1, ws.max_column + 1) if ws.cell(1, col).value is not None}

    def concentration_column(headers: dict[str, int], sample: int) -> Optional[int]:
        candidates = [col for key, col in headers.items()
                      if ("mgkg" in key or "concentration" in key)
                      and str(sample) in key and "mean" not in key]
        return min(candidates) if candidates else None

    # First put relative difference at the far right without deleting worksheet columns.
    headers = header_map()
    rel_column = next((col for key, col in headers.items()
                       if "reldifference" in key or "relativedifference" in key), None)
    if rel_column is not None and rel_column != ws.max_column:
        _move_worksheet_column(ws, rel_column, ws.max_column)

    # Record which detail row feeds each concentration cell before the column
    # positions change. This keeps the validated quantification chain intact.
    headers = header_map()
    concentration_columns = {sample: concentration_column(headers, sample)
                             for sample in (1, 2)}
    detail_sources: dict[tuple[int, int], tuple[str, int]] = {}
    for sample, column in concentration_columns.items():
        if column is None:
            continue
        for row in range(2, ws.max_row + 1):
            match = _DUPLICATE_DETAIL_REFERENCE.match(text(ws.cell(row, column).value))
            if match:
                detail_sources[(sample, row)] = (match.group(1), int(match.group(2)))

    # Insert Area 1/2 directly before their concentration columns.
    for sample, column in sorted(
            ((sample, column) for sample, column in concentration_columns.items()
             if column is not None), key=lambda item: item[1], reverse=True):
        _insert_worksheet_column(ws, column)
        header = ws.cell(1, column)
        header.value = f"Area {sample}"
        header._style = copy(ws.cell(1, column + 1)._style)
        header.alignment = copy(ws.cell(1, column + 1).alignment)
        ws.column_dimensions[get_column_letter(column)].width = 11

    # Link Area -> Concentration -> mean -> relative difference through the
    # detail sheets. "Bestimmung_n" holds the blank-corrected FID area in column
    # E and the mean quantification factor in the "Quantification factor" column,
    # and mg/kg is that product times the O/V ratio. Editing an area therefore recalculates the
    # concentration, the mean and the relative difference immediately.
    headers = header_map()
    area_columns = {sample: next((col for key, col in headers.items()
                                  if key == f"area{sample}"), None) for sample in (1, 2)}
    concentration_columns = {sample: concentration_column(headers, sample)
                             for sample in (1, 2)}
    ov_reference = _parameter_cell_reference(workbook, {"ovratio", "oberflachevolumen"}, 11)
    unlinked_rows: set[int] = set()
    for sample in (1, 2):
        area_col = area_columns[sample]
        conc_col = concentration_columns[sample]
        if not area_col or not conc_col:
            continue
        area_letter = get_column_letter(area_col)
        for row in range(2, ws.max_row + 1):
            source = detail_sources.get((sample, row))
            if source is None:
                if text(ws.cell(row, conc_col).value):
                    unlinked_rows.add(row)
                continue
            sheet_name, detail_row = source
            area_cell = ws.cell(row, area_col)
            area_cell.value = f"={sheet_name}!E{detail_row}"
            area_cell.number_format = "#,##0"
            factor_letter = _detail_factor_column(workbook, sheet_name)
            factor_cell = f"{sheet_name}!{factor_letter}{detail_row}"
            concentration_cell = ws.cell(row, conc_col)
            # Empty, not #VALUE!, while no ISTD is marked for the determination.
            concentration_cell.value = (
                f'=IF(OR({area_letter}{row}="",{factor_cell}=""),"",'
                f"{area_letter}{row}*{factor_cell}*{ov_reference})")
            concentration_cell.number_format = "0.000000"

    # Rebuild mg/kg mean and relative difference from the two actual
    # concentration columns, which moved when the area columns were added.
    headers = header_map()
    mean_column = next((col for key, col in headers.items()
                        if key in {"mgkgmean", "concentrationmgkgmean", "meanmgkg"}
                        or ("mgkg" in key and "mean" in key)), None)
    rel_column = next((col for key, col in headers.items()
                       if "reldifference" in key or "relativedifference" in key), None)
    review_column = next((col for key, col in headers.items() if key == "review"), None)
    concentration_1, concentration_2 = concentration_columns[1], concentration_columns[2]
    if concentration_1 and concentration_2 and mean_column:
        c1 = get_column_letter(concentration_1)
        c2 = get_column_letter(concentration_2)
        for row in range(2, ws.max_row + 1):
            ws.cell(row, mean_column).value = (
                f'=IF(COUNT({c1}{row},{c2}{row})=0,"",AVERAGE({c1}{row},{c2}{row}))')
            ws.cell(row, mean_column).number_format = "0.000000"
            if rel_column:
                ws.cell(row, rel_column).value = (
                    f'=IFERROR(ABS({c1}{row}-{c2}{row})/AVERAGE({c1}{row},{c2}{row}),"")')
                ws.cell(row, rel_column).number_format = "0.0%"

    # Name the rows that could not be linked instead of leaving them silently static.
    if review_column and unlinked_rows:
        note = "Fläche nicht zuordenbar – Konzentration nicht verknüpft"
        for row in sorted(unlinked_rows):
            cell = ws.cell(row, review_column)
            existing_note = text(cell.value)
            if note not in existing_note:
                cell.value = f"{existing_note}; {note}".lstrip("; ")

    # The engine marks rows needing manual review with a static red fill, which
    # cannot react to an edited area. Both that marking and the "below the
    # reporting limit" marking therefore become conditional formatting: results
    # under the limit are greyed out and never shown in red.
    no_fill = PatternFill(fill_type=None)
    formula_font = Font(color="008000")
    plain_font = Font(color="000000")
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=ws.max_column):
        for cell in row:
            cell.fill = copy(no_fill)
            is_formula = isinstance(cell.value, str) and cell.value.startswith("=")
            cell.font = copy(formula_font if is_formula else plain_font)

    grey_fill = PatternFill("solid", fgColor="D9D9D9")
    grey_font = Font(color="7F7F7F")
    review_fill = PatternFill("solid", fgColor="FFC7CE")
    review_font = Font(color="9C0006")
    headers = header_map()
    status_column = next((col for key, col in headers.items()
                          if key == "duplicatestatus"), None)
    identification_column = next((col for key, col in headers.items()
                                  if key == "identificationstatus"), None)
    value_columns = [column for column in
                     (mean_column, concentration_1, concentration_2) if column]
    if ws.max_row >= 2 and value_columns:
        data_range = f"A2:{get_column_letter(ws.max_column)}{ws.max_row}"
        value_refs = ",".join(f"${get_column_letter(column)}2" for column in value_columns)
        single_refs = ",".join(f"${get_column_letter(column)}2" for column in
                               (concentration_1, concentration_2) if column)
        mean_ref = f"${get_column_letter(mean_column)}2" if mean_column else ""
        # The mean decides, exactly as in the report. An artefact has no mean, so
        # its single determination decides instead.
        measure = (f"IF(ISNUMBER({mean_ref}),{mean_ref},MAX({single_refs}))"
                   if mean_ref and single_refs else f"MAX({value_refs})")
        grey_rule = FormulaRule(
            formula=[f"AND(COUNT({value_refs})>0,"
                     f"{measure}<{MIN_CONCENTRATION_MG_KG:g})"],
            fill=grey_fill, font=grey_font, stopIfTrue=True)
        grey_rule.priority = 1
        ws.conditional_formatting.add(data_range, grey_rule)
        conditions = []
        if status_column:
            # "Valid duplicate" and "Einzelbestimmung" are the two statuses that
            # settle a row; both may carry a ", also in Blank" suffix, so the
            # test is on the leading characters, not on equality (SS V.6).
            status_ref = f"${get_column_letter(status_column)}2"
            conditions.append(
                f'AND(LEFT({status_ref},15)<>"Valid duplicate",'
                f'LEFT({status_ref},{len(SINGLE_DETERMINATION_STATUS)})'
                f'<>"{SINGLE_DETERMINATION_STATUS}")')
        if identification_column:
            conditions.append(
                f'${get_column_letter(identification_column)}2<>"Accepted"')
        if review_column:
            conditions.append(f'${get_column_letter(review_column)}2<>""')
        if conditions:
            review_rule = FormulaRule(
                formula=["OR(" + ",".join(conditions) + ")"],
                fill=review_fill, font=review_font, stopIfTrue=True)
            review_rule.priority = 2
            ws.conditional_formatting.add(data_range, review_rule)

    # Requested review layout.
    headers = header_map()
    for key, column in headers.items():
        if (key in {"rt", "rtmin", "rtmean", "rtmittel"}
                or key in {"area1", "area2"}
                or (("mgkg" in key or "concentration" in key)
                    and ("1" in key or "2" in key) and "mean" not in key)
                or "reldifference" in key or "relativedifference" in key):
            ws.column_dimensions[get_column_letter(column)].width = 11
        if key == "review" or "review" in key:
            ws.column_dimensions[get_column_letter(column)].width = 45

    # Re-hide the second determination's columns. The engine hid them by letter
    # when it wrote the workbook, but this function inserts the two Area columns
    # and moves the relative difference to the far right, so the letters have
    # moved. Resolving them by header name is the only way that survives that.
    _hide_second_determination_columns(ws)

    if ws.auto_filter.ref:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    # Must run last: the table has to match the final header row, otherwise
    # Excel reports "Repaired Records: Table from /xl/tables/table1.xml".
    _rebuild_worksheet_table(ws)
    return True


# Batch folders holding a solvent blank. Blanks are named after the solvent
# abbreviation, e.g. "01_EtOH", "01_DCM", "01_EtOAc_ISTD". Only EtOH and EtOAc
# used to be listed here, so a DCM, hexane or heptane batch reported "no Syneris
# number" for its blank folder and was then evaluated without any blank
# correction at all. Extend this set when a new solvent enters routine use.
#
# Solvent matching uses whole tokens or the complete normalized name, never
# substrings, so short abbreviations such as "Hex" cannot be taken out of
# "Hexadecane".
BATCH_SOLVENT_TOKENS = {
    # EtOH / MeOH / IPA
    "etoh", "ethanol", "meoh", "methanol",
    "ipa", "ipoh", "isopropanol", "2propanol", "propan2ol",
    # EtOAc
    "etoac", "ethylacetat", "ethylacetate", "ethylacetic",
    # DCM
    "dcm", "dichlormethan", "dichloromethan", "dichloromethane",
    "methylenchlorid", "methylenechloride",
    # Hex / Hep
    "hex", "hexan", "hexane", "nhexan", "nhexane",
    "hep", "heptan", "heptane", "nheptan", "nheptane",
    # ACN / THF
    "acn", "mecn", "acetonitril", "acetonitrile", "thf", "tetrahydrofuran",
    # Migration simulants that appear as their own blank folder
    "tenax", "essigsaure", "essigsaeure", "aceticacid", "hac",
}
BATCH_BLANK_TOKENS = {"blank", "blanc", "blind", "leerwert"}
BATCH_ISTD_MARKERS = ("istd", "internalstandard")
SYNERIS_FOLDER_NUMBER = re.compile(r"(?<!\d)(\d{8})(?!\d)")
BATCH_LEADING_NUMBER = re.compile(r"^\s*(\d+)")
_UMLAUT_FOLDING = str.maketrans({
    "ä": "a", "ö": "o", "ü": "u", "ß": "ss",
    "Ä": "a", "Ö": "o", "Ü": "u",
})


def _fold_folder_name(value: Any) -> str:
    """Return a folder name reduced to ASCII letters and digits only.

    ``re.split(r"[^0-9A-Za-z]+", ...)`` treats every umlaut as a separator, which
    tore "Essigsäure" into "essigs" and "ure" and made the name unmatchable.
    Folding first keeps such names intact.
    """
    folded = text(value).translate(_UMLAUT_FOLDING)
    folded = unicodedata.normalize("NFKD", folded)
    return "".join(character for character in folded if not unicodedata.combining(character))


def _batch_folder_parts(value: Any) -> tuple[set[str], str]:
    """Return the separator-delimited tokens and the fully normalized name."""
    name = re.sub(r"\.d$", "", _fold_folder_name(value), flags=re.IGNORECASE)
    tokens = {part.casefold() for part in re.split(r"[^0-9A-Za-z]+", name) if part}
    return tokens, re.sub(r"[^a-z0-9]+", "", name.casefold())


def batch_folder_sort_key(value: Any) -> tuple[int, str]:
    """Sort batch folders by their leading injection number, then by name.

    Plain alphabetic ordering puts "10_EtOH" before "9_EtOH", which would pick
    the wrong blank when a solvent was injected more than once.
    """
    name = text(value)
    match = BATCH_LEADING_NUMBER.match(name)
    return (int(match.group(1)) if match else 10 ** 9, name.casefold())


def classify_batch_folder(value: Any) -> str:
    """Classify one batch subfolder as ``sample``, ``blank`` or ``blank_istd``.

    The engine only knows the words blank/blanc/blind/leerwert, so solvent
    folders such as ``EtOH`` or ``EtOAc_ISTD`` were reported as "no Syneris
    number" and their blank never applied to the batch. Glued spellings like
    ``EtOHISTD`` are covered through the normalized form, while ordinary
    matching stays on token boundaries.

    A folder carrying an 8-digit Syneris number is always a determination, even
    when its name also mentions the solvent or a standard. That guard is what
    keeps a real sample such as "20251234_EtOH_A" out of the blank slot now that
    the solvent list is broad. An explicit blank keyword still wins, because a
    folder named "Blank_20250826" carries eight digits without being a sample.
    """
    tokens, normalized = _batch_folder_parts(value)
    has_istd = any(marker in normalized for marker in BATCH_ISTD_MARKERS)
    solvent_core = normalized
    for marker in BATCH_ISTD_MARKERS:
        solvent_core = solvent_core.replace(marker, "")
    solvent_core = solvent_core.strip("0123456789")
    has_solvent = (bool(tokens & BATCH_SOLVENT_TOKENS)
                   or solvent_core in BATCH_SOLVENT_TOKENS)
    has_blank_keyword = (bool(tokens & BATCH_BLANK_TOKENS)
                         or any(token in normalized for token in BATCH_BLANK_TOKENS))
    if has_blank_keyword:
        return "blank_istd" if has_istd else "blank"
    if SYNERIS_FOLDER_NUMBER.search(_fold_folder_name(value)):
        return "sample"
    if has_solvent or has_istd:
        return "blank_istd" if has_istd else "blank"
    return "sample"


#: The engine attributes that carry batch discovery. ``discover_batch_samples``
#: is the name since spec v2.1 SS V.6 -- a group of one is a valid analysis, so
#: "duplicate" no longer describes what it finds -- and ``discover_duplicate_samples``
#: is the alias the engine keeps for older callers. Both have to be wrapped, or
#: whichever one a caller reaches would miss the solvent and ISTD recognition.
BATCH_DISCOVERY_ATTRIBUTES = ("discover_batch_samples", "discover_duplicate_samples")


def _install_batch_name_recognition(engine) -> None:
    """Extend engine batch discovery with solvent and ISTD folder recognition."""
    original = next((getattr(engine, name) for name in BATCH_DISCOVERY_ATTRIBUTES
                     if getattr(engine, name, None) is not None), None)
    if original is None or getattr(original, "_nias_solvent_aliases", False):
        return

    def discover_with_aliases(batch_folder):
        samples, blanks, issues = original(batch_folder)
        blanks = dict(blanks or {})
        issues = list(issues or [])
        root = Path(batch_folder)
        recognized: dict[str, list[Path]] = {"blank": [], "blank_istd": []}
        for folder in sorted((item for item in root.iterdir() if item.is_dir()),
                             key=lambda item: batch_folder_sort_key(item.name)):
            kind = classify_batch_folder(folder.name)
            if kind != "sample":
                recognized[kind].append(folder)

        labels = {"blank": "Blank", "blank_istd": "Blank+ISTD"}
        for kind, folders in recognized.items():
            for folder in folders:
                if blanks.get(kind) and text(blanks[kind].get("name")) == folder.name:
                    continue
                results = next((item for item in folder.iterdir() if item.is_file()
                                and item.name.casefold() == "results.csv"), None)
                if results is None:
                    issues.append({
                        "sample": folder.name,
                        "error": f"als {labels[kind]} erkannt, aber keine RESULTS.CSV gefunden"})
                    continue
                if blanks.get(kind):
                    # Never resolve a second candidate silently.
                    issues.append({
                        "sample": folder.name,
                        "error": f"weiterer {labels[kind]}-Ordner; verwendet wird "
                                 f"{blanks[kind]['name']}"})
                    continue
                library = next((item for item in folder.iterdir() if item.is_file()
                                and item.name.casefold() == "libresults.csv"), None)
                blanks[kind] = {"name": folder.name, "folder": folder,
                                "results": results, "library": library}

        # A folder recognized as a blank must never also run as a determination,
        # and the engine's "no Syneris number" note about it is now misleading.
        blank_names = {folder.name for folders in recognized.values() for folder in folders}
        issues = [item for item in issues
                  if text(item.get("sample")) not in blank_names
                  or "erkannt" in text(item.get("error"))
                  or "weiterer" in text(item.get("error"))]
        remaining = []
        for sample in samples:
            used = [item["name"] for item in sample.get("determinations", ())
                    if item["name"] in blank_names]
            if used:
                issues.append({
                    "sample": sample["name"],
                    "error": "Blank-Ordner " + ", ".join(used)
                             + " wird nicht als Bestimmung ausgewertet"})
                continue
            remaining.append(sample)
        return remaining, blanks, issues

    discover_with_aliases._nias_solvent_aliases = True
    for name in BATCH_DISCOVERY_ATTRIBUTES:
        if hasattr(engine, name):
            setattr(engine, name, discover_with_aliases)


def _install_missing_standard_recovery(engine) -> None:
    """Use the recovery for single and batch Doppelbestimmung processing."""
    original=engine.make_duplicate_workbook
    if getattr(original,"_missing_standard_recovery",False):
        return

    # Positions and keyword names of the CSV inputs in make_duplicate_workbook.
    # The keyword names must match the engine signature exactly, otherwise a
    # keyword call would silently bypass the recovery.
    csv_parameters = {0: "results1", 1: "lib1", 2: "results2", 3: "lib2",
                      6: "blank_path", 7: "blank_istd_path"}

    def recovered_make_duplicate_workbook(*args, **kwargs):
        positional = list(args)
        with tempfile.TemporaryDirectory(prefix="nias_gc_std_") as temp_name:
            folder = Path(temp_name)

            def recover(value, label):
                if not value:
                    return value
                source = Path(value)
                if not source.is_file() or source.suffix.casefold() != ".csv":
                    return value
                destination = folder / f"{label}_{source.name}"
                if _recover_missing_standard_pbm_rows(source, destination):
                    return str(destination)
                return value

            for index, label in csv_parameters.items():
                if index < len(positional):
                    positional[index] = recover(positional[index], label)
                elif label in kwargs:
                    kwargs[label] = recover(kwargs[label], label)
            result = original(*positional, **kwargs)
            output_value = (positional[4] if len(positional) > 4
                            else kwargs.get("output_path") or kwargs.get("output"))
            if output_value and _DEFERRED_DUPLICATE_OUTPUTS is not None:
                _DEFERRED_DUPLICATE_OUTPUTS.append(Path(output_value))
            elif output_value:
                _postprocess_duplicate_workbook(output_value)
            return result

    recovered_make_duplicate_workbook._missing_standard_recovery = True
    engine.make_duplicate_workbook = recovered_make_duplicate_workbook


# --------------------------------------------------------------------------
# The analysis list (spec v2.1 SS V.6)
#
# A batch folder produces one row per *analysis*, not per determination: a
# Doppelbestimmung is one row with two determinations behind it, a single
# determination is one row with one. Everything below is free of Tk so it can be
# tested without a display; the Treeview that shows it lives in the GC-Daten
# page.
# --------------------------------------------------------------------------

#: Suffix of a GC-workspace session file (``gc_workspace.SESSION_SUFFIX``).
#: Repeated here because gc_workspace is imported lazily -- the main window has
#: to open even without matplotlib and tksheet.
GC_SESSION_SUFFIX = ".niasgc"

ANALYSIS_STATUS_OPEN = "offen"
ANALYSIS_STATUS_EDITED = "bearbeitet"
ANALYSIS_STATUS_EXPORTED = "exportiert"


def analysis_session_path(output_path: Path) -> Path:
    """Where the workspace session of one analysis is kept.

    Next to that analysis's workbook and named after it, so one batch folder can
    hold a session per analysis. ``GCWorkspace`` would otherwise derive the name
    from the folder it loaded, which for a Doppelbestimmung is the batch folder
    and would therefore be the same file for every analysis in the batch.
    """
    stem = output_path.stem
    marker = "_Doppelbestimmung"
    if stem.endswith(marker):
        stem = stem[:-len(marker)]
    return output_path.with_name(f"{stem}{GC_SESSION_SUFFIX}")


def analysis_status(analysis: dict[str, Any], session_path: Path,
                    workbook_path: Path) -> str:
    """``exportiert`` / ``bearbeitet`` / ``offen`` for one analysis.

    Read from the file system rather than remembered, so the list tells the truth
    after a restart. An exported analysis outranks an edited one: the workbook is
    the result, the session is the way there.
    """
    if workbook_path.exists():
        return ANALYSIS_STATUS_EXPORTED
    candidates = [session_path]
    # A determination opened on its own gets the session file GCWorkspace
    # derives from the folder itself; that counts as edited too.
    for item in analysis.get("determinations", ()):
        folder = Path(item["folder"])
        candidates.append(folder / f"{folder.name}{GC_SESSION_SUFFIX}")
    if any(path.exists() for path in candidates):
        return ANALYSIS_STATUS_EDITED
    return ANALYSIS_STATUS_OPEN


def analysis_rows(samples: list[dict[str, Any]], issues: list[dict[str, Any]],
                  session_paths: dict[str, Path],
                  workbook_paths: dict[str, Path]) -> list[dict[str, Any]]:
    """One row per analysis, plus one row per discovery problem.

    A problem that names an analysis is folded into that analysis's ``Hinweis``;
    one that names a folder or a Syneris number with no valid analysis behind it
    -- three determinations under one number, a folder without a Syneris number,
    a missing RESULTS.CSV -- gets a row of its own, because otherwise it would
    only ever be visible in a summary line nobody reads.
    """
    by_key: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for sample in samples:
        syneris = text(sample.get("syneris"))
        determinations = list(sample.get("determinations", ()))
        row = {
            "syneris": syneris,
            "name": text(sample.get("name")),
            # The count travels with the group; len() is the fallback for an
            # engine that predates SS V.6.
            "count": int(sample.get("count") or len(determinations)),
            "folders": [text(item.get("name")) for item in determinations],
            "notes": [],
            "analysis": sample,
            "session": session_paths.get(syneris),
            "workbook": workbook_paths.get(syneris),
            "error": False,
        }
        rows.append(row)
        for key in (syneris, row["name"]):
            if key:
                by_key.setdefault(key, row)

    for issue in issues or ():
        key = text(issue.get("sample"))
        message = text(issue.get("error"))
        target = by_key.get(key)
        if target is not None:
            target["notes"].append(message)
            continue
        rows.append({
            "syneris": key if SYNERIS_FOLDER_NUMBER.fullmatch(key) else "",
            "name": key, "count": 0, "folders": [], "notes": [message],
            "analysis": None, "session": None, "workbook": None, "error": True,
        })

    for row in rows:
        if row["error"]:
            row["status"] = ""
        else:
            row["status"] = analysis_status(
                row["analysis"], row["session"], row["workbook"])
        row["note"] = "; ".join(row["notes"])
    return rows


def determination_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The Einzelbestimmungen under one Doppelbestimmung row.

    Each child is shaped like an analysis row with a single determination, so it
    opens in the workspace the same way a genuine Einzelbestimmung does. Its
    session is the one ``GCWorkspace`` derives from the determination folder,
    not the pair's, so reviewing one determination on its own never touches the
    Doppelbestimmung's session or workbook. Empty for anything but a pair.
    """
    analysis = row.get("analysis")
    if row.get("error") or not analysis or row.get("count") != 2:
        return []
    children = []
    for number, item in enumerate(analysis.get("determinations", ()), start=1):
        folder = Path(item["folder"])
        session = folder / f"{folder.name}{GC_SESSION_SUFFIX}"
        children.append({
            "syneris": row["syneris"],
            "name": text(item.get("name")) or folder.name,
            "count": 1,
            "number": number,
            "folders": [text(item.get("name")) or folder.name],
            "notes": [],
            "note": "",
            "analysis": {**analysis, "determinations": [item], "count": 1},
            "session": session,
            "workbook": None,
            "error": False,
            "status": (ANALYSIS_STATUS_EDITED if session.exists()
                       else ANALYSIS_STATUS_OPEN),
        })
    return children


def gc_load_analysis_into_workspace(workspace, container: Path,
                                    determination_dirs, session_path=None) -> None:
    """Load exactly these determination folders into an open GC workspace.

    ``GCWorkspace.load_folder`` discovers every ``.D`` folder below the folder it
    is handed, which for a batch is every sample in the batch rather than the one
    analysis that was double-clicked. Until ``gc_workspace.open_workspace``
    accepts the folders directly (requested in ``.v21_reports\\V6.md``), the
    discovery function it calls is replaced for the duration of this one
    synchronous load and restored immediately afterwards -- so "Ordner
    wechseln…" in the workspace still behaves normally.
    """
    import gc_load

    # An analysis with a saved session opens *as that session*: loading the raw
    # folders and only pointing ``project_path`` at the file would let the next
    # Strg+S overwrite the saved edits with an unedited state.
    if session_path is not None and Path(session_path).is_file():
        workspace.load_session(Path(session_path))
        return
    dirs = [Path(item) for item in determination_dirs]
    original = getattr(gc_load, "find_d_dirs", None)
    if original is None or not dirs:
        workspace.load_folder(Path(container))
    else:
        gc_load.find_d_dirs = lambda _root, _dirs=dirs: list(_dirs)
        try:
            workspace.load_folder(Path(container))
        finally:
            gc_load.find_d_dirs = original
    if session_path is not None:
        workspace.session.project_path = Path(session_path)


def disable_duplicate_view(workspace) -> None:
    """Take the Doppelbestimmung view off a single determination's workspace.

    SS V.6: a single determination opens with one tab and no merged view. The
    workspace builds its toolbar without keeping references, so the button is
    found by its label; if the label ever changes the button simply stays
    enabled and still refuses the merge itself, which is why this never raises.
    """
    import tkinter as tk

    def walk(widget):
        for child in widget.winfo_children():
            try:
                # Frames and panes have no "text" option; asking is cheaper than
                # enumerating the widget classes that do.
                label = text(child.cget("text"))
            except tk.TclError:
                label = ""
            if label.startswith("Doppelbestimmung…"):
                try:
                    child.configure(state=tk.DISABLED)
                except tk.TclError:
                    pass
            walk(child)

    walk(workspace)


# --- Folder look of the analysis list ---------------------------------------
#
# ttk's Treeview draws neither tree lines nor icons of its own, so every row
# image is exactly one row tall and carries its share of the line: the open
# folder a stub down from its bottom edge, each Einzelbestimmung a vertical
# line through the row (only its upper half on the last one) with a branch to
# its file icon. Stacked, the pieces make one continuous line. With the
# style's ``indent`` at 0 the images, not ttk, indent the child rows.

#: Row height of ``Analysis.Treeview``; the images span exactly one row.
ANALYSIS_ROW_HEIGHT = 26
#: x of the tree line: the middle of the folder icon.
_TREE_LINE_X = 8
#: Where a child's file icon starts, i.e. how far it is indented.
_CHILD_ICON_X = 22
_FOLDER_FILL = "#E8B84A"
_FOLDER_EDGE = "#B98A22"


def analysis_tree_images(master) -> dict[str, Any]:
    """Blank row images for the analysis list; ``draw_...`` fills them."""
    import tkinter as tk

    widths = {"folder": 18, "folder_open": 18, "file": 18, "blank": 18,
              "branch": _CHILD_ICON_X + 16, "last": _CHILD_ICON_X + 16}
    return {key: tk.PhotoImage(master=master, width=width,
                               height=ANALYSIS_ROW_HEIGHT)
            for key, width in widths.items()}


def draw_analysis_tree_images(images: dict[str, Any], colors: dict[str, str]) -> None:
    """(Re)paint the row images in the theme's colours, in place."""
    height = ANALYSIS_ROW_HEIGHT
    middle = height // 2
    line, outline = colors["muted"], colors["muted"]
    paper, ruling = colors["surface"], colors["border"]

    def rect(image, color, x0, y0, x1, y1):
        image.put(color, to=(x0, y0, x1, y1))

    def folder(image):
        top = middle - 6
        rect(image, _FOLDER_EDGE, 1, top, 7, top + 2)          # tab
        rect(image, _FOLDER_EDGE, 1, top + 1, 17, top + 13)    # body
        rect(image, _FOLDER_FILL, 2, top + 3, 16, top + 12)

    def file(image, x):
        top = middle - 8
        rect(image, outline, x, top, x + 12, top + 16)
        rect(image, paper, x + 1, top + 1, x + 11, top + 15)
        rect(image, outline, x + 8, top, x + 12, top + 4)      # folded corner
        for offset, length in ((6, 6), (9, 6), (12, 4)):
            rect(image, ruling, x + 3, top + offset, x + 3 + length, top + offset + 1)

    for image in images.values():
        image.blank()
    folder(images["folder"])
    folder(images["folder_open"])
    rect(images["folder_open"], line, _TREE_LINE_X, middle + 7,
         _TREE_LINE_X + 1, height)
    file(images["file"], 3)
    for key, bottom in (("branch", height), ("last", middle + 1)):
        rect(images[key], line, _TREE_LINE_X, 0, _TREE_LINE_X + 1, bottom)
        rect(images[key], line, _TREE_LINE_X + 1, middle,
             _CHILD_ICON_X - 2, middle + 1)
        file(images[key], _CHILD_ICON_X)


# --- Bildschirm der Hauptanwendung (Multi-Monitor) ---------------------------
#
# Tk kennt nur *einen* Bildschirm: ``winfo_screenwidth`` liefert unter Windows
# die Maße des Hauptmonitors, unabhängig davon, auf welchem Monitor ein Fenster
# steht. Neue Toplevels landen deshalb regelmäßig auf dem Hauptmonitor, während
# die Anwendung auf dem zweiten läuft. Die folgenden Helfer fragen Windows nach
# dem Monitor, auf dem das Hauptfenster tatsächlich liegt, und öffnen jedes
# weitere Fenster auf genau diesem Monitor.

MONITOR_DEFAULTTONEAREST = 2
# Nur eine Geometrie *mit* Position (``WxH+X+Y`` oder ``+X+Y``) ist eine bewusste
# Platzierung durch den Aufrufer und wird nie überschrieben.
_GEOMETRY_POSITION_RE = re.compile(r"[+-]\d+[+-]\d+\s*$")
_GEOMETRY_SIZE_RE = re.compile(r"^(\d+)x(\d+)")
# Ein minimiertes Fenster meldet unter Windows -32000; solche Koordinaten sagen
# nichts über den Monitor aus.
_OFFSCREEN_LIMIT = -30000
_MAIN_WINDOW: Any = None
_MONITOR_API: list[Any] = []


def set_main_window(window) -> None:
    """Das Fenster merken, dessen Monitor alle weiteren Fenster bestimmt."""
    global _MAIN_WINDOW
    _MAIN_WINDOW = window


def _monitor_api():
    """Zwischengespeicherte Win32-Aufrufe für die Monitorabfrage."""
    if _MONITOR_API:
        return _MONITOR_API[0]
    api = None
    if sys.platform == "win32":
        try:
            import ctypes

            class _Point(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class _Rect(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            class _MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _Rect),
                            ("rcWork", _Rect), ("dwFlags", ctypes.c_ulong)]

            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.argtypes = [_Point, ctypes.c_ulong]
            user32.MonitorFromPoint.restype = ctypes.c_void_p
            user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(_MonitorInfo)]
            user32.GetMonitorInfoW.restype = ctypes.c_int
            api = (ctypes, user32, _Point, _MonitorInfo)
        except Exception:
            api = None
    _MONITOR_API.append(api)
    return api


def monitor_work_area(x: int, y: int) -> Optional[tuple[int, int, int, int]]:
    """Arbeitsbereich des Monitors, der dem Punkt ``(x, y)`` am nächsten liegt.

    Der Arbeitsbereich lässt die Taskleiste aus, ein darin platziertes Fenster
    bleibt also vollständig bedienbar. ``None`` außerhalb von Windows und immer
    dann, wenn die Win32-Abfrage nicht zur Verfügung steht; der Aufrufer fällt
    dann auf den Bildschirm zurück, den Tk selbst kennt.
    """
    api = _monitor_api()
    if api is None:
        return None
    ctypes, user32, point_type, info_type = api
    try:
        handle = user32.MonitorFromPoint(point_type(int(x), int(y)),
                                         MONITOR_DEFAULTTONEAREST)
        info = info_type()
        info.cbSize = ctypes.sizeof(info_type)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return None
    except Exception:
        return None
    work = info.rcWork
    width, height = int(work.right - work.left), int(work.bottom - work.top)
    if width <= 0 or height <= 0:
        return None
    return (int(work.left), int(work.top), width, height)


def _window_center(window) -> Optional[tuple[int, int]]:
    """Bildschirmmittelpunkt eines dargestellten Fensters, sonst ``None``."""
    if window is None:
        return None
    try:
        if not window.winfo_exists():
            return None
        # No ``update_idletasks()`` here: this runs while a new Toplevel is
        # being constructed, and flushing the idle queue would map that window
        # (a tooltip, say) with decorations before its caller has made it
        # borderless and positioned it -- the small windows that flashed on
        # hover. The main window has long been mapped; its geometry is current.
        width, height = window.winfo_width(), window.winfo_height()
        x, y = window.winfo_rootx(), window.winfo_rooty()
    except Exception:
        return None
    if width <= 1 or height <= 1 or x <= _OFFSCREEN_LIMIT or y <= _OFFSCREEN_LIMIT:
        return None
    return (x + width // 2, y + height // 2)


def main_screen_work_area(fallback_widget=None) -> tuple[int, int, int, int]:
    """``(x, y, Breite, Höhe)`` des Monitors, auf dem die Hauptanwendung steht."""
    center = _window_center(_MAIN_WINDOW)
    if center is not None:
        area = monitor_work_area(*center)
        if area is not None:
            return area
    # Hauptfenster noch nicht dargestellt: Windows öffnet es auf dem
    # Hauptmonitor, dessen Arbeitsbereich (ohne Taskleiste) dann gilt.
    area = monitor_work_area(1, 1)
    if area is not None:
        return area
    widget = _MAIN_WINDOW if _MAIN_WINDOW is not None else fallback_widget
    try:
        if widget is not None:
            return (0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())
    except Exception:
        pass
    return (0, 0, 1920, 1080)


# Rahmen und Titelleiste, die Windows um die von Tk gemeldete Größe legt, und
# ein kleiner Abstand zum Bildschirmrand. Ohne sie ragt ein "passendes" Fenster
# mit seiner Titelleiste doch über den Arbeitsbereich hinaus.
_FRAME_W, _FRAME_H = 16, 40
_SCREEN_GAP = 12
_GEOMETRY_FULL_RE = re.compile(
    r"^\s*(?:(\d+)x(\d+))?(?:([+-])(-?\d+)([+-])(-?\d+))?\s*$")


def _fitted_size(width: int, height: int,
                 area: tuple[int, int, int, int]) -> tuple[int, int]:
    """``(width, height)``, verkleinert auf das, was samt Rahmen in ``area`` passt.

    Die Fenster sind für große Monitore bemessen (1600×950 u. Ä.). Auf einem
    Laptop mit 150 % Skalierung bleiben davon rund 1280×680 übrig – ohne diese
    Begrenzung hing ein Teil des Fensters unter der Taskleiste oder neben dem
    Bildschirm.
    """
    _, _, area_w, area_h = area
    max_w = max(200, area_w - _FRAME_W - 2 * _SCREEN_GAP)
    max_h = max(150, area_h - _FRAME_H - 2 * _SCREEN_GAP)
    return min(width, max_w), min(height, max_h)


def _centered_position(width: int, height: int, area: tuple[int, int, int, int],
                       vertical: float = 0.45) -> tuple[int, int]:
    """Position, an der ein Fenster dieser Größe mittig in ``area`` steht."""
    area_x, area_y, area_w, area_h = area
    outer_w, outer_h = width + _FRAME_W, height + _FRAME_H
    x = area_x + max(0, (area_w - outer_w) // 2)
    y = area_y + max(0, int((area_h - outer_h) * vertical))
    return x, y


def _clamped_position(x: int, y: int, width: int, height: int,
                      area: tuple[int, int, int, int]) -> tuple[int, int]:
    """``(x, y)`` so verschoben, dass das Fenster ganz in ``area`` liegt."""
    area_x, area_y, area_w, area_h = area
    x = min(x, area_x + area_w - width - _FRAME_W)
    y = min(y, area_y + area_h - height - _FRAME_H)
    return max(x, area_x), max(y, area_y)


def _area_at(x: int, y: int, fallback_widget=None) -> tuple[int, int, int, int]:
    """Arbeitsbereich des Monitors unter ``(x, y)``, sonst der der Anwendung."""
    area = monitor_work_area(x, y)
    return area if area is not None else main_screen_work_area(fallback_widget)


def _requested_size(window) -> tuple[int, int]:
    """Größe, die das Fenster einnehmen wird – auch vor dem ersten Darstellen."""
    # Vor dem ersten Leerlauf meldet ``wm geometry`` noch 1x1; die zuletzt
    # gesetzte Größe ist dann nur hier bekannt.
    fitted = getattr(window, "_fitted_size", None)
    if fitted:
        return fitted
    try:
        if not window.winfo_ismapped():
            # Ein noch nie dargestelltes Fenster meldet über wm geometry
            # die Tk-Vorgabe 200x200. Damit gemittelt landete ein großes
            # Fenster mit der linken oberen Ecke in der Bildschirmmitte.
            return window.winfo_reqwidth(), window.winfo_reqheight()
        match = _GEOMETRY_SIZE_RE.match(window.geometry() or "")
    except Exception:
        match = None
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if width > 1 and height > 1:
            return width, height
    try:
        return (max(window.winfo_width(), window.winfo_reqwidth()),
                max(window.winfo_height(), window.winfo_reqheight()))
    except Exception:
        return (0, 0)


def place_on_main_screen(window, vertical: float = 0.45) -> None:
    """``window`` mittig auf dem Bildschirm der Hauptanwendung platzieren.

    Die Größe bleibt, solange das Fenster samt Rahmen auf den Monitor passt;
    ein größeres wird auf den Arbeitsbereich verkleinert. ``vertical`` ist der
    Anteil des freien Platzes oberhalb des Fensters – etwas über der Mitte
    liegt ruhiger im Bild.
    """
    area = main_screen_work_area(window)
    width, height = _requested_size(window)
    if width <= 1 or height <= 1:
        x, y = _centered_position(0, 0, area, vertical)
        geometry = f"+{x}+{y}"
    else:
        fit_w, fit_h = _fitted_size(width, height, area)
        x, y = _centered_position(fit_w, fit_h, area, vertical)
        geometry = (f"+{x}+{y}" if (fit_w, fit_h) == (width, height)
                    else f"{fit_w}x{fit_h}+{x}+{y}")
    try:
        window.geometry(geometry)
    except Exception:                        # pragma: no cover - Fenster ist weg
        pass


def fit_to_screen(window, width: int, height: int, min_width: int = 0,
                  min_height: int = 0, vertical: float = 0.45) -> None:
    """Ein Fenster, das nicht über ``ScreenBoundToplevel`` läuft (``tk.Tk``),
    mit höchstens ``width``×``height`` mittig auf seinen Monitor setzen."""
    area = main_screen_work_area(window)
    width, height = _fitted_size(width, height, area)
    x, y = _centered_position(width, height, area, vertical)
    try:
        if min_width and min_height:
            window.wm_minsize(*_fitted_size(min_width, min_height, area))
        window.geometry(f"{width}x{height}+{x}+{y}")
    except Exception:                        # pragma: no cover
        pass


def bind_new_windows_to_main_screen() -> None:
    """Jedes künftige ``tk.Toplevel`` passend auf dem Monitor der Anwendung öffnen.

    Ersetzt ``tkinter.Toplevel`` durch eine Unterklasse. Das erfasst auch die
    Fenster von ``gc_workspace``/``gc_dinspec``: ``GCWorkspace`` erbt von
    ``tk.Toplevel`` und wird erst beim Klick importiert, also nach diesem
    Aufruf.

    * Eine Größe ohne Position (``geometry("1600x950")``) wird auf den
      Arbeitsbereich begrenzt und sofort mittig platziert – ein Aufruf, kein
      Springen im Leerlauf.
    * ``minsize`` wird ebenso begrenzt, sonst erzwänge es die Übergröße wieder.
    * Eine Geometrie mit Position (wiederhergestellte Sitzung) bleibt auf ihrem
      Monitor, wird aber verkleinert und hereingeschoben, wo sie übersteht.
    * Fenster ohne Rahmen (Tooltips) und reine ``+X+Y``-Positionen setzt der
      Aufrufer selbst; sie bleiben unangetastet.
    """
    if tk is None or getattr(tk.Toplevel, "follows_main_screen", False):
        return

    base = tk.Toplevel

    class ScreenBoundToplevel(base):
        """``tk.Toplevel``, das passend auf dem Monitor der Anwendung öffnet."""

        follows_main_screen = True

        def __init__(self, master=None, cnf={}, **kwargs):
            self._placed_by_caller = False
            self._borderless = False
            self._placing_on_main_screen = False
            self._fitted_size = None
            super().__init__(master, cnf, **kwargs)
            # Einmal sofort, damit das Fenster gar nicht erst auf dem falschen
            # Monitor erscheint, und einmal im Leerlauf, wenn die endgültige
            # Größe feststeht und die Mitte stimmt.
            self._place_on_main_screen()
            try:
                self.after_idle(self._place_on_main_screen)
            except Exception:                # pragma: no cover
                pass

        def wm_geometry(self, newGeometry=None):
            if (not newGeometry or self._placing_on_main_screen
                    or self._borderless):
                return base.wm_geometry(self, newGeometry)
            match = _GEOMETRY_FULL_RE.match(str(newGeometry))
            if match is None:
                return base.wm_geometry(self, newGeometry)
            has_size = match.group(1) is not None
            has_pos = match.group(3) is not None
            if has_pos:
                self._placed_by_caller = True
            if not has_size:                 # reine Position: Sache des Aufrufers
                return base.wm_geometry(self, newGeometry)
            width, height = int(match.group(1)), int(match.group(2))
            if has_pos and "-" in (match.group(3), match.group(5)):
                # Vom rechten/unteren Rand gemessen – selten, unverändert lassen.
                return base.wm_geometry(self, newGeometry)
            if has_pos:
                x, y = int(match.group(4)), int(match.group(6))
                area = _area_at(x + width // 2, y + height // 2, self)
                width, height = _fitted_size(width, height, area)
                x, y = _clamped_position(x, y, width, height, area)
            else:
                area = main_screen_work_area(self)
                width, height = _fitted_size(width, height, area)
                self._fitted_size = (width, height)
                if self._placed_by_caller:   # Position gehört dem Aufrufer
                    return base.wm_geometry(self, f"{width}x{height}")
                x, y = _centered_position(width, height, area)
            self._fitted_size = (width, height)
            return base.wm_geometry(self, f"{width}x{height}+{x}+{y}")

        geometry = wm_geometry

        def wm_minsize(self, width=None, height=None):
            if width is not None and height is not None and not self._borderless:
                area = main_screen_work_area(self)
                width, height = _fitted_size(int(width), int(height), area)
            return base.wm_minsize(self, width, height)

        minsize = wm_minsize

        def wm_overrideredirect(self, boolean=None):
            if boolean:                      # Tooltips u. Ä. setzen ihre Position
                self._placed_by_caller = True
                self._borderless = True
            return base.wm_overrideredirect(self, boolean)

        overrideredirect = wm_overrideredirect

        def _place_on_main_screen(self) -> None:
            if self._placed_by_caller:
                return
            try:
                if not self.winfo_exists():
                    return
            except Exception:                # pragma: no cover
                return
            self._placing_on_main_screen = True
            try:
                if self._fitted_size is None:
                    # Erst jetzt steht fest, wie groß der Inhalt das Fenster
                    # macht; der Konstruktor des Aufrufers ist durchgelaufen.
                    self.update_idletasks()
                place_on_main_screen(self)
            finally:
                self._placing_on_main_screen = False

    ScreenBoundToplevel.__name__ = base.__name__
    ScreenBoundToplevel.__qualname__ = base.__qualname__
    tk.Toplevel = ScreenBoundToplevel


def attach_tooltip(widget, resolve) -> None:
    """Show ``resolve(event)`` next to the pointer while it rests on ``widget``.

    Tk has no tooltip of its own and the analysis list needs one: the
    ``Bestimmungen`` column shows a count, and the folder names behind that count
    are what an analyst checks when a number looks wrong (SS V.6).
    """
    from gc_tooltip import Tooltip

    def theme_colors() -> tuple[str, str, str]:
        # The tooltip is drawn on the table header tone: it stays distinct from
        # both the window background and a card in either theme, and a solid
        # black border would disappear in the dark one.
        colors = active_theme_colors()
        return colors["table_header"], colors["text"], colors["border"]

    Tooltip(widget, resolve=lambda event: text(resolve(event)), colors=theme_colors)


SUMMARY_LABELS = {
    "styrene": "Sum of styrene oligomers (estimated)**",
    "hydrocarbon": (
        "Sum of hydrocarbons (alkanes, cyclic alkanes, "
        "aliphatic ... estimated)**"
    ),
    "siloxane": "Sum of siloxanes (estimated)**",
    "cyclic_polyester": (
        "Sum of cyclic polyester oligomers containing XXX (estimated)**"
    ),
}

CALCULATION_NOTE = (
    "** Only peaks with a concentration above or equal to 10 ppb "
    "were included in the calculation."
)

CYCLIC_POLYESTER_FOOTNOTE = (
    "Sum of cyclic polyester oligomers ToxTree: Cramer Class III "
    "(0.09 mg/60 kg-person/day or 0.09 mg/kg food). "
    "The potential migration of polyester monomers generated from the hydrolysis "
    "of the cyclic oligomers has to be assessed separately."
)

# Canonical abbreviations and explanations. Only abbreviations actually found in
# the cyclic polyester result rows are printed in the final abbreviation note.
MONOMER_ABBREVIATIONS = {
    "EG": "ethylene glycol",
    "1,4-BD": "1,4 butanediol",
    "MP-diol": "2-methyl-1, 3-propanediol",
    "DEG": "diethylene glycol",
    "1,6-HD": "1,6 hexanediol",
    "NPG": "neopentyl glycol",
    "IPA": "isophthalic acid",
    "PG": "propylene glycol",
    "DPG": "dipropylene glycol",
    "TPG": "tripropylene glycol",
    "AA": "adipic acid",
    "AzA": "azelaic acid",
    "SeA": "sebacic acid",
    "SuA": "suberic acid",
    "PA isomer": (
        "phthalic/isophthalic/terephthalic acid (the substitution patterns of "
        "the phthalic acid isomers could not be established)"
    ),
    # Source strings frequently contain the short form PA, e.g. EG-AA-PG-PA.
    "PA": (
        "phthalic/isophthalic/terephthalic acid (the substitution patterns of "
        "the phthalic acid isomers could not be established)"
    ),
}


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalized_header(value: Any) -> str:
    value_text = text(value).lower()
    # Excel headers often contain superscript units such as dm².
    value_text = value_text.translate(str.maketrans({"²": "2", "³": "3", "µ": "u"}))
    return re.sub(r"[^a-z0-9]+", "", value_text)


def normalize_cas(value: Any) -> str:
    """Remove leading zeroes from all three CAS number blocks."""
    value_text = text(value)
    if not value_text:
        return ""

    # If a cell contains alternatives, use the first CAS number for matching.
    candidate = value_text.split("/")[0].strip()
    match = re.fullmatch(r"0*(\d+)-0*(\d+)-0*(\d+)", candidate)
    if match:
        return f"{int(match.group(1))}-{int(match.group(2)):02d}-{int(match.group(3))}"

    return candidate.lstrip("0")



def is_valid_cas_number(value: Any) -> bool:
    """Return True only for a syntactically valid CAS Registry Number."""
    return bool(re.fullmatch(r"\d{1,7}-\d{2}-\d", text(value)))


def pubchem_cache_file_path() -> Path:
    """Return the persistent per-user PubChem cache path."""
    return settings_file_path().parent / "pubchem_cache.json"


def load_pubchem_cache() -> tuple[dict[str, Optional[str]], dict[str, str]]:
    """Load cached PubChem names and their last-check timestamps."""
    path = pubchem_cache_file_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("entries", {})
        names: dict[str, Optional[str]] = {}
        checked: dict[str, str] = {}
        for cas_number, record in entries.items():
            if not isinstance(record, dict):
                continue
            cached_name = record.get("name")
            names[cas_number] = text(cached_name) or None
            checked[cas_number] = text(record.get("checked_at"))
        return names, checked
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}, {}


def save_pubchem_cache(
    names: dict[str, Optional[str]],
    checked: dict[str, str],
) -> None:
    """Persist verified PubChem results; no synthetic values are written."""
    path = pubchem_cache_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = {
        cas_number: {
            "name": name,
            "checked_at": checked.get(cas_number, ""),
            "source": "PubChem PUG-REST",
        }
        for cas_number, name in sorted(names.items())
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# The SQLite register (gc_register.DB_FILENAME) is the system of record. It
# holds the entries, the sightings and the measured spectra, and it is the only
# file written on a report run.
UNKNOWN_REGISTER_DB_FILENAME = "unknown_register.sqlite"
# The JSON register is an import source and a backup: it is read by the
# migration, by ``gc_register.py --verify`` and by an explicit backup, and it is
# no longer written when a report runs. Its schema version describes that file,
# not the SQLite schema.
UNKNOWN_REGISTER_FILENAME = "unknown_register.json"
UNKNOWN_REGISTER_SCHEMA_VERSION = 1
UNKNOWN_REGISTER_EXCEL_FILENAME = "Unknown_Dokumentation.xlsx"

# The register as a NIST-searchable library. Two files, never one: a measured
# spectrum and a spectrum modelled from a rank order are different kinds of
# evidence, and mixing them into one library would make it impossible to tell
# a real hit from an arithmetic one. Both are derived - deleting them costs a
# rebuild, never data.
UNKNOWN_MSP_FILENAME = "unknowns.msp"
UNKNOWN_MSP_MODELLED_FILENAME = "unknowns_modelliert.msp"
UNKNOWN_MSP_STAMP_FILENAME = "unknowns.msp.stamp"
#: A discarded signature is noise in a search library.
UNKNOWN_MSP_SKIP_STATUS = frozenset({"verworfen"})
#: Pseudo intensity of the rank model: round(999 / rank).
UNKNOWN_MSP_MODELLED_BASE = 999


def configured_unknown_register_dir() -> str:
    """Return the shared register directory exactly as configured, unchecked."""
    return text(load_user_settings().get("unknown_register_dir"))


def unknown_register_dir() -> Path:
    """Return the directory actually used for the register.

    The register is meant to be a shared team knowledge base on a network drive.
    An unreachable share must not lose data, so the per-user directory is used as
    a fallback - never silently: ``unknown_register_location()`` reports which of
    the two is in effect and the register tab shows it.
    """
    configured = configured_unknown_register_dir()
    if configured:
        from nias_paths import project_path
        candidate = project_path(configured)
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if candidate.is_dir():
                return candidate
        except OSError:
            pass
    return settings_file_path().parent


def shared_data_dir() -> Path:
    """The one folder a team shares: register, CASINFO, ladders, MSP libraries.

    The same directory ``unknown_register_dir()`` returns, under the name the
    options page uses. The setting keeps its old key because the whole
    resolution chain outside this file - ``gc_seen.default_path()``,
    ``gc_register.default_db_path()``, ``gc_workspace.ladder_store_path()`` -
    already resolves through it; renaming the key would quietly send every
    module back to its per-user fallback.
    """
    return unknown_register_dir()


def unknown_register_location() -> tuple[Path, bool, str]:
    """Return (directory in use, is the configured share, explanatory note)."""
    configured = configured_unknown_register_dir()
    active = unknown_register_dir()
    if not configured:
        return active, False, "Lokales Register (kein gemeinsamer Ordner konfiguriert)."
    if Path(configured) == active:
        return active, True, "Gemeinsames Register."
    return active, False, (
        f"Gemeinsamer Ordner nicht erreichbar ({configured}) - "
        "es wird lokal gearbeitet."
    )


def unknown_register_file_path() -> Path:
    """Return the JSON register - import source and backup, no longer written.

    It is still the file ``gc_register.py --verify`` reads, so a suspicion can
    be checked against the pre-cutover state for one release.
    """
    return unknown_register_dir() / UNKNOWN_REGISTER_FILENAME


def unknown_register_db_path() -> Path:
    """Return the SQLite register, which is the system of record."""
    return unknown_register_dir() / UNKNOWN_REGISTER_DB_FILENAME


def unknown_documentation_file_path() -> Path:
    """Return the Excel export of the register for unknown substances."""
    return unknown_register_dir() / UNKNOWN_REGISTER_EXCEL_FILENAME


def unknown_msp_library_paths(directory: Optional[Path] = None) -> dict[str, Path]:
    """The two MSP libraries and their stamp, beside the register database."""
    folder = Path(directory) if directory else unknown_register_dir()
    return {
        "measured": folder / UNKNOWN_MSP_FILENAME,
        "modelled": folder / UNKNOWN_MSP_MODELLED_FILENAME,
        "stamp": folder / UNKNOWN_MSP_STAMP_FILENAME,
    }


def extract_syneris_number(nias_path: Path, source_ws) -> str:
    """Extract the Syneris number from filename first, then from the sample field."""
    candidates = (nias_path.stem, text(source_ws["B2"].value))
    for candidate in candidates:
        match = re.search(r"(?i)\bSYN[-_ ]?([A-Z0-9-]+)", candidate)
        if match:
            return f"SYN{match.group(1)}"
    return text(source_ws["B2"].value)


def syneris_number_from_text(*candidates: Any) -> str:
    """Return the plain 8-digit Syneris number contained in a sample or file name."""
    for candidate in candidates:
        match = re.search(r"(?<!\d)(\d{8})(?!\d)", text(candidate))
        if match:
            return match.group(1)
    return ""


def _split_register_list(value: Any) -> list[str]:
    return [part.strip() for part in text(value).split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Access to the SQLite register
#
# There is deliberately no file lock any more. SQLite's own ``BEGIN IMMEDIATE``
# plus ``busy_timeout=15000`` serialises the writers, and one report run is one
# transaction. Keeping the old ``unknown_register.lock`` alongside it would
# deadlock on a share: two clients would each hold one of the two locks and wait
# for the other.
# ---------------------------------------------------------------------------

_GC_REGISTER_MODULE: Any = None
_GC_NIST_MODULE: Any = None


def gc_register_module() -> Any:
    """Import ``gc_register`` from beside this script, whatever sys.path says.

    The script is started by double-click, from the command line and through
    ``importlib`` out of the GC workspace, so the package directory is not
    reliably importable. Loading it by path is.
    """
    global _GC_REGISTER_MODULE
    if _GC_REGISTER_MODULE is not None:
        return _GC_REGISTER_MODULE
    try:
        import gc_register as module  # type: ignore[import-not-found]
    except ImportError:
        path = Path(__file__).resolve().parent / "gc_register.py"
        spec = importlib.util.spec_from_file_location("gc_register", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Das Modul gc_register.py wurde neben dem Skript nicht gefunden "
                f"({path}). Ohne dieses Modul ist das Unknown-Register nicht "
                "zugänglich.")
        module = importlib.util.module_from_spec(spec)
        sys.modules["gc_register"] = module
        spec.loader.exec_module(module)
    _GC_REGISTER_MODULE = module
    return module


def gc_nist_module() -> Any:
    """Import ``gc_nist`` from beside this script, exactly as gc_register is.

    Separate from the register module on purpose: NIST MS Search is optional
    equipment. A workstation without it must still be able to open the register,
    so the import failure has to be catchable at the one place that needs it.
    """
    global _GC_NIST_MODULE
    if _GC_NIST_MODULE is not None:
        return _GC_NIST_MODULE
    try:
        import gc_nist as module  # type: ignore[import-not-found]
    except ImportError:
        path = Path(__file__).resolve().parent / "gc_nist.py"
        spec = importlib.util.spec_from_file_location("gc_nist", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Das Modul gc_nist.py wurde neben dem Skript nicht gefunden "
                f"({path}). Ohne dieses Modul ist weder die Übergabe an NIST "
                "MS Search noch der MSP-Export möglich.")
        module = importlib.util.module_from_spec(spec)
        sys.modules["gc_nist"] = module
        spec.loader.exec_module(module)
    _GC_NIST_MODULE = module
    return module


def ensure_unknown_register_migrated() -> dict[str, Any]:
    """Refuse to touch the register while a populated JSON is unmigrated.

    This is the safety net for the cutover order: after the cutover nothing
    writes the JSON any more, so quietly opening an empty SQLite register beside
    a JSON holding years of sightings would orphan all of them. The migration is
    a deliberate, one-off command the analyst runs - it is not started from
    here, because the dry run has to be read first.
    """
    return gc_register_module().require_migrated(
        unknown_register_file_path(), unknown_register_db_path())


class unknown_register_connection:
    """``with unknown_register_connection() as con:`` - the register, gated.

    Opens the SQLite register with the share-safe pragmas of gc_register,
    creating the schema when the directory is still empty, and closes it again.

    The gate runs on reads as well as on writes, and deliberately so: an
    unmigrated register read without it answers "no entries", which is
    indistinguishable from an empty register and is exactly how years of
    sightings would be declared missing without anybody noticing. Refusing to
    open it at all turns that into a message naming the migration command.
    """

    def __init__(self, *, write: bool = False) -> None:
        self.write = write
        self.connection = None

    def __enter__(self):
        register = gc_register_module()
        path = unknown_register_db_path()
        ensure_unknown_register_migrated()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = register.connect(path)
        register.create_schema(self.connection)
        return self.connection

    def __exit__(self, *_exception) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def load_unknown_register_db() -> dict[str, Any]:
    """Read the SQLite register into the legacy dict shape.

    ``as_register_dict`` already serves exactly the nested dict the register
    tab, the Excel export and the recognition hints consume, so none of them had
    to change with the cutover.
    """
    register = gc_register_module()
    with unknown_register_connection() as connection:
        return register.as_register_dict(connection)


def write_unknown_register_json_backup(path: Optional[Path] = None) -> Path:
    """Dump the SQLite register to a timestamped JSON backup.

    Two things depend on this file existing: ``gc_register.py --verify`` reads a
    JSON, and a rollback to the pre-cutover behaviour needs one. The measured
    spectra are *not* in it - they live only in the SQLite file, which is why
    the database itself has to be backed up as well.
    """
    if path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = unknown_register_dir() / f"unknown_register.{stamp}.backup.json"
    with unknown_register_connection() as connection:
        return gc_register_module().dump_register_json(connection, Path(path))


def _new_unknown_entry(register: dict[str, Any], ranked: tuple[int, ...]) -> dict[str, Any]:
    """Create an empty master record for a m/z signature seen for the first time."""
    identifier = int(register.get("next_id", 1))
    register["next_id"] = identifier + 1
    canonical = sorted({int(mass) for mass in ranked})
    return {
        "id": f"UNK-{identifier:04d}",
        # Kept identical to the label the previous register used, so rows
        # imported from the old workbook line up with newly recorded ones.
        "label": "unknown (m/z " + format_mz_list(canonical) + ")",
        "canonical_mz": canonical,
        "ranked_mz": list(ranked),
        "ranked_variants": {},
        "base_peak": int(ranked[0]) if ranked else None,
        "rank_unknown": False,
        "status": UNKNOWN_STATUS_CHOICES[0],
        "assigned_name": "",
        "assigned_cas": "",
        "note": "",
        "linked_to": None,
        "sightings": [],
    }


def _sighting_key(sighting: dict[str, Any]) -> tuple[str, str, Optional[float]]:
    """Identify one occurrence, so re-running a report updates instead of appends."""
    rt = numeric_value(sighting.get("rt"))
    return (
        text(sighting.get("sample")).casefold(),
        text(sighting.get("report_type")).casefold(),
        None if rt is None else round(rt, 4),
    )


def recompute_unknown_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Refresh every derived field from the sightings.

    All aggregates are stored for the Excel export but stay fully recomputable
    from the sightings, so the register repairs itself after a manual edit.
    """
    sightings = entry.get("sightings") or []

    variants = Counter({
        key: int(value) for key, value in (entry.get("ranked_variants") or {}).items()
    })
    if variants and not entry.get("rank_unknown"):
        # The order the analysts wrote most often wins the ranking.
        entry["ranked_mz"] = list(parse_mz_list(variants.most_common(1)[0][0]))
    ranked = tuple(entry.get("ranked_mz") or entry.get("canonical_mz") or ())
    entry["base_peak"] = int(ranked[0]) if ranked and not entry.get("rank_unknown") else None

    samples = [text(item.get("sample")) for item in sightings if text(item.get("sample"))]
    retention_times = [value for value in
                       (numeric_value(item.get("rt")) for item in sightings)
                       if value is not None]
    concentrations = [value for value in
                      (numeric_value(item.get("conc_kg")) for item in sightings)
                      if value is not None]
    area_percentages = [value for value in
                        (numeric_value(item.get("area_pct")) for item in sightings)
                        if value is not None]
    dates = sorted(text(item.get("date")) for item in sightings if text(item.get("date")))

    entry["samples"] = sorted(set(samples))
    entry["n_sightings"] = len(sightings)
    entry["n_samples"] = len(entry["samples"])
    entry["rt_mean"] = round(statistics.fmean(retention_times), 4) if retention_times else None
    entry["rt_min"] = round(min(retention_times), 4) if retention_times else None
    entry["rt_max"] = round(max(retention_times), 4) if retention_times else None
    # A wide RT spread is the warning sign that one signature covers more than
    # one substance - it is shown in the register instead of being averaged away.
    entry["rt_sd"] = (round(statistics.stdev(retention_times), 4)
                      if len(retention_times) > 1 else 0.0 if retention_times else None)
    entry["conc_min"] = min(concentrations) if concentrations else None
    entry["conc_median"] = round(statistics.median(concentrations), 6) if concentrations else None
    entry["conc_max"] = max(concentrations) if concentrations else None
    entry["conc_max_sample"] = ""
    if concentrations:
        worst = max(sightings, key=lambda item: numeric_value(item.get("conc_kg")) or -1.0)
        entry["conc_max_sample"] = text(worst.get("sample"))
    entry["area_pct_max"] = max(area_percentages) if area_percentages else None
    entry["simulants"] = sorted({text(item.get("simulant")) for item in sightings
                                 if text(item.get("simulant"))})
    entry["materials"] = sorted({text(item.get("sample_name")) for item in sightings
                                 if text(item.get("sample_name"))})
    entry["first_seen"] = dates[0] if dates else ""
    entry["last_seen"] = dates[-1] if dates else ""

    # Toxicological relevance of an unidentified migrant follows the TTC bands.
    maximum = entry["conc_max"]
    if maximum is None:
        entry["ttc_flag"] = ""
    elif maximum > TTC_CRAMER_III_MG_KG:
        entry["ttc_flag"] = f"> {TTC_CRAMER_III_MG_KG} mg/kg (Cramer III)"
    elif maximum > TTC_ALERT_MG_KG:
        entry["ttc_flag"] = f"> {TTC_ALERT_MG_KG} mg/kg (Alert)"
    else:
        entry["ttc_flag"] = ""

    # Both hint functions run on the significant-ion subset; where the entry has
    # a measured spectrum the real intensities decide the base peak and the
    # subset, otherwise the rank model does.
    ranked, intensities = entry_ion_profile(entry)
    entry["class_hint"] = diagnostic_class_hints(ranked, intensities)
    entry["homologue_series"] = internal_homologue_series(ranked, intensities)
    return entry


def entry_ion_profile(entry: dict[str, Any]
                      ) -> tuple[tuple[int, ...], Optional[dict[int, float]]]:
    """Return ``(significant ions, measured intensities)`` of a register entry.

    Two paths, and both stay permanently: an entry written from the workspace
    carries a measured spectrum and is described by its real intensities; an
    entry migrated from the JSON register never gains one and keeps the rank
    model. The returned masses are already the significant-ion subset, which is
    what the homologue and overlap detectors have to be fed.
    """
    measured = measured_entry_spectrum(entry)
    if measured:
        ions = gc_register_module().significant_ions(measured)
        if ions:
            # significant_ions reports per-mille of the base peak; the rest of
            # this module works in per cent of the base peak.
            return (tuple(mz for mz, _, _ in ions),
                    {mz: rel / 10.0 for mz, rel, _ in ions})
    ranked = tuple(entry.get("ranked_mz") or entry.get("canonical_mz") or ())
    # A migrated unordered list has no intensity ranking to model or truncate.
    return (ranked if entry.get("rank_unknown") else significant_mz_subset(ranked)), None


def entry_ranked_mz(entry: dict[str, Any]) -> tuple[int, ...]:
    """Return the entry's ions, strongest first, as the subset to score on."""
    return entry_ion_profile(entry)[0]


def unknown_entry_number(entry: Optional[dict[str, Any]]) -> Optional[int]:
    """Return the number inside ``UNK-0042``, the key of every SQLite lookup.

    gc_register keeps ``entry_id`` as exactly that integer, so this is the one
    place that has to know how the two identifiers relate.
    """
    if not entry:
        return None
    digits = "".join(char for char in text(entry.get("id")) if char.isdigit())
    return int(digits) if digits else None


def spectrum_ion_profile(measured) -> tuple[tuple[int, ...], Optional[dict[int, float]]]:
    """``(significant ions, intensities in % of the base peak)`` of a spectrum.

    The half of :func:`entry_ion_profile` that does not touch the database, so
    the matching rule can be handed a spectrum instead of fetching one - which
    is what makes it testable without a register file.
    """
    if measured:
        ions = gc_register_module().significant_ions(measured)
        if ions:
            # significant_ions reports per-mille of the base peak; the rest of
            # this module works in per cent of the base peak.
            return (tuple(mz for mz, _, _ in ions),
                    {mz: rel / 10.0 for mz, rel, _ in ions})
    return (), None


def best_register_spectrum(module: Any, connection: Any,
                           entry_id: int) -> Optional[list[tuple[float, float]]]:
    """The ion-richest stored spectrum of one entry, on an open connection.

    Falls back to the most recent spectrum when the gc_register on this machine
    predates ``best_spectrum_for_entry``: the module is loaded from a path at
    run time, so the two files can be a version apart.
    """
    picker = getattr(module, "best_spectrum_for_entry", None) or \
        module.spectrum_for_entry
    return picker(connection, int(entry_id))


# Keyed by (database, modification time, entry number). The spectrum canvas
# redraws on every <Configure> event, so the database must not be reopened
# for each resize step.
_MEASURED_SPECTRUM_CACHE: dict[tuple[str, int, int],
                              Optional[list[tuple[float, float]]]] = {}

# Which entries have a spectrum at all, keyed by (database, modification time).
# Every migrated entry has none, so without this the similarity matrix would
# open the database once per entry only to learn that.
_SPECTRUM_INDEX_CACHE: dict[tuple[str, int], frozenset] = {}

# The spectra list of one entry - metadata only, no blobs. Keyed like the two
# caches above and therefore invalidated by the same register write.
_ENTRY_SPECTRA_CACHE: dict[tuple[str, int, int], list] = {}


def _entries_with_spectra(path: Path, stamp: int) -> frozenset:
    cache_key = (str(path), stamp)
    cached = _SPECTRUM_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
    known: frozenset = frozenset()
    try:
        register = gc_register_module()
        connection = register.connect(path, create=False)
        try:
            known = frozenset(register.entries_with_spectra(connection))
        finally:
            connection.close()
    except Exception:
        # A missing module or an unreadable register must never break the tab.
        known = frozenset()
    _SPECTRUM_INDEX_CACHE[cache_key] = known
    return known


def measured_entry_spectrum(
        entry: Optional[dict[str, Any]]) -> Optional[list[tuple[float, float]]]:
    """Return the measured spectrum of a register entry as [(m/z, % of base peak)].

    ``None`` means "draw the modelled rank spectrum instead": there is no
    SQLite register, or the entry carries no spectrum. Entries migrated from
    the JSON register never do (gc_register.spectrum_for_entry).
    """
    number = unknown_entry_number(entry)
    if number is None:
        return None
    path = unknown_register_db_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    cache_key = (str(path), stamp, number)
    if cache_key in _MEASURED_SPECTRUM_CACHE:
        return _MEASURED_SPECTRUM_CACHE[cache_key]
    if number not in _entries_with_spectra(path, stamp):
        _MEASURED_SPECTRUM_CACHE[cache_key] = None
        return None
    spectrum = None
    try:
        register = gc_register_module()
        connection = register.connect(path, create=False)
        try:
            spectrum = best_register_spectrum(register, connection, number)
        finally:
            connection.close()
    except Exception:
        # A missing module or an unreadable register must never break the tab.
        spectrum = None
    result: Optional[list[tuple[float, float]]] = None
    if spectrum:
        base_peak = max((float(intensity) for _, intensity in spectrum), default=0.0)
        if base_peak > 0:
            result = [(float(mass), float(intensity) * 100.0 / base_peak)
                      for mass, intensity in spectrum if float(intensity) > 0]
    _MEASURED_SPECTRUM_CACHE[cache_key] = result
    return result


def entry_spectrum_rows(entry: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every stored spectrum of one entry, newest first, without the blobs.

    What the register tab lists so the analyst can pick one. The spectrum itself
    is loaded only for the row that was picked (:func:`load_entry_spectrum`) -
    an entry with a hundred sightings must not decode a hundred spectra to fill
    a list in which exactly one of them is then looked at.
    """
    number = unknown_entry_number(entry)
    if number is None:
        return []
    path = unknown_register_db_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return []
    cache_key = (str(path), stamp, number)
    if cache_key in _ENTRY_SPECTRA_CACHE:
        return _ENTRY_SPECTRA_CACHE[cache_key]
    rows: list[dict[str, Any]] = []
    try:
        register = gc_register_module()
        connection = register.connect(path, create=False)
        try:
            rows = list(register.entry_spectra(connection, number))
        finally:
            connection.close()
    except Exception:
        # A missing module or an unreadable register must never break the tab.
        rows = []
    _ENTRY_SPECTRA_CACHE[cache_key] = rows
    return rows


def load_entry_spectrum(spectrum_id: Any) -> list[tuple[float, float]]:
    """One stored spectrum by id, as measured: ``[(m/z, intensity)]``."""
    try:
        register = gc_register_module()
        connection = register.connect(unknown_register_db_path(), create=False)
        try:
            return list(register.load_spectrum(connection, int(spectrum_id)))
        finally:
            connection.close()
    except Exception:
        return []


def as_relative_spectrum(spectrum) -> list[tuple[float, float]]:
    """Normalise a measured spectrum to per cent of its base peak."""
    base_peak = max((float(intensity) for _, intensity in spectrum or ()),
                    default=0.0)
    if base_peak <= 0:
        return []
    return [(float(mass), float(intensity) * 100.0 / base_peak)
            for mass, intensity in spectrum if float(intensity) > 0]


#: What ``spectra.kind`` means in German. The column takes any text; these are
#: the four values the workspace writes. Mirrors gc_unknowns.KIND_LABELS.
SPECTRUM_KIND_LABELS = {"measured": "gemessen", "component": "dekonvoluiert",
                        "apex": "roh (Apex)", "library": "Bibliothek"}


def best_spectrum_row_index(rows: list[dict[str, Any]]) -> int:
    """Index of the ion-richest row, the one the tab preselects.

    The same order gc_register.best_spectrum_ids applies, so the list, the
    drawing and the MSP library all agree on which spectrum represents an entry.
    """
    if not rows:
        return -1
    def rank(item: tuple[int, dict[str, Any]]):
        index, row = item
        return (int(row.get("n_ions") or 0),
                float(row.get("total_intensity") or 0.0),
                int(row.get("spectrum_id") or 0))
    return max(enumerate(rows), key=rank)[0]


def unknown_entry_similarity(first: dict[str, Any], second: dict[str, Any],
                             tolerance: int = 0) -> dict[str, Any]:
    """Rank recognition hints, not identity probabilities.

    Spectral/ion evidence forms the baseline. RT can attenuate it and sample
    overlap can boost it proportionally, but neither creates evidence when
    there are no shared ions. Unordered legacy ions carry no spectral score.
    """
    left, left_intensities = entry_ion_profile(first)
    right, right_intensities = entry_ion_profile(second)
    spectral = spectral_match_factor(left, right, tolerance,
                                     left_intensities, right_intensities)
    spectral_available = not ((first.get("rank_unknown") and left_intensities is None)
                              or (second.get("rank_unknown") and right_intensities is None))
    if not spectral_available:
        spectral = 0.0
    jaccard, shared_count, shared = ion_overlap(left, right, tolerance, reduce=False)
    retention = rt_proximity_score(first.get("rt_mean"), second.get("rt_mean"))
    cooccurrence = jaccard_index(set(first.get("samples") or ()),
                                 set(second.get("samples") or ()))

    components = {
        "spectral": spectral / 1000.0 if spectral_available else None,
        "ions": jaccard,
    }
    available = {key: value for key, value in components.items() if value is not None}
    weight_sum = sum(SIMILARITY_WEIGHTS[key] for key in available)
    total = (sum(SIMILARITY_WEIGHTS[key] * value for key, value in available.items())
             / weight_sum) if weight_sum else 0.0
    evidence = total
    if retention is not None:
        total *= 1.0 - SIMILARITY_WEIGHTS["rt"] * (1.0 - retention)
    if cooccurrence:
        total += evidence * (1.0 - total) * COOCCURRENCE_BONUS * cooccurrence
    total = max(0.0, min(1.0, total))

    base_peaks_match = bool(
        first.get("base_peak") is not None
        and first.get("base_peak") == second.get("base_peak")
    )
    delta_rt = None
    if first.get("rt_mean") is not None and second.get("rt_mean") is not None:
        delta_rt = round(abs(first["rt_mean"] - second["rt_mean"]), 4)
    return {
        "total": round(total, 4),
        "spectral": spectral,
        "spectral_available": spectral_available,
        "basis": ("Messintensitäten" if left_intensities is not None and right_intensities is not None
                  else "Rangmodell" if spectral_available else "Nur Ionen"),
        "ions_jaccard": round(jaccard, 4),
        "shared_ions": shared,
        "shared_count": shared_count,
        "rt_score": retention,
        "delta_rt": delta_rt,
        "cooccurrence": None if cooccurrence is None else round(cooccurrence, 4),
        "shared_samples": len(set(first.get("samples") or ())
                              & set(second.get("samples") or ())),
        "base_peak_match": base_peaks_match,
        "homologue": pairwise_homologue_relation(left, right),
    }


def rank_similar_unknowns(register: dict[str, Any], key: str, tolerance: int = 0,
                          minimum: float = 0.0) -> list[tuple[str, dict[str, Any]]]:
    """Return every other entry ranked by combined similarity to ``key``."""
    entries = register.get("unknowns") or {}
    reference = entries.get(key)
    if reference is None:
        return []
    scored = []
    for other_key, other in entries.items():
        if other_key == key:
            continue
        similarity = unknown_entry_similarity(reference, other, tolerance)
        if similarity["total"] >= minimum:
            scored.append((other_key, similarity))
    scored.sort(key=lambda item: -item[1]["total"])
    return scored


UNKNOWN_CLUSTER_THRESHOLD = 0.70


def compute_unknown_clusters(register: dict[str, Any],
                             threshold: float = UNKNOWN_CLUSTER_THRESHOLD) -> int:
    """Group related unknowns by single linkage and store the cluster on each entry.

    Single linkage is the right choice here: members of one homologous series
    form a chain, where each member resembles its neighbours but not the far end
    of the series.
    """
    entries = register.get("unknowns") or {}
    keys = sorted(entries)
    parent = {key: key for key in keys}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for index, left in enumerate(keys):
        for right in keys[index + 1:]:
            if find(left) == find(right):
                continue
            if unknown_entry_similarity(entries[left], entries[right])["total"] >= threshold:
                parent[find(right)] = find(left)

    groups: dict[str, list[str]] = {}
    for key in keys:
        groups.setdefault(find(key), []).append(key)

    # Only real groups get a number; a lone unknown is not a cluster.
    numbered = sorted((members for members in groups.values() if len(members) > 1),
                      key=lambda members: (-len(members), members[0]))
    for key in keys:
        entries[key]["cluster"] = ""
    for number, members in enumerate(numbered, start=1):
        for key in members:
            entries[key]["cluster"] = f"CL-{number:02d}"
    return len(numbered)


# A new sighting is reported against already solved unknowns from this score up.
UNKNOWN_RECOGNITION_THRESHOLD = 0.85


def unknown_report_context(sample: Any = "", sample_name: Any = "", report_type: str = "NIAS",
                           simulant: Any = "", temperature: Any = "", duration: Any = "",
                           migration_cell: Any = "", analyst: Any = "",
                           source_file: Any = "", output_file: Any = "") -> dict[str, Any]:
    """Collect the sample context both report paths attach to every sighting."""
    return {
        "sample": text(sample),
        "sample_name": text(sample_name),
        "report_type": report_type,
        "simulant": text(simulant),
        "temperature": text(temperature),
        "duration": text(duration),
        "migration_cell": text(migration_cell),
        "analyst": text(analyst),
        "source_file": text(source_file),
        "output_file": text(output_file),
        "date": datetime.now().strftime("%d.%m.%Y"),
        "script_version": SCRIPT_VERSION,
    }


def build_unknown_sighting(item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Turn one report row plus the sample context into a register sighting.

    Everything recorded here answers a question the analyst will later ask of the
    register: the retention time links sightings to each other, the concentration
    decides the toxicological relevance, and the migration conditions narrow down
    what kind of substance can be behind the signature.
    """
    ranked = unknown_mz_ranked(item.get("name"))
    sighting = dict(context)
    sighting.update({
        "ranked_mz": list(ranked or ()),
        "name_raw": text(item.get("name")),
        "rt": numeric_value(item.get("rt")),
        "conc_kg": numeric_value(item.get("conc_kg")),
        "conc_area": numeric_value(item.get("conc_area")),
        "area_pct": numeric_value(item.get("area_pct")),
        "match": numeric_value(item.get("match")),
        "db": text(item.get("db")),
    })
    return sighting


def adopt_legacy_register_if_present(register: dict[str, Any]) -> int:
    """Take over the old workbook once, while the JSON register is still empty.

    Existing installations carry their whole history in the three-column
    ``Unknown_Dokumentation.xlsx``. It is adopted on first use so nobody has to
    start over. Once the new export has written that file, it no longer matches
    the old layout and is never read again.
    """
    if register.get("unknowns"):
        return 0
    legacy = unknown_documentation_file_path()
    if not legacy.exists() or not is_legacy_unknown_documentation(legacy):
        return 0
    return import_legacy_unknown_documentation(legacy, register)["created"]


def load_unknown_register_adopting_legacy() -> dict[str, Any]:
    """Load the register, taking over an old workbook on the very first use."""
    module = gc_register_module()
    with unknown_register_connection() as connection:
        register = module.as_register_dict(connection)
        if register.get("unknowns"):
            return register
        # Re-read inside the write transaction: between the check above and the
        # adoption another client may have filled the register.
        with module.writing(connection):
            register = module.as_register_dict(connection)
            if not register.get("unknowns") and \
                    adopt_legacy_register_if_present(register):
                module.save_register_dict(connection, register)
        return register


def record_unknown_sightings(rows: list[dict[str, Any]],
                             context: dict[str, Any]) -> dict[str, Any]:
    """Add every unknown of one report run to the register and report matches.

    Only unknowns carrying m/z values are registered, exactly as before: a row
    without a fragment list has no identity to record. Re-running the same report
    updates its sightings instead of appending duplicates.
    """
    sightings = [build_unknown_sighting(item, context) for item in rows
                 if unknown_mz_ranked(item.get("name"))]
    if not sightings or not text(context.get("sample")):
        return {"recorded": 0, "new": 0, "hints": [], "path": None,
                "clusters": 0, "splits": [], "split": 0}

    module = gc_register_module()
    # One report run is one BEGIN IMMEDIATE. That single transaction is the
    # whole concurrency story now: SQLite serialises the writers and waits up to
    # busy_timeout for a busy share, which is what the old lock file did badly.
    with unknown_register_connection(write=True) as connection:
        with module.writing(connection):
            register = module.as_register_dict(connection)
            adopt_legacy_register_if_present(register)
            entries = register["unknowns"]
            # Two indexes, and both hold *lists*: since a signature can be split
            # across several entries (one per retention time), first-wins would
            # make every entry after the first unreachable, and each further
            # sighting of that isomer would fork yet another entry.
            by_canonical: dict[str, list[str]] = {}
            by_identity: dict[str, list[str]] = {}
            for key, entry in sorted(entries.items()):
                by_canonical.setdefault(
                    canonical_mz_key(entry.get("canonical_mz") or ()), []
                ).append(key)
                identity = module.entry_identity_key(entry)
                if identity:
                    by_identity.setdefault(identity, []).append(key)
            touched: list[str] = []
            splits: list[dict[str, Any]] = []
            created = 0
            spectrum_of = register_spectrum_lookup(module, connection)
            for sighting in sightings:
                ranked = tuple(sighting["ranked_mz"])
                canonical = canonical_mz_key(ranked)
                # Both ways a signature can point at an existing entry: its own
                # canonical masses, and the identity key of the strongest four,
                # which catches a signature differing only in its weak ions.
                # The canonical matches come first, so a tie falls to them.
                identity = unknown_identity_key(ranked)
                names: list[str] = list(by_canonical.get(canonical) or ())
                for name in (by_identity.get(identity) or ()) if identity else ():
                    if name not in names:
                        names.append(name)
                candidates = [(name, entries[name]) for name in names
                              if name in entries]

                key, entry, rejected = choose_unknown_entry(
                    sighting, candidates, spectrum_of)
                if entry is None:
                    key = free_register_key(entries, canonical)
                    entry = _new_unknown_entry(register, ranked)
                    entries[key] = entry
                    by_canonical.setdefault(canonical, []).append(key)
                    new_identity = module.entry_identity_key(entry)
                    if new_identity:
                        by_identity.setdefault(new_identity, []).append(key)
                    created += 1
                    if rejected is not None:
                        # Only a *rejected* candidate is a split. A signature
                        # nobody has seen before is an ordinary new entry and
                        # must not be reported as one.
                        _, candidate, decision = rejected
                        splits.append({
                            "id": entry["id"],
                            "from": text(candidate.get("id")),
                            "mz": format_mz_list(ranked),
                            "rt": numeric_value(sighting.get("rt")),
                            "delta": decision["delta"],
                            "match": decision["match"],
                            "reason": decision["reason"],
                        })
                if entry.get("rank_unknown"):
                    # The legacy register lost the ranking; the first real
                    # sighting restores it.
                    entry["rank_unknown"] = False
                    entry["ranked_mz"] = list(ranked)
                variants = entry.setdefault("ranked_variants", {})
                written = format_mz_list(ranked)
                variants[written] = int(variants.get(written, 0)) + 1

                existing = entry.setdefault("sightings", [])
                key_of_new = _sighting_key(sighting)
                for index, previous in enumerate(existing):
                    if _sighting_key(previous) == key_of_new:
                        existing[index] = sighting
                        break
                else:
                    existing.append(sighting)
                touched.append(key)

            for key in set(touched):
                recompute_unknown_entry(entries[key])
            clusters = compute_unknown_clusters(register)
            hints = collect_unknown_recognition_hints(register, set(touched))
            module.save_register_dict(connection, register)
        path = unknown_register_db_path()

    return {
        "recorded": len(sightings),
        "new": created,
        "hints": hints,
        "path": path,
        "clusters": clusters,
        "splits": splits,
        "split": len(splits),
    }


def safe_record_unknown_sightings(rows: list[dict[str, Any]],
                                  context: dict[str, Any]) -> dict[str, Any]:
    """Record sightings without ever failing the report that produced them.

    A busy or unreachable shared register - or one whose JSON has not been
    migrated yet - is a problem worth reporting, but it must not discard a
    finished evaluation. The error is handed back with the result and shown in
    the result window, where the migration message names the command to run.
    """
    try:
        return record_unknown_sightings(rows, context)
    except Exception as exc:  # noqa: BLE001 - surfaced to the analyst, not swallowed
        return {"recorded": 0, "new": 0, "hints": [], "path": None,
                "clusters": 0, "splits": [], "split": 0, "error": str(exc)}


def collect_unknown_recognition_hints(register: dict[str, Any],
                                      keys: set) -> list[dict[str, Any]]:
    """Report freshly seen unknowns that resemble an already identified one.

    This is what the register is for: an unknown clarified once should clarify
    itself the next time it turns up.
    """
    entries = register.get("unknowns") or {}
    hints = []
    for key in sorted(keys):
        entry = entries.get(key)
        if entry is None or text(entry.get("status")) == "identifiziert":
            continue
        for other_key, similarity in rank_similar_unknowns(
                register, key, minimum=UNKNOWN_RECOGNITION_THRESHOLD):
            other = entries[other_key]
            if text(other.get("status")) != "identifiziert":
                continue
            hints.append({
                "id": entry["id"],
                "mz": format_mz_list(entry_ranked_mz(entry)),
                "match_id": other["id"],
                "match_name": text(other.get("assigned_name")) or other["label"],
                "match_cas": text(other.get("assigned_cas")),
                "score": similarity["total"],
            })
            break
    return hints


def parse_unknown_query(query: Any) -> dict[str, Any]:
    """Translate one search line into a filter description.

    Supported: bare masses and ``91/123/172`` (must contain all ions),
    ``bp:91``, ``-149`` (must not contain), ``rt:12.34`` or
    ``rt:12.0-12.6``, ``syn:12345678``, ``status:offen``, ``n>=3``, ``conc>0.1``,
    ``klasse:siloxan`` and free text over id, assigned name and note.
    """
    parsed: dict[str, Any] = {
        "signature": None, "require": set(), "exclude": set(), "base_peak": None,
        "rt": None, "sample": "", "status": "", "min_sightings": None,
        "min_conc": None, "class_hint": "", "free_text": "",
    }
    free_words: list[str] = []
    for token in re.split(r"[\s,;]+", re.sub(r"\s*/\s*", "/", text(query))):
        if not token:
            continue
        lowered = token.casefold()
        signature_match = re.fullmatch(r"\d+(?:/\d+)+", token)
        if signature_match:
            parsed["signature"] = parse_mz_list(token)
            parsed["require"].update(parsed["signature"])
            continue
        if lowered.startswith(("bp:", "basepeak:", "basispeak:")):
            masses = parse_mz_list(token.split(":", 1)[1])
            if masses:
                parsed["base_peak"] = masses[0]
            continue
        if lowered.startswith("rt:"):
            value = token.split(":", 1)[1].replace(",", ".")
            span = re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", value)
            if span:
                parsed["rt"] = (float(span.group(1)), float(span.group(2)))
            elif re.fullmatch(r"\d+(?:\.\d+)?", value):
                center = float(value)
                parsed["rt"] = (center - UNKNOWN_RT_TOLERANCE,
                                center + UNKNOWN_RT_TOLERANCE)
            continue
        if lowered.startswith(("syn:", "probe:", "sample:")):
            parsed["sample"] = token.split(":", 1)[1]
            continue
        if lowered.startswith("status:"):
            parsed["status"] = token.split(":", 1)[1]
            continue
        if lowered.startswith(("klasse:", "class:")):
            parsed["class_hint"] = token.split(":", 1)[1]
            continue
        count_match = re.fullmatch(r"n\s*>=?\s*(\d+)", lowered)
        if count_match:
            threshold = int(count_match.group(1))
            parsed["min_sightings"] = threshold if ">=" in lowered else threshold + 1
            continue
        conc_match = re.fullmatch(r"conc\s*>=?\s*(\d+(?:[.,]\d+)?)", lowered)
        if conc_match:
            parsed["min_conc"] = float(conc_match.group(1).replace(",", "."))
            continue
        if re.fullmatch(r"-\d+", token):
            parsed["exclude"].add(int(token[1:]))
            continue
        if re.fullmatch(r"\+?\d+", token):
            parsed["require"].add(int(token.lstrip("+")))
            continue
        free_words.append(token)
    parsed["free_text"] = " ".join(free_words)
    return parsed


def _contains_mass(masses: set, wanted: int, tolerance: int) -> bool:
    return any(abs(mass - wanted) <= tolerance for mass in masses)


def search_unknown_register(register: dict[str, Any], query: Any,
                            tolerance: int = 0) -> list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]]:
    """Return the matching entries, ranked by similarity for signature queries."""
    parsed = parse_unknown_query(query)
    entries = register.get("unknowns") or {}
    results: list[tuple[str, dict[str, Any], Optional[dict[str, Any]]]] = []
    reference = None
    if parsed["signature"]:
        reference = {"ranked_mz": list(parsed["signature"]), "samples": [],
                     "rt_mean": None, "base_peak": parsed["signature"][0]}

    for key, entry in entries.items():
        masses = set(entry.get("canonical_mz") or ()) | set(entry_ranked_mz(entry))
        if parsed["require"] and len(align_ions(parsed["require"], masses, tolerance)) != len(parsed["require"]):
            continue
        if parsed["exclude"] and any(
                _contains_mass(masses, wanted, tolerance) for wanted in parsed["exclude"]):
            continue
        if parsed["base_peak"] is not None and entry.get("base_peak") != parsed["base_peak"]:
            continue
        if parsed["rt"] is not None:
            mean = entry.get("rt_mean")
            if mean is None or not parsed["rt"][0] <= mean <= parsed["rt"][1]:
                continue
        if parsed["sample"] and not any(
                parsed["sample"].casefold() in sample.casefold()
                for sample in entry.get("samples") or ()):
            continue
        if parsed["status"] and parsed["status"].casefold() not in text(entry.get("status")).casefold():
            continue
        if parsed["min_sightings"] is not None and int(entry.get("n_sightings") or 0) < parsed["min_sightings"]:
            continue
        if parsed["min_conc"] is not None:
            maximum = entry.get("conc_max")
            if maximum is None or maximum <= parsed["min_conc"]:
                continue
        if parsed["class_hint"] and not any(
                parsed["class_hint"].casefold() in hint.casefold()
                for hint in entry.get("class_hint") or ()):
            continue
        if parsed["free_text"]:
            haystack = " ".join((
                entry.get("id", ""), entry.get("label", ""),
                text(entry.get("assigned_name")), text(entry.get("assigned_cas")),
                text(entry.get("note")), text(entry.get("cluster")),
                " ".join(" ".join(str(r.get(k) or '') for k in ('candidate_name','candidate_cas','note','next_steps'))
                         for r in entry.get('ei_investigations') or []),
            )).casefold()
            if not all(word.casefold() in haystack for word in parsed["free_text"].split()):
                continue
        similarity = (unknown_entry_similarity(reference, entry, tolerance)
                      if reference else None)
        results.append((key, entry, similarity))

    if reference:
        # Rank only entries that passed every required-ion filter above.
        results.sort(key=lambda item: -item[2]["total"])
    else:
        results.sort(key=lambda item: (-(item[1].get("n_sightings") or 0), item[1].get("id", "")))
    return results


# --------------------------------------------------------------------------
# The register as a NIST library
# --------------------------------------------------------------------------
#
# The register knows what a substance looks like; NIST knows what substances
# look like. Until these two files existed, bringing the two together meant
# exporting one spectrum at a time by hand. They are rewritten from the register
# whenever the register tab notices they have gone stale, so the library is a
# view of the register and never a second copy of it that can drift.


def unknown_msp_name(entry: dict[str, Any], modelled: bool = False) -> str:
    """What NIST shows in its hit list for one entry.

    The register ID always comes first: a hit is worthless if the analyst then
    has to guess which entry it was. The assigned name follows where there is
    one, the mass signature where there is not.
    """
    identifier = text(entry.get("id")) or "UNK"
    tag = " [modelliert]" if modelled else ""
    name = text(entry.get("assigned_name"))
    if name:
        return f"{identifier}{tag} {name}"
    masses = format_mz_list(entry.get("ranked_mz") or entry.get("canonical_mz") or ())
    return f"{identifier}{tag} (m/z {masses})" if masses else f"{identifier}{tag}"


def unknown_msp_comment(entry: dict[str, Any],
                        spectrum_row: Optional[dict[str, Any]] = None,
                        modelled: bool = False) -> str:
    """The Comment line: where this spectrum comes from and what is known."""
    parts = [text(entry.get("id")) or "UNK"]
    if modelled:
        parts.append("modelliert aus der m/z-Rangfolge, keine gemessenen "
                     "Intensitäten")
    if entry.get("rt_mean") is not None:
        parts.append(f"RT {format_de(entry.get('rt_mean'), 3)} min")
    sightings = int(entry.get("n_sightings") or 0)
    if sightings:
        parts.append(f"{sightings} Sichtungen")
    status = text(entry.get("status"))
    if status:
        parts.append(f"Status {status}")
    if spectrum_row:
        sample = text(spectrum_row.get("sample") or spectrum_row.get("sample_name"))
        created = text(spectrum_row.get("created_at"))[:10]
        origin = "bestes Spektrum"
        if sample:
            origin += f" aus Probe {sample}"
        if created:
            origin += f" vom {created}"
        parts.append(origin)
        if spectrum_row.get("n_ions"):
            parts.append(f"{int(spectrum_row['n_ions'])} Ionen")
    return "; ".join(parts)


def modelled_unknown_spectrum(entry: dict[str, Any]) -> list[tuple[float, int]]:
    """The rank pseudo spectrum: ``[(m/z, round(999 / rank))]``, strongest first.

    Empty when the ranking is not known: an entry whose order the legacy JSON
    register lost (``rank_unknown``) would otherwise contribute a spectrum whose
    intensities are invented rather than modelled.
    """
    if entry.get("rank_unknown"):
        return []
    ranked = [int(mass) for mass in (entry.get("ranked_mz") or ())]
    return [(float(mass), int(round(UNKNOWN_MSP_MODELLED_BASE / rank)))
            for rank, mass in enumerate(ranked, 1)]


def _best_register_spectra() -> dict[int, tuple[list, Optional[dict[str, Any]]]]:
    """``{entry number: (spectrum, its row)}`` for every entry that has one.

    One pass over the register: the library needs the best spectrum of every
    entry at once, and reopening the database per entry over a share is what
    makes the difference between a rebuild nobody notices and one that stalls
    the tab.
    """
    module = gc_register_module()
    path = unknown_register_db_path()
    found: dict[int, tuple[list, Optional[dict[str, Any]]]] = {}
    connection = module.connect(path, create=False)
    try:
        picker = getattr(module, "best_spectrum_ids", None)
        if picker is not None:
            chosen = picker(connection)
        else:  # an older gc_register beside a newer script
            chosen = {number: None for number in
                      module.entries_with_spectra(connection)}
        for number, spectrum_id in chosen.items():
            if spectrum_id is None:
                spectrum = module.spectrum_for_entry(connection, number)
                row = None
            else:
                spectrum = module.load_spectrum(connection, spectrum_id)
                row = next((item for item in module.entry_spectra(connection, number)
                            if int(item.get("spectrum_id") or 0) == int(spectrum_id)),
                           None)
            if spectrum:
                found[int(number)] = (spectrum, row)
    finally:
        connection.close()
    return found


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write a file the way the register is written: temp file, then replace.

    A half-written library on a shared drive is worse than none: NIST would load
    it without complaint.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent),
                                         prefix=f".{path.stem}_", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_unknown_msp_libraries(register: Optional[dict[str, Any]] = None,
                                directory: Optional[Path] = None) -> dict[str, Any]:
    """Rewrite both libraries and their stamp. Returns the tally.

    The stamp is written last and on purpose: if the run breaks off, the stamp
    is missing or stale and the next call rebuilds. A half-written state cannot
    present itself as finished.
    """
    paths = unknown_msp_library_paths(directory)
    result: dict[str, Any] = {"measured": 0, "modelled": 0, "skipped": 0,
                              "paths": paths, "written_at": "", "error": None}
    try:
        nist = gc_nist_module()
        if register is None:
            register = load_unknown_register_db()
        entries = register.get("unknowns") or {}
        try:
            best = _best_register_spectra()
        except Exception:  # noqa: BLE001 - no spectra is a valid register state
            best = {}

        measured_records: list[str] = []
        modelled_records: list[str] = []
        skipped = 0
        for _, entry in sorted(entries.items()):
            if text(entry.get("status")) in UNKNOWN_MSP_SKIP_STATUS:
                continue
            number = unknown_entry_number(entry)
            found = best.get(number) if number is not None else None
            if found and found[0]:
                spectrum, row = found
                measured_records.append(nist.msp_text(
                    spectrum, unknown_msp_name(entry), None,
                    comment=unknown_msp_comment(entry, row),
                    cas=text(entry.get("assigned_cas"))))
                continue
            model = modelled_unknown_spectrum(entry)
            if not model:
                skipped += 1
                continue
            modelled_records.append(nist.msp_text(
                model, unknown_msp_name(entry, modelled=True), None,
                comment=unknown_msp_comment(entry, None, modelled=True),
                cas=text(entry.get("assigned_cas"))))

        for kind, records in (("measured", measured_records),
                              ("modelled", modelled_records)):
            payload = nist.NIST_NEWLINE.join(records)
            _write_atomic(paths[kind],
                          payload.encode(nist.NIST_ENCODING, "replace"))
        result.update(measured=len(measured_records),
                      modelled=len(modelled_records), skipped=skipped,
                      written_at=datetime.now().isoformat(timespec="seconds"))
        _write_atomic(paths["stamp"], json.dumps(
            dict(_register_file_state(), entries=len(entries),
                 measured=result["measured"], modelled=result["modelled"],
                 skipped=skipped, written_at=result["written_at"],
                 script_version=SCRIPT_VERSION),
            ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the tab
        result["error"] = str(exc)
    return result


def _register_file_state() -> dict[str, Any]:
    """Size and modification time of the register database, or zeros.

    What the stamp compares against. Deliberately not a comparison of the two
    files' own timestamps: on a share the writing machine sets the mtime, and
    two clocks must not decide whether the library is current.
    """
    try:
        stat = unknown_register_db_path().stat()
        return {"db_mtime_ns": stat.st_mtime_ns, "db_size": stat.st_size}
    except OSError:
        return {"db_mtime_ns": 0, "db_size": 0}


def unknown_msp_stamp(directory: Optional[Path] = None) -> dict[str, Any]:
    """The stamp of the written libraries; ``{}`` when there is none to read."""
    try:
        return json.loads(unknown_msp_library_paths(directory)["stamp"]
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def unknown_msp_is_stale(directory: Optional[Path] = None) -> bool:
    """Do the libraries no longer describe the register as it stands now?"""
    paths = unknown_msp_library_paths(directory)
    if not all(path.exists() for path in paths.values()):
        return True
    stamp = unknown_msp_stamp(directory)
    state = _register_file_state()
    return (stamp.get("db_mtime_ns") != state["db_mtime_ns"]
            or stamp.get("db_size") != state["db_size"])


def refresh_unknown_msp_libraries(force: bool = False,
                                  directory: Optional[Path] = None
                                  ) -> dict[str, Any]:
    """Rewrite the libraries if they have gone stale. Never raises.

    Called from one place only, when the register tab reads the register, so
    the libraries also pick up spectra the GC workspace filed in the meantime.
    A report run writes no library.
    """
    if not force and not unknown_msp_is_stale(directory):
        stamp = unknown_msp_stamp(directory)
        return {"measured": int(stamp.get("measured") or 0),
                "modelled": int(stamp.get("modelled") or 0),
                "skipped": int(stamp.get("skipped") or 0),
                "paths": unknown_msp_library_paths(directory),
                "written_at": text(stamp.get("written_at")),
                "error": None, "rebuilt": False}
    result = write_unknown_msp_libraries(directory=directory)
    result["rebuilt"] = result.get("error") is None
    return result


UNKNOWN_MASTER_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("id", "Unknown-ID", 12),
    ("_ranked", "m/z (Rangfolge)", 24),
    ("base_peak", "Base Peak", 11),
    ("_canonical", "m/z sortiert", 24),
    ("_ions", "Ionen", 8),
    ("n_sightings", "Sichtungen", 11),
    ("n_samples", "Proben", 9),
    ("rt_mean", "RT Mittel [min]", 15),
    ("rt_sd", "RT SD [min]", 12),
    ("rt_min", "RT min [min]", 13),
    ("rt_max", "RT max [min]", 13),
    ("conc_min", "Konz. min [mg/kg]", 17),
    ("conc_median", "Konz. Median [mg/kg]", 20),
    ("conc_max", "Konz. max [mg/kg]", 17),
    ("conc_max_sample", "Maximum in Probe", 18),
    ("area_pct_max", "Area % max", 12),
    ("ttc_flag", "TTC-Relevanz", 24),
    ("_simulants", "Simulanzien", 30),
    ("_materials", "Materialien", 34),
    ("first_seen", "Erstmals", 12),
    ("last_seen", "Zuletzt", 12),
    ("_class_hint", "Klassen-Hinweis (Verdacht)", 34),
    ("homologue_series", "Homologe Serie", 18),
    ("cluster", "Cluster", 10),
    ("status", "Status", 14),
    ("assigned_name", "Zugeordneter Name", 34),
    ("assigned_cas", "CAS-No.", 14),
    ("note", "Notiz", 40),
    ("_samples", "Proben (Liste)", 44),
)

UNKNOWN_SIGHTING_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("_id", "Unknown-ID", 12),
    ("_ranked", "m/z (Rangfolge)", 24),
    ("sample", "Probe", 14),
    ("sample_name", "Probenname", 34),
    ("date", "Datum", 12),
    ("rt", "RT [min]", 11),
    ("conc_kg", "Konz. [mg/kg]", 14),
    ("conc_area", "Konz. [mg/dm²]", 15),
    ("area_pct", "Area %", 10),
    ("match", "% match", 9),
    ("db", "DB", 12),
    ("simulant", "Simulans", 18),
    ("temperature", "Temperatur", 12),
    ("duration", "Dauer", 12),
    ("migration_cell", "Migrationszelle", 18),
    ("analyst", "Auswerter", 16),
    ("report_type", "Report-Typ", 18),
    ("name_raw", "Originaltext", 30),
    ("source_file", "Quelldatei", 50),
    ("script_version", "Skriptversion", 13),
)


def _write_register_sheet(worksheet, columns, rows) -> None:
    """Write one export sheet with the header formatting used elsewhere."""
    worksheet.append([header for _, header, _ in columns])
    for column, (_, _, width) in enumerate(columns, start=1):
        worksheet.cell(1, column).font = Font(bold=True)
        worksheet.column_dimensions[get_column_letter(column)].width = width
    for row in rows:
        worksheet.append(row)
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(worksheet.max_row, 1)}"


def export_unknown_register_excel(register: Optional[dict[str, Any]] = None,
                                  path: Optional[Path] = None) -> Path:
    """Write the register to Excel: master data, sightings and clusters.

    The sightings sheet holds one row per occurrence, which is what makes pivot
    evaluations possible - the previous register packed every sample number of an
    unknown into a single comma separated cell.

    Reads through ``as_register_dict``, which serves the legacy nested shape out
    of SQLite, so the sheet layout below did not have to change with the cutover.
    """
    register = register if register is not None else load_unknown_register_db()
    path = path or unknown_documentation_file_path()
    entries = register.get("unknowns") or {}
    ordered = sorted(entries.values(), key=lambda entry: entry.get("id", ""))

    workbook = Workbook()
    master = workbook.active
    master.title = "Unknown Stammdaten"
    master_rows = []
    for entry in ordered:
        ranked = entry_ranked_mz(entry)
        derived = {
            "_ranked": format_mz_list(ranked) + (" (Rang unbekannt)"
                                                 if entry.get("rank_unknown") else ""),
            "_canonical": format_mz_list(entry.get("canonical_mz") or ()),
            "_ions": len(entry.get("canonical_mz") or ()),
            "_simulants": ", ".join(entry.get("simulants") or ()),
            "_materials": ", ".join(entry.get("materials") or ()),
            "_class_hint": ", ".join(entry.get("class_hint") or ()),
            "_samples": ", ".join(entry.get("samples") or ()),
        }
        master_rows.append([derived.get(key, entry.get(key, ""))
                            for key, _, _ in UNKNOWN_MASTER_COLUMNS])
    _write_register_sheet(master, UNKNOWN_MASTER_COLUMNS, master_rows)

    sighting_rows = []
    for entry in ordered:
        for sighting in entry.get("sightings") or ():
            derived = {
                "_id": entry.get("id", ""),
                "_ranked": format_mz_list(sighting.get("ranked_mz") or ()),
            }
            sighting_rows.append([derived.get(key, sighting.get(key, ""))
                                  for key, _, _ in UNKNOWN_SIGHTING_COLUMNS])
    sighting_rows.sort(key=lambda row: (text(row[2]), text(row[0])))
    _write_register_sheet(workbook.create_sheet("Sichtungen"),
                          UNKNOWN_SIGHTING_COLUMNS, sighting_rows)

    clusters: dict[str, list[dict[str, Any]]] = {}
    for entry in ordered:
        name = text(entry.get("cluster"))
        if name:
            clusters.setdefault(name, []).append(entry)
    cluster_columns = (
        ("cluster", "Cluster", 10), ("members", "Unknowns", 40), ("count", "Anzahl", 9),
        ("ions", "Gemeinsame Ionen", 24), ("rt", "RT Mittel [min]", 15),
        ("hint", "Klassen-Hinweis (Verdacht)", 34), ("series", "Homologe Serie", 18),
    )
    cluster_rows = []
    for name in sorted(clusters):
        members = clusters[name]
        shared = set(members[0].get("canonical_mz") or ())
        for entry in members[1:]:
            shared &= set(entry.get("canonical_mz") or ())
        retention = [entry["rt_mean"] for entry in members if entry.get("rt_mean") is not None]
        hints = sorted({hint for entry in members for hint in entry.get("class_hint") or ()})
        series = sorted({text(entry.get("homologue_series")) for entry in members
                         if text(entry.get("homologue_series"))})
        cluster_rows.append([
            name, ", ".join(entry.get("id", "") for entry in members), len(members),
            format_mz_list(sorted(shared)),
            round(statistics.fmean(retention), 4) if retention else "",
            ", ".join(hints), ", ".join(series),
        ])
    _write_register_sheet(workbook.create_sheet("Cluster"), cluster_columns, cluster_rows)

    research_columns = tuple((key, label, width) for key, label, width in (
        ('id','Unknown',14), ('created_at','Zeitpunkt',26), ('decision','Kandidatenbewertung',22),
        ('candidate_name','Kandidat (unbestätigt)',45), ('candidate_cas','Kandidaten-CAS',18),
        ('score','EI Atlas / 100',18), ('note','Beobachtungen',60), ('next_steps','Nächste Schritte',60),
        ('investigation_id','Untersuchungs-ID',36)))
    research_rows = []
    for entry in ordered:
        for record in entry.get('ei_investigations') or []:
            payload = json.loads(record['payload_json'])
            row = dict(record, id=entry.get('id'), score=(payload.get('candidate') or {}).get('score'))
            research_rows.append([row.get(key, '') for key, _, _ in research_columns])
    if research_rows:
        _write_register_sheet(workbook.create_sheet('EI Untersuchungen'), research_columns, research_rows)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def open_unknown_documentation() -> None:
    """Open the Excel export of the unknown register, refreshing it first.

    The export is written under a fresh name whenever the target file is locked
    by an open Excel window, which used to surface as ``PermissionError 13``.
    """
    path = unknown_documentation_file_path()
    try:
        path = export_unknown_register_excel()
    except PermissionError:
        if not path.exists():
            raise
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        import subprocess
        subprocess.Popen(["xdg-open", str(path)])


LEGACY_REGISTER_HEADERS = ("unknownsubstanz", "synerisnummer", "datum")


def is_legacy_unknown_documentation(path: Path) -> bool:
    """Recognise the three-column register written by earlier versions."""
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return False
    try:
        worksheet = workbook[workbook.sheetnames[0]]
        headers = tuple(normalized_header(worksheet.cell(1, column).value)
                        for column in (1, 2, 3))
    finally:
        workbook.close()
    return headers[:2] == LEGACY_REGISTER_HEADERS[:2]


def import_legacy_unknown_documentation(path: Path,
                                        register: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Take over the old three-column register.

    The previous version stored the m/z values sorted, so the intensity ranking -
    and with it the base peak - is not recoverable. Imported entries are marked
    ``rank_unknown`` instead of inventing an order: they take part in ion overlap
    and co-occurrence, but not in base-peak matching, until a real sighting
    restores the ranking.

    The caller normally passes the register it is holding open inside a write
    transaction; the default is only for a standalone import.
    """
    register = register if register is not None else load_unknown_register_db()
    entries = register["unknowns"]
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook[workbook.sheetnames[0]]
        imported = created = 0
        for row in worksheet.iter_rows(min_row=2, max_col=3, values_only=True):
            label = text(row[0] if row else "")
            masses = unknown_mz_signature(label) or parse_mz_list(label)
            if not masses:
                continue
            key = canonical_mz_key(masses)
            entry = entries.get(key)
            if entry is None:
                entry = _new_unknown_entry(register, tuple(sorted(masses)))
                entry["rank_unknown"] = True
                entry["note"] = "Aus dem alten Register übernommen; Rangfolge unbekannt."
                entries[key] = entry
                created += 1
            samples = _split_register_list(row[1] if len(row) > 1 else "")
            dates = _split_register_list(row[2] if len(row) > 2 else "")
            dates = (dates + [""] * len(samples))[:len(samples)]
            existing = entry.setdefault("sightings", [])
            known = {_sighting_key(item) for item in existing}
            for sample, date in zip(samples, dates):
                sighting = unknown_report_context(
                    sample=sample, report_type="Altregister")
                sighting["date"] = date or sighting["date"]
                sighting["ranked_mz"] = list(entry["canonical_mz"])
                sighting["name_raw"] = label
                sighting["rt"] = None
                if _sighting_key(sighting) in known:
                    continue
                existing.append(sighting)
                known.add(_sighting_key(sighting))
                imported += 1
            recompute_unknown_entry(entry)
    finally:
        workbook.close()
    compute_unknown_clusters(register)
    return {"register": register, "imported": imported, "created": created}


def file_sha256(path: Path) -> str:
    """Return a reproducible SHA-256 fingerprint for the reference database."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_version_text(path: Path) -> str:
    modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return f"{path.name} | {modified:%Y-%m-%d %H:%M:%S %Z}"


def is_unidentified_item(item: dict[str, Any]) -> bool:
    """Return True for every unresolved substance representation used by reports."""
    name = text(item.get("name"))
    normalized = name.casefold()
    return bool(
        item.get("unknown_summary")
        or item.get("name_lookup_failed")
        or not name
        or normalized.startswith("unknown")
        or normalized in {
            PUBCHEM_NO_HIT_TEXT.casefold(),
            "unidentified",
            "nicht identifiziert",
            "no hit found",
        }
    )


def unidentified_register_name(item: dict[str, Any]) -> str:
    """Return a stable, non-empty label for the unknown register."""
    name = text(item.get("name"))
    if name:
        signature = unknown_mz_signature(name)
        if signature:
            return "unknown (m/z " + "/".join(map(str, signature)) + ")"
        return name
    rt = numeric_value(item.get("rt"))
    return f"unknown (RT {rt:.4f} min)" if rt is not None else "unknown"


def report_status(item: dict[str, Any]) -> tuple[str, str, str]:
    """Return status text, fill color and font color for the four agreed states."""
    if is_unidentified_item(item):
        return "⚪ Substanz nicht identifiziert", "E7E6E6", "404040"
    # All calculated sum rows require an assessment because no substance-specific
    # migration limit can be assigned to the aggregate.
    if item.get("summary") and not item.get("substance_summary"):
        return "🟡 Kein SML vorhanden – Bewertung erforderlich", "FFF2CC", "7F6000"
    limit = item.get("sml")
    if limit is None:
        return "🟡 Kein SML vorhanden – Bewertung erforderlich", "FFF2CC", "7F6000"
    if item.get("conc_kg") is not None and item["conc_kg"] > limit:
        return "🔴 SML überschritten", "F4CCCC", "9C0006"
    return "🟢 SML eingehalten", "D9EAD3", "2E603A"


def pubchem_preferred_name(
    cas_number: str,
    cache: dict[str, Optional[str]],
    checked_at: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Retrieve PubChem's preferred compound title for a CAS number.

    Returns None only when PubChem explicitly provides no compound/title. Network,
    throttling and service errors raise RuntimeError so they are never reported as
    a false "Kein Treffer gefunden" result.
    """
    normalized = normalize_cas(cas_number)
    if normalized in cache:
        return cache[normalized]

    encoded_cas = quote(normalized, safe="")
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
        f"{encoded_cas}/property/Title/JSON"
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "NIAS-Screening-Processor/18 (PubChem PUG-REST lookup)",
        },
    )

    last_error: Optional[BaseException] = None
    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.8 * attempt)
        # PubChem asks clients to stay below five requests per second.
        time.sleep(PUBCHEM_REQUEST_INTERVAL_SECONDS)
        try:
            with urlopen(request, timeout=PUBCHEM_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            properties = payload.get("PropertyTable", {}).get("Properties", [])
            preferred = text(properties[0].get("Title")) if properties else ""
            cache[normalized] = preferred or None
            if checked_at is not None:
                checked_at[normalized] = datetime.now().astimezone().isoformat(timespec="seconds")
            return cache[normalized]
        except HTTPError as exc:
            if exc.code in {400, 404}:
                cache[normalized] = None
                if checked_at is not None:
                    checked_at[normalized] = datetime.now().astimezone().isoformat(timespec="seconds")
                return None
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                break
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc

    raise RuntimeError(
        f"PubChem konnte für CAS {normalized} nicht zuverlässig abgefragt werden. "
        "Die Verarbeitung wurde abgebrochen, damit kein fehlender API-Zugriff "
        "fälschlich als 'Kein Treffer gefunden' gespeichert wird. "
        f"Technische Ursache: {last_error}"
    ) from last_error


def numeric_value(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) else number

    cleaned = text(value).replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    return float(match.group()) if match else None


def is_excel_error(value: Any) -> bool:
    """Return True for cached Excel error values such as #DIV/0! or #VALUE!."""
    return isinstance(value, str) and value.strip().startswith("#")


def recalculate_workbook_with_excel(source_path: Path) -> Path:
    """Recalculate an Excel workbook in a temporary folder using desktop Excel.

    openpyxl does not calculate formulas. If the source file contains missing or
    stale cached formula results, desktop Excel is used to calculate a temporary
    copy. The original workbook is never modified.
    """
    if __import__("sys").platform != "win32":
        raise RuntimeError("Automatische Excel-Neuberechnung ist nur unter Windows verfügbar.")

    try:
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Für die automatische Neuberechnung wird 'pywin32' benötigt. "
            "Installation: pip install pywin32"
        ) from exc

    temp_dir = Path(tempfile.mkdtemp(prefix="nias_recalc_"))
    temp_path = temp_dir / source_path.name
    shutil.copy2(source_path, temp_path)

    excel = None
    workbook = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.AskToUpdateLinks = False
        workbook = excel.Workbooks.Open(
            str(temp_path.resolve()),
            UpdateLinks=0,
            ReadOnly=False,
        )
        excel.Calculation = -4105  # xlCalculationAutomatic
        excel.CalculateFullRebuild()
        workbook.Save()
        workbook.Close(SaveChanges=True)
        workbook = None
        return temp_path
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        if workbook is not None:
            try:
                workbook.Close(SaveChanges=False)
            except Exception:
                pass
        if excel is not None:
            try:
                excel.Quit()
            except Exception:
                pass


def sml_limit(value: Any) -> Optional[float]:
    """Return the lowest numerical SML if several limits are present."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    values = re.findall(r"\d+(?:[.,]\d+)?", text(value).replace("*", ""))
    return min((float(item.replace(",", ".")) for item in values), default=None)


def find_columns(ws, header_row: int, warn_missing: bool = True) -> dict[str, int]:
    """Find columns from one- or two-line Excel headers."""
    aliases = {
        "rt": {"rt", "rtmin", "retentiontime", "retentiontimemin"},
        "name": {"name", "compound", "compoundname", "substance", "substancename"},
        "cas": {"cas", "casno", "casnumber", "casnr"},
        "db": {"db", "database", "library"},
        "match": {"match", "percentmatch", "matchpercent", "matchfactor", "quality"},
        "area": {"area", "peakarea", "flache", "peakflache"},
        "conc_area": {
            "concmgdm2", "concentrationmgdm2", "concentrationmgdm",
            "concmgdm", "migrationmgdm2",
        },
        "conc_kg": {
            "concmgkg", "concentrationmgkg", "migrationmgkg",
            "contentmgkg",
        },
        "sml": {"sml", "smlmgkg", "specificmigrationlimit", "specificmigrationlimitmgkg"},
        "reference": {"reference", "ref", "regulation", "legalreference"},
    }

    found: dict[str, int] = {}
    # Some reports split a header across two rows, e.g. "Conc." / "mg/kg".
    for column in range(1, ws.max_column + 1):
        parts = [normalized_header(ws.cell(header_row, column).value)]
        if header_row + 1 <= ws.max_row:
            parts.append(normalized_header(ws.cell(header_row + 1, column).value))
        candidates = {part for part in parts if part}
        if len(parts) == 2 and all(parts):
            candidates.add("".join(parts))

        for key, accepted in aliases.items():
            if candidates & accepted:
                found[key] = column

    defaults = {
        "rt": 1, "name": 2, "cas": 3, "db": 4, "match": 5,
        "area": 6, "conc_area": 7, "conc_kg": 8, "sml": 9, "reference": 10,
    }
    # db and area are optional and are not written to the result workbook.
    required = {"rt", "name", "cas", "match", "conc_area", "conc_kg", "sml", "reference"}
    for key, column in defaults.items():
        if key not in found:
            found[key] = column
            if key in required and warn_missing:
                warnings.warn(
                    f"Column '{key}' not found in header rows {header_row}/{header_row + 1}. "
                    f"Falling back to column {column}; verify the input layout.",
                    stacklevel=2,
                )
    return found


def detect_header_row(ws, preferred: int) -> int:
    """Select the most plausible header row near the expected report table."""
    aliases = {
        "name": {"name", "compound", "compoundname", "substance", "substancename"},
        "cas": {"cas", "casno", "casnumber", "casnr"},
        "conc_kg": {"concmgkg", "concentrationmgkg", "migrationmgkg", "contentmgkg"},
        "rt": {"rt", "rtmin", "retentiontime", "retentiontimemin"},
    }
    best_row, best_score, best_matches = preferred, -1, 0
    lower = max(1, preferred - 5)
    upper = min(ws.max_row, preferred + 5)
    for row in range(lower, upper + 1):
        score = matches = 0
        for column in range(1, ws.max_column + 1):
            first = normalized_header(ws.cell(row, column).value)
            second = normalized_header(ws.cell(row + 1, column).value) if row < ws.max_row else ""
            for accepted in aliases.values():
                if first in accepted:
                    # A header in this very row is worth more than one that is
                    # only visible from the row below: the lookahead exists for
                    # two-line headers, but it also lets a filler row directly
                    # above the table tie with the table's own header row.
                    score += 2
                    matches += 1
                elif {second, first + second} & accepted:
                    score += 1
                    matches += 1
        if score > best_score:
            best_row, best_score, best_matches = row, score, matches
    if best_matches < 2:
        warnings.warn(
            f"No reliable table header found near row {preferred}; using row {preferred}.",
            stacklevel=2,
        )
        return preferred
    return best_row


#: ``load_cas_lookup`` results keyed by (path, mtime, size): a batch reads
#: CASINFO once, not once per report, and an edited file is picked up again.
_CAS_LOOKUP_CACHE: dict[tuple[str, int, int], dict[str, dict[str, Any]]] = {}


def load_cas_lookup(cas_path: Path) -> dict[str, dict[str, Any]]:
    cas_path = Path(cas_path)
    try:
        stat = cas_path.stat()
        key: Optional[tuple[str, int, int]] = (
            str(cas_path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None
    if key is not None and key in _CAS_LOOKUP_CACHE:
        # Copies, so a caller that edits a record cannot change the cache.
        return {cas: dict(record) for cas, record in _CAS_LOOKUP_CACHE[key].items()}
    lookup = _read_cas_lookup(cas_path)
    if key is not None:
        _CAS_LOOKUP_CACHE.clear()
        _CAS_LOOKUP_CACHE[key] = {cas: dict(record) for cas, record in lookup.items()}
    return lookup


def _read_cas_lookup(cas_path: Path) -> dict[str, dict[str, Any]]:
    workbook = load_workbook(cas_path, data_only=True, read_only=True)
    try:
        rows = list(workbook[workbook.sheetnames[0]].iter_rows(values_only=True))
    finally:
        workbook.close()
    header = rows[0] if rows else ()
    header_map = {normalized_header(value): column
                  for column, value in enumerate(header, start=1)}

    cas_col = header_map.get("cas") or header_map.get("casno") or 1
    sml_col = header_map.get("sml") or 2
    reference_col = header_map.get("reference") or 3
    footnote_col = header_map.get("footnote") or 4

    def cell(values: tuple, column: int) -> Any:
        return values[column - 1] if column <= len(values) else None

    lookup: dict[str, dict[str, Any]] = {}
    sml_values: dict[str, set[float]] = {}
    for values in rows[1:]:
        cas = normalize_cas(cell(values, cas_col))
        if not cas:
            continue
        sml_raw = cell(values, sml_col)
        reference = cell(values, reference_col)
        footnote = cell(values, footnote_col)
        # CASINFO historically also stores lettered explanatory notes in the
        # Reference column. Interpret these as footnotes when column 4 is empty.
        if not text(footnote) and re.match(r"^\s*\([A-Za-z]\)\s+", text(reference)):
            footnote, reference = reference, None
        record = {"sml": sml_limit(sml_raw), "reference": reference, "footnote": footnote}
        # CASINFO holds the same CAS number on several rows and spreads the
        # maintained details over them - one row carries the SML, a later one the
        # footnote. Merging field by field keeps both, where picking a single
        # "most complete" row silently dropped whichever detail it lacked.
        merged = lookup.setdefault(
            cas, {"sml": None, "reference": None, "footnote": None})
        for field, value in record.items():
            if merged[field] in (None, "", "-") and value not in (None, "", "-"):
                merged[field] = value
        if record["sml"] is not None:
            sml_values.setdefault(cas, set()).add(record["sml"])
    for cas, values in sml_values.items():
        if len(values) > 1:
            warnings.warn(
                f"CASINFO lists conflicting SML values for CAS {cas}: "
                + ", ".join(str(value) for value in sorted(values))
                + f"; using {lookup[cas]['sml']}.",
                stacklevel=3,
            )
    return lookup


def cas_lookup_match(cas_lookup: dict[str, dict[str, Any]],
                     raw_cas: Any) -> dict[str, Any]:
    """Look up every CAS number a report cell contains, not just the first one.

    A combined identification carries several numbers, e.g.
    "000112-95-8 / 000629-94-7". normalize_cas() keeps only the first, so a
    maintained SML, reference or footnote on any of the others was silently
    lost. Fields are filled from the first entry that provides them; a
    disagreement about the SML is reported rather than resolved quietly.
    """
    merged: dict[str, Any] = {}
    limits: list[tuple[str, float]] = []
    for part in re.split(r"[/;,]", text(raw_cas)):
        number = normalize_cas(part)
        record = cas_lookup.get(number)
        if not record:
            continue
        if record.get("sml") is not None:
            limits.append((number, record["sml"]))
        for field, value in record.items():
            if merged.get(field) in (None, "", "-") and value not in (None, "", "-"):
                merged[field] = value
    if len({limit for _, limit in limits}) > 1:
        warnings.warn(
            "Uneindeutige Identifikation mit unterschiedlichen SML-Werten: "
            + ", ".join(f"{number} = {limit}" for number, limit in limits)
            + f"; verwendet wird {merged.get('sml')}.",
            stacklevel=2,
        )
    return merged


def classify_name(name: str) -> Optional[str]:
    """Classify report names, including abbreviated cyclic ester oligomers.

    Besides the explicit description ``cyclic polyester oligomer``, library names
    such as ``Cyclic NPG-IPA-NPG-IPA`` are treated as cyclic polyester oligomers
    when they start with "Cyclic" and contain at least two recognized polyester
    monomer abbreviations. This avoids classifying unrelated cyclic compounds.
    """
    lowered = text(name).casefold()
    if "styrene oligomer" in lowered or "styreme oligomer" in lowered:
        return "styrene"
    if "cyclic polyester oligomer" in lowered or "cyclic ester oligomer" in lowered:
        return "cyclic_polyester"
    if re.match(r"^cyclic(?:\s+|[-_:])", lowered):
        monomers = extract_monomer_abbreviations(name)
        if len(monomers) >= 2:
            return "cyclic_polyester"
    if "hydrocarbon" in lowered:
        return "hydrocarbon"
    if "silox" in lowered:
        return "siloxane"
    return None


def unknown_mz_signature(name: Any) -> Optional[tuple[int, ...]]:
    """Return a canonical numeric m/z signature for an explicitly unknown name."""
    value = text(name)
    if not value.casefold().startswith("unknown"):
        return None
    match = re.search(r"m\s*/\s*z\s*[:=]?\s*([^)]*)", value, re.IGNORECASE)
    if not match:
        return None
    masses = [int(item) for item in re.findall(r"\d+", match.group(1))]
    return tuple(sorted(set(masses))) if masses else None


def unknown_summary_label(signature: tuple[int, ...]) -> str:
    return "Sum of unknown m/z (" + "/".join(map(str, signature)) + ")"


# ---------------------------------------------------------------------------
# Unknown register: m/z fingerprint analysis
#
# Analysts write an unknown as ``unknown (m/z 91/123/172/43)``. The order is the
# information: 91 is the base peak, 123 the second highest peak, 172 the third.
# ``unknown_mz_signature`` above deliberately sorts that list because it is the
# grouping key for report aggregation - the functions here keep the written
# order, which is what every spectral comparison needs.
# ---------------------------------------------------------------------------

# No intensities are available, only ranks. The intensity of rank r is therefore
# modelled as the reciprocal of the rank: 100, 50, 33.3, 25, 20 ...
#: Bildunterschriften der Spektrumsansicht im Unknown-Register. Eintraege aus
#: dem SQLite-Register bringen ein gemessenes Spektrum mit, aus der JSON-Datei
#: migrierte Eintraege nicht (siehe measured_entry_spectrum).
SPECTRUM_HINT_MEASURED = ("Balkenhöhe = gemessene Intensität in % des Basispeaks "
                          "aus dem SQLite-Register.")
SPECTRUM_HINT_MIXED = ("Balkenhöhe: gemessene Intensität, wo ein Spektrum vorliegt, "
                       "sonst modelliert aus dem Rang (100/Rang). Die Beschriftung "
                       "je Spur nennt die Quelle.")
SPECTRUM_HINT_MODELLED = ("Balkenhöhe = modellierte Intensität aus dem Rang (100/Rang). "
                          "Für diesen Eintrag liegen nur Rangfolgen vor, keine "
                          "gemessenen Intensitäten.")

# The significant-ion subset the set-based scoring runs on, kept numerically
# identical to gc_register.SIGNIFICANT_ION_FRACTION / _LIMIT. They are repeated
# rather than imported because this module must stay usable when gc_register is
# not importable, and a silent divergence would change every stored hint.
SIGNIFICANT_ION_FRACTION = 0.01
SIGNIFICANT_ION_LIMIT = 20


def rank_pseudo_intensity(rank: int) -> float:
    """Return the modelled relative intensity for a 1-based intensity rank.

    The fallback half of the pair, and it stays permanently: an entry migrated
    from the JSON register carries a ranking and never a spectrum, so there is
    nothing else to describe it with. Where a measured spectrum exists the
    callers pass the real intensities instead.
    """
    return 100.0 / max(1, int(rank))


def rank_intensities(ranked) -> dict:
    """Model an intensity per mass from its position: 100, 50, 33.3, 25 ..."""
    return {int(mass): rank_pseudo_intensity(rank)
            for rank, mass in enumerate(ranked or (), start=1)}


def significant_mz_subset(masses, intensities=None,
                          limit: int = SIGNIFICANT_ION_LIMIT,
                          fraction: float = SIGNIFICANT_ION_FRACTION):
    """Reduce a signature to the ions the set-based scoring may see.

    The subset is the ions at or above ``fraction`` of the base peak, the
    strongest ``limit`` of them, strongest first - the same rule
    ``gc_register.significant_ions`` applies to a measured spectrum.

    This is not a refinement, it is a correctness condition. A full spectrum has
    50-500 ions and therefore trivially contains a delta-14 ladder, so a
    homologue detector fed the full set fires on every single entry, and the ion
    Jaccard index collapses toward zero for every pair. Without measured
    intensities the rank model supplies them, where the ``limit`` is what bites:
    100/rank stays above 1 % of the base peak up to rank 100.
    """
    ordered = [int(mass) for mass in masses or ()]
    if not ordered:
        return ()
    weights = dict(intensities) if intensities else rank_intensities(ordered)
    peak = max((float(weights.get(mass, 0.0)) for mass in ordered), default=0.0)
    if peak <= 0:
        return tuple(ordered[:limit])
    keep = [mass for mass in ordered
            if float(weights.get(mass, 0.0)) >= peak * fraction]
    keep.sort(key=lambda mass: -float(weights.get(mass, 0.0)))
    seen: dict[int, None] = {}
    for mass in keep:
        seen.setdefault(mass, None)
        if len(seen) >= limit:
            break
    return tuple(seen)


# Stein & Scott weighting of the classic dot-product match factor. The m/z
# exponent favours the more diagnostic high masses, the intensity exponent
# dampens the dominance of the base peak.
MZ_WEIGHT_EXPONENT = 0.5
INTENSITY_WEIGHT_EXPONENT = 0.6

# The only genuinely repeatable building block: each further member of an alkyl
# homologous series differs by one more CH2.
HOMOLOGUE_REPEAT_UNIT = (14, "CH2")

# Single-step neutral losses that relate two fragments of related structures.
HOMOLOGUE_NEUTRAL_LOSSES: tuple[tuple[int, str], ...] = (
    (28, "CO / C2H4"),
    (18, "H2O"),
    (16, "O"),
    (44, "CO2 / C2H4O"),
)

# Spacings that form a ladder inside a single spectrum.
HOMOLOGUE_DELTAS: tuple[tuple[int, str], ...] = (
    (14, "CH2"),
    (28, "C2H4 / CO"),
    (42, "C3H6"),
    (16, "O"),
    (18, "H2O"),
    (44, "CO2 / C2H4O"),
)

# Diagnostic fragment ions. A hint fires when at least ``min_hits`` of the ions
# are present; membership of the base peak counts twice. These are deliberately
# broad indications, never identifications.
DIAGNOSTIC_ION_PATTERNS: tuple[tuple[str, frozenset, int], ...] = (
    ("Siloxan", frozenset({73, 147, 207, 221, 281, 355}), 2),
    ("Phthalat", frozenset({149, 167, 104, 76}), 2),
    ("Alkylaromat / Styrol-Oligomer", frozenset({91, 105, 117, 119, 131, 193, 207}), 2),
    ("Styrol", frozenset({104, 103, 78, 51}), 2),
    ("Aliphat / Kohlenwasserstoff", frozenset({43, 57, 71, 85, 99, 113, 127}), 3),
    ("Alken / Cycloaliphat", frozenset({55, 69, 83, 97, 111}), 3),
    ("Adipat", frozenset({129, 111, 147, 100}), 2),
    ("Fettsäureester / -amid", frozenset({74, 87, 59, 55, 43}), 3),
    ("Erucamid / Oleamid", frozenset({59, 72, 126, 338, 264}), 2),
    ("Gehindertes Phenol (Antioxidans)", frozenset({57, 161, 177, 191, 219, 205}), 2),
    ("Acrylat", frozenset({55, 56, 69, 99, 113, 86}), 3),
    ("Benzophenon / Photoinitiator", frozenset({105, 77, 51, 121, 135}), 3),
    ("Keton", frozenset({43, 58, 71, 99}), 2),
    ("Amid / Amin", frozenset({44, 59, 72, 86}), 2),
    ("Terpen", frozenset({93, 121, 136, 68, 79}), 3),
    ("Chlorierte Verbindung", frozenset({35, 36, 49, 83, 85}), 2),
)

UNKNOWN_STATUS_CHOICES = (
    "offen", "in Klärung", "identifiziert", "verworfen", "Artefakt",
)

# Same order of magnitude as Settings.rt_tolerance (0.035) and
# blank_rt_tolerance (0.04) used by the GC engine for peak matching.
UNKNOWN_RT_TOLERANCE = 0.05

# --------------------------------------------------------------------------
# What makes a sighting the same substance as an entry
# --------------------------------------------------------------------------
#
# Deliberately separate constants from UNKNOWN_RT_TOLERANCE above: that one is
# the width of the search query ``rt:12.34``, this one decides whether two
# measurements are the same substance. The numbers coincide today; the meanings
# must not be coupled.
#
# Until this existed the m/z key decided alone, so two substances sharing a
# fragmentation pattern three minutes apart became one entry. Retention time and
# the measured spectrum now have to agree as well - and where they do not, a new
# entry is created rather than a wrong one silently grown.

#: Up to this deviation the retention time confirms the assignment.
UNKNOWN_RT_SAFE_MIN = 0.05
#: Beyond this deviation nothing is ever merged, however well the spectrum fits.
UNKNOWN_RT_CHECK_MIN = 0.15
#: Required spectral match (0-1000) inside the safe retention-time zone.
UNKNOWN_MATCH_MIN = 700.0
#: Required match in the check zone and when the retention time cannot be
#: compared at all - missing evidence must not read as confirming evidence.
UNKNOWN_MATCH_CHECK_MIN = 850.0

# The three axes that carry evidence of identity.
SIMILARITY_WEIGHTS = {"spectral": 0.53, "rt": 0.24, "ions": 0.23}

# Appearing in the same samples is evidence of a common source, so it may only
# raise a score, never lower it: the same substance found in two different
# samples must not be scored down for it - that is precisely the case the
# register exists to catch.
COOCCURRENCE_BONUS = 0.25

# Cramer class III / genotoxic alert thresholds applied to unknown migrants.
TTC_ALERT_MG_KG = 0.0025
TTC_CRAMER_III_MG_KG = 0.09


def unknown_mz_ranked(name: Any) -> Optional[tuple[int, ...]]:
    """Return the m/z values in written order: base peak first, then descending.

    ``unknown_mz_signature`` sorts the same values because it is the aggregation
    key for the report. Here the written order is preserved, because it carries
    the intensity ranking the analyst recorded.
    """
    value = text(name)
    if not value.casefold().startswith("unknown"):
        return None
    match = re.search(r"m\s*/\s*z\s*[:=]?\s*([^)]*)", value, re.IGNORECASE)
    if not match:
        return None
    masses = [int(item) for item in re.findall(r"\d+", match.group(1))]
    # Duplicates are removed without disturbing the ranking.
    return tuple(dict.fromkeys(masses)) or None


def format_mz_list(masses) -> str:
    return "/".join(str(int(mass)) for mass in masses or ())


def canonical_mz_key(masses) -> str:
    """Return the register key: sorted, deduplicated m/z values."""
    return format_mz_list(sorted({int(mass) for mass in masses or ()}))


#: How many of the strongest ions define an entry's identity. Mirrors
#: gc_register.ENTRY_KEY_IONS; keying on the full significant set instead would
#: fragment the register, because two measurements of one substance almost never
#: share all twenty ions.
ENTRY_KEY_IONS = 4


def unknown_identity_key(ranked, count: int = ENTRY_KEY_IONS) -> str:
    """Return the dedup key of a signature: its strongest ``count`` masses.

    The same string ``gc_register.entry_identity_key`` produces, so a signature
    coming off a report run and an entry already in the register are compared in
    one namespace.
    """
    masses = [int(mass) for mass in ranked or ()]
    return format_mz_list(sorted(masses[:count]))


def format_de(value: Any, digits: int = 2) -> str:
    """A number as the report writes it: decimal comma, fixed digits."""
    number = numeric_value(value)
    return "" if number is None else f"{number:.{digits}f}".replace(".", ",")


def unknown_rt_delta(sighting: dict[str, Any],
                     entry: dict[str, Any]) -> Optional[float]:
    """Distance between a sighting's retention time and an entry's mean.

    ``None`` when either side has none: a legacy entry migrated from the JSON
    register carries no ``rt_mean``, and a report row need not carry an RT.
    """
    left = numeric_value(sighting.get("rt"))
    right = numeric_value(entry.get("rt_mean"))
    if left is None or right is None:
        return None
    # Retention times are kept to four decimals throughout the register; keeping
    # the delta at the same precision stops 0.05000000000000071 from falling out
    # of the safe zone that 12.05 - 12.00 is supposed to be inside of.
    return round(abs(left - right), 4)


#: The zone names, in the order of increasing doubt.
UNKNOWN_RT_ZONES = ("sicher", "pruefzone", "unbestimmt", "ausserhalb")


def unknown_rt_zone(delta: Optional[float]) -> str:
    """Which retention-time zone a deviation falls into. See UNKNOWN_RT_ZONES."""
    if delta is None:
        return "unbestimmt"
    value = abs(float(delta))
    if value <= UNKNOWN_RT_SAFE_MIN + 1e-9:
        return "sicher"
    if value <= UNKNOWN_RT_CHECK_MIN + 1e-9:
        return "pruefzone"
    return "ausserhalb"


def required_match_factor(zone: str) -> Optional[float]:
    """The spectral match a zone demands; ``None`` means "never merge"."""
    if zone == "sicher":
        return UNKNOWN_MATCH_MIN
    if zone in ("pruefzone", "unbestimmt"):
        return UNKNOWN_MATCH_CHECK_MIN
    return None


def sighting_entry_decision(sighting: dict[str, Any], entry: dict[str, Any],
                            entry_spectrum: Optional[list] = None,
                            tolerance: int = 0) -> dict[str, Any]:
    """May this sighting join this entry? With a reason, always.

    The caller has already established that the m/z key points here; this
    decides whether the evidence supports it. Two paths, because the register
    holds two kinds of entry:

    * an entry with a **measured** spectrum is compared spectrum against
      signature - the sighting contributes its rank model, since a report row
      from an Excel workbook has no intensities, and the entry contributes its
      real ones;
    * an entry **without** a spectrum has nothing to compare, so the m/z key
      keeps deciding as it always did, and only the retention time is added as
      a gate.

    Writes nothing and opens nothing: ``entry_spectrum`` is passed in, not
    fetched, so the same decision can be reached in a test without a register.
    """
    delta = unknown_rt_delta(sighting, entry)
    zone = unknown_rt_zone(delta)
    required = required_match_factor(zone)
    ions_entry, intensities_entry = spectrum_ion_profile(entry_spectrum)
    basis = "spektrum" if ions_entry else "schluessel"
    rt_text = (f"RT-Abweichung {format_de(delta, 3)} min" if delta is not None
               else "keine Retentionszeit zum Vergleich")

    if required is None:
        return {
            "accept": False, "zone": zone, "delta": delta, "match": None,
            "required": None, "basis": basis,
            "reason": (f"RT weicht um {format_de(delta, 3)} min ab "
                       f"(Grenze {format_de(UNKNOWN_RT_CHECK_MIN, 2)} min)."),
        }

    if basis == "schluessel":
        return {
            "accept": True, "zone": zone, "delta": delta, "match": None,
            "required": None, "basis": basis,
            "reason": (f"m/z-Schlüssel stimmt überein, {rt_text}; "
                       "zu diesem Eintrag ist kein gemessenes Spektrum "
                       "gespeichert."),
        }

    ions_sighting = significant_mz_subset(sighting.get("ranked_mz") or ())
    match = spectral_match_factor(ions_sighting, ions_entry, tolerance,
                                  None, intensities_entry)
    accept = match >= required
    if accept:
        reason = (f"Spektrenübereinstimmung {match:.0f} von 1000, "
                  f"gefordert {required:.0f} ({rt_text}).")
    elif zone == "pruefzone":
        reason = (f"Spektrenübereinstimmung {match:.0f} von 1000, gefordert "
                  f"{required:.0f} in der Prüfzone ({rt_text}).")
    else:
        reason = (f"Spektrenübereinstimmung {match:.0f} von 1000, "
                  f"gefordert {required:.0f} ({rt_text}).")
    return {"accept": accept, "zone": zone, "delta": delta, "match": match,
            "required": required, "basis": basis, "reason": reason}


def choose_unknown_entry(sighting: dict[str, Any], candidates: list,
                         spectrum_of) -> tuple[Optional[str],
                                               Optional[dict[str, Any]],
                                               Optional[tuple]]:
    """Pick the entry a sighting belongs to, or report why none of them fits.

    Returns ``(key, entry, rejected)``. ``entry`` is None when nothing was
    accepted; ``rejected`` then carries ``(key, entry, decision)`` of the
    closest candidate, which is what makes a split reportable. Both are None
    when there was no candidate at all - a signature nobody has seen before is
    an ordinary new entry, not a split.

    Among accepted candidates the strongest spectral match wins. Candidates
    arrive with the canonical-key entry first, and ``max`` keeps the first of
    equal scores, so a tie between two entries without spectra falls to the
    canonical key rather than to dictionary order.
    """
    scored = [(key, entry,
               sighting_entry_decision(sighting, entry, spectrum_of(entry)))
              for key, entry in candidates]
    accepted = [item for item in scored if item[2]["accept"]]
    if accepted:
        key, entry, _ = max(accepted, key=lambda item: item[2]["match"] or 0.0)
        return key, entry, None
    if scored:
        return None, None, max(scored, key=lambda item: item[2]["match"] or 0.0)
    return None, None, None


def free_register_key(entries: dict[str, Any], key: str) -> str:
    """A register key not yet taken, ``149/91/57`` or ``149/91/57#2``.

    ``entries.mz_key`` is UNIQUE in the SQLite schema, so two entries with the
    same signature cannot share the plain key -- and splitting an isomer off is
    exactly the case where two entries must carry the same masses. The suffix
    is never parsed anywhere: every consumer reads the masses from
    ``canonical_mz``, which both entries keep identical and correct.
    """
    if key not in entries:
        return key
    suffix = 2
    while f"{key}#{suffix}" in entries:
        suffix += 1
    return f"{key}#{suffix}"


def register_spectrum_lookup(module: Any, connection: Any):
    """``lookup(entry) -> spectrum | None`` on the already open connection.

    Not :func:`measured_entry_spectrum`: that one opens a second connection by
    path, and this runs inside the report run's BEGIN IMMEDIATE on a register
    that may live on a share. Cached per run, because one signature turns up
    repeatedly in a duplicate determination and each lookup would otherwise
    decode the same blobs again.
    """
    cache: dict[int, Optional[list]] = {}

    def lookup(entry: dict[str, Any]) -> Optional[list]:
        number = unknown_entry_number(entry)
        if number is None:
            return None
        if number not in cache:
            try:
                cache[number] = best_register_spectrum(module, connection, number)
            except Exception:  # noqa: BLE001 - a missing spectrum is not an error
                cache[number] = None
        return cache[number]

    return lookup


def unknown_identity_collisions(register: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the identity keys that more than one entry answers to.

    Reported, never resolved: the entry sorting first wins every new sighting,
    and the register tab says which entries are affected so the analyst can
    decide whether they really are one substance.
    """
    try:
        return gc_register_module().identity_collisions(register)
    except Exception:  # noqa: BLE001 - a missing module must not kill the tab
        return []


def parse_mz_list(value: Any) -> tuple[int, ...]:
    """Read a ``91/123/172`` style list back into integers, order preserved."""
    return tuple(dict.fromkeys(int(item) for item in re.findall(r"\d+", text(value))))


def _ion_weights(ranked, intensities=None) -> dict:
    """Return the Stein & Scott weight per ion of a signature.

    ``intensities`` is a ``{m/z: % of base peak}`` mapping taken from a measured
    spectrum. Without it the rank model supplies the intensities, which is the
    only thing available for an entry migrated from the JSON register. Both
    paths are permanent: legacy entries never gain a spectrum.
    """
    measured = dict(intensities) if intensities else None
    weights: dict[int, float] = {}
    for rank, mass in enumerate(ranked or (), start=1):
        mass = int(mass)
        intensity = (measured.get(mass, 0.0) if measured is not None
                     else rank_pseudo_intensity(rank))
        if intensity <= 0:
            continue
        weights[mass] = (
            float(mass) ** MZ_WEIGHT_EXPONENT
            * intensity ** INTENSITY_WEIGHT_EXPONENT
        )
    return weights


def align_ions(first, second, tolerance: int = 0) -> list:
    """Pair the ions of two signatures, each ion used at most once.

    With ``tolerance`` 0 this is the plain intersection. A tolerance of 1 also
    pairs neighbouring nominal masses, which absorbs typing slips and isotope
    peaks noted instead of the fragment itself.
    """
    left = tuple(sorted({int(m) for m in first or ()}))
    right = tuple(sorted({int(m) for m in second or ()}))
    if not tolerance:
        return [(m, m) for m in left if m in set(right)]
    # Canonical orientation makes ties symmetric. Dynamic programming maximises
    # one-to-one overlap, then minimises mass error; rank-order greedy matching
    # could consume the only partner of a later ion and depend on query order.
    if left > right:
        return [(b, a) for a, b in align_ions(right, left, tolerance)]
    previous = [(0, 0, ())] * (len(right) + 1)
    for a in left:
        current = [(0, 0, ())]
        for j, b in enumerate(right, 1):
            options = [previous[j], current[j - 1]]
            if abs(a - b) <= tolerance:
                count, error, pairs = previous[j - 1]
                options.append((count + 1, error - abs(a - b), pairs + ((a, b),)))
            current.append(max(options, key=lambda v: (v[0], v[1], v[2])))
        previous = current
    return list(previous[-1][2])


def spectral_match_factor(first, second, tolerance: int = 0,
                          first_intensities=None,
                          second_intensities=None) -> float:
    """Return the weighted dot-product match factor on the familiar 0-1000 scale.

    Ions present in only one of the two signatures stay in the denominators and
    therefore penalise missing overlap, exactly as in the NIST/PBM measure the
    analysts already read as "% match". Each side is weighted by its measured
    intensities where it has them and by the rank model where it has not, so a
    measured entry and a migrated one can still be compared.
    """
    weights_first = _ion_weights(first, first_intensities)
    weights_second = _ion_weights(second, second_intensities)
    if not weights_first or not weights_second:
        return 0.0
    numerator = sum(
        weights_first[left] * weights_second[right]
        for left, right in align_ions(first, second, tolerance)
    )
    if numerator <= 0:
        return 0.0
    denominator = (
        sum(weight * weight for weight in weights_first.values())
        * sum(weight * weight for weight in weights_second.values())
    )
    return round(1000.0 * numerator * numerator / denominator, 1)


def ion_overlap(first, second, tolerance: int = 0,
                first_intensities=None, second_intensities=None,
                reduce: bool = True):
    """Return (Jaccard index, number of shared ions, the shared ions).

    Runs on the significant-ion subset, never on the full spectrum: a full
    spectrum brings 50-500 mostly noise ions, and the union in the denominator
    would then drive the index toward zero for every pair, however well the two
    substances actually match. ``reduce=False`` is for tests that have to show
    what the unreduced behaviour looks like.
    """
    if reduce:
        first = significant_mz_subset(first, first_intensities)
        second = significant_mz_subset(second, second_intensities)
    left = {int(mass) for mass in first or ()}
    right = {int(mass) for mass in second or ()}
    if not left or not right:
        return 0.0, 0, ()
    pairs = align_ions(sorted(left), sorted(right), tolerance)
    shared = tuple(sorted(mass for mass, _ in pairs))
    union = len(left) + len(right) - len(shared)
    return (len(shared) / union if union else 0.0), len(shared), shared


def internal_homologue_series(masses, intensities=None,
                              reduce: bool = True) -> str:
    """Name a constant-spacing ladder inside one signature.

    Three or more ions spaced by a constant 14 Da form a CH2 ladder, the classic
    signature of an alkyl homologous series.

    The ladder is looked for in the significant-ion subset only. A full spectrum
    contains a delta-14 sequence by construction - the alkyl fragment series is
    present in almost every organic spectrum at some intensity - so feeding the
    full ion list in makes this fire on every entry and the answer stops meaning
    anything. ``reduce=False`` exists so a test can demonstrate exactly that.
    """
    if reduce:
        masses = significant_mz_subset(masses, intensities)
    ordered = sorted({int(mass) for mass in masses or ()})
    available = set(ordered)
    for delta, formula in HOMOLOGUE_DELTAS:
        for mass in ordered:
            length, current = 1, mass
            while current + delta in available:
                current += delta
                length += 1
            if length >= 3:
                return f"delta {delta} ({formula})"
    return ""


def pairwise_homologue_relation(first, second, first_intensities=None,
                                second_intensities=None,
                                reduce: bool = True) -> str:
    """Return the homologous relation between two signatures, if any.

    Shifting one signature by a repeat unit and hitting at least two ions of the
    other means both belong to the same series - chemically related even where
    the spectra themselves drift apart.

    Only CH2 is treated as a repeatable unit; the neutral losses are checked at a
    single step. Allowing multiples of every delta produced meaningless links such
    as a shift of 88 Da that happens to hit two ions by chance.

    Like the other set-based detectors this runs on the significant-ion subset.
    Two full spectra shifted by 14 Da will always hit two ions of each other.
    """
    if reduce:
        first = significant_mz_subset(first, first_intensities)
        second = significant_mz_subset(second, second_intensities)
    left = {int(mass) for mass in first or ()}
    right = {int(mass) for mass in second or ()}
    if len(left) < 2 or len(right) < 2 or left == right:
        return ""
    candidates = [(HOMOLOGUE_REPEAT_UNIT[0] * factor,
                   HOMOLOGUE_REPEAT_UNIT[1] if factor == 1
                   else f"{factor}x {HOMOLOGUE_REPEAT_UNIT[1]}")
                  for factor in (1, 2, 3)]
    candidates.extend(HOMOLOGUE_NEUTRAL_LOSSES)
    for delta, formula in candidates:
        for shift in (delta, -delta):
            hits = len({mass + shift for mass in left} & right)
            if hits >= 2:
                sign = "+" if shift > 0 else "-"
                return f"homolog {sign}{abs(shift)} ({formula})"
    return ""


def diagnostic_class_hints(ranked, intensities=None) -> list:
    """Return suspected substance classes from characteristic fragment ions.

    These are indications for the analyst, never identifications: the register
    feeds a compliance workflow, so a hint must not read like a finding.

    The base peak is the one place this function carries the pseudo-intensity
    assumption: without a spectrum it is the first mass of the ranked list,
    with one it is the strongest measured ion. Both paths stay.
    """
    ranked = significant_mz_subset(ranked, intensities)
    masses = {int(mass) for mass in ranked or ()}
    if not masses:
        return []
    if intensities:
        base_peak = max(masses, key=lambda mass: float(
            dict(intensities).get(mass, 0.0)))
    else:
        base_peak = int(ranked[0]) if ranked else None
    hints: list[tuple[int, str]] = []
    for label, ions, min_hits in DIAGNOSTIC_ION_PATTERNS:
        matched = masses & ions
        if not matched:
            continue
        # A characteristic ion that is also the base peak weighs twice.
        score = len(matched) + (1 if base_peak in ions else 0)
        if score >= min_hits:
            hints.append((score, label))
    hints.sort(key=lambda item: (-item[0], item[1]))
    return [label for _, label in hints]


def rt_proximity_score(first_rt, second_rt) -> Optional[float]:
    """Return 1.0 inside the RT window, decaying to 0 at ten times the window.

    Returns None when either retention time is unknown, so the caller can drop
    the term instead of scoring the pair down for missing data.
    """
    if first_rt is None or second_rt is None:
        return None
    distance = abs(first_rt - second_rt)
    if distance <= UNKNOWN_RT_TOLERANCE:
        return 1.0
    span = UNKNOWN_RT_TOLERANCE * 10
    return max(0.0, 1.0 - (distance - UNKNOWN_RT_TOLERANCE) / (span - UNKNOWN_RT_TOLERANCE))


def jaccard_index(first, second) -> Optional[float]:
    """Return the Jaccard index, or None when either set is empty."""
    if not first or not second:
        return None
    union = set(first) | set(second)
    return len(set(first) & set(second)) / len(union) if union else None


def substance_group_key(item: dict[str, Any]):
    """Return a conservative identity key suitable for quantity aggregation."""
    name = text(item.get("name"))
    if not name or name.casefold().startswith("sum of "):
        return None
    if name.casefold().startswith("unknown"):
        signature = unknown_mz_signature(name)
        return ("unknown", signature) if signature else None
    cas = normalize_cas(item.get("cas"))
    if cas:
        return "cas", cas
    normalized_name = re.sub(r"[\W_]+", "", name.casefold())
    return ("name", normalized_name) if normalized_name else None


def repeated_substance_summary_label(key, items: list[dict[str, Any]]) -> str:
    if key[0] == "unknown":
        return unknown_summary_label(key[1])
    return "Sum of " + text(items[0].get("name"))


def split_repeated_substance_groups(rows: list[dict[str, Any]]):
    """Remove substances occurring at least twice and return identity groups."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for item in rows:
        key = substance_group_key(item)
        if key:
            groups.setdefault(key, []).append(item)
    repeated = {key: items for key, items in groups.items() if len(items) >= 2}
    repeated_item_ids = {id(item) for items in repeated.values() for item in items}
    remaining = [item for item in rows if id(item) not in repeated_item_ids]
    return remaining, repeated


def normalize_fingerprint_area_percentages(rows: list[dict[str, Any]]) -> float:
    """Renormalize manually retained Fingerprint rows to exactly 100.0000%."""
    if not rows:
        return 0.0
    missing = [index for index, item in enumerate(rows, start=2)
               if item.get("area_pct") is None]
    if missing:
        raise ValueError(
            "Area % fehlt in der Fingerprint-Tabelle in Datenzeile(n): "
            + ", ".join(map(str, missing))
        )
    negative = [index for index, item in enumerate(rows, start=2)
                if item["area_pct"] < 0]
    if negative:
        raise ValueError(
            "Area % darf nicht negativ sein. Betroffene Datenzeile(n): "
            + ", ".join(map(str, negative))
        )
    original_total = sum(item["area_pct"] for item in rows)
    if original_total <= 0:
        raise ValueError(
            "Die Summe der verbleibenden Area-%-Werte ist 0. "
            "Eine Neuberechnung ist nicht möglich."
        )
    for item in rows:
        item["area_pct"] = round(item["area_pct"] / original_total * 100, 4)
    # Keep the displayed four-decimal values at exactly 100.0000%.
    residual = round(100.0 - sum(item["area_pct"] for item in rows), 4)
    if residual:
        largest = max(rows, key=lambda item: item["area_pct"])
        largest["area_pct"] = round(largest["area_pct"] + residual, 4)
    return original_total


def total_extraction_sum(rows: list[dict[str, Any]]) -> float:
    """Validate and add up the concentration column without renormalising it.

    The Fingerprint report scales its Area % column to exactly 100 %. A
    concentration column must keep the magnitude it was measured with - a
    column that sums to a measured total is the whole point of the Total
    Extraction report (spec v3.1 §VII.10, assumption 7). Only the two checks
    that guard the Fingerprint sum are kept, so a broken input still fails
    loudly instead of producing a plausible wrong total.
    """
    if not rows:
        return 0.0
    missing = [index for index, item in enumerate(rows, start=2)
               if item.get("conc_ugl") is None]
    if missing:
        raise ValueError(
            "c [µg/L] fehlt in der Total-Extraction-Tabelle in Datenzeile(n): "
            + ", ".join(map(str, missing))
        )
    negative = [index for index, item in enumerate(rows, start=2)
                if item["conc_ugl"] < 0]
    if negative:
        raise ValueError(
            "c [µg/L] darf nicht negativ sein. Betroffene Datenzeile(n): "
            + ", ".join(map(str, negative))
        )
    return round(sum(item["conc_ugl"] for item in rows), 4)


@functools.lru_cache(maxsize=1)
def _monomer_patterns() -> tuple[tuple[str, "re.Pattern[str]"], ...]:
    """One compiled pattern per abbreviation, longest first.

    Longer abbreviations win so that PG is not taken from DPG/TPG and PA is
    not taken from "PA isomer".
    """
    return tuple(
        (abbreviation,
         re.compile(rf"(?<![A-Za-z0-9]){re.escape(abbreviation)}(?![A-Za-z0-9])",
                    re.IGNORECASE))
        for abbreviation in sorted(MONOMER_ABBREVIATIONS, key=len, reverse=True))


def extract_monomer_abbreviations(value: Any) -> list[str]:
    """Extract known monomer abbreviations from a CAS-No. description.

    Example: "Cyclic EG-AA-PG-PA" -> ["EG", "AA", "PG", "PA"].
    Matching is case-insensitive, while the canonical spelling is returned.
    """
    source = text(value)
    if not source:
        return []

    matches: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for abbreviation, pattern in _monomer_patterns():
        for match in pattern.finditer(source):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            matches.append((span[0], abbreviation))
            occupied.append(span)

    matches.sort(key=lambda item: item[0])
    result: list[str] = []
    for _, abbreviation in matches:
        if abbreviation not in result:
            result.append(abbreviation)
    return result


def cyclic_polyester_summary_label(abbreviations: list[str]) -> str:
    """Build the cyclic polyester summary label with detected monomers."""
    monomer_text = ", ".join(abbreviations) if abbreviations else "XXX"
    return (
        f"Sum of cyclic polyester oligomers containing {monomer_text} "
        "(estimated)*"
    )


def cyclic_polyester_abbreviation_note(abbreviations: list[str]) -> str:
    """Build the filtered abbreviation note for monomers present in results."""
    entries = [
        f"{abbreviation}, {MONOMER_ABBREVIATIONS[abbreviation]}"
        for abbreviation in abbreviations
        if abbreviation in MONOMER_ABBREVIATIONS
    ]
    return "* Abbreviations: " + "; ".join(entries)


def clean_footnote(value: Any) -> str:
    return re.sub(r"^\s*\([A-Za-z]\)\s*", "", text(value)).strip()


def excel_superscript_marker(index: int) -> tuple[str, str]:
    """Return marker text and its alphabetic label."""
    label = chr(ord("a") + index)
    return f"({label})", label


def clean_sample_name(value: Any) -> str:
    """Remove report prefixes/suffixes such as '26_' and '_A' from sample names.

    Rules:
    - exactly two digits followed by an underscore at the beginning are removed;
    - one trailing letter or digit preceded by an underscore is removed.
    """
    sample = text(value)
    sample = re.sub(r"^\d{2}_", "", sample)
    sample = re.sub(r"_[A-Za-z0-9]$", "", sample)
    return sample


def copy_report_metadata(source_ws, target_ws, last_column: int = 9) -> None:
    """Write title and report metadata in rows 1 to 4.

    The populated source sheet is "Auswertung" in the supplied workbook.
    Values are taken from its fixed report cells.
    """
    title_fill = PatternFill("solid", fgColor="BA0C2F")
    base_font = Font(name="Arial", size=8, color="000000")
    label_font = Font(name="Arial", size=8, bold=False, color="000000")

    target_ws["A1"] = "GC-MS/FID – NIAS-Screening –"
    target_ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
    target_ws["A1"].font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    target_ws["A1"].fill = title_fill
    target_ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    target_ws.row_dimensions[1].height = 18

    method = text(source_ws["B22"].value)
    sample_name = clean_sample_name(source_ws["B2"].value)
    analyst = text(source_ws["J2"].value)
    operator = text(source_ws["J4"].value)
    simulant = text(source_ws["B10"].value)
    temperature = text(source_ws["E8"].value)
    duration = text(source_ws["E9"].value)
    sv_ratio = numeric_value(source_ws["E12"].value)

    if temperature:
        try:
            temperature = f"{float(temperature):g} °C"
        except ValueError:
            if "°c" not in temperature.lower():
                temperature = f"{temperature} °C"
    migrate_parts = [part for part in (simulant, temperature, duration) if part]
    migrate = " | ".join(migrate_parts)
    sv_text = "" if sv_ratio is None else f"{sv_ratio:.1f}"

    # Row 2: method across the full table width.
    target_ws["A2"] = method
    target_ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_column)
    target_ws["A2"].font = Font(name="Arial", size=8, bold=True, italic=True)

    # Row 3: sample and analyst.
    target_ws["A3"] = "Sample:"
    target_ws["B3"] = sample_name
    target_ws.merge_cells("B3:F3")
    target_ws["G3"] = "Analyst:"
    target_ws["H3"] = analyst

    # Row 4: migrate conditions, S/V-ratio and operator.
    target_ws["A4"] = "Migrate:"
    target_ws["B4"] = migrate
    target_ws.merge_cells("B4:D4")
    target_ws["E4"] = "S/V-ratio"
    target_ws["F4"] = sv_text
    target_ws["G4"] = "Operator:"
    target_ws["H4"] = operator

    for row in range(2, 5):
        target_ws.row_dimensions[row].height = 13
        for column in range(1, last_column + 1):
            cell = target_ws.cell(row, column)
            if cell.coordinate in {"A3", "G3", "A4", "E4", "G4"}:
                cell.font = label_font
            elif cell.coordinate != "A2":
                cell.font = base_font
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)



DUPLICATE_SHEET_NAME = "Doppelbestimmung"
DUPLICATE_PARAMETER_SHEET = "Parameter"
# ISTD table per determination of a batch Doppelbestimmung (``ISTD_1``,
# ``ISTD_2``); workbooks written before it carry ``Standards_n`` instead.
DUPLICATE_ISTD_SHEET_PREFIX = "ISTD_"
# Einzelbestimmung workbook (``AutoLib.make_single_workbook``): every PBM peak
# with an FID area is a row, the IS peaks are marked IS1..IS4 in column ISTD,
# and every row is quantified with the mean area and concentration of IS1..IS3
# from the ISTD sheet. Columns A to D are RT, Name, CAS and mg/kg.
SINGLE_SHEET_NAME = "Einzelbestimmung"
SINGLE_ISTD_SHEET = "ISTD"
SINGLE_ISTD_LABELS = ("is1", "is2", "is3", "is4")
SINGLE_ISTD_MEAN_LABELS = ("is1", "is2", "is3")
SINGLE_FORMAT = "Einzelbestimmung"
FINGERPRINT_SHEET_NAME = "Fingerprint"
FINGERPRINT_PARAMETER_SHEET = "Parameter"
# Total Extraction rides on the Fingerprint sheet and only adds this column, so
# the header is the single thing that separates the two report inputs.
TOTAL_EXTRACTION_HEADER = "c [µg/L]"
TOTAL_EXTRACTION_FORMAT = "Total Extraction"
TOTAL_EXTRACTION_SUM_LABEL = "Summe c [µg/L]"
# Both fingerprint-shaped inputs carry their own quantity column and are never
# compared against SML values, so neither needs the CAS reference.
CAS_FREE_FORMATS = frozenset({"Fingerprint", TOTAL_EXTRACTION_FORMAT})
# §VII.9: when the analyst asks for the retention index, the intermediate
# workbook carries one more column. The header is exactly `gc_export.RI_HEADER`;
# it is repeated here rather than imported, because the launcher must keep
# running when gc_export is unavailable. Two shapes reach the report:
# `RI` directly right of `RT (min)`, or - with `replace_rt` - in the first
# column instead of the retention time.
RI_HEADER = "RI"
RI_NORMALIZED_HEADER = normalized_header(RI_HEADER)


def retention_index_value(value: Any) -> Optional[int]:
    """A retention index as a whole number, or None for an empty cell.

    §VII.9 wants an integer, and an *absent* index has to stay an empty cell:
    a 0 would read as an alkane-series index of zero, which is a measurement,
    not a blank.
    """
    number = numeric_value(value)
    return None if number is None else int(round(number))
DUPLICATE_INTERNAL_STANDARD_NAMES = {
    "perdeutero-heptadecane",
    "benzyl-butyl-phthalate-d4",
    "di-n-nonyl-phthalate-d4",
    "dibutyl phthalate-3,4,5,6-d4",
}


def is_duplicate_determination_workbook(workbook) -> bool:
    """Return True for the validated two-determination result format."""
    if DUPLICATE_SHEET_NAME not in workbook.sheetnames:
        return False
    ws = workbook[DUPLICATE_SHEET_NAME]
    headers = {normalized_header(ws.cell(1, col).value) for col in range(1, ws.max_column + 1)}
    return {"rtmean", "name", "cas", "mgkgmean"}.issubset(headers)


def is_single_determination_workbook(workbook) -> bool:
    """Return True for an Einzelbestimmung workbook with a per-row ISTD."""
    if SINGLE_SHEET_NAME not in workbook.sheetnames:
        return False
    ws = workbook[SINGLE_SHEET_NAME]
    headers = {normalized_header(ws.cell(1, col).value) for col in range(1, ws.max_column + 1)}
    return {"rtmin", "name", "cas", "mgkg", "istd"}.issubset(headers)


def is_fingerprint_workbook(workbook) -> bool:
    """Return True for a reviewed Fingerprint Screening workbook.

    The five substance columns are mandatory. The sixth is the row's position
    in the chromatogram, and §VII.9 allows two ways of writing it: the
    retention time, or - with `replace_rt` - the retention index in its place.
    Either identifies the row, so either satisfies the format; a workbook
    written before §VII.9 carries `RT (min)` and is accepted exactly as before.
    """
    if FINGERPRINT_SHEET_NAME not in workbook.sheetnames:
        return False
    ws = workbook[FINGERPRINT_SHEET_NAME]
    headers = {
        normalized_header(ws.cell(1, column).value)
        for column in range(1, ws.max_column + 1)
    }
    required = {"name", "cas", "qualitymatch", "pbmarea", "imblank"}
    if not required.issubset(headers):
        return False
    return "rtmin" in headers or RI_NORMALIZED_HEADER in headers


def is_total_extraction_workbook(workbook) -> bool:
    """Return True for a Fingerprint workbook carrying the concentration column.

    The Total Extraction export writes the same sheet name and the same headers
    as the Fingerprint export and appends ``c [µg/L]``. Discriminating on that
    column keeps every workbook written before spec v3.1 on the Fingerprint
    branch it has always taken.
    """
    if not is_fingerprint_workbook(workbook):
        return False
    ws = workbook[FINGERPRINT_SHEET_NAME]
    headers = {
        normalized_header(ws.cell(1, column).value)
        for column in range(1, ws.max_column + 1)
    }
    return normalized_header(TOTAL_EXTRACTION_HEADER) in headers


def duplicate_parameter_lookup(workbook) -> dict[str, Any]:
    """Read the name/value parameter table without relying on fixed row numbers."""
    if DUPLICATE_PARAMETER_SHEET not in workbook.sheetnames:
        return {}
    ws = workbook[DUPLICATE_PARAMETER_SHEET]
    result = {}
    for row in range(2, ws.max_row + 1):
        key = normalized_header(ws.cell(row, 1).value)
        if key:
            result[key] = ws.cell(row, 2).value
    return result


def duplicate_quality_lookup(workbook) -> dict[int, Optional[float]]:
    """Map each combined result row to the mean available library quality.

    The final sheet intentionally contains no quality column. Therefore quality is
    recovered from Bestimmung_1 and Bestimmung_2 using the final mean RT and the
    duplicate RT tolerance. This affects only the report's informational %match
    column; concentrations always come directly from mg/kg (mean).
    """
    result_ws = workbook[DUPLICATE_SHEET_NAME]
    parameters = duplicate_parameter_lookup(workbook)
    tolerance = numeric_value(parameters.get("duplicaterttolerance")) or 0.035
    detail_records = []
    # GCWS-PATCH: every Bestimmung_n (N-fold replicates), not only 1 and 2
    detail_sheets = sorted((n for n in workbook.sheetnames if re.fullmatch(r"Bestimmung_\d+", n)),
                           key=lambda n: int(n.split("_")[1]))
    for sheet_name in detail_sheets:
        if sheet_name not in workbook.sheetnames:
            continue
        ws = workbook[sheet_name]
        headers = {normalized_header(ws.cell(1, col).value): col for col in range(1, ws.max_column + 1)}
        rt_col = headers.get("rtmin") or headers.get("rt")
        quality_col = headers.get("quality") or headers.get("match")
        name_col = headers.get("name")
        cas_col = headers.get("cas")
        if not rt_col or not quality_col:
            continue
        for row in range(2, ws.max_row + 1):
            rt = numeric_value(ws.cell(row, rt_col).value)
            if rt is None:
                continue
            detail_records.append({
                "rt": rt,
                "quality": numeric_value(ws.cell(row, quality_col).value),
                "name": text(ws.cell(row, name_col).value) if name_col else "",
                "cas": normalize_cas(ws.cell(row, cas_col).value) if cas_col else "",
            })
    lookup = {}
    for row in range(2, result_ws.max_row + 1):
        rt = numeric_value(result_ws.cell(row, 1).value)
        if rt is None:
            continue
        raw_name = text(result_ws.cell(row, 2).value)
        raw_cas = text(result_ws.cell(row, 3).value)
        names = {part.strip().casefold() for part in raw_name.split(" / ") if part.strip()}
        cases = {normalize_cas(part) for part in raw_cas.split(" / ") if normalize_cas(part)}
        close = [record for record in detail_records if abs(record["rt"] - rt) <= tolerance + 1e-12]
        identity_close = [record for record in close if
                          (record["cas"] and record["cas"] in cases) or
                          (record["name"] and record["name"].casefold() in names)]
        selected = identity_close or close
        qualities = [record["quality"] for record in selected if record["quality"] is not None]
        lookup[row] = statistics.mean(qualities) if qualities else None
    return lookup



# openpyxl reads cached formula results but never writes them, so a freshly
# generated Doppelbestimmung workbook contains formulas without values. Desktop
# Excel is not available everywhere, therefore the report resolves the
# quantification chain itself. These patterns describe the references it walks.
_SINGLE_CELL_REFERENCE = re.compile(
    r"^=\s*'?([^'!]+)'?!\$?([A-Z]{1,3})\$?(\d+)\s*$", re.IGNORECASE)
_DETAIL_CELL_REFERENCE = re.compile(
    r"(Bestimmung_(\d+))!\$?([A-Z]{1,3})\$?(\d+)", re.IGNORECASE)


def resolve_workbook_number(workbook, value: Any, depth: int = 0) -> Optional[float]:
    """Return a number for a value or for a plain single-cell reference formula.

    Handles the two shapes the duplicate workbook uses, e.g. ``=Parameter!$B$13``
    and ``=Bestimmung_1!E17``. Anything more complex yields None; the callers
    fall back to their own arithmetic in that case.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if math.isnan(number) else number
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped.startswith("="):
        return numeric_value(stripped)
    # numeric_value() would happily read the "1" out of "=Bestimmung_1!E2", so
    # formulas are only ever resolved through the reference pattern.
    if depth >= 4:
        return None
    match = _SINGLE_CELL_REFERENCE.match(stripped)
    if not match:
        return None
    sheet_name, column, row = match.group(1).strip(), match.group(2), int(match.group(3))
    if sheet_name not in workbook.sheetnames:
        return None
    return resolve_workbook_number(
        workbook, workbook[sheet_name][f"{column.upper()}{row}"].value, depth + 1)


def duplicate_quantification_factors(workbook) -> dict[int, Optional[float]]:
    """Quantification factor per determination, as the workbook computes it.

    ``ISTD_n!B8``: mean c(IS1..IS3) * ISTD amount / 1000 / mean area(IS1..IS3)
    / cell area / coverage over the IS marked in ``Bestimmung_n``. Older
    workbooks: ``Standards_n!H``, i.e. concentration * amount / 1000 / FID area
    / cell area / coverage averaged over the "Quantification" standards.
    """
    parameters = duplicate_parameter_lookup(workbook)
    cell_area = numeric_value(parameters.get("cellarea"))
    coverage = numeric_value(parameters.get("coverage"))
    is_amount = numeric_value(parameters.get("internalstandardamount"))
    factors: dict[int, Optional[float]] = {}
    for number in (1, 2):
        sheet_name = f"Standards_{number}"
        factors[number] = None
        istd_sheet = f"{DUPLICATE_ISTD_SHEET_PREFIX}{number}"
        if istd_sheet in workbook.sheetnames and is_selectable_istd_sheet(
                workbook[istd_sheet]):
            # GC Workspace v3.2: any ISTD, a quantify flag and a mode.
            if cell_area and coverage and is_amount is not None:
                factors[number] = selectable_istd_factor(
                    workbook, f"Bestimmung_{number}", istd_sheet,
                    is_amount, cell_area, coverage)
            continue
        if istd_sheet in workbook.sheetnames:
            # Current layout: IS1..IS4 marked per row in Bestimmung_n, factor =
            # mean c(IS1..IS3) x amount / 1000 / mean area / cell area / coverage.
            mean_conc, mean_area = istd_mean(single_istd_table(
                workbook, f"Bestimmung_{number}", istd_sheet))
            if (mean_conc is not None and mean_area and cell_area and coverage
                    and is_amount is not None):
                factors[number] = (mean_conc * is_amount / 1000 / mean_area
                                   / cell_area / coverage)
            continue
        if (sheet_name not in workbook.sheetnames
                or not cell_area or not coverage or is_amount is None):
            continue
        ws = workbook[sheet_name]
        headers = {normalized_header(ws.cell(1, col).value): col
                   for col in range(1, ws.max_column + 1)}
        role_col = headers.get("role")
        area_col = headers.get("fidarea")
        concentration_col = headers.get("concentrationmgml")
        code_col = headers.get("istd")
        if not role_col or not area_col or not concentration_col:
            continue
        values = []
        usable = []
        mode = ""
        for row in range(2, ws.max_row + 1):
            label = text(ws.cell(row, 7).value).casefold()
            if label.startswith("quantification factor") or label.startswith(
                    "mean quantification factor"):
                mode = label
                continue
            if text(ws.cell(row, role_col).value).casefold() != "quantification":
                continue
            area = resolve_workbook_number(workbook, ws.cell(row, area_col).value)
            concentration = resolve_workbook_number(
                workbook, ws.cell(row, concentration_col).value)
            if not area or concentration is None:
                continue
            values.append(concentration * is_amount / 1000 / area / cell_area / coverage)
            code = text(ws.cell(row, code_col).value) if code_col else ""
            usable.append((code, concentration, area))
        # GC Workspace v3.2 names its mode in the mean row: the mean ISTD area
        # (mean c / mean area) or one reference ISTD. Older sheets average the
        # per-standard factors.
        if usable and "mean istd area" in mode:
            conc = statistics.mean(c for _code, c, _a in usable)
            area = statistics.mean(a for _code, _c, a in usable)
            factors[number] = conc * is_amount / 1000 / area / cell_area / coverage
        elif usable and "reference" in mode:
            wanted = mode.rsplit("reference", 1)[1].strip(" )").casefold()
            ref = next((u for u in usable if u[0].casefold() == wanted), usable[0])
            factors[number] = ref[1] * is_amount / 1000 / ref[2] / cell_area / coverage
        elif values:
            factors[number] = statistics.mean(values)
    return factors


def duplicate_concentration_fallback(
        workbook, ov_ratio: Optional[float]) -> dict[int, dict[str, Optional[float]]]:
    """Compute mg/kg per result row when the workbook holds uncalculated formulas.

    A number typed into an Area column wins over the linked detail sheet, so a
    manually corrected peak area reaches the report even when no spreadsheet
    application recalculated the file.
    """
    if not ov_ratio or DUPLICATE_SHEET_NAME not in workbook.sheetnames:
        return {}
    factors = duplicate_quantification_factors(workbook)
    if not any(factors.values()):
        return {}
    ws = workbook[DUPLICATE_SHEET_NAME]
    headers = {_duplicate_header_key(ws.cell(1, col).value): col
               for col in range(1, ws.max_column + 1) if ws.cell(1, col).value is not None}
    area_columns = {number: headers.get(f"area{number}") for number in (1, 2)}
    concentration_columns = {}
    for number in (1, 2):
        candidates = [col for key, col in headers.items()
                      if ("mgkg" in key or "concentration" in key)
                      and str(number) in key and "mean" not in key]
        concentration_columns[number] = min(candidates) if candidates else None

    fallback: dict[int, dict[str, Optional[float]]] = {}
    for row in range(2, ws.max_row + 1):
        values: dict[str, Optional[float]] = {}
        for number in (1, 2):
            factor = factors.get(number)
            area = None
            if area_columns[number]:
                area = resolve_workbook_number(
                    workbook, ws.cell(row, area_columns[number]).value)
            if area is None and concentration_columns[number]:
                # No area column, or an unresolved one: read the detail row from
                # the concentration formula itself and take its area.
                match = _DETAIL_CELL_REFERENCE.search(
                    text(ws.cell(row, concentration_columns[number]).value))
                if match:
                    detail_sheet = match.group(1)
                    detail_row = int(match.group(4))
                    if detail_sheet in workbook.sheetnames:
                        area = resolve_workbook_number(
                            workbook, workbook[detail_sheet][f"E{detail_row}"].value)
            values[f"conc{number}"] = (
                area * factor * ov_ratio if area is not None and factor else None)
        present = [value for value in values.values() if value is not None]
        values["mean"] = statistics.mean(present) if present else None
        fallback[row] = values
    return fallback


#: Header of the ``Quantify`` column that marks a GC Workspace ISTD sheet
#: (v3.2): any number of ISTDs with any label, each quantifying or not.
SELECTABLE_ISTD_QUANTIFY_HEADER = "quantify"
#: Column A of the row that states how the factor is formed.
SELECTABLE_ISTD_MODE_LABEL = "quantification mode"


def is_selectable_istd_sheet(ws) -> bool:
    return any(normalized_header(ws.cell(1, col).value) == SELECTABLE_ISTD_QUANTIFY_HEADER
               for col in range(1, ws.max_column + 1))


def selectable_istd_factor(workbook, main_sheet: str, istd_sheet: str,
                           is_amount: float, cell_area: float,
                           coverage: float) -> Optional[float]:
    """The factor of a GC Workspace ``ISTD_n`` sheet, computed without Excel.

    Rows 2.. until column A is empty are the ISTDs: label, c [mg/mL], FID area
    (a typed number, or the row marked with the label in ``Bestimmung_n``'s
    ISTD column) and ``Quantify`` (Ja/Nein). The ``Quantification mode`` row
    says ``mean ISTD area`` -- mean c / mean area over the quantifying ISTDs
    with both -- or ``reference`` with the label in column C.
    """
    ws = workbook[istd_sheet]
    headers = {normalized_header(ws.cell(1, col).value): col
               for col in range(1, ws.max_column + 1)}
    quantify_col = headers.get(SELECTABLE_ISTD_QUANTIFY_HEADER)
    main = workbook[main_sheet] if main_sheet in workbook.sheetnames else None
    areas_by_label: dict[str, Optional[float]] = {}
    if main is not None:
        main_headers = {normalized_header(main.cell(1, col).value): col
                        for col in range(1, main.max_column + 1)}
        label_col = main_headers.get("istd")
        area_col = main_headers.get("blankcorrectedfidarea")
        if label_col and area_col:
            for row in range(2, main.max_row + 1):
                key = text(main.cell(row, label_col).value).casefold()
                if key and key not in areas_by_label:
                    areas_by_label[key] = resolve_workbook_number(
                        workbook, main.cell(row, area_col).value)
    usable: list[tuple[str, float, float]] = []
    mode, reference = "mean istd area", ""
    for row in range(2, ws.max_row + 1):
        label = text(ws.cell(row, 1).value)
        if label.casefold() == SELECTABLE_ISTD_MODE_LABEL:
            mode = text(ws.cell(row, 2).value).casefold()
            reference = text(ws.cell(row, 3).value).casefold()
            continue
        # The mean and factor rows below the list carry no ``Quantify`` mark.
        if not label or text(ws.cell(row, quantify_col).value).casefold() not in (
                "ja", "yes", "x"):
            continue
        concentration = resolve_workbook_number(workbook, ws.cell(row, 2).value)
        area_value = ws.cell(row, 3).value
        area = resolve_workbook_number(workbook, area_value)
        if area is None and isinstance(area_value, str) and area_value.startswith("="):
            area = areas_by_label.get(label.casefold())
        if concentration and area and concentration > 0 and area > 0:
            usable.append((label.casefold(), concentration, area))
    if not usable:
        return None
    if mode.startswith("reference"):
        ref = next((u for u in usable if u[0] == reference), usable[0])
        conc, area = ref[1], ref[2]
    else:
        conc = statistics.mean(u[1] for u in usable)
        area = statistics.mean(u[2] for u in usable)
    return conc * is_amount / 1000 / area / cell_area / coverage


def single_istd_table(workbook, main_sheet: str = SINGLE_SHEET_NAME,
                      istd_sheet: str = SINGLE_ISTD_SHEET
                      ) -> dict[str, dict[str, Optional[float]]]:
    """``{IS label (casefold): {"concentration", "area"}}`` from an ISTD sheet.

    A typed number wins. The engine's area cell is a lookup into the main
    sheet (row marked with the label in ``ISTD`` -> blank-corrected FID area),
    which is repeated here for a workbook no spreadsheet application has
    calculated. The mean row below the labels is not part of the table.
    ``main_sheet``/``istd_sheet`` are ``Bestimmung_n``/``ISTD_n`` for a
    Doppelbestimmung, which marks its ISTD per determination.
    """
    if istd_sheet not in workbook.sheetnames or main_sheet not in workbook.sheetnames:
        return {}
    main = workbook[main_sheet]
    main_headers = {normalized_header(main.cell(1, col).value): col
                    for col in range(1, main.max_column + 1)}
    label_col = main_headers.get("istd")
    area_col = main_headers.get("blankcorrectedfidarea")
    areas_by_label: dict[str, Optional[float]] = {}
    if label_col and area_col:
        for row in range(2, main.max_row + 1):
            key = text(main.cell(row, label_col).value).casefold()
            if key and key not in areas_by_label:
                areas_by_label[key] = resolve_workbook_number(
                    workbook, main.cell(row, area_col).value)
    ws = workbook[istd_sheet]
    table: dict[str, dict[str, Optional[float]]] = {}
    for row in range(2, ws.max_row + 1):
        key = text(ws.cell(row, 1).value).casefold()
        if key not in SINGLE_ISTD_LABELS:
            continue
        area_value = ws.cell(row, 3).value
        area = resolve_workbook_number(workbook, area_value)
        if area is None and isinstance(area_value, str) and area_value.startswith("="):
            area = areas_by_label.get(key)
        table[key] = {
            "concentration": resolve_workbook_number(workbook, ws.cell(row, 2).value),
            "area": area,
        }
    return table


def istd_mean(table: dict[str, dict[str, Optional[float]]]
              ) -> tuple[Optional[float], Optional[float]]:
    """``(mean concentration, mean area)`` over the marked IS1..IS3.

    Only IS with an area count, as in the sheet's AVERAGEIF(S) formulas.
    """
    marked = [table[label] for label in SINGLE_ISTD_MEAN_LABELS
              if label in table and (table[label]["area"] or 0) > 0]
    mean_area = statistics.mean(x["area"] for x in marked) if marked else None
    concentrations = [x["concentration"] for x in marked if x["concentration"] is not None]
    mean_conc = statistics.mean(concentrations) if concentrations else None
    return mean_conc, mean_area


def single_concentration_fallback(workbook) -> dict[int, dict[str, Optional[float]]]:
    """mg/kg per row of an Einzelbestimmung sheet, computed without Excel.

    Mirrors the sheet's formula: area x mean c(IS1..IS3) x ISTD amount / 1000
    / mean area(IS1..IS3) / cell area / coverage x O/V. Only IS with an area
    count toward the means; with none marked no row has a concentration. A
    number typed into ``ISTD area`` or ``ISTD c`` on the row itself wins over
    the ISTD sheet, as it does in Excel.
    """
    if SINGLE_SHEET_NAME not in workbook.sheetnames:
        return {}
    parameters = duplicate_parameter_lookup(workbook)
    cell_area = numeric_value(parameters.get("cellarea"))
    coverage = numeric_value(parameters.get("coverage"))
    is_amount = numeric_value(parameters.get("internalstandardamount"))
    ov_ratio = numeric_value(parameters.get("ovratio"))
    if not cell_area or not coverage or is_amount is None or not ov_ratio:
        return {}
    ws = workbook[SINGLE_SHEET_NAME]
    headers = {normalized_header(ws.cell(1, col).value): col
               for col in range(1, ws.max_column + 1)}
    istd_col = headers.get("istd")
    area_col = headers.get("blankcorrectedfidarea")
    istd_area_col = headers.get("istdarea")
    istd_conc_col = headers.get("istdcmgml")
    if not istd_col or not area_col:
        return {}
    mean_conc, mean_area = istd_mean(single_istd_table(workbook))
    fallback: dict[int, dict[str, Optional[float]]] = {}
    for row in range(2, ws.max_row + 1):
        area = resolve_workbook_number(workbook, ws.cell(row, area_col).value)
        istd_area = (resolve_workbook_number(workbook, ws.cell(row, istd_area_col).value)
                     if istd_area_col else None)
        istd_conc = (resolve_workbook_number(workbook, ws.cell(row, istd_conc_col).value)
                     if istd_conc_col else None)
        istd_area = istd_area if istd_area is not None else mean_area
        istd_conc = istd_conc if istd_conc is not None else mean_conc
        value = None
        if area is not None and istd_area and istd_conc is not None:
            value = (area * istd_conc * is_amount / 1000 / istd_area
                     / cell_area / coverage * ov_ratio)
        fallback[row] = {"mean": value, "istd": text(ws.cell(row, istd_col).value)}
    return fallback


MIGRATION_PARAMETER_LABELS = {
    "syneris_summary_report_no": "Syneris-Summary Report-Nr.",
    "analyst": "Auswerter",
    "migration_cell": "Migrationszelle",
    "cell_area_dm2": "Zellfläche [dm²]",
    "occupancy": "Belegung",
    "occupancy_factor": "Belegungsfaktor",
    "effective_area_dm2": "Wirksame Fläche [dm²]",
    "volume_ml": "Volumen [mL]",
    "ov_ratio": "Oberfläche/Volumen [dm²/kg]",
    "duration": "Dauer",
    "temperature": "Temperatur",
    "simulant": "Simulans",
    "migrate_text": "Migrate",
}

# Units are only written for rows this module creates; the engine keeps its own
# notes. Derived rows are greyed out so blue always means "editable input".
MIGRATION_PARAMETER_UNITS = {
    "cell_area_dm2": "dm2",
    "effective_area_dm2": "dm2; cell area x coverage",
    "volume_ml": "mL",
    "ov_ratio": "dm2/kg",
    "migrate_text": "simulant | temperature | duration",
}

MIGRATION_SECTION_LABEL = "reportmetadata"

# Spec v2.1 §V.3: ``syneris_sample_no`` was typed by hand although the same
# 8-digit number is already derivable from the folder or sample name
# (``extract_syneris_number`` / ``syneris_number_from_text``), which is also how
# ``discover_duplicate_samples`` pairs the two determinations. The field is
# therefore no longer collected, no longer written and no longer part of the
# Word file name; it stays *readable* so a workbook or a settings file created
# before this change still loads. Accepted on input, dropped on output.
LEGACY_MIGRATION_PARAMETER_LABELS = {
    "syneris_sample_no": "Syneris-Sample-Nr.",
}

# Labels the engine already writes, plus the German labels of this module.
# Matching them keeps a UI value in the engine's own row instead of creating a
# second, conflicting entry — and lets an old sheet be read back by key.
MIGRATION_PARAMETER_ALIASES: dict[str, set[str]] = {
    "syneris_summary_report_no": {"synerissummaryreportnr", "summaryreportnr"},
    "syneris_sample_no": {"synerissamplenr", "samplenr"},
    "analyst": {"auswerter", "analyst"},
    "migration_cell": {"migrationszelle", "migrationcell"},
    "cell_area_dm2": {"zellflachedm2", "zellflache", "cellareadm2", "cellarea"},
    "occupancy": {"belegung", "occupancy"},
    "occupancy_factor": {"belegungsfaktor", "coverage", "occupancyfactor"},
    "effective_area_dm2": {"wirksameflachedm2", "effectiveareadm2"},
    "volume_ml": {"volumenml", "volumen", "volumeml"},
    "ov_ratio": {"oberflachevolumendm2kg", "ovratio", "oberflachevolumen"},
    "duration": {"dauer", "duration"},
    "temperature": {"temperatur", "temperature"},
    "simulant": {"simulans", "simulant"},
    "migrate_text": {"migrate"},
}
for _key, _label in {**MIGRATION_PARAMETER_LABELS,
                     **LEGACY_MIGRATION_PARAMETER_LABELS}.items():
    MIGRATION_PARAMETER_ALIASES.setdefault(_key, set()).add(normalized_header(_label))
del _key, _label

DERIVED_PARAMETER_LABELS = {normalized_header(label) for label in (
    "FID-MS delay determination 1", "FID-MS delay determination 2", "FID-MS delay",
    "Blank FID-MS delay", "Blank+ISTD FID-MS delay",
    "Blank file", "Blank+ISTD file", "Note",
    "Effective area dm2",
    MIGRATION_PARAMETER_LABELS["effective_area_dm2"],
    MIGRATION_PARAMETER_LABELS["migrate_text"],
)}


def parse_localized_float(value: Any, field_name: str = "Wert") -> float:
    value_text = text(value).replace(" ", "").replace(",", ".")
    try:
        number = float(value_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} muss eine Zahl sein.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} muss eine endliche Zahl sein.")
    return number


def format_decimal_de(value: Any, decimals: int = 1) -> str:
    number = numeric_value(value)
    return "" if number is None else f"{number:.{decimals}f}".replace(".", ",")


def format_migrate_text(metadata: dict[str, Any]) -> str:
    return " | ".join(text(metadata.get(key)) for key in ("simulant", "temperature", "duration")
                      if text(metadata.get(key)))


def validate_migration_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result = dict(metadata)
    # Retired fields are tolerated on input so an old workbook, an old audit
    # payload or an old settings file still loads, and dropped here so they can
    # never be written back out again (spec v2.1 §V.3).
    for legacy_key in LEGACY_MIGRATION_PARAMETER_LABELS:
        result.pop(legacy_key, None)
    required_text = {
        "analyst": "Auswerter", "migration_cell": "Migrationszelle",
        "occupancy": "Belegung", "duration": "Dauer",
        "temperature": "Temperatur", "simulant": "Simulans",
    }
    missing = [label for key, label in required_text.items() if not text(result.get(key))]
    if missing:
        raise ValueError("Bitte folgende Pflichtangaben ergänzen: " + ", ".join(missing))
    for key, label in (("cell_area_dm2", "Zellfläche"), ("occupancy_factor", "Belegungsfaktor"),
                       ("volume_ml", "Volumen"), ("ov_ratio", "Oberfläche/Volumen")):
        result[key] = parse_localized_float(result.get(key), label)
        if result[key] <= 0:
            raise ValueError(f"{label} muss größer als 0 sein.")
    result["effective_area_dm2"] = result["cell_area_dm2"] * result["occupancy_factor"]
    result["migrate_text"] = format_migrate_text(result)
    result["schema_version"] = 1
    return result


# --- Migration metadata as calculation input (spec v2.1 §V.3) ----------------
#
# The metadata below is not report decoration: every one of these fields is a
# divisor, a factor or a threshold inside AutoLib's quantification chain
# (§11.3), so a change to any of them has to reach the ``Settings`` object the
# workspace recalculates from — not just the Parameter sheet.
#
#     factor_i    = conc_i * is_amount / 1000 / area_i / cell_area / coverage
#     mean_factor = mean(factor_1, factor_2, factor_3)
#     mg/dm2      = area * mean_factor
#     mg/kg       = mg/dm2 * ov_ratio
#
# Left-hand key: metadata field. Right-hand: attribute on ``AutoLib.Settings``.
MIGRATION_SETTINGS_FIELDS: dict[str, str] = {
    "cell_area_dm2": "cell_area_dm2",
    "occupancy_factor": "coverage",
    "ov_ratio": "ov_ratio",
    "is_amount": "is_amount",
    "fc17_conc": "fc17_conc",
    "bbp_conc": "bbp_conc",
    "dnnp_conc": "dnnp_conc",
    "quality_limit": "quality_limit",
    "solvent_end": "solvent_end",
    "rt_tolerance": "rt_tolerance",
    "qc_min_area": "qc_min_area",
    "blank_rt_tolerance": "blank_rt_tolerance",
}

# ``quality_limit`` is an int on ``Settings``; everything else is a float.
MIGRATION_SETTINGS_INT_FIELDS = frozenset({"quality_limit"})

# The reporting limit decides which rows are flagged below limit. It is *not* a
# ``Settings`` field — AutoLib fixes it at 0.01 mg/kg when it writes the
# Parameter sheet — so it travels with the metadata and is applied downstream.
MIGRATION_REPORTING_LIMIT_KEY = "reporting_limit"
DEFAULT_REPORTING_LIMIT = 0.01

# Documentation-only fields: they flow into the Parameter sheet and the Word
# report unchanged and never touch a number.
MIGRATION_DOCUMENTATION_FIELDS = (
    "syneris_summary_report_no", "analyst", "migration_cell", "occupancy",
    "volume_ml", "effective_area_dm2", "duration", "temperature", "simulant",
    "migrate_text",
)


def migration_settings_kwargs(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the ``AutoLib.Settings`` keyword arguments a metadata set implies.

    Only fields actually present are returned, so a partial per-analysis
    override leaves every other setting at the batch default.
    """
    kwargs: dict[str, Any] = {}
    for key, attribute in MIGRATION_SETTINGS_FIELDS.items():
        if key not in metadata:
            continue
        number = numeric_value(metadata[key])
        if number is None:
            continue
        kwargs[attribute] = int(round(number)) if key in MIGRATION_SETTINGS_INT_FIELDS else float(number)
    return kwargs


def _parameter_validator():
    """Return ``gc_fid.apply_setting`` if the workspace modules are installed.

    The Parameter panel validates every edit through that one function
    (``gc_fid.PARAMETER_BOUNDS``), so routing through it is what keeps the
    dialog and the panel from disagreeing about what a legal cell area is.
    It is optional: the batch tabs run without matplotlib or tksheet.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gc_fid
    except Exception:
        return None
    return getattr(gc_fid, "apply_setting", None)


def apply_migration_metadata_to_settings(settings: Any, metadata: dict[str, Any]) -> Any:
    """Write the metadata's calculation inputs onto an ``AutoLib.Settings``.

    Mutates in place and returns the same object, so the workspace keeps the
    identity of the settings instance it recalculates from and a parameter edit
    is seen by everything holding a reference to it. Calling
    ``NiasSample.apply_settings(settings)`` afterwards is what runs the §V.1
    factor chain; this function only supplies the inputs.
    """
    values = migration_settings_kwargs(metadata)
    reporting_limit = numeric_value(metadata.get(MIGRATION_REPORTING_LIMIT_KEY))
    if reporting_limit is not None:
        # Not an AutoLib dataclass field, but gc_fid treats it as one
        # (``default_settings``/``settings_as_dict``) and ``recalculate`` reads
        # it to flag rows below the reporting limit.
        values[MIGRATION_REPORTING_LIMIT_KEY] = float(reporting_limit)
    validate = _parameter_validator()
    for attribute, value in values.items():
        if validate is None:
            setattr(settings, attribute, value)
            continue
        try:
            validate(settings, attribute, value)
        except ValueError:
            # An out-of-bounds number must not silently become the setting the
            # calculation runs on; the previous value stands and the Parameter
            # panel shows it.
            raise ValueError(
                f"{MIGRATION_PARAMETER_LABELS.get(attribute, attribute)}: "
                f"{value} liegt außerhalb des zulässigen Bereichs.") from None
    return settings


def migration_metadata_from_settings(settings: Any) -> dict[str, Any]:
    """Read the calculation inputs back off a ``Settings`` object.

    Used to seed the batch defaults from whatever the parameter grid currently
    shows, so the dialog and the grid can never disagree about a number.
    """
    metadata: dict[str, Any] = {}
    for key, attribute in MIGRATION_SETTINGS_FIELDS.items():
        if hasattr(settings, attribute):
            metadata[key] = getattr(settings, attribute)
    if hasattr(settings, MIGRATION_REPORTING_LIMIT_KEY):
        metadata[MIGRATION_REPORTING_LIMIT_KEY] = getattr(settings, MIGRATION_REPORTING_LIMIT_KEY)
    return metadata


MIGRATION_METADATA_SETTING = "last_migration_metadata"


def load_migration_metadata_defaults() -> dict[str, Any]:
    """Return the metadata of the last run, ready to prefill the dialog.

    Retired keys in an old settings file are dropped rather than resurrected.
    """
    stored = load_user_settings().get(MIGRATION_METADATA_SETTING)
    if not isinstance(stored, dict):
        return {}
    return {key: value for key, value in stored.items()
            if key not in LEGACY_MIGRATION_PARAMETER_LABELS}


def save_migration_metadata_defaults(metadata: dict[str, Any]) -> None:
    """Persist the whole metadata set as the prefill for the next batch."""
    payload = {key: value for key, value in metadata.items()
               if key not in LEGACY_MIGRATION_PARAMETER_LABELS}
    save_user_settings(**{MIGRATION_METADATA_SETTING: payload})


class MigrationMetadataScope:
    """Batch defaults plus per-analysis overrides (spec v2.1 §V.3).

    The dialog is filled once per batch; its values become the defaults of every
    analysis in it. A deviation for a single analysis — a different cell area for
    one sample, say — is an override stored under that analysis' key, so the
    Parameter panel in the workspace stays editable per analysis without ever
    rewriting the batch default. ``for_analysis`` is what the calculation reads.
    """

    def __init__(self, defaults: Optional[dict[str, Any]] = None) -> None:
        self.defaults: dict[str, Any] = dict(defaults or {})
        self.overrides: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(analysis_key: Any) -> str:
        return text(analysis_key).strip().casefold()

    def for_analysis(self, analysis_key: Any = "") -> dict[str, Any]:
        """Return the effective metadata of one analysis: defaults + overrides."""
        merged = dict(self.defaults)
        merged.update(self.overrides.get(self._key(analysis_key), {}))
        return merged

def write_migration_metadata_to_workbook(workbook_path: Path, metadata: dict[str, Any]) -> None:
    """Populate and format the existing Parameter sheet from the UI metadata.

    The engine already wrote an English three-column sheet whose rows 9 to 15
    are referenced absolutely by every concentration formula
    (``Parameter!$B$9`` and following). Rows are therefore only updated or
    appended here, never inserted, moved or deleted.
    """
    metadata = validate_migration_metadata(metadata)
    workbook = load_workbook(workbook_path)
    try:
        _apply_migration_metadata(workbook, metadata)
    except BaseException:
        workbook.close()
        raise
    _save_recalculating(workbook, Path(workbook_path))


def _apply_migration_metadata(workbook, metadata: dict[str, Any]) -> None:
    """The body of :func:`write_migration_metadata_to_workbook`, on a loaded book."""
    if DUPLICATE_PARAMETER_SHEET in workbook.sheetnames:
        ws = workbook[DUPLICATE_PARAMETER_SHEET]
    else:
        ws = workbook.create_sheet(DUPLICATE_PARAMETER_SHEET)
        ws.append(["Parameter", "Value", "Unit / note"])

    # Labels the engine already writes (MIGRATION_PARAMETER_ALIASES). Matching
    # them keeps the UI value in the engine's own row instead of creating a
    # second, conflicting entry. The map still carries the retired
    # ``syneris_sample_no`` so an old sheet can be recognised; because that key
    # is no longer in MIGRATION_PARAMETER_LABELS the loop below never writes it.
    aliases = {key: set(names) for key, names in MIGRATION_PARAMETER_ALIASES.items()}

    existing: dict[str, int] = {}
    for row in range(2, ws.max_row + 1):
        normalized = normalized_header(ws.cell(row, 1).value)
        if normalized:
            existing[normalized] = row

    header_fill = PatternFill("solid", fgColor="1F4E78")
    input_font = Font(color="0000FF")
    derived_font = Font(color="808080", italic=True)
    for column, title in ((1, "Parameter"), (2, "Value"), (3, "Unit / note")):
        cell = ws.cell(1, column, title)
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    resolved = {
        key: next((existing[name] for name in aliases.get(key, ()) if name in existing), None)
        for key in MIGRATION_PARAMETER_LABELS
    }
    # New rows go below a separator so the report metadata is visibly a block of
    # its own, and so no existing row number ever shifts.
    if MIGRATION_SECTION_LABEL not in existing and any(
            row is None for row in resolved.values()):
        separator = ws.max_row + 1
        ws.cell(separator, 1, "Report metadata")
        ws.cell(separator, 1).fill = header_fill
        ws.cell(separator, 1).font = Font(color="FFFFFF", bold=True)
        existing[MIGRATION_SECTION_LABEL] = separator

    for key, label in MIGRATION_PARAMETER_LABELS.items():
        row = resolved[key]
        appended = row is None
        if appended:
            row = ws.max_row + 1
            ws.cell(row, 1, label)
            existing[normalized_header(label)] = row
        value_cell = ws.cell(row, 2)
        value_cell.value = metadata.get(key, "")
        value_cell.alignment = Alignment(horizontal="left", vertical="center")
        if key in {"cell_area_dm2", "effective_area_dm2", "ov_ratio"}:
            value_cell.number_format = "0.00"
        elif key == "volume_ml":
            value_cell.number_format = "0.0"
        # Only rows created here get a unit; the engine's own notes stay untouched.
        if appended and MIGRATION_PARAMETER_UNITS.get(key):
            ws.cell(row, 3, MIGRATION_PARAMETER_UNITS[key])

    # Blue means editable. Values the workbook derives or merely records are grey,
    # so it is obvious which cells may be changed and recalculated.
    for row in range(2, ws.max_row + 1):
        label = normalized_header(ws.cell(row, 1).value)
        if not label or label == MIGRATION_SECTION_LABEL:
            continue
        ws.cell(row, 2).font = copy(
            derived_font if label in DERIVED_PARAMETER_LABELS else input_font)
        ws.cell(row, 3).font = copy(derived_font)

    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 44
    ws.column_dimensions["C"].width = 46
    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False
    # No auto filter: a parameter list is not a data table, and a sheet-level
    # filter next to Excel tables is a known cause of repair prompts.
    ws.auto_filter.ref = None

    payload = read_audit_payload(workbook)
    payload.setdefault("fields", {})
    payload["migration_metadata"] = metadata
    payload["fields"].update({
        "Analyst": metadata["analyst"],
        "Migrate": metadata["migrate_text"],
        "Migration cell": metadata["migration_cell"],
        "Cell area dm2": metadata["cell_area_dm2"],
        "Occupancy": metadata["occupancy"],
        "Occupancy factor": metadata["occupancy_factor"],
        "Effective area dm2": metadata["effective_area_dm2"],
        "Volume mL": metadata["volume_ml"],
        "S/V ratio dm2/kg": metadata["ov_ratio"],
    })
    attach_audit_payload(workbook, payload)


def copy_duplicate_report_metadata(workbook, target_ws, last_column: int = 9,
                                   single: bool = False) -> None:
    """Write the usual NIAS header using metadata available in the new workbook."""
    parameters = duplicate_parameter_lookup(workbook)
    title_fill = PatternFill("solid", fgColor="BA0C2F")
    target_ws["A1"] = "GC-MS/FID – NIAS-Screening –"
    target_ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
    target_ws["A1"].font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    target_ws["A1"].fill = title_fill
    target_ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    target_ws["A2"] = ("PA 26.007, single determination" if single
                       else "PA 26.007, double determination")
    target_ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_column)
    target_ws["A2"].font = Font(name="Arial", size=8, bold=True, italic=True)
    target_ws["A3"] = "Sample:"
    target_ws["B3"] = text(parameters.get("samplename"))
    target_ws.merge_cells("B3:F3")
    target_ws["G3"] = "Analyst:"
    target_ws["H3"] = text(parameters.get("auswerter") or parameters.get("analyst"))
    target_ws["A4"] = "Migrate:"
    migrate_value = text(parameters.get("migrate"))
    if not migrate_value:
        migrate_value = " | ".join(value for value in (
            text(parameters.get("simulans")), text(parameters.get("temperatur")),
            text(parameters.get("dauer"))) if value)
    target_ws["B4"] = migrate_value
    target_ws.merge_cells("B4:D4")
    target_ws["E4"] = "S/V-ratio"
    target_ws["F4"] = text(parameters.get("oberflchevolumendm2kg") or parameters.get("ovratio"))
    target_ws["G4"] = "Operator:"
    target_ws["H4"] = ""
    labels = {"A3", "G3", "A4", "E4", "G4"}
    for row in range(2, 5):
        target_ws.row_dimensions[row].height = 13
        for column in range(1, last_column + 1):
            cell = target_ws.cell(row, column)
            if cell.coordinate != "A2":
                cell.font = Font(name="Arial", size=8, color="000000")
            if cell.coordinate in labels:
                cell.font = Font(name="Arial", size=8, color="000000")
            cell.alignment = Alignment(horizontal="left", vertical="center")


def select_source_sheet(workbook, requested_sheet: Optional[str]):
    """Select the populated NIAS evaluation sheet with its header in row 27.

    Several template sheets in the supplied workbook have identical headers in
    row 27 but contain no real data. Therefore selection is based primarily on
    populated data rows, not merely on the header text.
    """
    if requested_sheet:
        if requested_sheet not in workbook.sheetnames:
            raise ValueError(f"Worksheet not found: {requested_sheet}")
        return workbook[requested_sheet]

    if is_fingerprint_workbook(workbook):
        return workbook[FINGERPRINT_SHEET_NAME]

    # Prefer the new, already evaluated duplicate-determination result.
    if is_duplicate_determination_workbook(workbook):
        return workbook[DUPLICATE_SHEET_NAME]
    if is_single_determination_workbook(workbook):
        return workbook[SINGLE_SHEET_NAME]

    # Preserve the established legacy workflow unchanged.
    for worksheet_name in workbook.sheetnames:
        if worksheet_name.strip().casefold() == "auswertung":
            return workbook[worksheet_name]

    raise ValueError(
        "Weder 'Auswertung' noch ein gültiges Worksheet 'Doppelbestimmung', "
        "'Einzelbestimmung' oder 'Fingerprint' wurde in der NIAS-Datei gefunden."
    )



def is_excel_file_open(file_path: Path) -> bool:
    """Detect whether an Excel workbook is currently open/locked on Windows."""
    file_path = file_path.resolve()
    # Excel normally creates a temporary owner file while a workbook is open.
    owner_file = file_path.with_name(f"~${file_path.name}")
    if owner_file.exists():
        return True

    if os.name != "nt":
        return False

    try:
        import msvcrt
        with open(file_path, "r+b") as handle:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            finally:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
    except PermissionError:
        return True
    except OSError:
        # Do not classify unrelated filesystem errors as an Excel lock.
        return False
    return False


def detect_workbook_format(file_path: Path) -> str:
    """Identify a report input without relying on its filename."""
    workbook = None
    try:
        workbook = load_workbook(file_path, read_only=True, data_only=True)
        # A total extraction workbook is a Fingerprint workbook with one extra
        # column, so it has to be recognised first or it would never be named
        # (spec v3.1 §VII.10). Neither format needs a CAS reference.
        if is_total_extraction_workbook(workbook):
            return TOTAL_EXTRACTION_FORMAT
        if is_fingerprint_workbook(workbook):
            return "Fingerprint"
        if is_duplicate_determination_workbook(workbook):
            return "Doppelbestimmung"
        if is_single_determination_workbook(workbook):
            return SINGLE_FORMAT
        if any(name.strip().casefold() == "auswertung" for name in workbook.sheetnames):
            return "Legacy"
    except Exception:
        return "Unbekannt"
    finally:
        if workbook is not None:
            workbook.close()
    return "Unbekannt"


def ensure_excel_files_closed(paths: list[Path]) -> None:
    """Raise a clear error when one or more input workbooks are open in Excel."""
    open_paths = [path for path in paths if is_excel_file_open(path)]
    if open_paths:
        names = "\n".join(f"- {path.name}" for path in open_paths)
        raise PermissionError(
            "Mindestens eine ausgewählte Excel-Datei ist noch in Microsoft Excel geöffnet:\n"
            f"{names}\n\nBitte die Datei(en) schließen und danach erneut auf 'Prozessieren' klicken."
        )


def workbook_parameter_lookup(workbook, sheet_name: str = "Parameter") -> dict[str, Any]:
    """Read a two-column parameter sheet using normalized labels."""
    if sheet_name not in workbook.sheetnames:
        return {}
    ws = workbook[sheet_name]
    result = {}
    for row in range(2, ws.max_row + 1):
        key = normalized_header(ws.cell(row, 1).value)
        if key:
            result[key] = ws.cell(row, 2).value
    return result


AUDIT_DATA_SHEET = "_AuditData"


def attach_audit_payload(workbook, payload: dict[str, Any]) -> None:
    if AUDIT_DATA_SHEET in workbook.sheetnames:
        del workbook[AUDIT_DATA_SHEET]
    ws = workbook.create_sheet(AUDIT_DATA_SHEET)
    ws["A1"] = json.dumps(payload, ensure_ascii=False)
    ws.sheet_state = "veryHidden"


def read_audit_payload(workbook) -> dict[str, Any]:
    if AUDIT_DATA_SHEET not in workbook.sheetnames:
        return {}
    try:
        return json.loads(text(workbook[AUDIT_DATA_SHEET]["A1"].value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def copy_fingerprint_report_metadata(workbook, target_ws, last_column: int = 5) -> str:
    """Write the NIAS title band using Fingerprint parameter metadata."""
    parameters = workbook_parameter_lookup(workbook, FINGERPRINT_PARAMETER_SHEET)
    sample_name = text(parameters.get("probe")) or text(workbook.properties.title)
    sample_name = re.sub(r"\s*-\s*Fingerprint Screening\s*$", "", sample_name,
                         flags=re.IGNORECASE)
    title_fill = PatternFill("solid", fgColor="BA0C2F")
    target_ws["A1"] = "GC-MS/FID – NIAS-Screening –"
    target_ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_column)
    target_ws["A1"].font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    target_ws["A1"].fill = title_fill
    target_ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    target_ws.row_dimensions[1].height = 18
    target_ws["A2"] = "Fingerprint Screening; Area %"
    target_ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_column)
    target_ws["A2"].font = Font(name="Arial", size=8, bold=True, italic=True)
    target_ws["A3"] = "Sample:"
    target_ws["B3"] = sample_name
    target_ws.merge_cells(start_row=3, start_column=2, end_row=3, end_column=last_column)
    target_ws["A4"] = "Extraction"
    target_ws["B4"] = ""
    target_ws.merge_cells(start_row=4, start_column=2, end_row=4, end_column=last_column)
    for row in range(2, 5):
        target_ws.row_dimensions[row].height = 13
        for column in range(1, last_column + 1):
            cell = target_ws.cell(row, column)
            if cell.coordinate != "A2":
                cell.font = Font(name="Arial", size=8, color="000000")
            cell.alignment = Alignment(horizontal="left", vertical="center")
    return sample_name


#: Report column widths by role. `ri` is the only addition of §VII.9; every
#: other width is the one the five-column report has always written, so a
#: report without a retention index comes out with the same widths as before.
FINGERPRINT_COLUMN_WIDTHS = {"rt": 9, "ri": 8, "name": 43, "cas": 14,
                             "match": 10, "quantity": 12}


def fingerprint_report_layout(source_headers: dict[str, int],
                              quantity_header: str) -> dict[str, Any]:
    """Titles, roles and positions of the Fingerprint-shaped report table.

    Three shapes, decided purely by what the intermediate workbook carries
    (§VII.9, §VII.10):

    * no `RI` column -- the five columns both reports have always written,
      unchanged down to the widths and the print area;
    * `RT (min)` and `RI` -- six columns, `RI` directly right of `RT (min)`;
    * `RI` only (`replace_rt`) -- five columns again, the first one headed
      `RI` and carrying the index.

    The quantity column is always last, which is what keeps `c [µg/L]` behind
    any retention index on a total-extraction report.
    """
    roles: list[str] = []
    titles: list[str] = []
    if "rtmin" in source_headers:
        roles.append("rt")
        titles.append("RT (min)")
    if RI_NORMALIZED_HEADER in source_headers:
        roles.append("ri")
        titles.append(RI_HEADER)
    roles += ["name", "cas", "match", "quantity"]
    titles += ["Name", "CAS-No.", "% match", quantity_header]
    positions = {role: index for index, role in enumerate(roles, start=1)}
    last_column = len(roles)
    return {
        "roles": roles,
        "titles": titles,
        "positions": positions,
        "last_column": last_column,
        "last_letter": get_column_letter(last_column),
        "widths": {positions[role]: FINGERPRINT_COLUMN_WIDTHS[role]
                   for role in roles},
    }


def process_fingerprint_report(workbook, nias_path: Path, output_path: Path) -> dict[str, Any]:
    """Create a reviewed Fingerprint report without SML or concentration logic."""
    source_ws = workbook[FINGERPRINT_SHEET_NAME]
    headers = {
        normalized_header(source_ws.cell(1, column).value): column
        for column in range(1, source_ws.max_column + 1)
    }
    columns = {
        # Either of the two may be absent: §VII.9 lets the retention index
        # replace the retention time, and without §VII.9 there is no index.
        "rt": headers.get("rtmin"),
        "ri": headers.get(RI_NORMALIZED_HEADER),
        "name": headers["name"],
        "cas": headers["cas"],
        "match": headers["qualitymatch"],
        "area_pct": headers["pbmarea"],
        "blank": headers["imblank"],
    }
    retained_rows = []
    pubchem_cache, pubchem_checked_at = load_pubchem_cache()
    cached_before_run = set(pubchem_cache)
    pubchem_names_found = 0
    pubchem_no_hits = 0
    pubchem_api_queries: set[str] = set()
    blank_matches = 0
    for row in range(2, source_ws.max_row + 1):
        rt = (source_ws.cell(row, columns["rt"]).value
              if columns["rt"] else None)
        ri = (retention_index_value(source_ws.cell(row, columns["ri"]).value)
              if columns["ri"] else None)
        name = text(source_ws.cell(row, columns["name"]).value)
        raw_cas = text(source_ws.cell(row, columns["cas"]).value)
        match = source_ws.cell(row, columns["match"]).value
        area_pct = numeric_value(source_ws.cell(row, columns["area_pct"]).value)
        blank_value = text(source_ws.cell(row, columns["blank"]).value)
        if (not name and not raw_cas and numeric_value(rt) is None
                and ri is None and area_pct is None):
            continue

        name_lookup_failed = False
        if not name:
            normalized_cas_for_lookup = normalize_cas(raw_cas)
            if is_valid_cas_number(normalized_cas_for_lookup):
                if normalized_cas_for_lookup not in pubchem_cache:
                    pubchem_api_queries.add(normalized_cas_for_lookup)
                preferred_name = pubchem_preferred_name(
                    normalized_cas_for_lookup, pubchem_cache, pubchem_checked_at)
                if preferred_name:
                    name = preferred_name
                    pubchem_names_found += 1
                else:
                    name = PUBCHEM_NO_HIT_TEXT
                    name_lookup_failed = True
                    pubchem_no_hits += 1
        is_blank = blank_value.casefold() in {"ja", "yes", "true", "1", "x"}
        if is_blank:
            blank_matches += 1
        retained_rows.append({
            "rt": rt,
            "ri": ri,
            "name": name,
            "cas": normalize_cas(raw_cas) or None,
            "match": match,
            "area_pct": area_pct,
            "blank": "Ja" if is_blank else "Nein",
            "name_lookup_failed": name_lookup_failed,
            "summary": False,
            "unknown_summary": False,
            "substance_summary": False,
        })

    if set(pubchem_cache) != cached_before_run:
        save_pubchem_cache(pubchem_cache, pubchem_checked_at)

    original_area_pct_total = normalize_fingerprint_area_percentages(retained_rows)
    unknown_names_for_documentation = [
        unidentified_register_name(item) for item in retained_rows
        if is_unidentified_item(item)
    ]
    # Captured before the rows are regrouped, so every single occurrence reaches
    # the register instead of only the aggregated sum rows.
    unknown_rows = [item for item in retained_rows
                    if unknown_mz_ranked(item.get("name"))]
    category_sums = {"hydrocarbon": 0.0, "siloxane": 0.0}
    category_members = {"hydrocarbon": 0, "siloxane": 0}
    uncategorized_rows = []
    for item in retained_rows:
        category = classify_name(item["name"])
        if category in category_sums:
            category_sums[category] += item["area_pct"]
            category_members[category] += 1
        else:
            uncategorized_rows.append(item)
    retained_rows, repeated_substances = split_repeated_substance_groups(uncategorized_rows)
    for key, items in repeated_substances.items():
        representative = items[0]
        is_unknown = key[0] == "unknown"
        retained_rows.append({
            "rt": None,
            "ri": None,
            "name": repeated_substance_summary_label(key, items),
            "cas": None,
            "match": None,
            "area_pct": round(sum(item["area_pct"] for item in items), 4),
            "blank": "Ja" if any(item["blank"] == "Ja" for item in items) else "Nein",
            "name_lookup_failed": False,
            "summary": True,
            "unknown_summary": is_unknown,
            "substance_summary": not is_unknown,
        })
    for category in ("hydrocarbon", "siloxane"):
        if category_members[category]:
            retained_rows.append({
                "rt": None,
                "ri": None,
                "name": SUMMARY_LABELS[category],
                "cas": None,
                "match": None,
                "area_pct": round(category_sums[category], 4),
                "blank": "Nein",
                "name_lookup_failed": False,
                "summary": True,
                "unknown_summary": False,
                "substance_summary": False,
            })

    # CAS/name control disabled. CAS values remain for lookup and reporting.
    cas_verification_counts, cas_verification_details = {}, []

    output_workbook = Workbook()
    target_ws = output_workbook.active
    target_ws.title = "NIAS Result"
    target_ws.sheet_view.showGridLines = False
    # §VII.9: the table grows by one column when the intermediate carries a
    # retention index, and keeps its five columns when it does not.
    layout = fingerprint_report_layout(headers, "Area %")
    last_column = layout["last_column"]
    last_letter = layout["last_letter"]
    name_column = layout["positions"]["name"]
    sample_name = copy_fingerprint_report_metadata(workbook, target_ws, last_column)
    for column, header in enumerate(layout["titles"], start=1):
        cell = target_ws.cell(5, column, header)
        cell.font = Font(name="Arial", size=8, bold=True, color="000000")
        cell.fill = PatternFill("solid", fgColor="FFFFFF")
        cell.border = Border(bottom=Side(style="thin", color="000000"))
        cell.alignment = Alignment(horizontal="center", vertical="center")

    summary_separator_added = False
    for output_row, item in enumerate(retained_rows, start=6):
        for column, role in enumerate(layout["roles"], start=1):
            value = item["area_pct"] if role == "quantity" else item[role]
            cell = target_ws.cell(output_row, column, value)
            cell.font = Font(name="Arial", size=8, color="000000")
            cell.alignment = Alignment(
                vertical="top" if role == "name" else "center",
                wrap_text=role == "name",
                horizontal="right" if role in {"match", "quantity"} else "left")
            if role == "rt":
                cell.number_format = "0.0000"
            elif role == "ri":
                # A whole number, and an absent index stays an empty cell.
                cell.number_format = "0"
            elif role == "quantity":
                cell.number_format = "0.0000"
        if item["name_lookup_failed"]:
            target_ws.cell(output_row, name_column).font = Font(
                name="Arial", size=8, bold=True, color="FF0000")
        if item["summary"]:
            for column in range(1, last_column + 1):
                summary_cell = target_ws.cell(output_row, column)
                summary_cell.font = Font(name="Arial", size=8, bold=True, color="000000")
                if not summary_separator_added:
                    summary_cell.border = Border(top=Side(style="thin", color="000000"))
            summary_separator_added = True

    if not retained_rows:
        target_ws["A6"] = "No fingerprint peak remains after manual review."
        target_ws.merge_cells(f"A6:{last_letter}6")
        target_ws["A6"].font = Font(name="Arial", size=8, italic=True)

    last_data_row = max(6, 5 + len(retained_rows))
    for column, width in layout["widths"].items():
        target_ws.column_dimensions[get_column_letter(column)].width = width
    target_ws.freeze_panes = "A6"
    if retained_rows:
        target_ws.auto_filter.ref = f"A5:{last_letter}{last_data_row}"
    target_ws.print_title_rows = "1:5"
    target_ws.page_setup.orientation = "landscape"
    target_ws.page_setup.fitToWidth = 1
    target_ws.page_setup.fitToHeight = 0
    target_ws.sheet_properties.pageSetUpPr.fitToPage = True
    target_ws.page_margins.left = 0
    target_ws.page_margins.right = 0
    target_ws.page_margins.top = 0
    target_ws.page_margins.bottom = 0
    target_ws.print_area = f"A1:{last_letter}{last_data_row}"

    processed_at = datetime.now().astimezone()
    attach_audit_payload(output_workbook, {
        "fields": {
            "Processor version": SCRIPT_VERSION,
            "Processed at": processed_at.isoformat(timespec="seconds"),
            "Source format": "Fingerprint",
            "Source file": str(nias_path.resolve()),
            "Source worksheet": source_ws.title,
            "Output file": str(output_path.resolve()),
            "Sample": sample_name,
            "Quantity basis": "PBM Area %; recalculated to 100% after manual review",
            "Original retained Area % total": round(original_area_pct_total, 4),
            "Final Area % total": round(sum(item["area_pct"] for item in retained_rows), 4),
            "Reported rows": len(retained_rows),
            "Summary rows": sum(1 for item in retained_rows if item.get("summary")),
            "Category counts": category_members,
            "Blank matches": blank_matches,
            "Unidentified source rows registered": len(unknown_names_for_documentation),
        },
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    from gc_report_layout import fit_report_rows
    fit_report_rows(target_ws)
    output_workbook.save(output_path)
    register_number = syneris_number_from_text(sample_name, nias_path.stem)
    unknown_register_result = safe_record_unknown_sightings(
        unknown_rows,
        unknown_report_context(
            sample=register_number,
            sample_name=sample_name,
            report_type="Fingerprint",
            source_file=nias_path,
            output_file=output_path,
        ),
    )
    return {
        "output": output_path,
        "source_sheet": source_ws.title,
        "source_format": "Fingerprint",
        "unknown_register": unknown_register_result,
        "retained_rows": len(retained_rows),
        "blank_matches": blank_matches,
        "unidentified_count": len(unknown_names_for_documentation),
        "original_area_pct_total": original_area_pct_total,
        "final_area_pct_total": round(sum(item["area_pct"] for item in retained_rows), 4),
        "sum_counts": {},
        "sums_kg": {},
        "sums_area": {},
        "footnotes": 0,
        "sml_exceedances": [],
        "pubchem_names_found": pubchem_names_found,
        "pubchem_no_hits": pubchem_no_hits,
        "pubchem_unique_queries": len(pubchem_api_queries),
        "pubchem_cache_hits": 0,
        "cas_verification_counts": cas_verification_counts,
        "status_counts": {},
        # Same sample identity as the Doppelbestimmung path, so the Word report
        # is named the same way for a Fingerprint run (spec v2.1 §V.4).
        "syneris_number": register_number,
        "sample_label": sample_name,
        "syneris_summary_report_no": "",
        "processor_version": SCRIPT_VERSION,
        "database_version": "",
    }


def process_total_extraction_report(workbook, source_path: Path,
                                    output_path: Path) -> dict[str, Any]:
    """Create a Total Extraction report from the ISTD-based concentration column.

    Layout, title band and page setup are the Fingerprint report's. The one
    deliberate difference is the fifth column: it carries the concentration in
    the extract, is shown with one decimal and is never renormalised, because a
    concentration column that adds up to a measured total is exactly what this
    report exists for. Scaling it to 100 % would destroy it
    (spec v3.1 §VII.10, assumption 7).
    """
    source_ws = workbook[FINGERPRINT_SHEET_NAME]
    headers = {
        normalized_header(source_ws.cell(1, column).value): column
        for column in range(1, source_ws.max_column + 1)
    }
    columns = {
        # As in the Fingerprint report: §VII.9 may replace `RT (min)` by `RI`,
        # and without §VII.9 there is no index column at all.
        "rt": headers.get("rtmin"),
        "ri": headers.get(RI_NORMALIZED_HEADER),
        "name": headers["name"],
        "cas": headers["cas"],
        "match": headers["qualitymatch"],
        "conc_ugl": headers[normalized_header(TOTAL_EXTRACTION_HEADER)],
        "blank": headers["imblank"],
    }
    retained_rows = []
    pubchem_cache, pubchem_checked_at = load_pubchem_cache()
    cached_before_run = set(pubchem_cache)
    pubchem_names_found = 0
    pubchem_no_hits = 0
    pubchem_api_queries: set[str] = set()
    blank_matches = 0
    for row in range(2, source_ws.max_row + 1):
        rt = (source_ws.cell(row, columns["rt"]).value
              if columns["rt"] else None)
        ri = (retention_index_value(source_ws.cell(row, columns["ri"]).value)
              if columns["ri"] else None)
        name = text(source_ws.cell(row, columns["name"]).value)
        raw_cas = text(source_ws.cell(row, columns["cas"]).value)
        match = source_ws.cell(row, columns["match"]).value
        concentration = numeric_value(source_ws.cell(row, columns["conc_ugl"]).value)
        blank_value = text(source_ws.cell(row, columns["blank"]).value)
        if (not name and not raw_cas and numeric_value(rt) is None
                and ri is None and concentration is None):
            continue

        name_lookup_failed = False
        if not name:
            normalized_cas_for_lookup = normalize_cas(raw_cas)
            if is_valid_cas_number(normalized_cas_for_lookup):
                if normalized_cas_for_lookup not in pubchem_cache:
                    pubchem_api_queries.add(normalized_cas_for_lookup)
                preferred_name = pubchem_preferred_name(
                    normalized_cas_for_lookup, pubchem_cache, pubchem_checked_at)
                if preferred_name:
                    name = preferred_name
                    pubchem_names_found += 1
                else:
                    name = PUBCHEM_NO_HIT_TEXT
                    name_lookup_failed = True
                    pubchem_no_hits += 1
        is_blank = blank_value.casefold() in {"ja", "yes", "true", "1", "x"}
        if is_blank:
            blank_matches += 1
        retained_rows.append({
            "rt": rt,
            "ri": ri,
            "name": name,
            "cas": normalize_cas(raw_cas) or None,
            "match": match,
            "conc_ugl": concentration,
            "blank": "Ja" if is_blank else "Nein",
            "name_lookup_failed": name_lookup_failed,
            "summary": False,
            "unknown_summary": False,
            "substance_summary": False,
        })

    if set(pubchem_cache) != cached_before_run:
        save_pubchem_cache(pubchem_cache, pubchem_checked_at)

    # Validates the column and returns the measured total. Nothing is rescaled.
    source_concentration_total = total_extraction_sum(retained_rows)
    unknown_names_for_documentation = [
        unidentified_register_name(item) for item in retained_rows
        if is_unidentified_item(item)
    ]
    # Captured before the rows are regrouped, so every single occurrence reaches
    # the register instead of only the aggregated sum rows.
    unknown_rows = [item for item in retained_rows
                    if unknown_mz_ranked(item.get("name"))]
    category_sums = {"hydrocarbon": 0.0, "siloxane": 0.0}
    category_members = {"hydrocarbon": 0, "siloxane": 0}
    uncategorized_rows = []
    for item in retained_rows:
        category = classify_name(item["name"])
        if category in category_sums:
            category_sums[category] += item["conc_ugl"]
            category_members[category] += 1
        else:
            uncategorized_rows.append(item)
    retained_rows, repeated_substances = split_repeated_substance_groups(uncategorized_rows)
    for key, items in repeated_substances.items():
        is_unknown = key[0] == "unknown"
        retained_rows.append({
            "rt": None,
            "ri": None,
            "name": repeated_substance_summary_label(key, items),
            "cas": None,
            "match": None,
            "conc_ugl": round(sum(item["conc_ugl"] for item in items), 4),
            "blank": "Ja" if any(item["blank"] == "Ja" for item in items) else "Nein",
            "name_lookup_failed": False,
            "summary": True,
            "unknown_summary": is_unknown,
            "substance_summary": not is_unknown,
        })
    for category in ("hydrocarbon", "siloxane"):
        if category_members[category]:
            retained_rows.append({
                "rt": None,
                "ri": None,
                "name": SUMMARY_LABELS[category],
                "cas": None,
                "match": None,
                "conc_ugl": round(category_sums[category], 4),
                "blank": "Nein",
                "name_lookup_failed": False,
                "summary": True,
                "unknown_summary": False,
                "substance_summary": False,
            })

    # Grouping only moves concentrations between rows, so this stays the
    # measured total the source column carried.
    reported_total = round(sum(item["conc_ugl"] for item in retained_rows), 4)
    report_rows = list(retained_rows)
    if report_rows:
        report_rows.append({
            "rt": None,
            "ri": None,
            "name": TOTAL_EXTRACTION_SUM_LABEL,
            "cas": None,
            "match": None,
            "conc_ugl": reported_total,
            "blank": "",
            "name_lookup_failed": False,
            "summary": True,
            "unknown_summary": False,
            "substance_summary": False,
            "total": True,
        })

    # CAS/name control disabled. CAS values remain for lookup and reporting.
    cas_verification_counts, cas_verification_details = {}, []

    output_workbook = Workbook()
    target_ws = output_workbook.active
    target_ws.title = "NIAS Result"
    target_ws.sheet_view.showGridLines = False
    # The concentration column is the layout's last one, so it stays right of
    # any retention index the intermediate carried (§VII.9 contract).
    layout = fingerprint_report_layout(headers, TOTAL_EXTRACTION_HEADER)
    last_column = layout["last_column"]
    last_letter = layout["last_letter"]
    name_column = layout["positions"]["name"]
    sample_name = copy_fingerprint_report_metadata(workbook, target_ws, last_column)
    for column, header in enumerate(layout["titles"], start=1):
        cell = target_ws.cell(5, column, header)
        cell.font = Font(name="Arial", size=8, bold=True, color="000000")
        cell.fill = PatternFill("solid", fgColor="FFFFFF")
        cell.border = Border(bottom=Side(style="thin", color="000000"))
        cell.alignment = Alignment(horizontal="center", vertical="center")

    summary_separator_added = False
    for output_row, item in enumerate(report_rows, start=6):
        for column, role in enumerate(layout["roles"], start=1):
            value = item["conc_ugl"] if role == "quantity" else item[role]
            cell = target_ws.cell(output_row, column, value)
            cell.font = Font(name="Arial", size=8, color="000000")
            cell.alignment = Alignment(
                vertical="top" if role == "name" else "center",
                wrap_text=role == "name",
                horizontal="right" if role in {"match", "quantity"} else "left")
            if role == "rt":
                cell.number_format = "0.0000"
            elif role == "ri":
                # A whole number, and an absent index stays an empty cell.
                cell.number_format = "0"
            elif role == "quantity":
                cell.number_format = "0.0"
        if item["name_lookup_failed"]:
            target_ws.cell(output_row, name_column).font = Font(
                name="Arial", size=8, bold=True, color="FF0000")
        if item.get("total"):
            # The measured total is always set off from the rows above it, even
            # when it is the first bold line on the sheet.
            for column in range(1, last_column + 1):
                total_cell = target_ws.cell(output_row, column)
                total_cell.font = Font(name="Arial", size=8, bold=True, color="000000")
                total_cell.border = Border(top=Side(style="thin", color="000000"))
        elif item["summary"]:
            for column in range(1, last_column + 1):
                summary_cell = target_ws.cell(output_row, column)
                summary_cell.font = Font(name="Arial", size=8, bold=True, color="000000")
                if not summary_separator_added:
                    summary_cell.border = Border(top=Side(style="thin", color="000000"))
            summary_separator_added = True

    if not report_rows:
        target_ws["A6"] = "No total extraction peak remains after manual review."
        target_ws.merge_cells(f"A6:{last_letter}6")
        target_ws["A6"].font = Font(name="Arial", size=8, italic=True)

    last_data_row = max(6, 5 + len(report_rows))
    for column, width in layout["widths"].items():
        target_ws.column_dimensions[get_column_letter(column)].width = width
    target_ws.freeze_panes = "A6"
    if report_rows:
        target_ws.auto_filter.ref = f"A5:{last_letter}{last_data_row}"
    target_ws.print_title_rows = "1:5"
    target_ws.page_setup.orientation = "landscape"
    target_ws.page_setup.fitToWidth = 1
    target_ws.page_setup.fitToHeight = 0
    target_ws.sheet_properties.pageSetUpPr.fitToPage = True
    target_ws.page_margins.left = 0
    target_ws.page_margins.right = 0
    target_ws.page_margins.top = 0
    target_ws.page_margins.bottom = 0
    target_ws.print_area = f"A1:{last_letter}{last_data_row}"

    processed_at = datetime.now().astimezone()
    attach_audit_payload(output_workbook, {
        "fields": {
            "Processor version": SCRIPT_VERSION,
            "Processed at": processed_at.isoformat(timespec="seconds"),
            "Source format": TOTAL_EXTRACTION_FORMAT,
            "Source file": str(source_path.resolve()),
            "Source worksheet": source_ws.title,
            "Output file": str(output_path.resolve()),
            "Sample": sample_name,
            "Quantity basis": "ISTD; concentration in extract",
            "Source c [µg/L] total": source_concentration_total,
            "Reported c [µg/L] total": reported_total,
            "Reported rows": len(retained_rows),
            "Summary rows": sum(1 for item in retained_rows if item.get("summary")),
            "Category counts": category_members,
            "Blank matches": blank_matches,
            "Unidentified source rows registered": len(unknown_names_for_documentation),
        },
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    from gc_report_layout import fit_report_rows
    fit_report_rows(target_ws)
    output_workbook.save(output_path)
    register_number = syneris_number_from_text(sample_name, source_path.stem)
    unknown_register_result = safe_record_unknown_sightings(
        unknown_rows,
        unknown_report_context(
            sample=register_number,
            sample_name=sample_name,
            report_type=TOTAL_EXTRACTION_FORMAT,
            source_file=source_path,
            output_file=output_path,
        ),
    )
    return {
        "output": output_path,
        "source_sheet": source_ws.title,
        "source_format": TOTAL_EXTRACTION_FORMAT,
        "unknown_register": unknown_register_result,
        "retained_rows": len(retained_rows),
        "blank_matches": blank_matches,
        "unidentified_count": len(unknown_names_for_documentation),
        "source_concentration_ugl": source_concentration_total,
        "total_concentration_ugl": reported_total,
        "sum_counts": {},
        "sums_kg": {},
        "sums_area": {},
        "footnotes": 0,
        "sml_exceedances": [],
        "pubchem_names_found": pubchem_names_found,
        "pubchem_no_hits": pubchem_no_hits,
        "pubchem_unique_queries": len(pubchem_api_queries),
        "pubchem_cache_hits": 0,
        "cas_verification_counts": cas_verification_counts,
        "status_counts": {},
        # Same sample identity as the Fingerprint path, so the Word report is
        # named the same way for a Total Extraction run (spec v2.1 §V.4).
        "syneris_number": register_number,
        "sample_label": sample_name,
        "syneris_summary_report_no": "",
        "processor_version": SCRIPT_VERSION,
        "database_version": "",
    }


def process_workbook(
    nias_path: Path,
    cas_path: Optional[Path],
    output_path: Path,
    sheet_name: Optional[str] = None,
    include_traffic_light_summary: bool = False,
    _allow_excel_recalc: bool = True,
) -> dict[str, Any]:
    paths_to_check = [nias_path] + ([cas_path] if cas_path is not None else [])
    ensure_excel_files_closed(paths_to_check)

    # data_only=True reads the last calculated values stored by Excel.
    workbook = load_workbook(
        nias_path,
        data_only=True,
        keep_vba=nias_path.suffix.lower() == ".xlsm",
    )
    source_ws = select_source_sheet(workbook, sheet_name)
    if source_ws.title == FINGERPRINT_SHEET_NAME and is_fingerprint_workbook(workbook):
        # Both reports arrive on the Fingerprint sheet; only the appended
        # concentration column tells a total extraction from a fingerprint.
        if is_total_extraction_workbook(workbook):
            return process_total_extraction_report(workbook, nias_path, output_path)
        return process_fingerprint_report(workbook, nias_path, output_path)
    if cas_path is None:
        raise ValueError(
            "Für Legacy- und Doppelbestimmungsberichte ist eine CAS-Referenz erforderlich."
        )
    cas_lookup = load_cas_lookup(cas_path)

    single_mode = (source_ws.title == SINGLE_SHEET_NAME
                   and is_single_determination_workbook(workbook))
    # An Einzelbestimmung takes the Doppelbestimmung path: same column order,
    # same Parameter sheet, only its quality and fallback are read differently.
    duplicate_mode = single_mode or (
        source_ws.title == DUPLICATE_SHEET_NAME and is_duplicate_determination_workbook(workbook))
    if single_mode:
        data_start = 2
        columns = {"rt": 1, "name": 2, "cas": 3, "conc_kg": 4,
                   "conc_area": 0, "match": 0, "db": 0, "area": 0}
        duplicate_parameters = duplicate_parameter_lookup(workbook)
        duplicate_ov_ratio = numeric_value(duplicate_parameters.get("ovratio"))
        single_headers = {normalized_header(source_ws.cell(1, col).value): col
                          for col in range(1, source_ws.max_column + 1)}
        quality_col = single_headers.get("quality")
        duplicate_quality = ({row: numeric_value(source_ws.cell(row, quality_col).value)
                              for row in range(2, source_ws.max_row + 1)}
                             if quality_col else {})
        formula_workbook = load_workbook(nias_path, data_only=False)
        try:
            duplicate_fallback = single_concentration_fallback(formula_workbook)
        finally:
            formula_workbook.close()
        duplicate_factors = {}
    elif duplicate_mode:
        header_row = detect_header_row(source_ws, 1)
        data_start = header_row + 1
        columns = find_columns(source_ws, header_row, warn_missing=False)
        # Validated schema of the Doppelbestimmung result workbook.
        columns.update({"rt": 1, "name": 2, "cas": 3, "conc_kg": 4})
        columns["conc_area"] = 0  # derived from mg/kg mean and O/V ratio
        columns["match"] = 0     # reconstructed from both detailed determinations
        columns["db"] = 0
        columns["area"] = 0
        duplicate_parameters = duplicate_parameter_lookup(workbook)
        duplicate_ov_ratio = numeric_value(duplicate_parameters.get("ovratio"))
        duplicate_quality = duplicate_quality_lookup(workbook)
        # The workbook above was opened with data_only=True and therefore shows
        # cached results only. A freshly generated Doppelbestimmung has none, so
        # the formulas are read from a second view and resolved here.
        formula_workbook = load_workbook(nias_path, data_only=False)
        try:
            duplicate_factors = duplicate_quantification_factors(formula_workbook)
            duplicate_fallback = duplicate_concentration_fallback(
                formula_workbook, duplicate_ov_ratio)
        finally:
            formula_workbook.close()
    else:
        # Established NIAS template: table around row 27, concentration in H.
        header_row = detect_header_row(source_ws, 27)
        data_start = header_row + 1
        columns = find_columns(source_ws, header_row)
        columns["conc_kg"] = 8
        duplicate_parameters = {}
        duplicate_ov_ratio = None
        duplicate_quality = {}
        duplicate_factors = {}
        duplicate_fallback = {}

    retained_rows: list[dict[str, Any]] = []
    sums_kg = {key: 0.0 for key in SUMMARY_LABELS}
    sums_area = {key: 0.0 for key in SUMMARY_LABELS}
    sum_counts = {key: 0 for key in SUMMARY_LABELS}
    cyclic_monomer_abbreviations: list[str] = []
    pubchem_cache, pubchem_checked_at = load_pubchem_cache()
    cached_before_run = set(pubchem_cache)
    pubchem_names_found = 0
    pubchem_no_hits = 0
    pubchem_cache_hits = 0
    pubchem_api_queries: set[str] = set()
    resolved_concentrations = 0

    for row in range(data_start, source_ws.max_row + 1):
        name = text(source_ws.cell(row, columns["name"]).value)
        raw_cas = text(source_ws.cell(row, columns["cas"]).value)

        if (name.upper() in INTERNAL_STANDARDS or
                (duplicate_mode and name.casefold() in DUPLICATE_INTERNAL_STANDARD_NAMES)):
            continue
        if name.lower().startswith("sum of "):
            continue

        concentration = numeric_value(source_ws.cell(row, columns["conc_kg"]).value)
        if concentration is None and duplicate_mode:
            # A saved result wins; otherwise use the value computed from the
            # detail sheets, so the report never depends on desktop Excel.
            concentration = duplicate_fallback.get(row, {}).get("mean")
        if concentration is not None:
            resolved_concentrations += 1
        if concentration is None or concentration < MIN_CONCENTRATION_MG_KG:
            continue

        # If the substance name is missing but a valid CAS number is present,
        # retrieve PubChem's preferred compound title. Never invent a name.
        name_lookup_failed = False
        if not name:
            normalized_cas_for_lookup = normalize_cas(raw_cas)
            if not is_valid_cas_number(normalized_cas_for_lookup):
                # There is neither a substance name nor a usable CAS number.
                continue
            if normalized_cas_for_lookup in pubchem_cache:
                pubchem_cache_hits += 1
            else:
                pubchem_api_queries.add(normalized_cas_for_lookup)
            preferred_name = pubchem_preferred_name(
                normalized_cas_for_lookup,
                pubchem_cache,
                pubchem_checked_at,
            )
            if preferred_name:
                name = preferred_name
                pubchem_names_found += 1
            else:
                name = PUBCHEM_NO_HIT_TEXT
                name_lookup_failed = True
                pubchem_no_hits += 1

        if duplicate_mode:
            conc_area_value = (
                concentration / duplicate_ov_ratio
                if duplicate_ov_ratio not in (None, 0) else None
            )
        else:
            conc_area_value = numeric_value(source_ws.cell(row, columns["conc_area"]).value)

        category = classify_name(name)
        if category:
            if category == "cyclic_polyester":
                # The abbreviated oligomer composition is normally provided in
                # the Name column, e.g. "Cyclic NPG-IPA-NPG-IPA". Keep the CAS
                # description as a fallback for older source templates.
                abbreviation_sources = (
                    name,
                    source_ws.cell(row, columns["cas"]).value,
                )
                for abbreviation_source in abbreviation_sources:
                    for abbreviation in extract_monomer_abbreviations(abbreviation_source):
                        if abbreviation not in cyclic_monomer_abbreviations:
                            cyclic_monomer_abbreviations.append(abbreviation)
            sums_kg[category] += concentration
            if conc_area_value is not None:
                sums_area[category] += conc_area_value
            sum_counts[category] += 1
            continue

        cas = normalize_cas(raw_cas)
        match = cas_lookup_match(cas_lookup, raw_cas)

        if conc_area_value is not None:
            conc_area_value = round(conc_area_value, 4)

        retained_rows.append(
            {
                "rt": source_ws.cell(row, columns["rt"]).value,
                "name": name,
                "cas": cas or None,
                "db": None if duplicate_mode else source_ws.cell(row, columns["db"]).value,
                "match": duplicate_quality.get(row) if duplicate_mode else source_ws.cell(row, columns["match"]).value,
                "area": None if duplicate_mode else source_ws.cell(row, columns["area"]).value,
                "conc_area": conc_area_value,
                "conc_kg": round(concentration, 3),
                "sml": match.get("sml"),
                "reference": match.get("reference"),
                "footnote": match.get("footnote"),
                "summary": False,
                "unknown_summary": False,
                "substance_summary": False,
                "name_lookup_failed": name_lookup_failed,
            }
        )

    if set(pubchem_cache) != cached_before_run:
        save_pubchem_cache(pubchem_cache, pubchem_checked_at)

    unknown_names_for_documentation = [
        unidentified_register_name(item) for item in retained_rows
        if is_unidentified_item(item)
    ]
    # Captured before the rows are regrouped, so every single occurrence reaches
    # the register instead of only the aggregated sum rows.
    unknown_rows = [item for item in retained_rows
                    if unknown_mz_ranked(item.get("name"))]
    retained_rows, repeated_substances = split_repeated_substance_groups(retained_rows)

    if (not retained_rows and not any(sum_counts.values()) and not repeated_substances
            and duplicate_mode and not resolved_concentrations):
        # Not a single row yielded a number. Name the cause instead of reporting
        # that no substance was detected, which would be plainly wrong.
        data_rows = sum(1 for row in range(data_start, source_ws.max_row + 1)
                        if text(source_ws.cell(row, columns["name"]).value))
        if not duplicate_ov_ratio:
            reason = "im Blatt 'Parameter' das O/V-Verhältnis fehlt"
        elif single_mode:
            reason = ("in der Spalte 'ISTD' keiner Zeile ein interner Standard "
                      "zugeordnet ist oder dessen Fläche/Konzentration im Blatt "
                      "'ISTD' fehlt")
        elif not any(duplicate_factors.values()):
            reason = ("in der Spalte 'ISTD' von 'Bestimmung_1'/'Bestimmung_2' "
                      "kein interner Standard (IS1..IS3) markiert ist oder dessen "
                      "Fläche/Konzentration in 'ISTD_1'/'ISTD_2' fehlt"
                      if f"{DUPLICATE_ISTD_SHEET_PREFIX}1" in workbook.sheetnames else
                      "in 'Standards_1'/'Standards_2' kein verwertbarer "
                      "Quantifizierungsstandard gefunden wurde")
        else:
            reason = ("die Flächen aus 'Bestimmung_1'/'Bestimmung_2' den "
                      "Ergebniszeilen nicht zugeordnet werden konnten")
        raise ValueError(
            f"Aus dem Blatt {source_ws.title!r} konnte keine Konzentration gelesen "
            f"werden: {data_rows} Datenzeile(n) vorhanden, Spalte "
            f"{get_column_letter(columns['conc_kg'])} enthält nur unberechnete Formeln, "
            f"und die Ersatzrechnung ist nicht möglich, weil {reason}. "
            f"Bitte die {'Einzelbestimmung' if single_mode else 'Doppelbestimmung'} "
            "neu erzeugen oder die Datei in einer "
            "Tabellenkalkulation neu berechnen und speichern."
        )

    if (not retained_rows and not any(sum_counts.values()) and not repeated_substances
            and not duplicate_mode):
        error_rows = [
            row for row in range(data_start, source_ws.max_row + 1)
            if is_excel_error(source_ws.cell(row, columns["conc_kg"]).value)
        ]

        # openpyxl reads cached formula results but cannot calculate formulas.
        # If those results are Excel errors, recalculate a temporary copy with
        # desktop Excel and retry automatically without touching the source file.
        if error_rows and _allow_excel_recalc:
            recalculated_path = None
            try:
                recalculated_path = recalculate_workbook_with_excel(nias_path)
                return process_workbook(
                    recalculated_path,
                    cas_path,
                    output_path,
                    sheet_name=sheet_name,
                    include_traffic_light_summary=include_traffic_light_summary,
                    _allow_excel_recalc=False,
                )
            except Exception as recalc_exc:
                raise ValueError(
                    f"Im Blatt {source_ws.title!r}, Konzentrationsspalte {get_column_letter(columns['conc_kg'])}, "
                    f"wurden ab Zeile {data_start} in {len(error_rows)} Zeilen "
                    f"Excel-Fehlerwerte gefunden (z. B. #DIV/0!). Die automatische "
                    f"Neuberechnung mit Microsoft Excel ist fehlgeschlagen: {recalc_exc}. "
                    "Bitte die NIAS-Datei einmal in Excel oder in "
                    "LibreOffice/OpenOffice öffnen, neu berechnen (in Excel "
                    "Strg+Alt+F9), speichern und erneut verarbeiten."
                ) from recalc_exc
            finally:
                if recalculated_path is not None:
                    shutil.rmtree(recalculated_path.parent, ignore_errors=True)

        sample_values = []
        populated_rows = [
            row for row in range(data_start, source_ws.max_row + 1)
            if text(source_ws.cell(row, columns["name"]).value)
            or source_ws.cell(row, columns["conc_kg"]).value is not None
        ]
        for row in populated_rows[:5]:
            sample_values.append(
                f"row {row}: name={text(source_ws.cell(row, columns['name']).value)!r}, "
                f"conc_kg={source_ws.cell(row, columns['conc_kg']).value!r}"
            )
        detail = "; ".join(sample_values) if sample_values else "keine befüllten Datenzeilen"
        raise ValueError(
            f"Keine auswertbaren Datenzeilen auf Worksheet {source_ws.title!r}. "
            f"Header-Zeile={header_row}, Datenstart={data_start}, "
            f"Konzentrationsspalte={get_column_letter(columns['conc_kg'])}. "
            f"Beispielwerte: {detail}"
        )

    for category in ("styrene", "hydrocarbon", "siloxane", "cyclic_polyester"):
        if sum_counts[category] > 0:
            retained_rows.append(
                {
                    "rt": None,
                    "name": (
                        cyclic_polyester_summary_label(cyclic_monomer_abbreviations)
                        if category == "cyclic_polyester"
                        else SUMMARY_LABELS[category]
                    ),
                    "cas": None,
                    "db": None,
                    "match": None,
                    "area": None,
                    "conc_area": round(sums_area[category], 4),
                    "conc_kg": round(sums_kg[category], 3),
                    "sml": None,
                    "reference": None,
                    "footnote": (
                        CYCLIC_POLYESTER_FOOTNOTE
                        if category == "cyclic_polyester"
                        else None
                    ),
                    "summary": True,
                    "unknown_summary": False,
                    "substance_summary": False,
                    "name_lookup_failed": False,
                }
            )

    for key, items in repeated_substances.items():
        representative = items[0]
        is_unknown = key[0] == "unknown"
        conc_area_values = [item["conc_area"] for item in items
                            if item["conc_area"] is not None]
        retained_rows.append(
            {
                "rt": None,
                "name": repeated_substance_summary_label(key, items),
                "cas": None,
                "db": None,
                "match": None,
                "area": None,
                "conc_area": (round(sum(conc_area_values), 3)
                              if conc_area_values else None),
                "conc_kg": round(sum(item["conc_kg"] for item in items), 3),
                "sml": None if is_unknown else representative["sml"],
                "reference": None if is_unknown else representative["reference"],
                "footnote": None if is_unknown else representative["footnote"],
                "summary": True,
                "unknown_summary": is_unknown,
                "substance_summary": not is_unknown,
                "name_lookup_failed": False,
            }
        )

    # CAS/name control disabled. CAS values remain for lookup and reporting.
    cas_verification_counts, cas_verification_details = {}, []

    status_counts = {
        "🟢 SML eingehalten": 0,
        "🔴 SML überschritten": 0,
        "🟡 Kein SML vorhanden – Bewertung erforderlich": 0,
        "⚪ Substanz nicht identifiziert": 0,
    }
    if include_traffic_light_summary:
        for item in retained_rows:
            status_text, status_fill, status_font = report_status(item)
            # Detailed result table: symbol only. Full wording is reserved for
            # the Executive Summary.
            item["status"] = status_text.split(" ", 1)[0]
            item["status_description"] = status_text
            item["status_fill"] = status_fill
            item["status_font"] = status_font
            status_counts[status_text] += 1
    else:
        status_counts = {}

    # In duplicate mode ``source_ws`` is the result table, whose B2 holds the
    # first substance name - never the sample. Reading it there produced a
    # substance name wherever the report shows "Sample".
    syneris_number = (
        text(duplicate_parameters.get("samplename")) or nias_path.stem
        if duplicate_mode else extract_syneris_number(nias_path, source_ws)
    )
    sample_label = (
        syneris_number if duplicate_mode
        else clean_sample_name(source_ws["B2"].value) or syneris_number
    )
    register_candidates = [syneris_number]
    if not duplicate_mode:
        register_candidates.append(text(source_ws["B2"].value))
    register_candidates.append(nias_path.stem)
    register_number = syneris_number_from_text(*register_candidates)
    # The summary report number is the batch's, not the sample's, and names the
    # Word report of a multi-sample run (§V.4). A legacy workbook has no
    # Parameter sheet, in which case the lookup is simply empty.
    report_parameters = duplicate_parameters or duplicate_parameter_lookup(workbook)
    summary_report_no = text(
        next((report_parameters[name]
              for name in sorted(MIGRATION_PARAMETER_ALIASES["syneris_summary_report_no"])
              if report_parameters.get(name) not in (None, "")), ""))
    # Migration conditions travel with every sighting: an unknown that only shows
    # up in 95 % ethanol at 60 °C is an apolar migrant, which already narrows down
    # the substance class before anything is identified.
    if duplicate_mode:
        register_conditions = {
            "simulant": duplicate_parameters.get("simulans"),
            "temperature": duplicate_parameters.get("temperatur"),
            "duration": duplicate_parameters.get("dauer"),
            "migration_cell": duplicate_parameters.get("migrationszelle"),
            "analyst": duplicate_parameters.get("auswerter"),
        }
    else:
        register_conditions = {
            "simulant": source_ws["B10"].value,
            "temperature": source_ws["E8"].value,
            "duration": source_ws["E9"].value,
            "migration_cell": "",
            "analyst": source_ws["J2"].value,
        }
    unknown_register_result = safe_record_unknown_sightings(
        unknown_rows,
        unknown_report_context(
            sample=register_number,
            sample_name=sample_label,
            report_type=(SINGLE_FORMAT if single_mode
                         else "Doppelbestimmung" if duplicate_mode else "NIAS"),
            source_file=nias_path,
            output_file=output_path,
            **register_conditions,
        ),
    )

    footnotes: dict[str, int] = {}
    for item in retained_rows:
        cleaned = clean_footnote(item["footnote"])
        if cleaned and cleaned not in footnotes:
            footnotes[cleaned] = len(footnotes)

    output_workbook = Workbook()
    target_ws = output_workbook.active
    target_ws.title = "NIAS Result"
    target_ws.sheet_view.showGridLines = False
    if duplicate_mode:
        copy_duplicate_report_metadata(
            workbook, target_ws, 9 if include_traffic_light_summary else 8,
            single=single_mode,
        )
    else:
        copy_report_metadata(
            source_ws, target_ws, 9 if include_traffic_light_summary else 8
        )

    output_headers = [
        "RT (min)", "Name", "CAS-No.", "% match", "Conc. mg/dm²",
        "Conc. mg/kg", "SML (mg/kg)", "Ref.",
    ]
    if include_traffic_light_summary:
        output_headers.append("Status")
    last_output_column = len(output_headers)
    output_header_row = 5
    for column, header in enumerate(output_headers, start=1):
        cell = target_ws.cell(output_header_row, column, header)
        # Second header: black type on white background with a continuous bottom line.
        cell.font = Font(name="Arial", size=8, bold=True, color="000000")
        cell.fill = PatternFill("solid", fgColor="FFFFFF")
        cell.border = Border(bottom=Side(style="thin", color="000000"))
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=False,
        )

    first_data_row = output_header_row + 1
    if not retained_rows:
        message_cell = target_ws.cell(
            first_data_row, 1, "No substance was detected above 10 ppb."
        )
        target_ws.merge_cells(
            start_row=first_data_row, start_column=1,
            end_row=first_data_row, end_column=last_output_column,
        )
        message_cell.font = Font(name="Arial", size=8, italic=True, color="000000")
        message_cell.alignment = Alignment(horizontal="left", vertical="center")
        target_ws.row_dimensions[first_data_row].height = 15

    summary_separator_added = False
    for output_row, item in enumerate(retained_rows, start=first_data_row):
        # Prepare row values; Reference marker will be set below
        values = [
            item["rt"], item["name"], item["cas"], item["match"],
            item["conc_area"], item["conc_kg"], item["sml"], item.get("reference"),
        ]
        if include_traffic_light_summary:
            values.append(item.get("status"))
        for column, value in enumerate(values, start=1):
            cell = target_ws.cell(output_row, column, value)
            cell.font = Font(name="Arial", size=8, color="000000")
            if column == 5 and isinstance(value, (int, float)):
                cell.number_format = "0.0000"
            elif column == 6 and isinstance(value, (int, float)):
                cell.number_format = "0.000"
            cell.alignment = Alignment(
                vertical="top" if column == 2 else "center",
                wrap_text=column == 2,
                horizontal=("left" if column == 9 else "right" if column >= 4 else "left"),
            )

        if include_traffic_light_summary:
            status_cell = target_ws.cell(output_row, 9)
            status_cell.fill = PatternFill("solid", fgColor=item["status_fill"])
            status_cell.font = Font(
                name="Arial", size=8, bold=True, color=item["status_font"]
            )
            status_cell.alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=False
            )

        if item.get("name_lookup_failed"):
            missing_name_cell = target_ws.cell(output_row, 2)
            missing_name_cell.font = Font(
                name="Arial", size=8, bold=True, color="FF0000"
            )


        reference_text = text(item.get("reference"))
        cleaned = clean_footnote(item["footnote"])
        has_marker = bool(cleaned) and cleaned in footnotes

        if reference_text or has_marker:
            ref_cell = target_ws.cell(output_row, 8)
            if has_marker:
                # Use Excel rich text instead of Unicode superscript glyphs. This keeps
                # markers such as (a), (b), ... readable and genuinely superscripted.
                rich_value = CellRichText()
                if reference_text:
                    rich_value.append(reference_text + " ")
                _, label = excel_superscript_marker(footnotes[cleaned])
                rich_value.append(
                    TextBlock(
                        InlineFont(rFont="Arial", sz=8, color="000000", vertAlign="superscript"),
                        f"({label})",
                    )
                )
                ref_cell.value = rich_value
            else:
                ref_cell.value = reference_text
            ref_cell.font = Font(name="Arial", size=8, color="000000")
            ref_cell.alignment = Alignment(wrap_text=False, vertical="center", horizontal="right")

        limit = item.get("sml")
        # Bold the concentration when no SML is available, and also for exceedances.
        if limit is None or item["conc_kg"] > limit:
            concentration_cell = target_ws.cell(output_row, 6)
            concentration_cell.font = Font(name="Arial", size=8, bold=True, color="000000")

        if item["summary"]:
            for column in range(1, last_output_column + 1):
                summary_cell = target_ws.cell(output_row, column)
                summary_cell.font = Font(name="Arial", size=8, bold=True, color="000000")
                if not summary_separator_added:
                    summary_cell.border = Border(top=Side(style="thin", color="000000"))
            summary_separator_added = True

    last_data_row = first_data_row + len(retained_rows) - 1
    # Leave exactly one blank row after the substance rows/summaries.
    next_row = last_data_row + 2

    if footnotes:
        for footnote_text, index in footnotes.items():
            marker, _ = excel_superscript_marker(index)
            marker_cell = target_ws.cell(next_row, 1, marker)
            marker_cell.font = Font(name="Arial", size=8, vertAlign="superscript", color="000000")
            text_cell = target_ws.cell(next_row, 2, footnote_text)
            text_cell.font = Font(name="Arial", size=8, color="000000")
            text_cell.alignment = Alignment(wrap_text=True, vertical="top")
            target_ws.merge_cells(start_row=next_row, start_column=2, end_row=next_row, end_column=last_output_column)
            next_row += 1
    if any(sum_counts.values()) or repeated_substances:
        next_row += 1
        target_ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=last_output_column)
        note_cell = target_ws.cell(next_row, 1, CALCULATION_NOTE)
        note_cell.font = Font(name="Arial", size=8, italic=True)
        note_cell.alignment = Alignment(wrap_text=True, vertical="top")

    if sum_counts["cyclic_polyester"] > 0 and cyclic_monomer_abbreviations:
        next_row += 1
        target_ws.merge_cells(start_row=next_row, start_column=1, end_row=next_row, end_column=last_output_column)
        abbreviation_cell = target_ws.cell(
            next_row,
            1,
            cyclic_polyester_abbreviation_note(cyclic_monomer_abbreviations),
        )
        abbreviation_cell.font = Font(name="Arial", size=8, italic=True)
        abbreviation_cell.alignment = Alignment(wrap_text=True, vertical="top")

    widths = {1: 7, 2: 29, 3: 10, 4: 6, 5: 8, 6: 8, 7: 8, 8: 8}
    if include_traffic_light_summary:
        widths[9] = 7
    for column, width in widths.items():
        target_ws.column_dimensions[get_column_letter(column)].width = width

    target_ws.freeze_panes = "A6"
    if retained_rows:
        target_ws.auto_filter.ref = f"A5:{get_column_letter(last_output_column)}{last_data_row}"
    target_ws.print_title_rows = "1:5"
    target_ws.page_setup.orientation = "landscape"
    target_ws.page_setup.fitToWidth = 1
    target_ws.page_setup.fitToHeight = 0
    target_ws.sheet_properties.pageSetUpPr.fitToPage = True
    target_ws.sheet_properties.pageSetUpPr.autoPageBreaks = False
    target_ws.page_margins.left = 0
    target_ws.page_margins.right = 0
    target_ws.page_margins.top = 0
    target_ws.page_margins.bottom = 0
    target_ws.print_area = f"A1:{get_column_letter(last_output_column)}{max(next_row, last_data_row)}"

    # Executive Summary sheet
    if include_traffic_light_summary:
        summary_ws = output_workbook.create_sheet("Executive Summary")
        summary_ws.sheet_view.showGridLines = False
        summary_ws.merge_cells("A1:D1")
        summary_ws["A1"] = "Executive Summary"
        summary_ws["A1"].font = Font(name="Arial", size=14, bold=True, color="FFFFFF")
        summary_ws["A1"].fill = PatternFill("solid", fgColor="BA0C2F")
        summary_ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
        summary_ws.row_dimensions[1].height = 24
        summary_ws["A3"] = "Sample"
        summary_ws["B3"] = sample_label
        summary_ws["A4"] = "Source file"
        summary_ws["B4"] = nias_path.name
        summary_ws["A6"] = "Status"
        summary_ws["B6"] = "Anzahl"
        summary_ws["A6"].font = summary_ws["B6"].font = Font(name="Arial", size=10, bold=True)
        summary_ws["A6"].border = summary_ws["B6"].border = Border(bottom=Side(style="thin", color="000000"))
        status_styles = {
            "🟢 SML eingehalten": ("D9EAD3", "2E603A"),
            "🔴 SML überschritten": ("F4CCCC", "9C0006"),
            "🟡 Kein SML vorhanden – Bewertung erforderlich": ("FFF2CC", "7F6000"),
            "⚪ Substanz nicht identifiziert": ("E7E6E6", "404040"),
        }
        for summary_row, (label, count) in enumerate(status_counts.items(), start=7):
            summary_ws.cell(summary_row, 1, label)
            summary_ws.cell(summary_row, 2, count)
            fill_color, font_color = status_styles[label]
            for column in (1, 2):
                cell = summary_ws.cell(summary_row, column)
                cell.fill = PatternFill("solid", fgColor=fill_color)
                cell.font = Font(name="Arial", size=9, bold=True, color=font_color)
            summary_ws.cell(summary_row, 2).alignment = Alignment(horizontal="center")
        metrics = [
            ("Ausgewertete Ergebniszeilen", len(retained_rows)),
            ("Einzelsubstanzen", sum(1 for item in retained_rows if not item["summary"])),
            ("Summenzeilen", sum(1 for item in retained_rows if item["summary"])),
            ("PubChem-Namen ergänzt", pubchem_names_found),
            ("PubChem ohne Treffer", pubchem_no_hits),
        ]
        summary_ws["A13"] = "Kennzahl"
        summary_ws["B13"] = "Wert"
        summary_ws["A13"].font = summary_ws["B13"].font = Font(name="Arial", size=10, bold=True)
        summary_ws["A13"].border = summary_ws["B13"].border = Border(bottom=Side(style="thin", color="000000"))
        for row_number, (label, value) in enumerate(metrics, start=14):
            summary_ws.cell(row_number, 1, label)
            summary_ws.cell(row_number, 2, value)
            summary_ws.cell(row_number, 2).alignment = Alignment(horizontal="center")
        summary_ws.column_dimensions["A"].width = 52
        summary_ws.column_dimensions["B"].width = 18
        summary_ws.column_dimensions["C"].width = 3
        summary_ws.column_dimensions["D"].width = 3

    processed_at = datetime.now().astimezone()
    attach_audit_payload(output_workbook, {
        "fields": {
            "Processor version": SCRIPT_VERSION,
            "Processed at": processed_at.isoformat(timespec="seconds"),
            "Source format": (SINGLE_FORMAT if single_mode else "Doppelbestimmung" if duplicate_mode else "Legacy Auswertung"),
            "Source file": str(nias_path.resolve()),
            "Source worksheet": source_ws.title,
            "Output file": str(output_path.resolve()),
            "Sample": sample_label,
            "CAS database version": database_version_text(cas_path),
            "CAS database path": str(cas_path.resolve()),
            "CAS database SHA-256": file_sha256(cas_path),
            "PubChem cache hits": pubchem_cache_hits,
            "PubChem API queries": len(pubchem_api_queries),
            "PubChem names found": pubchem_names_found,
            "PubChem no hits": pubchem_no_hits,
            "Minimum concentration mg/kg": MIN_CONCENTRATION_MG_KG,
            "Reported rows": len(retained_rows),
            "Summary rows": sum(1 for item in retained_rows if item.get("summary")),
            "Category counts": sum_counts,
            "Unidentified source rows registered": len(unknown_names_for_documentation),
        },
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    from gc_report_layout import fit_report_rows
    fit_report_rows(target_ws)
    output_workbook.save(output_path)

    exceedances = [
        item["name"]
        for item in retained_rows
        if item.get("sml") is not None
        and item["conc_kg"] > item["sml"]
    ]

    return {
        "output": output_path,
        "source_sheet": source_ws.title,
        "source_format": (SINGLE_FORMAT if single_mode else "Doppelbestimmung" if duplicate_mode else "Legacy Auswertung"),
        "retained_rows": len(retained_rows),
        "sum_counts": sum_counts,
        "sums_kg": sums_kg,
        "sums_area": sums_area,
        "footnotes": len(footnotes),
        "sml_exceedances": exceedances,
        "pubchem_names_found": pubchem_names_found,
        "pubchem_no_hits": pubchem_no_hits,
        "pubchem_unique_queries": len(pubchem_api_queries),
        "pubchem_cache_hits": pubchem_cache_hits,
        "unidentified_count": len(unknown_names_for_documentation),
        "unknown_register": unknown_register_result,
        "cas_verification_counts": cas_verification_counts,
        "status_counts": status_counts,
        # Identity of the sample this report belongs to. ``syneris_number`` is
        # the derived 8-digit number (spec v2.1 §V.3) — the same one
        # ``discover_duplicate_samples`` groups determinations by — and both
        # values name the Word report in ``run_nias_batch`` (§V.4).
        "syneris_number": register_number,
        "sample_label": sample_label,
        "syneris_summary_report_no": summary_report_no,
        "processor_version": SCRIPT_VERSION,
        "database_version": database_version_text(cas_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a NIAS report from a reviewed Fingerprint, Doppelbestimmung, or legacy workbook."
    )
    parser.add_argument("nias_file", nargs="?", type=Path, help="Reviewed input .xlsx or .xlsm file")
    parser.add_argument(
        "cas_file", nargs="?", type=Path,
        help="CAS reference .xlsx file (not required for Fingerprint input)")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("NIAS_Result.xlsx"),
        help="Output .xlsx file (default: NIAS_Result.xlsx)",
    )
    parser.add_argument(
        "--sheet", help="Optional source worksheet name; otherwise selected automatically"
    )
    parser.add_argument(
        "--word", action="store_true",
        help="Also combine all processed Excel result tables into one Word document",
    )
    parser.add_argument(
        "--ampel-summary", action="store_true",
        help="Include traffic-light column and Executive Summary (off by default)",
    )
    return parser.parse_args()



def settings_file_path() -> Path:
    from nias_paths import settings_path
    return settings_path()


def load_user_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_file_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def save_user_settings(**updates: Any) -> None:
    settings_path = settings_file_path()
    data = load_user_settings()
    data.update(updates)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


LEGACY_CAS_PATH = Path(r"C:/Skript/CASINFO.xlsx")


def _same_path(first: Path, second: Path) -> bool:
    return (os.path.normcase(os.path.normpath(str(first)))
            == os.path.normcase(os.path.normpath(str(second))))


def load_saved_cas_path() -> Path:
    """Load the standard CAS path, preferring the CASINFO next to this script.

    The former default pointed at a fixed folder, so installations that had never
    chosen a reference kept reading a stale CASINFO after the maintained file had
    moved. A path the user selected deliberately still wins; only that old
    default is superseded by the file shipped with the running script.
    """
    local = Path(__file__).resolve().parent / "CASINFO.xlsx"
    saved = text(load_user_settings().get("standard_cas_path"))
    if saved:
        from nias_paths import project_path
        saved = str(project_path(saved))
    if saved and Path(saved).is_file() and not _same_path(Path(saved), LEGACY_CAS_PATH):
        # A stored path is only honoured while the file is actually there; a
        # renamed or removed reference must not leave the program pointing at
        # nothing when a usable CASINFO sits next to the script.
        return Path(saved)
    if local.is_file():
        return local
    return Path(saved) if saved else LEGACY_CAS_PATH


def save_standard_cas_path(cas_path: Path) -> None:
    """Persist the selected standard CAS path without deleting other settings."""
    resolved = cas_path.resolve()
    try:
        saved = str(resolved.relative_to(Path(__file__).resolve().parent))
    except ValueError:
        saved = str(resolved)
    save_user_settings(standard_cas_path=saved)



# ---------------------------------------------------------------------------
# Embedded GC_MS_mitteln engine
# ---------------------------------------------------------------------------
# The full validated GC-MS engine is embedded to keep this GUI application
# self-contained. It executes in an isolated module namespace, avoiding name
# collisions with the NIAS workflow while requiring no second .py file.
import types as _types

_GC_MS_MITTELN_SOURCE = '#!/usr/bin/env python3\n"""GC-MS Dreifachbestimmung: RT-basiertes Peak-Matching und Mittelwertbildung.\n\nAufruf:\n    python gc_ms_mitteln.py eingabe.xlsx [-o ausgabe.xlsx] [--rt-toleranz 0.035]\n\nErwartet drei Rohdatenblätter mit den Spalten:\nRT, Start Tm, End Tm, m/z, Area, Area%, Hight/Height, Height%, A/H,\nMark, Name, CAS #, SI.\n"""\nfrom __future__ import annotations\nimport argparse, math, re, shutil\nfrom collections import Counter\nfrom dataclasses import dataclass\nfrom pathlib import Path\nfrom statistics import mean, stdev\nfrom typing import Any\nfrom openpyxl import load_workbook\nfrom openpyxl.styles import Font, PatternFill, Border, Side, Alignment\nfrom openpyxl.formatting.rule import FormulaRule\nfrom openpyxl.utils import get_column_letter\nfrom openpyxl.worksheet.views import Selection\n\nREQUIRED = {"RT", "Area", "Area%", "Name", "CAS #", "SI"}\nLETTERS = "ABC"\nIS_CONCENTRATION_UG_L = 10166.67\nREPORTING_LIMIT_UG_L = 100.0\nCAS_MATCH_TOLERANCE_MIN = 0.100\nREVIEW_RT_TOLERANCE_MIN = 0.150\nPRIMARY_RT_TOLERANCE_MIN = 0.035\nALLOWED_INTERNAL_STANDARDS = {"IS1", "IS2", "IS3"}\n\n@dataclass\nclass Peak:\n    sample: int\n    sheet: str\n    row: int\n    rt: float\n    area: float | None\n    area_pct: float | None\n    height: float | None\n    height_pct: float | None\n    si: float | None\n    name: str\n    cas: str\n\n\ndef num(v: Any) -> float | None:\n    if v is None or v == "": return None\n    if isinstance(v, (int, float)) and not isinstance(v, bool): return float(v)\n    s = str(v).strip().replace(" ", "").replace(",", ".")\n    try: return float(s)\n    except ValueError: return None\n\n\ndef clean_name(v: Any) -> str:\n    return "" if v is None else re.sub(r"\\s+", " ", str(v).strip())\n\n\ndef clean_cas(v: Any) -> str:\n    if v is None: return "0"\n    # Excel-Datumsobjekte entstehen bei CAS wie 4860-03-1; Excel zeigt sie z. B. als 03/01/4860.\n    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day"):\n        return f"{v.year}-{v.month:02d}-{v.day}"\n    s = str(v).strip().replace("/", "-")\n    parts = s.split("-")\n    if len(parts) == 3 and all(p.isdigit() for p in parts):\n        # dd-mm-yyyy oder mm-dd-yyyy aus Excel-Anzeige zurückdrehen\n        if len(parts[2]) == 4:\n            a,b,y = map(int, parts)\n            return f"{y}-{b:02d}-{a}"\n        a,b,c = map(int, parts)\n        if a == 0 and b == 0 and c == 0: return "0"\n        return f"{a}-{b:02d}-{c}"\n    return "0" if s in {"", "0", "0-0-0", "0-00-0"} else s\n\n\ndef valid_cas(cas: str) -> bool:\n    m = re.fullmatch(r"(\\d{2,7})-(\\d{2})-(\\d)", cas or "")\n    if not m: return False\n    digits = m.group(1) + m.group(2)\n    check = sum((i+1)*int(d) for i,d in enumerate(reversed(digits))) % 10\n    return check == int(m.group(3))\n\n\ndef read_peaks(ws, sample: int) -> list[Peak]:\n    # DIN SPEC 91521: Zeile 1 ist zwingend die Kopfzeile. Es wird nicht mehr\n    # weiter unten nach einer zweiten Kopfzeile gesucht, weil dies Datenzeilen\n    # überspringen oder einen falschen Tabellenbereich auswählen konnte.\n    headers = {str(c.value).strip(): c.column for c in ws[1] if c.value is not None}\n    missing = REQUIRED - set(headers)\n    if missing:\n        raise ValueError(\n            f"Blatt {ws.title}: Zeile 1 enthält nicht alle Pflichtspalten "\n            f"{sorted(missing)}. Die DIN-Daten müssen in Zeile 1 beginnen."\n        )\n    hcol = headers.get("Hight", headers.get("Height"))\n    hpcol = headers.get("Hight%", headers.get("Height%"))\n    peaks=[]\n    for r in range(2, ws.max_row+1):\n        rt=num(ws.cell(r,headers["RT"]).value)\n        if rt is None: continue\n        peaks.append(Peak(sample,ws.title,r,rt,\n            num(ws.cell(r,headers["Area"]).value), num(ws.cell(r,headers["Area%"]).value),\n            num(ws.cell(r,hcol).value) if hcol else None,\n            num(ws.cell(r,hpcol).value) if hpcol else None,\n            num(ws.cell(r,headers["SI"]).value), clean_name(ws.cell(r,headers["Name"]).value),\n            clean_cas(ws.cell(r,headers["CAS #"]).value)))\n    return peaks\n\n\ndef cluster_peaks(peaks: list[Peak], tol: float) -> list[list[Peak]]:\n    """Deterministisches Matching mit CAS-Priorität und RT-Fallback.\n\n    Ein gültiger, in A/B/C identischer CAS-Treffer wird zuerst als 3/3-Cluster\n    zugeordnet, sofern die gesamte RT-Spanne höchstens\n    CAS_MATCH_TOLERANCE_MIN beträgt. Dadurch gehen eindeutig identifizierte\n    Stoffe bei geringfügigem RT-Drift nicht verloren. Alle übrigen Peaks werden\n    anschließend unverändert mit der strengeren allgemeinen RT-Toleranz geclustert.\n    Pro Cluster bleibt höchstens ein Peak je Replikat zulässig.\n    """\n    ordered=sorted(peaks, key=lambda x:(x.rt,x.sample,x.row))\n    used=set()\n    clusters=[]\n    by_cas={}\n    for p in ordered:\n        if valid_cas(p.cas):\n            by_cas.setdefault(p.cas, {0:[],1:[],2:[]})[p.sample].append(p)\n    cas_candidates=[]\n    for cas, groups in by_cas.items():\n        for a in groups[0]:\n            for b in groups[1]:\n                for c in groups[2]:\n                    rts=[a.rt,b.rt,c.rt]\n                    spread=max(rts)-min(rts)\n                    if spread <= CAS_MATCH_TOLERANCE_MIN + 1e-12:\n                        m=mean(rts)\n                        deviation=sum(abs(rt-m) for rt in rts)\n                        cas_candidates.append((spread,deviation,cas,a.row,b.row,c.row,a,b,c))\n    for _,_,_,_,_,_,a,b,c in sorted(cas_candidates, key=lambda x:x[:6]):\n        ids=(id(a),id(b),id(c))\n        if any(item in used for item in ids):\n            continue\n        clusters.append([a,b,c])\n        used.update(ids)\n    for p in ordered:\n        if id(p) in used:\n            continue\n        candidates=[]\n        for i,c in enumerate(clusters):\n            if any(q.sample==p.sample for q in c): continue\n            rts=[q.rt for q in c]+[p.rt]\n            if max(rts)-min(rts) <= tol + 1e-12:\n                candidates.append((abs(p.rt-mean(q.rt for q in c)), max(rts)-min(rts), i))\n        if candidates:\n            clusters[min(candidates)[2]].append(p)\n        else:\n            clusters.append([p])\n    return sorted(clusters, key=lambda c:mean(p.rt for p in c))\n\ndef consensus(c: list[Peak]):\n    names=[p.name for p in c if p.name and not is_unidentified(p.name)]\n    cases=[p.cas for p in c]\n    valid=[x for x in cases if valid_cas(x)]\n    vc=Counter(valid)\n    majority_cas = vc.most_common(1)[0][0] if vc and vc.most_common(1)[0][1]>=2 else None\n    if majority_cas:\n        subset=[p for p in c if p.cas==majority_cas and p.name and not is_unidentified(p.name)]\n        chosen=max(subset, key=lambda p:((sum(q.name==p.name for q in subset)), p.si or -1)).name if subset else ""\n        cas=majority_cas\n    else:\n        nc=Counter(names)\n        if nc:\n            best=max(nc.values())\n            tied={n for n,v in nc.items() if v==best}\n            chosen=max((p for p in c if p.name in tied), key=lambda p:(p.si or -1,-p.sample)).name\n        else: chosen=""\n        linked=[p.cas for p in c if p.name==chosen and valid_cas(p.cas)]\n        cas=Counter(linked).most_common(1)[0][0] if linked else "0"\n    same_name=len(set(names))<=1 if names else True\n    same_cas=len(set(cases))<=1\n    agree = "Ja" if (same_name and same_cas) or majority_cas or (names and Counter(names).most_common(1)[0][1]>=2) else "Nein"\n    notes=[]\n    if not valid_cas(cas): notes.append("Keine gültige CAS-Nummer")\n    if len(set(cases))>1: notes.append("Abweichende CAS-Zuordnung zwischen Replikaten")\n    if len(set(names))>1: notes.append("Bibliotheksidentifikation uneinheitlich")\n    if internal_standard_code(chosen):\n        chosen=internal_standard_display_name(chosen)\n        notes.insert(0,"Interner Standard")\n    return chosen or "Unidentified", cas if valid_cas(cas) else "0", agree, "; ".join(dict.fromkeys(notes))\n\n\ndef avg(vals):\n    x=[v for v in vals if v is not None]\n    return mean(x) if x else None\n\ndef sd(vals):\n    x=[v for v in vals if v is not None]\n    return stdev(x) if len(x)>1 else None\n\ndef cv(vals):\n    a=avg(vals); s=sd(vals)\n    return 100*s/a if a not in (None,0) and s is not None else None\n\n\n# Replikat-QC anhand der Summe aller detektierten Peak-Areas.\n# Ein Replikat wird verworfen, wenn seine Gesamtsumme um mehr als 25 % vom\n# Mittelwert der beiden anderen Replikate abweicht.\ndef replicate_area_qc(all_peaks: list[Peak], threshold: float = 0.25):\n    """One-pass global replicate QC.\n\n    Calculates each replicate\'s total Area and its relative deviation from the\n    average total Area of the other two. If the largest deviation is > threshold,\n    exactly that worst replicate is excluded. Selecting the single worst replicate\n    avoids the mathematical artifact where one extreme outlier makes the two good\n    replicates also appear >25% away from an average containing the outlier.\n    """\n    totals = [sum((p.area or 0.0) for p in all_peaks if p.sample == i) for i in range(3)]\n    deviations=[]; references=[]\n    for i,total in enumerate(totals):\n        others=[totals[j] for j in range(3) if j!=i]\n        reference=mean(others); references.append(reference)\n        deviations.append(abs(total-reference)/reference if reference else (0.0 if total==0 else math.inf))\n    worst=max(range(3),key=lambda i: deviations[i])\n    excluded={worst} if deviations[worst] > threshold else set()\n    rows=[]\n    for i,total in enumerate(totals):\n        if i in excluded:\n            status="VERWERFEN / Messung wiederholen"\n        elif excluded and deviations[i] > threshold:\n            status="Beibehalten – nicht größte Abweichung"\n        else:\n            status="OK"\n        rows.append({"sample":LETTERS[i],"total":total,"reference":references[i],\n                     "deviation":deviations[i],"threshold":threshold,"status":status})\n    if excluded:\n        i=worst\n        alert=(f"ACHTUNG: Replikat {LETTERS[i]} zeigt mit {deviations[i]:.2%} die größte Abweichung "\n               f"und liegt über dem Grenzwert von {threshold:.0%}. Es wurde aus Mittelwerten, "\n               "SD/VK, Clustering und Kategoriesummen ausgeschlossen. Messung bitte prüfen und "\n               "vorzugsweise wiederholen. Die Prüfung ist ein einmaliger globaler Ausreißertest; "\n               "es wird höchstens ein Replikat verworfen.")\n    else:\n        alert=(f"OK: Kein Replikat weicht mit der Summe aller Peak-Areas um mehr als "\n               f"{threshold:.0%} vom Mittelwert der jeweils anderen zwei Replikate ab.")\n    return totals, rows, excluded, alert\n\ndef write_replicate_qc_sheet(ws, qc_rows, alert):\n    headers = ["Replikat", "Summe Area aller Peaks", "Mittelwert andere zwei",\n               "Abweichung", "Grenzwert", "Status"]\n    ws.append(["Replikat-QC", alert])\n    ws.append(headers)\n    for q in qc_rows:\n        ws.append([q["sample"], q["total"], q["reference"], q["deviation"],\n                   q["threshold"], q["status"]])\n    ws.sheet_view.showGridLines = False\n    ws.sheet_view.topLeftCell = "A1"\n    ws.freeze_panes = "A3"\n    ws.column_dimensions["A"].width = 18\n    ws.column_dimensions["B"].width = 25\n    ws.column_dimensions["C"].width = 24\n    ws.column_dimensions["D"].width = 15\n    ws.column_dimensions["E"].width = 13\n    ws.column_dimensions["F"].width = 34\n    ws["A1"].font = Font(bold=True, color="FFFFFF")\n    ws["A1"].fill = PatternFill("solid", fgColor=DARK)\n    ws["B1"].alignment = Alignment(wrap_text=True)\n    ws["B1"].font = Font(bold=True, color="9C0006" if "ACHTUNG" in alert else "006100")\n    for c in ws[2]:\n        c.fill = PatternFill("solid", fgColor=DARK)\n        c.font = Font(color="FFFFFF", bold=True)\n        c.alignment = Alignment(horizontal="center", wrap_text=True)\n    for r in range(3, 6):\n        ws.cell(r, 2).number_format = \'#,##0\'\n        ws.cell(r, 3).number_format = \'#,##0\'\n        ws.cell(r, 4).number_format = \'0.00%\'\n        ws.cell(r, 5).number_format = \'0%\'\n        if str(ws.cell(r, 6).value).startswith("VERWERFEN"):\n            for c in ws[r]: c.fill = PatternFill("solid", fgColor=RED)\n\n# Deterministische Stoffklassen-Erkennung (offline, ohne KI).\n# Grundsatz: Ein Kohlenwasserstoff darf nur Kohlenstoff/Wasserstoff enthalten.\n# Weil die Rohdaten keine Summenformel enthalten, wird dies konservativ aus dem\n# Bibliotheksnamen abgeleitet: erst Ausschluss heteroatomhaltiger Funktionen,\n# danach positive Erkennung typischer KW-Nomenklatur.\nHETERO_OR_FUNCTIONAL = re.compile(\n    r"acid|ester|alcohol|phenol|\\bol\\b|\\d-ol\\b|anol\\b|enol\\b|"\n    r"amine|amide|imide|nitrile|nitro|sulfur|sulfon|sulfone|phosph|"\n    r"ether|oxy|oxa|glycol|glycer|pyran|furan|pyraz|triaz|tetraz|"\n    r"one\\b|aldehyde|carbox|carbonate|acetate|citrate|maleate|phthalate|"\n    r"chloro|bromo|iodo|fluoro|chloride|bromide|iodide|"\n    r"deuter|\\bd-\\d|octocrylene|bumetrizole|iron|grease",\n    re.I,\n)\nHYDROCARBON_NOMENCLATURE = re.compile(\n    r"(?:^|[\\s,(\\-])(?:n-)?[a-z0-9,.\'()\\-]*"\n    r"(?:ane|ene|yne)(?:$|[\\s,;)\\-])|"\n    r"benzene|toluene|xylene|naphthalene|anthracene|phenanthrene|"\n    r"biphenyl|terphenyl|indene|styrene|squalene",\n    re.I,\n)\n\nINTERNAL_STANDARD_NAMES = {\n    "IS1": "n-Heptadecane d-36",\n    "IS2": "Benzyl butyl phthalate d-4",\n    "IS3": "Diisononylphthalate d-4",\n}\n\ndef internal_standard_code(name: str) -> str | None:\n    """Recognize IS1/IS2/IS3 even when the descriptive name follows the code."""\n    normalized=clean_name(name).replace("–","-").replace("—","-")\n    match=re.match(r"^\\s*(IS[123])(?:\\s+|[-:;_]\\s*)", normalized, re.I)\n    return match.group(1).upper() if match else None\n\n\ndef is_internal_standard(name: str) -> bool:\n    return internal_standard_code(name) is not None\n\n\ndef internal_standard_display_name(name: str) -> str:\n    code=internal_standard_code(name)\n    return f"{code} {INTERNAL_STANDARD_NAMES[code]}" if code else clean_name(name)\n\ndef is_unidentified(name: str) -> bool:\n    return clean_name(name).lower() in {"", "0", "0-0-0", "unknown", "unidentified"}\n\ndef is_hydrocarbon(name: str) -> bool:\n    """Konservative Offline-Klassifikation anhand des GC-MS-Library-Namens.\n\n    True fuer Alkane, Alkene, Alkine, Cycloalkane sowie rein aromatische KW.\n    Halogenierte, oxygenierte, N/S/P-haltige oder deuterierte Verbindungen\n    werden ausgeschlossen. Interne Standards werden immer separat behandelt.\n    """\n    n = clean_name(name)\n    if is_unidentified(n) or is_internal_standard(n):\n        return False\n    if HETERO_OR_FUNCTIONAL.search(n):\n        return False\n    return bool(HYDROCARBON_NOMENCLATURE.search(n))\n\ndef cluster_category(c: list[Peak]) -> str:\n    name, _, _, _ = consensus(c)\n    if is_internal_standard(name): return "Interne Standards, separat"\n    if is_unidentified(name): return "Nicht identifizierte Peaks"\n    if is_hydrocarbon(name): return "Kohlenwasserstoffe"\n    return "Identifizierte Substanzen ohne KW/IS"\n\ndef cluster_concentrations(c, is_areas):\n    """Return concentrations per replicate using the selected IS and RF = 1.0."""\n    values=[]\n    by_sample={p.sample:p for p in c}\n    for sample in range(3):\n        p=by_sample.get(sample)\n        area=p.area if p else None\n        is_area=is_areas[sample]\n        values.append((area / is_area) * IS_CONCENTRATION_UG_L if area is not None else None)\n    return values\n\n\ndef category_rows(clusters3, is_areas, accepted_samples=None):\n    cats = ["Kohlenwasserstoffe", "Identifizierte Substanzen ohne KW/IS",\n            "Nicht identifizierte Peaks"]\n    accepted_samples = set(range(3)) if accepted_samples is None else set(accepted_samples)\n    sums = {cat:[0.0 if i in accepted_samples else None for i in range(3)] for cat in cats}\n    for _, c in clusters3:\n        cat=cluster_category(c)\n        if cat == "Interne Standards, separat":\n            continue\n        concentrations=cluster_concentrations(c,is_areas)\n        for i,value in enumerate(concentrations):\n            if i in accepted_samples and value is not None:\n                sums[cat][i] += value\n    rows=[]\n    for cat in cats:\n        a=sums[cat]; present=[x for x in a if x is not None]\n        m=mean(present) if present else None\n        s=stdev(present) if len(present)>1 else None\n        vk=100*s/m if m not in (None,0) and s is not None else None\n        rows.append([cat,a[0],a[1],a[2],m,s,vk])\n    return rows, sums\n\n\ndef write_3of3_sheet(ws, clusters3, is_areas, selected_is, accepted_samples=None):\n    summary_headers=["Kategorie","A Summe [µg/L]","B Summe [µg/L]","C Summe [µg/L]",\n                     "Mittelwert [µg/L]","SD [µg/L]","VK [%]"]\n    ws.append(summary_headers)\n    rows, sums=category_rows(clusters3,is_areas,accepted_samples)\n    for row in rows: ws.append(row)\n    ws.append([])\n    ws.append(["Quantifizierung", f"Interner Standard: {selected_is}; c(IS) = {IS_CONCENTRATION_UG_L:.2f} µg/L; RF = 1.0; Berichtsgrenze = {REPORTING_LIMIT_UG_L:.0f} µg/L."])\n    ws.append(["Filter", "Nur Substanzen mit vollständigem 3/3-Nachweis und Konzentrationsmittelwert >= 100 µg/L werden berücksichtigt. IS1, IS2 und IS3 erscheinen als eigene Zeilen in der Ergebnisliste, werden aber nicht in Stoffklassensummen einbezogen."])\n    ws.append([]); ws.append(HEADERS); header_row=ws.max_row\n    for cid,c in clusters3:\n        ws.append(result_row(cid,c,is_areas,selected_is))\n\n    # Excel formulas for the unidentified-substance summary. The criterion is\n    # explicitly applied to the Konsens_Name column, not to CAS or status.\n    first_result_row=header_row+1\n    last_result_row=ws.max_row\n    unidentified_summary_row=next(\n        row for row in range(2, header_row)\n        if ws.cell(row,1).value == "Nicht identifizierte Peaks"\n    )\n    concentration_columns=("X","Y","Z")\n    if last_result_row >= first_result_row:\n        for target_column, concentration_column in zip((2,3,4), concentration_columns):\n            ws.cell(unidentified_summary_row,target_column).value=(\n                f\'=SUMIF($C${first_result_row}:$C${last_result_row},\'\n                f\'"Unidentified",${concentration_column}${first_result_row}:\'\n                f\'${concentration_column}${last_result_row})\'\n            )\n    else:\n        for target_column in (2,3,4):\n            ws.cell(unidentified_summary_row,target_column).value=0\n    ws.cell(unidentified_summary_row,5).value=(\n        f\'=AVERAGE(B{unidentified_summary_row}:D{unidentified_summary_row})\'\n    )\n    ws.cell(unidentified_summary_row,6).value=(\n        f\'=STDEV.S(B{unidentified_summary_row}:D{unidentified_summary_row})\'\n    )\n    ws.cell(unidentified_summary_row,7).value=(\n        f\'=IFERROR(100*F{unidentified_summary_row}/E{unidentified_summary_row},0)\'\n    )\n\n    ws.freeze_panes=f"A{header_row+1}"; ws.auto_filter.ref=f"A{header_row}:{get_column_letter(len(HEADERS))}{ws.max_row}"\n    ws.sheet_view.showGridLines=False; ws.sheet_view.topLeftCell="A1"\n    ws.sheet_view.selection=[Selection(pane="bottomLeft",activeCell=f"A{header_row+1}",sqref=f"A{header_row+1}")]\n    for r in (1,header_row):\n        for cell in ws[r]:\n            cell.fill=PatternFill("solid",fgColor=DARK); cell.font=Font(color="FFFFFF",bold=True)\n            cell.alignment=Alignment(horizontal="center",vertical="center",wrap_text=True)\n    for r in range(2,5):\n        ws.cell(r,1).font=Font(bold=True)\n        for c in range(2,7): ws.cell(r,c).number_format=\'0.00\'\n        ws.cell(r,7).number_format=\'0.00\'\n    for r in (6,7):\n        ws.cell(r,1).font=Font(bold=True,color="9C6500")\n        ws.cell(r,2).alignment=Alignment(wrap_text=True)\n    widths={1:38,2:18,3:18,4:18,5:20,6:16,7:12}\n    for c,w in widths.items(): ws.column_dimensions[get_column_letter(c)].width=w\n    for c in range(8,30): ws.column_dimensions[get_column_letter(c)].width=15\n    for row in ws.iter_rows(min_row=header_row+1):\n        for cell in row: cell.alignment=Alignment(vertical="top",wrap_text=cell.column in (3,6))\n    for col in [8,9,18,19,20]:\n        for row in range(header_row+1,ws.max_row+1): ws.cell(row,col).number_format="0.000"\n    for col in [10,11,14,15,21,22,23,24,25,26,27,28]:\n        for row in range(header_row+1,ws.max_row+1): ws.cell(row,col).number_format=\'0.00\'\n    ws.column_dimensions[\'AC\'].width=18\n    return rows, sums, header_row\n\n\nHEADERS=["Cluster_ID","Status","Konsens_Name","Konsens_CAS","ID_Übereinstimmung","Hinweis","N",\n "RT Mittel [min]","RT SD [min]","Area Mittel","Area SD","Area CV [%]","Area% Mittel",\n "Height Mittel","Height SD","Height CV [%]","SI Mittel","RT A","RT B","RT C","Area A","Area B","Area C",\n "c A [µg/L]","c B [µg/L]","c C [µg/L]","c Mittel [µg/L]","c SD [µg/L]","c VK [%]","Verwendeter IS"]\nDETAIL=["Cluster_ID","N","RT-Spanne","Konsens_Name","Konsens_CAS","ID_Übereinstimmung","Hinweis"] + \\\n [f"{x}_{l}" for l in LETTERS for x in ("RT","Area","Area%","Height","Height%","SI","Name","CAS")]\n\ndef result_row(cid,c,is_areas=None,selected_is=""):\n    d={p.sample:p for p in c}; name,cas,agree,note=consensus(c); n=len(c)\n    rts=[p.rt for p in c]; areas=[p.area for p in c]; heights=[p.height for p in c]\n    is_code=internal_standard_code(name)\n    rt_spread=max(rts)-min(rts) if rts else 0.0\n    status = ("Interner Standard – nominal" if is_code else\n              ("3/3 – CAS-gestützt" if n == 3 and rt_spread > PRIMARY_RT_TOLERANCE_MIN + 1e-12 else\n               ("3/3 – RT-sicher" if n == 3 else ("2/3 – prüfen" if n == 2 else "1/3 – Einzelpeak"))))\n    if is_code:\n        concentrations=[IS_CONCENTRATION_UG_L if i in d else None for i in range(3)]\n        selected_is=is_code\n    else:\n        concentrations=cluster_concentrations(c,is_areas) if is_areas else [None,None,None]\n    c_present=[x for x in concentrations if x is not None]\n    c_mean=mean(c_present) if c_present else None\n    c_sd=stdev(c_present) if len(c_present)>1 else None\n    c_cv=100*c_sd/c_mean if c_mean not in (None,0) and c_sd is not None else None\n    return [cid, status, name,cas,agree,note,n,\n        avg(rts),sd(rts),avg(areas),sd(areas),cv(areas),avg([p.area_pct for p in c]),\n        avg(heights),sd(heights),cv(heights),avg([p.si for p in c])] + \\\n        [d[i].rt if i in d else None for i in range(3)] + [d[i].area if i in d else None for i in range(3)] + \\\n        concentrations + [c_mean,c_sd,c_cv,selected_is]\n\ndef detail_row(cid,c):\n    name,cas,agree,note=consensus(c); d={p.sample:p for p in c}; rts=[p.rt for p in c]\n    out=[cid,len(c),max(rts)-min(rts),name,cas,agree,note]\n    for i in range(3):\n        p=d.get(i); out += [p.rt,p.area,p.area_pct,p.height,p.height_pct,p.si,p.name,p.cas] if p else [None]*8\n    return out\n\nDARK="1F4E78"; LIGHT="D9EAF7"; ORANGE="FCE4D6"; RED="F4CCCC"\ndef style_table(ws, widths=None):\n    ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions; ws.sheet_view.showGridLines=False\n    ws.sheet_view.topLeftCell = "A1"\n    ws.sheet_view.selection = [Selection(pane="bottomLeft", activeCell="A2", sqref="A2")]\n    for c in ws[1]:\n        c.fill=PatternFill("solid",fgColor=DARK); c.font=Font(color="FFFFFF",bold=True); c.alignment=Alignment(horizontal="center",vertical="center",wrap_text=True)\n    ws.row_dimensions[1].height=32\n    for row in ws.iter_rows(min_row=2):\n        for c in row: c.alignment=Alignment(vertical="top",wrap_text=c.column in (3,6))\n    for col in [8,9,18,19,20]:\n        for c in ws.iter_cols(min_col=col,max_col=col,min_row=2):\n            for x in c: x.number_format="0.000"\n    for col in [10,11,14,15,21,22,23]:\n        if col<=ws.max_column:\n            for c in ws.iter_cols(min_col=col,max_col=col,min_row=2):\n                for x in c: x.number_format=\'#,##0\'\n    for col in [12,13,16,17]:\n        if col<=ws.max_column:\n            for c in ws.iter_cols(min_col=col,max_col=col,min_row=2):\n                for x in c: x.number_format=\'0.00\'\n    defaults={1:11,2:16,3:40,4:16,5:18,6:48,7:7,8:14,9:12,10:15,11:14,12:13,13:13,14:15,15:14,16:14,17:11,18:11,19:11,20:11,21:15,22:15,23:15}\n    for k,v in (widths or defaults).items(): ws.column_dimensions[get_column_letter(k)].width=v\n    # Beide Regeln setzen eine bestimmte Spaltenbedeutung voraus und werden\n    # deshalb ueber die Kopfzeile aufgeloest. Auf "Abgleich_Details" und\n    # "Nicht_zugeordnete_Signale" stehen an denselben Positionen andere\n    # Spalten, dort darf die jeweilige Regel nicht greifen.\n    header_columns={str(c.value).strip():c.column for c in ws[1] if c.value is not None}\n    if ws.max_row>=2:\n        status_column=header_columns.get("Status")\n        if status_column:\n            letter=get_column_letter(status_column)\n            ws.conditional_formatting.add(f"{letter}2:{letter}{ws.max_row}",FormulaRule(formula=[f\'LEFT({letter}2,3)="2/3"\'],fill=PatternFill("solid",fgColor=ORANGE)))\n        agreement_column=header_columns.get("ID_Übereinstimmung")\n        if agreement_column:\n            letter=get_column_letter(agreement_column)\n            ws.conditional_formatting.add(f"{letter}2:{letter}{ws.max_row}",FormulaRule(formula=[f\'{letter}2="Nein"\'],fill=PatternFill("solid",fgColor=RED)))\n\ndef normalized_identity_name(name: str) -> str:\n    return re.sub(r"[^a-z0-9]+", "", clean_name(name).casefold())\n\n\ndef duplicate_replicate_pairs(all_peaks: list[Peak]):\n    """Return pairs of replicates whose parsed peak records are fully identical."""\n    signatures={}\n    for sample in range(3):\n        signatures[sample]=[\n            (p.rt,p.area,p.area_pct,p.height,p.height_pct,p.si,p.name,p.cas)\n            for p in all_peaks if p.sample==sample\n        ]\n    return [\n        (LETTERS[a],LETTERS[b],len(signatures[a]))\n        for a in range(3) for b in range(a+1,3)\n        if signatures[a] and signatures[a]==signatures[b]\n    ]\n\n\ndef common_identity_candidates(all_peaks: list[Peak]):\n    """Find the best A/B/C combination for every common valid CAS and name."""\n    by_cas={i:{} for i in range(3)}\n    by_name={i:{} for i in range(3)}\n    for p in all_peaks:\n        if valid_cas(p.cas):\n            by_cas[p.sample].setdefault(p.cas,[]).append(p)\n        key=normalized_identity_name(p.name)\n        if key and not is_unidentified(p.name):\n            by_name[p.sample].setdefault(key,[]).append(p)\n\n    def best_candidates(indexes, identity_type):\n        common=set(indexes[0]) & set(indexes[1]) & set(indexes[2])\n        out=[]\n        for key in sorted(common):\n            candidates=[]\n            for a in indexes[0][key]:\n                for b in indexes[1][key]:\n                    for c in indexes[2][key]:\n                        rts=[a.rt,b.rt,c.rt]\n                        spread=max(rts)-min(rts)\n                        m=mean(rts)\n                        candidates.append((spread,sum(abs(x-m) for x in rts),a.row,b.row,c.row,a,b,c))\n            if candidates:\n                _,_,_,_,_,a,b,c=min(candidates,key=lambda x:x[:5])\n                out.append({"identity_type":identity_type,"identity":key,"peaks":[a,b,c],\n                            "rt_spread":max(a.rt,b.rt,c.rt)-min(a.rt,b.rt,c.rt)})\n        return out\n    return best_candidates(by_cas,"CAS"), best_candidates(by_name,"NAME")\n\n\ndef write_matching_qc_sheet(ws, qc):\n    ws.append(["Matching-QC","Wert","Status / Erläuterung"])\n    rows=[\n        ("Rohpeaks A",qc["raw_counts"][0],"Information"),\n        ("Rohpeaks B",qc["raw_counts"][1],"Information"),\n        ("Rohpeaks C",qc["raw_counts"][2],"Information"),\n        ("3/3 RT-sicher",qc["rt_safe"],"OK"),\n        ("3/3 CAS-gestützt",qc["cas_supported"],"Prüfbar und vollständig dokumentiert"),\n        ("Gemeinsame gültige CAS ohne Ergebnis",qc["missing_common_cas"],"OK" if qc["missing_common_cas"]==0 else "FEHLER"),\n        ("Ungeklärte Identitätskandidaten",qc["review_candidates"],"OK" if qc["review_candidates"]==0 else "PRÜFEN"),\n        ("Identische Replikatpaare",len(qc["duplicate_pairs"]),"OK" if not qc["duplicate_pairs"] else "KRITISCH PRÜFEN"),\n        ("Peak-Bilanz",qc["accounted_peaks"],f"von {qc[\'raw_total\']} Rohpeaks in Clustern erfasst"),\n    ]\n    for row in rows: ws.append(row)\n    for a,b,count in qc["duplicate_pairs"]:\n        ws.append(["Duplikatwarnung",f"{a} = {b}",f"Alle {count} geparsten Peakzeilen sind identisch."])\n    style_table(ws,{1:34,2:22,3:70})\n    for r in range(2,ws.max_row+1):\n        status=str(ws.cell(r,3).value or "")\n        if "FEHLER" in status or "KRITISCH" in status:\n            for c in ws[r]: c.fill=PatternFill("solid",fgColor=RED)\n        elif "PRÜFEN" in status:\n            for c in ws[r]: c.fill=PatternFill("solid",fgColor=ORANGE)\n\n\ndef write_unresolved_sheet(ws, candidates, is_areas):\n    headers=["Kandidaten_ID","Priorität","Identitätstyp","Name","CAS","Grund","RT-Spanne [min]",\n             "RT A","RT B","RT C","Area A","Area B","Area C","c A [µg/L]","c B [µg/L]","c C [µg/L]",\n             "SI A","SI B","SI C","Quellzeile A","Quellzeile B","Quellzeile C"]\n    ws.append(headers)\n    for cid,item in enumerate(candidates,1):\n        ps=item["peaks"]\n        concentrations=[(p.area/is_areas[i])*IS_CONCENTRATION_UG_L if p.area is not None else None for i,p in enumerate(ps)]\n        priority="HOCH" if item["identity_type"]=="CAS" else "MITTEL"\n        reason=("CAS in A/B/C, aber außerhalb der automatischen CAS-Toleranz" if item["identity_type"]=="CAS"\n                else "Name in A/B/C, aber keine sichere CAS-/RT-Zuordnung")\n        ws.append([cid,priority,item["identity_type"],ps[0].name,ps[0].cas,reason,item["rt_spread"],\n                   ps[0].rt,ps[1].rt,ps[2].rt,ps[0].area,ps[1].area,ps[2].area,\n                   concentrations[0],concentrations[1],concentrations[2],ps[0].si,ps[1].si,ps[2].si,\n                   ps[0].row,ps[1].row,ps[2].row])\n    style_table(ws,{1:13,2:12,3:16,4:54,5:18,6:58,7:17,8:12,9:12,10:12,11:16,12:16,13:16,14:16,15:16,16:16,17:10,18:10,19:10,20:14,21:14,22:14})\n\n\ndef process(inp:Path,out:Path,tol:float, replicate_threshold:float=0.25, selected_is:str=""):\n    selected_is=clean_name(selected_is).upper()\n    if selected_is not in ALLOWED_INTERNAL_STANDARDS:\n        raise ValueError("Bitte vor der Verarbeitung IS1, IS2 oder IS3 auswählen.")\n    wb=load_workbook(inp)\n    # Nur Arbeitsblätter verwenden, deren Kopfzeile direkt in Zeile 1 steht.\n    # Ergebnis- oder Hilfsblätter werden dadurch nicht versehentlich als Rohdaten\n    # eingelesen. Wenn _A/_B/_C vorhanden sind, wird diese Reihenfolge erzwungen.\n    candidates=[]\n    by_label={}\n    for sheet_name in wb.sheetnames:\n        ws_candidate=wb[sheet_name]\n        row1_headers={str(c.value).strip() for c in ws_candidate[1] if c.value is not None}\n        if not REQUIRED.issubset(row1_headers):\n            continue\n        candidates.append(sheet_name)\n        for label in LETTERS:\n            if re.search(rf"(?:^|[_\\- ]){label}$", sheet_name, re.I):\n                by_label[label]=sheet_name\n    if all(label in by_label for label in LETTERS):\n        sheets=[by_label[label] for label in LETTERS]\n    elif len(candidates)==3:\n        sheets=candidates\n    else:\n        raise ValueError(\n            "Es werden genau drei Rohdatenblätter erwartet. Jedes Blatt muss die "\n            "Pflichtspalten bereits in Zeile 1 enthalten; bevorzugte Endungen: "\n            "_A, _B und _C. Gefunden: " + (", ".join(candidates) or "keine")\n        )\n    allp=[]\n    source_counts={}\n    for i,s in enumerate(sheets):\n        sheet_peaks=read_peaks(wb[s],i)\n        source_counts[s]=len(sheet_peaks)\n        allp += sheet_peaks\n    duplicate_pairs=duplicate_replicate_pairs(allp)\n    common_cas_candidates, common_name_candidates=common_identity_candidates(allp)\n    # Der ausgewählte interne Standard muss in jedem Rohdatenblatt genau einmal\n    # mit einer numerischen Area > 0 vorhanden sein. Andernfalls wird abgebrochen.\n    is_areas=[]\n    is_rows=[]\n    for sample,label in enumerate(LETTERS):\n        matches=[p for p in allp if p.sample==sample and internal_standard_code(p.name)==selected_is]\n        if len(matches)!=1:\n            raise ValueError(\n                f"Quantifizierung abgebrochen: {selected_is} muss im Replikat {label} "\n                f"genau einmal vorhanden sein; gefunden: {len(matches)}."\n            )\n        if matches[0].area is None or matches[0].area <= 0:\n            raise ValueError(\n                f"Quantifizierung abgebrochen: {selected_is} hat im Replikat {label} "\n                "keine belastbare Area (> 0)."\n            )\n        is_areas.append(matches[0].area); is_rows.append(matches[0].row)\n    raw_totals, qc_rows, excluded_samples, qc_alert = replicate_area_qc(allp, replicate_threshold)\n    if excluded_samples:\n        labels=", ".join(LETTERS[i] for i in sorted(excluded_samples))\n        raise ValueError(\n            "Quantifizierung abgebrochen: Die 3/3-Konzentrationsbewertung erfordert drei "\n            f"akzeptierte Replikate. Replikat-QC würde ausschließen: {labels}. {qc_alert}"\n        )\n    clusters_all=cluster_peaks(allp,tol)\n    complete=[c for c in clusters_all if len(c)==3 and {p.sample for p in c}==set(range(3))]\n    quantified=[]\n    below_limit=0\n    for c in complete:\n        name,_,_,_=consensus(c)\n        if is_internal_standard(name):\n            # IS1, IS2 and IS3 remain visible in the result sheets. They are\n            # exempt from the 100 µg/L analyte reporting limit and from sums.\n            quantified.append(c)\n            continue\n        concentrations=cluster_concentrations(c,is_areas)\n        concentration_mean=mean(concentrations)\n        if concentration_mean >= REPORTING_LIMIT_UG_L:\n            quantified.append(c)\n        else:\n            below_limit += 1\n    # Ab diesem Punkt basieren sämtliche DIN-Ergebnislisten und Summen nur auf\n    # vollständigen, quantifizierten Clustern oberhalb/gleich der Berichtsgrenze.\n    clusters=quantified\n    analysis_peaks=[p for c in clusters for p in c]\n    result_cas={consensus(c)[1] for c in clusters if valid_cas(consensus(c)[1])}\n    result_names={normalized_identity_name(consensus(c)[0]) for c in clusters}\n    missing_common_cas=[item for item in common_cas_candidates if item["identity"] not in result_cas]\n    unresolved=[]\n    unresolved.extend(item for item in missing_common_cas if item["rt_spread"] <= REVIEW_RT_TOLERANCE_MIN)\n    unresolved.extend(item for item in common_name_candidates\n                      if item["identity"] not in result_names and item["rt_spread"] <= 2.0\n                      and not any(x["identity_type"]=="CAS" and x["peaks"]==item["peaks"] for x in unresolved))\n    rt_safe=sum((max(p.rt for p in c)-min(p.rt for p in c)) <= PRIMARY_RT_TOLERANCE_MIN+1e-12 for c in clusters)\n    cas_supported=sum((max(p.rt for p in c)-min(p.rt for p in c)) > PRIMARY_RT_TOLERANCE_MIN+1e-12 for c in clusters)\n    matching_qc={"raw_counts":[source_counts[s] for s in sheets],"raw_total":len(allp),\n                 "rt_safe":rt_safe,"cas_supported":cas_supported,\n                 "missing_common_cas":len(missing_common_cas),"review_candidates":len(unresolved),\n                 "duplicate_pairs":duplicate_pairs,"accounted_peaks":sum(len(c) for c in clusters_all)}\n    # Vorhandene Ergebnisblätter bei Wiederholung sauber ersetzen.\n    for s in ["Gemittelt","Nur_3_von_3","Abgleich_Details","Replikat_QC","Matching_QC","Nicht_zugeordnete_Signale","Methodik"]:\n        if s in wb.sheetnames: del wb[s]\n    ws=wb.create_sheet("Gemittelt",0); ws.append(HEADERS)\n    for cid,c in enumerate(clusters,1):\n        # Vollständige Gesamtliste: auch Peaks aus nur einem Replikat bleiben sichtbar.\n        ws.append(result_row(cid,c,is_areas,selected_is))\n    style_table(ws)\n    accepted_samples=set(range(3))\n    clusters3=[(cid,c) for cid,c in enumerate(clusters,1)]\n    totals=raw_totals\n    w3=wb.create_sheet("Nur_3_von_3",1)\n    category_summary, category_sums, summary_header_row = write_3of3_sheet(w3,clusters3,is_areas,selected_is,accepted_samples)\n    wd=wb.create_sheet("Abgleich_Details",2); wd.append(DETAIL)\n    for cid,c in enumerate(clusters,1): wd.append(detail_row(cid,c))\n    style_table(wd,{1:11,2:7,3:11,4:38,5:16,6:18,7:48,8:11,9:15,10:11,11:15,12:11,13:9,14:36,15:16,16:11,17:15,18:11,19:15,20:11,21:9,22:36,23:16,24:11,25:15,26:11,27:15,28:11,29:9,30:36,31:16})\n    wq=wb.create_sheet("Replikat_QC",3)\n    write_replicate_qc_sheet(wq, qc_rows, qc_alert)\n    wmq=wb.create_sheet("Matching_QC",4)\n    write_matching_qc_sheet(wmq,matching_qc)\n    wur=wb.create_sheet("Nicht_zugeordnete_Signale",5)\n    write_unresolved_sheet(wur,unresolved,is_areas)\n    wm=wb.create_sheet("Methodik",6); wm.sheet_view.showGridLines=False\n    method=[("Auswertung",f"GC-MS Dreifachbestimmung: {sheets[0]}, {sheets[1]}, {sheets[2]}"),\n      ("Datenbereich","Jedes Eingabeblatt muss die Kopfzeile direkt in Zeile 1 enthalten; alle Datenzeilen werden ab Zeile 2 bis zur letzten belegten Zeile verarbeitet."),\n      ("Quantifizierung",f"Alle Substanzen wurden mit {selected_is} und RF = 1,0 berechnet: c(Substanz) = Area(Substanz) / Area({selected_is}) × {IS_CONCENTRATION_UG_L:.2f} µg/L."),\n      ("IS-Areas",f"A: {is_areas[0]:.2f}; B: {is_areas[1]:.2f}; C: {is_areas[2]:.2f}. Quellzeilen: {is_rows[0]}, {is_rows[1]}, {is_rows[2]}."),\n      ("Berichtsgrenze",f"Nur vollständige 3/3-Cluster mit Konzentrationsmittelwert >= {REPORTING_LIMIT_UG_L:.0f} µg/L werden in Ergebnislisten und Summen berücksichtigt. Ausgefiltert: {below_limit}."),\n      ("Unidentified-Summe","Die Summenzeile \'Nicht identifizierte Peaks\' verwendet dynamische Excel-SUMIF-Formeln. Kriterium ist Konsens_Name = Unidentified; summiert werden c A, c B und c C in µg/L."),\n      ("Replikat-QC",f"Vor der Peakzuordnung wird je Replikat die Summe aller detektierten Peak-Areas gebildet. Abweichung > {replicate_threshold:.0%} gegenüber dem Mittelwert der anderen zwei Replikate führt beim Replikat mit der größten Abweichung zum Ausschluss aus allen Berechnungen und erzeugt einen Warntext; pro Lauf wird höchstens ein Replikat ausgeschlossen."),\n      ("Replikat-QC Ergebnis",qc_alert),\n      ("Abgleichskriterium",f"Peaks mit identischer gültiger CAS-Nummer wurden zuerst bis zu einer RT-Spanne von {CAS_MATCH_TOLERANCE_MIN:.3f} min als CAS-gestützte 3/3-Cluster zugeordnet; übrige Peaks anschließend RT-basiert mit maximal {tol:.3f} min."),\n      ("Vollständigkeitskontrolle",f"Nach dem Clustering wurden gemeinsame CAS- und Namensidentitäten unabhängig abgeglichen. Gemeinsame gültige CAS ohne Ergebnis: {len(missing_common_cas)}; Review-Kandidaten: {len(unresolved)}."),\n      ("Duplikatkontrolle",("Keine vollständig identischen Replikatblätter erkannt." if not duplicate_pairs else "KRITISCH: Identische Replikatpaare erkannt: " + ", ".join(f"{a}={b}" for a,b,_ in duplicate_pairs))),\n      ("Zuordnung","Pro Cluster ist höchstens ein Peak je Replikat zulässig; bei mehreren Kandidaten wird der Peak mit der kleinsten Abweichung zum bisherigen Cluster-Mittel zugeordnet."),\n      ("Mittelwert","Mittelwerte werden nur aus tatsächlich vorhandenen Messwerten berechnet; fehlende Replikate werden nicht als Null behandelt."),\n      ("Gemittelt","Vollständige Peakliste: enthält 1/3-, 2/3- und 3/3-Cluster. Einzelpeaks werden nicht gemittelt, sondern als 1/3 – Einzelpeak ausgewiesen."),\n      ("Nur_3_von_3","Enthält die Kategoriesummen der vollständigen 3/3-Cluster und darunter die 3/3-Einzelliste ohne Kohlenwasserstoffe."),\n      ("Kohlenwasserstoffe","Offline-Erkennung anhand des Stoffnamens: positive KW-Nomenklatur (Alkane, Alkene, Alkine, Cycloalkane und reine Aromaten) nach vorherigem Ausschluss heteroatomhaltiger Funktionen, Halogenverbindungen und interner Standards."),\n      ("Kategorie Area %","Area-Prozent je Replikat = Kategoriesumme der 3/3-Peaks dividiert durch die Summe aller Peak-Areas des jeweiligen Rohdatenblatts."),\n      ("Identifikation","Konsens bevorzugt eine in mindestens zwei Replikaten übereinstimmende, formal gültige CAS-Nummer; andernfalls den häufigsten Namen."),\n      ("Prüfhinweis","Abweichende Library-Treffer werden markiert. Die Identifizierung ist vor regulatorischer oder toxikologischer Bewertung fachlich zu prüfen."),\n      ("CAS-Korrektur","Von Excel versehentlich als Datum interpretierte CAS-Werte werden in das Muster Jahr-Monat-Tag zurückgeführt."),\n      ("Interne Standards",f"IS1, IS2 und IS3 werden mit ihren vollständigen Bezeichnungen in den Ergebnislisten ausgegeben. {selected_is} dient der Quantifizierung aller Analyten; interne Standards werden nicht in Stoffklassensummen einbezogen."),\n      ("Kennzahlen","Ausgegeben werden Mittelwert, Stichproben-SD und Variationskoeffizient für Area und Height sowie Mittelwerte für RT, Area% und SI.")]\n    for x in method: wm.append(x)\n    wm.column_dimensions[\'A\'].width=24; wm.column_dimensions[\'B\'].width=120\n    for c in wm[\'A\']: c.font=Font(bold=True,color="666666")\n    for row in wm.iter_rows():\n        for c in row: c.alignment=Alignment(vertical="top",wrap_text=True)\n    for raw in sheets:\n        rws=wb[raw]\n        # Eingabeblätter immer sichtbar ab Zeile 1 öffnen. Alte gespeicherte Scrollpositionen\n        # (z. B. A217/A214) und Mehrfachmarkierungen dürfen nicht übernommen werden.\n        rws.freeze_panes="A2"\n        rws.sheet_view.topLeftCell = "A1"\n        rws.sheet_view.selection = [Selection(pane="bottomLeft", activeCell="A2", sqref="A2")]\n        rws.auto_filter.ref = None\n        for row_no in range(1, rws.max_row + 1):\n            rws.row_dimensions[row_no].hidden = False\n        rws.sheet_view.showGridLines=False\n        for c in rws[1]: c.fill=PatternFill("solid",fgColor=DARK); c.font=Font(color="FFFFFF",bold=True)\n        for col in range(1,rws.max_column+1): rws.column_dimensions[get_column_letter(col)].width=14\n        rws.column_dimensions[\'K\'].width=52; rws.column_dimensions[\'L\'].width=18\n    try: wb.calculation.fullCalcOnLoad=True; wb.calculation.forceFullCalc=True; wb.calculation.calcMode="auto"\n    except Exception: pass\n    wb.save(out)\n    return {"raw":len(allp),"source_sheets":sheets,"source_rows":source_counts,"selected_is":selected_is,"is_areas":is_areas,"below_reporting_limit":below_limit,"internal_standard_rows":sum(is_internal_standard(consensus(c)[0]) for c in clusters),"analysis_peaks":len(analysis_peaks),"excluded_replicates":[LETTERS[i] for i in sorted(excluded_samples)],"qc_alert":qc_alert,"clusters":len(clusters),"gemittelt_gesamt":len(clusters),"n2plus":sum(len(c)>=2 for c in clusters),"n3":sum(len(c)==3 for c in clusters),"complete_accepted":len(clusters3),"singles":sum(len(c)==1 for c in clusters),"hydrocarbon_3of3":sum(cluster_category(c)=="Kohlenwasserstoffe" for _,c in clusters3),"cas_supported_3of3":cas_supported,"missing_common_cas":len(missing_common_cas),"review_candidates":len(unresolved),"duplicate_replicate_pairs":duplicate_pairs}\n\ndef main():\n    ap=argparse.ArgumentParser()\n    ap.add_argument("input",type=Path); ap.add_argument("-o","--output",type=Path)\n    ap.add_argument("--rt-toleranz",type=float,default=0.035)\n    ap.add_argument("--replikat-grenzwert",type=float,default=0.25, help="Relativer Grenzwert, Standard 0.25 = 25 %%")\n    ap.add_argument("--is",dest="selected_is",choices=["IS1","IS2","IS3"],required=True,help="Interner Standard für alle Substanzen")\n    a=ap.parse_args(); out=a.output or a.input.with_name(a.input.stem+"_gemittelt.xlsx")\n    stats=process(a.input,out,a.rt_toleranz,a.replikat_grenzwert,a.selected_is); print(out); print(stats)\n'
_GC_MS_MITTELN = _types.ModuleType("embedded_gc_ms_mitteln")
_GC_MS_MITTELN.__file__ = __file__
_GC_MS_MITTELN.__package__ = ""
import sys as _sys
_sys.modules[_GC_MS_MITTELN.__name__] = _GC_MS_MITTELN
exec(compile(_GC_MS_MITTELN_SOURCE, "<embedded_gc_ms_mitteln>", "exec"), _GC_MS_MITTELN.__dict__)

# ---------------------------------------------------------------------------
# DIN SPEC 91521 GC-MS triplicate workflow
# ---------------------------------------------------------------------------
def process_din_spec_workbook(
    input_path: Path,
    output_path: Path,
    tolerance: float = 0.035,
    replicate_threshold: float = 0.25,
    selected_is: str = "",
) -> dict[str, Any]:
    """Run DIN SPEC workflow with analyst-selected internal standard."""
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    ensure_excel_files_closed([input_path])
    if input_path == output_path:
        raise ValueError(
            "Die Quelldatei darf nicht überschrieben werden. "
            "Bitte einen neuen Ausgabepfad wählen."
        )
    info = _GC_MS_MITTELN.process(
        input_path, output_path, tolerance, replicate_threshold, selected_is
    )
    info["output"] = str(output_path)
    return info



# ---------------------------------------------------------------------------
# DIN SPEC 91521 MACE Word report
# ---------------------------------------------------------------------------
DIN_MACE_FACTORS = {"A": 148000.0, "B": 6200000.0, "C": 769200000.0}
DIN_UNIDENTIFIED_MACE_UG_KG_BW_D = 0.0025
DIN_REPORT_GREEN = "C6EFCE"
DIN_REPORT_RED = "FFC7CE"
DIN_REPORT_YELLOW = "FFF2CC"
DIN_REPORT_BLUE = "17365D"


def _din_norm_default_path() -> Path:
    """Return a sensible local default for the DIN SPEC PDF."""
    candidates = [
        Path(__file__).resolve().with_name("DINSPEC91521_N0029_DIN_SPEC_91521_final_draft_for_publication.pdf"),
        Path(r"C:/Skript/DINSPEC91521_N0029_DIN_SPEC_91521_final_draft_for_publication.pdf"),
    ]
    return next((item for item in candidates if item.is_file()), candidates[0])


def _extract_pdf_pages(pdf_path: Path, first_page: int = 32, last_page: int = 101) -> list[tuple[int, str]]:
    """Extract page text with pypdf/PyPDF2 and retain physical PDF page numbers."""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError as exc:
            raise RuntimeError(
                "Für den MACE-Report fehlt pypdf. Installation: pip install pypdf"
            ) from exc
    reader = PdfReader(str(pdf_path))
    result = []
    for page_number in range(first_page, min(last_page, len(reader.pages)) + 1):
        result.append((page_number, reader.pages[page_number - 1].extract_text() or ""))
    return result


def _din_parse_mace_lookup(pdf_path: Path) -> dict[str, dict[str, Any]]:
    """Create a CAS-keyed Annex C lookup with normalized MACE values."""
    lookup: dict[str, dict[str, Any]] = {}
    cas_pattern = re.compile(r"(?<!\d)(\d{2,7}-\d{2}-\d)(?!\d)")
    value_pattern = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(µg/kg\s*bw/d|mg/kg\s*bw/d)", re.I)
    for page_number, page_text in _extract_pdf_pages(pdf_path):
        compact = re.sub(r"\s+", " ", page_text)
        matches = list(cas_pattern.finditer(compact))
        for position, match in enumerate(matches):
            cas_number = match.group(1)
            # Inspect only the current table record, ending at the next CAS.
            end = matches[position + 1].start() if position + 1 < len(matches) else min(len(compact), match.end() + 500)
            segment = compact[match.end():end]
            lower = segment.casefold()
            numeric = value_pattern.search(segment)
            if "no hazard identified" in lower:
                record = {"kind": "no_hazard", "page": page_number}
            elif numeric:
                original = float(numeric.group(1).replace(",", "."))
                unit = numeric.group(2)
                normalized_unit = unit.casefold().replace("μ", "µ")
                mace_mg = original / 1000.0 if "µg/" in normalized_unit else original
                record = {"kind": "numeric", "page": page_number,
                          "value_original": original, "unit": unit, "mace_mg": mace_mg}
            else:
                record = {"kind": "unknown", "page": page_number}
            previous = lookup.get(cas_number)
            if previous is None or (record["kind"] == "numeric" and previous["kind"] != "numeric"):
                lookup[cas_number] = record
    return lookup


def _din_find_report_rows(
        workbook_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, float], str, list[list[Any]]]:
    """Smartly detect a report-ready table and ignore every hidden Excel row."""
    wb = load_workbook(workbook_path, data_only=True, read_only=False)
    # Preferred laboratory reporting sheet.
    if "Sheet1" in wb.sheetnames:
        ws = wb["Sheet1"]
        header_row = next((r for r in range(1, min(ws.max_row, 40) + 1)
                           if normalized_header(ws.cell(r, 3).value) in {"name", "substancename"}
                           and "cas" in normalized_header(ws.cell(r, 4).value)), None)
        if header_row:
            records = []
            for row in range(header_row + 1, ws.max_row + 1):
                if ws.row_dimensions[row].hidden:
                    continue
                name = text(ws.cell(row, 3).value)
                concentration = numeric_value(ws.cell(row, 9).value)
                if not name or concentration is None or name.upper().startswith("IS"):
                    continue
                records.append({
                    "rt": numeric_value(ws.cell(row, 1).value),
                    "ri": numeric_value(ws.cell(row, 2).value),
                    "name": name,
                    "cas": text(ws.cell(row, 4).value),
                    "match": numeric_value(ws.cell(row, 5).value),
                    "concentration_mg_kg": concentration,
                })
            # The visible summary values are formula results in the source workbook.
            summary = {"Sum of Hydrocarbons": 0.0,
                       "Identified substances without hydrocarbons": 0.0,
                       "Unidentified": 0.0}
            # In the laboratory template the summary block is in columns Q:R.
            # Scan the entire top area by label so that moved columns remain supported.
            for summary_row in range(1, min(header_row, 25)):
                for label_col in range(1, ws.max_column):
                    label = text(ws.cell(summary_row, label_col).value).casefold()
                    value = numeric_value(ws.cell(summary_row, label_col + 1).value)
                    if value is None:
                        continue
                    if "sum of hydrocarbons" in label or "kohlenwasser" in label:
                        summary["Sum of Hydrocarbons"] = value
                    elif "identified substances without hydrocarbons" in label or "identifizierte substanzen ohne" in label:
                        summary["Identified substances without hydrocarbons"] = value
                    elif label.strip() in {"unidentified", "unidentified peaks", "nicht identifizierte peaks"}:
                        summary["Unidentified"] = value
            qc_rows = []
            if "Sheet2" in wb.sheetnames:
                qc_ws = wb["Sheet2"]
                # Requested source range: Sheet2!A12:F15.
                for qc_row in range(12, min(15, qc_ws.max_row) + 1):
                    qc_rows.append([qc_ws.cell(qc_row, col).value for col in range(1, 7)])
            return records, summary, "Sheet1", qc_rows

    # Fallback to the output generated by the DIN workflow.
    if "Nur_3_von_3" in wb.sheetnames:
        ws = wb["Nur_3_von_3"]
        header_row = None
        for row in range(1, ws.max_row + 1):
            values = {text(ws.cell(row, c).value): c for c in range(1, ws.max_column + 1)}
            if "Konsens_Name" in values and "Konsens_CAS" in values:
                header_row = row
                columns = values
                break
        if header_row:
            records = []
            for row in range(header_row + 1, ws.max_row + 1):
                if ws.row_dimensions[row].hidden:
                    continue
                name = text(ws.cell(row, columns["Konsens_Name"]).value)
                if not name or name.upper().startswith("IS"):
                    continue
                conc_ug_l = numeric_value(ws.cell(row, columns.get("c Mittel [µg/L]", 27)).value)
                if conc_ug_l is None:
                    continue
                records.append({
                    "rt": numeric_value(ws.cell(row, columns.get("RT Mittel [min]", 8)).value),
                    "ri": None, "name": name,
                    "cas": text(ws.cell(row, columns["Konsens_CAS"]).value),
                    "match": numeric_value(ws.cell(row, columns.get("SI Mittel", 17)).value),
                    # DIN extraction/migration uses 1 g pellet per 1 ml liquid; µg/L = mg/kg pellets.
                    "concentration_mg_kg": conc_ug_l,
                })
            summary = {}
            for row in range(2, 5):
                label = text(ws.cell(row, 1).value)
                mean_value = numeric_value(ws.cell(row, 5).value) or 0.0
                mapped = {
                    "Kohlenwasserstoffe": "Sum of Hydrocarbons",
                    "Identifizierte Substanzen ohne KW/IS": "Identified substances without hydrocarbons",
                    "Nicht identifizierte Peaks": "Unidentified",
                }.get(label, label)
                summary[mapped] = mean_value
            return records, summary, "Nur_3_von_3", []
    raise ValueError(
        "Keine reportfähige Tabelle gefunden. Erwartet wird Sheet1 oder das DIN-Ergebnisblatt Nur_3_von_3."
    )


def create_din_mace_word_report(source_workbook: Path, norm_pdf: Path, word_path: Path) -> dict[str, Any]:
    """Create the portrait DIN MACE report with red/green/yellow traffic lights."""
    try:
        from docx import Document
        from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Cm, Pt, RGBColor
    except ImportError as exc:
        raise RuntimeError("Für den MACE-Report fehlt python-docx. Installation: pip install python-docx") from exc

    rows, summary, source_sheet, qc_rows = _din_find_report_rows(source_workbook)
    lookup = _din_parse_mace_lookup(norm_pdf)
    evaluated = []
    counts = {level: {"confirm": 0, "not_confirm": 0, "unknown": 0} for level in "ABC"}
    for item in rows:
        unidentified = item["name"].strip().casefold() in {"unidentified", "unknown"}
        if unidentified:
            mace = {"kind": "numeric", "page": 15,
                    "value_original": DIN_UNIDENTIFIED_MACE_UG_KG_BW_D,
                    "unit": "µg/kg bw/d", "mace_mg": DIN_UNIDENTIFIED_MACE_UG_KG_BW_D / 1000.0}
        else:
            mace = lookup.get(item["cas"], {"kind": "unknown"})
        thresholds = {}
        for level, factor in DIN_MACE_FACTORS.items():
            if mace["kind"] == "numeric":
                threshold = mace["mace_mg"] * factor
                passed = item["concentration_mg_kg"] <= threshold
                thresholds[level] = {"kind": "numeric", "value": threshold, "passed": passed}
                counts[level]["confirm" if passed else "not_confirm"] += 1
            elif mace["kind"] == "no_hazard":
                thresholds[level] = {"kind": "no_hazard", "passed": True}
                counts[level]["confirm"] += 1
            else:
                thresholds[level] = {"kind": "unknown", "passed": None}
                counts[level]["unknown"] += 1
        evaluated.append({**item, "mace": mace, "thresholds": thresholds})

    def shade(cell, color):
        properties = cell._tc.get_or_add_tcPr()
        element = properties.find(qn("w:shd"))
        if element is None:
            element = OxmlElement("w:shd"); properties.append(element)
        element.set(qn("w:fill"), color)

    def write_cell(cell, value, bold=False, color=None, size=6.2, align=WD_ALIGN_PARAGRAPH.CENTER):
        cell.text = ""
        paragraph = cell.paragraphs[0]
        paragraph.alignment = align
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        run = paragraph.add_run(text(value))
        run.bold = bold; run.font.name = "Arial"; run.font.size = Pt(size)
        if color:
            run.font.color.rgb = RGBColor.from_string(color)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

    def fmt(value):
        if value is None: return ""
        if not isinstance(value, (int, float)): return text(value)
        if abs(value) >= 1000: return f"{value:,.0f}".replace(",", " ")
        if abs(value) >= 10: return f"{value:.2f}"
        if abs(value) >= 1: return f"{value:.3f}"
        return f"{value:.3g}"

    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21.0); section.page_height = Cm(29.7)
    section.left_margin = Cm(0.7); section.right_margin = Cm(0.7)
    section.top_margin = Cm(1.0); section.bottom_margin = Cm(1.0)
    title = document.add_paragraph()
    run = title.add_run("GC-MS Screening - DIN SPEC 91521 MACE Evaluation")
    run.bold = True; run.font.name = "Arial"; run.font.size = Pt(14)
    run.font.color.rgb = RGBColor.from_string(DIN_REPORT_BLUE)
    subtitle = document.add_paragraph(f"Source: {source_workbook.name} | Sheet: {source_sheet}")
    subtitle.paragraph_format.space_after = Pt(5)

    summary_table = document.add_table(rows=6, cols=2)
    summary_table.alignment = WD_TABLE_ALIGNMENT.LEFT
    write_cell(summary_table.cell(0, 0), "Sum of identified, unidentified, Hydrocarbons", True, "FFFFFF", 7, WD_ALIGN_PARAGRAPH.LEFT)
    shade(summary_table.cell(0, 0), "C00000")
    merged = summary_table.cell(0, 0).merge(summary_table.cell(0, 1)); shade(merged, "C00000")
    write_cell(summary_table.cell(1, 0), "Sample:", True, size=7, align=WD_ALIGN_PARAGRAPH.LEFT)
    sample_match = re.search(r"\d{6,}", source_workbook.stem)
    write_cell(summary_table.cell(1, 1), sample_match.group(0) if sample_match else source_workbook.stem, size=7)
    write_cell(summary_table.cell(2, 0), "Category", True, size=7, align=WD_ALIGN_PARAGRAPH.LEFT)
    write_cell(summary_table.cell(2, 1), "µg/kg Granulate", True, size=7)
    for row_index, label in enumerate(("Sum of Hydrocarbons", "Identified substances without hydrocarbons", "Unidentified"), 3):
        write_cell(summary_table.cell(row_index, 0), label, size=7, align=WD_ALIGN_PARAGRAPH.LEFT)
        write_cell(summary_table.cell(row_index, 1), fmt(summary.get(label, 0.0)), size=7)

    document.add_paragraph().paragraph_format.space_after = Pt(1)

    headers = ["RT", "RI", "Name", "CAS No.", "Match %", "Detected\nmg/kg", "MACE\nmg/kg bw/d",
               "Threshold A\nmg/kg", "Threshold B\nmg/kg", "Threshold C\nmg/kg"]
    table = document.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER; table.autofit = False
    widths = [0.8, 0.8, 4.0, 1.45, 0.75, 1.1, 1.45, 2.1, 2.1, 2.1]
    for index, heading in enumerate(headers):
        write_cell(table.cell(0, index), heading, True, "FFFFFF", 5.2); shade(table.cell(0, index), DIN_REPORT_BLUE)
        table.cell(0, index).width = Cm(widths[index])
    header_properties = table.rows[0]._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader"); repeat.set(qn("w:val"), "true"); header_properties.append(repeat)
    for item in evaluated:
        cells = table.add_row().cells
        match_display = "" if item["match"] is None else f"{item['match']:.0f}"
        base = [fmt(item["rt"]), fmt(item["ri"]), item["name"], item["cas"] if item["cas"] not in {"", "0"} else "",
                match_display, fmt(item["concentration_mg_kg"])]
        for index, value in enumerate(base):
            write_cell(cells[index], value, size=4.9,
                       align=WD_ALIGN_PARAGRAPH.LEFT if index == 2 else WD_ALIGN_PARAGRAPH.CENTER)
        mace = item["mace"]
        if mace["kind"] == "numeric":
            write_cell(cells[6], f"{mace['mace_mg']:.3g}\nDIN p. {mace.get('page', '')}", size=4.8)
        else:
            label = "Unknown Value\n(no hazard identified)" if mace["kind"] == "no_hazard" else "Unknown Value"
            write_cell(cells[6], label, True, size=4.8); shade(cells[6], DIN_REPORT_YELLOW)
        for column, level in enumerate("ABC", 7):
            threshold_result = item["thresholds"][level]
            if threshold_result["kind"] == "no_hazard":
                write_cell(cells[column], "CONFIRM", True, size=4.8)
                shade(cells[column], DIN_REPORT_GREEN)
            elif threshold_result["kind"] == "unknown":
                write_cell(cells[column], "EVALUATION NEEDED", True, size=4.6)
                shade(cells[column], DIN_REPORT_YELLOW)
            else:
                threshold = threshold_result["value"]
                passed = threshold_result["passed"]
                write_cell(cells[column], f"{fmt(threshold)}\n{'CONFIRM' if passed else 'NOT CONFIRM'}", True, size=4.7)
                shade(cells[column], DIN_REPORT_GREEN if passed else DIN_REPORT_RED)
        for index, width in enumerate(widths): cells[index].width = Cm(width)

    if qc_rows:
        qc_heading = document.add_paragraph()
        qc_heading.paragraph_format.space_before = Pt(6)
        qc_heading.paragraph_format.space_after = Pt(3)
        qc_run = qc_heading.add_run("Replicate quality control")
        qc_run.bold = True; qc_run.font.name = "Arial"; qc_run.font.size = Pt(9)
        qc_run.font.color.rgb = RGBColor.from_string(DIN_REPORT_BLUE)
        qc_table = document.add_table(rows=len(qc_rows), cols=6)
        qc_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        qc_widths = [1.6, 3.4, 3.2, 2.1, 1.8, 2.8]
        for row_index, source_row in enumerate(qc_rows):
            for column_index, raw_value in enumerate(source_row):
                if row_index == 0:
                    display_value = text(raw_value)
                elif column_index in {1, 2}:
                    display_value = fmt(numeric_value(raw_value))
                elif column_index in {3, 4} and numeric_value(raw_value) is not None:
                    display_value = f"{numeric_value(raw_value):.2%}" if column_index == 3 else f"{numeric_value(raw_value):.0%}"
                else:
                    display_value = text(raw_value)
                write_cell(qc_table.cell(row_index, column_index), display_value,
                           bold=(row_index == 0 or column_index == 5),
                           color="FFFFFF" if row_index == 0 else None, size=6.2)
                qc_table.cell(row_index, column_index).width = Cm(qc_widths[column_index])
                if row_index == 0:
                    shade(qc_table.cell(row_index, column_index), DIN_REPORT_BLUE)
                elif column_index == 5:
                    shade(qc_table.cell(row_index, column_index),
                          DIN_REPORT_GREEN if text(raw_value).upper() == "OK" else DIN_REPORT_RED)

    note = document.add_paragraph()
    note.paragraph_format.space_before = Pt(5)
    note.add_run(
        "Calculation: A = MACE x 148,000; B = MACE x 6,200,000; C = MACE x 769,200,000. "
        "Unidentified substances: 0.0025 µg/kg bw/day. Hidden Excel rows and internal standards are excluded. "
        "Green = CONFIRM, including 'no hazard identified'; red = NOT CONFIRM; yellow = EVALUATION NEEDED when no MACE assessment is available."
    ).font.size = Pt(7)
    document.save(word_path)
    return {"output": str(word_path), "rows": len(evaluated), "counts": counts, "source_sheet": source_sheet, "summary": summary, "qc_rows": len(qc_rows)}


# Design tokens. Every color the application draws comes from here, so a theme
# is one dictionary and nothing else in the file carries a literal color. The
# two themes are deliberate about meaning: blue carries the action, and
# green/yellow/red are reserved for the assessment, never for a button.
#
# Every foreground was checked against the surface it sits on; the ratio is
# noted where it is close to the 4.5:1 floor.
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "label": "Analytic Blue",
        "ttk_theme": "vista",
        # Action
        "brand": "#1668B3",          # 5.7:1 on white
        "brand_dark": "#0F5292",     # hover, 8.0:1 on white
        "brand_fg": "#FFFFFF",       # 5.7:1 on brand
        "disabled_bg": "#C3D4E3",
        "disabled_fg": "#405B72",    # 4.7:1 on the muted face
        # Surfaces
        "header": "#0E3A5C",
        "header_text": "#FFFFFF",
        "header_muted": "#BBD4E6",   # 7.7:1 on header
        "nav_label": "#7FA5C0",
        "background": "#EEF3F8",
        "surface": "#FFFFFF",
        "border": "#CBD8E4",
        "console": "#F4F8FB",
        # Text
        "text": "#1B2A38",           # 14.6:1 on white
        "muted": "#5A6B7A",          # 5.5:1 on white
        # Assessment
        "success": "#12715A",        # 5.9:1 on white
        "warning": "#9A6400",        # 5.0:1 on white
        "error": "#B3261E",          # 6.5:1 on white
        "info": "#2A6FA8",           # 5.3:1 on white
        "accent2": "#0E7C86",        # measured values, plots
        # Tables
        "table_header": "#E2EBF4",
        "selection": "#D9E8F6",
        "selection_text": "#0E3A5C",  # 9.5:1 on the selected row
        "trough": "#DCE6F0",
        "row_ok": "#E4F1E8",
        "row_edit": "#FBF0D2",
        "row_error": "#F8DEDC",
        "row_error_text": "#8C1D18",
    },
    "dark": {
        "label": "Cool Slate",
        # vista draws entries, notebook tabs and scrollbars with native light
        # elements that ignore every color option, so the dark theme has to be
        # clam. This is the whole reason the ttk theme is a token.
        "ttk_theme": "clam",
        # Action. Dark text on light blue: white on the same blue reaches only
        # 2.7:1, which is why the primary button inverts in this theme.
        "brand": "#4DA3E8",          # 6.0:1 on the card surface
        "brand_dark": "#6FB6EE",     # hover, 7.5:1
        "brand_fg": "#08131D",       # 6.9:1 on brand
        "disabled_bg": "#26384A",
        "disabled_fg": "#93A8BB",    # 4.9:1 on the muted face
        # Surfaces. Neither pure black nor pure white; both flicker over a long
        # evening of evaluations.
        "header": "#0B131C",
        "header_text": "#F2F7FB",
        "header_muted": "#93A8BB",
        "nav_label": "#6E8397",
        "background": "#0F1720",
        "surface": "#16212C",
        "border": "#2A3A4A",
        "console": "#101A24",
        # Text
        "text": "#E4EDF5",           # 13.8:1 on the card surface
        "muted": "#93A8BB",          # 6.7:1 on the card surface
        # Assessment, lightened: the light theme's tones fall below 3:1 here.
        "success": "#4CC79C",        # 7.7:1
        "warning": "#E3A93F",        # 7.8:1
        "error": "#F07178",          # 5.7:1
        "info": "#7FB6E8",           # 7.6:1
        "accent2": "#45C2C7",        # 7.6:1
        # Tables
        "table_header": "#1D2A37",
        "selection": "#1B3348",
        "selection_text": "#EAF4FC",  # 11.7:1 on the selected row
        "trough": "#22303E",
        "row_ok": "#16352B",
        "row_edit": "#3A3016",
        "row_error": "#3E1F20",
        "row_error_text": "#F2B8B5",
    },
}

DEFAULT_THEME = "light"
THEME_SETTINGS_KEY = "theme"

# The theme a window built outside the application window (the progress window,
# the result dashboard) should use. Kept in sync by ThemeManager.apply(); until
# then it is unresolved, so that a batch run without the application window
# still opens its progress window in the theme the user chose.
_active_theme: Optional[str] = None


def load_saved_theme() -> str:
    """Return the stored theme, falling back to the light one."""
    name = load_user_settings().get(THEME_SETTINGS_KEY)
    return name if name in THEMES else DEFAULT_THEME


def save_theme_choice(name: str) -> None:
    """Remember the theme for the next start; never fatal if it cannot."""
    try:
        save_user_settings(**{THEME_SETTINGS_KEY: name})
    except OSError:
        pass


#: Two of the three GC-Daten tabs are for one-off investigations, not for the
#: routine batch run. They are hidden unless the analyst asks for them in the
#: options, so the page that is used every day shows exactly one way in.
EXPERT_MODE_SETTINGS_KEY = "expert_mode"


def load_expert_mode() -> bool:
    """Return whether the expert tabs are switched on; off by default."""
    return bool(load_user_settings().get(EXPERT_MODE_SETTINGS_KEY, False))


def save_expert_mode(enabled: bool) -> None:
    """Remember the mode for the next start; never fatal if it cannot."""
    try:
        save_user_settings(**{EXPERT_MODE_SETTINGS_KEY: bool(enabled)})
    except OSError:
        pass


def active_theme_name() -> str:
    global _active_theme
    if _active_theme not in THEMES:
        _active_theme = load_saved_theme()
    return _active_theme


def active_theme_colors() -> dict[str, str]:
    """Palette for windows that are built fresh instead of being repainted."""
    return THEMES[active_theme_name()]


def configure_ttk_styles(style: "ttk.Style", colors: dict[str, str], root=None) -> None:
    """Paint every ttk style this application uses from one palette.

    Called again on every theme change: ``theme_use`` resets the style
    database, so this has to be the single place that fills it.
    """
    try:
        style.theme_use(colors["ttk_theme"])
    except tk.TclError:
        style.theme_use("clam")

    style.configure("App.TFrame", background=colors["background"])
    style.configure("Surface.TFrame", background=colors["surface"])
    style.configure("Header.TFrame", background=colors["header"])
    style.configure("HeaderTitle.TLabel", background=colors["header"],
                    foreground=colors["header_text"], font=("Segoe UI", 19, "bold"))
    style.configure("HeaderSub.TLabel", background=colors["header"],
                    foreground=colors["header_muted"], font=("Segoe UI", 9))
    style.configure("Card.TLabelframe", background=colors["surface"], padding=12,
                    bordercolor=colors["border"], darkcolor=colors["surface"],
                    lightcolor=colors["surface"], relief="solid")
    style.configure("Card.TLabelframe.Label", background=colors["surface"],
                    foreground=colors["brand"], font=("Segoe UI", 10, "bold"))
    style.configure("Body.TLabel", background=colors["surface"],
                    foreground=colors["text"], font=("Segoe UI", 9))
    style.configure("Muted.TLabel", background=colors["surface"],
                    foreground=colors["muted"], font=("Segoe UI", 8))
    style.configure("Ready.TLabel", background=colors["background"],
                    foreground=colors["success"], font=("Segoe UI", 9, "bold"))
    # Page level: these sit directly on the window background, not on a card.
    style.configure("PageTitle.TLabel", background=colors["background"],
                    foreground=colors["text"], font=("Segoe UI", 18, "bold"))
    style.configure("PageText.TLabel", background=colors["background"],
                    foreground=colors["muted"], font=("Segoe UI", 9))
    style.configure("PageNote.TLabel", background=colors["background"],
                    foreground=colors["warning"], font=("Segoe UI", 9))
    style.configure("Secondary.TButton", font=("Segoe UI", 9), padding=(10, 6))
    style.configure("Brand.Horizontal.TProgressbar", troughcolor=colors["trough"],
                    background=colors["brand"], lightcolor=colors["brand"],
                    darkcolor=colors["brand_dark"], bordercolor=colors["border"])

    style.configure("Treeview", font=("Segoe UI", 9), rowheight=26,
                    background=colors["surface"], fieldbackground=colors["surface"],
                    foreground=colors["text"], bordercolor=colors["border"])
    style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"),
                    background=colors["table_header"], foreground=colors["text"],
                    relief="flat")
    style.map("Treeview",
              background=[("selected", colors["selection"])],
              foreground=[("selected", colors["selection_text"])])
    style.map("Treeview.Heading", background=[("active", colors["table_header"])])
    # The GC-Daten analysis list draws its own indentation and tree lines into
    # the row images (see ``analysis_tree_images``), so ttk must not indent.
    style.configure("Analysis.Treeview", indent=0, rowheight=ANALYSIS_ROW_HEIGHT)

    # Under clam every one of these is drawn by ttk itself and would otherwise
    # stay light grey in the dark theme. Under vista most are ignored, which is
    # harmless because the native look already matches the light theme.
    style.configure("TFrame", background=colors["background"])
    style.configure("TLabel", background=colors["background"], foreground=colors["text"])
    style.configure("TSeparator", background=colors["border"])
    style.configure("TCheckbutton", background=colors["surface"], foreground=colors["text"])
    style.configure("TRadiobutton", background=colors["surface"], foreground=colors["text"])
    # clam draws the tick box itself; without these it stays light grey on the
    # dark card. The names are clam's own element options.
    for control in ("TCheckbutton", "TRadiobutton"):
        style.configure(control, indicatorbackground=colors["surface"],
                        indicatorforeground=colors["brand"],
                        upperbordercolor=colors["border"],
                        lowerbordercolor=colors["border"],
                        focuscolor=colors["brand"])
        style.map(control,
                  background=[("active", colors["surface"])],
                  indicatorbackground=[("disabled", colors["background"]),
                                       ("selected", colors["surface"])],
                  indicatorforeground=[("selected", colors["brand"])],
                  foreground=[("disabled", colors["muted"])])
    style.configure("TButton", background=colors["surface"], foreground=colors["text"],
                    bordercolor=colors["border"], lightcolor=colors["surface"],
                    darkcolor=colors["surface"], focuscolor=colors["brand"])
    style.map("TButton", background=[("active", colors["table_header"])],
              foreground=[("disabled", colors["muted"])])
    style.configure("TEntry", fieldbackground=colors["surface"], foreground=colors["text"],
                    bordercolor=colors["border"], lightcolor=colors["border"],
                    darkcolor=colors["border"], insertcolor=colors["text"])
    style.configure("TCombobox", fieldbackground=colors["surface"],
                    background=colors["surface"], foreground=colors["text"],
                    bordercolor=colors["border"], lightcolor=colors["border"],
                    darkcolor=colors["border"], arrowcolor=colors["text"])
    style.map("TCombobox", fieldbackground=[("readonly", colors["surface"])],
              foreground=[("readonly", colors["text"])])
    style.configure("TNotebook", background=colors["background"],
                    bordercolor=colors["border"])
    style.configure("TNotebook.Tab", background=colors["table_header"],
                    foreground=colors["muted"], padding=(12, 6),
                    bordercolor=colors["border"], font=("Segoe UI", 9))
    style.map("TNotebook.Tab",
              background=[("selected", colors["surface"])],
              foreground=[("selected", colors["text"])])
    # Both orientations by name: a bare "TScrollbar" leaves the vertical one
    # in the clam default, which is a white bar down the dark window.
    for bar in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(bar, background=colors["table_header"],
                        troughcolor=colors["background"],
                        bordercolor=colors["border"],
                        lightcolor=colors["table_header"],
                        darkcolor=colors["table_header"],
                        arrowcolor=colors["muted"])
        style.map(bar, background=[("active", colors["border"])])
    style.configure("TPanedwindow", background=colors["background"])
    style.configure("Sash", sashthickness=6, gripcount=0,
                    background=colors["border"])

    if root is not None:
        # The combobox drop-down is a plain Tk listbox owned by the toplevel, so
        # it is reachable only through the option database.
        for option, value in (
            ("*TCombobox*Listbox.background", colors["surface"]),
            ("*TCombobox*Listbox.foreground", colors["text"]),
            ("*TCombobox*Listbox.selectBackground", colors["selection"]),
            ("*TCombobox*Listbox.selectForeground", colors["selection_text"]),
        ):
            try:
                root.option_add(option, value)
            except tk.TclError:
                pass


class ThemeManager:
    """Single owner of the application window's colors.

    ttk widgets follow their style, so a theme change reaches them through
    ``configure_ttk_styles``. Plain Tk widgets carry their colors themselves:
    those register the palette keys they draw from and get repainted here.
    Nothing is rebuilt, so switching the theme keeps chosen files, entries and
    the open page exactly as they are.
    """

    def __init__(self, root, name: Optional[str] = None):
        self.root = root
        self.style = ttk.Style(root)
        self.name = name if name in THEMES else load_saved_theme()
        self._widgets: list[tuple[Any, dict[str, str]]] = []
        self._callbacks: list[Any] = []

    @property
    def colors(self) -> dict[str, str]:
        return THEMES[self.name]

    def color(self, token: str) -> str:
        """Resolve a palette key; a literal color is passed through."""
        return self.colors.get(token, token)

    def register(self, widget, **roles: str):
        """Bind widget options to palette keys, e.g. ``bg="header"``."""
        self._widgets.append((widget, roles))
        self._paint(widget, roles)
        return widget

    def on_change(self, callback):
        """Run ``callback(colors)`` after every theme change, and now."""
        self._callbacks.append(callback)
        callback(self.colors)
        return callback

    def _paint(self, widget, roles: dict[str, str]) -> None:
        for option, token in roles.items():
            try:
                widget.configure(**{option: self.color(token)})
            except tk.TclError:
                # Widget gone, or this widget does not know the option.
                pass

    def apply(self) -> None:
        global _active_theme
        _active_theme = self.name
        colors = self.colors
        configure_ttk_styles(self.style, colors, self.root)
        try:
            self.root.configure(bg=colors["background"])
        except tk.TclError:
            pass
        alive: list[tuple[Any, dict[str, str]]] = []
        for widget, roles in self._widgets:
            try:
                if not widget.winfo_exists():
                    continue
            except tk.TclError:
                continue
            alive.append((widget, roles))
            self._paint(widget, roles)
        self._widgets = alive
        surviving = []
        for callback in self._callbacks:
            try:
                callback(colors)
            except tk.TclError:
                continue
            surviving.append(callback)
        self._callbacks = surviving

    def switch(self, name: str) -> None:
        if name not in THEMES or name == self.name:
            return
        self.name = name
        save_theme_choice(name)
        self.apply()

    def toggle(self) -> None:
        self.switch("dark" if self.name == "light" else "light")

    @property
    def palette(self) -> "LivePalette":
        """A ``C["token"]`` view that always reads the *current* theme.

        Handing out the plain dictionary would freeze the colors of every
        closure that captured it, and those closures - ``show_page``, the
        status setters - run long after the window was built.
        """
        return LivePalette(self)


class LivePalette:
    """Read-only mapping onto whichever theme the manager holds right now."""

    def __init__(self, manager: ThemeManager):
        self._manager = manager

    def __getitem__(self, key: str) -> str:
        return self._manager.colors[key]

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._manager.colors.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._manager.colors

    def __iter__(self):
        return iter(self._manager.colors)


def brand_button(parent, text: str, command, theme: Optional["ThemeManager"] = None,
                 **kw) -> tk.Button:
    """Branded primary action, drawn as a native Tk button.

    A ttk style cannot carry this look: on Windows the ``vista`` and
    ``xpnative`` themes draw ``TButton`` with a native element that ignores
    ``background`` but honours ``foreground``, so the former ``Brand.TButton``
    painted white text onto the light native button face - white on white.
    ``tk.Button`` honours ``bg``/``fg`` on every platform and theme, which is
    why the other primary actions in this file are already native Tk buttons.
    This helper is the single place that defines their appearance.

    Hover is handled explicitly through ``<Enter>``/``<Leave>`` and is skipped
    while the button is disabled. ``configure(state=...)`` and
    ``button["state"] = ...`` keep working and repaint the button, so it
    answers to the same calls a ``ttk.Button`` would.

    Pass ``theme`` for a button that lives in the application window: it then
    follows a theme change. Without it the button takes the colors that are
    active at the moment it is built, which is what the short-lived windows
    (progress, results) need.
    """
    colors = theme.colors if theme is not None else active_theme_colors()
    # One mutable record, so hover and the disabled state keep working after a
    # theme change without rebinding anything.
    look = {
        "bg": colors["brand"],
        "hover": colors["brand_dark"],
        "fg": colors["brand_fg"],
        "disabled_bg": colors["disabled_bg"],
        "disabled_fg": colors["disabled_fg"],
    }
    options: dict[str, Any] = {
        "text": text,
        "command": command,
        "bg": look["bg"],
        "fg": look["fg"],
        "activebackground": look["hover"],
        "activeforeground": look["fg"],
        "disabledforeground": look["disabled_fg"],
        "font": ("Segoe UI", 10, "bold"),
        "relief": tk.FLAT,
        "borderwidth": 0,
        "padx": 20,
        "pady": 9,
        "cursor": "hand2",
    }
    options.update(kw)
    button = tk.Button(parent, **options)
    # A caller that overrode the colors keeps them, theme change included.
    look.update(bg=options["bg"], hover=options["activebackground"],
                fg=options["fg"], disabled_fg=options["disabledforeground"])
    overridden = {key for key in ("bg", "fg", "activebackground",
                                  "activeforeground", "disabledforeground")
                  if key in kw}
    # Bound method captured before the instance attribute below shadows it, so
    # repainting cannot recurse through the wrapper.
    set_options = button.configure

    def repaint(hover: bool = False) -> None:
        if str(button["state"]) == tk.DISABLED:
            set_options(bg=look["disabled_bg"], fg=look["disabled_fg"])
        else:
            set_options(bg=look["hover"] if hover else look["bg"], fg=look["fg"])

    def configure_and_repaint(cnf=None, **changes):
        result = set_options(cnf, **changes)
        keys = set(changes)
        if isinstance(cnf, dict):
            keys |= set(cnf)
        if "state" in keys:
            repaint()
        return result

    def adopt(new_colors: dict[str, str]) -> None:
        if not button.winfo_exists():
            raise tk.TclError("button gone")
        if not overridden:
            look.update(bg=new_colors["brand"], hover=new_colors["brand_dark"],
                        fg=new_colors["brand_fg"])
        look.update(disabled_bg=new_colors["disabled_bg"],
                    disabled_fg=new_colors["disabled_fg"])
        set_options(activebackground=look["hover"], activeforeground=look["fg"],
                    disabledforeground=look["disabled_fg"])
        repaint()

    button.configure = configure_and_repaint
    button.config = configure_and_repaint
    button.bind("<Enter>", lambda _event: repaint(hover=True))
    button.bind("<Leave>", lambda _event: repaint())
    if theme is not None:
        theme.on_change(adopt)
    return button


def run_nias_gui() -> None:
    """Branded application window for the Constantia Flexibles NIAS workflow.

    The window stays open for the whole session. Processing runs from the start
    button, shows its progress and results in child windows, and returns to this
    window afterwards, so a second batch can follow without restarting. It used
    to destroy itself before processing, which is why the program closed once the
    Word report had been written.
    """
    if tk is None:
        raise RuntimeError(
            "Die grafische Oberfläche benötigt Tkinter. Installiere Tkinter oder "
            "starte das Skript mit NIAS- und CAS-Datei als Argumente."
        )

    default_cas_path = load_saved_cas_path()
    full_auto_root_var: Optional[Path] = None

    root = tk.Tk()
    root.title(f"NIAS Report 4.0 | Constantia Flexibles | {SCRIPT_VERSION}")
    # Colors come from the theme the user chose last (see THEMES). ``C`` reads
    # the active palette live, so every closure below stays correct across a
    # theme change instead of holding the colors it was built with.
    theme = ThemeManager(root)
    C = theme.palette
    # Ab hier ist dieses Fenster der Bezugspunkt: jedes weitere Fenster – Dialoge
    # dieser Datei ebenso wie GC-Workspace und DIN-SPEC-Workspace – öffnet auf
    # dem Monitor, auf dem die Anwendung gerade steht, nicht auf dem Hauptmonitor.
    set_main_window(root)
    bind_new_windows_to_main_screen()
    fit_to_screen(root, 1260, 760, 1080, 680)

    # Every ttk style is filled from the palette in configure_ttk_styles, which
    # runs again on each theme change. The branded button is a native Tk button
    # (see brand_button): the vista theme ignores a ttk background, which made
    # white on brand color unreadable.
    style = theme.style
    theme.apply()

    # Scrollable application surface: when the window is reduced, all controls
    # remain reachable through the vertical scrollbar on the right-hand side.
    viewport = ttk.Frame(root, style="App.TFrame")
    viewport.pack(fill=tk.BOTH, expand=True)

    canvas = theme.register(tk.Canvas(
        viewport,
        borderwidth=0,
        highlightthickness=0,
    ), background="background")
    vertical_scrollbar = ttk.Scrollbar(
        viewport,
        orient="vertical",
        command=canvas.yview,
    )
    canvas.configure(yscrollcommand=vertical_scrollbar.set)
    vertical_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    shell = ttk.Frame(canvas, style="App.TFrame")
    shell_window = canvas.create_window((0, 0), window=shell, anchor="nw")

    def update_scroll_region(_event=None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def fit_shell_to_canvas(event) -> None:
        canvas.itemconfigure(shell_window, width=event.width)

    def scroll_with_mousewheel(event) -> None:
        if event.delta:
            canvas.yview_scroll(int(-event.delta / 120), "units")

    def bind_mousewheel(widget) -> None:
        """Bind the wheel to the scrollable surface only.

        ``bind_all`` also captured the wheel above the progress and result
        windows, which then scrolled this canvas invisibly in the background.
        """
        widget.bind("<MouseWheel>", scroll_with_mousewheel)
        for child in widget.winfo_children():
            bind_mousewheel(child)

    shell.bind("<Configure>", update_scroll_region)
    canvas.bind("<Configure>", fit_shell_to_canvas)
    canvas.bind("<MouseWheel>", scroll_with_mousewheel)

    header = ttk.Frame(shell, style="Header.TFrame", padding=(20, 14))
    header.pack(fill=tk.X)
    logo_path = Path(__file__).resolve().with_name("CFLEX_logo_app.png")
    logo_image = None
    if logo_path.is_file():
        try:
            logo_image = tk.PhotoImage(file=str(logo_path))
            # The window now lives for the whole session, so the image must be
            # anchored somewhere that outlives this function.
            root.nias_logo_image = logo_image
            theme.register(ttk.Label(header, image=logo_image),
                           background="header").pack(side=tk.LEFT, padx=(0, 24))
        except tk.TclError:
            logo_image = None
    title_box = ttk.Frame(header, style="Header.TFrame")
    title_box.pack(side=tk.LEFT, fill=tk.Y, expand=True)
    ttk.Label(title_box, text="NIAS Report 4.0", style="HeaderTitle.TLabel").pack(anchor="w", pady=(8, 0))
    ttk.Label(title_box, text="Migration Screening & Compliance Reporting", style="HeaderSub.TLabel").pack(anchor="w", pady=(2, 0))

    # Right-hand corner of the header: the version, and nothing else. The
    # Hell/Dunkel switch moved to the options page, where every other setting
    # that applies to the whole program now lives.
    header_side = ttk.Frame(header, style="Header.TFrame")
    header_side.pack(side=tk.RIGHT, anchor="ne", pady=(8, 0))
    ttk.Label(header_side, text=f"R&D Analytics  ·  {SCRIPT_VERSION}",
              style="HeaderSub.TLabel").pack(anchor="e")

    # The switch is one page away now, so the keyboard stays the short way.
    root.bind("<Control-Shift-D>", lambda _event: theme.toggle())

    # Main workspace with persistent navigation on the LEFT.
    workspace = ttk.Frame(shell, style="App.TFrame")
    workspace.pack(fill=tk.BOTH, expand=True)

    navigation = theme.register(tk.Frame(workspace, width=195), bg="header")
    navigation.pack(side=tk.LEFT, fill=tk.Y)
    navigation.pack_propagate(False)

    page_host = ttk.Frame(workspace, style="App.TFrame")
    page_host.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    gc_content = ttk.Frame(page_host, style="App.TFrame", padding=(18, 14, 18, 8))
    content = ttk.Frame(page_host, style="App.TFrame", padding=(18, 14, 18, 8))
    din_content = ttk.Frame(page_host, style="App.TFrame", padding=(18, 14, 18, 8))
    unknown_content = ttk.Frame(page_host, style="App.TFrame", padding=(18, 14, 18, 8))
    options_content = ttk.Frame(page_host, style="App.TFrame", padding=(18, 14, 18, 8))
    atlas_content = ttk.Frame(page_host, style="App.TFrame", padding=(18, 14, 18, 8))

    theme.register(tk.Label(navigation, text="WORKFLOWS", font=("Segoe UI", 9, "bold")),
                   bg="header", fg="nav_label").pack(anchor="w", padx=18, pady=(22, 12))

    # One entry per workflow page. Each page used to hide the others by hand,
    # which meant every new page had to be added to every existing page.
    workflow_pages = (
        ("1.  GC-Daten", gc_content, f"GC-Daten | Constantia Flexibles | {SCRIPT_VERSION}"),
        ("2.  NIAS Report", content, f"NIAS Report 4.0 | Constantia Flexibles | {SCRIPT_VERSION}"),
        ("3.  DIN_SPEC_91521", din_content, f"DIN SPEC 91521 Auswerter | Constantia Flexibles | {SCRIPT_VERSION}"),
        ("4.  Unknown Register", unknown_content, f"Unknown Register | Constantia Flexibles | {SCRIPT_VERSION}"),
        ("5.  Options", options_content, f"Optionen | Constantia Flexibles | {SCRIPT_VERSION}"),
        ("6.  EI Atlas", atlas_content, f"EI Atlas | Constantia Flexibles | {SCRIPT_VERSION}"),
    )
    page_buttons: dict = {}
    page_shown_once: set = set()

    def show_page(label: str) -> None:
        if label.endswith("EI Atlas"):
            import gc_atlas
            gc_atlas.open_library(root)
            return
        for name, frame, title in workflow_pages:
            active = name == label
            frame.pack(fill=tk.BOTH, expand=True) if active else frame.pack_forget()
            page_buttons[name].configure(
                bg=C["brand"] if active else C["header"],
                fg=C["brand_fg"] if active else C["header_text"])
            if active:
                root.title(title)
                if name not in page_shown_once:
                    page_shown_once.add(name)
                    on_page_first_shown(name)
        update_scroll_region()

    def on_page_first_shown(label: str) -> None:
        """Hook for pages that load their data lazily."""
        if label.endswith("Unknown Register"):
            refresh_unknown_register(select_first=True)
        elif label.endswith("Options"):
            refresh_options()

    def show_gc_page() -> None:
        show_page("1.  GC-Daten")

    def nav_button(label: str, command) -> tk.Button:
        button = tk.Button(
            navigation, text=label, command=command, anchor="w", relief="flat",
            bd=0, padx=18, pady=12,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        # Background and label follow show_page (active entry vs. the rest);
        # only the hover color is fixed here.
        theme.register(button, activebackground="brand_dark",
                       activeforeground="brand_fg")
        button.pack(fill=tk.X, padx=8, pady=3)
        return button

    for _label, _frame, _title in workflow_pages:
        page_buttons[_label] = nav_button(
            _label, lambda name=_label: show_page(name))

    # A theme change has to repaint the active entry, which only show_page knows.
    def repaint_navigation(_colors: dict[str, str]) -> None:
        if not navigation.winfo_exists():
            raise tk.TclError("navigation gone")
        for name, _frame, _title in workflow_pages:
            active = _frame.winfo_ismapped()
            page_buttons[name].configure(
                bg=C["brand"] if active else C["header"],
                fg=C["brand_fg"] if active else C["header_text"])

    theme.on_change(repaint_navigation)
    ttk.Label(gc_content, text="GC-Daten",
              style="PageTitle.TLabel").pack(anchor="w", pady=(0, 3))
    ttk.Label(
        gc_content,
        text=("Schritt 1: GC-Rohdaten in eine Excel-Auswertung überführen. Danach die erzeugte "
              "Excel-Datei manuell prüfen und erst anschließend auf der Seite NIAS Report verarbeiten."),
        style="PageText.TLabel",
        wraplength=900, justify="left").pack(anchor="w", pady=(0, 14))

    # Interactive workspace: reads the .D folders directly instead of going via
    # a ChemStation Excel export, and replaces the "check the sheet in Excel"
    # step with an editable grid that shows each peak, its spectrum and its
    # m/z list. Lives in gc_workspace.py; imported lazily so the main window
    # still opens if matplotlib or tksheet are missing.
    workspace_card = ttk.Frame(gc_content, style="App.TFrame")
    workspace_card.pack(fill=tk.X, pady=(0, 12))

    def open_nias_workspace(analysis: Optional[dict[str, Any]] = None) -> None:
        """Open the NIAS workspace, on the chosen files or on one analysis.

        Spec v3.2 §VIII.4 §3: DIN SPEC 91521 is its own module now, so this
        function is the NIAS path and nothing else — ``open_dinspec_workspace``
        below is the other half of the former ``open_gc_workspace(mode)``.

        ``analysis`` is a row of the batch's analysis list (§V.6). It carries its
        own determination folders, so the workspace opens on exactly that
        analysis instead of on whatever the file pickers happen to hold — with
        two Bestimmung tabs for a Doppelbestimmung and one, without the merged
        view, for a single determination.
        """
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import gc_workspace
        except ImportError as exc:
            messagebox.showerror(
                "GC-Workspace nicht verfügbar",
                "Der interaktive GC-Workspace benötigt matplotlib und tksheet.\n\n"
                "Installation:\n"
                f"    {sys.executable} -m pip install matplotlib tksheet\n\n"
                f"Details: {exc}",
                parent=root)
            return

        # The analysis list belongs to the batch tab and carries that tab's
        # parameter grid; the file pickers belong to the single-sample tab.
        variables = gc_duplicate_batch_vars if analysis else gc_duplicate_vars
        analysis_key = text(analysis.get("syneris")) if analysis else ""
        determination_dirs = ([Path(item["folder"]) for item
                               in analysis["analysis"]["determinations"]]
                              if analysis else [])

        start_folder: Optional[Path] = None
        if analysis:
            # Loaded explicitly below, so the window must not start a discovery
            # of its own over the whole batch folder.
            start_folder = None
        elif gc_single_files:
            # The Einzelbestimmung tab's first CSV, so the workspace opens
            # where the analyst was just working.
            start_folder = gc_single_files[0].parent
        # Spec v2.1 §V.3: the metadata dialog runs *before* a batch analysis is
        # opened, so the workspace starts from real cell area, coverage and O/V
        # ratio instead of the Settings defaults. Unconditional since §VIII.4 §3:
        # only the NIAS path quantifies from migration geometry, and this
        # function is only ever the NIAS path.
        scope: Optional[MigrationMetadataScope] = gc_collect_migration_scope(
            variables,
            "Gelten als Vorgabe für alle Analysen dieses Batches; Abweichungen "
            "einzelner Analysen im Parameter-Panel des Workspace.")
        if scope is None:
            return
        # Persisted regardless of whether the workspace can take them as an
        # argument yet, so nothing entered here is lost.
        save_migration_metadata_defaults(scope.for_analysis())
        try:
            keywords: dict[str, Any] = {
                "folder": start_folder,
                "cas_path": Path(cas_var.get()) if cas_var.get() else None,
            }
            if scope is not None:
                # ``open_workspace`` takes both parameters as of §V.3; the
                # signature probe that stood here while it did not is gone.
                keywords["migration_metadata"] = scope.for_analysis(analysis_key)
                keywords["migration_scope"] = scope
            workspace = gc_workspace.open_workspace(root, **keywords)
            if analysis and determination_dirs:
                gc_open_analysis_in_workspace(workspace, analysis,
                                              determination_dirs)
        except Exception as exc:
            messagebox.showerror("GC-Workspace", str(exc), parent=root)

    def gc_open_analysis_in_workspace(workspace, analysis: dict[str, Any],
                                      determination_dirs: list[Path]) -> None:
        """Load one analysis into a freshly opened workspace (§V.6).

        The batch's Blank and Blank+ISTD folders apply to every analysis in the
        batch, so they are set before loading; the session file is put next to
        the analysis's own workbook, because ``GCWorkspace`` would otherwise
        derive one name from the batch folder for every analysis in it.
        """
        batch = Path(gc_duplicate_batch_vars["batch"].get().strip() or ".")
        for attribute, key in (("blank_path", "blank"),
                               ("blank_istd_path", "blank_istd")):
            entry = (gc_analysis_blanks or {}).get(key)
            if entry and hasattr(workspace, attribute):
                setattr(workspace, attribute, Path(entry["folder"]))
        # One determination opens on its own folder, a pair on the batch folder;
        # only the folders listed here are read either way.
        container = (determination_dirs[0] if len(determination_dirs) == 1
                     else batch)
        gc_load_analysis_into_workspace(workspace, container, determination_dirs,
                                        analysis.get("session"))
        if len(determination_dirs) < 2:
            disable_duplicate_view(workspace)

    def open_dinspec_workspace() -> None:
        """Open the DIN SPEC 91521 workspace (spec v3.2 §VIII.4 §3).

        Deliberately *not* a mode of the NIAS window: DIN SPEC 91521 lives in
        ``gc_dinspec.py``, which imports ``gc_workspace`` and specialises it.
        The import stays inside this function so that the launcher — which
        ``gc_export.load_main_script`` imports for ``process_workbook`` — never
        pulls the DIN SPEC module in at import time.

        No migration metadata is collected here: DIN SPEC quantifies over the
        MS area against a chosen internal standard, not from migration
        geometry, so the dialog would ask for numbers it never uses.
        """
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import gc_dinspec
        except ImportError as exc:
            messagebox.showerror(
                "DIN-SPEC-Workspace nicht verfügbar",
                "Der DIN-SPEC-Workspace benötigt die Datei gc_dinspec.py "
                "neben diesem Skript.\n\n"
                "Fehlt eine der Bibliotheken, die gc_dinspec.py lädt, steht "
                "sie in den Details.\n\n"
                f"Details: {exc}",
                parent=root)
            return

        start_folder: Optional[Path] = (gc_single_files[0].parent
                                        if gc_single_files else None)
        try:
            gc_dinspec.open_workspace(
                root,
                folder=start_folder,
                cas_path=Path(cas_var.get()) if cas_var.get() else None)
        except Exception as exc:
            messagebox.showerror("DIN-SPEC-Workspace", str(exc), parent=root)

    workspace_buttons = ttk.Frame(workspace_card, style="App.TFrame")
    workspace_buttons.pack(anchor="w")
    brand_button(workspace_buttons, "GC-Workspace: Doppelbestimmung  →",
                 open_nias_workspace, theme=theme).pack(side=tk.LEFT)
    ttk.Label(
        workspace_card,
        text=("Liest .D-Ordner direkt ein und zeigt jeden Peak mit Chromatogramm, "
              "Massenspektrum und m/z-Liste. Substanznamen, CAS-Nummern und Flächen "
              "können dort geprüft und korrigiert werden; die Auswertung entsteht "
              "aus der bearbeiteten Tabelle.\n"
              "Doppelbestimmung: zwei Bestimmungen, Quantifizierung über die "
              "blankkorrigierte FID-Fläche und den Mittelwert der drei automatisch "
              "erkannten internen Standards."),
        style="PageText.TLabel",
        wraplength=900, justify="left").pack(anchor="w", pady=(4, 0))
    ttk.Separator(gc_content, orient="horizontal").pack(fill=tk.X, pady=(0, 12))

    gc_tabs = ttk.Notebook(gc_content)
    gc_tabs.pack(fill=tk.BOTH, expand=True)
    gc_duplicate_tab = ttk.Frame(gc_tabs, style="App.TFrame", padding=12)
    gc_duplicate_batch_tab = ttk.Frame(gc_tabs, style="App.TFrame", padding=12)
    gc_fingerprint_tab = ttk.Frame(gc_tabs, style="App.TFrame", padding=12)

    # The single-sample Doppelbestimmung and the Fingerprint screening are
    # investigation tools, not the routine path: the batch tab is what runs
    # every day. All three frames are always built - the expert mode only
    # decides which of them the notebook manages, so switching it costs nothing
    # and loses nothing that was typed into a tab.
    expert_mode_var = tk.BooleanVar(value=load_expert_mode())
    gc_tab_order = (
        (gc_duplicate_tab, "Einzelbestimmung", True),
        (gc_duplicate_batch_tab, "Doppelbestimmung (Batch)", False),
        (gc_fingerprint_tab, "Fingerprint Screening", True),
    )

    def apply_expert_mode() -> None:
        """Show exactly the tabs the current mode allows, in a fixed order."""
        expert = bool(expert_mode_var.get())
        for tab in gc_tabs.tabs():
            gc_tabs.forget(tab)
        # ``forget`` and ``add`` rather than ``hide`` and ``insert``: re-adding
        # in this loop is what keeps the order the same in both modes.
        for frame, label, expert_only in gc_tab_order:
            if expert or not expert_only:
                gc_tabs.add(frame, text=label)
        gc_tabs.select(gc_duplicate_batch_tab)

    apply_expert_mode()

    gc_defaults = {
        "solvent_end": "5.5", "quality_limit": "70", "rt_tolerance": "0.035",
        "cell_area_dm2": "0.51", "coverage": "1", "ov_ratio": "6",
        "is_amount": "10", "fc17_conc": "0.82", "bbp_conc": "0.83",
        "dnnp_conc": "0.82", "qc_min_area": "0", "blank_rt_tolerance": "0.04",
    }
    # The Einzelbestimmung tab: any number of RESULTS.CSV files, each one its
    # own workbook in its own folder, so there is no output path to enter.
    gc_duplicate_vars = {key: tk.StringVar() for key in ("blank", "blank_istd")}
    gc_single_files: list[Path] = []
    gc_duplicate_vars.update({key: tk.StringVar(value=value)
                              for key, value in gc_defaults.items()})
    gc_duplicate_status = tk.StringVar(value="Bereit")

    def gc_open_path(path: Path) -> None:
        if not str(path) or str(path) == "." or not path.exists():
            messagebox.showwarning("GC-Daten", "Die Ausgabe wurde noch nicht erstellt.", parent=root)
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Ausgabe öffnen", str(exc), parent=root)

    def gc_pick_csv(variable) -> None:
        selected = filedialog.askopenfilename(
            parent=root, title="CSV-Datei auswählen",
            filetypes=[("CSV-Dateien", "*.csv *.CSV"), ("Alle Dateien", "*.*")])
        if selected:
            variable.set(selected)

    def gc_single_refresh() -> None:
        """Redraw the file list: folder, CSV, detected LIB file and output."""
        gc_single_tree.delete(*gc_single_tree.get_children())
        engine = None
        try:
            engine = gc_engine()
        except Exception:
            pass
        for index, csv_path in enumerate(gc_single_files):
            if engine is not None:
                _name, output = engine.single_output_name(csv_path)
                library = engine.find_library_results(csv_path)
            else:
                output, library = csv_path.parent / "?", None
            gc_single_tree.insert("", tk.END, iid=str(index), values=(
                csv_path.parent.name, csv_path.name,
                library.name if library else "–", output.name))
        count = len(gc_single_files)
        gc_single_count.set(f"{count} Datei(en) ausgewählt" if count
                            else "Noch keine RESULTS.CSV ausgewählt")

    def gc_single_add_files() -> None:
        selected = filedialog.askopenfilenames(
            parent=root, title="RESULTS.CSV auswählen (eine oder mehrere)",
            filetypes=[("CSV-Dateien", "*.csv *.CSV"), ("Alle Dateien", "*.*")])
        known = {path.resolve() for path in gc_single_files}
        for item in selected:
            path = Path(item)
            if path.resolve() not in known:
                gc_single_files.append(path)
                known.add(path.resolve())
        gc_single_refresh()

    def gc_single_remove_selected() -> None:
        chosen = {int(iid) for iid in gc_single_tree.selection()}
        gc_single_files[:] = [path for index, path in enumerate(gc_single_files)
                              if index not in chosen]
        gc_single_refresh()

    def gc_single_clear() -> None:
        gc_single_files.clear()
        gc_single_refresh()

    duplicate_files = ttk.LabelFrame(
        gc_duplicate_tab, text="Eingaben und Ausgabe", style="Card.TLabelframe")
    duplicate_files.pack(fill=tk.X, pady=(0, 10))
    gc_single_count = tk.StringVar(value="Noch keine RESULTS.CSV ausgewählt")
    files_row = ttk.Frame(duplicate_files, style="Surface.TFrame")
    files_row.pack(fill=tk.X, pady=3)
    ttk.Label(files_row, text="RESULTS.CSV (eine oder mehrere)", style="Body.TLabel",
              width=38).pack(side=tk.LEFT)
    ttk.Label(files_row, textvariable=gc_single_count, style="Muted.TLabel").pack(
        side=tk.LEFT, fill=tk.X, expand=True)
    ttk.Button(files_row, text="Leeren", command=gc_single_clear,
               style="Secondary.TButton").pack(side=tk.RIGHT)
    ttk.Button(files_row, text="Entfernen", command=gc_single_remove_selected,
               style="Secondary.TButton").pack(side=tk.RIGHT, padx=(0, 6))
    ttk.Button(files_row, text="Auswählen", command=gc_single_add_files,
               style="Secondary.TButton").pack(side=tk.RIGHT, padx=(0, 6))

    single_list = ttk.Frame(duplicate_files, style="Surface.TFrame")
    single_list.pack(fill=tk.X, pady=(2, 6))
    single_columns = (
        ("folder", "Ordner", 300, "w"),
        ("csv", "RESULTS.CSV", 110, "w"),
        ("library", "LIBresults.csv (automatisch)", 170, "w"),
        ("output", "Ausgabe Excel", 300, "w"),
    )
    gc_single_tree = ttk.Treeview(
        single_list, columns=[key for key, *_ in single_columns],
        show="headings", height=4, selectmode="extended")
    for key, heading, width, anchor in single_columns:
        gc_single_tree.heading(key, text=heading, anchor=anchor)
        gc_single_tree.column(key, width=width, anchor=anchor,
                              stretch=key in {"folder", "output"})
    single_scroll = ttk.Scrollbar(single_list, orient="vertical",
                                  command=gc_single_tree.yview)
    gc_single_tree.configure(yscrollcommand=single_scroll.set)
    gc_single_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
    single_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    for label, key in (("Blank RESULTS.CSV (optional)", "blank"),
                       ("Blank+ISTD RESULTS.CSV (optional)", "blank_istd")):
        row_frame = ttk.Frame(duplicate_files, style="Surface.TFrame")
        row_frame.pack(fill=tk.X, pady=3)
        ttk.Label(row_frame, text=label, style="Body.TLabel", width=38).pack(side=tk.LEFT)
        ttk.Entry(row_frame, textvariable=gc_duplicate_vars[key]).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row_frame, text="Auswählen",
                   command=lambda variable=gc_duplicate_vars[key]: gc_pick_csv(variable),
                   style="Secondary.TButton").pack(side=tk.RIGHT)
    output_row = ttk.Frame(duplicate_files, style="Surface.TFrame")
    output_row.pack(fill=tk.X, pady=3)
    ttk.Label(output_row, text="Ausgabe Excel", style="Body.TLabel", width=38).pack(side=tk.LEFT)
    ttk.Label(output_row,
              text=("automatisch: im Ordner der jeweiligen RESULTS.CSV, benannt nach "
                    "diesem Ordner (ohne „.D“). Blank / Blank+ISTD gelten für alle "
                    "Dateien und sind optional."),
              style="Muted.TLabel", wraplength=620, justify="left").pack(
                  side=tk.LEFT, fill=tk.X, expand=True)

    duplicate_settings = ttk.LabelFrame(
        gc_duplicate_tab, text="Parameter", style="Card.TLabelframe")
    duplicate_settings.pack(fill=tk.X, pady=(0, 10))
    duplicate_parameter_rows = [
        ("LM-Ende [min]", "solvent_end"), ("Library Quality-Grenze", "quality_limit"),
        ("RT-Toleranz [min]", "rt_tolerance"), ("Zellfläche [dm²]", "cell_area_dm2"),
        ("Belegung", "coverage"), ("O/V-Ratio", "ov_ratio"),
        ("Menge interner Standard", "is_amount"), ("FC17 [mg/mL]", "fc17_conc"),
        ("BBP-d4 [mg/mL]", "bbp_conc"), ("DnNP-d4 [mg/mL]", "dnnp_conc"),
        ("QC Mindestfläche", "qc_min_area"), ("Blank RT-Toleranz [min]", "blank_rt_tolerance"),
    ]
    parameter_grid = ttk.Frame(duplicate_settings, style="Surface.TFrame")
    parameter_grid.pack(fill=tk.X)
    for index, (label, key) in enumerate(duplicate_parameter_rows):
        row_number, pair = divmod(index, 2)
        column = pair * 2
        ttk.Label(parameter_grid, text=label, style="Body.TLabel").grid(
            row=row_number, column=column, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(parameter_grid, textvariable=gc_duplicate_vars[key], width=14).grid(
            row=row_number, column=column + 1, sticky="w", padx=(0, 24), pady=3)

    def collect_migration_metadata(initial: Optional[dict[str, Any]] = None,
                                   subtitle: str = "") -> Optional[dict[str, Any]]:
        """Collect migration conditions once per batch and return the metadata.

        Spec v2.1 §V.3: the dialog runs before a batch is processed and before a
        batch analysis is opened in the workspace. What it returns are the
        *defaults* of every analysis in that batch; a per-analysis deviation is
        an override in the workspace's Parameter panel and never comes back
        here. ``initial`` is the caller's prefill, which falls back to the
        metadata of the last run.
        """
        # The last run wins over nothing, the caller wins over the last run.
        initial = {**load_migration_metadata_defaults(), **(initial or {})}
        dialog = tk.Toplevel(root)
        dialog.title("Migrations- und Berichtsdaten")
        dialog.geometry("900x690")
        dialog.minsize(820, 620)
        dialog.transient(root)
        dialog.grab_set()
        dialog.configure(bg=C["background"])
        result_holder: dict[str, Any] = {"value": None}

        saved_settings = load_user_settings()
        cell_choices = ["Stahlzelle groß (0,51 dm²)", "Stahlzelle klein (0,34 dm²)",
                        "Glaszelle (0,44 dm²)", "andere"]
        occupancy_choices = ["einfach", "doppelt"]
        volume_choices = ["10 mL", "100 mL", "andere"]
        ov_choices = ["6 dm²/kg", "andere"]
        duration_choices = ["2 d", "10 d", "andere"]
        temperature_choices = ["20 °C / RT", "40 °C", "60 °C", "andere"]
        simulant_choices = ["Ethanol 95 %", "Ethanol 50 %", "Ethanol 20 %",
                            "Essigsäure 3 %", "Tenax", "andere"]

        def remembered(setting: str, default: str, choices: list[str],
                       initial_key: Optional[str] = None) -> str:
            """Prefill from the caller, then from the last run, then the default.

            A stored value that is no longer offered falls back to the default,
            so a renamed option can never leave the radio group unselected.
            """
            candidate = text(initial.get(initial_key)) if initial_key else ""
            if not candidate:
                candidate = text(saved_settings.get(setting))
            return candidate if candidate in choices else default

        # ``syneris_sample_no`` is gone (§V.3): the sample number is derived from
        # the folder or sample name, so typing it again could only contradict the
        # number the batch is actually grouped by.
        fields = {key: tk.StringVar(value=text(initial.get(key))) for key in
                  ("syneris_summary_report_no", "analyst")}
        if not fields["analyst"].get().strip():
            fields["analyst"].set(text(saved_settings.get("last_analyst")))
        cell_var = tk.StringVar(value=remembered(
            "last_migration_cell", cell_choices[0], cell_choices, "migration_cell"))
        custom_area = tk.StringVar(value=text(saved_settings.get("last_migration_cell_custom")))
        occupancy_var = tk.StringVar(value=remembered(
            "last_occupancy", "einfach", occupancy_choices, "occupancy"))
        volume_var = tk.StringVar(value=remembered("last_volume", "10 mL", volume_choices))
        custom_volume = tk.StringVar(value=text(saved_settings.get("last_volume_custom")))
        ov_var = tk.StringVar(value=remembered("last_ov_ratio", "6 dm²/kg", ov_choices))
        custom_ov = tk.StringVar(value=text(saved_settings.get("last_ov_ratio_custom")))
        duration_var = tk.StringVar(value=remembered("last_duration", "10 d", duration_choices))
        custom_duration = tk.StringVar(value=text(saved_settings.get("last_duration_custom")))
        temperature_var = tk.StringVar(value=remembered(
            "last_temperature", "40 °C", temperature_choices))
        custom_temperature = tk.StringVar(value=text(saved_settings.get("last_temperature_custom")))
        simulant_var = tk.StringVar(value=remembered(
            "last_simulant", "Ethanol 95 %", simulant_choices))
        custom_simulant = tk.StringVar(value=text(saved_settings.get("last_simulant_custom")))
        preview_var = tk.StringVar()

        header = tk.Frame(dialog, bg=C["header"], height=72)
        header.pack(fill=tk.X)
        tk.Label(header, text="Migrations- und Berichtsdaten", bg=C["header"], fg=C["header_text"],
                 font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=22, pady=(13, 0))
        tk.Label(header, text=subtitle or (
                     "Gelten für den ganzen Batch. Abweichungen einzelner Analysen werden "
                     "später im Parameter-Panel des Workspace gesetzt."),
                 bg=C["header"], fg=C["header_muted"], font=("Segoe UI", 9)).pack(anchor="w", padx=22)
        body = ttk.Frame(dialog, style="App.TFrame", padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        columns = ttk.Frame(body, style="App.TFrame")
        columns.pack(fill=tk.BOTH, expand=True)
        left = ttk.LabelFrame(columns, text="Berichtsdaten", style="Card.TLabelframe")
        middle = ttk.LabelFrame(columns, text="Versuchsgeometrie", style="Card.TLabelframe")
        right = ttk.LabelFrame(columns, text="Migrationsbedingungen", style="Card.TLabelframe")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        middle.grid(row=0, column=1, sticky="nsew", padx=7)
        right.grid(row=0, column=2, sticky="nsew", padx=(7, 0))
        columns.columnconfigure((0, 1, 2), weight=1)

        for label, key in (("Syneris-Summary Report-Nr.", "syneris_summary_report_no"),
                           ("Auswerter *", "analyst")):
            ttk.Label(left, text=label, style="Body.TLabel").pack(anchor="w", pady=(6, 2))
            ttk.Entry(left, textvariable=fields[key]).pack(fill=tk.X)
        ttk.Label(left, text=("Syneris-Sample-Nr.: wird aus dem Ordner- bzw. "
                              "Probennamen abgeleitet (8-stellig) und benennt "
                              "den Word-Report einer Einzelprobe."),
                  style="Muted.TLabel", wraplength=230, justify="left").pack(
                      anchor="w", pady=(8, 2))

        def update_preview(*_args) -> None:
            # Rebound below after all controls have been created.
            return

        def radio_group(parent, title, variable, choices, custom_variable=None, unit=""):
            ttk.Label(parent, text=title, style="Body.TLabel").pack(anchor="w", pady=(8, 2))
            for choice in choices:
                row = ttk.Frame(parent, style="Surface.TFrame")
                row.pack(fill=tk.X, pady=1)
                ttk.Radiobutton(row, text=choice, value=choice, variable=variable,
                                command=update_preview).pack(side=tk.LEFT)
                if choice == "andere" and custom_variable is not None:
                    ttk.Entry(row, textvariable=custom_variable, width=9).pack(side=tk.LEFT, padx=4)
                    if unit:
                        ttk.Label(row, text=unit, style="Muted.TLabel").pack(side=tk.LEFT)
            if custom_variable is not None:
                custom_variable.trace_add("write", lambda *_: update_preview())

        radio_group(middle, "Migrationszelle", cell_var, cell_choices, custom_area, "dm²")
        radio_group(middle, "Belegung", occupancy_var, occupancy_choices)
        radio_group(middle, "Volumen", volume_var, volume_choices, custom_volume, "mL")
        radio_group(middle, "Oberfläche/Volumen", ov_var, ov_choices, custom_ov, "dm²/kg")
        radio_group(right, "Dauer", duration_var, duration_choices, custom_duration, "")
        radio_group(right, "Temperatur", temperature_var, temperature_choices,
                    custom_temperature, "")
        radio_group(right, "Simulans", simulant_var, simulant_choices, custom_simulant, "")

        preview = ttk.LabelFrame(body, text="Berechnungsvorschau", style="Card.TLabelframe")
        preview.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(preview, textvariable=preview_var, style="Body.TLabel", justify="left").pack(anchor="w")

        def selected_text(variable, custom, suffix=""):
            if variable.get() != "andere":
                return variable.get()
            value = custom.get().strip()
            return f"{value} {suffix}".strip()

        def build_metadata() -> dict[str, Any]:
            areas = {"Stahlzelle groß": 0.51, "Stahlzelle groß (0,51 dm²)": 0.51,
                     "Stahlzelle klein": 0.34, "Stahlzelle klein (0,34 dm²)": 0.34,
                     "Glaszelle": 0.44, "Glaszelle (0,44 dm²)": 0.44}
            area = areas.get(cell_var.get())
            if area is None:
                area = parse_localized_float(custom_area.get(), "andere Fläche")
            volumes = {"10 mL": 10.0, "100 mL": 100.0}
            volume = volumes.get(volume_var.get())
            if volume is None:
                volume = parse_localized_float(custom_volume.get(), "anderes Volumen")
            ratio = 6.0 if ov_var.get() == "6 dm²/kg" else parse_localized_float(custom_ov.get(), "anderes O/V-Verhältnis")
            occupancy_factor = 1.0 if occupancy_var.get() == "einfach" else 2.0
            # Calculation inputs this dialog does not edit — ISTD amount, the
            # three standard concentrations, reporting limit, quality threshold —
            # travel through unchanged, so what the dialog returns is a complete
            # input for AutoLib.Settings and not just the geometry half of it.
            carried = {key: initial[key] for key in
                       (*MIGRATION_SETTINGS_FIELDS, MIGRATION_REPORTING_LIMIT_KEY)
                       if key in initial}
            return validate_migration_metadata({
                **carried,
                **{key: variable.get().strip() for key, variable in fields.items()},
                "migration_cell": cell_var.get() if cell_var.get() != "andere" else f"andere Fläche ({format_decimal_de(area, 2)} dm²)",
                "cell_area_dm2": area, "occupancy": occupancy_var.get(),
                "occupancy_factor": occupancy_factor, "volume_ml": volume, "ov_ratio": ratio,
                "duration": selected_text(duration_var, custom_duration),
                "temperature": selected_text(temperature_var, custom_temperature),
                "simulant": selected_text(simulant_var, custom_simulant),
            })

        def update_preview(*_args) -> None:
            try:
                data = build_metadata()
                preview_var.set(
                    f"Wirksame Fläche: {format_decimal_de(data['effective_area_dm2'], 2)} dm²   |   "
                    f"Volumen: {format_decimal_de(data['volume_ml'], 1)} mL   |   "
                    f"S/V: {format_decimal_de(data['ov_ratio'], 1)} dm²/kg\n"
                    f"Word-Feld Migrate: {data['migrate_text']}")
            except Exception as exc:
                preview_var.set(f"Eingabe noch unvollständig: {exc}")

        for variable in (cell_var, occupancy_var, volume_var, ov_var, duration_var, temperature_var, simulant_var):
            variable.trace_add("write", update_preview)
        update_preview()

        actions = ttk.Frame(body, style="App.TFrame")
        actions.pack(fill=tk.X, pady=(12, 0))
        def accept() -> None:
            try:
                result_holder["value"] = build_metadata()
                # Remember the whole form, not just the analyst, so a series of
                # samples under identical conditions needs no repeated typing.
                save_user_settings(
                    last_analyst=result_holder["value"]["analyst"],
                    last_migration_cell=cell_var.get(),
                    last_migration_cell_custom=custom_area.get().strip(),
                    last_occupancy=occupancy_var.get(),
                    last_volume=volume_var.get(),
                    last_volume_custom=custom_volume.get().strip(),
                    last_ov_ratio=ov_var.get(),
                    last_ov_ratio_custom=custom_ov.get().strip(),
                    last_duration=duration_var.get(),
                    last_duration_custom=custom_duration.get().strip(),
                    last_temperature=temperature_var.get(),
                    last_temperature_custom=custom_temperature.get().strip(),
                    last_simulant=simulant_var.get(),
                    last_simulant_custom=custom_simulant.get().strip(),
                )
                # The validated metadata itself is the prefill of the next batch
                # (§V.3): the radio choices above only remember the widgets, not
                # the numbers a caller carried in.
                save_migration_metadata_defaults(result_holder["value"])
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("Eingaben prüfen", str(exc), parent=dialog)
        ttk.Button(actions, text="Abbrechen", command=dialog.destroy, style="Secondary.TButton").pack(side=tk.RIGHT)
        brand_button(actions, "Übernehmen und auswerten", accept, theme=theme,
                     padx=18, pady=8).pack(side=tk.RIGHT, padx=8)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        root.wait_window(dialog)
        return result_holder["value"]

    def gc_grid_calculation_inputs(variables) -> dict[str, Any]:
        """Read the calculation inputs the parameter grid owns, as metadata.

        The dialog does not edit ISTD amount, standard concentrations or the
        thresholds, but they belong to the same ``Settings`` object, so they are
        carried into the metadata and back out again as one set (§V.3).
        """
        inputs: dict[str, Any] = {}
        for key, attribute in MIGRATION_SETTINGS_FIELDS.items():
            variable = variables.get(attribute)
            number = numeric_value(variable.get()) if variable is not None else None
            if number is not None:
                inputs[key] = number
        return inputs

    def gc_collect_migration_scope(variables, subtitle: str = "") -> Optional[MigrationMetadataScope]:
        """Run the metadata dialog once for a batch and return its scope.

        Spec v2.1 §V.3: what the analyst enters here are the defaults of every
        analysis in the batch. The three geometry values are written back into
        the parameter grid so grid and dialog can never disagree; per-analysis
        deviations are overrides on the returned scope.
        """
        metadata = collect_migration_metadata(gc_grid_calculation_inputs(variables), subtitle)
        if metadata is None:
            return None
        for key, attribute in (("cell_area_dm2", "cell_area_dm2"),
                               ("occupancy_factor", "coverage"),
                               ("ov_ratio", "ov_ratio")):
            variable = variables.get(attribute)
            if variable is not None:
                variable.set(str(metadata[key]))
        return MigrationMetadataScope(metadata)

    def gc_run_single() -> None:
        """Write one Einzelbestimmung workbook per selected RESULTS.CSV.

        The metadata dialog runs once for the whole selection. A file that
        fails is reported at the end instead of stopping the others. No ISTD is
        looked for here: the analyst chooses it per row in the workbook.
        """
        try:
            engine = gc_engine()
            if not gc_single_files:
                raise ValueError("Bitte mindestens eine RESULTS.CSV auswählen.")
            missing = [str(path) for path in gc_single_files if not path.is_file()]
            if missing:
                raise ValueError("Datei nicht gefunden:\n" + "\n".join(missing))
            jobs = []
            for csv_path in gc_single_files:
                name, output = engine.single_output_name(csv_path)
                jobs.append((csv_path, name, output))
            existing = [output for _csv, _name, output in jobs if output.exists()]
            if existing and not messagebox.askyesno(
                    "Dateien vorhanden",
                    "Diese Ausgabedateien existieren bereits und werden überschrieben:\n\n"
                    + "\n".join(str(path) for path in existing[:12])
                    + ("\n…" if len(existing) > 12 else "") + "\n\nFortfahren?",
                    parent=root):
                return
            scope = gc_collect_migration_scope(
                gc_duplicate_vars,
                "Gelten für alle ausgewählten Einzelbestimmungen.")
            if scope is None:
                return
            blank = gc_duplicate_vars["blank"].get().strip()
            blank_istd = gc_duplicate_vars["blank_istd"].get().strip()
            created, failed, without_istd = [], [], []
            for index, (csv_path, name, output) in enumerate(jobs, start=1):
                gc_duplicate_status.set(
                    f"Einzelbestimmung {index}/{len(jobs)}: {name} …")
                root.update_idletasks()
                try:
                    migration_metadata = scope.for_analysis(name)
                    settings = apply_migration_metadata_to_settings(
                        gc_duplicate_settings(gc_duplicate_vars), migration_metadata)
                    library = engine.find_library_results(csv_path)
                    info = engine.make_single_workbook(
                        str(csv_path), str(library) if library else "", str(output),
                        settings, blank, blank_istd, name)
                    write_migration_metadata_to_workbook(Path(info["output"]),
                                                         migration_metadata)
                    created.append(Path(info["output"]))
                    if not info.get("istd_named"):
                        without_istd.append(name)
                except Exception as exc:
                    failed.append(f"{name}: {exc}")
            gc_duplicate_status.set(
                f"Fertig: {len(created)} erstellt, {len(failed)} fehlgeschlagen. "
                "ISTD je Zeile in Excel wählen.")
            message = [f"{len(created)} Einzelbestimmung(en) erstellt:"]
            message += [f"  {path}" for path in created[:12]]
            if len(created) > 12:
                message.append("  …")
            if without_istd:
                message += ["", "Kein ISTD per Name gefunden (Fläche auf Blatt 'ISTD' "
                                "bei Bedarf eintragen):"]
                message += [f"  {name}" for name in without_istd]
            if failed:
                message += ["", "Fehlgeschlagen:"] + [f"  {item}" for item in failed]
            message += ["", "In der Spalte 'ISTD' je Zeile den internen Standard wählen, "
                            "die Excel-Datei prüfen und anschließend auf der Seite "
                            "NIAS Report auswählen."]
            (messagebox.showwarning if failed else messagebox.showinfo)(
                "Einzelbestimmung", "\n".join(message), parent=root)
        except Exception as exc:
            gc_duplicate_status.set("Fehler")
            messagebox.showerror("GC-Daten – Fehler", str(exc), parent=root)

    def gc_open_single_output() -> None:
        """Open the workbook of the selected file, or of the first one."""
        if not gc_single_files:
            messagebox.showwarning("GC-Daten", "Bitte zuerst eine RESULTS.CSV auswählen.",
                                   parent=root)
            return
        selection = gc_single_tree.selection()
        csv_path = gc_single_files[int(selection[0])] if selection else gc_single_files[0]
        try:
            _name, output = gc_engine().single_output_name(csv_path)
        except Exception as exc:
            messagebox.showerror("Ausgabe öffnen", str(exc), parent=root)
            return
        gc_open_path(output)

    duplicate_actions = ttk.Frame(gc_duplicate_tab, style="App.TFrame")
    duplicate_actions.pack(fill=tk.X)
    brand_button(duplicate_actions, "Einzelbestimmung auswerten",
                 gc_run_single, theme=theme).pack(side=tk.LEFT)
    ttk.Button(
        duplicate_actions, text="Excel öffnen", command=gc_open_single_output,
        style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
    ttk.Label(duplicate_actions, textvariable=gc_duplicate_status,
              style="Ready.TLabel").pack(side=tk.LEFT, padx=14)

    gc_duplicate_batch_vars = {"batch": tk.StringVar(), "output": tk.StringVar()}
    gc_duplicate_batch_vars.update({key: tk.StringVar(value=value)
                                    for key, value in gc_defaults.items()})
    gc_duplicate_batch_discovery = tk.StringVar(value="Noch kein Batch-Ordner ausgewählt")
    gc_duplicate_batch_status = tk.StringVar(value="Bereit")

    def gc_duplicate_settings(variables):
        engine = gc_engine()
        return engine.Settings(
            solvent_end=float(variables["solvent_end"].get()),
            quality_limit=int(variables["quality_limit"].get()),
            rt_tolerance=float(variables["rt_tolerance"].get()),
            cell_area_dm2=float(variables["cell_area_dm2"].get()),
            coverage=float(variables["coverage"].get()),
            ov_ratio=float(variables["ov_ratio"].get()),
            is_amount=float(variables["is_amount"].get()),
            fc17_conc=float(variables["fc17_conc"].get()),
            bbp_conc=float(variables["bbp_conc"].get()),
            dnnp_conc=float(variables["dnnp_conc"].get()),
            qc_min_area=float(variables["qc_min_area"].get()),
            blank_rt_tolerance=float(variables["blank_rt_tolerance"].get()))

    #: The analysis list's rows, keyed by their Treeview item id (§V.6).
    gc_analysis_rows: dict[str, dict[str, Any]] = {}
    #: The batch's Blank and Blank+ISTD folders; they apply to every analysis.
    gc_analysis_blanks: dict[str, Any] = {}

    def gc_refresh_duplicate_batch():
        """Discover the batch and redraw the analysis list.

        Returns the analyses so the batch run can use the same discovery result
        the analyst is looking at. One row per analysis — a Doppelbestimmung is
        one row, not two (§V.6).
        """
        gc_analysis_tree.delete(*gc_analysis_tree.get_children())
        gc_analysis_rows.clear()
        gc_analysis_blanks.clear()
        batch = gc_duplicate_batch_vars["batch"].get().strip()
        if not batch:
            gc_duplicate_batch_discovery.set("Noch kein Batch-Ordner ausgewählt")
            return []
        try:
            engine = gc_engine()
            samples, blanks, issues = engine.discover_duplicate_samples(batch)
            gc_analysis_blanks.update(blanks or {})
            output_folder = gc_duplicate_batch_vars["output"].get().strip() or batch
            # The session stays with the batch even when the workbooks are
            # written somewhere else, so the two folders are asked separately.
            sessions = {key: analysis_session_path(path) for key, path in
                        engine.duplicate_output_paths(samples, batch).items()}
            workbooks = engine.duplicate_output_paths(samples, output_folder)
            rows = analysis_rows(samples, issues, sessions, workbooks)
        except Exception as exc:
            gc_duplicate_batch_discovery.set(str(exc))
            return []

        for row in rows:
            children = determination_rows(row)
            icon = ("folder_open" if children else "blank" if row["error"]
                    else "file")
            item = gc_analysis_tree.insert(
                "", "end", text=row["name"], image=analysis_images[icon],
                open=bool(children),
                tags=("error",) if row["error"] else (row["status"],),
                values=("—" if row["error"] and not row["syneris"] else row["syneris"],
                        "—" if row["error"] else str(row["count"]),
                        row["status"] or "—", row["note"]))
            gc_analysis_rows[item] = row
            # A Doppelbestimmung carries its two Einzelbestimmungen as child
            # rows, each of which opens on its own like a single determination.
            for index, child in enumerate(children):
                branch = "last" if index == len(children) - 1 else "branch"
                child_item = gc_analysis_tree.insert(
                    item, "end", text=child["name"],
                    image=analysis_images[branch], tags=(child["status"],),
                    values=(child["syneris"], f"Bestimmung {child['number']}",
                            child["status"], ""))
                gc_analysis_rows[child_item] = child

        singles = sum(1 for row in rows if not row["error"] and row["count"] == 1)
        pairs = sum(1 for row in rows if not row["error"] and row["count"] == 2)
        problems = sum(1 for row in rows if row["error"])
        summary = [f"{pairs} Doppelbestimmung(en), {singles} Einzelbestimmung(en)",
                   "Blank: " + (blanks["blank"]["name"] if blanks["blank"] else "keiner"),
                   "Blank+ISTD: " + (blanks["blank_istd"]["name"]
                                     if blanks["blank_istd"] else "keiner")]
        if problems:
            summary.append(f"{problems} Hinweis(e)")
        gc_duplicate_batch_discovery.set("  ·  ".join(summary))
        return samples

    def gc_selected_analysis(event=None) -> Optional[dict[str, Any]]:
        """The row under the pointer, or the selected one."""
        item = ""
        if event is not None:
            item = gc_analysis_tree.identify_row(event.y)
        if not item:
            selection = gc_analysis_tree.selection()
            item = selection[0] if selection else gc_analysis_tree.focus()
        return gc_analysis_rows.get(item)

    def gc_open_selected_analysis(event=None) -> None:
        """Double-click or button: open that analysis in the GC workspace."""
        row = gc_selected_analysis(event)
        if row is None:
            messagebox.showinfo("Analyse öffnen",
                                "Bitte zuerst eine Analyse in der Liste wählen.",
                                parent=root)
            return
        if row["error"]:
            messagebox.showwarning(
                "Analyse nicht auswertbar",
                row["note"] or "Diese Zeile ist kein auswertbarer Datensatz.",
                parent=root)
            return
        open_nias_workspace(analysis=row)

    def gc_analysis_tooltip(event) -> str:
        """The determination folder names behind the ``Bestimmungen`` count."""
        row = gc_analysis_rows.get(gc_analysis_tree.identify_row(event.y))
        if row is None or not row["folders"]:
            return ""
        return "\n".join(row["folders"])

    def gc_pick_duplicate_batch() -> None:
        selected = filedialog.askdirectory(parent=root, title="Batch-Ordner auswählen")
        if selected:
            gc_duplicate_batch_vars["batch"].set(selected)
            gc_duplicate_batch_vars["output"].set(selected)
            gc_refresh_duplicate_batch()

    def gc_pick_duplicate_batch_output() -> None:
        selected = filedialog.askdirectory(parent=root, title="Ausgabeordner auswählen")
        if selected:
            gc_duplicate_batch_vars["output"].set(selected)
            # "exportiert" is read from the output folder, so the list has to be
            # redrawn when that folder changes.
            gc_refresh_duplicate_batch()

    duplicate_batch_inputs = ttk.LabelFrame(
        gc_duplicate_batch_tab, text="Batch und Ausgabe", style="Card.TLabelframe")
    duplicate_batch_inputs.pack(fill=tk.X, pady=(0, 10))
    for label, key, command in (
        ("Batch-Ordner", "batch", gc_pick_duplicate_batch),
        ("Ausgabeordner", "output", gc_pick_duplicate_batch_output),
    ):
        row_frame = ttk.Frame(duplicate_batch_inputs, style="Surface.TFrame")
        row_frame.pack(fill=tk.X, pady=4)
        ttk.Label(row_frame, text=label, style="Body.TLabel", width=38).pack(side=tk.LEFT)
        ttk.Entry(row_frame, textvariable=gc_duplicate_batch_vars[key]).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row_frame, text="Auswählen", command=command,
                   style="Secondary.TButton").pack(side=tk.RIGHT)

    # Spec v2.1 §V.6: the batch folder produces a list of analyses, and the
    # analysis — not the folder — is what gets opened. One row per analysis: a
    # Doppelbestimmung is one row with two determinations behind it, a single
    # determination is one row with one. Double-click opens it.
    duplicate_batch_list = ttk.LabelFrame(
        gc_duplicate_batch_tab, text="Analysen in diesem Batch",
        style="Card.TLabelframe")
    duplicate_batch_list.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
    analysis_columns = (
        ("syneris", "Syneris-Nr.", 100, "w"),
        ("count", "Bestimmungen", 100, "center"),
        ("status", "Status", 100, "w"),
        ("note", "Hinweis", 380, "w"),
    )
    analysis_body = ttk.Frame(duplicate_batch_list, style="Surface.TFrame")
    analysis_body.pack(fill=tk.BOTH, expand=True)
    gc_analysis_tree = ttk.Treeview(
        analysis_body, columns=[key for key, *_ in analysis_columns],
        show="tree headings", height=8, selectmode="browse",
        style="Analysis.Treeview")
    # The sample sits in the tree column, so the list reads like a folder
    # tree: a Doppelbestimmung is a folder, its Einzelbestimmungen are files
    # hanging off it by a line.
    gc_analysis_tree.heading("#0", text="Probe", anchor="w")
    gc_analysis_tree.column("#0", width=380, minwidth=200, stretch=False)
    for key, heading, width, anchor in analysis_columns:
        gc_analysis_tree.heading(key, text=heading, anchor=anchor)
        gc_analysis_tree.column(key, width=width, anchor=anchor,
                                stretch=(key == "note"))
    analysis_scroll = ttk.Scrollbar(analysis_body, orient="vertical",
                                    command=gc_analysis_tree.yview)
    gc_analysis_tree.configure(yscrollcommand=analysis_scroll.set)
    analysis_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    gc_analysis_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    # The status is the first thing to read off the list, so it is a colour too.
    def paint_analysis_tags(colors: dict[str, str]) -> None:
        if not gc_analysis_tree.winfo_exists():
            raise tk.TclError("analysis tree gone")
        gc_analysis_tree.tag_configure(ANALYSIS_STATUS_EXPORTED,
                                       background=colors["row_ok"],
                                       foreground=colors["text"])
        gc_analysis_tree.tag_configure(ANALYSIS_STATUS_EDITED,
                                       background=colors["row_edit"],
                                       foreground=colors["text"])
        gc_analysis_tree.tag_configure("error", background=colors["row_error"],
                                       foreground=colors["row_error_text"])
        draw_analysis_tree_images(analysis_images, colors)

    analysis_images = analysis_tree_images(gc_analysis_tree)
    theme.on_change(paint_analysis_tags)

    def gc_sync_folder_images(_event=None) -> None:
        """An open folder has a line down to its files, a closed one does not."""
        for item in gc_analysis_tree.get_children(""):
            if gc_analysis_tree.get_children(item):
                opened = bool(gc_analysis_tree.item(item, "open"))
                gc_analysis_tree.item(item, image=analysis_images[
                    "folder_open" if opened else "folder"])

    # The open state changes after the event fires, hence after_idle.
    for sequence in ("<<TreeviewOpen>>", "<<TreeviewClose>>"):
        gc_analysis_tree.bind(
            sequence, lambda _event: gc_analysis_tree.after_idle(gc_sync_folder_images))
    # "break": a double-click opens the analysis instead of also folding the
    # Doppelbestimmung's Einzelbestimmungen away (the Treeview's class binding).
    gc_analysis_tree.bind("<Double-1>",
                          lambda event: (gc_open_selected_analysis(event), "break")[1])
    gc_analysis_tree.bind("<Return>", gc_open_selected_analysis)
    attach_tooltip(gc_analysis_tree, gc_analysis_tooltip)

    analysis_actions = ttk.Frame(duplicate_batch_list, style="Surface.TFrame")
    analysis_actions.pack(fill=tk.X, pady=(8, 2))
    brand_button(analysis_actions, "Analyse im GC-Workspace öffnen  →",
                 gc_open_selected_analysis, theme=theme).pack(side=tk.LEFT)
    ttk.Button(analysis_actions, text="Liste aktualisieren",
               command=gc_refresh_duplicate_batch,
               style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
    ttk.Label(analysis_actions, textvariable=gc_duplicate_batch_discovery,
              style="Muted.TLabel", wraplength=620).pack(side=tk.LEFT, padx=10)

    duplicate_batch_settings = ttk.LabelFrame(
        gc_duplicate_batch_tab, text="Parameter", style="Card.TLabelframe")
    duplicate_batch_settings.pack(fill=tk.X, pady=(0, 10))
    duplicate_batch_grid = ttk.Frame(duplicate_batch_settings, style="Surface.TFrame")
    duplicate_batch_grid.pack(fill=tk.X)
    for index, (label, key) in enumerate(duplicate_parameter_rows):
        row_number, pair = divmod(index, 2)
        column = pair * 2
        ttk.Label(duplicate_batch_grid, text=label, style="Body.TLabel").grid(
            row=row_number, column=column, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(duplicate_batch_grid, textvariable=gc_duplicate_batch_vars[key],
                  width=14).grid(row=row_number, column=column + 1, sticky="w",
                                 padx=(0, 24), pady=3)

    ttk.Label(
        gc_duplicate_batch_tab,
        text=("Gruppiert über die 8-stellige Syneris-Nummer im Ordnernamen: eine Analyse besteht "
              "aus einer oder zwei Bestimmungen, drei oder mehr sind ein Benennungsfehler. Bei "
              "einer Einzelbestimmung bleiben die Spalten der zweiten Bestimmung leer und "
              "ausgeblendet. Unter jeder Doppelbestimmung stehen ihre zwei Einzelbestimmungen; "
              "ein Doppelklick darauf öffnet sie einzeln im GC-Workspace. Blank- und Blank+ISTD-Ordner werden am Namen erkannt und gelten für "
              "den ganzen Batch. Die erzeugten Dateien vor der Seite NIAS Report manuell prüfen."),
        style="PageNote.TLabel",
        wraplength=880, justify="left").pack(anchor="w", pady=(2, 10))

    def gc_run_duplicate_batch() -> None:
        try:
            engine = gc_engine()
            samples = gc_refresh_duplicate_batch()
            if not samples:
                raise ValueError("Keine auswertbare Analyse im Batch gefunden.")
            output_folder = Path(gc_duplicate_batch_vars["output"].get()
                                 or gc_duplicate_batch_vars["batch"].get())
            if not output_folder.is_dir():
                raise ValueError("Bitte einen gültigen Ausgabeordner auswählen.")
            # Once per batch (§V.3): these become the defaults of every analysis.
            scope = gc_collect_migration_scope(
                gc_duplicate_batch_vars,
                f"Gelten als Vorgabe für alle {len(samples)} Probe(n) dieses Batches; "
                "Abweichungen einzelner Analysen später im Parameter-Panel.")
            if scope is None:
                return
            settings = apply_migration_metadata_to_settings(
                gc_duplicate_settings(gc_duplicate_batch_vars), scope.for_analysis())
            targets = engine.duplicate_output_paths(samples, str(output_folder))
            existing = [path for path in targets.values() if path.exists()]
            overwrite = False
            if existing:
                overwrite = messagebox.askyesno(
                    "Vorhandene Dateien",
                    f"{len(existing)} Zieldatei(en) existieren bereits. Überschreiben?\n\n"
                    "Bei 'Nein' werden diese Proben übersprungen.", parent=root)

            def progress(index, total, sample_name):
                gc_duplicate_batch_status.set(f"Probe {index} von {total}: {sample_name}")
                root.update_idletasks()

            # The review layout and the report metadata go into each workbook
            # in one save after the engine, not in one save each.
            with deferred_duplicate_postprocessing() as pending:
                info = engine.process_duplicate_batch(
                    gc_duplicate_batch_vars["batch"].get(), str(output_folder), settings,
                    overwrite, progress)

            def batch_metadata(created_item) -> dict[str, Any]:
                # Keyed by the derived Syneris number — the same key
                # ``discover_duplicate_samples`` grouped the two determinations
                # by — so a per-analysis deviation lands in the right workbook.
                analysis_key = (created_item.get("syneris")
                                if isinstance(created_item, dict) else "") or ""
                return scope.for_analysis(analysis_key)

            gc_duplicate_batch_status.set("Arbeitsmappen werden fertiggestellt …")
            root.update_idletasks()
            finish_duplicate_batch(info, pending, batch_metadata)
            gc_duplicate_batch_status.set(
                f"Fertig: {len(info['created'])} erstellt, {len(info['skipped'])} übersprungen, "
                f"{len(info['failed'])} Fehler. Excel jetzt manuell prüfen.")
            details = "\n".join(
                f"{item['sample']}: {item['error']}"
                for item in info["failed"] + info["discovery_issues"])
            message = (
                f"Erstellt: {len(info['created'])}\nÜbersprungen: {len(info['skipped'])}\n"
                f"Fehler: {len(info['failed'])}\n\n"
                f"Blank: {info['blank'] or 'nicht verwendet'}\n"
                f"Blank+ISTD: {info['blank_istd'] or 'nicht verwendet'}\n\n"
                "Bitte die erzeugten Doppelbestimmungs-Dateien manuell prüfen und "
                "anschließend auf der Seite NIAS Report auswählen.")
            # The ISTD is marked per row now; a standard the detection missed
            # only leaves its mark empty, so name those workbooks for review.
            incomplete = []
            for item in info.get("created", []):
                if not isinstance(item, dict):
                    continue
                gaps = []
                for number in range(1, int(item.get("determinations") or 1) + 1):
                    found = item.get(f"istd_found_{number}")
                    if found is None:
                        continue
                    missing = [label for label in ("IS1", "IS2", "IS3") if label not in found]
                    if missing:
                        gaps.append(f"Bestimmung {number}: {', '.join(missing)}")
                if gaps:
                    incomplete.append(f"{item.get('sample')}: " + "; ".join(gaps))
            message += ("\n\nDie internen Standards sind in 'Bestimmung_1'/'Bestimmung_2' "
                        "in der Spalte 'ISTD' vorbelegt und können dort geändert werden.")
            if incomplete:
                message += ("\n\nNicht automatisch gefunden (bitte in Spalte 'ISTD' "
                            "markieren):\n" + "\n".join(incomplete))
            if details:
                message += "\n\n" + details
            messagebox.showinfo("Doppelbestimmung Batch abgeschlossen", message, parent=root)
        except Exception as exc:
            gc_duplicate_batch_status.set("Fehler")
            messagebox.showerror("GC-Daten – Fehler", str(exc), parent=root)

    duplicate_batch_actions = ttk.Frame(gc_duplicate_batch_tab, style="App.TFrame")
    duplicate_batch_actions.pack(fill=tk.X)
    brand_button(duplicate_batch_actions, "Doppelbestimmung Batch auswerten",
                 gc_run_duplicate_batch, theme=theme).pack(side=tk.LEFT)
    ttk.Button(
        duplicate_batch_actions, text="Ausgabeordner öffnen",
        command=lambda: gc_open_path(Path(gc_duplicate_batch_vars["output"].get())),
        style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
    ttk.Label(duplicate_batch_actions, textvariable=gc_duplicate_batch_status,
              style="Ready.TLabel", wraplength=650).pack(side=tk.LEFT, padx=14)

    gc_fingerprint_vars = {
        "batch": tk.StringVar(), "blank": tk.StringVar(), "output": tk.StringVar(),
        "solvent_end": tk.StringVar(value=gc_defaults["solvent_end"]),
        "quality_limit": tk.StringVar(value=gc_defaults["quality_limit"]),
        "rt_tolerance": tk.StringVar(value=gc_defaults["rt_tolerance"]),
        "blank_rt_tolerance": tk.StringVar(value=gc_defaults["blank_rt_tolerance"]),
    }
    gc_fingerprint_discovery = tk.StringVar(value="Noch kein Batch-Ordner ausgewählt")
    gc_fingerprint_status = tk.StringVar(value="Bereit")

    def gc_refresh_fingerprint() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            samples, issues = gc_engine().discover_fingerprint_samples(
                gc_fingerprint_vars["batch"].get(), gc_fingerprint_vars["blank"].get())
            message = f"{len(samples)} gültige Probenordner"
            if issues:
                message += f", {len(issues)} Problem(e): " + "; ".join(
                    f"{item['sample']}: {item['error']}" for item in issues)
            gc_fingerprint_discovery.set(message)
            return samples, issues
        except Exception as exc:
            gc_fingerprint_discovery.set(str(exc))
            return [], []

    def gc_pick_batch() -> None:
        selected = filedialog.askdirectory(parent=root, title="Batch-Ordner auswählen")
        if selected:
            gc_fingerprint_vars["batch"].set(selected)
            if not gc_fingerprint_vars["output"].get():
                gc_fingerprint_vars["output"].set(selected)
            gc_refresh_fingerprint()

    def gc_pick_fingerprint_blank() -> None:
        selected = filedialog.askopenfilename(
            parent=root, title="Gemeinsamen Blank auswählen",
            filetypes=[("CSV-Dateien", "*.csv *.CSV"), ("Alle Dateien", "*.*")])
        if selected:
            gc_fingerprint_vars["blank"].set(selected)
            gc_refresh_fingerprint()

    def gc_pick_fingerprint_output() -> None:
        selected = filedialog.askdirectory(parent=root, title="Ausgabeordner auswählen")
        if selected:
            gc_fingerprint_vars["output"].set(selected)

    fingerprint_inputs = ttk.LabelFrame(
        gc_fingerprint_tab, text="Batch und Ausgabe", style="Card.TLabelframe")
    fingerprint_inputs.pack(fill=tk.X, pady=(0, 10))
    for label, key, command in (
        ("Batch-Ordner", "batch", gc_pick_batch),
        ("Gemeinsamer Blank RESULTS.CSV (optional)", "blank", gc_pick_fingerprint_blank),
        ("Ausgabeordner", "output", gc_pick_fingerprint_output),
    ):
        row_frame = ttk.Frame(fingerprint_inputs, style="Surface.TFrame")
        row_frame.pack(fill=tk.X, pady=4)
        ttk.Label(row_frame, text=label, style="Body.TLabel", width=38).pack(side=tk.LEFT)
        ttk.Entry(row_frame, textvariable=gc_fingerprint_vars[key]).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(row_frame, text="Auswählen", command=command,
                   style="Secondary.TButton").pack(side=tk.RIGHT)
    ttk.Label(fingerprint_inputs, textvariable=gc_fingerprint_discovery,
              style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(5, 0))

    fingerprint_settings = ttk.LabelFrame(
        gc_fingerprint_tab, text="Parameter", style="Card.TLabelframe")
    fingerprint_settings.pack(fill=tk.X, pady=(0, 10))
    settings_row = ttk.Frame(fingerprint_settings, style="Surface.TFrame")
    settings_row.pack(fill=tk.X)
    for index, (label, key) in enumerate((
        ("LM-Ende [min]", "solvent_end"), ("Quality-Grenze", "quality_limit"),
        ("FID/PBM RT-Toleranz [min]", "rt_tolerance"),
        ("Blank RT-Toleranz [min]", "blank_rt_tolerance"),
    )):
        ttk.Label(settings_row, text=label, style="Body.TLabel").grid(
            row=0, column=index * 2, sticky="w", padx=(0, 5))
        ttk.Entry(settings_row, textvariable=gc_fingerprint_vars[key], width=10).grid(
            row=0, column=index * 2 + 1, sticky="w", padx=(0, 18))

    ttk.Label(
        gc_fingerprint_tab,
          text=("Ein Workbook wird pro Probenordner erstellt. In dieser Prüfdatei bleibt die rohe "
              "Area % unverändert; Blanktreffer werden nur markiert. Danach die Tabelle "
              "'Fingerprint' manuell prüfen und unerwünschte Zeilen löschen. Im finalen NIAS "
              "Report werden die verbleibenden Area-%-Werte automatisch auf 100 % neu berechnet."),
        style="PageNote.TLabel",
        wraplength=880, justify="left").pack(anchor="w", pady=(2, 10))

    def gc_run_fingerprint() -> None:
        try:
            engine = gc_engine()
            samples, _ = gc_refresh_fingerprint()
            output_folder = Path(gc_fingerprint_vars["output"].get())
            if not samples:
                raise ValueError("Keine gültigen Probenordner im Batch gefunden.")
            if not output_folder.is_dir():
                raise ValueError("Bitte einen gültigen Ausgabeordner auswählen.")
            settings = engine.Settings(
                solvent_end=float(gc_fingerprint_vars["solvent_end"].get()),
                quality_limit=int(gc_fingerprint_vars["quality_limit"].get()),
                rt_tolerance=float(gc_fingerprint_vars["rt_tolerance"].get()),
                blank_rt_tolerance=float(gc_fingerprint_vars["blank_rt_tolerance"].get()))
            targets = engine.fingerprint_output_paths(samples, str(output_folder))
            existing = [path for path in targets.values() if path.exists()]
            overwrite = False
            if existing:
                overwrite = messagebox.askyesno(
                    "Vorhandene Dateien",
                    f"{len(existing)} Zieldatei(en) existieren bereits. Überschreiben?\n\n"
                    "Bei 'Nein' werden diese Proben übersprungen.", parent=root)

            def progress(index, total, sample_name):
                gc_fingerprint_status.set(f"Probe {index} von {total}: {sample_name}")
                root.update_idletasks()

            info = engine.process_fingerprint_batch(
                gc_fingerprint_vars["batch"].get(), str(output_folder), settings,
                gc_fingerprint_vars["blank"].get(), overwrite, progress)
            gc_fingerprint_status.set(
                f"Fertig: {len(info['created'])} erstellt, {len(info['skipped'])} übersprungen, "
                f"{len(info['failed'])} Fehler. Excel jetzt manuell prüfen.")
            details = "\n".join(
                f"{item['sample']}: {item['error']}" for item in info["failed"])
            message = (
                f"Erstellt: {len(info['created'])}\nÜbersprungen: {len(info['skipped'])}\n"
                f"Fehler: {len(info['failed'])}\n\nBitte die erzeugten Fingerprint-Dateien manuell "
                "prüfen und anschließend auf der Seite NIAS Report auswählen.")
            if details:
                message += "\n\n" + details
            messagebox.showinfo("Fingerprint Screening abgeschlossen", message, parent=root)
        except Exception as exc:
            gc_fingerprint_status.set("Fehler")
            messagebox.showerror("GC-Daten – Fehler", str(exc), parent=root)

    fingerprint_actions = ttk.Frame(gc_fingerprint_tab, style="App.TFrame")
    fingerprint_actions.pack(fill=tk.X)
    brand_button(fingerprint_actions, "Fingerprint Batch auswerten",
                 gc_run_fingerprint, theme=theme).pack(side=tk.LEFT)
    ttk.Button(
        fingerprint_actions, text="Ausgabeordner öffnen",
        command=lambda: gc_open_path(Path(gc_fingerprint_vars["output"].get())),
        style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
    ttk.Label(fingerprint_actions, textvariable=gc_fingerprint_status,
              style="Ready.TLabel", wraplength=650).pack(side=tk.LEFT, padx=14)

    # DIN SPEC page: one workbook with three sheets (_A, _B, _C).
    ttk.Label(din_content, text="DIN SPEC 91521", style="PageTitle.TLabel").pack(anchor="w", pady=(0, 3))
    ttk.Label(din_content, text="Zwei unabhängige Werkzeuge: Dreifachbestimmung verarbeiten oder aus einer reportfähigen Excel-Datei einen MACE-Word-Report erzeugen.", style="PageText.TLabel").pack(anchor="w", pady=(0, 14))

    # The DIN SPEC workspace is reached from the DIN SPEC page, not from the
    # GC-Daten page, so each page opens the mode it is about (spec v3.1 §VII.11).
    din_workspace_card = ttk.Frame(din_content, style="App.TFrame")
    din_workspace_card.pack(fill=tk.X, pady=(0, 12))
    brand_button(din_workspace_card, "GC-Workspace: DIN SPEC 91521  →",
                 open_dinspec_workspace, theme=theme).pack(anchor="w")
    ttk.Label(
        din_workspace_card,
        text=("Drei Bestimmungen über die MS-Fläche und einen gewählten "
              "Standard."),
        style="PageText.TLabel",
        wraplength=900, justify="left").pack(anchor="w", pady=(4, 0))

    din_card = ttk.LabelFrame(din_content, text="1  Dreifachbestimmung verarbeiten", style="Card.TLabelframe")
    din_card.pack(fill=tk.X, pady=(0, 12))
    din_input_var = tk.StringVar()
    din_output_var = tk.StringVar()
    din_is_var = tk.StringVar(value="")
    din_status_var = tk.StringVar(value="Bitte eine Arbeitsmappe mit den Arbeitsblättern _A, _B und _C auswählen.")
    mace_input_var = tk.StringVar()
    mace_norm_pdf_var = tk.StringVar(value=str(_din_norm_default_path()))
    mace_report_output_var = tk.StringVar()
    mace_status_var = tk.StringVar(value="Bitte die reportfähige Excel-Datei auswählen, z. B. die Vorlage mit Sheet1.")

    def din_pick_input() -> None:
        selected = filedialog.askopenfilename(parent=root, title="DIN-SPEC-Eingabedatei auswählen", filetypes=[("Excel-Arbeitsmappen", "*.xlsx *.xlsm"), ("Alle Dateien", "*.*")])
        if selected:
            input_path = Path(selected)
            din_input_var.set(str(input_path))
            din_output_var.set(str(input_path.with_name(f"{input_path.stem}_gemittelt.xlsx")))
            din_status_var.set("Eingabedatei gewählt. Ausgabepfad prüfen und Verarbeitung starten.")

    def din_pick_output() -> None:
        initial = Path(din_output_var.get()) if din_output_var.get() else Path.home() / "DIN_SPEC_91521_Auswertung.xlsx"
        selected = filedialog.asksaveasfilename(parent=root, title="DIN-SPEC-Ausgabedatei speichern", initialdir=str(initial.parent), initialfile=initial.name, defaultextension=".xlsx", filetypes=[("Excel-Arbeitsmappe", "*.xlsx")])
        if selected:
            din_output_var.set(selected)

    is_frame = ttk.Frame(din_card, style="Card.TFrame")
    is_frame.pack(fill=tk.X, padx=14, pady=(12, 4))
    ttk.Label(is_frame, text="Interner Standard für alle Substanzen:", style="Card.TLabel").pack(side=tk.LEFT)
    din_is_combo = ttk.Combobox(is_frame, textvariable=din_is_var, values=("IS1", "IS2", "IS3"), state="readonly", width=8)
    din_is_combo.pack(side=tk.LEFT, padx=(10, 0))
    ttk.Label(is_frame, text="c(IS) = 10.166,67 µg/L; RF = 1,0; Grenzwert = 100 µg/L", style="Muted.TLabel").pack(side=tk.LEFT, padx=(12, 0))

    # Independent MACE workflow. This input is deliberately separate from the
    # triplicate-processing input because a report-ready workbook has a different structure.
    mace_card = ttk.LabelFrame(din_content, text="2  MACE-Report erstellen", style="Card.TLabelframe")
    mace_card.pack(fill=tk.X, pady=(0, 12))
    ttk.Label(
        mace_card,
        text="Eigener Workflow für eine bereits reportfähige Excel-Datei. Unterstützt Sheet1 sowie das erzeugte Blatt Nur_3_von_3. "
             "Ausgeblendete Zeilen und interne Standards werden ignoriert.",
        style="Muted.TLabel", wraplength=900,
    ).pack(anchor="w", padx=12, pady=(10, 8))

    def mace_pick_input() -> None:
        selected = filedialog.askopenfilename(
            parent=root, title="Excel-Datei für MACE-Report auswählen",
            filetypes=[("Excel-Arbeitsmappen", "*.xlsx *.xlsm"), ("Alle Dateien", "*.*")],
        )
        if selected:
            input_path = Path(selected)
            mace_input_var.set(str(input_path))
            mace_report_output_var.set(str(input_path.with_name(f"{input_path.stem}_MACE_Report.docx")))
            mace_status_var.set("MACE-Eingabedatei gewählt. DIN-PDF und Word-Ausgabe prüfen.")

    def mace_pick_norm_pdf() -> None:
        selected = filedialog.askopenfilename(
            parent=root, title="DIN SPEC 91521 PDF auswählen",
            filetypes=[("PDF-Dokument", "*.pdf"), ("Alle Dateien", "*.*")],
        )
        if selected:
            mace_norm_pdf_var.set(selected)

    def mace_pick_report_output() -> None:
        initial = Path(mace_report_output_var.get()) if mace_report_output_var.get() else Path.home() / "DIN_SPEC_91521_MACE_Report.docx"
        selected = filedialog.asksaveasfilename(
            parent=root, title="MACE-Word-Report speichern", initialdir=str(initial.parent),
            initialfile=initial.name, defaultextension=".docx",
            filetypes=[("Word-Dokument", "*.docx")],
        )
        if selected:
            mace_report_output_var.set(selected)

    for label, variable, picker, button_text in (
        ("MACE Excel", mace_input_var, mace_pick_input, "Excel laden"),
        ("DIN PDF", mace_norm_pdf_var, mace_pick_norm_pdf, "Norm auswählen"),
        ("Word-Report", mace_report_output_var, mace_pick_report_output, "Speicherort"),
    ):
        report_row = ttk.Frame(mace_card, style="Surface.TFrame")
        report_row.pack(fill=tk.X, padx=12, pady=4)
        ttk.Label(report_row, text=label, style="Body.TLabel", width=12).pack(side=tk.LEFT)
        ttk.Entry(report_row, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(report_row, text=button_text, command=picker, style="Secondary.TButton").pack(side=tk.RIGHT)

    def din_run() -> None:
        try:
            input_path = Path(din_input_var.get())
            output_path = Path(din_output_var.get())
            if not input_path.is_file():
                raise FileNotFoundError("Bitte eine vorhandene Excel-Eingabedatei auswählen.")
            if not output_path.name:
                raise ValueError("Bitte einen Ausgabepfad festlegen.")
            din_process_button.configure(state="disabled")
            din_status_var.set("Verarbeitung läuft: Arbeitsblätter prüfen, Peaks zuordnen und Ergebnis schreiben …")
            root.update_idletasks()
            selected_is = din_is_var.get().strip().upper()
            if selected_is not in {"IS1", "IS2", "IS3"}:
                raise ValueError("Bitte den internen Standard IS1, IS2 oder IS3 auswählen.")
            result_info = process_din_spec_workbook(input_path, output_path, selected_is=selected_is)
            excluded = result_info.get("excluded_replicates", [])
            qc_text = (
                " | AUSGESCHLOSSEN: " + ", ".join(excluded)
                if excluded else " | Replikat-QC: OK"
            )
            din_status_var.set(
                f"Fertig: {result_info['clusters']} Cluster, "
                f"{result_info['complete_accepted']} vollständige Cluster{qc_text}."
            )
            messagebox.showinfo(
                "DIN SPEC 91521",
                "Auswertung erfolgreich erstellt:\n\n"
                f"{result_info['output']}\n\n"
                f"Rohpeaks: {result_info['raw']}\n"
                f"Cluster: {result_info['clusters']}\n"
                f"Interner Standard: {result_info['selected_is']}\n"
                f"Ausgefiltert (< 100 µg/L): {result_info['below_reporting_limit']}\n"
                f"Interne Standards im Ergebnis: {result_info['internal_standard_rows']}\n"
                f"Vollständige Ergebnis-Cluster: {result_info['complete_accepted']}\n"
                f"Kohlenwasserstoff-Cluster: {result_info['hydrocarbon_3of3']}\n\n"
                f"{result_info['qc_alert']}",
                parent=root,
            )
        except Exception as exc:
            din_status_var.set("Fehler bei der Verarbeitung.")
            messagebox.showerror("DIN SPEC 91521 – Fehler", str(exc), parent=root)
        finally:
            din_process_button.configure(state="normal")

    def din_open_output() -> None:
        path = Path(din_output_var.get())
        if not path.is_file():
            messagebox.showwarning("DIN SPEC 91521", "Es wurde noch keine Ausgabedatei erstellt.", parent=root)
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Datei öffnen", str(exc), parent=root)

    def mace_run() -> None:
        try:
            input_path = Path(mace_input_var.get())
            norm_pdf = Path(mace_norm_pdf_var.get())
            report_path = Path(mace_report_output_var.get())
            if not input_path.is_file():
                raise FileNotFoundError("Bitte eine vorhandene Excel-Datei für den MACE-Report auswählen.")
            if not norm_pdf.is_file():
                raise FileNotFoundError("Bitte die DIN SPEC 91521 PDF auswählen.")
            if not report_path.name:
                report_path = input_path.with_name(f"{input_path.stem}_MACE_Report.docx")
                mace_report_output_var.set(str(report_path))
            if input_path.resolve() == report_path.resolve():
                raise ValueError("Excel-Eingabe und Word-Ausgabe dürfen nicht identisch sein.")
            mace_process_button.configure(state="disabled")
            mace_status_var.set("MACE-Report wird erstellt: sichtbare Zeilen prüfen, Annex C auslesen und Ampel berechnen …")
            root.update_idletasks()
            info = create_din_mace_word_report(input_path, norm_pdf, report_path)
            totals = info["counts"]
            mace_status_var.set(
                f"Fertig: {info['rows']} sichtbare Stoffzeilen | "
                f"A: {totals['A']['confirm']} grün, {totals['A']['not_confirm']} rot, {totals['A']['unknown']} gelb."
            )
            messagebox.showinfo(
                "DIN SPEC 91521 - MACE-Report",
                "MACE-Word-Report erfolgreich erstellt:\n\n"
                f"{info['output']}\n\n"
                f"Ausgewertete sichtbare Zeilen: {info['rows']}\n"
                f"Quelle: {info['source_sheet']}\n"
                f"Level A: {totals['A']['confirm']} CONFIRM, {totals['A']['not_confirm']} NOT CONFIRM, {totals['A']['unknown']} UNKNOWN\n"
                f"Level B: {totals['B']['confirm']} CONFIRM, {totals['B']['not_confirm']} NOT CONFIRM, {totals['B']['unknown']} UNKNOWN\n"
                f"Level C: {totals['C']['confirm']} CONFIRM, {totals['C']['not_confirm']} NOT CONFIRM, {totals['C']['unknown']} UNKNOWN",
                parent=root,
            )
        except Exception as exc:
            mace_status_var.set("Fehler bei der MACE-Report-Erstellung.")
            messagebox.showerror("DIN SPEC 91521 – MACE-Report Fehler", str(exc), parent=root)
        finally:
            mace_process_button.configure(state="normal")

    def mace_open_report() -> None:
        path = Path(mace_report_output_var.get())
        if not path.is_file():
            messagebox.showwarning("DIN SPEC 91521", "Es wurde noch kein MACE-Word-Report erstellt.", parent=root)
            return
        try:
            if os.name == "nt": os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess; subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Report öffnen", str(exc), parent=root)

    for label, variable, picker, button_text in (
        ("Eingabe", din_input_var, din_pick_input, "Excel laden"),
        ("Ausgabe", din_output_var, din_pick_output, "Speicherort"),
    ):
        din_row = ttk.Frame(din_card, style="Surface.TFrame")
        din_row.pack(fill=tk.X, pady=5)
        ttk.Label(din_row, text=label, style="Body.TLabel", width=10).pack(side=tk.LEFT)
        ttk.Entry(din_row, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(din_row, text=button_text, command=picker, style="Secondary.TButton").pack(side=tk.RIGHT)

    ttk.Label(din_card, text="Erwartet: drei Tabellenblätter mit Endung _A, _B, _C und den Spalten RT, Area, Area%, Name, CAS # und SI.", style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
    din_actions = ttk.Frame(din_content, style="App.TFrame")
    din_actions.pack(fill=tk.X, pady=(4, 10))
    # Identical primary action styling and wording as on the NIAS page.
    din_process_button = brand_button(
        din_actions, "Dreifachbestimmung verarbeiten", din_run, theme=theme)
    din_process_button.pack(side=tk.LEFT)
    ttk.Button(din_actions, text="Excel-Ergebnis öffnen", command=din_open_output, style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
    ttk.Label(din_content, textvariable=din_status_var, style="Ready.TLabel", wraplength=900).pack(anchor="w", pady=(6, 10))

    mace_actions = ttk.Frame(din_content, style="App.TFrame")
    mace_actions.pack(fill=tk.X, pady=(0, 8))
    mace_process_button = brand_button(
        mace_actions, "MACE-Report erstellen", mace_run, theme=theme)
    mace_process_button.pack(side=tk.LEFT)
    ttk.Button(mace_actions, text="MACE-Report öffnen", command=mace_open_report, style="Secondary.TButton").pack(side=tk.LEFT, padx=8)
    ttk.Label(din_content, textvariable=mace_status_var, style="Ready.TLabel", wraplength=900).pack(anchor="w", pady=(2, 0))

    din_info = ttk.LabelFrame(din_content, text="Erzeugte Auswertung", style="Card.TLabelframe")
    din_info.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
    ttk.Label(din_info, text="Werkzeug 1 erzeugt die DIN-Excel-Auswertung aus den drei Rohdatenblättern _A, _B und _C. Werkzeug 2 erzeugt unabhängig davon den portraitorientierten MACE-Word-Report aus einer reportfähigen Excel-Datei. Keine Quelldatei wird überschrieben.", style="Body.TLabel", wraplength=790, justify="left").pack(anchor="w")
    ttk.Label(din_info, text="Die Verarbeitung nutzt RT-Matching (0,035 min), CAS-/Namenskonsens, Offline-Kohlenwasserstofferkennung und die 25-%-Prüfung der Gesamt-Area je Replikat. Auffällige Replikate werden gemeldet und aus der Auswertung ausgeschlossen.", style="Muted.TLabel", wraplength=790, justify="left").pack(anchor="w", pady=(8, 0))

    # ------------------------------------------------------------------
    # Page 4: Unknown Register
    # ------------------------------------------------------------------
    ttk.Label(unknown_content, text="Unknown Register",
              style="PageTitle.TLabel").pack(anchor="w", pady=(0, 3))
    ttk.Label(
        unknown_content,
        text=("Wissensbasis aller nicht identifizierten Substanzen. Jede Auswertung legt ihre "
              "Unknowns automatisch hier ab. Die Reihenfolge der m/z-Werte ist die "
              "Intensitätsrangfolge: der erste Wert ist der Base Peak."),
        style="PageText.TLabel",
        wraplength=980, justify="left").pack(anchor="w", pady=(0, 10))

    unknown_state: dict[str, Any] = {
        "register": None, "rows": [], "selected": None,
        "compare": None, "sort": None, "sort_reverse": False,
        "collisions": [], "spectrum": None,
    }

    search_card = ttk.LabelFrame(unknown_content, text="Suche", style="Card.TLabelframe")
    search_card.pack(fill=tk.X, pady=(0, 8))
    search_row = ttk.Frame(search_card, style="Surface.TFrame")
    search_row.pack(fill=tk.X)
    unknown_query_var = tk.StringVar()
    unknown_tolerance_var = tk.BooleanVar(value=False)
    query_entry = ttk.Entry(search_row, textvariable=unknown_query_var, font=("Segoe UI", 10))
    query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
    ttk.Checkbutton(search_row, text="m/z ±1", variable=unknown_tolerance_var,
                    command=lambda: refresh_unknown_register()).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(search_row, text="Suchen", command=lambda: refresh_unknown_register(),
               style="Secondary.TButton").pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(search_row, text="Zurücksetzen",
               command=lambda: (unknown_query_var.set(""), refresh_unknown_register()),
               style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))
    ttk.Label(
        search_card,
        text=("43/57 · 43,57 · 43 57 (enthält immer alle angegebenen Ionen) · "
              "bp:91 (Base Peak) · -149 (enthält nicht) · rt:12.34 · rt:12.0-12.6 · syn:12345678 · "
              "status:offen · n>=3 · conc>0.1 · klasse:siloxan · freier Text"),
        style="Muted.TLabel", wraplength=980, justify="left").pack(anchor="w", pady=(6, 0))

    unknown_panes = ttk.PanedWindow(unknown_content, orient=tk.HORIZONTAL)
    unknown_panes.pack(fill=tk.BOTH, expand=True)

    list_pane = ttk.Frame(unknown_panes, style="Surface.TFrame")
    detail_pane = ttk.Frame(unknown_panes, style="Surface.TFrame")
    unknown_panes.add(list_pane, weight=3)
    unknown_panes.add(detail_pane, weight=2)

    UNKNOWN_TREE_COLUMNS = (
        ("id", "ID", 68, "w"),
        ("mz", "m/z (Rangfolge)", 165, "w"),
        ("bp", "BP", 46, "center"),
        ("rt", "RT ⌀", 68, "center"),
        ("n", "n", 36, "center"),
        ("samples", "Proben", 56, "center"),
        ("conc", "max mg/kg", 82, "center"),
        ("hint", "Klassen-Hinweis (Verdacht)", 190, "w"),
        ("series", "Serie", 105, "w"),
        ("cluster", "Cluster", 62, "center"),
        ("status", "Status", 92, "w"),
        ("score", "Score", 60, "center"),
    )
    unknown_tree = ttk.Treeview(
        list_pane, columns=[key for key, _, _, _ in UNKNOWN_TREE_COLUMNS],
        show="headings", selectmode="browse", height=18)
    for key, heading, width, anchor in UNKNOWN_TREE_COLUMNS:
        unknown_tree.heading(key, text=heading,
                             command=lambda name=key: sort_unknown_rows(name))
        unknown_tree.column(key, width=width, minwidth=36, anchor=anchor,
                            stretch=(key in {"mz", "hint"}))
    unknown_scroll = ttk.Scrollbar(list_pane, orient="vertical", command=unknown_tree.yview)
    unknown_tree.configure(yscrollcommand=unknown_scroll.set)
    list_pane.rowconfigure(0, weight=1)
    list_pane.columnconfigure(0, weight=1)
    unknown_tree.grid(row=0, column=0, sticky="nsew")
    unknown_scroll.grid(row=0, column=1, sticky="ns")
    unknown_xscroll = ttk.Scrollbar(list_pane, orient="horizontal", command=unknown_tree.xview)
    unknown_xscroll.grid(row=1, column=0, sticky="ew")
    unknown_tree.configure(xscrollcommand=unknown_xscroll.set)
    # An unknown whose ranking was lost by the old register must be visibly
    # different from one whose base peak is confirmed.
    def paint_unknown_tags(colors: dict[str, str]) -> None:
        if not unknown_tree.winfo_exists():
            raise tk.TclError("unknown tree gone")
        unknown_tree.tag_configure("rank_unknown", foreground=colors["muted"])
        unknown_tree.tag_configure("identified", foreground=colors["success"])

    theme.on_change(paint_unknown_tags)
    unknown_tree_keys: dict[str, str] = {}

    detail_tabs = ttk.Notebook(detail_pane)
    detail_tabs.pack(fill=tk.BOTH, expand=True)
    details_tab = ttk.Frame(detail_tabs, style="Surface.TFrame", padding=10)
    sightings_tab = ttk.Frame(detail_tabs, style="Surface.TFrame", padding=10)
    similar_tab = ttk.Frame(detail_tabs, style="Surface.TFrame", padding=10)
    spectrum_tab = ttk.Frame(detail_tabs, style="Surface.TFrame", padding=10)
    detail_tabs.add(details_tab, text="Details")
    detail_tabs.add(sightings_tab, text="Sichtungen")
    detail_tabs.add(similar_tab, text="Ähnliche Unknowns")
    detail_tabs.add(spectrum_tab, text="Spektrum")

    # --- Details ------------------------------------------------------
    from gc_register_ui import copyable_text, enable_tree_copy
    unknown_headline_var = tk.StringVar(value="Kein Eintrag gewählt")
    theme.register(copyable_text(details_tab, unknown_headline_var, height=2, bold=True),
                   bg="surface", fg="text").pack(fill=tk.X, pady=(0, 6))
    unknown_facts_var = tk.StringVar(value="")
    theme.register(copyable_text(details_tab, unknown_facts_var, height=9),
                   bg="surface", fg="text").pack(fill=tk.BOTH, expand=True, pady=(0, 4))
    unknown_hint_var = tk.StringVar(value="")
    theme.register(copyable_text(details_tab, unknown_hint_var, height=3),
                   bg="surface", fg="muted").pack(fill=tk.X, pady=(0, 10))

    ttk.Separator(details_tab, orient="horizontal").pack(fill=tk.X, pady=(0, 10))
    ttk.Label(details_tab, text="Bewertung durch den Auswerter", style="Body.TLabel",
              font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 6))

    edit_grid = ttk.Frame(details_tab, style="Surface.TFrame")
    edit_grid.pack(fill=tk.X)
    edit_grid.columnconfigure(1, weight=1)
    unknown_status_var = tk.StringVar(value=UNKNOWN_STATUS_CHOICES[0])
    unknown_name_var = tk.StringVar()
    unknown_cas_var = tk.StringVar()
    ttk.Label(edit_grid, text="Status", style="Body.TLabel").grid(row=0, column=0, sticky="w", pady=3)
    status_box = ttk.Combobox(edit_grid, textvariable=unknown_status_var, state="readonly",
                              values=list(UNKNOWN_STATUS_CHOICES))
    status_box.grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))
    ttk.Label(edit_grid, text="Zugeordneter Name", style="Body.TLabel").grid(row=1, column=0, sticky="w", pady=3)
    unknown_name_entry = ttk.Entry(edit_grid, textvariable=unknown_name_var)
    unknown_name_entry.grid(row=1, column=1, sticky="ew", pady=3, padx=(8, 0))
    ttk.Label(edit_grid, text="CAS-No.", style="Body.TLabel").grid(row=2, column=0, sticky="w", pady=3)
    unknown_cas_entry = ttk.Entry(edit_grid, textvariable=unknown_cas_var)
    unknown_cas_entry.grid(row=2, column=1, sticky="ew", pady=3, padx=(8, 0))
    ttk.Label(details_tab, text="Notiz", style="Body.TLabel").pack(anchor="w", pady=(8, 3))
    unknown_note_text = tk.Text(details_tab, height=5, wrap="word", font=("Segoe UI", 9),
                                relief="solid", borderwidth=1, highlightthickness=0)
    unknown_note_text.pack(fill=tk.X)
    save_row = ttk.Frame(details_tab, style="Surface.TFrame")
    save_row.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(save_row, text="Bewertung speichern", command=lambda: save_unknown_details(),
               style="Secondary.TButton").pack(side=tk.LEFT)
    unknown_save_var = tk.StringVar(value="")
    ttk.Label(save_row, textvariable=unknown_save_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=(10, 0))

    # --- Sightings ----------------------------------------------------
    SIGHTING_COLUMNS = (
        ("sample", "Probe", 90, "w"), ("sample_name", "Probenname", 150, "w"),
        ("date", "Datum", 80, "center"), ("rt", "RT", 60, "center"),
        ("conc", "mg/kg", 65, "center"), ("area", "Area %", 60, "center"),
        ("simulant", "Simulans", 100, "w"), ("temperature", "Temp.", 60, "center"),
        ("report", "Report", 110, "w"),
    )
    sighting_tree = ttk.Treeview(
        sightings_tab, columns=[key for key, _, _, _ in SIGHTING_COLUMNS],
        show="headings", selectmode="browse", height=14)
    for key, heading, width, anchor in SIGHTING_COLUMNS:
        sighting_tree.heading(key, text=heading)
        sighting_tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=False)
    sighting_scroll = ttk.Scrollbar(sightings_tab, orient="vertical", command=sighting_tree.yview)
    sighting_tree.configure(yscrollcommand=sighting_scroll.set)
    sightings_tab.rowconfigure(0, weight=1)
    sightings_tab.columnconfigure(0, weight=1)
    sighting_tree.grid(row=0, column=0, sticky="nsew")
    sighting_scroll.grid(row=0, column=1, sticky="ns")
    sighting_xscroll = ttk.Scrollbar(sightings_tab, orient="horizontal", command=sighting_tree.xview)
    sighting_xscroll.grid(row=1, column=0, sticky="ew")
    sighting_tree.configure(xscrollcommand=sighting_xscroll.set)
    sighting_sources: dict[str, str] = {}

    # --- Similar unknowns ---------------------------------------------
    ttk.Label(
        similar_tab,
        text=("Gesamt (0–1) ist ein Vergleichshinweis, keine Identitätswahrscheinlichkeit. "
              "Spektral (0–1000) und Ionenüberlappung bilden die Grundlage. RT-Abstand "
              "kann diese abschwächen; gemeinsame Proben geben einen anteiligen Bonus. "
              "Ohne gemeinsame Ionen bleibt Gesamt 0. Basis zeigt Messintensitäten, "
              "Rangmodell oder nur Ionen bei unbekannter Rangfolge."),
        style="Muted.TLabel", wraplength=430, justify="left").pack(anchor="w", pady=(0, 8))
    SIMILAR_COLUMNS = (
        ("id", "ID", 66, "w"), ("mz", "m/z (Rangfolge)", 140, "w"),
        ("total", "Gesamt", 60, "center"), ("spectral", "Spektral", 68, "center"),
        ("ions", "Ionen", 70, "center"), ("drt", "ΔRT", 60, "center"),
        ("bp", "BP", 42, "center"), ("homologue", "Homolog", 120, "w"),
        ("shared", "gem. Proben", 80, "center"), ("status", "Status", 90, "w"),
        ("basis", "Basis", 120, "w"),
    )
    similar_tree = ttk.Treeview(
        similar_tab, columns=[key for key, _, _, _ in SIMILAR_COLUMNS],
        show="headings", selectmode="browse", height=12)
    for key, heading, width, anchor in SIMILAR_COLUMNS:
        similar_tree.heading(key, text=heading)
        similar_tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=False)
    similar_scroll = ttk.Scrollbar(similar_tab, orient="vertical", command=similar_tree.yview)
    similar_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    similar_tree.configure(yscrollcommand=similar_scroll.set)
    similar_tree.pack(fill=tk.BOTH, expand=True)
    similar_xscroll = ttk.Scrollbar(similar_tab, orient="horizontal", command=similar_tree.xview)
    similar_xscroll.pack(fill=tk.X)
    similar_tree.configure(xscrollcommand=similar_xscroll.set)
    similar_keys: dict[str, str] = {}
    similar_actions = ttk.Frame(similar_tab, style="Surface.TFrame")
    similar_actions.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(similar_actions, text="Im Spektrum vergleichen",
               command=lambda: compare_selected_unknown(), style="Secondary.TButton").pack(side=tk.LEFT)
    ttk.Button(similar_actions, text="Als gleiche Substanz verknüpfen",
               command=lambda: link_selected_unknown(), style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(similar_actions, text="Dorthin springen",
               command=lambda: jump_to_similar_unknown(), style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))

    # --- Spectrum ------------------------------------------------------
    # The stored spectra of the selected entry. Until this list existed the tab
    # drew one spectrum with no way of saying which one it was, and an entry
    # seen in six samples has six of them.
    UNKNOWN_SPECTRA_COLUMNS = (
        ("created", "Gespeichert", 88, "w"),
        ("sample", "Probe", 140, "w"),
        ("report_type", "Bericht", 90, "w"),
        ("rt", "RT", 60, "e"),
        ("n_ions", "Ionen", 52, "e"),
        ("base_peak_mz", "BP", 50, "e"),
        ("kind", "Art", 84, "w"),
    )
    unknown_spectra_tree = ttk.Treeview(
        spectrum_tab, columns=[key for key, _, _, _ in UNKNOWN_SPECTRA_COLUMNS],
        show="headings", selectmode="browse", height=5)
    for key, heading, width, anchor in UNKNOWN_SPECTRA_COLUMNS:
        unknown_spectra_tree.heading(key, text=heading)
        unknown_spectra_tree.column(key, width=width, minwidth=40, anchor=anchor,
                                    stretch=(key == "sample"))
    # The row the tab preselects has to be recognisable as such: without it the
    # analyst cannot tell a deliberate choice from the list's own order.
    unknown_spectra_tree.tag_configure("best", font=("Segoe UI", 9, "bold"))
    unknown_spectra_tree.pack(fill=tk.X, pady=(0, 6))
    unknown_spectra_rows: list[dict[str, Any]] = []

    spectra_actions = ttk.Frame(spectrum_tab, style="Surface.TFrame")
    atlas_actions = ttk.Frame(spectrum_tab, style="Surface.TFrame")
    atlas_actions.pack(fill=tk.X, pady=(0, 6))
    spectra_actions.pack(fill=tk.X, pady=(0, 8))
    ttk.Button(atlas_actions, text="EI Atlas …",
               command=lambda: search_unknown_with_atlas(),
               style="Secondary.TButton").pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(atlas_actions, text="EI-Verlauf …",
               command=lambda: unknown_atlas_history(),
               style="Secondary.TButton").pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(spectra_actions, text="Mit NIST suchen",
               command=lambda: search_unknown_with_nist(),
               style="Secondary.TButton").pack(side=tk.LEFT)
    ttk.Button(spectra_actions, text="Spektrum als MSP speichern …",
               command=lambda: save_selected_unknown_msp(),
               style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))

    unknown_spectrum_var = tk.StringVar(value="")
    ttk.Label(spectrum_tab, textvariable=unknown_spectrum_var, style="Body.TLabel",
              wraplength=430, justify="left").pack(anchor="w", pady=(0, 6))
    # Die Bildunterschrift haengt davon ab, ob das SQLite-Register fuer den
    # gezeigten Eintrag ein gemessenes Spektrum hat; sie wird in
    # draw_unknown_spectrum() gesetzt.
    unknown_spectrum_hint_var = tk.StringVar(value=SPECTRUM_HINT_MODELLED)
    ttk.Label(
        spectrum_tab, textvariable=unknown_spectrum_hint_var,
        style="Muted.TLabel", wraplength=430, justify="left").pack(anchor="w", pady=(0, 8))
    spectrum_canvas = theme.register(
        tk.Canvas(spectrum_tab, height=290, highlightthickness=1),
        background="surface", highlightbackground="border")
    spectrum_canvas.pack(fill=tk.BOTH, expand=True)
    unknown_library_var = tk.StringVar(value="")
    ttk.Label(spectrum_tab, textvariable=unknown_library_var, style="Muted.TLabel",
              wraplength=430, justify="left").pack(anchor="w", pady=(6, 0))

    # --- Footer --------------------------------------------------------
    unknown_footer = ttk.Frame(unknown_content, style="App.TFrame", padding=(0, 10, 0, 4))
    unknown_footer.pack(fill=tk.X)
    ttk.Button(unknown_footer, text="Excel-Export öffnen",
               command=lambda: open_unknown_export(), style="Secondary.TButton").pack(side=tk.LEFT)
    ttk.Button(unknown_footer, text="Cluster neu berechnen",
               command=lambda: recompute_unknown_clusters(), style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(unknown_footer, text="Gemeinsamen Datenordner wählen",
               command=lambda: choose_shared_data_dir(), style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(unknown_footer, text="Altes Register importieren",
               command=lambda: import_legacy_register(), style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(unknown_footer, text="JSON-Sicherung schreiben",
               command=lambda: write_register_backup(), style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))
    ttk.Button(unknown_footer, text="Aktualisieren",
               command=lambda: refresh_unknown_register(), style="Secondary.TButton").pack(side=tk.LEFT, padx=(6, 0))
    unknown_status_line = tk.StringVar(value="Register noch nicht geladen.")
    ttk.Label(unknown_content, textvariable=unknown_status_line, style="Ready.TLabel",
              wraplength=980, justify="left").pack(anchor="w", pady=(0, 8))

    def unknown_tolerance() -> int:
        return 1 if unknown_tolerance_var.get() else 0

    def load_register_or_report() -> Optional[dict[str, Any]]:
        try:
            return load_unknown_register_adopting_legacy()
        except Exception as exc:  # noqa: BLE001 - reported to the analyst
            # An unmigrated register is its own case: the message names the two
            # commands to run, so it must not be filed under "not readable".
            migration = getattr(gc_register_module(), "MigrationRequired", None)
            if migration is not None and isinstance(exc, migration):
                messagebox.showwarning("Migration ausstehend", str(exc), parent=root)
            else:
                messagebox.showerror("Register nicht lesbar", str(exc), parent=root)
            return None

    def format_number(value: Any, decimals: int = 4) -> str:
        number = numeric_value(value)
        return "" if number is None else f"{number:.{decimals}f}"

    def unknown_row_values(entry: dict[str, Any],
                           similarity: Optional[dict[str, Any]]) -> tuple:
        ranked = entry_ranked_mz(entry)
        return (
            entry.get("id", ""),
            format_mz_list(ranked[:5]) + (" (Rang?)" if entry.get("rank_unknown") else ""),
            "" if entry.get("base_peak") is None else entry["base_peak"],
            format_number(entry.get("rt_mean"), 3),
            entry.get("n_sightings", 0),
            entry.get("n_samples", 0),
            format_number(entry.get("conc_max"), 4),
            ", ".join(entry.get("class_hint") or ()),
            text(entry.get("homologue_series")),
            text(entry.get("cluster")),
            text(entry.get("status")),
            "" if similarity is None else f"{similarity['total']:.3f}",
        )

    def fill_unknown_tree() -> None:
        unknown_tree.delete(*unknown_tree.get_children())
        unknown_tree_keys.clear()
        for key, entry, similarity in unknown_state["rows"]:
            tags = []
            if entry.get("rank_unknown"):
                tags.append("rank_unknown")
            if text(entry.get("status")) == "identifiziert":
                tags.append("identified")
            item = unknown_tree.insert("", "end", values=unknown_row_values(entry, similarity),
                                       tags=tuple(tags))
            unknown_tree_keys[item] = key

    def apply_unknown_sort() -> None:
        column = unknown_state["sort"]
        if not column:
            return
        index = [key for key, _, _, _ in UNKNOWN_TREE_COLUMNS].index(column)

        def sort_key(row):
            value = unknown_row_values(row[1], row[2])[index]
            number = numeric_value(value)
            # Numeric columns must not sort as text; empty values stay last either way.
            return (value == "", number if number is not None else 0.0, text(value).casefold())

        unknown_state["rows"].sort(key=sort_key, reverse=unknown_state["sort_reverse"])

    def sort_unknown_rows(column: str) -> None:
        if unknown_state["sort"] == column:
            unknown_state["sort_reverse"] = not unknown_state["sort_reverse"]
        else:
            unknown_state["sort"] = column
            # Counts, concentrations and scores are read highest first.
            unknown_state["sort_reverse"] = column in {"n", "samples", "conc", "score"}
        apply_unknown_sort()
        fill_unknown_tree()

    def refresh_unknown_register(select_first: bool = False) -> None:
        register = load_register_or_report()
        if register is None:
            return
        unknown_state["register"] = register
        try:
            unknown_state["rows"] = search_unknown_register(
                register, unknown_query_var.get(), unknown_tolerance())
        except Exception as exc:  # noqa: BLE001 - a bad query must not kill the tab
            messagebox.showwarning("Suche nicht auswertbar", str(exc), parent=root)
            unknown_state["rows"] = []
        apply_unknown_sort()
        fill_unknown_tree()

        previous = unknown_state.get("selected")
        items = list(unknown_tree_keys)
        target = next((item for item in items if unknown_tree_keys[item] == previous), None)
        if target is None and items and (select_first or previous is None):
            target = items[0]
        if target:
            unknown_tree.selection_set(target)
            unknown_tree.see(target)
        else:
            unknown_state["selected"] = None
            show_unknown_details(None)

        entries = register.get("unknowns") or {}
        total_sightings = sum(len(entry.get("sightings") or ()) for entry in entries.values())
        open_count = sum(1 for entry in entries.values()
                         if text(entry.get("status")) == "offen")
        directory, shared, note = unknown_register_location()
        # Identity is the strongest four masses, so two entries can answer to
        # one key. That is shown, never resolved automatically: merging two
        # entries would merge two substances nobody agreed to merge.
        collisions = unknown_identity_collisions(register)
        unknown_state["collisions"] = collisions
        collision_note = ""
        if collisions:
            affected = sum(len(item["ids"]) for item in collisions)
            collision_note = (
                f"  ·  ⚠ {len(collisions)} Identitätsschlüssel von je mehreren "
                f"Einträgen belegt ({affected} Einträge) - eine neue Sichtung "
                "wird gegen alle davon geprüft")
        unknown_status_line.set(
            f"{len(unknown_state['rows'])} von {len(entries)} Unknowns angezeigt  ·  "
            f"{total_sightings} Sichtungen  ·  {open_count} offen  ·  "
            f"{note}  ·  {directory}{collision_note}")
        refresh_msp_libraries()

    def refresh_msp_libraries() -> None:
        """Rewrite the NIST libraries if the register has moved on since.

        The one place they are written. Opening the tab and pressing
        "Aktualisieren" both land here, so the libraries also pick up spectra
        the GC workspace filed in the meantime. An unwritable register folder
        costs this one status line and nothing else.
        """
        info = refresh_unknown_msp_libraries()
        if info.get("error"):
            unknown_library_var.set(
                f"MSP-Bibliothek nicht geschrieben: {info['error']}. "
                "Die Registeranzeige ist davon nicht betroffen.")
            return
        written = text(info.get("written_at")).replace("T", " ")[:16]
        skipped = int(info.get("skipped") or 0)
        parts = [f"MSP-Bibliothek: {info.get('measured', 0)} gemessen",
                 f"{info.get('modelled', 0)} modelliert"]
        if skipped:
            parts.append(f"{skipped} ohne Rangfolge übersprungen")
        if written:
            parts.append(f"geschrieben {written}")
        unknown_library_var.set(" · ".join(parts))

    def selected_unknown_entry() -> Optional[dict[str, Any]]:
        register = unknown_state.get("register") or {}
        key = unknown_state.get("selected")
        return (register.get("unknowns") or {}).get(key) if key else None

    def show_unknown_details(key: Optional[str]) -> None:
        register = unknown_state.get("register") or {}
        entry = (register.get("unknowns") or {}).get(key) if key else None
        sighting_tree.delete(*sighting_tree.get_children())
        sighting_sources.clear()
        similar_tree.delete(*similar_tree.get_children())
        similar_keys.clear()
        unknown_state["compare"] = None
        unknown_save_var.set("")

        if entry is None:
            unknown_headline_var.set("Kein Eintrag gewählt")
            unknown_facts_var.set("")
            unknown_hint_var.set("")
            unknown_status_var.set(UNKNOWN_STATUS_CHOICES[0])
            unknown_name_var.set("")
            unknown_cas_var.set("")
            unknown_note_text.delete("1.0", tk.END)
            unknown_spectrum_var.set("")
            fill_unknown_spectra_list(None)
            spectrum_canvas.delete("all")
            return

        ranked = entry_ranked_mz(entry)
        unknown_headline_var.set(f"{entry.get('id', '')}   m/z {format_mz_list(ranked)}")
        facts = [
            "Base Peak: " + ("unbekannt (aus Altregister)" if entry.get("rank_unknown")
                             else str(entry.get("base_peak"))),
            f"Sichtungen: {entry.get('n_sightings', 0)} in {entry.get('n_samples', 0)} Probe(n)",
            # rt_min/rt_max are written together with rt_mean by
            # recompute_unknown_entry, so they are present for every entry the
            # report path built. An entry that arrived any other way must not
            # take the whole tab down for a missing span.
            "RT: " + (f"{entry['rt_mean']:.4f} min (SD {entry.get('rt_sd') or 0:.4f}"
                      + (f", {entry['rt_min']:.4f}-{entry['rt_max']:.4f})"
                         if entry.get("rt_min") is not None
                         and entry.get("rt_max") is not None else ")")
                      if entry.get("rt_mean") is not None else "nicht erfasst"),
            "Konzentration: " + (f"max {entry['conc_max']:.4f} mg/kg in Probe "
                                 f"{entry.get('conc_max_sample')}"
                                 if entry.get("conc_max") is not None else "nicht erfasst"),
        ]
        if entry.get("ttc_flag"):
            facts.append("TTC-Relevanz: " + entry["ttc_flag"])
        if entry.get("simulants"):
            facts.append("Simulanzien: " + ", ".join(entry["simulants"]))
        if entry.get("materials"):
            facts.append("Materialien: " + ", ".join(entry["materials"]))
        if entry.get("cluster"):
            facts.append("Cluster: " + entry["cluster"])
        if entry.get("first_seen"):
            facts.append(f"Erstmals {entry['first_seen']}, zuletzt {entry.get('last_seen')}")
        unknown_facts_var.set("\n".join(facts))

        hints = []
        if entry.get("class_hint"):
            hints.append("Verdacht (keine Identifizierung): " + ", ".join(entry["class_hint"]))
        if entry.get("homologue_series"):
            hints.append("Homologe Leiter im Spektrum: " + entry["homologue_series"])
        if entry.get("rank_unknown"):
            hints.append("Aus dem alten Register übernommen: die Intensitätsrangfolge "
                         "ging dort verloren und wird beim nächsten echten Fund ergänzt.")
        if entry.get("rt_sd") and entry["rt_sd"] > UNKNOWN_RT_TOLERANCE:
            hints.append(f"Die RT streut um {entry['rt_sd']:.4f} min - stärker als das "
                         "Suchfenster. Möglicherweise verbergen sich hinter dieser "
                         "Signatur mehrere Substanzen.")
        for collision in unknown_state.get("collisions") or ():
            if entry.get("id") not in collision["ids"]:
                continue
            partners = [i for i in collision["ids"] if i != entry.get("id")]
            leads = collision["winner"] == entry.get("id")
            hints.append(
                f"Identitätsschlüssel m/z {collision['identity_key']} wird auch von "
                f"{', '.join(partners)} belegt. Neue Sichtungen mit dieser Signatur "
                + ("landen bei diesem Eintrag."
                   if leads else f"landen bei {collision['winner']}, nicht hier.")
                + " Der Schlüssel sind die stärksten vier Massen; ob es wirklich "
                "dieselbe Substanz ist, entscheidet die Auswertung, nicht das "
                "Programm.")
        unknown_hint_var.set("\n".join(hints))

        unknown_status_var.set(text(entry.get("status")) or UNKNOWN_STATUS_CHOICES[0])
        unknown_name_var.set(text(entry.get("assigned_name")))
        unknown_cas_var.set(text(entry.get("assigned_cas")))
        unknown_note_text.delete("1.0", tk.END)
        unknown_note_text.insert("1.0", text(entry.get("note")))

        for sighting in sorted(entry.get("sightings") or (),
                               key=lambda item: text(item.get("sample"))):
            item_id = sighting_tree.insert("", "end", values=(
                text(sighting.get("sample")), text(sighting.get("sample_name")),
                text(sighting.get("date")), format_number(sighting.get("rt"), 4),
                format_number(sighting.get("conc_kg"), 4),
                format_number(sighting.get("area_pct"), 2),
                text(sighting.get("simulant")), text(sighting.get("temperature")),
                text(sighting.get("report_type")),
            ))
            sighting_sources[item_id] = text(sighting.get("source_file"))

        for other_key, similarity in rank_similar_unknowns(
                register, key, unknown_tolerance(), minimum=0.05)[:60]:
            other = register["unknowns"][other_key]
            item_id = similar_tree.insert("", "end", values=(
                other.get("id", ""), format_mz_list(entry_ranked_mz(other)[:5]),
                f"{similarity['total']:.3f}", f"{similarity['spectral']:.0f}" if similarity['spectral_available'] else "—",
                f"{similarity['shared_count']} ({similarity['ions_jaccard']:.2f})",
                "" if similarity["delta_rt"] is None else f"{similarity['delta_rt']:.3f}",
                "ja" if similarity["base_peak_match"] else "",
                similarity["homologue"], similarity["shared_samples"],
                text(other.get("status")), similarity["basis"],
            ))
            similar_keys[item_id] = other_key

        fill_unknown_spectra_list(entry)
        draw_unknown_spectrum()

    def fill_unknown_spectra_list(entry: Optional[dict[str, Any]]) -> None:
        """List the stored spectra of one entry and preselect the best.

        Best means most ions - the same order gc_register picks by, so the
        list, the drawing and the MSP library never disagree about which
        spectrum represents an entry.
        """
        unknown_spectra_tree.delete(*unknown_spectra_tree.get_children())
        unknown_spectra_rows.clear()
        unknown_state["spectrum"] = None
        rows = entry_spectrum_rows(entry)
        unknown_spectra_rows.extend(rows)
        for index, row in enumerate(rows):
            kind = text(row.get("kind"))
            unknown_spectra_tree.insert("", "end", iid=str(index), values=(
                text(row.get("created_at"))[:10],
                text(row.get("sample") or row.get("sample_name")),
                text(row.get("report_type")),
                format_de(row.get("rt"), 3),
                "" if row.get("n_ions") is None else str(int(row["n_ions"])),
                "" if row.get("base_peak_mz") is None
                else str(int(row["base_peak_mz"])),
                SPECTRUM_KIND_LABELS.get(kind, kind),
            ))
        best = best_spectrum_row_index(rows)
        if best >= 0:
            unknown_spectra_tree.item(str(best), tags=("best",))
            unknown_spectra_tree.selection_set(str(best))
            unknown_spectra_tree.see(str(best))

    def selected_unknown_spectrum() -> tuple[list, Optional[dict[str, Any]]]:
        """The picked spectrum as measured, and its row. ``([], None)`` if none.

        Loaded on demand and remembered: the canvas redraws on every
        <Configure> event, and the register may live on a share.
        """
        selection = unknown_spectra_tree.selection()
        if not selection:
            return [], None
        try:
            row = unknown_spectra_rows[int(selection[0])]
        except (ValueError, IndexError):
            return [], None
        spectrum_id = int(row.get("spectrum_id") or 0)
        cached = unknown_state.get("spectrum") or {}
        if cached.get("id") != spectrum_id:
            cached = {"id": spectrum_id, "raw": load_entry_spectrum(spectrum_id)}
            unknown_state["spectrum"] = cached
        return cached["raw"], row

    def on_unknown_spectrum_select(_event=None) -> None:
        draw_unknown_spectrum()

    unknown_spectra_tree.bind("<<TreeviewSelect>>", on_unknown_spectrum_select)

    def warn_without_measured_spectrum(entry: dict[str, Any], title: str) -> None:
        """Say why nothing happens - once, for both actions that need one."""
        messagebox.showinfo(
            title,
            f"Zu {text(entry.get('id'))} ist kein gemessenes Spektrum "
            "gespeichert. Gemessene Spektren entstehen im GC-Workspace über "
            "„Substanz + Spektrum im Register speichern“.\n\n"
            "Ohne echte Intensitäten wird nichts übergeben: eine Suche über das "
            "modellierte Rangspektrum wäre eine Suche über erfundene Werte.",
            parent=root)

    def ask_nist_fallback(message: str) -> Optional[str]:
        """Offer the two ways out of a failed handoff. "ordner" | "msp" | None."""
        dialog = tk.Toplevel(root)
        dialog.title("NIST MS Search nicht gefunden")
        dialog.transient(root)
        dialog.resizable(False, False)
        choice: dict[str, Optional[str]] = {"value": None}

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=message, wraplength=520,
                  justify="left").pack(anchor="w")

        def pick(value: Optional[str]) -> None:
            choice["value"] = value
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=(14, 0, 14, 14))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Abbrechen",
                   command=lambda: pick(None)).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Als MSP speichern …",
                   command=lambda: pick("msp")).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(buttons, text="Ordner wählen …",
                   command=lambda: pick("ordner")).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Escape>", lambda _event: pick(None))
        dialog.protocol("WM_DELETE_WINDOW", lambda: pick(None))
        try:
            dialog.grab_set()
        except tk.TclError:  # pragma: no cover - headless
            pass
        root.wait_window(dialog)
        return choice["value"]

    def search_unknown_with_atlas() -> None:
        import gc_atlas
        entry = selected_unknown_entry()
        if entry is None:
            return
        spectrum, row = selected_unknown_spectrum()
        if not spectrum or not row:
            warn_without_measured_spectrum(entry, "EI Atlas")
            return
        gc_atlas.open_research(root,
            dict(spectrum=spectrum, name=unknown_msp_name(entry), rt=row.get('rt'),
                 bg_scan=row.get('bg_scan'), deconvoluted=row.get('kind') == 'component'),
            unknown_register_db_path(), entry_id=gc_register_module()._entry_number(entry.get('id'), 0),
            spectrum_id=int(row['spectrum_id']), context=dict(row, source_file=row.get('source_path', '')),
            on_saved=lambda _record: refresh_unknown_register())

    def unknown_atlas_history() -> None:
        import gc_atlas
        entry = selected_unknown_entry()
        if entry is not None:
            gc_atlas.show_history(root, unknown_register_db_path(),
                                 gc_register_module()._entry_number(entry.get('id'), 0),
                                 on_saved=lambda _record: refresh_unknown_register())

    def search_unknown_with_nist() -> None:
        """Hand the selected stored spectrum to NIST MS Search."""
        entry = selected_unknown_entry()
        if entry is None:
            return
        spectrum, row = selected_unknown_spectrum()
        if not spectrum:
            warn_without_measured_spectrum(entry, "Mit NIST suchen")
            return
        try:
            nist = gc_nist_module()
        except Exception as exc:  # noqa: BLE001 - module missing beside the script
            messagebox.showerror("Mit NIST suchen", str(exc), parent=root)
            return
        rt = numeric_value((row or {}).get("rt"))
        if rt is None:
            rt = numeric_value(entry.get("rt_mean"))
        name = unknown_msp_name(entry)
        # Two attempts at most: the second one runs after the analyst has named
        # the MSSEARCH folder, and a second failure is a real failure.
        for attempt in (1, 2):
            try:
                unknown_status_line.set(nist.search_spectrum(spectrum, name, rt))
                return
            except nist.NistError as exc:
                if attempt == 2:
                    messagebox.showerror("Mit NIST suchen", str(exc), parent=root)
                    return
                choice = ask_nist_fallback(str(exc))
                if choice == "ordner":
                    folder = filedialog.askdirectory(
                        parent=root, title="MSSEARCH-Ordner von NIST MS Search wählen")
                    if not folder:
                        return
                    nist.remember_mssearch_dir(folder)
                    try:
                        nist.installation(refresh=True)
                    except Exception:  # noqa: BLE001 - the retry reports it
                        pass
                    continue
                if choice == "msp":
                    save_selected_unknown_msp()
                return
            except Exception as exc:  # noqa: BLE001 - never a traceback in the UI
                messagebox.showerror(
                    "Mit NIST suchen",
                    f"Die Übergabe an NIST MS Search ist fehlgeschlagen:\n{exc}",
                    parent=root)
                return

    def save_selected_unknown_msp() -> None:
        """Write the selected stored spectrum as a single MSP file."""
        entry = selected_unknown_entry()
        if entry is None:
            return
        spectrum, row = selected_unknown_spectrum()
        if not spectrum:
            warn_without_measured_spectrum(entry, "Spektrum als MSP speichern")
            return
        try:
            nist = gc_nist_module()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Spektrum als MSP speichern", str(exc), parent=root)
            return
        name = unknown_msp_name(entry)
        keep = "".join(char if char.isalnum() or char in " _-." else "_"
                       for char in name).strip()
        path = filedialog.asksaveasfilename(
            parent=root, title="Spektrum als MSP speichern", defaultextension=".msp",
            initialfile=f"{keep or 'Spektrum'}.msp",
            filetypes=[("NIST MSP", "*.msp"), ("Alle Dateien", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(
                nist.msp_text(spectrum, name, None,
                              comment=unknown_msp_comment(entry, row),
                              cas=text(entry.get("assigned_cas"))),
                encoding=nist.NIST_ENCODING, errors="replace", newline="")
        except OSError as exc:
            messagebox.showerror(
                "Spektrum als MSP speichern",
                f"Die Datei konnte nicht geschrieben werden:\n{exc}", parent=root)
            return
        unknown_status_line.set(f"MSP geschrieben: {path}")

    def draw_unknown_spectrum() -> None:
        """Draw the spectrum head-to-tail against a comparison entry.

        The measured spectrum from the SQLite register is used where one
        exists; entries without one keep the modelled rank pseudo spectrum.
        """
        spectrum_canvas.delete("all")
        entry = selected_unknown_entry()
        if entry is None:
            return
        register = unknown_state.get("register") or {}
        other = (register.get("unknowns") or {}).get(unknown_state.get("compare"))
        top = entry_ranked_mz(entry)
        bottom = entry_ranked_mz(other) if other else ()
        if not top:
            return
        # The top trace is the spectrum picked in the list; the comparison entry
        # below is described by its best one, since nobody picked for it.
        picked, _row = selected_unknown_spectrum()
        top_measured = as_relative_spectrum(picked) if picked else None
        bottom_measured = measured_entry_spectrum(other) if other else None
        if top_measured and (bottom_measured or not bottom):
            unknown_spectrum_hint_var.set(SPECTRUM_HINT_MEASURED)
        elif top_measured or bottom_measured:
            unknown_spectrum_hint_var.set(SPECTRUM_HINT_MIXED)
        else:
            unknown_spectrum_hint_var.set(SPECTRUM_HINT_MODELLED)

        width = max(spectrum_canvas.winfo_width(), 380)
        height = max(spectrum_canvas.winfo_height(), 260)
        left, right = 42, width - 14
        middle = height / 2 if bottom else height - 30
        span = (height / 2 - 26) if bottom else (height - 56)

        masses = sorted(
            set(top) | set(bottom)
            | {int(round(mass)) for mass, _ in (top_measured or ())}
            | {int(round(mass)) for mass, _ in (bottom_measured or ())})
        low = min(masses) - 8
        high = max(masses) + 8
        scale = (right - left) / max(1, high - low)

        spectrum_canvas.create_line(left, middle, right, middle, fill=C["border"])
        shared = set(top) & set(bottom)

        def draw(series, direction, color, measured=None):
            weights = (dict(measured) if measured else
                       {int(mass): rank_pseudo_intensity(rank)
                        for rank, mass in enumerate(series, start=1)})
            # A measured spectrum carries far too many ions to label them
            # all; only the ones that are actually readable get a mass.
            label_floor = 5.0 if measured else 0.0
            for mass, intensity in weights.items():
                x = left + (mass - low) * scale
                bar = span * intensity / 100.0
                y = middle - bar * direction
                highlight = int(round(mass)) in shared and bottom
                spectrum_canvas.create_line(
                    x, middle, x, y, width=3 if highlight else 2,
                    fill=C["success"] if highlight else color)
                if intensity >= label_floor:
                    spectrum_canvas.create_text(
                        x, y - 9 * direction, text=str(int(round(mass))),
                        fill=C["text"], font=("Segoe UI", 7))

        draw(top, 1, C["brand"], top_measured)
        spectrum_canvas.create_text(
            left, 12, anchor="w", font=("Segoe UI", 8, "bold"), fill=C["brand"],
            text=f"{entry.get('id', '')}  m/z {format_mz_list(top)}"
                 + ("  (gemessen)" if top_measured else "  (modelliert)"))
        if bottom:
            draw(bottom, -1, C["info"], bottom_measured)
            spectrum_canvas.create_text(
                left, height - 10, anchor="w", font=("Segoe UI", 8, "bold"),
                fill=C["info"],
                text=f"{other.get('id', '')}  m/z {format_mz_list(bottom)}"
                     + ("  (gemessen)" if bottom_measured else "  (modelliert)"))
            similarity = unknown_entry_similarity(entry, other, unknown_tolerance())
            unknown_spectrum_var.set(
                f"Vergleich {entry.get('id')} gegen {other.get('id')}: "
                f"Gesamt {similarity['total']:.3f}, Spektral {similarity['spectral']:.0f}/1000, "
                f"{similarity['shared_count']} gemeinsame Ionen"
                + (f", ΔRT {similarity['delta_rt']:.3f} min" if similarity["delta_rt"] is not None else "")
                + (f", {similarity['homologue']}" if similarity["homologue"] else ""))
        else:
            unknown_spectrum_var.set(
                f"{entry.get('id', '')} - für einen Head-to-Tail-Vergleich einen Eintrag "
                "unter \"Ähnliche Unknowns\" wählen.")

    def on_unknown_tree_select(_event=None) -> None:
        selection = unknown_tree.selection()
        unknown_state["selected"] = unknown_tree_keys.get(selection[0]) if selection else None
        show_unknown_details(unknown_state["selected"])

    def compare_selected_unknown() -> None:
        selection = similar_tree.selection()
        if not selection:
            messagebox.showinfo("Kein Vergleich gewählt",
                                "Bitte einen ähnlichen Unknown in der Liste auswählen.",
                                parent=root)
            return
        unknown_state["compare"] = similar_keys.get(selection[0])
        draw_unknown_spectrum()
        detail_tabs.select(spectrum_tab)

    def jump_to_similar_unknown() -> None:
        selection = similar_tree.selection()
        if not selection:
            return
        target = similar_keys.get(selection[0])
        for item, key in unknown_tree_keys.items():
            if key == target:
                unknown_tree.selection_set(item)
                unknown_tree.see(item)
                return
        # The related entry is filtered out by the current query: clear it first.
        unknown_query_var.set("")
        unknown_state["selected"] = target
        refresh_unknown_register()

    def open_sighting_source(_event=None) -> None:
        selection = sighting_tree.selection()
        source = sighting_sources.get(selection[0]) if selection else ""
        if not source:
            return
        path = Path(source)
        if not path.exists():
            messagebox.showwarning("Datei nicht gefunden",
                                   f"Die Quelldatei existiert nicht mehr:\n{path}", parent=root)
            return
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]

    unknown_tree.bind("<<TreeviewSelect>>", on_unknown_tree_select)
    def edit_selected_unknown():
        # Synchronise immediately: a context-menu command can precede the
        # queued TreeviewSelect event for the row just right-clicked.
        on_unknown_tree_select()
        if selected_unknown_entry():
            detail_tabs.select(details_tab)
            unknown_name_entry.focus_set()
            unknown_name_entry.selection_range(0, tk.END)

    enable_tree_copy(unknown_tree, edit=edit_selected_unknown,
                     extra=(("EI-Untersuchungsverlauf …", lambda: (on_unknown_tree_select(), unknown_atlas_history())),))
    enable_tree_copy(similar_tree)
    enable_tree_copy(sighting_tree)
    enable_tree_copy(unknown_spectra_tree)
    similar_tree.bind("<Double-1>", lambda _event: jump_to_similar_unknown())
    similar_tree.bind("<Return>", lambda _event: jump_to_similar_unknown())
    sighting_tree.bind("<Double-1>", open_sighting_source)
    sighting_tree.bind("<Return>", open_sighting_source)
    query_entry.bind("<Return>", lambda _event: refresh_unknown_register())
    query_entry.bind("<KP_Enter>", lambda _event: refresh_unknown_register())
    for widget in (status_box, unknown_name_entry, unknown_cas_entry):
        widget.bind("<Return>", lambda _event: (save_unknown_details(), "break")[-1])
        widget.bind("<KP_Enter>", lambda _event: (save_unknown_details(), "break")[-1])
    unknown_note_text.bind("<Control-Return>", lambda _event: (save_unknown_details(), "break")[-1])
    # Dragging a window edge sends a burst of <Configure> events; redraw once
    # the size has settled instead of once per pixel.
    spectrum_resize_job: dict[str, Optional[str]] = {"id": None}

    def on_spectrum_resize(_event=None) -> None:
        if spectrum_resize_job["id"] is not None:
            spectrum_canvas.after_cancel(spectrum_resize_job["id"])
        spectrum_resize_job["id"] = spectrum_canvas.after(50, redraw_after_resize)

    def redraw_after_resize() -> None:
        spectrum_resize_job["id"] = None
        draw_unknown_spectrum()

    spectrum_canvas.bind("<Configure>", on_spectrum_resize)
    # Built once: a menu created per right-click is never freed.
    atlas_menu = tk.Menu(root, tearoff=0)
    atlas_menu.add_command(label="Mit EI Atlas untersuchen …", command=search_unknown_with_atlas)
    atlas_menu.add_command(label="EI-Untersuchungsverlauf …", command=unknown_atlas_history)

    def atlas_spectrum_menu(event):
        try:
            atlas_menu.tk_popup(event.x_root, event.y_root)
        finally:
            atlas_menu.grab_release()
    spectrum_canvas.bind("<Button-3>", atlas_spectrum_menu)

    def redraw_spectrum_for_theme(_colors: dict[str, str]) -> None:
        if not spectrum_canvas.winfo_exists():
            raise tk.TclError("spectrum canvas gone")
        draw_unknown_spectrum()

    theme.on_change(redraw_spectrum_for_theme)

    def update_selected_unknown(mutate, success: str) -> None:
        """Apply a change to one entry in one transaction, then reload.

        The register is re-read inside the transaction rather than reusing what
        the tab is showing, so an edit made elsewhere in the meantime is not
        overwritten with a stale copy.
        """
        key = unknown_state.get("selected")
        if not key:
            messagebox.showinfo("Kein Eintrag gewählt",
                                "Bitte zuerst einen Unknown auswählen.", parent=root)
            return
        try:
            module = gc_register_module()
            with unknown_register_connection(write=True) as connection:
                with module.writing(connection):
                    register = module.as_register_dict(connection)
                    entry = (register.get("unknowns") or {}).get(key)
                    if entry is None:
                        raise RuntimeError(
                            "Der Eintrag ist im Register nicht mehr vorhanden.")
                    mutate(entry, register)
                    module.save_register_dict(connection, register)
        except Exception as exc:  # noqa: BLE001 - reported to the analyst
            messagebox.showerror("Speichern fehlgeschlagen", str(exc), parent=root)
            return
        refresh_unknown_register()
        unknown_save_var.set(success)

    def save_unknown_details() -> None:
        status = unknown_status_var.get()
        name = unknown_name_var.get().strip()
        cas = unknown_cas_var.get().strip()
        note = unknown_note_text.get("1.0", tk.END).strip()
        if status == "identifiziert" and not name:
            messagebox.showwarning(
                "Name fehlt",
                "Für den Status \"identifiziert\" wird der zugeordnete Substanzname "
                "benötigt - sonst kann der Treffer bei künftigen Auswertungen nicht "
                "gemeldet werden.", parent=root)
            return

        def mutate(entry, _register):
            entry["status"] = status
            entry["assigned_name"] = name
            entry["assigned_cas"] = cas
            entry["note"] = note

        update_selected_unknown(mutate, "Gespeichert.")

    def link_selected_unknown() -> None:
        selection = similar_tree.selection()
        if not selection:
            messagebox.showinfo("Kein Partner gewählt",
                                "Bitte einen ähnlichen Unknown auswählen.", parent=root)
            return
        target = similar_keys.get(selection[0])
        register = unknown_state.get("register") or {}
        other = (register.get("unknowns") or {}).get(target)
        if other is None:
            return
        if not messagebox.askyesno(
                "Als gleiche Substanz verknüpfen",
                f"Diesen Eintrag mit {other.get('id')} (m/z "
                f"{format_mz_list(entry_ranked_mz(other))}) als dieselbe Substanz "
                "verknüpfen?\n\nDie Einträge bleiben getrennt bestehen; die "
                "Verknüpfung wird nur vermerkt.", parent=root):
            return

        def mutate(entry, _register):
            entry["linked_to"] = other.get("id")

        update_selected_unknown(mutate, f"Mit {other.get('id')} verknüpft.")

    def recompute_unknown_clusters() -> None:
        try:
            module = gc_register_module()
            with unknown_register_connection(write=True) as connection:
                with module.writing(connection):
                    register = module.as_register_dict(connection)
                    for entry in (register.get("unknowns") or {}).values():
                        recompute_unknown_entry(entry)
                    count = compute_unknown_clusters(register)
                    module.save_register_dict(connection, register)
        except Exception as exc:  # noqa: BLE001 - reported to the analyst
            messagebox.showerror("Neuberechnung fehlgeschlagen", str(exc), parent=root)
            return
        refresh_unknown_register()
        messagebox.showinfo("Cluster neu berechnet",
                            f"{count} Cluster gebildet.", parent=root)

    def open_unknown_export() -> None:
        try:
            open_unknown_documentation()
        except Exception as exc:  # noqa: BLE001 - reported to the analyst
            messagebox.showerror(
                "Export konnte nicht geöffnet werden",
                "Der Excel-Export des Unknown-Registers konnte nicht erzeugt oder "
                f"geöffnet werden.\n\nDetails: {exc}", parent=root)

    def write_register_backup() -> None:
        """Write the SQLite register out as a dated JSON snapshot.

        The JSON is no longer written on every report run, so a snapshot has to
        be asked for. It is what ``gc_register.py --verify`` reads and what a
        rollback restores from. It does not contain the measured spectra - those
        live only in the SQLite file, which has to be copied separately.
        """
        try:
            path = write_unknown_register_json_backup()
        except Exception as exc:  # noqa: BLE001 - reported to the analyst
            messagebox.showerror(
                "Sicherung fehlgeschlagen",
                f"Die JSON-Sicherung konnte nicht geschrieben werden.\n\n"
                f"Details: {exc}", parent=root)
            return
        messagebox.showinfo(
            "Sicherung geschrieben",
            f"Das Register wurde als JSON gesichert:\n{path}\n\n"
            "Die gemessenen Spektren sind darin nicht enthalten - für eine "
            "vollständige Sicherung zusätzlich die Datei "
            f"{unknown_register_db_path().name} kopieren.", parent=root)

    def import_legacy_register() -> None:
        selected = filedialog.askopenfilename(
            parent=root, title="Altes Unknown-Register auswählen",
            filetypes=[("Excel-Dateien", "*.xlsx *.xlsm"), ("Alle Dateien", "*.*")])
        if not selected:
            return
        path = Path(selected)
        if not is_legacy_unknown_documentation(path):
            messagebox.showwarning(
                "Kein Altregister",
                "Die Datei hat nicht den Aufbau des alten Registers "
                "(Unknown Substanz | Syneris Nummer | Datum).", parent=root)
            return
        try:
            module = gc_register_module()
            with unknown_register_connection(write=True) as connection:
                with module.writing(connection):
                    register = module.as_register_dict(connection)
                    result = import_legacy_unknown_documentation(path, register)
                    module.save_register_dict(connection, register)
        except Exception as exc:  # noqa: BLE001 - reported to the analyst
            messagebox.showerror("Import fehlgeschlagen", str(exc), parent=root)
            return
        refresh_unknown_register(select_first=True)
        messagebox.showinfo(
            "Altregister übernommen",
            f"{result['created']} neue Unknowns, {result['imported']} Sichtungen "
            "übernommen.\n\nDas alte Register hat die m/z-Werte sortiert gespeichert, "
            "die Intensitätsrangfolge ist dort verloren. Diese Einträge sind als "
            "\"Rang?\" gekennzeichnet und werden beim nächsten echten Fund ergänzt.",
            parent=root)

    # ------------------------------------------------------------------
    # 5. Options - everything that applies to the whole program
    # ------------------------------------------------------------------
    # Three settings used to live in three different places: the shared folder
    # behind a button on the register page, the standard CAS reference in the
    # report options, the theme in the title bar. None of them belongs to one
    # workflow, and an analyst setting up a second workstation had to find all
    # three. They are here now, in the order they matter for a team: where the
    # data lives, which reference it is read against, how the window looks, how
    # much of it is shown.
    ttk.Label(options_content, text="Optionen",
              style="PageTitle.TLabel").pack(anchor="w", pady=(0, 3))
    ttk.Label(
        options_content,
        text=("Programmweite Einstellungen. Der gemeinsame Datenordner und die "
              "CAS-Referenz gelten für alle Auswertungen dieses Arbeitsplatzes; "
              "damit ein Team dieselben Daten sieht, muss auf jedem Arbeitsplatz "
              "derselbe Ordner eingetragen sein."),
        style="PageText.TLabel", wraplength=900, justify="left").pack(
            anchor="w", pady=(0, 14))

    options_cards = ttk.Frame(options_content, style="App.TFrame")
    options_cards.pack(fill=tk.X)
    options_cards.columnconfigure(0, weight=1, uniform="optioncards")
    options_cards.columnconfigure(1, weight=1, uniform="optioncards")

    shared_card = ttk.LabelFrame(options_cards, text="1  Gemeinsame Daten",
                                 style="Card.TLabelframe")
    shared_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
    cas_card = ttk.LabelFrame(options_cards, text="2  CAS-Referenz (Standard)",
                              style="Card.TLabelframe")
    cas_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
    appearance_card = ttk.LabelFrame(options_cards, text="3  Darstellung",
                                     style="Card.TLabelframe")
    appearance_card.grid(row=1, column=0, sticky="nsew", padx=(0, 5), pady=(10, 0))
    mode_card = ttk.LabelFrame(options_cards, text="4  Arbeitsmodus",
                               style="Card.TLabelframe")
    mode_card.grid(row=1, column=1, sticky="nsew", padx=(5, 0), pady=(10, 0))

    shared_dir_var = tk.StringVar()
    shared_state_var = tk.StringVar()
    options_cas_var = tk.StringVar()

    ttk.Label(shared_card, text="Ordner", style="Body.TLabel",
              font=("Segoe UI", 9, "bold")).pack(anchor="w")
    ttk.Entry(shared_card, textvariable=shared_dir_var,
              state="readonly").pack(fill=tk.X, pady=(2, 4))
    ttk.Label(shared_card, textvariable=shared_state_var, style="Muted.TLabel",
              wraplength=430).pack(anchor="w", pady=(0, 8))
    shared_buttons = ttk.Frame(shared_card, style="Surface.TFrame")
    shared_buttons.pack(fill=tk.X, pady=(0, 8))
    ttk.Button(shared_buttons, text="Ordner wählen",
               command=lambda: choose_shared_data_dir(),
               style="Secondary.TButton").pack(side=tk.LEFT)
    ttk.Button(shared_buttons, text="Ordner öffnen",
               command=lambda: gc_open_path(shared_data_dir()),
               style="Secondary.TButton").pack(side=tk.LEFT, padx=(8, 0))
    ttk.Label(
        shared_card,
        text=("Hier liegen: unknown_register.sqlite (Register und Sichtungen), "
              "CASINFO.xlsx, ladders.json (Alkanreihen), unknowns.msp und "
              "unknowns_modelliert.msp (NIST-Bibliotheken) sowie "
              "Unknown_Dokumentation.xlsx (Excel-Export).\n"
              "Ist der Ordner nicht erreichbar, arbeitet das Programm lokal "
              "weiter, statt Daten zu verlieren - der Zustand oben sagt, was "
              "gerade gilt."),
        style="Muted.TLabel", wraplength=430, justify="left").pack(anchor="w")

    ttk.Label(cas_card, text="Standarddatei", style="Body.TLabel",
              font=("Segoe UI", 9, "bold")).pack(anchor="w")
    ttk.Label(cas_card, textvariable=options_cas_var, style="Muted.TLabel",
              wraplength=430).pack(anchor="w", pady=(2, 8))
    ttk.Button(cas_card, text="Standard-CASINFO wählen",
               command=lambda: manage_standard(),
               style="Secondary.TButton").pack(anchor="w")
    ttk.Label(
        cas_card,
        text=("Diese Datei ist die Voreinstellung jeder Auswertung. Liegt eine "
              "CASINFO.xlsx im gemeinsamen Ordner, wird sie beim Waehlen des "
              "Ordners automatisch übernommen.\n"
              "Auf der Seite NIAS Report lässt sich davon für einen einzelnen "
              "Durchlauf abweichen, ohne diese Einstellung zu ändern."),
        style="Muted.TLabel", wraplength=430, justify="left").pack(
            anchor="w", pady=(8, 0))

    # Two segments rather than a checkbox: the current theme is readable at a
    # glance instead of having to be inferred from a tick.
    theme_switch = theme.register(
        tk.Frame(appearance_card, highlightthickness=1),
        bg="surface", highlightbackground="border", highlightcolor="border")
    theme_switch.pack(anchor="w", pady=(2, 8))
    theme_segments: dict[str, tk.Button] = {}

    def paint_theme_switch(colors: dict[str, str]) -> None:
        for name, button in theme_segments.items():
            if not button.winfo_exists():
                raise tk.TclError("theme switch gone")
            selected = name == theme.name
            button.configure(
                bg=colors["brand"] if selected else colors["surface"],
                fg=colors["brand_fg"] if selected else colors["muted"],
                activebackground=colors["brand"] if selected else colors["surface"],
                activeforeground=colors["brand_fg"] if selected else colors["text"])

    for _theme_name, _theme_text in (("light", "Hell"), ("dark", "Dunkel")):
        theme_segments[_theme_name] = tk.Button(
            theme_switch, text=_theme_text,
            command=lambda name=_theme_name: theme.switch(name),
            font=("Segoe UI", 8, "bold"), relief=tk.FLAT, borderwidth=0,
            padx=14, pady=5, cursor="hand2", takefocus=False)
        theme_segments[_theme_name].pack(side=tk.LEFT)
    theme.on_change(paint_theme_switch)
    ttk.Label(
        appearance_card,
        text=("Gilt für das Hauptfenster, den GC-Workspace und alle Dialoge. "
              "Strg+Umschalt+D schaltet ebenfalls um."),
        style="Muted.TLabel", wraplength=430).pack(anchor="w")

    def on_expert_mode_toggled() -> None:
        save_expert_mode(expert_mode_var.get())
        apply_expert_mode()

    ttk.Checkbutton(mode_card, text="Expertenmodus",
                    variable=expert_mode_var,
                    command=on_expert_mode_toggled).pack(anchor="w", pady=(2, 6))
    ttk.Label(
        mode_card,
        text=("Blendet auf der Seite GC-Daten zusätzlich die Reiter "
              "Einzelbestimmung und Fingerprint Screening "
              "ein.\n"
              "Im Standardmodus steht dort nur Doppelbestimmung (Batch) - der "
              "Weg, den die Routineauswertung geht. Die Einstellung wirkt "
              "sofort und gilt auch nach einem Neustart."),
        style="Muted.TLabel", wraplength=430, justify="left").pack(anchor="w")

    def refresh_options() -> None:
        """Show what the settings currently say. Reads, never writes."""
        active, _is_share, note = unknown_register_location()
        shared_dir_var.set(str(active))
        shared_state_var.set(note)
        standard = load_saved_cas_path()
        if standard.is_file():
            options_cas_var.set(
                f"{standard}  -  verfügbar  -  {database_version_text(standard)}")
        else:
            options_cas_var.set(f"{standard}  -  nicht verfügbar")

    def choose_shared_data_dir() -> None:
        """Point the whole program at one folder, and say what that changed.

        The CASINFO is written into the setting rather than resolved through the
        folder at read time: an analyst who picks a folder has to be able to
        read back afterwards which file is in force, and a silent fallback order
        cannot be read back anywhere.
        """
        directory = filedialog.askdirectory(
            parent=root, title="Gemeinsamen Datenordner wählen")
        if not directory:
            return
        try:
            save_user_settings(unknown_register_dir=directory)
        except OSError as exc:
            messagebox.showerror(
                "Einstellung konnte nicht gespeichert werden",
                "Der gemeinsame Ordner konnte nicht gespeichert werden."
                f"\n\nDetails: {exc}", parent=root)
            return
        candidate = Path(directory) / "CASINFO.xlsx"
        if candidate.is_file():
            adopt_standard_cas_path(candidate)
        else:
            messagebox.showinfo(
                "Ordner übernommen",
                f"Der gemeinsame Datenordner ist jetzt:\n{directory}\n\n"
                "Dort liegt keine CASINFO.xlsx. Als Standardreferenz gilt "
                f"weiterhin:\n{load_saved_cas_path()}", parent=root)
        refresh_unknown_register(select_first=True)
        refresh_options()

    refresh_options()

    show_gc_page()

    file_card = ttk.LabelFrame(content, text="1  Eingabedateien", style="Card.TLabelframe")
    file_card.pack(fill=tk.X, expand=False, pady=(0, 10))
    ttk.Label(file_card, text="NIAS-Dateien hinzufügen oder einen Ordner rekursiv durchsuchen. Die Verarbeitung startet erst nach Ihrer Prüfung.", style="Muted.TLabel").pack(anchor="w", pady=(0, 7))

    table_wrap = ttk.Frame(file_card, style="Surface.TFrame")
    table_wrap.pack(fill=tk.X, expand=False)
    columns = ("file", "folder", "type", "status")
    tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="extended", height=4)
    tree.heading("file", text="Datei")
    tree.heading("folder", text="Ordner")
    tree.heading("type", text="Format")
    tree.heading("status", text="Status")
    tree.column("file", width=350, minwidth=220)
    tree.column("folder", width=390, minwidth=220)
    tree.column("type", width=130, anchor="center", stretch=False)
    tree.column("status", width=120, anchor="center", stretch=False)
    ybar = ttk.Scrollbar(table_wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ybar.set)
    tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
    ybar.pack(side=tk.RIGHT, fill=tk.Y)

    selected_paths: dict[str, Path] = {}
    selected_formats: dict[str, str] = {}
    file_count_var = tk.StringVar(value="Keine Dateien ausgewählt")
    readiness_var = tk.StringVar(value="Bitte Eingabedateien hinzufügen.")

    def path_status(path: Path) -> str:
        if not path.exists():
            return "Fehlt"
        if path.name.startswith("~$"):
            return "Temporär"
        if path.stem.casefold().endswith("_processed"):
            return "Verarbeitet"
        return "Bereit"

    def update_readiness() -> None:
        count = len(selected_paths)
        file_count_var.set(f"{count} Datei{'en' if count != 1 else ''} ausgewählt" if count else "Keine Dateien ausgewählt")
        cas = Path(cas_var.get()) if cas_var.get().strip() else None
        formats = list(selected_formats.values())
        cas_free_only = bool(formats) and all(item in CAS_FREE_FORMATS for item in formats)
        requires_cas = bool(formats) and not cas_free_only
        if cas_free_only:
            traffic_summary_var.set(False)
            traffic_summary_check.configure(state=tk.DISABLED)
        else:
            traffic_summary_check.configure(state=tk.NORMAL)
        if not count:
            readiness_var.set("Bitte Eingabedateien hinzufügen.")
            start_button.configure(state=tk.DISABLED)
        elif requires_cas and (cas is None or not cas.is_file()):
            readiness_var.set("CAS-Referenz fehlt oder ist nicht verfügbar.")
            start_button.configure(state=tk.DISABLED)
        else:
            note = (" CAS/SML wird für Fingerprint und Total Extraction nicht verwendet."
                    if cas_free_only else "")
            readiness_var.set(
                f"Bereit zur Verarbeitung von {count} Datei{'en' if count != 1 else ''}.{note}")
            start_button.configure(state=tk.NORMAL)

    def add_paths(paths) -> None:
        for raw in paths:
            path = Path(raw)
            key = str(path.resolve()) if path.exists() else str(path)
            if path.suffix.lower() not in {".xlsx", ".xlsm", ".niasgc"} or key in selected_paths:
                continue
            status = path_status(path)
            if status in {"Temporär", "Verarbeitet"}:
                continue
            source_format = 'GC-Sitzung' if path.suffix.lower() == '.niasgc' else detect_workbook_format(path)
            item = tree.insert("", tk.END, values=(path.name, str(path.parent), source_format, status))
            selected_paths[item] = path
            selected_formats[item] = source_format
        update_readiness()

    def add_files() -> None:
        nonlocal full_auto_root_var
        full_auto_root_var = None
        add_paths(filedialog.askopenfilenames(parent=root, title="NIAS-Dateien / gespeicherte Doppelbestimmungen auswählen", filetypes=[("NIAS-Dateien", "*.xlsx *.xlsm *.niasgc"), ("GC-Sitzungen", "*.niasgc"), ("Excel-Dateien", "*.xlsx *.xlsm")]))

    def scan_folder() -> None:
        nonlocal full_auto_root_var
        selected = filedialog.askdirectory(parent=root, title="Bezugsordner durchsuchen")
        if not selected:
            return
        folder = Path(selected)
        found = sorted(
            (p for p in folder.rglob("*")
             if p.is_file()
             and p.suffix.lower() in {".xlsx", ".xlsm", ".niasgc"}
             and (p.suffix.lower() == '.niasgc' or p.name.casefold().startswith("nias-screening-syn")
                  or p.stem.casefold().endswith("_fingerprint_screening"))
             and not p.stem.casefold().endswith("_processed")
             and not p.name.startswith("~$")),
            key=lambda p: str(p).casefold())
        if not found:
            messagebox.showinfo("Keine NIAS-Dateien gefunden", "Im ausgewählten Ordner wurden keine passenden NIAS-Dateien gefunden.", parent=root)
            return
        full_auto_root_var = folder
        add_paths(found)
        readiness_var.set(f"{len(found)} Datei(en) gefunden. Auswahl prüfen und Verarbeitung starten.")

    def remove_selected() -> None:
        for item in tree.selection():
            selected_paths.pop(item, None)
            selected_formats.pop(item, None)
            tree.delete(item)
        update_readiness()

    def clear_files() -> None:
        nonlocal full_auto_root_var
        full_auto_root_var = None
        for item in tree.get_children():
            tree.delete(item)
        selected_paths.clear()
        selected_formats.clear()
        update_readiness()

    action_row = ttk.Frame(file_card, style="Surface.TFrame")
    action_row.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(action_row, text="Dateien hinzufügen", command=add_files, style="Secondary.TButton").pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(action_row, text="Ordner durchsuchen", command=scan_folder, style="Secondary.TButton").pack(side=tk.LEFT, padx=6)
    ttk.Button(action_row, text="Auswahl entfernen", command=remove_selected, style="Secondary.TButton").pack(side=tk.LEFT, padx=6)
    ttk.Button(action_row, text="Liste leeren", command=clear_files, style="Secondary.TButton").pack(side=tk.LEFT, padx=6)
    ttk.Label(action_row, textvariable=file_count_var, style="Body.TLabel").pack(side=tk.RIGHT)

    options = ttk.Frame(content, style="App.TFrame")
    options.pack(fill=tk.X)
    options.columnconfigure(0, weight=1, uniform="cards")
    options.columnconfigure(1, weight=1, uniform="cards")

    ref_card = ttk.LabelFrame(options, text="2  Referenz und Ausgabe", style="Card.TLabelframe")
    ref_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
    report_card = ttk.LabelFrame(options, text="3  Reportoptionen", style="Card.TLabelframe")
    report_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

    cas_mode_var = tk.StringVar(value="standard")
    cas_var = tk.StringVar(value=str(default_cas_path))
    cas_info_var = tk.StringVar()

    def refresh_cas_info() -> None:
        path = Path(cas_var.get()) if cas_var.get().strip() else None
        if path and path.is_file():
            cas_info_var.set(f"{path}  ·  verfügbar  ·{database_version_text(path)}")
        else:
            cas_info_var.set("Referenzdatei nicht verfügbar")
        update_readiness()

    def choose_alternative() -> None:
        filename = filedialog.askopenfilename(parent=root, title="Alternative CAS-Referenz wählen", filetypes=[("Excel-Dateien", "*.xlsx *.xlsm"), ("Alle Dateien", "*.*")])
        if filename:
            cas_mode_var.set("alternative")
            cas_var.set(filename)
            refresh_cas_info()

    def adopt_standard_cas_path(path: Path) -> bool:
        """Store ``path`` as the standard CAS reference and select it everywhere.

        The report page and the options page write the same setting, so the
        writing half lives here once. Returns whether it was stored.
        """
        nonlocal default_cas_path
        try:
            save_standard_cas_path(path)
        except OSError as exc:
            messagebox.showerror("Einstellung konnte nicht gespeichert werden", f"Der Standardpfad konnte nicht gespeichert werden.\n\nDetails: {exc}", parent=root)
            return False
        default_cas_path = Path(path)
        cas_mode_var.set("standard")
        cas_var.set(str(default_cas_path))
        refresh_cas_info()
        refresh_options()
        return True

    def manage_standard() -> None:
        filename = filedialog.askopenfilename(parent=root, title="Standard-CAS-Referenz festlegen", initialdir=str(default_cas_path.parent) if default_cas_path.parent.exists() else None, filetypes=[("Excel-Dateien", "*.xlsx *.xlsm"), ("Alle Dateien", "*.*")])
        if not filename:
            return
        adopt_standard_cas_path(Path(filename))

    def select_standard() -> None:
        cas_var.set(str(default_cas_path))
        refresh_cas_info()

    ttk.Radiobutton(ref_card, text="Standard-CAS-Referenz", variable=cas_mode_var, value="standard", command=select_standard).pack(anchor="w")
    ttk.Label(ref_card, textvariable=cas_info_var, style="Muted.TLabel", wraplength=430).pack(anchor="w", padx=(22, 0), pady=(0, 4))
    cas_buttons = ttk.Frame(ref_card, style="Surface.TFrame")
    cas_buttons.pack(fill=tk.X, padx=(18, 0), pady=(0, 8))
    ttk.Button(cas_buttons, text="Standard verwalten", command=manage_standard, style="Secondary.TButton").pack(side=tk.LEFT)
    ttk.Radiobutton(ref_card, text="Alternative Referenz für diesen Durchlauf", variable=cas_mode_var, value="alternative", command=choose_alternative).pack(anchor="w")
    ttk.Button(ref_card, text="Alternative auswählen", command=choose_alternative, style="Secondary.TButton").pack(anchor="w", padx=(18, 0), pady=(2, 10))

    output_mode_var = tk.StringVar(value="source")
    outdir_var = tk.StringVar(value="")
    ttk.Label(ref_card, text="Ausgabeort", style="Body.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 2))
    ttk.Radiobutton(ref_card, text="Ergebnisse im jeweiligen Quellordner speichern", variable=output_mode_var, value="source").pack(anchor="w")
    ttk.Radiobutton(ref_card, text="Alle Ergebnisse in ausgewähltem Ordner speichern", variable=output_mode_var, value="custom").pack(anchor="w")
    out_row = ttk.Frame(ref_card, style="Surface.TFrame")
    out_row.pack(fill=tk.X, padx=(18, 0), pady=(3, 0))
    ttk.Label(out_row, textvariable=outdir_var, style="Muted.TLabel").pack(side=tk.LEFT, fill=tk.X, expand=True)
    def choose_outdir() -> None:
        directory = filedialog.askdirectory(parent=root, title="Ausgabeordner auswählen")
        if directory:
            output_mode_var.set("custom")
            outdir_var.set(directory)
    ttk.Button(out_row, text="Ordner wählen", command=choose_outdir, style="Secondary.TButton").pack(side=tk.RIGHT)

    create_word_var = tk.BooleanVar(value=True)
    traffic_summary_var = tk.BooleanVar(value=False)
    open_output_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(report_card, text="Kombinierten Word-Report erstellen", variable=create_word_var).pack(anchor="w", pady=(0, 5))
    traffic_summary_check = ttk.Checkbutton(
        report_card, text="Ampelbewertung und Executive Summary erstellen",
        variable=traffic_summary_var)
    traffic_summary_check.pack(anchor="w", pady=5)
    ttk.Label(report_card, text="Ergänzt Statusspalte und Übersicht für eingehaltene, zu bewertende, überschrittene und nicht identifizierte Substanzen.", style="Muted.TLabel", wraplength=430).pack(anchor="w", padx=(22, 0), pady=(0, 8))
    ttk.Checkbutton(report_card, text="Ausgabeordner nach Abschluss öffnen", variable=open_output_var).pack(anchor="w", pady=5)
    # The two register tools that used to stand here - open the register, export
    # it as Excel - are on the Unknown Register page, which is where an analyst
    # goes to work with the register. Two entrances to one function were one
    # entrance too many.

    footer = ttk.Frame(content, style="App.TFrame", padding=(0, 8, 0, 14))
    footer.pack(fill=tk.X)
    ttk.Label(footer, textvariable=readiness_var, style="Ready.TLabel").pack(side=tk.LEFT)

    def on_process() -> None:
        paths = [selected_paths[item] for item in tree.get_children()]
        if not paths:
            return
        formats = [selected_formats[item] for item in tree.get_children()]
        cas_free_only = bool(formats) and all(item in CAS_FREE_FORMATS for item in formats)
        selected_cas = Path(cas_var.get()) if cas_var.get().strip() else None
        if not cas_free_only and (selected_cas is None or not selected_cas.is_file()):
            messagebox.showwarning("CAS-Referenz fehlt", "Bitte eine verfügbare CAS-Referenz auswählen.", parent=root)
            return
        save_in_source = output_mode_var.get() == "source"
        selected_outdir = None if save_in_source else Path(outdir_var.get()) if outdir_var.get() else None
        if not save_in_source and (selected_outdir is None or not selected_outdir.is_dir()):
            messagebox.showwarning("Ausgabeordner fehlt", "Bitte einen gültigen Ausgabeordner auswählen.", parent=root)
            return
        try:
            ensure_excel_files_closed(paths + ([selected_cas] if selected_cas else []))
        except PermissionError as exc:
            messagebox.showwarning("Excel-Datei ist geöffnet", f"Eine Eingabedatei ist noch in Excel geöffnet.\n\n{exc}\n\nBitte schließen und erneut versuchen.", parent=root)
            return
        session_metadata = {}
        for path in paths:
            if path.suffix.lower() == '.niasgc':
                try:
                    saved = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    messagebox.showerror('Sitzung', str(exc), parent=root)
                    return
                if not saved.get('migration_metadata'):
                    import gc_fid
                    from gc_migration_dialog import collect
                    settings = gc_fid.default_settings()
                    for key, value in saved.get('settings', {}).items():
                        if key in gc_fid.PARAMETER_BOUNDS:
                            gc_fid.apply_setting(settings, key, value)
                    metadata = collect(root, settings)
                    if metadata is None:
                        return
                    session_metadata[str(path)] = metadata
        start_button.configure(state=tk.DISABLED)
        readiness_var.set(f"Verarbeitung von {len(paths)} Datei(en) läuft …")
        root.update_idletasks()
        progress_window = BatchProgressWindow(len(paths), parent=root)
        aborted = ""
        results: list[dict[str, Any]] = []
        failures: list[tuple[Path, str]] = []
        try:
            results, failures, _word_path = run_nias_batch(
                paths,
                selected_cas,
                selected_outdir,
                create_word_var.get(),
                save_in_source,
                full_auto_root_var,
                traffic_summary_var.get(),
                progress=progress_window,
                session_metadata=session_metadata,
            )
        except Exception as exc:
            aborted = str(exc)
            failures = [(path, aborted) for path in paths]
        finally:
            progress_window.close()
            start_button.configure(state=tk.NORMAL)
        if aborted:
            # Reported only after the modal progress window has released its grab.
            messagebox.showerror("Verarbeitung fehlgeschlagen", aborted, parent=root)

        # Mark what has been produced so a second, accidental run is obvious.
        failed_paths = {path for path, _reason in failures}
        for item in tree.get_children():
            path = selected_paths.get(item)
            if path is None or path not in paths:
                continue
            values = list(tree.item(item, "values"))
            values[3] = "Fehler" if path in failed_paths else "Verarbeitet"
            tree.item(item, values=values)

        if results or failures:
            show_result_dashboard(results, failures, parent=root)
        if open_output_var.get() and results:
            gc_open_path(Path(results[0]["output"]).parent)
        update_readiness()

    # A native Tk button is used here because the Windows ``vista`` ttk theme
    # ignores custom foreground colors on push buttons, which made the label
    # unreadable. Explicit colors guarantee white text on Constantia dark red.
    start_button = brand_button(footer, "Verarbeitung starten", on_process,
                                theme=theme)
    start_button.pack(side=tk.RIGHT)

    def quit_app() -> None:
        root.destroy()

    ttk.Button(footer, text="Beenden", command=quit_app, style="Secondary.TButton").pack(side=tk.RIGHT, padx=(0, 8))
    root.protocol("WM_DELETE_WINDOW", quit_app)
    refresh_cas_info()
    bind_mousewheel(shell)
    root.mainloop()

def _excel_color_to_hex(color, default: str = "000000") -> str:
    """Return a six-digit RGB value for an openpyxl color when possible."""
    if color is not None and color.type == "rgb" and color.rgb:
        return color.rgb[-6:]
    return default


WORD_REPORT_FALLBACK_STEM = "NIAS_Results_combined"

# Reserved on Windows in a file name; a trailing dot or space is stripped by the
# shell and would silently rename the report.
WINDOWS_FORBIDDEN_FILENAME_CHARS = r'\/:*?"<>|'
_WINDOWS_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_windows_stem(value: Any, fallback: str = "") -> str:
    """Return ``value`` as a file stem Windows will actually accept.

    Forbidden characters become ``_``; trailing dots and spaces are stripped,
    because Windows drops them and the report would end up under a name nobody
    asked for. An empty result falls back rather than producing ``".docx"``.
    """
    cleaned = _WINDOWS_FORBIDDEN_RE.sub("_", text(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    return cleaned or fallback


def word_report_file_stem(sample_numbers: Sequence[Any],
                          summary_report_no: Any = "") -> str:
    """Choose the Word report's file name per spec v2.1 §V.4.

    More than one sample in the run is a summary and is named after the Syneris
    summary report number; a single sample is named after its own derived
    Syneris number. Neither available keeps the old combined name rather than
    failing a run that has otherwise succeeded.

    ``sample_numbers`` holds one entry per processed workbook, so the two
    determinations of one sample — which carry the same derived number — count
    as the one sample they are.
    """
    cleaned = [text(number).strip() for number in sample_numbers]
    distinct = list(dict.fromkeys(number for number in cleaned if number))
    summary = sanitize_windows_stem(summary_report_no)
    sample_count = len(distinct) if distinct else len(cleaned)
    if sample_count > 1:
        # A per-sample number would be wrong for a multi-sample report, so the
        # summary number is the only alternative to the fallback here.
        return summary or sanitize_windows_stem('_'.join(distinct[:5]) + (f'_und_{len(distinct)-5}_weitere' if len(distinct) > 5 else '')) or WORD_REPORT_FALLBACK_STEM
    single = sanitize_windows_stem(distinct[0]) if distinct else ""
    return single or summary or WORD_REPORT_FALLBACK_STEM


def unique_output_path(folder: Path, stem: str, extension: str = ".docx") -> Path:
    """Return a free path, appending ``_2``, ``_3``, … like the batch export.

    Mirrors ``AutoLib.duplicate_output_paths``: same separator, same start at 2,
    so the workbooks and the Word report never disagree about what a second file
    of the same name is called. A report is never silently overwritten.
    """
    folder = Path(folder)
    candidate = folder / f"{stem}{extension}"
    suffix = 2
    while candidate.exists():
        candidate = folder / f"{stem}_{suffix}{extension}"
        suffix += 1
    return candidate


def create_combined_word(excel_paths: list[Path], word_path: Path) -> Path:
    """Copy all result sheets into one portrait Word document.

    The generated Word tables preserve the visible Excel values, merged cells,
    font emphasis/colors, cell fills, alignment, column proportions and the
    header bottom border as closely as Word's table model allows.
    """
    try:
        from docx import Document
        from docx.enum.section import WD_ORIENT
        from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from docx.shared import Cm, Pt, RGBColor
    except ImportError as exc:
        raise RuntimeError(
            "Für die Word-Ausgabe fehlt python-docx. Installation: pip install python-docx"
        ) from exc

    def add_hyperlink(paragraph, display_text: str, url: str, xcell):
        """Add a clickable external hyperlink formatted as plain black text."""
        relationship_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)

        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), relationship_id)

        run = OxmlElement("w:r")
        run_properties = OxmlElement("w:rPr")

        run_fonts = OxmlElement("w:rFonts")
        font_name = xcell.font.name or "Arial"
        run_fonts.set(qn("w:ascii"), font_name)
        run_fonts.set(qn("w:hAnsi"), font_name)
        run_properties.append(run_fonts)

        font_size = OxmlElement("w:sz")
        font_size.set(qn("w:val"), str(int(round((xcell.font.sz or 8) * 2))))
        run_properties.append(font_size)

        color = OxmlElement("w:color")
        color.set(qn("w:val"), _excel_color_to_hex(xcell.font.color, "000000"))
        run_properties.append(color)

        underline = OxmlElement("w:u")
        underline.set(qn("w:val"), "none")
        run_properties.append(underline)

        if xcell.font.bold:
            run_properties.append(OxmlElement("w:b"))
        if xcell.font.italic:
            run_properties.append(OxmlElement("w:i"))

        text_element = OxmlElement("w:t")
        text_element.text = display_text
        run.append(run_properties)
        run.append(text_element)
        hyperlink.append(run)
        paragraph._p.append(hyperlink)
        return hyperlink

    def set_cell_fill(word_cell, fill: str) -> None:
        tc_pr = word_cell._tc.get_or_add_tcPr()
        shd = tc_pr.find(qn("w:shd"))
        if shd is None:
            shd = OxmlElement("w:shd")
            tc_pr.append(shd)
        shd.set(qn("w:fill"), fill)

    def set_bottom_border(word_cell, color: str = "000000", size: str = "8") -> None:
        tc_pr = word_cell._tc.get_or_add_tcPr()
        borders = tc_pr.find(qn("w:tcBorders"))
        if borders is None:
            borders = OxmlElement("w:tcBorders")
            tc_pr.append(borders)
        bottom = borders.find(qn("w:bottom"))
        if bottom is None:
            bottom = OxmlElement("w:bottom")
            borders.append(bottom)
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), size)
        bottom.set(qn("w:color"), color)

    def set_top_border(word_cell, color: str = "000000", size: str = "8") -> None:
        tc_pr = word_cell._tc.get_or_add_tcPr()
        borders = tc_pr.find(qn("w:tcBorders"))
        if borders is None:
            borders = OxmlElement("w:tcBorders")
            tc_pr.append(borders)
        top = borders.find(qn("w:top"))
        if top is None:
            top = OxmlElement("w:top")
            borders.append(top)
        top.set(qn("w:val"), "single")
        top.set(qn("w:sz"), size)
        top.set(qn("w:color"), color)

    document = Document()
    audit_payloads = []
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    # Explicit A4 portrait page. Margins of 17.5 mm leave exactly 175 mm table width.
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(1.75)
    section.right_margin = Cm(1.75)
    section.top_margin = Cm(1.5)
    section.bottom_margin = Cm(1.5)

    for sheet_index, excel_path in enumerate(excel_paths):
        if sheet_index:
            document.add_page_break()
        workbook = load_workbook(excel_path, data_only=False, rich_text=True)
        audit_payload = read_audit_payload(workbook)
        if audit_payload:
            audit_payloads.append(audit_payload)
        if "NIAS Result" not in workbook.sheetnames:
            workbook.close()
            raise RuntimeError(
                f"Die Ergebnisdatei {Path(excel_path).name!r} enthält kein Blatt "
                "'NIAS Result' und kann nicht in den Word-Report übernommen werden."
            )
        ws = workbook["NIAS Result"]
        max_row = ws.max_row
        table_columns = max(
            (column for column in range(1, ws.max_column + 1)
             if ws.cell(5, column).value is not None),
            default=ws.max_column,
        )
        # Both fingerprint-shaped reports keep their quantity in the *last*
        # column, and §VII.9 may add an `RI` column between `RT (min)` and
        # `Name` - so the roles are read off the header row instead of being
        # counted from the left. Five or six columns; the NIAS report has
        # eight or nine and is never mistaken for either.
        header_names = [normalized_header(ws.cell(5, column).value)
                        for column in range(1, table_columns + 1)]
        quantity_header = header_names[-1] if header_names else ""
        fingerprint_page = table_columns in {5, 6} and quantity_header == "area"
        # The Total Extraction report is the Fingerprint report's layout with a
        # concentration in the last column instead of Area % (spec v3.1
        # §VII.10). Without this the eight-column NIAS widths would be applied
        # to a five-column table.
        total_extraction_page = (
            table_columns in {5, 6}
            and quantity_header == normalized_header(TOTAL_EXTRACTION_HEADER)
        )
        report_page = fingerprint_page or total_extraction_page
        # Positions of the columns whose rendering depends on their meaning.
        # Outside the two report pages these are the fixed NIAS positions, so
        # nothing about that table moves.
        rt_column = (header_names.index("rtmin") + 1
                     if "rtmin" in header_names else (0 if report_page else 1))
        ri_column = (header_names.index(RI_NORMALIZED_HEADER) + 1
                     if report_page and RI_NORMALIZED_HEADER in header_names
                     else 0)
        cas_column = (header_names.index("casno") + 1
                      if "casno" in header_names else 3)
        quantity_column = table_columns if report_page else 5
        while max_row > 1 and all(ws.cell(max_row, c).value is None for c in range(1, table_columns + 1)):
            max_row -= 1

        if "Executive Summary" in workbook.sheetnames:
            summary_sheet = workbook["Executive Summary"]
            heading = document.add_paragraph()
            heading.paragraph_format.space_after = Pt(4)
            heading_run = heading.add_run("Executive Summary")
            heading_run.bold = True
            heading_run.font.name = "Arial"
            heading_run.font.size = Pt(12)
            heading_run.font.color.rgb = RGBColor.from_string("BA0C2F")
            sample_text = text(summary_sheet["B3"].value)
            if sample_text:
                sample_p = document.add_paragraph()
                sample_p.paragraph_format.space_after = Pt(5)
                sample_run = sample_p.add_run(f"Sample: {sample_text}")
                sample_run.bold = True
                sample_run.font.name = "Arial"
                sample_run.font.size = Pt(9)
            summary_table = document.add_table(rows=1, cols=2)
            summary_table.alignment = WD_TABLE_ALIGNMENT.CENTER
            summary_table.autofit = False
            summary_table.cell(0, 0).text = "Status"
            summary_table.cell(0, 1).text = "Anzahl"
            for row_number in range(7, 11):
                cells = summary_table.add_row().cells
                cells[0].text = text(summary_sheet.cell(row_number, 1).value)
                cells[1].text = text(summary_sheet.cell(row_number, 2).value)
                fill = _excel_color_to_hex(summary_sheet.cell(row_number, 1).fill.fgColor, "FFFFFF")
                set_cell_fill(cells[0], fill)
                set_cell_fill(cells[1], fill)
            for cell in summary_table.rows[0].cells:
                set_cell_fill(cell, "BA0C2F")
                for run in cell.paragraphs[0].runs:
                    run.bold = True
                    run.font.color.rgb = RGBColor.from_string("FFFFFF")
                    run.font.name = "Arial"
                    run.font.size = Pt(8)
            for row in summary_table.rows[1:]:
                for cell in row.cells:
                    for run in cell.paragraphs[0].runs:
                        run.font.name = "Arial"
                        run.font.size = Pt(8)
            document.add_paragraph().paragraph_format.space_after = Pt(1)

        table = document.add_table(rows=max_row, cols=table_columns)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False

        if report_page:
            # By header, not by position: a six-column table with `RI` gets the
            # same widths as the five-column one plus the index's own, and the
            # five-column tables come out with exactly [8, 46, 14, 10, 12] as
            # before - with or without §VII.9's `replace_rt`.
            report_width_units = {"rtmin": 8, RI_NORMALIZED_HEADER: 8,
                                  "name": 46, "casno": 14, "match": 10}
            width_units = [report_width_units.get(name, 12)
                           for name in header_names]
        else:
            width_units = [7, 30, 10, 6, 8, 8, 8, 8] + ([7] if table_columns == 9 else [])
        table_width = Cm(17.5)
        total_units = sum(width_units)
        for index, unit in enumerate(width_units[:table_columns]):
            table.columns[index].width = int(table_width * unit / total_units)
        for row in table.rows:
            # Sliced: a result sheet with an unforeseen column count must come
            # out narrow, never as an IndexError in the middle of a report.
            for col_index, unit in enumerate(width_units[:table_columns]):
                row.cells[col_index].width = int(table_width * unit / total_units)

        merged_slaves = set()
        for merged_range in ws.merged_cells.ranges:
            if merged_range.max_col > table_columns or merged_range.max_row > max_row:
                continue
            top = table.cell(merged_range.min_row - 1, merged_range.min_col - 1)
            bottom = table.cell(merged_range.max_row - 1, merged_range.max_col - 1)
            top.merge(bottom)
            for rr in range(merged_range.min_row, merged_range.max_row + 1):
                for cc in range(merged_range.min_col, merged_range.max_col + 1):
                    if (rr, cc) != (merged_range.min_row, merged_range.min_col):
                        merged_slaves.add((rr, cc))

        h_map = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                 "right": WD_ALIGN_PARAGRAPH.RIGHT}
        for row_index in range(1, max_row + 1):
            for col_index in range(1, table_columns + 1):
                if (row_index, col_index) in merged_slaves:
                    continue
                xcell = ws.cell(row_index, col_index)
                wcell = table.cell(row_index - 1, col_index - 1)
                wcell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                paragraph = wcell.paragraphs[0]
                paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.alignment = h_map.get(xcell.alignment.horizontal, WD_ALIGN_PARAGRAPH.LEFT)

                value = xcell.value
                if isinstance(value, CellRichText):
                    for part in value:
                        run = paragraph.add_run(part.text if isinstance(part, TextBlock) else str(part))
                        if isinstance(part, TextBlock):
                            run.font.superscript = part.font.vertAlign == "superscript"
                elif value is not None:
                    if (isinstance(value, (int, float)) and row_index >= 6
                            and col_index == ri_column):
                        # §VII.9: a retention index is a whole number.
                        display = f"{int(round(value))}"
                    elif (isinstance(value, float) and row_index >= 6
                            and col_index == rt_column):
                        # Final Word report: retention time with exactly two decimals.
                        display = f"{value:.2f}"
                    elif (isinstance(value, float) and fingerprint_page
                            and col_index == quantity_column):
                        display = f"{value:.4f}"
                    elif (isinstance(value, (int, float)) and total_extraction_page
                            and col_index == quantity_column and row_index >= 6):
                        # Same one decimal the report's own "0.0" format shows,
                        # so the Word table repeats the Excel value (spec §VII.10).
                        display = f"{value:.1f}"
                    elif (isinstance(value, (int, float)) and not report_page
                            and col_index == 5 and row_index >= 6):
                        # The report is English, so every numeric column uses a
                        # decimal point: RT 2, mg/dm2 4, mg/kg 3 places.
                        display = f"{value:.4f}"
                    elif (isinstance(value, float) and not report_page
                            and col_index == 6):
                        display = f"{value:.3f}"
                    else:
                        display = str(value)

                    normalized_cas_link = (normalize_cas(display)
                                           if col_index == cas_column else "")
                    if col_index == cas_column and is_valid_cas_number(normalized_cas_link):
                        pubchem_url = (
                            "https://pubchem.ncbi.nlm.nih.gov/compound/"
                            + quote(normalized_cas_link, safe="")
                        )
                        add_hyperlink(paragraph, display, pubchem_url, xcell)
                    else:
                        paragraph.add_run(display)

                for run in paragraph.runs:
                    run.font.name = xcell.font.name or "Arial"
                    run.font.size = Pt(xcell.font.sz or 8)
                    run.bold = bool(xcell.font.bold)
                    run.italic = bool(xcell.font.italic)
                    if xcell.font.vertAlign == "superscript":
                        run.font.superscript = True
                    run.font.color.rgb = RGBColor.from_string(
                        _excel_color_to_hex(xcell.font.color, "000000")
                    )

                # Only the main title band (row 1) keeps its Excel color.
                # Every other Word table cell is explicitly white/no-color.
                if row_index == 1:
                    fill = _excel_color_to_hex(xcell.fill.fgColor, "BA0C2F")
                elif col_index == cas_column and row_index >= 6 and xcell.fill.fill_type:
                    fill = _excel_color_to_hex(xcell.fill.fgColor, "FFFFFF")
                else:
                    fill = "FFFFFF"
                set_cell_fill(wcell, fill)
                if xcell.border.top is not None and xcell.border.top.style:
                    set_top_border(
                        wcell,
                        _excel_color_to_hex(xcell.border.top.color, "000000"),
                    )
                if xcell.border.bottom is not None and xcell.border.bottom.style:
                    set_bottom_border(
                        wcell,
                        _excel_color_to_hex(xcell.border.bottom.color, "000000"),
                    )

        workbook.close()

    if audit_payloads:
        document.add_page_break()
        heading = document.add_paragraph()
        heading_run = heading.add_run("Audit Trail")
        heading_run.bold = True
        heading_run.font.name = "Arial"
        heading_run.font.size = Pt(14)
        heading_run.font.color.rgb = RGBColor.from_string("BA0C2F")
        for index, payload in enumerate(audit_payloads, start=1):
            fields = payload.get("fields", {})
            subheading = document.add_paragraph()
            subheading_run = subheading.add_run(
                f"Report {index}: {fields.get('Sample') or Path(fields.get('Source file', '')).name}"
            )
            subheading_run.bold = True
            subheading_run.font.name = "Arial"
            subheading_run.font.size = Pt(9)
            audit_table = document.add_table(rows=1, cols=2)
            audit_table.alignment = WD_TABLE_ALIGNMENT.CENTER
            audit_table.cell(0, 0).text = "Field"
            audit_table.cell(0, 1).text = "Value"
            for cell in audit_table.rows[0].cells:
                set_cell_fill(cell, "BA0C2F")
                for run in cell.paragraphs[0].runs:
                    run.bold = True
                    run.font.color.rgb = RGBColor.from_string("FFFFFF")
                    run.font.name = "Arial"
                    run.font.size = Pt(7)
            for field, value in fields.items():
                if value in (None, "", {}, []):
                    continue
                cells = audit_table.add_row().cells
                cells[0].text = str(field)
                cells[1].text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                for cell in cells:
                    for run in cell.paragraphs[0].runs:
                        run.font.name = "Arial"
                        run.font.size = Pt(7)

    word_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(word_path)
    return word_path


class BatchProgressWindow:
    """Small branded progress surface kept visible during synchronous processing.

    Created as a ``Toplevel`` of the application window whenever one exists. The
    previous ``tk.Tk()`` opened a second interpreter root, which only worked
    because the main window had already been destroyed.
    """
    def __init__(self, total: int, parent=None):
        self.total = max(total, 1)
        self.parent = parent
        self.root = tk.Toplevel(parent) if parent is not None else tk.Tk()
        self.root.title("NIAS Screening Processor – Verarbeitung")
        self.root.geometry("720x350")
        self.root.resizable(False, False)
        colors = active_theme_colors()
        self.root.configure(bg=colors["background"])
        if parent is not None:
            self.root.transient(parent)
            self.root.grab_set()
        progress_style = ttk.Style(self.root)
        configure_ttk_styles(progress_style, colors, self.root)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        header = tk.Frame(self.root, bg=colors["header"], height=75)
        header.pack(fill=tk.X)
        tk.Label(header, text="NIAS Screening Processor", bg=colors["header"],
                 fg=colors["header_text"], font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=24, pady=(15, 0))
        tk.Label(header, text="Dateien werden verarbeitet", bg=colors["header"],
                 fg=colors["header_muted"], font=("Segoe UI", 9)).pack(anchor="w", padx=24)
        body = tk.Frame(self.root, bg=colors["surface"],
                        highlightbackground=colors["border"], highlightthickness=1)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=18)
        self.count_var = tk.StringVar(value=f"0 von {total} Dateien")
        self.file_var = tk.StringVar(value="Vorbereitung …")
        self.stage_var = tk.StringVar(value="Eingaben werden geprüft.")
        tk.Label(body, textvariable=self.count_var, bg=colors["surface"], fg=colors["brand"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=20, pady=(18, 3))
        tk.Label(body, textvariable=self.file_var, bg=colors["surface"], fg=colors["text"],
                 font=("Segoe UI", 11, "bold"), wraplength=640, justify="left").pack(anchor="w", padx=20)
        tk.Label(body, textvariable=self.stage_var, bg=colors["surface"], fg=colors["muted"],
                 font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(5, 14))
        self.progress = ttk.Progressbar(body, maximum=self.total, mode="determinate", style="Brand.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, padx=20)
        self.log = tk.Text(body, height=4, state="disabled", bg=colors["console"],
                           fg=colors["text"], insertbackground=colors["text"],
                           font=("Consolas", 8), relief="flat")
        self.log.pack(fill=tk.X, padx=20, pady=(14, 18))
        self.root.update()

    def update(self, index: int, file_name: str, stage: str) -> None:
        self.count_var.set(f"{index} von {self.total} Dateien")
        self.file_var.set(file_name)
        self.stage_var.set(stage)
        self.progress["value"] = max(0, index - 1)
        self._log(f"[{index}/{self.total}] {file_name}: {stage}")
        self.root.update_idletasks()
        self.root.update()

    def complete_file(self, index: int, message: str) -> None:
        self.progress["value"] = index
        self._log(message)
        self.root.update_idletasks()
        self.root.update()

    def _log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, line + "\n")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def close(self) -> None:
        try:
            self.root.grab_release()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def show_result_dashboard(results: list[dict[str, Any]], failures: list[tuple[Path, str]],
                          parent=None) -> None:
    """Show a scannable result dashboard with direct output actions.

    Opened as a ``Toplevel`` when the application window is still alive, so
    closing the dashboard returns to it instead of ending the program.
    """
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    root.title("NIAS Screening Processor – Ergebnisse")
    root.geometry("980x650")
    root.minsize(860, 560)
    colors = active_theme_colors()
    root.configure(bg=colors["background"])
    if parent is not None:
        root.transient(parent)
    dashboard_style = ttk.Style(root)
    configure_ttk_styles(dashboard_style, colors, root)
    header = tk.Frame(root, bg=colors["header"])
    header.pack(fill=tk.X)
    status_title = "Verarbeitung abgeschlossen" if not failures else "Verarbeitung mit Hinweisen abgeschlossen"
    tk.Label(header, text=status_title, bg=colors["header"], fg=colors["header_text"],
             font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=24, pady=(16, 2))
    tk.Label(header, text=f"{len(results)} erfolgreich  ·  {len(failures)} fehlgeschlagen",
             bg=colors["header"], fg=colors["header_muted"],
             font=("Segoe UI", 9)).pack(anchor="w", padx=24, pady=(0, 16))
    body = tk.Frame(root, bg=colors["background"])
    body.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)
    total_rows = sum(r.get("retained_rows", 0) for r in results)
    exceedances = sum(len(r.get("sml_exceedances", [])) for r in results)
    unknowns = sum(r.get("unidentified_count", r.get("pubchem_no_hits", 0)) for r in results)
    has_compliance_results = any(
        r.get("source_format") not in {"Fingerprint", TOTAL_EXTRACTION_FORMAT}
        for r in results)
    metrics = tk.Frame(body, bg=colors["background"])
    metrics.pack(fill=tk.X, pady=(0, 12))
    metric_values = [("Dateien", len(results), colors["text"]),
                     ("Ergebniszeilen", total_rows, colors["text"])]
    if has_compliance_results:
        metric_values.append(("SML-Überschreitungen", exceedances,
                              colors["error"] if exceedances else colors["success"]))
    metric_values.append(("Nicht identifiziert", unknowns, colors["muted"]))
    for label, value, color in metric_values:
        card = tk.Frame(metrics, bg=colors["surface"],
                        highlightbackground=colors["border"], highlightthickness=1)
        card.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        tk.Label(card, text=str(value), bg=colors["surface"], fg=color,
                 font=("Segoe UI", 20, "bold")).pack(pady=(10, 0))
        tk.Label(card, text=label, bg=colors["surface"], fg=colors["muted"],
                 font=("Segoe UI", 8)).pack(pady=(0, 10))
    # Unknown register feedback. This is the payoff of keeping the register: an
    # unknown that was clarified once should announce itself the next time it
    # turns up, instead of being investigated from scratch again.
    register_recorded = 0
    register_hints: list[dict[str, Any]] = []
    register_splits: list[dict[str, Any]] = []
    register_errors: list[str] = []
    seen_hints: set = set()
    for result in results:
        info = result.get("unknown_register") or {}
        register_recorded += int(info.get("recorded") or 0)
        for hint in info.get("hints") or ():
            marker = (hint.get("id"), hint.get("match_id"))
            if marker not in seen_hints:
                seen_hints.add(marker)
                register_hints.append(hint)
        register_splits.extend(info.get("splits") or ())
        if info.get("error"):
            register_errors.append(info["error"])

    if register_recorded or register_hints or register_splits or register_errors:
        register_card = tk.Frame(body, bg=colors["surface"],
                                 highlightbackground=colors["border"],
                                 highlightthickness=1)
        register_card.pack(fill=tk.X, pady=(0, 12))
        tk.Label(register_card, text="Unknown Register", bg=colors["surface"],
                 fg=colors["brand"],
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        summary = (f"{register_recorded} nicht identifizierte Substanz(en) "
                   "mit m/z-Angabe erfasst")
        if register_splits:
            summary += (f", davon {len(register_splits)} als neuer Eintrag "
                        "getrennt")
        tk.Label(register_card, text=summary + ".",
                 bg=colors["surface"], fg=colors["text"],
                 font=("Segoe UI", 9)).pack(anchor="w", padx=12)
        # A split is the one thing in this card the analyst may have to act on:
        # the register decided that a familiar mass signature is a different
        # substance this time, and only a human can confirm that.
        for split in register_splits:
            rt_text = (f" bei RT {format_de(split.get('rt'), 3)} min"
                       if split.get("rt") is not None else "")
            tk.Label(
                register_card,
                text=(f"m/z {split.get('mz')}{rt_text} als neuer Eintrag "
                      f"{split.get('id')} angelegt – gleiche Massensignatur wie "
                      f"{split.get('from')}, aber: {split.get('reason')}"),
                bg=colors["surface"], fg=colors["warning"], font=("Segoe UI", 9),
                wraplength=880,
                justify="left").pack(anchor="w", padx=12, pady=(4, 0))
        for hint in register_hints:
            identity = hint.get("match_name") or hint.get("match_id")
            if hint.get("match_cas"):
                identity = f"{identity} (CAS {hint['match_cas']})"
            tk.Label(
                register_card,
                text=(f"m/z {hint.get('mz')} stimmt zu {hint.get('score', 0) * 100:.0f} % mit "
                      f"{hint.get('match_id')} überein – dort identifiziert als {identity}."),
                bg=colors["surface"], fg=colors["success"], font=("Segoe UI", 9),
                wraplength=880,
                justify="left").pack(anchor="w", padx=12, pady=(4, 0))
        for message in register_errors:
            tk.Label(register_card, text="Register nicht aktualisiert: " + message,
                     bg=colors["surface"], fg=colors["warning"], font=("Segoe UI", 9),
                     wraplength=880,
                     justify="left").pack(anchor="w", padx=12, pady=(4, 0))
        tk.Frame(register_card, bg=colors["surface"], height=8).pack()

    table = ttk.Treeview(
        body, columns=("source", "format", "result", "rows", "finding", "output"),
        show="headings", height=12)
    for col, title, width in (
        ("source", "Quelldatei", 190), ("format", "Format", 120),
        ("result", "Ergebnis", 85), ("rows", "Zeilen", 60),
        ("finding", "SML", 85), ("output", "Ausgabedatei", 350),
    ):
        table.heading(col, text=title)
        table.column(col, width=width,
                     anchor="center" if col in {"format", "result", "rows", "finding"} else "w")
    output_by_item = {}
    for r in results:
        output = Path(r["output"])
        finding = len(r.get("sml_exceedances", []))
        item = table.insert("", tk.END, values=(
            output.stem.replace("_processed", ""), r.get("source_format", ""),
            "Erfolgreich", r.get("retained_rows", 0), finding, str(output)))
        output_by_item[item] = output
    for path, reason in failures:
        table.insert("", tk.END, values=(path.name, "–", "Fehler", "–", "–", reason))
    table.pack(fill=tk.BOTH, expand=True)
    actions = tk.Frame(body, bg=colors["background"])
    actions.pack(fill=tk.X, pady=(12, 0))
    def open_path(path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Datei konnte nicht geöffnet werden", f"{path}\n\nDetails: {exc}", parent=root)
    def open_selected() -> None:
        selected = table.selection()
        if selected and selected[0] in output_by_item:
            open_path(output_by_item[selected[0]])
    def open_folder() -> None:
        selected = table.selection()
        if selected and selected[0] in output_by_item:
            open_path(output_by_item[selected[0]].parent)
        elif results:
            open_path(Path(results[0]["output"]).parent)
    ttk.Button(actions, text="Ausgewähltes Ergebnis öffnen", command=open_selected).pack(side=tk.LEFT)
    ttk.Button(actions, text="Ausgabeordner öffnen", command=open_folder).pack(side=tk.LEFT, padx=8)
    word_outputs = [Path(r["word_output"]) for r in results if r.get("word_output")]
    if word_outputs:
        ttk.Button(actions, text="Word-Report öffnen", command=lambda: open_path(word_outputs[0])).pack(side=tk.LEFT)
    brand_button(actions, "Schließen", root.destroy).pack(side=tk.RIGHT)
    if parent is not None:
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.grab_set()
        parent.wait_window(root)
    else:
        root.mainloop()

def run_nias_batch(
    nias_files: list[Path],
    cas_file: Optional[Path],
    outdir: Optional[Path],
    create_word: bool,
    save_in_source: bool,
    full_auto_root: Optional[Path],
    include_traffic_light_summary: bool,
    sheet_name: Optional[str] = None,
    single_output_path: Optional[Path] = None,
    progress=None,
    verbose: bool = True,
    raise_on_error: bool = False,
    session_metadata=None,
) -> tuple[list[dict[str, Any]], list[tuple[Path, str]], Optional[Path]]:
    """Process every selected workbook and optionally build the Word report.

    Shared by the command line and the graphical workflow so both take exactly
    the same path through the evaluation. ``progress`` is any object offering
    ``update(index, name, stage)`` and ``complete_file(index, message)``.
    """
    results: list[dict[str, Any]] = []
    failures: list[tuple[Path, str]] = []
    word_path: Optional[Path] = None

    def emit(line: str) -> None:
        if verbose:
            print(line)

    for file_index, nias_path in enumerate(nias_files, start=1):
        if progress:
            progress.update(file_index, nias_path.name,
                            "Arbeitsmappe lesen, Referenzen prüfen und Report erstellen …")
        try:
            if single_output_path is not None and len(nias_files) == 1:
                output_path = single_output_path
            else:
                if full_auto_root is not None:
                    target_folder = full_auto_root
                else:
                    target_folder = nias_path.parent if save_in_source else outdir
                output_path = Path(target_folder) / f"{nias_path.stem}_processed.xlsx"
            if nias_path.suffix.lower() == '.niasgc':
                from gc_session_io import export_session
                import tempfile
                with tempfile.TemporaryDirectory(prefix='nias_session_') as staging:
                    intermediate = export_session(nias_path, Path(staging) / f'{nias_path.stem}.xlsx', cas_file,
                        metadata_override=(session_metadata or {}).get(str(nias_path)))
                    result = process_workbook(intermediate, cas_file, output_path,
                        sheet_name=sheet_name, include_traffic_light_summary=include_traffic_light_summary)
            else:
                result = process_workbook(nias_path, cas_file, output_path,
                    sheet_name=sheet_name, include_traffic_light_summary=include_traffic_light_summary)
            results.append(result)
            if progress:
                progress.complete_file(file_index, f"Erfolgreich: {Path(result['output']).name}")
            emit(f"Created: {result['output']}")
            emit(f"Source worksheet: {result['source_sheet']}")
            emit(f"Rows in result: {result['retained_rows']}")
            emit(f"Source format: {result['source_format']}")
            emit(f"Footnotes: {result['footnotes']}")
            emit(f"PubChem names found: {result['pubchem_names_found']}")
            emit(f"PubChem no hits: {result['pubchem_no_hits']}")
            if result["source_format"] == "Fingerprint":
                emit(f"Final Area % total: {result.get('final_area_pct_total', 0):.4f}")
            elif result["source_format"] == TOTAL_EXTRACTION_FORMAT:
                emit(f"{TOTAL_EXTRACTION_SUM_LABEL}: "
                     f"{result.get('total_concentration_ugl', 0):.1f}")
            else:
                emit(f"Grouped peak counts: {result['sum_counts']}")
                emit(f"Grouped concentrations (mg/dm2): {result['sums_area']}")
                emit(f"Grouped concentrations (mg/kg): {result['sums_kg']}")
                if result["sml_exceedances"]:
                    emit("SML exceedances:")
                    for name in result["sml_exceedances"]:
                        emit(f"  - {name}")
                else:
                    emit("No numerical SML exceedances found.")
        except Exception as exc:
            failures.append((nias_path, str(exc)))
            if progress:
                progress.complete_file(file_index, f"Fehler: {nias_path.name}")
            if raise_on_error:
                raise

    if progress:
        progress.update(
            len(nias_files), "Berichte abschließen",
            "Kombinierten Word-Report vorbereiten …" if results and create_word
            else "Ergebnisübersicht vorbereiten …")

    if results and create_word:
        if full_auto_root is not None:
            word_folder = Path(full_auto_root)
        else:
            word_folder = nias_files[0].parent if save_in_source else Path(outdir)
        # Spec v2.1 §V.4: a run covering several samples is named after the
        # Syneris summary report number, a single sample after its own derived
        # Syneris number, and neither keeps the old combined name so a finished
        # run is never lost over a missing number.
        summary_report_no = next(
            (text(item.get("syneris_summary_report_no")) for item in results
             if text(item.get("syneris_summary_report_no")).strip()), "")
        word_stem = word_report_file_stem(
            [item.get("syneris_number") for item in results], summary_report_no)
        candidate = unique_output_path(word_folder, word_stem, ".docx")
        try:
            create_combined_word([Path(item["output"]) for item in results], candidate)
            word_path = candidate
            emit(f"Created combined Word document: {candidate}")
            for item in results:
                item["word_output"] = candidate
        except Exception as exc:
            failures.append((candidate, f"Word-Report konnte nicht erstellt werden: {exc}"))
            if progress:
                progress.complete_file(
                    len(nias_files), "Hinweis: Word-Report konnte nicht erstellt werden.")

    return results, failures, word_path


def main() -> None:
    args = parse_args()

    if args.nias_file is None:
        # The graphical workflow owns its own window and never returns results;
        # the program stays open until the analyst closes it.
        run_nias_gui()
        return

    results, failures, _ = run_nias_batch(
        [args.nias_file],
        args.cas_file,
        args.output.parent if args.output else Path("."),
        args.word,
        False,
        None,
        args.ampel_summary,
        sheet_name=args.sheet,
        single_output_path=args.output,
        raise_on_error=True,
    )
    if failures and not results:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

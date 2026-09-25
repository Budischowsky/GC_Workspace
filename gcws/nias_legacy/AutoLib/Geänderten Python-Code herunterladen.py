#!/usr/bin/env python3
"""NIAS GC-MS/FID peak assignment with a simple Tkinter UI.

Inputs:
  * Agilent RESULTS.CSV (Area Percent Report with TIC, FID and PBM sections)
  * Optional LIBresults.csv (detailed top library hits)
Output:
  * Formatted Excel workbook (.xlsx)

No spectra are decoded. Unknown m/z lists therefore remain unavailable.
"""
from __future__ import annotations

import csv
import math
import re
import statistics
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.formatting.rule import FormulaRule
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.datavalidation import DataValidation


@dataclass
class FIDPeak:
    peak: int
    rt: float
    start: float
    end: float
    pk_type: str
    height: float
    area: float

    @property
    def width_min(self): return self.end - self.start

    @property
    def area_height(self): return self.area / self.height if self.height else None


@dataclass
class TICPeak:
    peak: int
    rt: float
    height: float
    area: float


@dataclass
class Hit:
    name: str
    ref: str = ""
    cas: str = ""
    quality: Optional[int] = None


@dataclass
class PBMPeak:
    peak: int
    rt: float
    area_pct: float
    hits: list[Hit] = field(default_factory=list)


@dataclass
class Settings:
    solvent_end: float = 5.5
    quality_limit: int = 70
    rt_tolerance: float = 0.035
    cell_area_dm2: float = 0.51
    coverage: float = 1.0
    ov_ratio: float = 6.0
    is_amount: float = 10.0
    fc17_conc: float = 0.82
    bbp_conc: float = 0.83
    dnnp_conc: float = 0.82
    qc_min_area: float = 0.0
    blank_rt_tolerance: float = 0.04


IS_DEFS = [
    ("Perdeutero-Heptadecane", 13.444, "Quantification", "fc17_conc"),
    ("Benzyl-butyl-phthalate-d4", 18.954, "Quantification", "bbp_conc"),
    ("Di-n-nonyl-phthalate-d4", 22.509, "Quantification", "dnnp_conc"),
    ("Dibutyl phthalate-3,4,5,6-d4", 15.967, "QC", None),
]

CLASS_RULES = [
    ("phthal", "phthalate"), ("silox", "siloxane"), ("phenol", "phenol"),
    ("benzaldehyde", "benzaldehyde"), ("benzenepropanoic", "benzenepropanoic acid derivative"),
    ("amide", "amide"), ("ester", "ester"), ("ether", "ether"),
    ("alkane", "hydrocarbon"), ("decane", "hydrocarbon"), ("dodecane", "hydrocarbon"),
    ("tridecane", "hydrocarbon"), ("tetradecane", "hydrocarbon"),
    ("pentadecane", "hydrocarbon"), ("hexadecane", "hydrocarbon"),
    ("heptadecane", "hydrocarbon"), ("octadecane", "hydrocarbon"),
    ("nonadecane", "hydrocarbon"), ("eicosane", "hydrocarbon"),
    ("heneicosane", "hydrocarbon"), ("docosane", "hydrocarbon"),
    ("tricosane", "hydrocarbon"), ("tetracosane", "hydrocarbon"),
    ("pentacosane", "hydrocarbon"), ("hexacosane", "hydrocarbon"),
    ("heptacosane", "hydrocarbon"), ("octacosane", "hydrocarbon"),
    ("nonacosane", "hydrocarbon"), ("triacontane", "hydrocarbon"),
]


def _f(x):
    return float(str(x).strip())


def parse_results(path: str):
    text = Path(path).read_text(encoding="latin-1", errors="replace")
    section = None
    tic, fid, pbm = [], [], []
    for raw in text.splitlines():
        line = raw.strip("\ufeff")
        if line.startswith("[INT TIC:"):
            section = "tic"; continue
        if line.startswith("[INT ") and "FID1A.ch" in line:
            section = "fid"; continue
        if line.startswith("[PBM Apex"):
            section = "pbm"; continue
        if not re.match(r"^\d+=,", line):
            continue
        try:
            row = next(csv.reader([line]))
            if section == "tic":
                tic.append(TICPeak(int(row[1]), _f(row[2]), _f(row[7]), _f(row[8])))
            elif section == "fid":
                fid.append(FIDPeak(int(row[1]), _f(row[2]), _f(row[3]), _f(row[4]),
                                   row[5].strip(), _f(row[6]), _f(row[7])))
            elif section == "pbm":
                pbm.append(PBMPeak(int(row[1]), _f(row[2]), _f(row[3]),
                                   [Hit(row[4].strip(), row[5].strip(), row[6].strip(), int(row[7]))]))
        except (ValueError, IndexError):
            pass
    if not fid or not pbm:
        raise ValueError("FID- oder PBM-Tabelle wurde in RESULTS.CSV nicht gefunden.")
    return tic, fid, pbm


def parse_library_report(path: str) -> dict[int, list[Hit]]:
    """Best-effort parser for the first three hits in Agilent Library Search Report."""
    if not path:
        return {}
    lines = Path(path).read_text(encoding="latin-1", errors="replace").splitlines()
    result: dict[int, list[Hit]] = {}
    current_pk = None
    buffer = ""
    hit_re = re.compile(r"^(.*?)\s+(\d+)\s+(\d{6}-\d{2}-\d|\d{1,7}-\d{2}-\d)\s+(\d{1,3})\s*$")
    pk_re = re.compile(r"^\s*(\d+)\s+(\d+\.\d+)\s+([\d.]+)\s+.*$")

    def consume(s):
        nonlocal buffer
        m = hit_re.match(s.strip())
        if m and current_pk is not None and len(result.setdefault(current_pk, [])) < 3:
            result[current_pk].append(Hit(m.group(1).strip(), m.group(2), m.group(3), int(m.group(4))))
            buffer = ""
            return True
        return False

    for line in lines:
        m = pk_re.match(line)
        if m:
            if buffer: consume(buffer)
            current_pk = int(m.group(1)); buffer = ""
            # Text following Area% may already include library path, not a hit.
            continue
        if current_pk is None or "GC-Datenbanken" in line or not line.strip():
            continue
        candidate = (buffer + " " + line.strip()).strip()
        if not consume(candidate):
            buffer = candidate
            # Avoid endlessly carrying headers or malformed text.
            if len(buffer) > 500: buffer = ""
    if buffer: consume(buffer)
    return result


def estimate_delay(tic: list[TICPeak], fid: list[FIDPeak], solvent_end: float):
    """Robust median FID-minus-MS delay from mutually nearest, close apex pairs."""
    diffs = []
    usable_fid = [x for x in fid if x.rt > solvent_end]
    for t in tic:
        if t.rt <= solvent_end or not usable_fid: continue
        f = min(usable_fid, key=lambda x: abs(x.rt - t.rt))
        d = f.rt - t.rt
        if abs(d) <= 0.03:
            diffs.append(d)
    if not diffs:
        return 0.006
    med = statistics.median(diffs)
    mad = statistics.median(abs(x-med) for x in diffs) or 0.002
    clean = [x for x in diffs if abs(x-med) <= max(3*mad, 0.006)]
    return statistics.median(clean or diffs)


def common_class(hits: list[Hit]):
    names = [h.name.lower() for h in hits if h.name]
    if len(names) < 2: return None
    for token, label in CLASS_RULES:
        if sum(token in n for n in names) >= 2:
            return label
    return None


def display_identification(p: PBMPeak, quality_limit: int):
    if not p.hits:
        return "unknown (m/z unavailable)", "Unknown", ""
    h = p.hits[0]
    if h.quality is not None and h.quality >= quality_limit:
        return h.name, "Accepted", h.cas
    cls = common_class(p.hits[:3])
    if cls:
        return f"possible derivative of {cls}", "Uncertain; class inferred", ""
    return "unknown (m/z unavailable)", "Uncertain", ""


def assign(fid: list[FIDPeak], pbm: list[PBMPeak], delay: float, s: Settings):
    rows = []
    for f in fid:
        if f.rt <= s.solvent_end:
            continue
        # Prefer candidates whose delay-corrected RT lies inside integration limits.
        candidates = [p for p in pbm if f.start <= p.rt + delay <= f.end]
        # Guard against exceptionally broad integrations: require proximity to apex.
        candidates = [p for p in candidates if abs((p.rt + delay) - f.rt) <= s.rt_tolerance]
        if not candidates:
            rows.append((f, None, [], "No MS match")); continue
        # Main component: largest PBM area%, then quality, then apex proximity.
        candidates.sort(key=lambda p: (p.area_pct,
                                       p.hits[0].quality if p.hits and p.hits[0].quality is not None else -1,
                                       -abs((p.rt + delay)-f.rt)), reverse=True)
        main = candidates[0]
        rows.append((f, main, candidates[1:], "Matched"))
    return rows



@dataclass(frozen=True)
class BlankCorrection:
    """Traceable blank correction for one sample FID peak."""
    sample_peak: int
    raw_area: float
    blank_area: float = 0.0
    blank_istd_area: float = 0.0
    subtracted_area: float = 0.0
    corrected_area: float = 0.0
    protected_standard: bool = False
    status: str = "not corrected"


def _normalise_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _matches_standard_name(p: Optional[PBMPeak], std_name: str) -> bool:
    """Return True when the library hit clearly names this internal standard.

    The standard's full key must occur in the library name, never the other way
    round. Accepting the reverse direction made every undeuterated parent
    compound match its own deuterated standard, because "dibutylphthalate" is a
    substring of "dibutylphthalate3456d4". Dibutyl phthalate, benzyl butyl
    phthalate, di-n-nonyl phthalate and heptadecane - all routine NIAS analytes -
    were therefore treated as internal standards: protected from blank
    correction in the sample and discarded from the blank reference.
    """
    hit_name = _normalise_name(p.hits[0].name) if p and p.hits else ""
    if not hit_name:
        return False
    std_key = _normalise_name(std_name)
    # Avoid false positives from short/generic library names such as "A".
    return hit_name == std_key or (len(std_key) >= 8 and std_key in hit_name)


def internal_standard_peak_ids(rows, rt_tolerance: float = 0.08) -> set[int]:
    """Return the FID peak numbers that represent internal standards.

    Identification is by library name first. Only where a standard was not found
    by name does retention time act as a fallback, and then it protects exactly
    one peak per standard: the closest one still unclaimed inside the tolerance.
    The previous per-peak RT test protected every peak inside a 0.08 min window
    around any of the four target retention times, which is more than twice the
    assignment tolerance. Co-eluting analytes were therefore treated as standards
    and escaped blank correction entirely.
    """
    protected: set[int] = set()
    named: set[str] = set()
    for row in rows:
        fid_peak, pbm_peak = row[0], row[1]
        for std_name, _target_rt, _role, _conc in IS_DEFS:
            if _matches_standard_name(pbm_peak, std_name):
                protected.add(fid_peak.peak)
                named.add(std_name)
                break
    for std_name, target_rt, _role, _conc in IS_DEFS:
        if std_name in named:
            continue
        candidates = sorted(
            (abs(row[0].rt - target_rt), row[0].peak) for row in rows
            if row[0].peak not in protected
            and abs(row[0].rt - target_rt) <= rt_tolerance
        )
        if candidates:
            protected.add(candidates[0][1])
    return protected


def subtract_blank_signals(sample_rows, blank_rows=None, blank_istd_rows=None,
                           rt_tolerance: float = 0.04):
    """Subtract the one-to-one RT-matched Blank / Blank+ISTD FID area.

    Blank and Blank+ISTD describe the same solvent and system background, so the
    larger of the two matched areas is subtracted once. Subtracting both in turn
    removed a shared contamination twice, biased results low and occasionally
    pushed a real peak under the reporting limit.

    Overlapping integration windows are preferred before apex distance. Internal
    standards are protected in the sample and ignored in both blank references.
    Negative results are clipped to zero. Corrected FIDPeak copies and a complete
    peak-level audit record are returned.
    """
    blank_rows = list(blank_rows or [])
    blank_istd_rows = list(blank_istd_rows or [])
    sample_protected = internal_standard_peak_ids(sample_rows)

    def usable_reference(rows):
        standards = internal_standard_peak_ids(rows)
        return [(f, p) for f, p, _, _ in rows if f.peak not in standards]

    def match_one_to_one(samples, references):
        candidates = []
        for sample_index, (sf, _, _, _) in enumerate(samples):
            if sf.peak in sample_protected:
                continue
            for ref_index, (rf, _) in enumerate(references):
                delta = abs(sf.rt - rf.rt)
                if delta > rt_tolerance:
                    continue
                overlaps = not (sf.end < rf.start or rf.end < sf.start)
                candidates.append((0 if overlaps else 1, delta, sample_index, ref_index))
        candidates.sort()
        matches, used_samples, used_refs = {}, set(), set()
        for _, _, sample_index, ref_index in candidates:
            if sample_index in used_samples or ref_index in used_refs:
                continue
            matches[sample_index] = references[ref_index][0]
            used_samples.add(sample_index); used_refs.add(ref_index)
        return matches

    blank_matches = match_one_to_one(sample_rows, usable_reference(blank_rows))
    blank_istd_matches = match_one_to_one(sample_rows, usable_reference(blank_istd_rows))
    corrected_rows, audit = [], {}
    for index, (f, p, secondary, match_status) in enumerate(sample_rows):
        protected = f.peak in sample_protected
        blank_area = blank_matches[index].area if index in blank_matches and not protected else 0.0
        blank_istd_area = blank_istd_matches[index].area if index in blank_istd_matches and not protected else 0.0
        # Both references describe the same background, so it is subtracted once.
        subtracted_area = 0.0 if protected else max(blank_area, blank_istd_area)
        corrected_area = f.area if protected else max(0.0, f.area - subtracted_area)
        if protected:
            status = "ISTD geschützt; keine Blanksubtraktion"
        elif not subtracted_area:
            status = "kein passendes Blanksignal"
        else:
            source = "Blank" if blank_area >= blank_istd_area else "Blank+ISTD"
            status = f"{source} subtrahiert (Maximum aus Blank/Blank+ISTD)"
            if corrected_area == 0.0:
                status += "; auf 0 begrenzt"
        corrected_rows.append((replace(f, area=corrected_area), p, secondary, match_status))
        audit[f.peak] = BlankCorrection(f.peak, f.area, blank_area, blank_istd_area,
                                        subtracted_area, corrected_area, protected, status)
    return corrected_rows, audit


def load_blank_reference(path: str, s: Settings):
    """Parse and assign an optional blank reference independently."""
    if not path:
        return [], None
    tic, fid, pbm = parse_results(path)
    delay = estimate_delay(tic, fid, s.solvent_end)
    return assign(fid, pbm, delay, s), delay

def find_standard(assigned_rows, target_rt, name):
    name_low = name.lower()
    by_name = []
    for f, p, _, _ in assigned_rows:
        if p and p.hits and name_low in p.hits[0].name.lower():
            by_name.append((abs(p.rt-target_rt), f, p))
    if by_name:
        return min(by_name, key=lambda x:x[0])[1:]
    near = [(abs(f.rt-target_rt), f, p) for f,p,_,_ in assigned_rows if abs(f.rt-target_rt) <= 0.08]
    return min(near, key=lambda x:x[0])[1:] if near else (None, None)


def make_workbook(results_path, lib_path, output_path, s: Settings):
    """Create the original detailed workbook, with linked parameters and a static
    0.01 mg/kg inclusion filter applied at generation time.

    Parameter changes recalculate concentrations for retained rows. To reconsider
    previously excluded peaks after changing parameters, run the analysis again.
    """
    tic, fid, pbm = parse_results(results_path)
    detailed = parse_library_report(lib_path) if lib_path else {}
    for p in pbm:
        if detailed.get(p.peak):
            p.hits = detailed[p.peak]
    delay = estimate_delay(tic, fid, s.solvent_end)
    assigned = assign(fid, pbm, delay, s)

    # Find standards and calculate current factor for the generation-time filter.
    standards = []
    factors = []
    conc_lookup = {"fc17_conc": s.fc17_conc, "bbp_conc": s.bbp_conc, "dnnp_conc": s.dnnp_conc}
    for name, rt, role, conc_attr in IS_DEFS:
        f, p = find_standard(assigned, rt, name)
        conc = conc_lookup.get(conc_attr) if conc_attr else None
        factor = None
        if role == "Quantification" and f and f.area and s.cell_area_dm2 and s.coverage:
            factor = conc * s.is_amount / 1000 / f.area / s.cell_area_dm2 / s.coverage
            factors.append(factor)
        status = "Found" if f else "Not found"
        if role == "QC" and f and s.qc_min_area > 0 and f.area < s.qc_min_area:
            status = "Below QC minimum"
        standards.append((name, rt, role, f, p, conc_attr, status))
    mean_factor = statistics.mean(factors) if len(factors) == 3 else None
    threshold = 0.01

    wb = Workbook()
    ws = wb.active
    ws.title = "Zuordnung"
    wsr = wb.create_sheet("Manuell_pruefen")
    wsi = wb.create_sheet("Interne_Standards")
    wsp = wb.create_sheet("Parameter")
    wsa = wb.create_sheet("Alle_PBM_Treffer")

    # Linked parameter sheet.
    wsp.append(["Parameter", "Wert", "Einheit / Hinweis"])
    params = [
        ("Grenzwert", threshold, "mg/kg; Filter wird beim Erstellen angewendet"),
        ("Lösungsmittelende", s.solvent_end, "min"),
        ("Quality-Grenze", s.quality_limit, ""),
        ("RT-Toleranz", s.rt_tolerance, "min"),
        ("Ermittelter FID-MS-Delay", delay, "min"),
        ("Zellfläche", s.cell_area_dm2, "dm²"),
        ("Belegung", s.coverage, ""),
        ("O/V-Ratio", s.ov_ratio, ""),
        ("Menge interner Standard", s.is_amount, ""),
        ("FC17 Konzentration", s.fc17_conc, "mg/mL"),
        ("BBP-d4 Konzentration", s.bbp_conc, "mg/mL"),
        ("DnNP-d4 Konzentration", s.dnnp_conc, "mg/mL"),
        ("QC Mindestfläche", s.qc_min_area, "0 = nur Erkennung"),
        ("Hinweis", "Bei geänderten Parametern neu auswerten, damit ausgeschlossene Peaks erneut geprüft werden.", "")
    ]
    for row in params:
        wsp.append(row)

    # Standards linked to parameter values.
    wsi.append(["Name", "Soll-RT [min]", "Rolle", "FID Peak", "FID RT [min]", "FID Fläche",
                "Konz. [mg/mL]", "Einzelfaktor", "Abw. vom Mittel [%]", "Status"])
    conc_refs = {"fc17_conc": "Parameter!$B$11", "bbp_conc": "Parameter!$B$12",
                 "dnnp_conc": "Parameter!$B$13"}
    quant_rows = []
    for row_no, (name, rt, role, f, p, conc_attr, status) in enumerate(standards, start=2):
        conc_formula = f"={conc_refs[conc_attr]}" if conc_attr else None
        factor_formula = None
        if role == "Quantification" and f:
            factor_formula = (f"=G{row_no}*Parameter!$B$10/1000/F{row_no}/"
                              f"Parameter!$B$7/Parameter!$B$8")
            quant_rows.append(row_no)
        wsi.append([name, rt, role, f.peak if f else None, f.rt if f else None,
                    f.area if f else None, conc_formula, factor_formula, None, status])
    mean_row = len(standards) + 3
    wsi.cell(mean_row, 7, "Mittelwert Quantifizierungsfaktor")
    wsi.cell(mean_row, 8, "=AVERAGE(" + ",".join(f"H{r}" for r in quant_rows) + ")")
    for r in quant_rows:
        wsi.cell(r, 9, f'=IFERROR((H{r}/$H${mean_row}-1)*100,"")')

    # Original detailed columns from version 1.
    headers = ["RT / min", "Name", "CAS-#", "%match", "Fläche", "Conc. mg/dm²",
               "Conc. mg/kg", "Review", "ID Status", "Zuordnungsstatus", "Nebenkomponenten",
               "FID Peak", "PBM Peak", "MS RT [min]", "korr. MS RT [min]", "Delta Apex [min]",
               "Start [min]", "End [min]", "Breite [min]", "FID Typ", "FID Höhe",
               "Fläche/Höhe", "Quant.-Faktor"]
    ws.append(headers)
    wsr.append(headers)

    kept = 0
    for f, p, secondary, match_status in assigned:
        if not p or mean_factor is None:
            continue
        current_mgkg = f.area * mean_factor * s.ov_ratio
        if current_mgkg < threshold:
            continue
        kept += 1
        name, id_status, cas = display_identification(p, s.quality_limit)
        qual = p.hits[0].quality if p.hits else None
        corr = p.rt + delay
        delta = corr - f.rt
        sec = "; ".join(
            f"{x.rt:.4f}: {x.hits[0].name if x.hits else 'unknown'} "
            f"(Q={x.hits[0].quality if x.hits else ''})" for x in secondary)
        review = []
        if id_status != "Accepted": review.append("unsichere Identifikation")
        if secondary: review.append("Koelution; Fläche nur Hauptkomponente")
        row_no = ws.max_row + 1
        # Linked formulas. Raw imported values remain fixed; derived values reference parameters.
        qfactor = f"=Interne_Standards!$H${mean_row}"
        conc_dm2 = f"=E{row_no}*W{row_no}"
        conc_kg = f"=F{row_no}*Parameter!$B$9"
        # Requested report-first order, followed by identification/matching details,
        # then technical chromatographic fields.
        row = [f.rt, name, cas, qual, f.area, conc_dm2, conc_kg, "; ".join(review),
               id_status, match_status, sec, f.peak, p.peak, p.rt, corr, delta,
               f.start, f.end, f.width_min, f.pk_type, f.height, f.area_height, qfactor]
        ws.append(row)
        if review:
            wsr.append(row)
            rr = wsr.max_row
            wsr.cell(rr, 23, f"=Interne_Standards!$H${mean_row}")
            wsr.cell(rr, 6, f"=E{rr}*W{rr}")
            wsr.cell(rr, 7, f"=F{rr}*Parameter!$B$9")

    wsa.append(["PBM Peak", "MS RT [min]", "Area %", "Hit 1", "CAS 1", "Q1",
                "Hit 2", "CAS 2", "Q2", "Hit 3", "CAS 3", "Q3"])
    for p in pbm:
        vals = [p.peak, p.rt, p.area_pct]
        for i in range(3):
            h = p.hits[i] if i < len(p.hits) else Hit("")
            vals += [h.name, h.cas, h.quality]
        wsa.append(vals)

    # Formatting close to version 1.
    header_fill = PatternFill("solid", fgColor="1F4E78")
    orange = PatternFill("solid", fgColor="FCE4D6")
    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for col in range(1, sheet.max_column + 1):
            vals = [str(sheet.cell(r, col).value or "") for r in range(1, min(sheet.max_row, 200) + 1)]
            sheet.column_dimensions[get_column_letter(col)].width = min(max(max(map(len, vals)) + 2, 10), 45)
        for row in sheet.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(vertical="top", wrap_text=isinstance(c.value, str) and len(c.value) > 25)
    for r in range(2, 14):
        wsp.cell(r, 2).font = Font(color="0000FF")
    for row in wsr.iter_rows(min_row=2):
        for c in row: c.fill = orange
    for sheet in [ws, wsr, wsi, wsa]:
        if sheet.max_row > 1:
            ref = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
            tab = Table(displayName="T_" + re.sub(r"\W", "", sheet.title), ref=ref)
            tab.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True,
                                                showFirstColumn=False, showLastColumn=False)
            sheet.add_table(tab)
    for sheet in [ws, wsr]:
        for col in ["A", "N", "O", "P", "Q", "R", "S"]:
            for cell in sheet[col][1:]: cell.number_format = "0.0000"
        for col in ["W"]:
            for cell in sheet[col][1:]: cell.number_format = "0.000E+00"
        for col in ["F", "G"]:
            for cell in sheet[col][1:]: cell.number_format = "0.000000"
        for col in ["E", "U"]:
            for cell in sheet[col][1:]: cell.number_format = "0"
    try:
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
        wb.calculation.calcMode = "auto"
    except Exception:
        pass
    wb.save(output_path)
    return {"output": output_path, "delay": delay, "fid": len(fid), "pbm": len(pbm),
            "assigned": sum(1 for _, p, _, _ in assigned if p), "above_limit": kept,
            "mean_factor": mean_factor}



def _normalise_identity(name: str) -> str:
    """Normalise a library name for duplicate matching without changing output text."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def analyse_fingerprint(results_path: str, lib_path: str, s: Settings):
    """Analyse one sample without standards or concentration calculations."""
    tic, fid, pbm = parse_results(results_path)
    detailed = parse_library_report(lib_path) if lib_path else {}
    for peak in pbm:
        if detailed.get(peak.peak):
            peak.hits = detailed[peak.peak]

    delay = estimate_delay(tic, fid, s.solvent_end)
    assigned = assign(fid, pbm, delay, s)
    peaks = []
    for fid_peak, pbm_peak, secondary, match_status in assigned:
        if not pbm_peak:
            continue
        name, id_status, cas = display_identification(pbm_peak, s.quality_limit)
        review = []
        if id_status != "Accepted":
            review.append("unsichere Identifikation")
        if secondary:
            review.append("Koelution; Area % bezieht sich auf die PBM-Hauptkomponente")
        peaks.append({
            "rt": fid_peak.rt,
            "name": name,
            "cas": cas,
            "quality": pbm_peak.hits[0].quality if pbm_peak.hits else None,
            "area_pct": pbm_peak.area_pct,
            "id_status": id_status,
            "match_status": match_status,
            "review": "; ".join(review),
            "fid_peak": fid_peak.peak,
            "pbm_peak": pbm_peak.peak,
            "ms_rt": pbm_peak.rt,
            "corrected_ms_rt": pbm_peak.rt + delay,
            "fid_area": fid_peak.area,
            "secondary": secondary,
            "also_in_blank": False,
            "blank_match": None,
        })
    return {
        "peaks": peaks,
        "pbm": pbm,
        "delay": delay,
        "fid_count": len(fid),
        "pbm_count": len(pbm),
    }


def _same_fingerprint_identity(sample_peak, blank_peak):
    if sample_peak["id_status"] != "Accepted" or blank_peak["id_status"] != "Accepted":
        return False
    if sample_peak["cas"] and blank_peak["cas"]:
        return sample_peak["cas"].strip() == blank_peak["cas"].strip()
    sample_name = _normalise_identity(sample_peak["name"])
    return bool(sample_name) and sample_name == _normalise_identity(blank_peak["name"])


def match_fingerprint_blank(sample_peaks, blank_peaks, rt_tolerance: float):
    """Flag one-to-one blank matches without modifying sample Area %."""
    candidates = []
    for sample_index, sample_peak in enumerate(sample_peaks):
        for blank_index, blank_peak in enumerate(blank_peaks):
            rt_delta = abs(sample_peak["rt"] - blank_peak["rt"])
            if rt_delta > rt_tolerance:
                continue
            identity_match = _same_fingerprint_identity(sample_peak, blank_peak)
            conflicting_ids = (
                sample_peak["id_status"] == "Accepted"
                and blank_peak["id_status"] == "Accepted"
                and not identity_match
            )
            if conflicting_ids:
                continue
            candidates.append((0 if identity_match else 1, rt_delta, sample_index, blank_index))

    used_samples, used_blanks = set(), set()
    for priority, rt_delta, sample_index, blank_index in sorted(candidates):
        if sample_index in used_samples or blank_index in used_blanks:
            continue
        sample_peak = sample_peaks[sample_index]
        blank_peak = blank_peaks[blank_index]
        sample_peak["also_in_blank"] = True
        sample_peak["blank_match"] = {
            "rt": blank_peak["rt"],
            "name": blank_peak["name"],
            "cas": blank_peak["cas"],
            "area_pct": blank_peak["area_pct"],
            "rt_delta": rt_delta,
            "method": "Identität + RT" if priority == 0 else "RT",
        }
        blank_note = "auch im Blank"
        sample_peak["review"] = "; ".join(
            note for note in (sample_peak["review"], blank_note) if note
        )
        used_samples.add(sample_index)
        used_blanks.add(blank_index)
    return sample_peaks


def discover_fingerprint_samples(batch_folder: str, excluded_results_path: str = ""):
    """Find one RESULTS.CSV and at most one LIBresults.csv per child folder."""
    root = Path(batch_folder)
    if not root.is_dir():
        raise ValueError("Der ausgewählte Batch-Ordner ist nicht gültig.")
    excluded = Path(excluded_results_path).resolve() if excluded_results_path else None

    samples, issues = [], []
    for sample_folder in sorted((path for path in root.iterdir() if path.is_dir()),
                                key=lambda path: path.name.casefold()):
        files = [path for path in sample_folder.iterdir() if path.is_file()]
        results_files = [path for path in files if path.name.casefold() == "results.csv"]
        library_files = [path for path in files if path.name.casefold() == "libresults.csv"]
        errors = []
        if len(results_files) != 1:
            errors.append(f"{len(results_files)} RESULTS.CSV-Dateien gefunden; genau 1 erforderlich")
        if len(library_files) > 1:
            errors.append(f"{len(library_files)} LIBresults.csv-Dateien gefunden; höchstens 1 erlaubt")
        if errors:
            issues.append({"sample": sample_folder.name, "error": "; ".join(errors)})
            continue
        if excluded and results_files[0].resolve() == excluded:
            continue
        samples.append({
            "name": sample_folder.name,
            "folder": sample_folder,
            "results": results_files[0],
            "library": library_files[0] if library_files else None,
        })
    return samples, issues


def _safe_output_stem(value: str):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._") or "Sample"


def make_fingerprint_workbook(data, output_path: str, s: Settings, sample_name: str,
                              results_path: str, lib_path: str = "", blank_path: str = "",
                              blank_delay=None):
    """Write one semi-quantitative fingerprint workbook using PBM Area %."""
    wb = Workbook()
    wb.properties.title = f"{sample_name} - Fingerprint Screening"
    ws = wb.active
    ws.title = "Fingerprint"
    wsr = wb.create_sheet("Manuell_pruefen")
    wsp = wb.create_sheet("Parameter")
    wsa = wb.create_sheet("Alle_PBM_Treffer")
    wsb = wb.create_sheet("Blankabgleich")

    headers = ["RT [min]", "Name", "CAS", "Quality / % match", "PBM Area %",
               "Im Blank", "Identifikationsstatus", "Zuordnungsstatus", "Review",
               "FID Peak", "PBM Peak", "MS RT [min]", "Korr. MS RT [min]", "FID Fläche"]
    ws.append(headers)
    wsr.append(headers)
    for peak in data["peaks"]:
        row = [peak["rt"], peak["name"], peak["cas"], peak["quality"], peak["area_pct"],
               "Ja" if peak["also_in_blank"] else "Nein", peak["id_status"],
               peak["match_status"], peak["review"], peak["fid_peak"], peak["pbm_peak"],
               peak["ms_rt"], peak["corrected_ms_rt"], peak["fid_area"]]
        ws.append(row)
        if peak["id_status"] != "Accepted" or peak["review"]:
            wsr.append(row)

    parameters = [
        ("Modus", "Fingerprint Screening", ""),
        ("Probe", sample_name, "Ordnername"),
        ("RESULTS.CSV", results_path, ""),
        ("LIBresults.csv", lib_path or "nicht verwendet", ""),
        ("Lösungsmittelende", s.solvent_end, "min"),
        ("Quality-Grenze", s.quality_limit, ""),
        ("FID/PBM RT-Toleranz", s.rt_tolerance, "min"),
        ("Ermittelter FID-MS-Delay", data["delay"], "min"),
        ("Blank RESULTS.CSV", blank_path or "nicht verwendet", ""),
        ("Blank RT-Toleranz", s.blank_rt_tolerance, "min"),
        ("Blank FID-MS-Delay", blank_delay if blank_delay is not None else "", "min"),
        ("Mengeninformation", "PBM Area %", "semiquantitativ; keine Konzentration berechnet"),
    ]
    wsp.append(["Parameter", "Wert", "Einheit / Hinweis"])
    for row in parameters:
        wsp.append(row)

    wsa.append(["PBM Peak", "MS RT [min]", "Area %", "Hit 1", "CAS 1", "Q1",
                "Hit 2", "CAS 2", "Q2", "Hit 3", "CAS 3", "Q3"])
    for peak in data["pbm"]:
        values = [peak.peak, peak.rt, peak.area_pct]
        for index in range(3):
            hit = peak.hits[index] if index < len(peak.hits) else Hit("")
            values += [hit.name, hit.cas, hit.quality]
        wsa.append(values)

    wsb.append(["FID Peak", "Proben-RT [min]", "Probenname", "Proben-CAS",
                "Proben Area %", "Im Blank", "Blank-RT [min]", "Blankname", "Blank-CAS",
                "Blank Area %", "RT-Delta [min]", "Methode"])
    for peak in data["peaks"]:
        blank_match = peak["blank_match"] or {}
        wsb.append([peak["fid_peak"], peak["rt"], peak["name"], peak["cas"],
                    peak["area_pct"], "Ja" if peak["also_in_blank"] else "Nein",
                    blank_match.get("rt"), blank_match.get("name"), blank_match.get("cas"),
                    blank_match.get("area_pct"), blank_match.get("rt_delta"),
                    blank_match.get("method")])

    header_fill = PatternFill("solid", fgColor="1F4E78")
    orange = PatternFill("solid", fgColor="FCE4D6")
    manual_review_fill = PatternFill("solid", fgColor="FFC7CE")
    manual_review_font = Font(color="9C0006")
    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for column in range(1, sheet.max_column + 1):
            values = [str(sheet.cell(row, column).value or "")
                      for row in range(1, min(sheet.max_row, 200) + 1)]
            sheet.column_dimensions[get_column_letter(column)].width = min(
                max(max(map(len, values)) + 2, 10), 45)
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(
                    vertical="top", wrap_text=isinstance(cell.value, str) and len(cell.value) > 25)

    for row in ws.iter_rows(min_row=2):
        if row[6].value != "Accepted" or row[8].value:
            for cell in row:
                cell.fill = manual_review_fill
                cell.font = manual_review_font
    for row in wsr.iter_rows(min_row=2):
        for cell in row:
            cell.fill = orange
    for row in wsb.iter_rows(min_row=2):
        if row[5].value == "Ja":
            for cell in row:
                cell.fill = PatternFill("solid", fgColor="FFF2CC")

    for sheet in (ws, wsr, wsa, wsb):
        if sheet.max_row > 1:
            reference = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
            table = Table(displayName="T_" + re.sub(r"\W", "", sheet.title), ref=reference)
            table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True,
                                                  showFirstColumn=False, showLastColumn=False)
            sheet.add_table(table)
    for sheet in (ws, wsr):
        for column in ("A", "L", "M"):
            for cell in sheet[column][1:]:
                cell.number_format = "0.0000"
        for cell in sheet["E"][1:]:
            cell.number_format = "0.0000"
    for row in wsb.iter_rows(min_row=2):
        for index in (1, 6, 10):
            row[index].number_format = "0.0000"
        for index in (4, 9):
            row[index].number_format = "0.0000"

    wb.save(output_path)
    return {"output": output_path, "reported": len(data["peaks"]),
            "blank_matches": sum(1 for peak in data["peaks"] if peak["also_in_blank"])}


def fingerprint_output_paths(samples, output_folder: str):
    output_root = Path(output_folder)
    used_names = set()
    paths = {}
    for sample in samples:
        stem = _safe_output_stem(sample["name"])
        candidate = stem
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{suffix}"
            suffix += 1
        used_names.add(candidate.casefold())
        paths[sample["name"]] = output_root / f"{candidate}_Fingerprint_Screening.xlsx"
    return paths


def process_fingerprint_batch(batch_folder: str, output_folder: str, s: Settings,
                              blank_path: str = "", overwrite: bool = False,
                              progress=None):
    """Create independent fingerprint workbooks and retain partial successes."""
    samples, discovery_issues = discover_fingerprint_samples(batch_folder, blank_path)
    output_root = Path(output_folder)
    if not output_root.is_dir():
        raise ValueError("Der ausgewählte Ausgabeordner ist nicht gültig.")

    blank_data = analyse_fingerprint(blank_path, "", s) if blank_path else None
    created, skipped, failed = [], [], list(discovery_issues)
    output_paths = fingerprint_output_paths(samples, output_folder)
    total = len(samples)
    for index, sample in enumerate(samples, start=1):
        if progress:
            progress(index, total, sample["name"])
        output_path = output_paths[sample["name"]]
        if output_path.exists() and not overwrite:
            skipped.append({"sample": sample["name"], "output": str(output_path)})
            continue
        try:
            data = analyse_fingerprint(str(sample["results"]),
                                       str(sample["library"]) if sample["library"] else "", s)
            if blank_data:
                match_fingerprint_blank(data["peaks"], blank_data["peaks"],
                                        s.blank_rt_tolerance)
            info = make_fingerprint_workbook(
                data, str(output_path), s, sample["name"], str(sample["results"]),
                str(sample["library"]) if sample["library"] else "", blank_path,
                blank_data["delay"] if blank_data else None)
            created.append({"sample": sample["name"], **info})
        except Exception as exc:
            failed.append({"sample": sample["name"], "error": str(exc)})
    return {"created": created, "skipped": skipped, "failed": failed,
            "discovered": len(samples), "discovery_issues": discovery_issues}


def analyse_determination(results_path: str, lib_path: str, s: Settings,
                            blank_rows=None, blank_istd_rows=None,
                            require_standards: bool = True):
    """Run one determination and return peak-level, standard and PBM information.

    ``require_standards=False`` is the batch Doppelbestimmung, whose ISTD is
    chosen per row in the workbook: a missing standard is then not an error,
    ``mean_factor`` is the mean of the factors found (``None`` without any)
    and the peaks carry no concentration where there is no factor.
    """
    tic, fid, pbm = parse_results(results_path)
    detailed = parse_library_report(lib_path) if lib_path else {}
    for p in pbm:
        if detailed.get(p.peak):
            p.hits = detailed[p.peak]

    delay = estimate_delay(tic, fid, s.solvent_end)
    assigned_raw = assign(fid, pbm, delay, s)
    assigned, blank_audit = subtract_blank_signals(
        assigned_raw, blank_rows, blank_istd_rows, s.blank_rt_tolerance)
    conc_lookup = {"fc17_conc": s.fc17_conc, "bbp_conc": s.bbp_conc, "dnnp_conc": s.dnnp_conc}
    standards, factors = [], []
    for name, rt, role, conc_attr in IS_DEFS:
        f, p = find_standard(assigned, rt, name)
        conc = conc_lookup.get(conc_attr) if conc_attr else None
        factor = None
        if role == "Quantification" and f and f.area and s.cell_area_dm2 and s.coverage:
            factor = conc * s.is_amount / 1000 / f.area / s.cell_area_dm2 / s.coverage
            factors.append(factor)
        status = "Found" if f else "Not found"
        if role == "QC" and f and s.qc_min_area > 0 and f.area < s.qc_min_area:
            status = "Below QC minimum"
        standards.append({"name": name, "target_rt": rt, "role": role, "fid": f,
                          "pbm": p, "concentration": conc, "factor": factor, "status": status})

    mean_factor = statistics.mean(factors) if len(factors) == 3 else None
    if mean_factor is None and not require_standards:
        mean_factor = statistics.mean(factors) if factors else None
    elif mean_factor is None:
        raise ValueError(
            f"In {Path(results_path).name} wurden nicht alle drei Quantifizierungsstandards gefunden. "
            "Eine Doppelbestimmung kann daher nicht zuverlässig berechnet werden."
        )

    peaks = []
    for f, p, secondary, match_status in assigned:
        if not p:
            continue
        name, id_status, cas = display_identification(p, s.quality_limit)
        qual = p.hits[0].quality if p.hits else None
        review = []
        if id_status != "Accepted": review.append("unsichere Identifikation")
        if secondary: review.append("Koelution; Fläche nur Hauptkomponente")
        correction = blank_audit[f.peak]
        peaks.append({
            "rt": f.rt, "name": name, "cas": cas, "quality": qual,
            "area": f.area,
            "mg_dm2": f.area * mean_factor if mean_factor is not None else None,
            "mg_kg": f.area * mean_factor * s.ov_ratio if mean_factor is not None else None,
            "id_status": id_status, "match_status": match_status,
            "review": "; ".join(review), "fid_peak": f.peak, "pbm_peak": p.peak,
            "ms_rt": p.rt, "corrected_ms_rt": p.rt + delay,
            "secondary": secondary, "raw_area": correction.raw_area,
            "blank_area": correction.blank_area, "blank_istd_area": correction.blank_istd_area,
            "subtracted_area": correction.subtracted_area,
            "blank_status": correction.status, "protected_standard": correction.protected_standard,
        })
    return {"peaks": peaks, "standards": standards, "pbm": pbm, "delay": delay,
            "mean_factor": mean_factor, "fid_count": len(fid), "pbm_count": len(pbm),
            "blank_audit": blank_audit,
            "blank_corrected": sum(1 for x in blank_audit.values() if x.subtracted_area)}


def _same_identity(a, b):
    """Accepted identities match by CAS where possible, otherwise by normalised name."""
    if a["id_status"] != "Accepted" or b["id_status"] != "Accepted":
        return False
    if a["cas"] and b["cas"]:
        return a["cas"].strip() == b["cas"].strip()
    return bool(_normalise_identity(a["name"])) and _normalise_identity(a["name"]) == _normalise_identity(b["name"])


#: Duplicate status of a row that has only one determination behind it because
#: the analysis itself is a single determination (spec v2.1 SS V.6). Distinct
#: from "Artefact: only determination 1", which means a peak went missing in a
#: Doppelbestimmung and is therefore a finding rather than a decision.
SINGLE_DETERMINATION_STATUS = "Einzelbestimmung"

#: Columns of "Doppelbestimmung"/"Manuell_pruefen" that belong to the second
#: determination. For a single determination they are written empty and hidden
#: instead of being removed, so the layout stays the one layout (SS V.6).
SINGLE_DETERMINATION_HIDDEN_COLUMNS = ("F", "G")

#: The same columns by header name, for a consumer that reorders or inserts
#: columns and therefore cannot rely on the letters above. "Area 2" is not
#: written here but is inserted by the NIAS post-processing step.
SINGLE_DETERMINATION_HIDDEN_HEADERS = (
    "Area 2", "Concentration 2 [mg/kg]", "Relative difference [%]")


def single_determination_rows(peaks):
    """Result rows of an analysis that consists of one determination.

    Same row shape and the same keys as ``combine_determinations``, so the
    Doppelbestimmung sheet, the review sheet and the workspace's merged view all
    keep reading one layout. The second determination simply has no values:
    ``c2`` and ``reldiff`` stay ``None`` and ``mean`` is the one measured
    concentration rather than the average of two.
    """
    rows = []
    for a in sorted(peaks, key=lambda x: x["rt"]):
        status = SINGLE_DETERMINATION_STATUS
        if a.get("blank_area") or a.get("blank_istd_area"):
            status += ", also in Blank"
        rows.append({"rt": a["rt"], "name": a["name"], "cas": a["cas"],
                     "mean": a["mg_kg"], "c1": a["mg_kg"], "c2": None,
                     "reldiff": None, "status": status,
                     "id_status": a["id_status"], "review": a["review"],
                     "source1": a, "source2": None})
    return rows


def combine_determinations(peaks1, peaks2, tolerance: float):
    """Pair duplicate peaks. Identity is primary; RT is used for unknowns and conflict detection.

    ``peaks2`` is ``None`` for an analysis with a single determination (spec
    v2.1 SS V.6). Pairing is then meaningless, so the rows come from
    ``single_determination_rows`` instead -- with identical keys, so no consumer
    has to learn a second shape.
    """
    if peaks2 is None:
        return single_determination_rows(peaks1)
    remaining = set(range(len(peaks2)))
    combined = []
    for a in sorted(peaks1, key=lambda x: x["rt"]):
        close = [j for j in remaining if abs(peaks2[j]["rt"] - a["rt"]) <= tolerance]
        identity_matches = [j for j in close if _same_identity(a, peaks2[j])]
        if identity_matches:
            j = min(identity_matches, key=lambda k: abs(peaks2[k]["rt"] - a["rt"]))
            status = "Valid duplicate"
            conflict = False
        elif close:
            j = min(close, key=lambda k: abs(peaks2[k]["rt"] - a["rt"]))
            b0 = peaks2[j]
            both_accepted = a["id_status"] == "Accepted" and b0["id_status"] == "Accepted"
            conflict = both_accepted and not _same_identity(a, b0)
            status = "Identification conflict" if conflict else "Valid duplicate"
        else:
            j = None
            conflict = False
            status = "Artefact: only determination 1"

        b = peaks2[j] if j is not None else None
        if j is not None:
            remaining.remove(j)

        # Document blank correction directly in the duplicate status. A peak is
        # considered present in the blank when an area from Blank and/or
        # Blank+ISTD was subtracted in either determination.
        also_in_blank = bool(
            a.get("blank_area") or a.get("blank_istd_area") or
            (b and (b.get("blank_area") or b.get("blank_istd_area")))
        )
        if also_in_blank:
            status += ", also in Blank"

        if b is None:
            combined.append({"rt": a["rt"], "name": a["name"], "cas": a["cas"], "mean": None,
                             "c1": a["mg_kg"], "c2": None, "reldiff": None, "status": status,
                             "id_status": a["id_status"], "review": "Nur in Bestimmung 1 detektiert; als Artefakt bewertet",
                             "source1": a, "source2": None})
            continue

        mean_rt = statistics.mean([a["rt"], b["rt"]])
        # A determination without a quantifying ISTD has no mg/kg yet; the
        # workbook computes it once the analyst marks one.
        present_c = [x for x in (a["mg_kg"], b["mg_kg"]) if x is not None]
        mean_c = statistics.mean(present_c) if present_c else None
        if len(present_c) == 2:
            rel = abs(a["mg_kg"] - b["mg_kg"]) / mean_c * 100 if mean_c else 0.0
        else:
            rel = None
        reviews = [x for x in [a["review"], b["review"]] if x]
        if conflict:
            name = f"{a['name']} / {b['name']}"
            cas = f"{a['cas']} / {b['cas']}".strip(" /")
            id_status = "Conflict; manual review"
            reviews.append("Abweichende Identifikationen bei vergleichbarer RT; keine automatische Endidentifikation")
        else:
            if a["id_status"] == "Accepted":
                name, cas = a["name"], a["cas"]
            elif b["id_status"] == "Accepted":
                name, cas = b["name"], b["cas"]
            else:
                name, cas = a["name"], a["cas"] or b["cas"]
            id_status = "Accepted" if a["id_status"] == b["id_status"] == "Accepted" else "Manual review"
        combined.append({"rt": mean_rt, "name": name, "cas": cas, "mean": mean_c,
                         "c1": a["mg_kg"], "c2": b["mg_kg"], "reldiff": rel,
                         "status": status, "id_status": id_status, "review": "; ".join(dict.fromkeys(reviews)),
                         "source1": a, "source2": b})

    for j in sorted(remaining, key=lambda k: peaks2[k]["rt"]):
        b = peaks2[j]
        status = "Artefact: only determination 2"
        if b.get("blank_area") or b.get("blank_istd_area"):
            status += ", also in Blank"
        combined.append({"rt": b["rt"], "name": b["name"], "cas": b["cas"], "mean": None,
                         "c1": None, "c2": b["mg_kg"], "reldiff": None,
                         "status": status, "id_status": b["id_status"],
                         "review": "Nur in Bestimmung 2 detektiert; als Artefakt bewertet",
                         "source1": None, "source2": b})
    return sorted(combined, key=lambda x: x["rt"])


#: Batch Doppelbestimmung: the ISTD is marked per row, as in the
#: Einzelbestimmung, but per determination. ``Bestimmung_n`` gets the dropdown
#: as its last column (E, G and U must not move: NIAS reads them), and
#: ``ISTD_n`` replaces ``Standards_n`` with the Einzelbestimmung's table plus
#: the quantification factor that column U of ``Bestimmung_n`` references.
DUPLICATE_ISTD_HEADER = "ISTD"
DUPLICATE_ISTD_COLUMN = "V"
DUPLICATE_ISTD_SHEET_PREFIX = "ISTD_"
DUPLICATE_ISTD_FACTOR_ROW = 8


def make_duplicate_workbook(results1, lib1, results2, lib2, output_path, s: Settings,
                            blank_path: str = "", blank_istd_path: str = "",
                            sample_name: str = ""):
    """Evaluate one or two determinations and create a fully parameter-linked workbook.

    An empty ``results2`` means a single determination (spec v2.1 SS V.6). The
    sheet layout does not change: the second determination's columns are written
    empty and hidden, so a consumer that reads by header name keeps working. Only
    ``Bestimmung_2``, ``Standards_2`` and ``PBM_2`` are left out, because an
    empty detail sheet documents nothing.
    """
    blank_rows, blank_delay = load_blank_reference(blank_path, s)
    blank_istd_rows, blank_istd_delay = load_blank_reference(blank_istd_path, s)
    # The ISTD is chosen per row in the workbook, so a standard the automatic
    # detection misses no longer aborts the sample; it is only not pre-filled.
    d1 = analyse_determination(results1, lib1, s, blank_rows, blank_istd_rows,
                               require_standards=False)
    d2 = (analyse_determination(results2, lib2, s, blank_rows, blank_istd_rows,
                                require_standards=False)
          if results2 else None)
    single = d2 is None
    # Only the determinations that exist; the loops below iterate this instead
    # of a hard-coded (1, 2) so a single determination writes one of everything.
    determinations = [(1, d1)] + ([] if single else [(2, d2)])
    combined = combine_determinations(d1["peaks"], None if single else d2["peaks"],
                                      s.rt_tolerance)
    threshold = 0.01

    wb = Workbook()
    wb.properties.title = sample_name or "NIAS Doppelbestimmung"
    ws = wb.active; ws.title = "Doppelbestimmung"
    wsr = wb.create_sheet("Manuell_pruefen")
    wsp = wb.create_sheet("Parameter")

    # Fixed parameter positions. All concentration formulas reference these cells.
    parameters = [
        ("Sample name", sample_name, "used for result file and workbook identification"),
        ("Reporting limit", threshold, "mg/kg; applied at workbook generation"),
        ("Solvent end", s.solvent_end, "min"),
        ("Quality threshold", s.quality_limit, ""),
        ("Duplicate RT tolerance", s.rt_tolerance, "min"),
        ("FID-MS delay determination 1", d1["delay"], "min"),
        # The row itself must stay: every concentration formula references
        # Parameter!$B$9 and following absolutely, so no row may move.
        ("FID-MS delay determination 2", "" if single else d2["delay"], "min"),
        ("Cell area", s.cell_area_dm2, "dm2"),
        ("Coverage", s.coverage, ""),
        ("O/V ratio", s.ov_ratio, ""),
        ("Internal standard amount", s.is_amount, ""),
        ("FC17 concentration", s.fc17_conc, "mg/mL"),
        ("BBP-d4 concentration", s.bbp_conc, "mg/mL"),
        ("DnNP-d4 concentration", s.dnnp_conc, "mg/mL"),
        ("Blank RT tolerance", s.blank_rt_tolerance, "min; one-to-one FID apex matching"),
        ("Blank file", blank_path or "not used", ""),
        ("Blank+ISTD file", blank_istd_path or "not used", ""),
        ("Blank FID-MS delay", blank_delay if blank_delay is not None else "", "min"),
        ("Blank+ISTD FID-MS delay", blank_istd_delay if blank_istd_delay is not None else "", "min"),
        ("Note", "Mark the IS1..IS4 peaks in column 'ISTD' of 'Bestimmung_1' and "
                 "'Bestimmung_2' (pre-filled from the automatic detection). Each "
                 "determination is quantified with the mean of its IS1..IS3 (sheets "
                 "'ISTD_1'/'ISTD_2'). Change parameters in blue cells; concentrations, "
                 "means and relative differences recalculate automatically.", "")]
    wsp.append(["Parameter", "Value", "Unit / note"])
    for row in parameters: wsp.append(row)
    # Excel references after header: cell area B9, coverage B10, O/V B11, amount B12, concentrations B13:B15.
    param_ref = {"cell_area": "Parameter!$B$9", "coverage": "Parameter!$B$10",
                 "ov_ratio": "Parameter!$B$11", "is_amount": "Parameter!$B$12",
                 "fc17_conc": "Parameter!$B$13", "bbp_conc": "Parameter!$B$14",
                 "dnnp_conc": "Parameter!$B$15"}

    detail_headers = ["RT [min]", "Name", "CAS", "Quality", "Blank-corrected FID area",
                      "mg/dm2", "mg/kg", "Identification status", "Match status", "Review",
                      "FID peak", "PBM peak", "MS RT [min]", "Corrected MS RT [min]",
                      "Raw FID area", "Blank area", "Blank+ISTD area", "Subtracted area",
                      "Blank correction", "ISTD protected", "Quantification factor",
                      DUPLICATE_ISTD_HEADER]
    detail_rows = {1: {}, 2: {}}
    factor_cells = {}
    istd_found = {}
    label_of = {chemical: label for label, chemical, _row in SINGLE_ISTD_DEFAULTS}
    conc_refs = {"IS1": param_ref["fc17_conc"], "IS2": param_ref["bbp_conc"],
                 "IS3": param_ref["dnnp_conc"]}
    for number, data in determinations:
        wd = wb.create_sheet(f"Bestimmung_{number}"); wd.append(detail_headers)
        istd_sheet = f"{DUPLICATE_ISTD_SHEET_PREFIX}{number}"
        wi = wb.create_sheet(istd_sheet)
        factor_cells[number] = f"{istd_sheet}!$B${DUPLICATE_ISTD_FACTOR_ROW}"

        # Pre-fill: the peak the automatic detection took for each standard is
        # marked with its IS label. The analyst may change or clear any mark.
        found = {}
        for st in data["standards"]:
            if st["fid"] is not None and st["name"] in label_of:
                found[st["fid"].peak] = (label_of[st["name"]], st)
        marked = {}
        for x in data["peaks"]:
            r = wd.max_row + 1
            label = found.get(x["fid_peak"], (None, None))[0]
            if label in marked.values():
                label = None
            if label:
                marked[r] = label
            wd.append([x["rt"], x["name"], x["cas"], x["quality"], x["area"],
                       f'=IF(U{r}="","",E{r}*U{r})',
                       f'=IF(F{r}="","",F{r}*{param_ref["ov_ratio"]})',
                       x["id_status"], x["match_status"], x["review"], x["fid_peak"],
                       x["pbm_peak"], x["ms_rt"], x["corrected_ms_rt"], x["raw_area"],
                       x["blank_area"], x["blank_istd_area"], x["subtracted_area"],
                       x["blank_status"], "Ja" if x["protected_standard"] else "Nein",
                       f'=IF({factor_cells[number]}="","",{factor_cells[number]})',
                       label])
            detail_rows[number][x["fid_peak"]] = r
        istd_found[number] = sorted(marked.values())
        last_row = max(wd.max_row, 2)

        # ISTD_n: the same table as the Einzelbestimmung's "ISTD" sheet, per
        # determination. Area = the row marked with the label in column V.
        wi.append(["Name", "c [mg/mL]", "FID area", "Note"])
        label_range = f"Bestimmung_{number}!${DUPLICATE_ISTD_COLUMN}$2:${DUPLICATE_ISTD_COLUMN}${last_row}"
        area_range = f"Bestimmung_{number}!$E$2:$E${last_row}"
        by_label = {label: st for label, st in found.values()}
        for label, chemical, _parameter_row in SINGLE_ISTD_DEFAULTS:
            r = wi.max_row + 1
            st = by_label.get(label)
            if st is not None and label in marked.values():
                detection = (f"auto-detected at RT {st['fid'].rt:.3f} min, "
                             f"FID peak {st['fid'].peak}")
            else:
                detection = "not found automatically"
            note = f"{chemical}; {detection}; " + (
                "concentration from Parameter" if label in conc_refs else "enter concentration")
            if label not in SINGLE_ISTD_MEAN_OF:
                note += "; not part of the mean"
            wi.append([label, f"={conc_refs[label]}" if label in conc_refs else None,
                       f'=IFERROR(INDEX({area_range},MATCH(A{r},{label_range},0)),"")',
                       note + f"; area of the row marked {label} in column "
                              f"{DUPLICATE_ISTD_COLUMN} of Bestimmung_{number}"])
        first, last = 2, 1 + len(SINGLE_ISTD_MEAN_OF)
        m = SINGLE_ISTD_MEAN_ROW
        wi.cell(m, 1, f"Mean {SINGLE_ISTD_MEAN_OF[0]}-{SINGLE_ISTD_MEAN_OF[-1]}")
        wi.cell(m, 2, f'=IFERROR(AVERAGEIFS(B{first}:B{last},C{first}:C{last},">0"),"")')
        wi.cell(m, 3, f'=IFERROR(AVERAGEIF(C{first}:C{last},">0"),"")')
        wi.cell(m, 4, "mean over the marked IS only")
        f_row = DUPLICATE_ISTD_FACTOR_ROW
        wi.cell(f_row, 1, "Quantification factor")
        wi.cell(f_row, 2, (f'=IFERROR(B{m}*{param_ref["is_amount"]}/1000/C{m}/'
                           f'{param_ref["cell_area"]}/{param_ref["coverage"]},"")'))
        wi.cell(f_row, 4, f"mg/dm2 per area unit; used by every row of Bestimmung_{number}")

        if wd.max_row > 1:
            validation = DataValidation(type="list", formula1=f"={istd_sheet}!$A$2:$A$5",
                                        allow_blank=True)
            validation.error = "Bitte IS1, IS2, IS3 oder IS4 wählen."
            validation.errorTitle = "ISTD"
            validation.prompt = "Diese Zeile als IS1..IS4 markieren (Mittelwert IS1-IS3 quantifiziert)"
            validation.promptTitle = "ISTD"
            wd.add_data_validation(validation)
            validation.add(f"{DUPLICATE_ISTD_COLUMN}2:{DUPLICATE_ISTD_COLUMN}{wd.max_row}")

        wp = wb.create_sheet(f"PBM_{number}")
        wp.append(["PBM Peak", "MS RT [min]", "Area %", "Hit 1", "CAS 1", "Q1",
                   "Hit 2", "CAS 2", "Q2", "Hit 3", "CAS 3", "Q3"])
        for peak in data["pbm"]:
            vals = [peak.peak, peak.rt, peak.area_pct]
            for i in range(3):
                h = peak.hits[i] if i < len(peak.hits) else Hit("")
                vals += [h.name, h.cas, h.quality]
            wp.append(vals)

    # Dedicated audit view showing exactly which sample peaks were corrected.
    wsb = wb.create_sheet("Blankkorrektur")
    wsb.append(["Determination", "FID peak", "RT [min]", "Name", "Raw FID area",
                "Blank area", "Blank+ISTD area", "Subtracted area", "Corrected FID area",
                "Correction status", "ISTD protected", "Corrected?"])
    for number, data in determinations:
        for x in data["peaks"]:
            corrected = bool(x["subtracted_area"])
            wsb.append([number, x["fid_peak"], x["rt"], x["name"], x["raw_area"],
                        x["blank_area"], x["blank_istd_area"], x["subtracted_area"],
                        x["area"], x["blank_status"],
                        "Ja" if x["protected_standard"] else "Nein", "Ja" if corrected else "Nein"])

    headers = ["RT (mean)", "Name", "CAS", "mg/kg (mean)", "Concentration 1 [mg/kg]",
               "Concentration 2 [mg/kg]", "Relative difference [%]", "Duplicate status",
               "Identification status", "Review"]
    ws.append(headers); wsr.append(headers)
    # A row is settled when the duplicate itself raises no question. A single
    # determination is settled for the same reason a valid duplicate is: nothing
    # disagrees. It is not a finding, so it must not flood "Manuell_pruefen".
    settled_statuses = ("Valid duplicate", SINGLE_DETERMINATION_STATUS)
    kept = 0
    # Every row is written: its concentration depends on the IS the analyst
    # marks, so the reporting limit is applied by conditional formatting in the
    # sheet and by NIAS Report when it reads the file.
    for item in combined:
        s1, s2 = item.get("source1"), item.get("source2")
        c1 = f"=Bestimmung_1!G{detail_rows[1][s1['fid_peak']]}" if s1 else None
        c2 = f"=Bestimmung_2!G{detail_rows[2][s2['fid_peak']]}" if s2 else None

        def combined_row(target):
            """The ten result columns for one sheet, addressed to its own row."""
            r = target.max_row + 1
            if single:
                # The mean of one determination is that determination; writing
                # AVERAGE over an empty cell would read as an error in Excel.
                mean_formula = f"=E{r}" if s1 else None
                rel_formula = None
            else:
                mean_formula = f"=AVERAGE(E{r}:F{r})" if s1 and s2 else None
                rel_formula = f'=IFERROR(ABS(E{r}-F{r})/D{r}*100,"")' if s1 and s2 else None
            return [item["rt"], item["name"], item["cas"], mean_formula, c1, c2,
                    rel_formula, item["status"], item["id_status"], item["review"]]

        ws.append(combined_row(ws)); kept += 1
        if not item["status"].startswith(settled_statuses) or item["id_status"] != "Accepted" or item["review"]:
            wsr.append(combined_row(wsr))

    header_fill = PatternFill("solid", fgColor="1F4E78")
    orange = PatternFill("solid", fgColor="FCE4D6")
    manual_review_fill = PatternFill("solid", fgColor="FFC7CE")
    manual_review_font = Font(color="9C0006")
    green_font = Font(color="008000")
    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"; sheet.sheet_view.showGridLines = False
        for cell in sheet[1]:
            cell.fill = header_fill; cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for col in range(1, sheet.max_column + 1):
            vals = [str(sheet.cell(r, col).value or "") for r in range(1, min(sheet.max_row, 200) + 1)]
            sheet.column_dimensions[get_column_letter(col)].width = min(max(max(map(len, vals)) + 2, 10), 45)
        for row in sheet.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(vertical="top", wrap_text=isinstance(c.value, str) and len(c.value) > 25)
                if isinstance(c.value, str) and c.value.startswith("="): c.font = green_font
    # Mark substances requiring manual review red in the main
    # "Doppelbestimmung" sheet. This uses the same rule that copies rows to
    # "Manuell_pruefen", so both sheets remain consistent.
    for row in ws.iter_rows(min_row=2):
        duplicate_status = row[7].value
        identification_status = row[8].value
        review_note = row[9].value
        if (not str(duplicate_status).startswith(settled_statuses) or
                identification_status != "Accepted" or review_note):
            for c in row:
                c.fill = manual_review_fill
                c.font = manual_review_font

    for row in wsr.iter_rows(min_row=2):
        for c in row: c.fill = orange
    for row in wsb.iter_rows(min_row=2):
        if row[11].value == "Ja":
            for c in row: c.fill = PatternFill("solid", fgColor="FFF2CC")
    # User-editable parameter values are blue, matching the original workbook convention.
    for r in range(2, 16): wsp.cell(r, 2).font = Font(color="0000FF")
    istd_fill = PatternFill("solid", fgColor="FFF2CC")
    blue_font = Font(color="0000FF")
    istd_sheets = {f"{DUPLICATE_ISTD_SHEET_PREFIX}{n}" for n, _data in determinations}
    detail_sheets = {f"Bestimmung_{n}" for n, _data in determinations}
    for number, _data in determinations:
        wd = wb[f"Bestimmung_{number}"]
        wd.column_dimensions[DUPLICATE_ISTD_COLUMN].width = 12
        for cell in wd[DUPLICATE_ISTD_COLUMN][1:]:
            cell.fill = istd_fill
            cell.font = Font(color="000000")
        wi = wb[f"{DUPLICATE_ISTD_SHEET_PREFIX}{number}"]
        for r in range(2, SINGLE_ISTD_LAST_ROW + 1):
            for col in (1, 2, 3):
                wi.cell(r, col).font = blue_font
        for r in (SINGLE_ISTD_MEAN_ROW, DUPLICATE_ISTD_FACTOR_ROW):
            for col in (2, 3):
                wi.cell(r, col).font = Font(color="008000", bold=True)
            wi.cell(r, 1).font = Font(bold=True)
        wi.column_dimensions["A"].width = 22
        wi.column_dimensions["D"].width = 45
        for cell in wi["C"][1:]: cell.number_format = "0"
        wi[f"B{DUPLICATE_ISTD_FACTOR_ROW}"].number_format = "0.000E+00"
    for sheet in wb.worksheets:
        if sheet.title in istd_sheets:
            continue
        if sheet.title in detail_sheets:
            # A filter instead of an Excel table: a table would fight the
            # per-row ISTD dropdown whenever the analyst sorts or extends it.
            if sheet.max_row > 1:
                sheet.auto_filter.ref = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
            continue
        if sheet.title != "Parameter" and sheet.max_row > 1:
            ref = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
            tab = Table(displayName="T_" + re.sub(r"\W", "", sheet.title), ref=ref)
            tab.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True,
                                                showFirstColumn=False, showLastColumn=False)
            sheet.add_table(tab)
    for sheet in (ws, wsr):
        for cell in sheet["A"][1:]: cell.number_format = "0.0000"
        for col in ("D", "E", "F"):
            for cell in sheet[col][1:]: cell.number_format = "0.000000"
        for cell in sheet["G"][1:]: cell.number_format = "0.0"
    for number, _data in determinations:
        wd = wb[f"Bestimmung_{number}"]
        for col in ("F", "G"):
            for cell in wd[col][1:]: cell.number_format = "0.000000"
        for cell in wd["U"][1:]: cell.number_format = "0.000E+00"
    if single:
        # Spec v2.1 SS V.6: the layout is the same for one and two
        # determinations; the second determination's columns are written empty
        # and hidden rather than removed, so anything that reads by header name
        # keeps working and nothing has to learn a second layout.
        for sheet in (ws, wsr):
            for col in SINGLE_DETERMINATION_HIDDEN_COLUMNS:
                sheet.column_dimensions[col].hidden = True
    try:
        wb.calculation.fullCalcOnLoad = True; wb.calculation.forceFullCalc = True
        wb.calculation.calcMode = "auto"
    except Exception: pass
    wb.save(output_path)
    return {"output": output_path, "reported": kept,
            "determinations": len(determinations),
            # By sources, not by mean: a pair has no mean until an IS is marked.
            "paired": 0 if single else sum(bool(x["source1"] and x["source2"]) for x in combined),
            "artefacts": 0 if single else sum(not (x["source1"] and x["source2"]) for x in combined),
            "conflicts": sum(x["status"] == "Identification conflict" for x in combined),
            "blank_corrected_1": d1["blank_corrected"],
            "blank_corrected_2": 0 if single else d2["blank_corrected"],
            "istd_found_1": istd_found.get(1, []),
            "istd_found_2": istd_found.get(2, [])}


# ---------------------------------------------------------------------------
# Einzelbestimmung (single determination, ISTD chosen per row)
# ---------------------------------------------------------------------------

#: Main sheet of a single-determination workbook. NIAS Report recognises the
#: file by this sheet and reads RT, Name, CAS and mg/kg from columns A to D.
SINGLE_SHEET_NAME = "Einzelbestimmung"
SINGLE_ISTD_SHEET_NAME = "ISTD"

#: Internal standard labels offered in the ISTD dropdown, the chemical each one
#: normally is, and the Parameter row that holds its concentration (``None``:
#: typed on the ISTD sheet). No standard is searched for in the data: the
#: analyst marks the peak of each IS in column F, and the ISTD sheet looks its
#: area up by that label inside Excel.
SINGLE_ISTD_DEFAULTS = (
    ("IS1", "Perdeutero-Heptadecane", 11),
    ("IS2", "Benzyl-butyl-phthalate-d4", 12),
    ("IS3", "Di-n-nonyl-phthalate-d4", 13),
    ("IS4", "Dibutyl phthalate-3,4,5,6-d4", None),
)
#: Last row of the ISTD list (IS1..IS4).
SINGLE_ISTD_LAST_ROW = 5
#: Labels whose mean area and concentration quantify every row.
SINGLE_ISTD_MEAN_OF = ("IS1", "IS2", "IS3")
#: ISTD sheet row holding that mean: B = mean c, C = mean area.
SINGLE_ISTD_MEAN_ROW = 7

SINGLE_HEADERS = (
    "RT [min]", "Name", "CAS", "mg/kg", "mg/dm2", "ISTD", "ISTD area",
    "ISTD c [mg/mL]", "Blank-corrected FID area", "Quality", "PBM Area %",
    "PBM peak", "Library/ID (hit 1)", "FID peak", "FID RT [min]",
    "Raw FID area", "Blank area", "Blank+ISTD area", "Subtracted area",
    "Blank correction", "Review",
)


def single_output_name(results_path) -> tuple[str, Path]:
    """``(sample name, workbook path)`` for one RESULTS.CSV.

    The sample is named after the folder the CSV was loaded from -- normally the
    ``.D`` folder, whose suffix is dropped -- and the workbook is written into
    that same folder.
    """
    folder = Path(results_path).resolve().parent
    name = folder.name
    if name.lower().endswith(".d"):
        name = name[:-2]
    name = name.strip() or "Sample"
    safe = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .") or "Sample"
    return name, folder / f"{safe}.xlsx"


def find_library_results(results_path) -> Optional[Path]:
    """The LIBresults.csv next to a RESULTS.CSV, if there is exactly one."""
    folder = Path(results_path).parent
    try:
        hits = [p for p in folder.iterdir()
                if p.is_file() and p.name.casefold() == "libresults.csv"]
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def make_single_workbook(results_path, lib_path, output_path, s: Settings,
                         blank_path: str = "", blank_istd_path: str = "",
                         sample_name: str = ""):
    """Write one single-determination workbook, one row per PBM peak with FID area.

    Nothing here looks for internal standards: every row has an ``ISTD`` cell
    with an IS1..IS4 dropdown that marks the row as that standard. The ``ISTD``
    sheet picks up the marked rows' areas, and every row is quantified against
    the mean area and mean concentration of IS1..IS3 by formula. Until an IS is
    marked no row has a concentration, so the workbook is created whether or
    not a standard is present.

    Blank and Blank+ISTD correction stay optional and work exactly as in the
    Doppelbestimmung (the larger matched blank area is subtracted once).
    """
    tic, fid, pbm = parse_results(results_path)
    detailed = parse_library_report(lib_path) if lib_path else {}
    for p in pbm:
        if detailed.get(p.peak):
            p.hits = detailed[p.peak]
    delay = estimate_delay(tic, fid, s.solvent_end)
    assigned_raw = assign(fid, pbm, delay, s)
    blank_rows, blank_delay = load_blank_reference(blank_path, s)
    blank_istd_rows, blank_istd_delay = load_blank_reference(blank_istd_path, s)
    corrected, audit = subtract_blank_signals(
        assigned_raw, blank_rows, blank_istd_rows, s.blank_rt_tolerance)

    # PBM peak number -> (corrected FID peak, audit) for the main component. A
    # co-eluting component behind the same FID peak has no area of its own.
    main_fid = {}
    for f, p, _secondary, _status in corrected:
        if p is not None:
            main_fid[p.peak] = (f, audit[f.peak])

    wb = Workbook()
    wb.properties.title = f"{sample_name or Path(results_path).parent.name} - Einzelbestimmung"
    ws = wb.active
    ws.title = SINGLE_SHEET_NAME
    wsi = wb.create_sheet(SINGLE_ISTD_SHEET_NAME)
    wsp = wb.create_sheet("Parameter")

    # Fixed rows (number in the comment): every formula addresses them absolutely.
    parameters = [
        ("Sample name", sample_name, "folder name of the loaded RESULTS.CSV"),            # 2
        ("Solvent end", s.solvent_end, "min"),                                             # 3
        ("Quality threshold", s.quality_limit, ""),                                        # 4
        ("FID/PBM RT tolerance", s.rt_tolerance, "min"),                                   # 5
        ("FID-MS delay", delay, "min"),                                                    # 6
        ("Cell area", s.cell_area_dm2, "dm2"),                                             # 7
        ("Coverage", s.coverage, ""),                                                      # 8
        ("O/V ratio", s.ov_ratio, ""),                                                     # 9
        ("Internal standard amount", s.is_amount, ""),                                     # 10
        ("FC17 concentration", s.fc17_conc, "mg/mL"),                                      # 11
        ("BBP-d4 concentration", s.bbp_conc, "mg/mL"),                                     # 12
        ("DnNP-d4 concentration", s.dnnp_conc, "mg/mL"),                                   # 13
        ("Blank RT tolerance", s.blank_rt_tolerance, "min; one-to-one FID apex matching"), # 14
        ("Blank file", blank_path or "not used", ""),                                      # 15
        ("Blank+ISTD file", blank_istd_path or "not used", ""),                            # 16
        ("Blank FID-MS delay", blank_delay if blank_delay is not None else "", "min"),     # 17
        ("Blank+ISTD FID-MS delay",
         blank_istd_delay if blank_istd_delay is not None else "", "min"),                # 18
        ("Note", "Mark the IS1..IS4 peaks in column F of 'Einzelbestimmung'. Every row "
                 "is quantified with the mean of IS1..IS3 (sheet 'ISTD'). "
                 "Concentrations recalculate from the blue cells.", ""),
    ]
    wsp.append(["Parameter", "Value", "Unit / note"])
    for row in parameters:
        wsp.append(row)
    cell_area, coverage, ov = "Parameter!$B$7", "Parameter!$B$8", "Parameter!$B$9"
    amount = "Parameter!$B$10"
    istd_names = f"{SINGLE_ISTD_SHEET_NAME}!$A$2:$A${SINGLE_ISTD_LAST_ROW}"
    mean_conc = f"{SINGLE_ISTD_SHEET_NAME}!$B${SINGLE_ISTD_MEAN_ROW}"
    mean_area = f"{SINGLE_ISTD_SHEET_NAME}!$C${SINGLE_ISTD_MEAN_ROW}"

    # Only peaks with an FID area are quantifiable; MS-only peaks are not written.
    ws.append(list(SINGLE_HEADERS))
    for p in pbm:
        if p.peak not in main_fid:
            continue
        r = ws.max_row + 1
        name, id_status, cas = display_identification(p, s.quality_limit)
        hit = p.hits[0] if p.hits else Hit("")
        review = []
        if id_status != "Accepted":
            review.append("unsichere Identifikation")
        f, correction = main_fid[p.peak]
        ws.append([
            p.rt, name, cas,
            f'=IF(E{r}="","",E{r}*{ov})',
            (f'=IF(OR(G{r}="",H{r}="",I{r}=""),"",'
             f'IFERROR(I{r}*H{r}*{amount}/1000/G{r}/{cell_area}/{coverage},""))'),
            None,
            f'=IF({mean_area}="","",{mean_area})',
            f'=IF({mean_conc}="","",{mean_conc})',
            f.area, hit.quality, p.area_pct, p.peak, hit.name,
            f.peak, f.rt, correction.raw_area, correction.blank_area,
            correction.blank_istd_area, correction.subtracted_area,
            correction.status, "; ".join(review),
        ])
    last_row = max(ws.max_row, 2)

    # ISTD sheet: concentration from Parameter, area of the row marked with the
    # label in column F of the main sheet. Both are ordinary cells the analyst
    # may overwrite. The mean row below feeds every concentration.
    wsi.append(["Name", "c [mg/mL]", "FID area", "Note"])
    label_range = f"{SINGLE_SHEET_NAME}!$F$2:$F${last_row}"
    area_range = f"{SINGLE_SHEET_NAME}!$I$2:$I${last_row}"
    names_in_data = {(p.hits[0].name if p.hits else "").strip().casefold()
                     for p in pbm if p.peak in main_fid}
    istd_named = []
    for label, chemical, parameter_row in SINGLE_ISTD_DEFAULTS:
        r = wsi.max_row + 1
        concentration = f"=Parameter!$B${parameter_row}" if parameter_row else None
        note = f"{chemical}; " + ("concentration from Parameter" if parameter_row
                                  else "enter concentration")
        if label not in SINGLE_ISTD_MEAN_OF:
            note += "; not part of the mean"
        wsi.append([label, concentration,
                    f'=IFERROR(INDEX({area_range},MATCH(A{r},{label_range},0)),"")',
                    note + f"; area of the row marked {label} in column F"])
        if chemical.casefold() in names_in_data:
            istd_named.append(chemical)
    first = 2
    last = first + len(SINGLE_ISTD_MEAN_OF) - 1
    m = SINGLE_ISTD_MEAN_ROW
    wsi.cell(m, 1, f"Mean {SINGLE_ISTD_MEAN_OF[0]}-{SINGLE_ISTD_MEAN_OF[-1]}")
    wsi.cell(m, 2, f'=IFERROR(AVERAGEIFS(B{first}:B{last},C{first}:C{last},">0"),"")')
    wsi.cell(m, 3, f'=IFERROR(AVERAGEIF(C{first}:C{last},">0"),"")')
    wsi.cell(m, 4, "mean over the marked IS only; used for mg/kg and mg/dm2 of every row")

    wpbm = wb.create_sheet("PBM")
    wpbm.append(["PBM Peak", "MS RT [min]", "Area %", "Hit 1", "CAS 1", "Q1",
                 "Hit 2", "CAS 2", "Q2", "Hit 3", "CAS 3", "Q3"])
    for peak in pbm:
        vals = [peak.peak, peak.rt, peak.area_pct]
        for i in range(3):
            h = peak.hits[i] if i < len(peak.hits) else Hit("")
            vals += [h.name, h.cas, h.quality]
        wpbm.append(vals)

    wsb = wb.create_sheet("Blankkorrektur")
    wsb.append(["FID peak", "RT [min]", "Raw FID area", "Blank area", "Blank+ISTD area",
                "Subtracted area", "Corrected FID area", "Correction status",
                "ISTD protected", "Corrected?"])
    for f, _p, _secondary, _status in corrected:
        c = audit[f.peak]
        wsb.append([f.peak, f.rt, c.raw_area, c.blank_area, c.blank_istd_area,
                    c.subtracted_area, c.corrected_area, c.status,
                    "Ja" if c.protected_standard else "Nein",
                    "Ja" if c.subtracted_area else "Nein"])

    # The dropdown lists the whole ISTD name column, so rows added there are offered.
    if ws.max_row > 1:
        validation = DataValidation(type="list", formula1=f"={istd_names}", allow_blank=True)
        validation.error = "Bitte IS1, IS2, IS3 oder IS4 wählen."
        validation.errorTitle = "ISTD"
        validation.prompt = "Diese Zeile als IS1..IS4 markieren (Mittelwert IS1-IS3 quantifiziert)"
        validation.promptTitle = "ISTD"
        ws.add_data_validation(validation)
        validation.add(f"F2:F{ws.max_row}")

    header_fill = PatternFill("solid", fgColor="1F4E78")
    green_font = Font(color="008000")
    blue_font = Font(color="0000FF")
    istd_fill = PatternFill("solid", fgColor="FFF2CC")
    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        sheet.sheet_view.showGridLines = False
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for col in range(1, sheet.max_column + 1):
            vals = [str(sheet.cell(r, col).value or "") for r in range(1, min(sheet.max_row, 200) + 1)]
            # A formula's text says nothing about the width of its result.
            shown = [v for v in vals if not v.startswith("=")] or [vals[0]]
            width = max(max(map(len, shown)) + 2, 12 if len(shown) < len(vals) else 10)
            sheet.column_dimensions[get_column_letter(col)].width = min(width, 45)
        for row in sheet.iter_rows(min_row=2):
            for c in row:
                is_formula = isinstance(c.value, str) and c.value.startswith("=")
                c.alignment = Alignment(vertical="top", wrap_text=isinstance(c.value, str)
                                        and not is_formula and len(c.value) > 25)
                if is_formula:
                    c.font = green_font
    ws.column_dimensions["F"].width = 12
    for r in range(2, ws.max_row + 1):
        ws.cell(r, 6).fill = istd_fill
        if ws.cell(r, 21).value:
            ws.cell(r, 21).font = Font(color="9C0006")
    if ws.max_row > 1:
        ws.conditional_formatting.add(
            f"D2:D{ws.max_row}",
            FormulaRule(formula=["AND(ISNUMBER(D2),D2>=0.01)"],
                        fill=PatternFill("solid", fgColor="FFC7CE", bgColor="FFC7CE"),
                        font=Font(color="9C0006")))
    for r in range(2, SINGLE_ISTD_LAST_ROW + 1):
        for col in (1, 2, 3):
            wsi.cell(r, col).font = blue_font
    for col in (1, 2, 3):
        wsi.cell(SINGLE_ISTD_MEAN_ROW, col).font = Font(color="008000", bold=True)
    wsi.cell(SINGLE_ISTD_MEAN_ROW, 1).font = Font(bold=True)
    wsi.column_dimensions["A"].width = 14
    for r in range(2, 15):
        wsp.cell(r, 2).font = blue_font
    wsp.cell(6, 2).font = Font(color="808080", italic=True)  # derived, not an input
    for cell in ws["A"][1:]: cell.number_format = "0.0000"
    for col in ("D", "E"):
        for cell in ws[col][1:]: cell.number_format = "0.000000"
    for col in ("G", "I", "P", "Q", "R", "S"):
        for cell in ws[col][1:]: cell.number_format = "0"
    for col in ("K", "O"):
        for cell in ws[col][1:]: cell.number_format = "0.0000"
    for cell in wsi["C"][1:]: cell.number_format = "0"
    for sheet in (wpbm, wsb):
        if sheet.max_row > 1:
            ref = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
            tab = Table(displayName="T_" + re.sub(r"\W", "", sheet.title), ref=ref)
            tab.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True,
                                                showFirstColumn=False, showLastColumn=False)
            sheet.add_table(tab)
    # A filter instead of an Excel table: a table on this sheet would fight the
    # per-row dropdown whenever the analyst sorts or extends it.
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    try:
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
        wb.calculation.calcMode = "auto"
    except Exception:
        pass
    wb.save(output_path)
    return {"output": str(output_path), "pbm": len(pbm), "fid": len(fid),
            "fid_matched": len(main_fid),
            "blank_corrected": sum(1 for x in audit.values() if x.subtracted_area),
            "istd_named": istd_named, "delay": delay}


SYNERIS_NUMBER_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
BLANK_FOLDER_RE = re.compile(r"blank|blanc|blind|leerwert", re.IGNORECASE)
BLANK_ISTD_RE = re.compile(r"istd|i\.?s\.?td|[_\-\s]is[_\-\s]|standard", re.IGNORECASE)


def _common_prefix(values):
    if not values:
        return ""
    shortest = min(values, key=len)
    for index, char in enumerate(shortest):
        if any(value[index] != char for value in values):
            return shortest[:index]
    return shortest


def _duplicate_sample_name(syneris_number: str, folder_names):
    """Derive the sample name from the shared part of the determination folders.

    With one folder the shared part is that folder's whole name, which is what
    the analysis list shows for a single determination.
    """
    prefix = re.sub(r"[\s_\-.]+$", "", _common_prefix(folder_names)).strip()
    if len(prefix) >= 3:
        return prefix
    return folder_names[0] if folder_names else syneris_number


def _csv_pair(sample_folder: Path):
    """Return (RESULTS.CSV, LIBresults.csv, errors) for one determination folder."""
    files = [path for path in sample_folder.iterdir() if path.is_file()]
    results_files = [path for path in files if path.name.casefold() == "results.csv"]
    library_files = [path for path in files if path.name.casefold() == "libresults.csv"]
    errors = []
    if len(results_files) != 1:
        errors.append(f"{len(results_files)} RESULTS.CSV-Dateien gefunden; genau 1 erforderlich")
    if len(library_files) > 1:
        errors.append(f"{len(library_files)} LIBresults.csv-Dateien gefunden; höchstens 1 erlaubt")
    if errors:
        return None, None, errors
    return results_files[0], (library_files[0] if library_files else None), []


#: An analysis may consist of one or two determinations (spec v2.1 SS V.6).
#: Three or more folders under one Syneris number is a naming mistake, not a
#: decision, and stays an error.
MAX_DETERMINATIONS_PER_ANALYSIS = 2


def discover_batch_samples(batch_folder: str):
    """Group batch subfolders into analyses by Syneris number.

    An analysis is one **or** two determinations; the count travels with the
    group as ``"count"`` so the caller never has to re-measure it. Only three or
    more determinations under one number is an error, because that means two
    samples were given the same number rather than that someone chose to run a
    single determination.

    Blank folders are recognised by name and apply to the whole batch.
    """
    root = Path(batch_folder)
    if not root.is_dir():
        raise ValueError("Der ausgewählte Batch-Ordner ist nicht gültig.")

    blanks = {"blank": None, "blank_istd": None}
    grouped: dict[str, list] = {}
    issues = []
    for sample_folder in sorted((path for path in root.iterdir() if path.is_dir()),
                                key=lambda path: path.name.casefold()):
        results, library, errors = _csv_pair(sample_folder)
        if errors:
            issues.append({"sample": sample_folder.name, "error": "; ".join(errors)})
            continue
        entry = {"name": sample_folder.name, "folder": sample_folder,
                 "results": results, "library": library}
        if BLANK_FOLDER_RE.search(sample_folder.name):
            key = "blank_istd" if BLANK_ISTD_RE.search(sample_folder.name) else "blank"
            if blanks[key] is None:
                blanks[key] = entry
            else:
                issues.append({"sample": sample_folder.name,
                               "error": "weiterer Blank-Ordner gefunden; wird ignoriert"})
            continue
        match = SYNERIS_NUMBER_RE.search(sample_folder.name)
        if not match:
            issues.append({"sample": sample_folder.name,
                           "error": "keine 8-stellige Syneris-Nummer im Ordnernamen"})
            continue
        grouped.setdefault(match.group(1), []).append(entry)

    samples = []
    for syneris_number, entries in sorted(grouped.items()):
        if len(entries) > MAX_DETERMINATIONS_PER_ANALYSIS:
            issues.append({
                "sample": syneris_number,
                "error": f"{len(entries)} Bestimmungen gefunden; höchstens "
                         f"{MAX_DETERMINATIONS_PER_ANALYSIS} je Syneris-Nummer erlaubt "
                         f"({', '.join(item['name'] for item in entries)})"})
            continue
        samples.append({
            "syneris": syneris_number,
            "name": _duplicate_sample_name(syneris_number, [item["name"] for item in entries]),
            "determinations": entries,
            "count": len(entries),
        })
    return samples, blanks, issues


#: The name this function had before SS V.6 renamed it, kept because a group of
#: one is now a valid analysis and "duplicate" no longer describes what it
#: finds. Callers outside this module still use the old name.
discover_duplicate_samples = discover_batch_samples


def duplicate_output_paths(samples, output_folder: str):
    output_root = Path(output_folder)
    used_names = set()
    paths = {}
    for sample in samples:
        stem = _safe_output_stem(sample["name"])
        candidate = stem
        suffix = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{suffix}"
            suffix += 1
        used_names.add(candidate.casefold())
        paths[sample["syneris"]] = output_root / f"{candidate}_Doppelbestimmung.xlsx"
    return paths


def process_duplicate_batch(batch_folder: str, output_folder: str = "",
                            s: Optional[Settings] = None, overwrite: bool = False,
                            progress=None):
    """Create one workbook per Syneris number and keep partial successes.

    An analysis with a single determination produces a workbook of the same
    layout, with the second determination's columns empty and hidden (SS V.6).
    """
    # Deliberately the alias, not the new name: NIAS installs its solvent and
    # ISTD folder recognition by replacing this module attribute, and calling
    # the renamed function directly would silently bypass it.
    samples, blanks, discovery_issues = discover_duplicate_samples(batch_folder)
    settings = s or Settings()
    output_root = Path(output_folder or batch_folder)
    if not output_root.is_dir():
        raise ValueError("Der ausgewählte Ausgabeordner ist nicht gültig.")

    blank_path = str(blanks["blank"]["results"]) if blanks["blank"] else ""
    blank_istd_path = str(blanks["blank_istd"]["results"]) if blanks["blank_istd"] else ""
    created, skipped, failed = [], [], []
    output_paths = duplicate_output_paths(samples, str(output_root))
    total = len(samples)
    for index, sample in enumerate(samples, start=1):
        if progress:
            progress(index, total, sample["name"])
        output_path = output_paths[sample["syneris"]]
        if output_path.exists() and not overwrite:
            skipped.append({"sample": sample["name"], "output": str(output_path)})
            continue
        determinations = sample["determinations"]
        first = determinations[0]
        # An analysis of one determination passes empty CSV paths for the
        # second; make_duplicate_workbook reads that as a single determination.
        second = determinations[1] if len(determinations) > 1 else None
        try:
            info = make_duplicate_workbook(
                str(first["results"]), str(first["library"]) if first["library"] else "",
                str(second["results"]) if second else "",
                str(second["library"]) if second and second["library"] else "",
                str(output_path), settings, blank_path, blank_istd_path, sample["name"])
            created.append({"sample": sample["name"], "syneris": sample["syneris"],
                            "count": len(determinations), **info})
        except Exception as exc:
            failed.append({"sample": sample["name"], "error": str(exc)})
    return {"created": created, "skipped": skipped, "failed": failed,
            "discovered": total, "discovery_issues": discovery_issues,
            "blank": blank_path, "blank_istd": blank_istd_path}


def launch_ui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("NIAS GC-MS / FID Auswertung")
    root.geometry("980x880")
    mode = tk.StringVar(value="duplicate")
    defaults = {"solvent_end":"5.5", "quality_limit":"70", "rt_tolerance":"0.035",
                "cell_area_dm2":"0.51", "coverage":"1", "ov_ratio":"6", "is_amount":"10",
                "fc17_conc":"0.82", "bbp_conc":"0.83", "dnnp_conc":"0.82", "qc_min_area":"0",
                "blank_rt_tolerance":"0.04"}

    main = ttk.Frame(root, padding=14)
    main.pack(fill="both", expand=True)
    title = ttk.Label(main, text="NIAS GC-MS / FID Doppelbestimmung",
                      font=("Segoe UI", 16, "bold"))
    title.pack(anchor="w", pady=(0, 8))
    mode_bar = ttk.Frame(main)
    mode_bar.pack(fill="x", pady=(0, 12))
    ttk.Radiobutton(mode_bar, text="Doppelbestimmung", value="duplicate",
                    variable=mode).pack(side="left")
    ttk.Radiobutton(mode_bar, text="Fingerprint Screening", value="fingerprint",
                    variable=mode).pack(side="left", padx=(18, 0))
    content = ttk.Frame(main)
    content.pack(fill="both", expand=True)

    duplicate_frame = ttk.Frame(content)
    duplicate_vars = {key: tk.StringVar() for key in
                      ("sample_name", "results1", "library1", "results2", "library2",
                       "blank", "blank_istd", "output")}
    duplicate_vars.update({key: tk.StringVar(value=value) for key, value in defaults.items()})

    def browse_duplicate_input(key):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv;*.CSV"), ("All files", "*.*")])
        if p:
            duplicate_vars[key].set(p)
            if key == "results1" and not duplicate_vars["output"].get():
                safe_name = _safe_output_stem(duplicate_vars["sample_name"].get())
                base_name = safe_name or Path(p).stem
                duplicate_vars["output"].set(str(Path(p).with_name(base_name + "_Doppelbestimmung.xlsx")))

    def browse_duplicate_output():
        p = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")])
        if p:
            duplicate_vars["output"].set(p)

    file_rows = [("Probenname", "sample_name"),
                 ("RESULTS.CSV - Bestimmung 1", "results1"),
                 ("LIBresults.csv - Bestimmung 1 (optional)", "library1"),
                 ("RESULTS.CSV - Bestimmung 2", "results2"),
                 ("LIBresults.csv - Bestimmung 2 (optional)", "library2"),
                 ("Blank RESULTS.CSV (optional, für beide Bestimmungen)", "blank"),
                 ("Blank+ISTD RESULTS.CSV (optional, für beide Bestimmungen)", "blank_istd")]
    r = 0
    for label, key in file_rows:
        ttk.Label(duplicate_frame, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(duplicate_frame, textvariable=duplicate_vars[key], width=75).grid(
            row=r, column=1, sticky="ew", padx=8)
        if key != "sample_name":
            ttk.Button(duplicate_frame, text="Auswählen",
                       command=lambda k=key: browse_duplicate_input(k)).grid(row=r, column=2)
        r += 1
    ttk.Label(duplicate_frame, text="Ausgabe Excel").grid(row=r, column=0, sticky="w", pady=4)
    ttk.Entry(duplicate_frame, textvariable=duplicate_vars["output"], width=75).grid(
        row=r, column=1, sticky="ew", padx=8)
    ttk.Button(duplicate_frame, text="Auswählen", command=browse_duplicate_output).grid(row=r, column=2)
    r += 1
    ttk.Separator(duplicate_frame).grid(row=r, column=0, columnspan=3, sticky="ew", pady=12)
    r += 1

    labels=[("LM-Ende [min]","solvent_end"),("Library Quality-Grenze","quality_limit"),
            ("RT-Toleranz / Doppel-Matching [min]","rt_tolerance"),("Zellfläche [dm²]","cell_area_dm2"),
            ("Belegung","coverage"),("O/V-Ratio","ov_ratio"),("Menge interner Standard","is_amount"),
            ("FC17 Konzentration [mg/mL]","fc17_conc"),("BBP-d4 Konzentration [mg/mL]","bbp_conc"),
            ("DnNP-d4 Konzentration [mg/mL]","dnnp_conc"),("QC Mindestfläche (0 = nur Erkennung)","qc_min_area"),
            ("Blank RT-Toleranz [min]","blank_rt_tolerance")]
    for label,key in labels:
        ttk.Label(duplicate_frame, text=label).grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(duplicate_frame, textvariable=duplicate_vars[key], width=18).grid(
            row=r, column=1, sticky="w", padx=8)
        r += 1
    note=("Beide Bestimmungen werden separat quantifiziert. Der Mittelwert wird erst aus den beiden mg/kg-Ergebnissen gebildet.\n"
          "Einseitig detektierte Peaks werden als Artefakt markiert; Identifikationskonflikte werden manuell geprüft.")
    ttk.Label(duplicate_frame, text=note, foreground="#7F6000").grid(
        row=r, column=0, columnspan=3, sticky="w", pady=12)
    r += 1
    duplicate_status = tk.StringVar(value="Bereit")
    ttk.Label(duplicate_frame, textvariable=duplicate_status).grid(
        row=r, column=0, columnspan=2, sticky="w")

    def run_duplicate():
        try:
            if not duplicate_vars["sample_name"].get().strip():
                raise ValueError("Bitte zuerst einen Probennamen eingeben.")
            if (not duplicate_vars["results1"].get() or not duplicate_vars["results2"].get()
                    or not duplicate_vars["output"].get()):
                raise ValueError("Bitte beide RESULTS.CSV-Dateien und eine Ausgabedatei auswählen.")
            sample_name = duplicate_vars["sample_name"].get().strip()
            safe_name = _safe_output_stem(sample_name)
            selected_output = Path(duplicate_vars["output"].get())
            if safe_name.lower() not in selected_output.stem.lower():
                selected_output = selected_output.with_name(f"{safe_name}_{selected_output.stem}.xlsx")
                duplicate_vars["output"].set(str(selected_output))
            s = Settings(solvent_end=float(duplicate_vars["solvent_end"].get()),
                         quality_limit=int(duplicate_vars["quality_limit"].get()),
                         rt_tolerance=float(duplicate_vars["rt_tolerance"].get()),
                         cell_area_dm2=float(duplicate_vars["cell_area_dm2"].get()),
                         coverage=float(duplicate_vars["coverage"].get()),
                         ov_ratio=float(duplicate_vars["ov_ratio"].get()),
                         is_amount=float(duplicate_vars["is_amount"].get()),
                         fc17_conc=float(duplicate_vars["fc17_conc"].get()),
                         bbp_conc=float(duplicate_vars["bbp_conc"].get()),
                         dnnp_conc=float(duplicate_vars["dnnp_conc"].get()),
                         qc_min_area=float(duplicate_vars["qc_min_area"].get()),
                         blank_rt_tolerance=float(duplicate_vars["blank_rt_tolerance"].get()))
            duplicate_status.set("Doppelbestimmung wird ausgewertet ...")
            root.update_idletasks()
            info = make_duplicate_workbook(
                duplicate_vars["results1"].get(), duplicate_vars["library1"].get(),
                duplicate_vars["results2"].get(), duplicate_vars["library2"].get(),
                duplicate_vars["output"].get(), s, duplicate_vars["blank"].get(),
                duplicate_vars["blank_istd"].get(), sample_name)
            duplicate_status.set(
                f"Fertig: {info['paired']} Paare, {info['artefacts']} Artefakte, "
                f"{info['conflicts']} Konflikte, "
                f"{info['blank_corrected_1'] + info['blank_corrected_2']} blankkorrigiert")
            messagebox.showinfo("Fertig", f"Excel-Datei erstellt:\n{info['output']}\n\n"
                                f"Gültig gepaart: {info['paired']}\nArtefakte: {info['artefacts']}\n"
                                f"Identifikationskonflikte: {info['conflicts']}\n"
                                f"Blankkorrigierte Peaks: {info['blank_corrected_1'] + info['blank_corrected_2']}")
        except Exception as e:
            duplicate_status.set("Fehler")
            messagebox.showerror("Fehler", str(e))
    ttk.Button(duplicate_frame, text="Doppelbestimmung auswerten",
               command=run_duplicate).grid(row=r, column=2, sticky="e", pady=8)
    duplicate_frame.columnconfigure(1, weight=1)

    fingerprint_frame = ttk.Frame(content)
    fingerprint_vars = {
        "batch": tk.StringVar(), "blank": tk.StringVar(), "output": tk.StringVar(),
        "solvent_end": tk.StringVar(value=defaults["solvent_end"]),
        "quality_limit": tk.StringVar(value=defaults["quality_limit"]),
        "rt_tolerance": tk.StringVar(value=defaults["rt_tolerance"]),
        "blank_rt_tolerance": tk.StringVar(value=defaults["blank_rt_tolerance"]),
    }
    discovery_status = tk.StringVar(value="Noch kein Batch-Ordner ausgewählt")
    fingerprint_status = tk.StringVar(value="Bereit")

    def refresh_discovery():
        try:
            samples, issues = discover_fingerprint_samples(
                fingerprint_vars["batch"].get(), fingerprint_vars["blank"].get())
            text = f"{len(samples)} gültige Probenordner"
            if issues:
                text += f", {len(issues)} Problem(e): " + "; ".join(
                    f"{item['sample']}: {item['error']}" for item in issues)
            discovery_status.set(text)
            return samples, issues
        except Exception as exc:
            discovery_status.set(str(exc))
            return [], []

    def browse_batch_folder():
        path = filedialog.askdirectory(title="Batch-Ordner auswählen")
        if path:
            fingerprint_vars["batch"].set(path)
            if not fingerprint_vars["output"].get():
                fingerprint_vars["output"].set(path)
            refresh_discovery()

    def browse_fingerprint_blank():
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv;*.CSV"), ("All files", "*.*")])
        if path:
            fingerprint_vars["blank"].set(path)
            refresh_discovery()

    def browse_fingerprint_output():
        path = filedialog.askdirectory(title="Ausgabeordner auswählen")
        if path:
            fingerprint_vars["output"].set(path)

    fingerprint_rows = [("Batch-Ordner", "batch", browse_batch_folder),
                        ("Gemeinsamer Blank RESULTS.CSV (optional)", "blank", browse_fingerprint_blank),
                        ("Ausgabeordner", "output", browse_fingerprint_output)]
    r = 0
    for label, key, command in fingerprint_rows:
        ttk.Label(fingerprint_frame, text=label).grid(row=r, column=0, sticky="w", pady=5)
        ttk.Entry(fingerprint_frame, textvariable=fingerprint_vars[key], width=75).grid(
            row=r, column=1, sticky="ew", padx=8)
        ttk.Button(fingerprint_frame, text="Auswählen", command=command).grid(row=r, column=2)
        r += 1
    ttk.Label(fingerprint_frame, textvariable=discovery_status, foreground="#7F6000",
              wraplength=850).grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 12))
    r += 1
    ttk.Separator(fingerprint_frame).grid(row=r, column=0, columnspan=3, sticky="ew", pady=8)
    r += 1
    fingerprint_settings = [("LM-Ende [min]", "solvent_end"),
                            ("Library Quality-Grenze", "quality_limit"),
                            ("FID/PBM RT-Toleranz [min]", "rt_tolerance"),
                            ("Blank RT-Toleranz [min]", "blank_rt_tolerance")]
    for label, key in fingerprint_settings:
        ttk.Label(fingerprint_frame, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(fingerprint_frame, textvariable=fingerprint_vars[key], width=18).grid(
            row=r, column=1, sticky="w", padx=8)
        r += 1
    ttk.Label(
        fingerprint_frame,
        text=("Ein Workbook wird pro Probenordner erstellt. PBM Area % dient als semiquantitative "
              "Mengeninformation; Standards und Konzentrationen werden nicht verwendet. "
              "Blanktreffer werden markiert, aber nicht abgezogen."),
        foreground="#7F6000", wraplength=850).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=12)
    r += 1
    ttk.Label(fingerprint_frame, textvariable=fingerprint_status).grid(
        row=r, column=0, columnspan=2, sticky="w")

    def run_fingerprint():
        try:
            batch_folder = fingerprint_vars["batch"].get()
            output_folder = fingerprint_vars["output"].get()
            samples, _ = refresh_discovery()
            if not samples:
                raise ValueError("Keine gültigen Probenordner im Batch gefunden.")
            if not Path(output_folder).is_dir():
                raise ValueError("Bitte einen gültigen Ausgabeordner auswählen.")
            settings = Settings(
                solvent_end=float(fingerprint_vars["solvent_end"].get()),
                quality_limit=int(fingerprint_vars["quality_limit"].get()),
                rt_tolerance=float(fingerprint_vars["rt_tolerance"].get()),
                blank_rt_tolerance=float(fingerprint_vars["blank_rt_tolerance"].get()))
            targets = fingerprint_output_paths(samples, output_folder)
            existing = [path for path in targets.values() if path.exists()]
            overwrite = False
            if existing:
                overwrite = messagebox.askyesno(
                    "Vorhandene Dateien",
                    f"{len(existing)} Zieldatei(en) existieren bereits. Sollen sie überschrieben werden?\n\n"
                    "Bei 'Nein' werden diese Proben übersprungen.")

            def update_progress(index, total, sample_name):
                fingerprint_status.set(f"Probe {index} von {total}: {sample_name}")
                root.update_idletasks()

            info = process_fingerprint_batch(
                batch_folder, output_folder, settings, fingerprint_vars["blank"].get(),
                overwrite, update_progress)
            summary = (f"Erstellt: {len(info['created'])}\n"
                       f"Übersprungen: {len(info['skipped'])}\n"
                       f"Fehler: {len(info['failed'])}")
            if info["failed"]:
                summary += "\n\n" + "\n".join(
                    f"{item['sample']}: {item['error']}" for item in info["failed"])
            fingerprint_status.set(
                f"Fertig: {len(info['created'])} erstellt, {len(info['skipped'])} übersprungen, "
                f"{len(info['failed'])} Fehler")
            messagebox.showinfo("Fingerprint Screening abgeschlossen", summary)
        except Exception as exc:
            fingerprint_status.set("Fehler")
            messagebox.showerror("Fehler", str(exc))

    ttk.Button(fingerprint_frame, text="Fingerprint Batch auswerten",
               command=run_fingerprint).grid(row=r, column=2, sticky="e", pady=8)
    fingerprint_frame.columnconfigure(1, weight=1)

    def show_mode(*_):
        duplicate_frame.pack_forget()
        fingerprint_frame.pack_forget()
        if mode.get() == "fingerprint":
            title.configure(text="NIAS GC-MS / FID Fingerprint Screening")
            fingerprint_frame.pack(fill="both", expand=True)
        else:
            title.configure(text="NIAS GC-MS / FID Doppelbestimmung")
            duplicate_frame.pack(fill="both", expand=True)

    mode.trace_add("write", show_mode)
    show_mode()
    root.mainloop()


if __name__ == "__main__":
    launch_ui()

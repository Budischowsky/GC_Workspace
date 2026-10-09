"""Records parsed from a human NIAS evaluation workbook."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawPeak:
    """One row of a ChemStation integration table in the 'Rohdaten' sheet (TIC or FID)."""
    signal: str
    number: int
    rt: float
    area: float
    height: Optional[float]
    start: Optional[float]          # minutes (FID table); None where the table gives scan numbers (TIC)
    end: Optional[float]
    peak_type: str
    manual: bool                    # integrated by hand in ChemStation (peak type contains "M")


@dataclass
class AlkanePoint:
    name: str
    rt: Optional[float]
    ri: float


@dataclass
class IstdEntry:
    name: str
    conc: Optional[float]
    area: Optional[float]


@dataclass
class Header:
    sample_name: str = ""
    syn_id: str = ""
    syn_summary: str = ""
    evaluator: str = ""
    operator: str = ""
    data_file: str = ""
    simulant: str = ""
    temperature: Optional[float] = None
    temperature_text: str = ""
    duration: str = ""
    volume: Optional[float] = None
    sv_ratio: Optional[float] = None
    gc_method: str = ""
    inj_volume: Optional[float] = None
    istd: list[IstdEntry] = field(default_factory=list)
    istd_mean_area: Optional[float] = None
    alkanes: list[AlkanePoint] = field(default_factory=list)
    conc_units: list[str] = field(default_factory=list)


@dataclass
class EvalRow:
    """One row of the analyst's worksheet ('Auswertung' or the pre-clean 'Auswertung (2)')."""
    sheet_row: int
    rt: Optional[float]
    label: str = ""
    cas: str = ""
    library: str = ""
    match: Optional[float] = None
    area: Optional[float] = None
    area_formula: str = ""          # the analyst corrected the area by a formula, e.g. "=L29-F30"
    area_original: Optional[float] = None
    conc: dict[str, Optional[float]] = field(default_factory=dict)
    sml: str = ""
    reference: str = ""
    note: str = ""
    row_class: str = ""


@dataclass
class ReportRow:
    """One substance line of the client report ('externerBericht')."""
    rt: Optional[float]
    label: str = ""
    cas: str = ""
    library: str = ""
    match: Optional[float] = None
    conc: dict[str, Optional[float]] = field(default_factory=dict)
    sml: str = ""
    reference: str = ""
    row_class: str = ""


@dataclass
class HumanEvaluation:
    path: str
    template: str = ""
    header: Header = field(default_factory=Header)
    raw: list[RawPeak] = field(default_factory=list)
    pre_clean: Optional[list[EvalRow]] = None
    final: list[EvalRow] = field(default_factory=list)
    report: list[ReportRow] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def normalise_cas(text) -> str:
    """'000097-88-1' -> '97-88-1'; '' for empty, '-' or None."""
    s = str(text or "").strip()
    if not s or set(s) <= {"-"}:
        return ""
    head, sep, rest = s.partition("-")
    head = head.lstrip("0") or "0"
    return head + sep + rest

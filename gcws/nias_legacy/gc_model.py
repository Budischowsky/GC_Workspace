#!/usr/bin/env python3
"""Data model for the GC workspace.

Holds the peak rows of one injection, tracks every manual edit against the
values as loaded, and recomputes the derived columns whenever a raw value
changes.

The model is deliberately free of tkinter, matplotlib and openpyxl imports so
it can be unit-tested and reused by the exporters.

Identity rule
-------------
Every row carries a ``row_id`` that is allocated once and never reused. Edit
flags, unknown flags and cell colours are keyed by ``(row_id, field)`` and never
by row position -- sorting, filtering and re-merging all reorder the list, and
an index key would silently move a flag onto a different substance.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# Classification thresholds
# --------------------------------------------------------------------------

#: The single quality threshold used throughout, exactly as the workbook does.
#: There is no second band: a hit at or above the limit is an identification, and
#: anything below it is not.
DEFAULT_QUALITY_LIMIT = 70

# Identification states, spelled as ``AutoLib.display_identification`` writes
# them so the grid, the workbook and the register all say the same thing.
ID_ACCEPTED = "Accepted"                        # top hit >= quality limit
ID_UNCERTAIN_CLASS = "Uncertain; class inferred"  # below limit, class inferable
ID_UNCERTAIN = "Uncertain"                      # below limit, nothing inferable
ID_UNKNOWN = "Unknown"                          # no library hit at all

#: Name AutoLib writes when there is no usable identification.
UNKNOWN_DISPLAY_NAME = "unknown (m/z unavailable)"

#: Prefix of a class-inferred name, e.g. "possible derivative of hydrocarbon".
CLASS_INFERRED_PREFIX = "possible derivative of"

# --------------------------------------------------------------------------
# Quantification constants, taken from the DIN SPEC engine
# --------------------------------------------------------------------------

IS_CONCENTRATION_UG_L = 10166.67   # nominal concentration of the internal standard

#: The DIN SPEC engine only recognises a standard whose name begins with one of
#: these codes followed by a separator; a bare "IS2" does not match. The export
#: therefore rewrites the chosen row's name into this exact form.
ALLOWED_INTERNAL_STANDARDS = ("IS1", "IS2", "IS3")
INTERNAL_STANDARD_NAMES = {
    "IS1": "n-Heptadecane d-36",
    "IS2": "Benzyl butyl phthalate d-4",
    "IS3": "Diisononylphthalate d-4",
}

#: Substring markers used to guess which code a library name corresponds to.
INTERNAL_STANDARD_MARKERS = {
    "IS1": ("perdeutero-heptadecane", "heptadecane d-36", "heptadecane-d36"),
    "IS2": ("benzyl-butyl-phthalate-d4", "benzyl butyl phthalate d-4"),
    "IS3": ("di-n-nonyl-phthalate-d4", "diisononylphthalate d-4",
            "di-n-nonyl phthalate d-4"),
}


def guess_internal_standard_code(name: Any) -> Optional[str]:
    """Match a library name to IS1/IS2/IS3, or None when it is not a standard."""
    lowered = clean_name(name).casefold()
    if not lowered:
        return None
    for code, markers in INTERNAL_STANDARD_MARKERS.items():
        if any(m in lowered for m in markers):
            return code
    return None


def internal_standard_display(code: str) -> str:
    """The name the engine expects, e.g. ``"IS1 n-Heptadecane d-36"``."""
    return f"{code} {INTERNAL_STANDARD_NAMES.get(code, '')}".strip()
REPORTING_LIMIT_UG_L = 100.0       # below this a 3/3 cluster is not reported
OV_RATIO = 6.0                     # mg/dm2 -> mg/kg, AutoLib default

# The DIN extraction uses 1 g pellet per 1 mL liquid, and the existing pipeline
# therefore carries ug/L straight across into mg/kg with no scaling
# (NIAS.py:4633, "concentration_mg_kg": conc_ug_l). Kept as an explicit factor
# so the workspace cannot drift away from the reports already issued.
#
# Note: taken literally, 1 g per 1 mL makes 1 ug/L equal 1 ug/kg, i.e. 0.001
# mg/kg -- a factor of 1000 below what the pipeline uses. The convention here
# deliberately follows the existing code rather than the dimensional analysis;
# changing it would silently move every reported concentration by 1000x.
UG_L_TO_MG_KG = 1.0

# report_status(): text, fill, font
STATUS_UNIDENTIFIED = ("⚪ Substanz nicht identifiziert", "E7E6E6", "404040")
STATUS_NO_SML = ("\U0001f7e1 Kein SML vorhanden", "FFF2CC", "7F6000")
STATUS_OVER_SML = ("\U0001f534 SML überschritten", "F4CCCC", "9C0006")
STATUS_OK = ("\U0001f7e2 SML eingehalten", "D9EAD3", "2E603A")

# Names the instrument writes when nothing was identified.
_NO_HIT_NAMES = {
    "", "unidentified", "nicht identifiziert", "kein treffer gefunden",
    "no hit found",
}

PLACEHOLDER_CAS = {"000000-00-0", "0-00-0", "0", "0-0-0", "0-00-0", ""}


# --------------------------------------------------------------------------
# Value coercion -- must match the DIN SPEC engine exactly
# --------------------------------------------------------------------------

def num(value: Any) -> Optional[float]:
    """Parse a numeric cell the way the DIN SPEC engine does."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        return f if math.isfinite(f) else None
    s = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        f = float(s)
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def clean_name(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def clean_cas(value: Any) -> str:
    """Normalise a CAS cell, repairing values Excel turned into dates.

    Mirrors ``clean_cas`` in the embedded DIN SPEC engine: the sentinel for
    "no CAS" is the string ``"0"``, not the empty string.
    """
    if value is None:
        return "0"
    # Excel silently converts e.g. 4860-03-1 into a date.
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return f"{value.year}-{value.month:02d}-{value.day}"
    s = str(value).strip().replace("/", "-")
    parts = s.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        a, b, c = parts
        if len(c) == 4:                     # reversed by a date conversion
            s = f"{c}-{int(b):02d}-{a}"
        else:
            s = f"{a}-{int(b):02d}-{c}"
    if s in {"", "0", "0-0-0", "0-00-0"}:
        return "0"
    return s


def valid_cas(cas: Any) -> bool:
    """Strict CAS validation: syntax *and* the mod-10 check digit.

    This is the DIN SPEC rule. The NIAS side uses a laxer, syntax-only check
    (``is_valid_cas_number``); the two disagree, and this one is authoritative
    inside the workspace.
    """
    m = re.fullmatch(r"(\d{2,7})-(\d{2})-(\d)", str(cas).strip())
    if not m:
        return False
    digits = m.group(1) + m.group(2)
    checksum = sum((i + 1) * int(d) for i, d in enumerate(reversed(digits)))
    return checksum % 10 == int(m.group(3))


def display_cas(cas: Any) -> str:
    """CAS for display: placeholders become blank, leading zeros are stripped."""
    s = str(cas or "").strip()
    if s in PLACEHOLDER_CAS:
        return ""
    m = re.fullmatch(r"0*(\d+)-0*(\d+)-0*(\d+)", s)
    if m:
        return f"{int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3))}"
    return s


def is_no_hit(name: Any) -> bool:
    """True when the name carries no library identification at all."""
    n = clean_name(name).casefold()
    return n in _NO_HIT_NAMES or n.startswith("unknown")


def sml_limit(value: Any) -> Optional[float]:
    """Lowest numeric limit in an SML cell.

    ``"6 / 0.05*"`` yields 0.05: where a substance carries several limits the
    strictest one governs.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = re.findall(r"\d+(?:[.,]\d+)?", str(value).replace("*", ""))
    values = [float(f.replace(",", ".")) for f in found]
    return min(values) if values else None


def report_status(unidentified: bool, sml: Optional[float],
                  conc_kg: Optional[float]) -> tuple[str, str, str]:
    """Traffic-light status: (text, fill hex, font hex)."""
    if unidentified:
        return STATUS_UNIDENTIFIED
    if sml is None:
        return STATUS_NO_SML
    if conc_kg is not None and conc_kg > sml:
        return STATUS_OVER_SML
    return STATUS_OK


def classify_identification(name: Any, si: Optional[float],
                            quality_limit: float = DEFAULT_QUALITY_LIMIT) -> str:
    """Identification state, following ``AutoLib.display_identification``.

    One threshold, four outcomes:

    * no library hit at all                     -> ``Unknown``
    * top hit quality >= ``quality_limit``      -> ``Accepted``
    * below the limit, a class was inferable    -> ``Uncertain; class inferred``
    * below the limit, nothing inferable        -> ``Uncertain``

    The workbook applies this before we ever see the row, so on the NIAS path the
    engine's own status is used verbatim and this function only has to reproduce
    it for DIN SPEC, which has no such step of its own.
    """
    text = clean_name(name)
    lowered = text.casefold()
    if lowered.startswith(CLASS_INFERRED_PREFIX):
        return ID_UNCERTAIN_CLASS
    if si is None or is_no_hit(text):
        return ID_UNKNOWN
    if si >= quality_limit:
        return ID_ACCEPTED
    return ID_UNCERTAIN


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# FID integration (spec v3.0 SS VI.5 / SS VI.13)
# --------------------------------------------------------------------------
#
# The flat ``fid_*`` fields on :class:`PeakRow` are the **system of record** for
# the integration: grid, session file and export all read them.
# ``gc_integrate.Integration`` is a transient adapter built by
# ``Integration.from_row(row)`` and written back by ``apply_to_row(row)``; there
# is deliberately no ``row.integration`` attribute, so there is exactly one
# place a bound can live and no way for the two to drift apart.

ORIGIN_CHEMSTATION = "CHEMSTATION"   # as the instrument integrated it
ORIGIN_MANUAL = "MANUAL"             # bounds set or peak added by the analyst
ORIGIN_SPLIT = "SPLIT"               # fragment of a split peak
ORIGIN_MERGED = "MERGED"             # two peaks integrated as one
ORIGIN_DISABLED = "DISABLED"         # rejected: area 0, row kept for the record
ORIGIN_DECONV = "DECONV"             # fragment of a deconvolution-driven split

#: German labels for the ``Integration`` column and the report (SS VI.14).
INTEGRATION_LABELS = {
    ORIGIN_CHEMSTATION: "Automatisch",
    ORIGIN_MANUAL: "Manuell",
    ORIGIN_SPLIT: "Split",
    ORIGIN_MERGED: "Zusammengefasst",
    ORIGIN_DISABLED: "Verworfen",
    ORIGIN_DECONV: "Dekonvolution",
}

#: Every origin except the instrument's own: a row carrying one of these has had
#: its integration touched and must reach ``Manuell_pruefen`` (SS VI.14).
TOUCHED_ORIGINS = frozenset(INTEGRATION_LABELS) - {ORIGIN_CHEMSTATION}

#: Baseline models of :class:`gc_integrate.Integration`.
BASELINE_ENDPOINT = "endpoint"
BASELINE_CLUSTER = "cluster"
BASELINE_MANUAL = "manual"
BASELINE_DROP = "drop"

#: Review note every row created or re-integrated by hand carries (SS VI.7).
MANUAL_INTEGRATION_REVIEW = "Manuelle Integration; Prüfung"

#: Audit fields that make an ``EditRecord`` an *integration* action rather than
#: an ordinary cell edit. The ``Integration`` sheet is the projection of the
#: audit trail over exactly these (SS VI.14), so a command that creates, splits,
#: merges or disables a row records ``integration_origin`` and the sheet picks
#: it up without a second set of books.
INTEGRATION_AUDIT_FIELDS = frozenset({
    "fid_start", "fid_end", "fid_baseline", "integration_origin",
})

#: Fields the analyst may edit. Everything else is read-only or derived.
#:
#: ``raw_area`` and ``blank_area`` belong to the NIAS/FID path and live in
#: ``PeakRow.derived``; the two properties below make them look like ordinary
#: attributes so snapshot/reset/override handling needs no special case.
#:
#: ``fid_start``/``fid_end`` are editable twice over: as a typed cell in the
#: grid and as the field the mouse writes when a bound is dragged (SS VI.13).
#: Both go to the same place, so the two ways in cannot disagree.
EDITABLE_FIELDS = frozenset({
    "name", "cas", "area", "height", "start_tm", "end_tm", "mark", "si",
    "raw_area", "blank_area", "fid_start", "fid_end",
})

#: Editable fields stored in ``PeakRow.derived`` rather than as dataclass
#: fields. ``as_dict`` must not emit them twice, and the session file has to
#: read them back through ``getattr``.
DERIVED_EDITABLE_FIELDS = frozenset({"raw_area", "blank_area"})

#: Fields recomputed from the raw values; never edited directly.
DERIVED_FIELDS = ("area_pct", "height_pct", "a_h")

#: Edits that invalidate the whole sample's derived block, not just their row.
#: A single area moves every percentage on the DIN SPEC path and every
#: concentration on the NIAS path, because the quantification factor is built
#: from the standards' areas (spec v2.1 SS V.1).
#:
#: ``fid_start``/``fid_end`` join them for the same reason one step earlier: a
#: moved bound moves an area, and a moved area rebuilds ``mean_factor``
#: (SS VI.13). Requantifying the whole determination off a dragged bound is the
#: point, not a side effect.
RECALC_FIELDS = frozenset({"area", "height", "raw_area", "blank_area",
                           "fid_start", "fid_end"})


@dataclass
class PeakRow:
    """One peak of one injection."""

    row_id: int                      # stable identity, never reused
    peak_no: int                     # display number, renumbered by retention time
    source: str                      # "INT_TIC+PBM" | "INT_TIC" | "PBM_ONLY"

    rt: float
    start_tm: Optional[float] = None
    end_tm: Optional[float] = None
    base_mz: Optional[int] = None

    area: Optional[float] = None
    area_pct: Optional[float] = None       # derived from the displayed Area
    pbm_area_pct: Optional[float] = None   # as reported by the library search
    height: Optional[float] = None
    height_pct: Optional[float] = None     # derived; not present in the raw data
    a_h: Optional[float] = None            # derived

    pk_ty: str = ""                  # ChemStation peak type: BB, BV, PV, VV
    mark: str = ""                   # manual annotation; not present in the .D

    # Original numbering of the two source lists, kept for traceability back
    # into RESULTS.CSV. They collide with each other, hence the renumbering.
    tic_peak_no: Optional[int] = None
    pbm_peak_no: Optional[int] = None

    name: str = ""
    cas: str = "0"
    si: Optional[float] = None
    ref: str = ""
    alt_hits: list[tuple[str, str, int]] = field(default_factory=list)

    # raw-data linkage (scan numbers are 1-based as ChemStation reports them)
    first_scan: Optional[int] = None
    max_scan: Optional[int] = None
    last_scan: Optional[int] = None
    apex_scan: int = 0               # 0-based index into DataMS
    bg_scan: int = 0                 # 0-based index into DataMS
    bounds_rule: str = ""            # "INT_TIC" | "DERIVED"
    apex_nudge: int = 0              # -1 / 0 / +1, see the spec on ChemStation's Max

    # -- FID integration (SS VI.13) ---------------------------------------
    # The system of record for the chromatogram bounds. Written by
    # ``RESULTS.CSV`` at load, by the grid when the analyst types, and by the
    # mouse when a bound is dragged; ``gc_integrate.Integration`` only ever
    # borrows them.
    fid_start: Optional[float] = None                       # min
    fid_end: Optional[float] = None                         # min
    fid_baseline: str = BASELINE_ENDPOINT
    fid_anchor_l: Optional[tuple[float, float]] = None      # (rt, y), manual
    fid_anchor_r: Optional[tuple[float, float]] = None      # (rt, y), manual
    #: ChemStation area / raw integral over the reported bounds. Per-peak ratio
    #: scaling (SS VI.5): with it an untouched peak reproduces its reported area
    #: bit for bit, and the baseline-model error cancels for the peak it was
    #: measured on. ``None`` for a peak ChemStation never reported -- those fall
    #: back to ``gc_integrate.AREA_SCALE``.
    fid_scale: Optional[float] = None
    fid_pk_ty: str = ""              # BB/BV/VB/VV/PV from RESULTS.CSV
    integration_origin: str = ORIGIN_CHEMSTATION

    # -- quality figures, computed lazily by gc_qc (SS VI.11 / SS VI.12) ----
    sn: Optional[float] = None       # 2 * height / noise_pp
    purity: Optional[float] = None   # percent, MS peak purity
    ri: Optional[float] = None       # Kovats retention index

    derived: dict[str, Any] = field(default_factory=dict)
    original: dict[str, Any] = field(default_factory=dict)
    #: Fields whose current value differs from the snapshot. Drives the
    #: "edited" cell colour, and therefore includes a value that a *dependent*
    #: recomputation moved -- a corrected area following its components.
    edited: set[str] = field(default_factory=set)
    #: The per-cell override set: fields the analyst typed a value into. A
    #: recalculation skips these and never writes over them (spec v2.1 SS V.1).
    #: Deliberately narrower than ``edited``: a corrected area recomputed from
    #: an edited raw area is shown as edited but is not itself an override, or
    #: the next component edit could no longer reach it.
    manual: set[str] = field(default_factory=set)

    id_status: str = ID_ACCEPTED
    review_done: bool = False
    unknown_override: Optional[bool] = None   # None -> the automatic rule applies

    # -- classification ----------------------------------------------------

    @property
    def is_unknown(self) -> bool:
        """Whether this peak belongs in the unknown register.

        Follows the existing name-based convention (``is_unidentified_item`` in
        the main script): a peak carrying the ``unknown (m/z …)`` name is an
        unknown, while a class-inferred row such as "possible derivative of
        hydrocarbon" is uncertain but not registered as unknown.
        """
        if self.unknown_override is not None:
            return self.unknown_override
        return self.id_status == ID_UNKNOWN or is_no_hit(self.name)

    @property
    def needs_review(self) -> bool:
        """Any identification the engine did not accept, until confirmed."""
        return self.id_status != ID_ACCEPTED and not self.review_done

    @property
    def effective_apex(self) -> int:
        return self.apex_scan + self.apex_nudge

    # -- FID integration ---------------------------------------------------

    @property
    def integration_label(self) -> str:
        """German label of the integration origin, for the grid and the report."""
        return INTEGRATION_LABELS.get(self.integration_origin,
                                      self.integration_origin)

    @property
    def integration_touched(self) -> bool:
        """True when this row's integration was changed by hand.

        Either the origin says so -- manual, split, merged, added or disabled --
        or a bound was moved while the peak stayed the instrument's. Both reach
        ``Manuell_pruefen`` and the ``Integration`` sheet (SS VI.14).
        """
        return (self.integration_origin in TOUCHED_ORIGINS
                or bool(self.edited & {"fid_start", "fid_end"}))

    @property
    def fid_bounds(self) -> Optional[tuple[float, float]]:
        """``(start, end)`` in minutes, or None when the row has no bounds."""
        if self.fid_start is None or self.fid_end is None:
            return None
        return (self.fid_start, self.fid_end)

    # -- FID area components (NIAS) ----------------------------------------
    #
    # The raw and the blank area come out of AutoLib's blank-correction audit
    # and are kept in ``derived`` with everything else the engine produced.
    # They are nonetheless editable (spec v2.1 SS V.1), so they are exposed as
    # real attributes: ``snapshot``, ``reset_field`` and the session file all
    # go through getattr/setattr and must not need to know where a field lives.

    @property
    def raw_area(self) -> Optional[float]:
        """FID area before blank subtraction, as integrated."""
        return self.derived.get("raw_area")

    @raw_area.setter
    def raw_area(self, value: Optional[float]) -> None:
        self.derived["raw_area"] = value

    @property
    def blank_area(self) -> Optional[float]:
        """Area of the blank signal matched to this peak, 0.0 when none was."""
        return self.derived.get("blank_area")

    @blank_area.setter
    def blank_area(self, value: Optional[float]) -> None:
        self.derived["blank_area"] = value

    @property
    def protected_standard(self) -> bool:
        """True for an internal standard: never blank-subtracted (SS 11.4)."""
        return self.derived.get("protected_standard") == "Ja"

    @property
    def library_hits(self) -> list[tuple[str, str, Any]]:
        """``(name, CAS, quality)`` for the peak's library hits, rank 1 first.

        The NIAS path stores the whole list, because AutoLib may replace the
        top hit's name with "unknown (m/z …)" or an inferred class, so the row's
        own name is not the hit's name. The DIN SPEC path writes the top hit
        into ``name``/``cas``/``si`` and keeps only the alternatives, so hit 1
        is reconstructed from the row there.
        """
        hits = self.derived.get("pbm_hits")
        if hits:
            return [tuple(h) for h in hits]
        if self.alt_hits:
            return ([(self.name, display_cas(self.cas), self.si)]
                    + [tuple(h) for h in self.alt_hits])
        return []

    def reclassify(self, quality_limit: float = DEFAULT_QUALITY_LIMIT) -> None:
        self.id_status = classify_identification(self.name, self.si, quality_limit)

    # -- editing -----------------------------------------------------------

    def snapshot(self) -> None:
        """Record the values as loaded. Call once, after construction."""
        self.original = {f: getattr(self, f) for f in EDITABLE_FIELDS}

    def is_edited(self, field_name: str) -> bool:
        return field_name in self.edited

    def is_manual(self, field_name: str) -> bool:
        """True when the analyst typed this cell -- see :attr:`manual`."""
        return field_name in self.manual

    def mark_edited(self, field_name: str, manual: bool = False) -> None:
        """Flag one field against its snapshot, keeping SS V.1's distinction.

        ``manual=True`` is for a value the analyst **typed**: it becomes a
        sticky override that no recalculation may write over. ``manual=False``
        is for a value something else moved -- a dragged bound, and the
        ``raw_area`` that bound recomputed. Those show in the "edited" colour but
        stay *reachable*: the next drag has to be able to move the area again,
        or the feature deadlocks after one edit (SS VI.13).

        A field back at its loaded value is neither edited nor manual again, so
        dragging a bound back where it came from leaves no trace behind.
        """
        if _same(getattr(self, field_name, None), self.original.get(field_name)):
            self.edited.discard(field_name)
            self.manual.discard(field_name)
            return
        self.edited.add(field_name)
        if manual:
            self.manual.add(field_name)

    def overrides(self) -> dict[str, Any]:
        """The per-cell override set: every field the analyst moved by hand.

        A manual value is sticky until it is reset (spec v2.1 SS V.1), so any
        recalculation has to skip the fields returned here rather than write
        over an analyst's number. Derived from ``original``, which
        :meth:`snapshot` took at load time, so it survives ``recalculate()``
        without a second bookkeeping structure that could drift out of sync.
        """
        return {f: getattr(self, f, None) for f in sorted(self.manual)}

    def reset_field(self, field_name: str) -> Any:
        """Restore one field to its loaded value. Returns the restored value.

        Only clears the override; the caller recalculates, which is what
        rebuilds a value that is computed from other (possibly still edited)
        cells rather than simply loaded.
        """
        if field_name in self.original:
            setattr(self, field_name, self.original[field_name])
            self.edited.discard(field_name)
            self.manual.discard(field_name)
        return getattr(self, field_name, None)

    def as_dict(self) -> dict[str, Any]:
        """Flat mapping used by the grid and the session file."""
        out = {
            f.name: getattr(self, f.name)
            for f in self.__dataclass_fields__.values()  # type: ignore[attr-defined]
            if f.name not in {"derived", "original", "edited", "manual",
                              "alt_hits"}
        }
        out["alt_hits"] = [list(h) for h in self.alt_hits]
        out["edited"] = sorted(self.edited)
        out.update(self.derived)
        return out


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

@dataclass
class EditRecord:
    timestamp: str
    sample: str
    row_id: int
    peak_no: int
    field: str
    old: Any
    new: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp, "sample": self.sample,
            "row_id": self.row_id, "peak_no": self.peak_no,
            "field": self.field, "old": self.old, "new": self.new,
        }


# --------------------------------------------------------------------------
# Sample -- one injection / one .D folder
# --------------------------------------------------------------------------

class Sample:
    """Rows plus raw-data access for a single injection.

    Spectra are decoded on demand and cached per row: decoding every scan up
    front costs ~9 MB per sample for no benefit, while a single scan decodes in
    about 60 microseconds.
    """

    def __init__(self, label: str, path, rows: list[PeakRow],
                 ms=None, meta: Optional[dict[str, Any]] = None):
        self.label = label            # "A" | "B" | "C"
        self.path = path              # the .D directory
        self.rows = rows
        self.ms = ms                  # extract_ms_spectra.DataMS, or None
        self.meta = meta or {}
        #: (row_id, apex_nudge, bg_override) -> spectrum. The background is part
        #: of the key so a picked background (SS VII.7) cannot poison the entry
        #: the row's own ``bg_scan`` produced.
        self._spectra: dict[tuple[int, int, Optional[int]],
                            list[tuple[float, int]]] = {}
        #: row_id of the internal standard; without it no concentration can be
        #: computed and the derived columns stay blank.
        #: Threshold that separates an identification from a bad match. The
        #: NIAS engine applies it before we see a row; DIN SPEC does not, so the
        #: grid applies it there.
        self.quality_limit: float = DEFAULT_QUALITY_LIMIT
        self.is_row_id: Optional[int] = None
        #: Which of IS1/IS2/IS3 the selected row represents. Required by the
        #: export; the engine rejects any other spelling.
        self.is_code: Optional[str] = None
        #: {normalised CAS: {"sml", "reference", "footnote"}} from CASINFO.xlsx
        self.cas_lookup: dict[str, dict[str, Any]] = {}
        self.recalculate()

    # -- lookup ------------------------------------------------------------

    def row(self, row_id: int) -> Optional[PeakRow]:
        # The grid asks for every visible row on every paint, so a linear scan
        # made each refresh quadratic. ``rows`` is edited in place all over
        # the code base, so the index is a hint: a hit is only trusted after
        # checking the row is still at that position, a miss rebuilds it.
        index = self.__dict__.get("_row_index")
        if index is not None:
            pos = index.get(row_id)
            if pos is not None and pos < len(self.rows):
                r = self.rows[pos]
                if r.row_id == row_id:
                    return r
        index = {}
        for pos, r in enumerate(self.rows):
            index.setdefault(r.row_id, pos)
        self._row_index = index
        pos = index.get(row_id)
        return self.rows[pos] if pos is not None else None

    # -- derived columns ---------------------------------------------------

    def recalculate(self) -> None:
        """Recompute every derived column across the whole sample.

        Area% is normalised over the rows that actually carry an Area, so the
        column is always consistent with the Area column shown next to it. The
        library search's own percentage is kept separately in ``pbm_area_pct``.
        """
        areas = [r.area for r in self.rows if r.area is not None]
        total = sum(areas) if areas else 0.0
        heights = [r.height for r in self.rows if r.height is not None]
        max_height = max(heights) if heights else 0.0

        for r in self.rows:
            r.area_pct = (r.area / total * 100.0) if (r.area is not None and total) else None
            r.height_pct = (
                r.height / max_height * 100.0
                if (r.height is not None and max_height) else None
            )
            r.a_h = (
                r.area / r.height
                if (r.area is not None and r.height not in (None, 0)) else None
            )
            r.reclassify(self.quality_limit)

        self._recalculate_derived()

    def _recalculate_derived(self) -> None:
        """Concentration, SML and traffic-light status for every row.

        Without a selected internal standard there is nothing to quantify
        against, so the concentration columns stay blank rather than showing a
        number that would be wrong.
        """
        is_row = self.row(self.is_row_id) if self.is_row_id is not None else None
        is_area = is_row.area if is_row is not None else None

        for r in self.rows:
            conc_ugl: Optional[float] = None
            if r is is_row:
                conc_ugl = IS_CONCENTRATION_UG_L
            elif is_area not in (None, 0) and r.area is not None:
                conc_ugl = r.area / is_area * IS_CONCENTRATION_UG_L

            conc_kg = conc_ugl * UG_L_TO_MG_KG if conc_ugl is not None else None
            info = self.cas_lookup.get(display_cas(r.cas)) or {}
            sml = sml_limit(info.get("sml"))
            text, fill, font = report_status(r.is_unknown, sml, conc_kg)

            r.derived = {
                "conc_ugl": conc_ugl,
                "conc_mgkg": conc_kg,
                "sml": sml,
                "reference": info.get("reference", ""),
                "status": text,
                "status_fill": fill,
                "status_font": font,
                "below_limit": (conc_ugl is not None
                                and conc_ugl < REPORTING_LIMIT_UG_L
                                and r is not is_row),
            }

    def set_internal_standard(self, row_id: Optional[int],
                              code: Optional[str] = None) -> None:
        self.is_row_id = row_id
        if row_id is None:
            self.is_code = None
        else:
            row = self.row(row_id)
            self.is_code = code or (guess_internal_standard_code(row.name)
                                    if row is not None else None)
        self.recalculate()

    def set_cas_lookup(self, lookup: dict[str, dict[str, Any]]) -> None:
        self.cas_lookup = lookup or {}
        self.recalculate()

    # -- editing -----------------------------------------------------------

    def validate_edit(self, row: PeakRow, field_name: str, value: Any) -> None:
        """Reject an edit before it is applied. Raise ``ValueError`` to refuse.

        The base model has no rule of its own; the NIAS sample uses this to
        protect the three quantification standards (SS 11.2), whose area may
        never become 0 or blank.
        """

    def after_edit(self, row: PeakRow, field_name: str) -> None:
        """Propagate one applied edit inside its own row, before recalculation.

        The NIAS sample uses this for the one-directional area rule of SS V.1:
        editing a component recomputes the corrected area, editing the
        corrected area decouples it from its components.
        """

    def set_value(self, row_id: int, field_name: str, value: Any) -> Optional[EditRecord]:
        """Apply one manual edit. Returns the audit record, or None if unchanged.

        Raises ValueError when the field is not editable or the value cannot be
        coerced; the caller keeps the editor open and shows the message.
        """
        if field_name not in EDITABLE_FIELDS:
            raise ValueError(f"Spalte '{field_name}' ist nicht editierbar.")
        row = self.row(row_id)
        if row is None:
            raise ValueError(f"Zeile {row_id} existiert nicht.")

        new = _coerce(field_name, value)
        old = getattr(row, field_name)
        if _same(old, new):
            return None

        _check_fid_bounds(row, field_name, new)
        self.validate_edit(row, field_name, new)

        setattr(row, field_name, new)
        # Typed by the analyst, so it is a sticky override (spec v2.1 SS V.1).
        row.mark_edited(field_name, manual=True)

        self.after_edit(row, field_name)

        # An edit to Name or CAS can change the classification; an edit to an
        # area or a height changes every percentage -- and, on the NIAS path,
        # every concentration -- in the sample.
        if field_name in RECALC_FIELDS:
            self.recalculate()
        else:
            row.reclassify(self.quality_limit)

        return EditRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            sample=self.label, row_id=row.row_id, peak_no=row.peak_no,
            field=field_name, old=old, new=new,
        )

    # -- raw data ----------------------------------------------------------

    def _clamp_scan(self, scan: int) -> int:
        """A scan index forced into the file's range.

        Every caller of ``build_spectrum`` needs the same guard, and a picked
        scan (SS VII.7) comes straight off a mouse position, so it is the one
        most likely to sit one past the last scan.
        """
        return min(max(int(scan), 0), self.ms.n_scans - 1)

    def spectrum(self, row_id: int, normalise: bool = False,
                 min_permille: float = 1.0,
                 bg_override: Optional[int] = None) -> list[tuple[float, int]]:
        """Background-corrected spectrum for one row, cached.

        ``bg_override`` (SS VII.7) replaces the row's own ``bg_scan`` with a
        background the analyst picked in the MS window. It is part of the cache
        key, so a picked background cannot poison the entry the row's own
        ``bg_scan`` produced: leaving the picked background switches back to
        the cached default instead of rebuilding it.

        The argument is appended last and defaults to ``None``, so every v3.0
        call form -- ``spectrum(row_id)``, ``spectrum(row_id, True, 5.0)`` --
        returns exactly what it returned before.
        """
        row = self.row(row_id)
        if row is None or self.ms is None:
            return []
        bg = None if bg_override is None else int(bg_override)
        key = (row.row_id, row.apex_nudge, bg)
        hit = self._spectra.get(key)
        if hit is not None:
            return hit

        import extract_ms_spectra as ex
        apex = self._clamp_scan(row.effective_apex)
        ref = row.bg_scan if bg is None else self._clamp_scan(bg)
        spec = ex.build_spectrum(self.ms, apex, ref, 1.0,
                                 normalise, min_permille)
        self._spectra[key] = spec
        return spec

    def spectrum_at(self, scan: int, bg: Optional[int] = None,
                    normalise: bool = False, min_permille: float = 1.0
                    ) -> list[tuple[float, int]]:
        """Spectrum of one scan, background-corrected against ``bg`` (SS VII.7).

        No correction when ``bg`` is None. Uncached: a free pick is a one-off,
        one ``build_spectrum`` costs about 8 ms on the reference data, and
        caching it would hold a spectrum that belongs to no row.

        The subtraction goes through ``extract_ms_spectra.build_spectrum`` and
        not through the caller, because ``Mit NIST suchen``, ``Spektrum als MSP
        kopieren`` and ``Als MSP speichern...`` all operate on whatever the
        panel displays: a second subtraction path would let the MSP file and
        the plot disagree.
        """
        if self.ms is None:
            return []
        import extract_ms_spectra as ex
        apex = self._clamp_scan(scan)
        # ``build_spectrum`` skips the subtraction when bg == apex, which is
        # exactly "no background correction" -- so None maps onto the scan
        # itself rather than onto a second code path.
        ref = apex if bg is None else self._clamp_scan(bg)
        return ex.build_spectrum(self.ms, apex, ref, 1.0,
                                 normalise, min_permille)

    def invalidate_spectrum(self, row_id: int) -> None:
        for key in [k for k in self._spectra if k[0] == row_id]:
            del self._spectra[key]

    def tic_window(self, rt_lo: float, rt_hi: float) -> tuple[list[float], list[int]]:
        """(retention times, intensities) for an RT window, endpoints included."""
        if self.ms is None:
            return [], []
        import bisect
        lo = max(bisect.bisect_left(self.ms.rt, rt_lo) - 1, 0)
        hi = min(bisect.bisect_right(self.ms.rt, rt_hi) + 1, self.ms.n_scans)
        return self.ms.rt[lo:hi], self.ms.tic[lo:hi]

    def rows_in_window(self, rt_lo: float, rt_hi: float) -> list[PeakRow]:
        """Peaks whose retention time falls inside a window -- plot context."""
        return [r for r in self.rows if rt_lo <= r.rt <= rt_hi]

    # -- summaries ---------------------------------------------------------

    @property
    def unknowns(self) -> list[PeakRow]:
        return [r for r in self.rows if r.is_unknown]

    @property
    def open_reviews(self) -> list[PeakRow]:
        return [r for r in self.rows if r.needs_review]


# --------------------------------------------------------------------------
# Session -- the set of injections being processed together
# --------------------------------------------------------------------------

#: Session file format written by this build (SS VI.13). Format 2 still loads;
#: a format-3 file is refused by older builds by design, because they would drop
#: the integration state on the next save without noticing.
SESSION_FORMAT = 3

#: Oldest format this build reads.
SESSION_FORMAT_MIN = 2

#: Row state the session file carries beyond the manual overrides. These are
#: values no re-run of ``RESULTS.CSV`` can reproduce, because they are the
#: analyst's integration, not the instrument's.
SESSION_ROW_FIELDS = (
    "fid_start", "fid_end", "fid_baseline", "fid_anchor_l", "fid_anchor_r",
    "fid_scale", "fid_pk_ty", "integration_origin",
)


def session_row_state(row: PeakRow) -> dict[str, Any]:
    """The integration state of one row, for the session file (format 3).

    Only rows whose integration was touched need this: an untouched row is
    refilled from ``RESULTS.CSV`` on the next load and would only make the file
    bigger. ``edited``/``manual`` travel too, because the SS V.1 distinction is
    not derivable from the values alone -- a dragged bound and a typed bound
    look identical afterwards, and only one of them is sticky.
    """
    state: dict[str, Any] = {f: getattr(row, f, None) for f in SESSION_ROW_FIELDS}
    state["edited"] = sorted(row.edited)
    state["manual"] = sorted(row.manual)
    return state


def apply_session_row_state(row: PeakRow, state: dict[str, Any]) -> None:
    """Restore :func:`session_row_state`, tolerating a format-2 entry.

    A format-2 session carries none of these keys. Every one of them is then
    simply left at the value the fresh load from ``RESULTS.CSV`` produced --
    the instrument's own bounds, its ``PK TY`` and ``integration_origin ==
    CHEMSTATION`` -- so an old session loses nothing it ever had (SS VI.13).
    """
    if not state:
        return
    for field_name in SESSION_ROW_FIELDS:
        if field_name not in state:
            continue                     # format 2: keep what the .D supplied
        value = state[field_name]
        if field_name in {"fid_anchor_l", "fid_anchor_r"} and value is not None:
            value = tuple(value)
        setattr(row, field_name, value)
    if not row.integration_origin:
        row.integration_origin = ORIGIN_CHEMSTATION
    # Restored after the values, so ``mark_edited``'s snapshot comparison does
    # not immediately discard them again.
    for key, target in (("edited", row.edited), ("manual", row.manual)):
        for name in state.get(key) or []:
            if name in EDITABLE_FIELDS:
                target.add(name)


def migrate_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bring a stored session up to format 3. Raises on a file from the future.

    Format 2 needs no rewriting: everything format 3 adds is either refilled
    from ``RESULTS.CSV`` on load or defaults to the instrument's own
    integration. The function exists so there is one place that decides what an
    old file means, rather than a scatter of ``payload.get`` defaults.
    """
    payload = dict(payload or {})
    stored = int(payload.get("format")
                 or payload.get("schema_version") or SESSION_FORMAT_MIN)
    if stored > SESSION_FORMAT:
        raise ValueError(
            f"Diese Sitzung wurde mit einer neueren Version gespeichert "
            f"(Format {stored}, diese Version kennt {SESSION_FORMAT}).")
    if stored < SESSION_FORMAT_MIN:
        raise ValueError(f"Sitzungsformat {stored} wird nicht mehr gelesen.")
    payload["format"] = SESSION_FORMAT
    payload["loaded_format"] = stored
    return payload


def integration_record(sample_label: str, row: PeakRow, field_name: str,
                       old: Any, new: Any) -> EditRecord:
    """An audit record for an integration action, for the command objects.

    ``SetBounds`` and friends do not go through :meth:`Sample.set_value` -- they
    move several rows at once and own their undo -- but the ``Integration``
    sheet is a projection of the audit trail and nothing else (SS VI.14). This
    is the one way in: append the record to ``Session.audit`` and the sheet has
    it. ``field_name`` should be one of :data:`INTEGRATION_AUDIT_FIELDS`.
    """
    return EditRecord(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        sample=sample_label, row_id=row.row_id, peak_no=row.peak_no,
        field=field_name, old=old, new=new,
    )


class Session:
    """A/B/C injections, the audit trail, and the project file location."""

    SCHEMA_VERSION = SESSION_FORMAT

    def __init__(self, project_path=None):
        self.project_path = project_path
        self.samples: dict[str, Sample] = {}
        self.audit: list[EditRecord] = []
        self.active: str = ""

    def add_sample(self, sample: Sample) -> None:
        self.samples[sample.label] = sample
        if not self.active:
            self.active = sample.label

    @property
    def sample(self) -> Optional[Sample]:
        return self.samples.get(self.active)

    @property
    def labels(self) -> list[str]:
        return sorted(self.samples)

    def set_value(self, field_name: str, row_id: int, value: Any,
                  label: str = "") -> Optional[EditRecord]:
        target = self.samples.get(label or self.active)
        if target is None:
            raise ValueError("Keine Probe geladen.")
        record = target.set_value(row_id, field_name, value)
        if record is not None:
            self.audit.append(record)
            target.invalidate_spectrum(row_id)
        return record

    @property
    def dirty(self) -> bool:
        return bool(self.audit)

    def audit_rows(self) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self.audit]

    def pending_unknowns(self) -> list[tuple[str, PeakRow]]:
        """Every unknown across all injections, for the pre-export dialog."""
        out: list[tuple[str, PeakRow]] = []
        for label in self.labels:
            out.extend((label, r) for r in self.samples[label].unknowns)
        return out

    def open_reviews(self) -> list[tuple[str, PeakRow]]:
        out: list[tuple[str, PeakRow]] = []
        for label in self.labels:
            out.extend((label, r) for r in self.samples[label].open_reviews)
        return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_NUMERIC_FIELDS = {"area", "height", "start_tm", "end_tm", "si",
                   "raw_area", "blank_area", "fid_start", "fid_end"}

#: Areas that may never go negative -- an integrated signal cannot.
_NON_NEGATIVE_FIELDS = {"area", "height", "raw_area", "blank_area"}

#: Retention times that may never go negative -- the run starts at 0.
_NON_NEGATIVE_TIME_FIELDS = {"fid_start", "fid_end"}


def _check_fid_bounds(row: "PeakRow", field_name: str, value: Any) -> None:
    """Refuse an integration bound that would invert the peak.

    A start at or after the end is not a narrow peak, it is an empty one: the
    integral is zero or negative and every number downstream of it is nonsense.
    Rejected here rather than clipped, because silently moving the analyst's
    number is worse than saying no.
    """
    if field_name not in _NON_NEGATIVE_TIME_FIELDS or value is None:
        return
    start = value if field_name == "fid_start" else row.fid_start
    end = value if field_name == "fid_end" else row.fid_end
    if start is not None and end is not None and start >= end:
        raise ValueError(
            "Der Integrationsbeginn muss vor dem Integrationsende liegen.")


def _coerce(field_name: str, value: Any) -> Any:
    """Turn an editor string into the field's stored type."""
    if field_name == "cas":
        return clean_cas(value)
    if field_name in {"name", "mark"}:
        return clean_name(value)
    if field_name in _NUMERIC_FIELDS:
        if value in (None, ""):
            return None
        parsed = num(value)
        if parsed is None:
            raise ValueError(f"'{value}' ist keine Zahl.")
        if field_name in _NON_NEGATIVE_FIELDS and parsed < 0:
            raise ValueError("Fläche und Höhe dürfen nicht negativ sein.")
        if field_name in _NON_NEGATIVE_TIME_FIELDS and parsed < 0:
            raise ValueError("Retentionszeiten dürfen nicht negativ sein.")
        if field_name == "si" and not 0 <= parsed <= 100:
            raise ValueError("SI muss zwischen 0 und 100 liegen.")
        return parsed
    return value


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)
        except (TypeError, ValueError):
            return False
    return a == b


def allocate_ids(start: int = 1) -> Iterable[int]:
    """Monotonic row-id source. One per session; ids are never reused."""
    n = start
    while True:
        yield n
        n += 1

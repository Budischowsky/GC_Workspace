#!/usr/bin/env python3
"""NIAS Doppelbestimmung: FID quantification, standards, blank correction.

This is the **NIAS** pipeline and has nothing in common with DIN SPEC 91521
beyond the raw-data reader. NIAS runs two determinations, quantifies on the
blank-corrected **FID** area, and detects all three internal standards
automatically by target retention time. DIN SPEC runs three determinations,
quantifies on the MS/TIC area, and uses one hand-picked standard.

The engine of record is ``AutoLib\\Geänderten Python-Code herunterladen.py``,
which already implements peak parsing, the FID-MS delay, FID/PBM assignment,
blank subtraction, standard detection and the duplicate merge. This module wraps
it so the workspace can show and edit what it computes; none of that maths is
reimplemented here.
"""

from __future__ import annotations

import importlib.util
import statistics
import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Optional

import extract_ms_spectra as ex
import gc_model as M

_ENGINE = None
_ENGINE_FILENAME = "Geänderten Python-Code herunterladen.py"

#: How many library hits PBM reports per peak. ``parse_library_report`` stops
#: at three, and ``common_class`` looks at exactly ``hits[:3]``.
MAX_PBM_HITS = 3

#: Parameter sheet rows that are report metadata rather than calculation inputs.
REPORT_METADATA_KEYS = (
    # ``syneris_sample`` was removed here per spec v2.1 SS V.3: the sample number
    # is derived from the folder name rather than typed. An old Parameter sheet
    # may still carry the row - it is accepted on input and dropped, never
    # re-emitted, so reading a pre-v2.1 workbook must not fail.
    "syneris_report", "analyst", "migration_cell",
    "cell_area_label", "coverage_label", "effective_area", "volume_ml",
    "duration", "temperature", "simulant", "migrate",
)


def engine():
    """Load the AutoLib module once.

    Mirrors ``NIAS.py:gc_engine()``: the filename contains spaces and an umlaut,
    so it cannot be imported by name.
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    path = Path(__file__).resolve().parent / "AutoLib" / _ENGINE_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"GC-Datenmodul nicht gefunden: {path}")
    spec = importlib.util.spec_from_file_location("nias_gc_data_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Das GC-Datenmodul konnte nicht geladen werden.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _ENGINE = module
    return module


# --------------------------------------------------------------------------
# Parameter sheet
# --------------------------------------------------------------------------

#: (settings attribute, label, unit/note) in the order of the Parameter sheet.
#: Matches the reference workbook so the exported sheet stays recognisable.
PARAMETER_LAYOUT: tuple[tuple[Optional[str], str, str], ...] = (
    (None, "Sample name", "used for result file and workbook identification"),
    ("reporting_limit", "Reporting limit", "mg/kg; applied at workbook generation"),
    ("solvent_end", "Solvent end", "min"),
    ("quality_limit", "Quality threshold", ""),
    ("rt_tolerance", "Duplicate RT tolerance", "min"),
    ("duplicate_max_reldiff", "Duplicate difference limit",
     "%; above it the pair is flagged"),
    (None, "FID-MS delay determination 1", "min"),
    (None, "FID-MS delay determination 2", "min"),
    ("cell_area_dm2", "Cell area", "dm2"),
    ("coverage", "Coverage", ""),
    ("ov_ratio", "O/V ratio", ""),
    ("is_amount", "Internal standard amount", ""),
    ("fc17_conc", "FC17 concentration", "mg/mL"),
    ("bbp_conc", "BBP-d4 concentration", "mg/mL"),
    ("dnnp_conc", "DnNP-d4 concentration", "mg/mL"),
    ("blank_rt_tolerance", "Blank RT tolerance", "min; one-to-one FID apex matching"),
    (None, "Blank file", ""),
    (None, "Blank+ISTD file", ""),
    (None, "Blank FID-MS delay", "min"),
    (None, "Blank+ISTD FID-MS delay", "min"),
    # Appended at the *end* on purpose (spec v3.1 SS VII.15 assumption 1).
    # ``gc_export._parameter_ref`` turns a position in this tuple into
    # ``Parameter!$B$n``; inserting a row mid-list silently moves every formula
    # reference below it, so a new parameter goes here and nowhere else.
    ("extract_volume_ml", "Extract volume", "mL; NIAS total extraction"),
)

#: Default for ``duplicate_max_reldiff`` in percent (spec v3.1 SS VII.4).
#: ``gc_duplicate.DEFAULT_MAX_RELDIFF`` is the canonical copy; repeated here for
#: the same reason ``gc_duplicate`` repeats ``SINGLE_DETERMINATION_STATUS``
#: rather than importing it -- the Parameter sheet must keep working even when
#: the rule module is not on the path -- and a test asserts the two stay equal.
DEFAULT_MAX_RELDIFF = 30.0

#: Volume of the NIAS total-extraction extract, in mL (spec v3.1 SS VII.15
#: assumption 1, resolved). The analyst's statement -- "the extract is 10 mL,
#: spiked with the NIAS ISTD" -- is what turns the ISTD *amount* into an ISTD
#: *concentration*, which is the number SS VII.10 quantifies ``c [µg/L]`` with.
DEFAULT_EXTRACT_VOLUME_ML = 10.0

#: The three quantification standards' concentration attributes, in the order
#: ``IS_DEFS`` lists them. The QC standard has none, which is exactly what
#: separates it from the three (SS 11.2).
ISTD_CONCENTRATION_ATTRS: tuple[str, ...] = ("fc17_conc", "bbp_conc", "dnnp_conc")

#: Editable numeric parameters, with the bounds a value must stay inside.
PARAMETER_BOUNDS: dict[str, tuple[float, float]] = {
    "reporting_limit": (0.0, 1000.0),
    "solvent_end": (0.0, 60.0),
    "quality_limit": (0.0, 100.0),
    "rt_tolerance": (0.0, 1.0),
    # Spec v3.1 SS VII.4. 0 % flags every pair that disagrees at all, 200 % is
    # the arithmetic ceiling of |c1-c2|/mean*100 for two non-negative results,
    # so the whole meaningful range is reachable and nothing outside it is.
    "duplicate_max_reldiff": (0.0, 200.0),
    "cell_area_dm2": (0.0001, 100.0),
    "coverage": (0.0001, 100.0),
    "ov_ratio": (0.0001, 1000.0),
    "is_amount": (0.0001, 1000.0),
    "fc17_conc": (0.0001, 100.0),
    "bbp_conc": (0.0001, 100.0),
    "dnnp_conc": (0.0001, 100.0),
    "blank_rt_tolerance": (0.0, 1.0),
    "qc_min_area": (0.0, 1e12),
    # 1 µL to 10 L: every extract volume a bench method could plausibly
    # produce, and nothing that would make the derived ISTD
    # concentration meaningless (0 mL would divide by zero).
    "extract_volume_ml": (0.001, 10000.0),
}


def default_settings():
    """A fresh ``AutoLib.Settings`` with the documented defaults."""
    s = engine().Settings()
    # AutoLib's Settings has no reporting limit; the Parameter sheet does.
    if not hasattr(s, "reporting_limit"):
        s.reporting_limit = 0.01
    # Same pattern for the duplicate difference limit (spec v3.1 SS VII.4): the
    # engine decides the duplicate *status* and knows nothing about a relative
    # difference limit, so the attribute is grafted on here rather than in
    # AutoLib -- which must stay untouched.
    if not hasattr(s, "duplicate_max_reldiff"):
        s.duplicate_max_reldiff = DEFAULT_MAX_RELDIFF
    # And for the extract volume (spec v3.1 SS VII.15 assumption 1): AutoLib
    # quantifies in mg/dm2 and mg/kg and never forms an extract concentration,
    # so it has no reason to carry a volume. The NIAS total-extraction report
    # does -- see :func:`derived_istd_concentration_ugl`.
    if not hasattr(s, "extract_volume_ml"):
        s.extract_volume_ml = DEFAULT_EXTRACT_VOLUME_ML
    return s


def settings_as_dict(settings) -> dict[str, Any]:
    out = {f.name: getattr(settings, f.name) for f in dataclass_fields(settings)}
    out["reporting_limit"] = getattr(settings, "reporting_limit", 0.01)
    out["duplicate_max_reldiff"] = getattr(settings, "duplicate_max_reldiff",
                                           DEFAULT_MAX_RELDIFF)
    out["extract_volume_ml"] = getattr(settings, "extract_volume_ml",
                                       DEFAULT_EXTRACT_VOLUME_ML)
    return out


def apply_setting(settings, key: str, value: Any):
    """Coerce and validate one Parameter change. Raises ValueError on bad input."""
    if key not in PARAMETER_BOUNDS:
        raise ValueError(f"'{key}' ist kein editierbarer Parameter.")
    parsed = M.num(value)
    if parsed is None:
        raise ValueError(f"'{value}' ist keine Zahl.")
    low, high = PARAMETER_BOUNDS[key]
    if not low <= parsed <= high:
        raise ValueError(f"Wert muss zwischen {low:g} und {high:g} liegen.")
    if key == "quality_limit":
        parsed = int(round(parsed))
    setattr(settings, key, parsed)
    return settings


def parameter_rows(settings, meta: Optional[dict[str, Any]] = None
                   ) -> list[tuple[str, Any, str, Optional[str]]]:
    """``(label, value, unit, editable-key)`` in Parameter-sheet order."""
    meta = meta or {}
    dynamic = {
        "Sample name": meta.get("sample_name", ""),
        "FID-MS delay determination 1": meta.get("delay_1", ""),
        "FID-MS delay determination 2": meta.get("delay_2", ""),
        "Blank file": meta.get("blank_file", ""),
        "Blank+ISTD file": meta.get("blank_istd_file", ""),
        "Blank FID-MS delay": meta.get("blank_delay", ""),
        "Blank+ISTD FID-MS delay": meta.get("blank_istd_delay", ""),
    }
    rows: list[tuple[str, Any, str, Optional[str]]] = []
    for key, label, unit in PARAMETER_LAYOUT:
        value = getattr(settings, key, "") if key else dynamic.get(label, "")
        rows.append((label, value, unit, key))
    return rows


# --------------------------------------------------------------------------
# Standards
# --------------------------------------------------------------------------

def standard_definitions() -> list[tuple[str, float, str, Optional[str]]]:
    """``IS_DEFS``: name, target RT, role, concentration attribute."""
    return list(engine().IS_DEFS)


#: Role string AutoLib gives the three standards that build the mean factor.
ROLE_QUANTIFICATION = "Quantification"

#: Role of the Dibutyl phthalate-d4 standard (SS 11.2): reported, never
#: quantifying, so it has no factor and never gets one.
ROLE_QC = "QC"

STATUS_FOUND = "Found"
STATUS_NOT_FOUND = "Not found"
STATUS_BELOW_QC = "Below QC minimum"
#: A quantifying ISTD whose concentration has not been typed yet: it has an
#: area, but nothing to quantify with, so it stays out of the factor.
STATUS_NO_CONCENTRATION = "Konzentration fehlt"
#: The one ISTD that quantifies everything when the mean-area box is unticked.
STATUS_REFERENCE = "Referenz"

# --------------------------------------------------------------------------
# Selectable ISTDs
# --------------------------------------------------------------------------
#
# The analyst decides which peaks are internal standards. The four AutoLib
# standards are only the *default* list: they are still found by name and target
# RT on load and prefilled, but any substance can be set as ISTD (right click in
# the grid) or typed into the Standards panel, and a determination loads whether
# or not any standard is present.
#
# The list of definitions is shared by both determinations -- IS2 is the same
# chemical in Bestimmung 1 and 2 -- while each determination keeps its own
# binding to a peak and so its own area (``NiasSample.standards``).

#: RT window for finding an ISTD by retention time alone. The tolerance
#: ``AutoLib.find_standard`` uses, so a remembered ISTD is found the same way
#: the built-in ones are.
ISTD_MATCH_TOLERANCE = 0.08

#: How the mean factor is formed: ``use_mean_area`` ticked is
#: ``mean c x amount / 1000 / mean area / cell / coverage`` over the quantifying
#: ISTDs (the Einzelbestimmung workbook formula); unticked, the one
#: ``reference`` ISTD quantifies everything.
DEFAULT_ISTD_OPTIONS: dict[str, Any] = {"use_mean_area": True, "reference": ""}


def istd_code(n: int) -> str:
    return f"IS{n}"


def next_istd_code(defs: list[dict[str, Any]]) -> str:
    """The lowest ``ISn`` not used yet."""
    used = {str(d.get("code") or "").strip().upper() for d in defs}
    n = 1
    while istd_code(n).upper() in used:
        n += 1
    return istd_code(n)


def default_istd_defs(settings=None) -> list[dict[str, Any]]:
    """IS1..IS4 from ``IS_DEFS``, with the Parameter concentrations.

    Used when the workstation has no remembered list yet. The QC standard
    (DBP-d4) is listed but does not quantify, exactly as before.
    """
    out: list[dict[str, Any]] = []
    for n, (name, rt, role, attr) in enumerate(standard_definitions(), 1):
        conc = M.num(getattr(settings, attr, None)) if (attr and settings) else None
        out.append({"code": istd_code(n), "name": name, "concentration": conc,
                    "quantify": role == ROLE_QUANTIFICATION,
                    "target_rt": float(rt)})
    return out


def normalise_istd_defs(value: Any) -> list[dict[str, Any]]:
    """Coerce a stored list of definitions, dropping what cannot be used.

    Tolerant on purpose: this reads a JSON file on the workstation and the
    session file, and a hand-edited or older file must never stop a load.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code or code.upper() in seen:
            continue
        seen.add(code.upper())
        conc = M.num(item.get("concentration"))
        rt = M.num(item.get("target_rt"))
        out.append({
            "code": code,
            "name": str(item.get("name") or "").strip(),
            "concentration": conc if (conc is not None and conc > 0) else None,
            "quantify": bool(item.get("quantify", True)),
            "target_rt": rt,
        })
    return out


def normalise_istd_options(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {"use_mean_area": bool(value.get("use_mean_area", True)),
            "reference": str(value.get("reference") or "").strip()}


def istd_role(definition: dict[str, Any]) -> str:
    """``Quantification`` or ``QC``: what ``gc_qc`` and the export key on."""
    return ROLE_QUANTIFICATION if definition.get("quantify", True) else ROLE_QC


def reference_code(defs: list[dict[str, Any]],
                   options: Optional[dict[str, Any]]) -> str:
    """The reference ISTD, falling back to the first quantifying one."""
    options = options or DEFAULT_ISTD_OPTIONS
    wanted = str(options.get("reference") or "").strip().upper()
    quantifying = [d for d in defs if d.get("quantify", True)]
    for d in quantifying:
        if str(d.get("code") or "").upper() == wanted:
            return d["code"]
    return quantifying[0]["code"] if quantifying else ""


def _name_matches(row_name: str, istd_name: str) -> bool:
    a, b = (row_name or "").casefold().strip(), (istd_name or "").casefold().strip()
    return bool(a and b) and (a == b or b in a)


def standards_table(standards: list[dict[str, Any]],
                    mean_factor: Optional[float]) -> list[dict[str, Any]]:
    """Flatten the engine's standards into rows for the Standards panel.

    All three quantification standards must be found; ``analyse_determination``
    raises otherwise, because a mean factor over two of them would silently
    misquantify everything.

    This is the load-time snapshot only. From here on the table is rebuilt by
    :meth:`NiasSample.recompute_factors` on every recalculation, so panel, grid
    and workbook always show the same numbers (spec v2.1 SS V.1/V.2).
    """
    out: list[dict[str, Any]] = []
    for std in standards:
        fid = std.get("fid")
        factor = std.get("factor")
        deviation = (
            (factor / mean_factor - 1.0) * 100.0
            if (factor and mean_factor) else None
        )
        out.append({
            "name": std.get("name", ""),
            "target_rt": std.get("target_rt"),
            "role": std.get("role", ""),
            "conc_attr": std.get("conc_attr"),
            "fid_peak": getattr(fid, "peak", None),
            "fid_rt": getattr(fid, "rt", None),
            "fid_area": getattr(fid, "area", None),
            "concentration": std.get("concentration"),
            "factor": factor,
            "deviation": deviation,
            "status": std.get("status", ""),
            # The engine's own area, the fallback while no grid row carries the
            # standard's FID peak.
            "engine_area": getattr(fid, "area", None),
            # Filled in by NiasSample once the rows exist: the grid row that
            # carries this standard's FID peak, so an edit in the Standards
            # panel and an edit in the grid are the same edit.
            "row_id": None,
            "edited": False,
        })
    return out


def quantification_factor(concentration: Optional[float], area: Optional[float],
                          settings) -> Optional[float]:
    """SS 11.3's factor for one standard, verbatim from AutoLib.

    ``AutoLib.analyse_determination`` computes::

        factor = conc * s.is_amount / 1000 / f.area / s.cell_area_dm2 / s.coverage

    The operand order is reproduced exactly -- floating-point multiplication is
    not associative, and a reordered expression would move the last bits of
    every concentration in the report. The ``/1000`` converts the standard
    amount from the sheet's unit into the area unit and is part of the formula,
    not a rounding convenience.
    """
    if concentration is None or not area:
        return None
    cell_area = getattr(settings, "cell_area_dm2", None)
    coverage = getattr(settings, "coverage", None)
    if not cell_area or not coverage:
        return None
    is_amount = getattr(settings, "is_amount", 0.0)
    return concentration * is_amount / 1000 / area / cell_area / coverage


def istd_mean_factor(standards: list[dict[str, Any]], settings,
                     options: Optional[dict[str, Any]] = None
                     ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """``(mean factor, mean concentration, mean area)`` of one determination.

    Only quantifying ISTDs with both a concentration and an area count. With
    ``use_mean_area`` the factor is formed from the *means*::

        factor = mean(c) * is_amount / 1000 / mean(area) / cell_area / coverage

    which is the Einzelbestimmung workbook's formula. Unticked, the reference
    ISTD's own factor quantifies everything. ``None`` when nothing is usable --
    the determination then simply carries no concentrations.
    """
    options = options or DEFAULT_ISTD_OPTIONS
    usable = [s for s in standards
              if s.get("role") == ROLE_QUANTIFICATION
              and M.num(s.get("concentration")) and M.num(s.get("fid_area"))]
    if not usable:
        return None, None, None
    if not options.get("use_mean_area", True):
        wanted = str(options.get("reference") or "").upper()
        ref = next((s for s in usable
                    if str(s.get("code") or "").upper() == wanted), None)
        if ref is None:
            ref = usable[0]
        conc, area = float(ref["concentration"]), float(ref["fid_area"])
        return quantification_factor(conc, area, settings), conc, area
    conc = statistics.mean(float(s["concentration"]) for s in usable)
    area = statistics.mean(float(s["fid_area"]) for s in usable)
    return quantification_factor(conc, area, settings), conc, area


def derived_istd_concentration_ugl(settings, defs=None,
                                   options=None) -> Optional[float]:
    """The ISTD concentration in the NIAS extract, in ug/L -- or ``None``.

    Spec v3.1 SS VII.10 quantifies the total-extraction column as
    ``c = area_corr / mean(ISTD areas) * c_ISTD``. ``c_ISTD`` used to be
    ``gc_model.IS_CONCENTRATION_UG_L``, carried over from the DIN SPEC path as
    assumption 1 of SS VII.15. The analyst has since stated the missing number:
    **the NIAS total-extraction extract is 10 mL, spiked with the NIAS ISTD**,
    so the concentration is no longer an assumption -- it follows from the
    Parameter sheet.

    The unit chain is the one :func:`quantification_factor` already encodes::

        factor = conc * is_amount / 1000 / area / cell_area_dm2 / coverage

    ``factor`` is mg/dm2 per area count, so ``conc * is_amount / 1000`` is a
    mass in mg, so ``conc * is_amount`` is a mass in **ug** -- which pins
    ``is_amount`` to **uL** given ``conc`` in mg/mL (0.82 mg/mL * 10 uL =
    8.2 ug, and 8.2 ug / 1000 = 0.0082 mg). Diluting that spike into
    ``extract_volume_ml`` millilitres gives::

        c_ISTD [ug/L] = mean(fc17, bbp, dnnp) [mg/mL] * is_amount [uL]
                        * 1000 / extract_volume_ml [mL]

    With the defaults: ``0.823333 * 10 * 1000 / 10 = 823.33 ug/L``.

    ``ov_ratio`` deliberately does **not** appear. O/V only matters for a report
    with an SML, and neither Total Extraction nor Fingerprint has one -- the
    same reason :func:`gc_export._extract_concentration` is free of it.

    Returns ``None`` when any of the five settings is missing or non-numeric, so
    a caller can tell "cannot derive" apart from a derived value and fall back
    to the DIN SPEC constant rather than to a silently wrong number.

    With ``defs`` -- the selectable ISTD list -- the concentrations are the
    typed ones of the quantifying ISTDs instead of the three fixed Parameter
    rows: their mean, or the reference ISTD's alone when ``options`` has the
    mean-area box unticked.
    """
    concentrations: list[float] = []
    if defs is not None:
        options = options or DEFAULT_ISTD_OPTIONS
        usable = [d for d in defs
                  if d.get("quantify", True) and M.num(d.get("concentration"))]
        if not options.get("use_mean_area", True):
            ref = reference_code(usable, options)
            usable = [d for d in usable if d.get("code") == ref]
        if not usable:
            return None
        concentrations = [float(d["concentration"]) for d in usable]
    else:
        for attr in ISTD_CONCENTRATION_ATTRS:
            value = M.num(getattr(settings, attr, None))
            if value is None:
                return None
            concentrations.append(value)
    is_amount = M.num(getattr(settings, "is_amount", None))
    volume = M.num(getattr(settings, "extract_volume_ml", None))
    if not is_amount or not volume:
        return None
    # statistics.mean over the three, for the same reason
    # mean_quantification_factor uses it: it is the mean AutoLib forms, and the
    # three areas the report divides by are meaned the same way.
    return statistics.mean(concentrations) * is_amount * 1000.0 / volume


def mean_quantification_factor(factors: list[float]) -> Optional[float]:
    """The mean over the three quantification factors, or None.

    ``statistics.mean`` rather than ``sum(...)/3``: AutoLib uses it, and the two
    do not always produce the same float.
    """
    return statistics.mean(factors) if len(factors) == 3 else None


# --------------------------------------------------------------------------
# Library hits
# --------------------------------------------------------------------------
#
# The hits reported here are the ones AutoLib itself classified with. The engine
# parses the LIB report (``parse_library_report``) and hangs the top three hits
# on each ``PBMPeak`` before ``display_identification`` runs, so
# ``result["pbm"]`` already carries exactly the list that decided whether a peak
# reads "Accepted", "possible derivative of <class>" or "unknown (m/z
# unavailable)". Re-parsing the file with a second parser could report hits the
# classifier never saw, which is the one thing a PBM sheet must not do.
#
# With ``use_library=False`` no LIB is passed, so each peak keeps the single hit
# RESULTS.CSV carries and hits 2/3 come out blank -- the same rule on different
# input, exactly as spec section 8.1 describes.

def hit_tuples(hits) -> list[tuple[str, str, Optional[int]]]:
    """``(name, CAS, quality)`` for the top hits of one PBM peak, rank 1 first."""
    out: list[tuple[str, str, Optional[int]]] = []
    for h in list(hits or [])[:MAX_PBM_HITS]:
        out.append((
            getattr(h, "name", "") or "",
            M.display_cas(getattr(h, "cas", "")) or "",
            getattr(h, "quality", None),
        ))
    return out


def pbm_hit_table(pbm) -> list[dict[str, Any]]:
    """Every PBM peak with its library hits, in library-report order.

    Kept per determination rather than per row because the PBM list is longer
    than the assigned one: a library hit the FID-tuned integrator merged away
    has no FID peak to hang on, and dropping it would make the sheet an
    incomplete copy of the library report.
    """
    out: list[dict[str, Any]] = []
    for p in pbm or []:
        out.append({
            "peak": getattr(p, "peak", None),
            "rt": getattr(p, "rt", None),
            "area_pct": getattr(p, "area_pct", None),
            "hits": hit_tuples(getattr(p, "hits", [])),
        })
    return out


# --------------------------------------------------------------------------
# Determination -> rows
# --------------------------------------------------------------------------

def blank_reference_rows(value) -> list[Any]:
    """The assigned rows of a blank reference, whatever shape it arrives in.

    ``AutoLib.load_blank_reference`` returns ``(assigned_rows, delay)``, and
    :func:`load_blank` hands that pair straight on, so a caller may pass either
    the pair or the rows. Both are accepted here rather than guessed at the four
    places that need them.
    """
    if not value:
        return []
    if (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[0], list)
            and isinstance(value[1], (int, float, type(None)))):
        return list(value[0])
    return list(value)


def load_blank(path: Optional[Path], settings):
    """Blank reference rows, or None when no blank is configured."""
    if not path:
        return None
    path = Path(path)
    target = path / "RESULTS.CSV" if path.is_dir() else path
    if not target.is_file():
        raise FileNotFoundError(f'Blankreferenz fehlt: {target}')
    return engine().load_blank_reference(str(target), settings)


def analyse_determination(d_dir: Path, settings, *,
                          blank_rows=None, blank_istd_rows=None,
                          use_library: bool = True) -> dict[str, Any]:
    """Run one determination through AutoLib.

    ``use_library`` controls whether the fixed-width ``LIB`` report is supplied
    alongside ``RESULTS.CSV``. It matters more than it looks:

    * **With** the LIB (default) each peak carries up to three hits, so
      ``common_class`` can fire and a weak hit becomes
      "possible derivative of <class>" rather than "unknown (m/z unavailable)".
    * **Without** it only the single PBM hit from RESULTS.CSV is available, class
      inference never triggers, and every sub-threshold peak reads as unknown.

    The reference workbook was produced without the LIB, which is why its
    Bestimmung sheets show "unknown (m/z unavailable)" where the workspace shows
    an inferred class. Both are the same rule on different input.
    """
    d_dir = Path(d_dir)
    results = d_dir / "RESULTS.CSV"
    lib = d_dir / "LIB"
    if not results.is_file():
        raise FileNotFoundError(f"{d_dir.name}: RESULTS.CSV fehlt")
    lib_path = str(lib) if (use_library and lib.is_file()) else ""
    # Normalise before the engine sees them. ``load_blank`` hands on
    # ``AutoLib.load_blank_reference``'s ``(assigned_rows, delay)`` pair
    # verbatim, and ``subtract_blank_signals`` would then iterate the pair
    # itself -- ``for f, p, _, _ in [rows, delay]`` -- and raise. Every run with
    # a configured blank crashed; the reference sample has none, which is why it
    # stayed hidden. Empty list and None are equivalent to the engine
    # (``list(blank_rows or [])``), so this is safe for the no-blank path.
    #
    # ``require_standards=False``: a determination without the NIAS standards
    # still loads. Its ISTDs are then chosen in the workspace, and until one
    # with a concentration is set the rows simply carry no concentration.
    return engine().analyse_determination(
        str(results), lib_path, settings,
        blank_reference_rows(blank_rows), blank_reference_rows(blank_istd_rows),
        require_standards=False)


# --------------------------------------------------------------------------
# FID integration bounds (spec v3.0 SS VI.5 / SS VI.13)
# --------------------------------------------------------------------------
#
# ``analyse_determination`` returns peak dicts, and those carry the FID area but
# neither the integration limits nor ``PK TY`` -- although ``parse_results``
# reads all three into ``FIDPeak``. The engine is not ours to change, so the FID
# table is read a second time here and joined onto the rows by peak number. The
# second parse costs about a millisecond and touches nothing the engine decided.

#: Peak types that mark a shared, valley-drawn baseline. ChemStation writes the
#: two ends of a chain as ``BV``/``VB`` and everything between them as ``VV``;
#: ``PV``/``PB`` are the same thing with a penetrated (tangent) start.
_VALLEY_PK_TYPES = ("VV", "VB", "BV", "PV", "PB", "VP")

#: Two bounds count as the same boundary within this many minutes. The CSV
#: reports three decimals, so an exact float comparison would miss chains that
#: ChemStation itself drew as one.
_BOUND_EPS = 5e-4


def fid_peaks_by_number(d_dir: Path) -> dict[int, Any]:
    """``{peak number: AutoLib.FIDPeak}`` for one determination.

    The FID peak number is the join key both sides already use: every row keeps
    it as ``derived["fid_peak"]``, and ``find_standard`` returns the same object.
    """
    results = Path(d_dir) / "RESULTS.CSV" if Path(d_dir).is_dir() else Path(d_dir)
    _tic, fid, _pbm = engine().parse_results(str(results))
    return {f.peak: f for f in fid}


def _in_valley_chain(fid_peak, previous, following) -> bool:
    """Whether this FID peak shares a drawn baseline with a neighbour.

    ``end == next.start`` with a valley peak type is exactly the chain SS VI.5
    measures at 20 % CV against 92 % for a per-peak endpoint baseline, so it is
    the model the peak is loaded with.
    """
    pk_ty = (getattr(fid_peak, "pk_type", "") or "").strip().upper()
    if not any(pk_ty.startswith(t) for t in _VALLEY_PK_TYPES):
        return False
    if previous is not None and abs(previous.end - fid_peak.start) <= _BOUND_EPS:
        return True
    return (following is not None
            and abs(fid_peak.end - following.start) <= _BOUND_EPS)


def apply_fid_bounds(rows: list[M.PeakRow], by_number: dict[int, Any]) -> int:
    """Carry ``Start``/``End``/``PK TY``/``Height`` from the FID table onto the rows.

    Returns how many rows were filled. Called before ``snapshot()``, so the
    instrument's bounds are what a reset and the "edited" colour compare
    against.

    ``Height`` is carried for the same reason as the bounds: without it
    ``gc_qc.signal_to_noise`` has no height to work with, so the ``S/N`` column
    stayed empty for exactly the untouched instrument peaks it is most needed
    on, and only appeared once a row had been re-integrated. The FID list's
    height is the right one here -- on this path a row *is* an FID peak.
    ``NiasSample.recalculate`` does not derive Area%/Height%, so filling it
    cannot produce the meaningless percentages the DIN SPEC path would.
    """
    ordered = sorted(by_number.values(), key=lambda f: f.peak)
    position = {f.peak: i for i, f in enumerate(ordered)}
    filled = 0
    for row in rows:
        peak = row.derived.get("fid_peak")
        fid_peak = by_number.get(peak)
        if fid_peak is None:
            continue
        index = position[peak]
        previous = ordered[index - 1] if index > 0 else None
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        row.fid_start = float(fid_peak.start)
        row.fid_end = float(fid_peak.end)
        row.fid_pk_ty = (getattr(fid_peak, "pk_type", "") or "").strip()
        height = getattr(fid_peak, "height", None)
        if height is not None:
            row.height = float(height)
        row.fid_baseline = (M.BASELINE_CLUSTER
                            if _in_valley_chain(fid_peak, previous, following)
                            else M.BASELINE_ENDPOINT)
        row.integration_origin = M.ORIGIN_CHEMSTATION
        filled += 1
    return filled


def calibrate_sample(sample: M.Sample, trace=None) -> int:
    """Store each row's per-peak area scale (SS VI.5). Returns rows calibrated.

    ``scale = area_reported / area_raw(reported bounds)``, so an untouched peak
    reproduces its ChemStation area and the baseline-model error cancels for the
    peak it was measured on. The reported area is the **raw** FID area, before
    blank subtraction: that is the number ChemStation integrated.

    The work is ``gc_integrate.calibrate_sample``'s, not repeated here. That
    matters for more than tidiness: the contract's ``calibrate()`` returns a
    bare float, and reconstructing an area as ``(a / r) * r`` lands one ulp away
    from ``a`` for about 8 % of float64 pairs -- on this sample, for 14 of the
    139 FID peaks. ``gc_integrate.calibrate_sample`` stores the reference pair
    alongside the scale, so an unmoved bound hands back the reported number
    verbatim instead of recomputing it. Going through it makes SS VI.5's first
    consequence structural rather than a property that happens to hold.

    Everything here is best-effort. ``gc_ch``/``gc_integrate`` may be absent,
    the ``.D`` may hold no ``FID1A.ch``, and a peak may sit in the saturated
    solvent region where no baseline model means anything. None of that may stop
    a determination from loading -- a row without a scale simply falls back to
    ``gc_integrate.AREA_SCALE`` if it is ever re-integrated.
    """
    try:
        import gc_integrate
    except ImportError:
        return 0
    try:
        return int(gc_integrate.calibrate_sample(sample, trace))
    except Exception:
        # A chromatogram that cannot be read is a missing convenience, not a
        # broken determination: every number in the report comes from
        # RESULTS.CSV either way.
        return 0


#: Review note on an FID peak AutoLib could not assign to any PBM peak.
NO_MS_MATCH_REVIEW = "keine MS-Zuordnung; Prüfung"


def _unmatched_fid_rows(d_dir: Path, result: dict[str, Any], settings,
                        rows: list[M.PeakRow], ms, int_tic, ids) -> list[M.PeakRow]:
    """Rows for the FID peaks ``analyse_determination`` left out.

    AutoLib's ``assign`` marks an FID peak "No MS match" when no PBM peak lies
    inside its integration limits *and* within ``rt_tolerance`` of its apex, and
    ``analyse_determination`` then skips it (``if not p: continue``). The FID
    integrated it all the same, so it disappeared from the grid and the report
    without a trace -- 6 to 17 peaks per determination on the reference batch.
    The engine is not ours to change; the peaks are brought back here as
    unknowns, with the engine's own blank correction, and flagged for review.
    """
    try:
        _tic, fid, _pbm = engine().parse_results(str(Path(d_dir) / "RESULTS.CSV"))
    except (OSError, ValueError):
        return []
    solvent_end = getattr(settings, "solvent_end", None)
    if solvent_end is None:
        solvent_end = 0.0
    present = {r.derived.get("fid_peak") for r in rows}
    audit = result.get("blank_audit") or {}
    delay = result.get("delay") or 0.0
    mean_factor = result.get("mean_factor")
    ov_ratio = getattr(settings, "ov_ratio", None)

    out: list[M.PeakRow] = []
    for f in fid:
        if f.rt <= solvent_end or f.peak in present:
            continue
        corr = audit.get(f.peak)
        area = corr.corrected_area if corr is not None else f.area
        row = M.PeakRow(
            row_id=next(ids),
            peak_no=0,
            source="FID+PBM",
            rt=float(f.rt),
            area=area,
            name=M.UNKNOWN_DISPLAY_NAME,
            cas="0",
            si=None,
        )
        row.id_status = M.ID_UNKNOWN
        if ms is not None:
            apex, bg, rule = ex.locate_bounds(ms, float(f.rt) - delay, int_tic)
            row.apex_scan, row.bg_scan, row.bounds_rule = apex, bg, rule
        mg_dm2 = area * mean_factor if (mean_factor and area is not None) else None
        row.derived = {
            "pbm_hits": [],
            "mg_dm2": mg_dm2,
            "mg_kg": (mg_dm2 * ov_ratio
                      if (mg_dm2 is not None and ov_ratio) else None),
            "raw_area": corr.raw_area if corr is not None else f.area,
            "blank_area": corr.blank_area if corr is not None else 0.0,
            "blank_istd_area": corr.blank_istd_area if corr is not None else 0.0,
            "subtracted_area": corr.subtracted_area if corr is not None else 0.0,
            "blank_status": (corr.status if corr is not None
                             else "kein passendes Blanksignal"),
            "protected_standard": ("Ja" if corr is not None and corr.protected_standard
                                   else "Nein"),
            "id_status": M.ID_UNKNOWN,
            "match_status": "No MS match",
            "review": NO_MS_MATCH_REVIEW,
            "fid_peak": f.peak,
            "pbm_peak": None,
            "ms_rt": None,
            "corrected_ms_rt": None,
            "secondary": [],
        }
        out.append(row)
    return out


def load_determination(d_dir: Path, settings, label: str = "", ids=None, *,
                       blank_rows=None, blank_istd_rows=None,
                       use_library: bool = True,
                       istd_defs: Optional[list[dict[str, Any]]] = None,
                       istd_options: Optional[dict[str, Any]] = None
                       ) -> M.Sample:
    """Read one determination into a :class:`gc_model.Sample`.

    Rows are **FID peaks with their assigned PBM identification**: the FID area
    is what gets quantified, while the PBM peak behind the row supplies the
    spectrum shown in the detail panels.

    ``istd_defs``/``istd_options`` are the shared, selectable ISTD list and how
    it quantifies (v3.2). The workspace passes the *same* objects for both
    determinations, so an edit reaches both. Without them the four NIAS
    standards are the list, with the Parameter concentrations.
    """
    d_dir = Path(d_dir)
    result = analyse_determination(d_dir, settings,
                                   blank_rows=blank_rows,
                                   blank_istd_rows=blank_istd_rows,
                                   use_library=use_library)

    ms = None
    data_ms = d_dir / "data.ms"
    if data_ms.is_file():
        ms = ex.DataMS(data_ms)
    int_tic = []
    if ms is not None:
        try:
            import gc_load
            int_tic = [(t["rt"], t["first"], t["max"])
                       for t in gc_load.parse_int_tic_full(d_dir / "RESULTS.CSV")]
        except Exception:
            int_tic = []

    pbm_hits = pbm_hit_table(result.get("pbm"))
    hits_by_peak = {e["peak"]: e["hits"] for e in pbm_hits if e["peak"] is not None}

    ids = ids if ids is not None else M.allocate_ids()
    rows: list[M.PeakRow] = []
    for n, peak in enumerate(result["peaks"], 1):
        row = M.PeakRow(
            row_id=next(ids),
            peak_no=n,
            source="FID+PBM",
            rt=float(peak["rt"]),
            area=peak.get("area"),
            name=peak.get("name", ""),
            cas=peak.get("cas") or "0",
            si=peak.get("quality"),
        )
        # Spectrum linkage: the identification comes from the MS side, so the
        # panels follow the PBM peak's retention time, not the FID one.
        if ms is not None:
            ms_rt = peak.get("ms_rt")
            apex, bg, rule = ex.locate_bounds(
                ms, float(ms_rt if ms_rt is not None else peak["rt"]), int_tic)
            row.apex_scan, row.bg_scan, row.bounds_rule = apex, bg, rule

        # The full hit list travels with the row (and so with its row_id), not
        # with its position: sorting and re-merging reorder the list.
        hits = hits_by_peak.get(peak.get("pbm_peak")) or []
        row.alt_hits = [tuple(h) for h in hits[1:]]

        row.derived = {
            "pbm_hits": [tuple(h) for h in hits],
            "mg_dm2": peak.get("mg_dm2"),
            "mg_kg": peak.get("mg_kg"),
            "raw_area": peak.get("raw_area"),
            "blank_area": peak.get("blank_area"),
            "blank_istd_area": peak.get("blank_istd_area"),
            "subtracted_area": peak.get("subtracted_area"),
            "blank_status": peak.get("blank_status", ""),
            "protected_standard": "Ja" if peak.get("protected_standard") else "Nein",
            "id_status": peak.get("id_status", ""),
            "match_status": peak.get("match_status", ""),
            "review": peak.get("review", ""),
            "fid_peak": peak.get("fid_peak"),
            "pbm_peak": peak.get("pbm_peak"),
            "ms_rt": peak.get("ms_rt"),
            "corrected_ms_rt": peak.get("corrected_ms_rt"),
            "secondary": peak.get("secondary"),
        }
        rows.append(row)

    unmatched = _unmatched_fid_rows(d_dir, result, settings, rows, ms, int_tic, ids)
    if unmatched:
        rows.extend(unmatched)
        rows.sort(key=lambda r: (r.rt if r.rt is not None else 0.0, r.row_id))
        for number, row in enumerate(rows, 1):
            row.peak_no = number

    # Bounds and PK TY before the snapshot: they are the instrument's values, so
    # they are what "edited" and "Zelle zurücksetzen" compare against (SS VI.13).
    try:
        apply_fid_bounds(rows, fid_peaks_by_number(d_dir))
    except (OSError, ValueError):
        pass                       # a row without bounds is read-only, not fatal
    for row in rows:
        row.snapshot()

    meta = {
        "sample": d_dir.name[:-2] if d_dir.name.lower().endswith(".d") else d_dir.name,
        "delay": result.get("delay"),
        "mean_factor": result.get("mean_factor"),
        "fid_count": result.get("fid_count"),
        "pbm_count": result.get("pbm_count"),
        "blank_corrected": result.get("blank_corrected"),
        "unmatched_fid": len(unmatched),
        "use_library": use_library,
        "standards": [],
        # What the engine found, kept apart from the bindings: it is where a
        # NIAS standard's peak comes from the first time it is linked.
        "engine_standards": standards_table(result.get("standards") or [],
                                            result.get("mean_factor")),
        "pbm_hits": pbm_hits,
    }

    sample = NiasSample(label or d_dir.name, d_dir, rows, ms, meta)
    sample.settings = settings
    sample.quality_limit = getattr(settings, "quality_limit",
                                   M.DEFAULT_QUALITY_LIMIT)
    sample.mean_factor = result.get("mean_factor")
    sample.istd_defs = (istd_defs if istd_defs is not None
                        else default_istd_defs(settings))
    sample.istd_options = (istd_options if istd_options is not None
                           else dict(DEFAULT_ISTD_OPTIONS))
    # The row-id source travels with the sample: a peak added or split later
    # allocates from it, and ids are never reused (SS 4).
    sample.ids = ids
    # The blank references stay on the sample so a re-integrated peak can be
    # re-matched against them without reloading the blank (SS VI.7 step 6).
    sample.blank_rows = blank_reference_rows(blank_rows)
    sample.blank_istd_rows = blank_reference_rows(blank_istd_rows)
    # Bind the standards to their grid rows before the first requantification,
    # so the factor chain runs off the same areas the analyst sees and edits.
    sample.link_standards()
    calibrate_sample(sample)
    sample.recalculate()
    return sample


class NiasSample(M.Sample):
    """A determination. Concentrations come from the FID area and the mean factor.

    Overrides the DIN SPEC recalculation entirely: there is no single internal
    standard row here, and Area%/Height% are meaningless on the FID list.

    Since v2.1 the quantification chain of SS 11.3 is evaluated here on every
    recalculation instead of being frozen by ``analyse_determination``: the
    standards' effective areas produce ``factor_i``, those produce
    ``mean_factor``, and only then is every row requantified. AutoLib stays the
    engine of record for everything it decides -- peak assignment, blank
    correction, identification, standard detection; what is re-evaluated here
    is the arithmetic v2.0 already documents, so that an edited area actually
    moves the numbers it should move.
    """

    def __init__(self, *args, **kwargs):
        self.settings = None
        self.mean_factor: Optional[float] = None
        #: Live copy of ``meta["standards"]``, each entry linked to its grid row.
        self.standards: list[dict[str, Any]] = []
        #: Row-id source; ``None`` until :func:`load_determination` supplies one.
        self.ids = None
        #: Blank references, kept for re-matching a re-integrated peak.
        self.blank_rows: list[Any] = []
        self.blank_istd_rows: list[Any] = []
        #: The shared, selectable ISTD definitions and how they quantify
        #: (v3.2). ``None`` keeps the engine's four standards as they were.
        self.istd_defs: Optional[list[dict[str, Any]]] = None
        self.istd_options: Optional[dict[str, Any]] = None
        super().__init__(*args, **kwargs)

    # -- integration state -------------------------------------------------

    @property
    def integration_touched(self) -> bool:
        """True once any row's integration was changed by hand.

        The gate on everything this class does differently since v3.0. While it
        is False the determination behaves exactly as it did before the
        workspace gained a chromatogram, which is the one rule that outranks the
        rest (SS VI.18).
        """
        return any(r.integration_touched for r in self.rows)

    # -- standards ---------------------------------------------------------

    def link_standards(self) -> None:
        """Bind each ISTD to the grid row carrying its FID peak.

        With ``istd_defs`` set (every determination the workspace loads) the
        bindings follow that shared list: one entry per definition, whatever
        its name. Without it -- a hand-built sample in a test, an old caller --
        the engine's four standards are linked exactly as before v3.2.
        """
        if getattr(self, "istd_defs", None) is None:
            self._link_engine_standards()
        else:
            self._link_istds()

    def _link_engine_standards(self) -> None:
        """The pre-v3.2 link: the engine's standards, by FID peak number.

        The link is by FID peak number, which is what both sides are keyed on:
        ``find_standard`` returns the (blank-corrected) ``FIDPeak``, and every
        row keeps its ``fid_peak``. A standard whose FID peak has no assigned
        PBM peak has no row -- AutoLib drops those from ``peaks`` -- and then
        falls back to the engine's own area, which cannot be edited.
        """
        self.standards = list(self.meta.get("standards") or [])
        by_fid = {r.derived.get("fid_peak"): r for r in self.rows
                  if r.derived.get("fid_peak") is not None}
        conc_attrs = {name: attr for name, _rt, _role, attr in standard_definitions()}
        for std in self.standards:
            row = by_fid.get(std.get("fid_peak"))
            if row is None:
                # The standard's peak was split, so its FID number now reads
                # "74a"/"74b" and the exact join misses. SS VI.7 step 7:
                # protection follows the standard's own retention time, so the
                # fragment whose bounds contain it takes the standard over.
                row = self._standard_fragment(std)
            std["row_id"] = row.row_id if row is not None else None
            if not std.get("conc_attr"):
                std["conc_attr"] = conc_attrs.get(std.get("name"))
        self._apply_fragment_protection()
        self.meta["standards"] = self.standards

    @staticmethod
    def _new_binding(code: str) -> dict[str, Any]:
        return {"code": code, "fid_peak": None, "fid_rt": None,
                "fid_area": None, "engine_area": None, "manual_area": None,
                "bound_row": None, "factor": None, "deviation": None,
                "status": "", "row_id": None, "edited": False}

    def _link_istds(self) -> None:
        """One binding per shared ISTD definition (v3.2).

        An existing binding keeps its peak -- by FID peak number, then the
        split fragment holding its RT, then the row it was set on. A
        definition that was never bound here is looked for: the engine's own
        find (by library name and target RT) for the four NIAS standards,
        :meth:`match_istd` for everything else. One that the analyst unbound
        (``detached``) is left alone.
        """
        old = {str(s.get("code") or "").upper(): s for s in self.standards
               if s.get("code")}
        engine_found = {s.get("name"): s
                        for s in (self.meta.get("engine_standards") or [])
                        if s.get("fid_peak") is not None}
        by_fid = {r.derived.get("fid_peak"): r for r in self.rows
                  if r.derived.get("fid_peak") is not None}
        claimed: set[int] = set()
        bindings: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for d in self.istd_defs:
            std = old.get(str(d["code"]).upper())
            if std is None:
                std = self._new_binding(d["code"])
                eng = engine_found.get(d.get("name"))
                if eng is not None:
                    std.update(fid_peak=eng.get("fid_peak"),
                               fid_rt=eng.get("fid_rt"),
                               engine_area=eng.get("engine_area",
                                                   eng.get("fid_area")))
            std.update(code=d["code"], name=d.get("name", ""),
                       target_rt=d.get("target_rt"), role=istd_role(d),
                       concentration=d.get("concentration"), conc_attr=None)
            bindings.append((d, std))

        # Existing bindings first, so a match below cannot take their peak.
        for _d, std in bindings:
            row = None
            if std.get("fid_peak") is not None:
                row = by_fid.get(std["fid_peak"]) or self._standard_fragment(std)
            if row is None and std.get("bound_row") is not None:
                row = self.row(std["bound_row"])
            if row is not None and row.row_id in claimed:
                row = None
            std["row_id"] = row.row_id if row is not None else None
            if row is not None:
                claimed.add(row.row_id)
        for d, std in bindings:
            if (std["row_id"] is not None or std.get("fid_peak") is not None
                    or std.get("bound_row") is not None
                    or std.get("manual_area") is not None
                    or std.get("detached")):
                continue
            row = self.match_istd(d, claimed)
            if row is not None:
                self._bind(std, row)
                claimed.add(row.row_id)

        self.standards = [std for _d, std in bindings]
        self._apply_istd_protection()
        self.meta["standards"] = self.standards

    def match_istd(self, definition: dict[str, Any],
                   exclude: Optional[set] = None) -> Optional[M.PeakRow]:
        """The row that most likely is this ISTD, or ``None``.

        By name first (nearest to the target RT when several match), then --
        only with a target RT -- the nearest peak inside
        :data:`ISTD_MATCH_TOLERANCE`, which is ``AutoLib.find_standard``'s rule.
        """
        exclude = exclude or set()
        target = M.num(definition.get("target_rt"))
        free = [r for r in self.rows
                if r.row_id not in exclude and r.rt is not None]
        named = [r for r in free
                 if _name_matches(r.name, definition.get("name", ""))]
        if named:
            if target is None:
                return named[0]
            close = [r for r in named if abs(r.rt - target) <= 1.0]
            if close:
                return min(close, key=lambda r: abs(r.rt - target))
        if target is None:
            return None
        near = [r for r in free if abs(r.rt - target) <= ISTD_MATCH_TOLERANCE]
        return min(near, key=lambda r: abs(r.rt - target)) if near else None

    @staticmethod
    def _bind(std: dict[str, Any], row: M.PeakRow) -> None:
        std["fid_peak"] = row.derived.get("fid_peak")
        std["fid_rt"] = row.rt
        std["bound_row"] = row.row_id
        std["row_id"] = row.row_id
        std["manual_area"] = None
        std["engine_area"] = None
        std.pop("detached", None)

    def binding(self, code: str) -> Optional[dict[str, Any]]:
        """This determination's binding of ISTD ``code``, or ``None``."""
        wanted = str(code or "").upper()
        return next((s for s in self.standards
                     if str(s.get("code") or "").upper() == wanted), None)

    def set_istd_row(self, code: str, row: Optional[M.PeakRow]) -> None:
        """Bind ISTD ``code`` to ``row`` in this determination (right click).

        ``row=None`` unbinds it here and it is not looked for again; the
        definition itself stays in the shared list. A row carries at most one
        ISTD, so it is taken away from any other.
        """
        self.link_standards()
        std = self.binding(code)
        if std is None:
            raise KeyError(code)
        if row is not None:
            for other in self.standards:
                if other is not std and other.get("row_id") == row.row_id:
                    other.update(self._new_binding(other["code"]),
                                 detached=True)
            self._bind(std, row)
        else:
            std.update(fid_peak=None, fid_rt=None, bound_row=None, row_id=None,
                       engine_area=None, detached=True)
        self._apply_istd_protection()
        self.recalculate()

    def set_manual_istd_area(self, code: str, area: Optional[float]) -> None:
        """An area typed for an ISTD that has no grid row in this determination."""
        std = self.binding(code)
        if std is None:
            raise KeyError(code)
        if area is not None and area <= 0:
            raise ValueError("Die ISTD-Fläche muss größer als 0 sein.")
        std["manual_area"] = area
        if area is not None:
            std["detached"] = True
        self.recalculate()

    def istd_bindings(self) -> dict[str, dict[str, Any]]:
        """What a session file needs to rebuild the bindings."""
        out = {}
        for std in self.standards:
            if not std.get("code"):
                continue
            out[std["code"]] = {
                "fid_peak": std.get("fid_peak"), "fid_rt": std.get("fid_rt"),
                "bound_row": std.get("bound_row"),
                "manual_area": std.get("manual_area"),
                "detached": bool(std.get("detached"))}
        return out

    def restore_istd_bindings(self, payload: Any) -> None:
        """The reverse of :meth:`istd_bindings`; unknown codes are ignored."""
        if not isinstance(payload, dict) or getattr(self, "istd_defs", None) is None:
            return
        self.standards = [s for s in self.standards
                          if str(s.get("code") or "") not in payload]
        for code, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            std = self._new_binding(str(code))
            std.update(fid_peak=entry.get("fid_peak"),
                       fid_rt=M.num(entry.get("fid_rt")),
                       bound_row=entry.get("bound_row"),
                       manual_area=M.num(entry.get("manual_area")))
            if entry.get("detached"):
                std["detached"] = True
            self.standards.append(std)
        self.link_standards()

    def _apply_istd_protection(self) -> None:
        """ISTD rows are never blank-subtracted (SS 11.4), whichever they are.

        The engine only protects the four NIAS standards. A peak the analyst
        makes an ISTD gets the same treatment, and a peak that stops being one
        gets back what it was loaded with -- ``protected_loaded`` keeps that.
        An engine standard that is no longer in the list is unprotected.
        """
        bound = {s.get("row_id") for s in self.standards
                 if s.get("row_id") is not None}
        engine_peaks = {s.get("fid_peak")
                        for s in (self.meta.get("engine_standards") or [])}
        for row in self.rows:
            if not hasattr(row, "protected_loaded"):
                row.protected_loaded = row.derived.get("protected_standard",
                                                       "Nein")
            peak = row.derived.get("fid_peak")
            if row.row_id in bound:
                wanted = "Ja"
            elif peak is not None and fid_peak_base(peak) != peak:
                wanted = "Nein"              # a fragment of a split standard
            elif row.protected_loaded == "Ja" and peak in engine_peaks:
                wanted = "Nein"              # engine standard nobody lists now
            else:
                wanted = row.protected_loaded
            row.derived["protected_standard"] = wanted

    @staticmethod
    def _protection_changed(row: M.PeakRow) -> bool:
        """The row's ISTD protection differs from what it was loaded with."""
        loaded = getattr(row, "protected_loaded", None)
        return (loaded is not None
                and loaded != row.derived.get("protected_standard", "Nein"))

    def _standard_fragment(self, std: dict[str, Any]) -> Optional[M.PeakRow]:
        """The fragment of a split standard peak that carries the standard.

        The standard's retention time decides, not the fragment's size or its
        identification: a split that leaves the standard's apex in the right-hand
        fragment moves the standard there, and with it the area the whole
        determination is quantified against.
        """
        base = std.get("fid_peak")
        if base is None:
            return None
        fragments = [r for r in self.rows
                     if fid_peak_base(r.derived.get("fid_peak")) == base]
        if not fragments:
            return None
        target = std.get("fid_rt")
        if target is None:
            return fragments[0]
        inside = [r for r in fragments
                  if r.fid_bounds is not None and r.fid_start <= target <= r.fid_end]
        if inside:
            return min(inside, key=lambda r: abs(r.rt - target))
        return min(fragments, key=lambda r: abs(r.rt - target))

    def _apply_fragment_protection(self) -> None:
        """ISTD protection follows the standard, not the peak number.

        Only fragments are touched: an untouched determination keeps exactly the
        ``protected_standard`` values ``AutoLib.subtract_blank_signals``
        produced, so nothing moves while nobody splits anything.
        """
        protected = {std.get("row_id") for std in self.standards
                     if std.get("row_id") is not None}
        for row in self.rows:
            peak = row.derived.get("fid_peak")
            if peak is None or fid_peak_base(peak) == peak:
                continue                       # not a fragment: leave it alone
            row.derived["protected_standard"] = (
                "Ja" if row.row_id in protected else "Nein")

    def standard_area(self, std: dict[str, Any]) -> Optional[float]:
        """The standard's **effective** area: the manual value if one was
        entered, otherwise the blank-corrected value AutoLib produced.

        Both come from the same place -- the grid row's ``area``, which the
        override machinery keeps at the analyst's number once it was edited --
        so panel and grid cannot disagree by construction.
        """
        row = self.row(std["row_id"]) if std.get("row_id") is not None else None
        if row is not None:
            return row.area
        if "manual_area" in std:
            # A selectable ISTD without a row: the typed area, else the
            # engine's own, else nothing -- never a stale ``fid_area`` from a
            # peak it is no longer bound to.
            if std.get("manual_area") is not None:
                return std["manual_area"]
            return std.get("engine_area")
        return std.get("fid_area")

    def standard_for_row(self, row_id: int) -> Optional[dict[str, Any]]:
        """The standard carried by this grid row, or None."""
        for std in self.standards:
            if std.get("row_id") == row_id:
                return std
        return None

    def recompute_factors(self) -> None:
        """SS 11.3, step 1 and 2: effective areas -> factor_i -> mean_factor.

        Runs before every requantification, so a changed standard area, a
        changed cell area, coverage, ISTD amount or standard concentration all
        reach the concentrations in the same pass.
        """
        # Re-linking is only needed once a peak was created, split or removed:
        # the standard's FID number then no longer matches its row. Gated on
        # exactly that, so an untouched determination runs the identical path it
        # ran before v3.0. The selectable ISTD list (v3.2) always re-links: a
        # definition may have been added, renamed or removed in the panel.
        selectable = getattr(self, "istd_defs", None) is not None
        if selectable or not self.standards or self.integration_touched:
            self.link_standards()
        settings = getattr(self, "settings", None)
        factors: list[float] = []
        for std in self.standards:
            area = self.standard_area(std)
            std["fid_area"] = area
            if std.get("conc_attr") and settings is not None:
                std["concentration"] = getattr(settings, std["conc_attr"],
                                               std.get("concentration"))
            row = self.row(std["row_id"]) if std.get("row_id") is not None else None
            std["edited"] = bool(row is not None
                                 and row.edited & {"area", "raw_area", "blank_area"})

            if std.get("role") == ROLE_QUANTIFICATION:
                factor = quantification_factor(std.get("concentration"), area,
                                               settings)
                std["factor"] = factor
                if factor is not None:
                    factors.append(factor)
            else:
                # The QC standard is reported but never quantifies (SS 11.2).
                std["factor"] = None

            if selectable:
                std["status"] = self._istd_status(std, area)
            elif std.get("status") != STATUS_NOT_FOUND:
                std["status"] = self._standard_status(std, area)

        if selectable:
            options = getattr(self, "istd_options", None) or DEFAULT_ISTD_OPTIONS
            mean, mean_conc, mean_area = istd_mean_factor(
                self.standards, settings, options)
            self.meta["istd_mean_conc"] = mean_conc
            self.meta["istd_mean_area"] = mean_area
            if not options.get("use_mean_area", True):
                ref = reference_code(
                    [{"code": s.get("code"), "quantify": True}
                     for s in self.standards
                     if s.get("role") == ROLE_QUANTIFICATION
                     and M.num(s.get("concentration"))
                     and M.num(s.get("fid_area"))], options)
                for std in self.standards:
                    if std.get("code") == ref and std["status"] == STATUS_FOUND:
                        std["status"] = STATUS_REFERENCE
        else:
            mean = mean_quantification_factor(factors)
        for std in self.standards:
            factor = std.get("factor")
            std["deviation"] = ((factor / mean - 1.0) * 100.0
                                if (factor and mean) else None)
        self.mean_factor = mean
        self.meta["mean_factor"] = self.mean_factor
        self.meta["standards"] = self.standards

    def _standard_status(self, std: dict[str, Any],
                         area: Optional[float]) -> str:
        """Re-judge ``Below QC minimum`` against the current effective area."""
        if std.get("role") != ROLE_QC:
            return STATUS_FOUND
        minimum = getattr(getattr(self, "settings", None), "qc_min_area", 0.0) or 0.0
        if minimum > 0 and area is not None and area < minimum:
            return STATUS_BELOW_QC
        return STATUS_FOUND

    def _istd_status(self, std: dict[str, Any], area: Optional[float]) -> str:
        """Status of a selectable ISTD; ``Referenz`` is set by the caller."""
        if area is None:
            return STATUS_NOT_FOUND
        if std.get("role") == ROLE_QC:
            return self._standard_status(std, area)
        if not M.num(std.get("concentration")):
            return STATUS_NO_CONCENTRATION
        return STATUS_FOUND

    # -- editing rules -----------------------------------------------------

    def validate_edit(self, row: M.PeakRow, field_name: str, value: Any) -> None:
        """Refuse an edit that would destroy a quantification standard.

        Clearing a quantifying ISTD's area to 0 or blank would divide by zero
        -- an infinite factor and a report full of infinite concentrations --
        so the edit is rejected instead. Removing the ISTD is the way to stop
        using it.
        """
        if field_name not in {"area", "raw_area"}:
            return
        std = self.standard_for_row(row.row_id)
        if std is None or std.get("role") != ROLE_QUANTIFICATION:
            return
        if not value:
            raise ValueError(
                f"Die Fläche des Quantifizierungsstandards „{std['name']}“ darf "
                "nicht 0 oder leer sein. Um ihn nicht mehr zu verwenden, den "
                "ISTD unter Standards entfernen oder „Quantifizieren“ abwählen.")

    def after_edit(self, row: M.PeakRow, field_name: str) -> None:
        """SS V.1's one-directional relationship between the three area columns.

        Editing a component recomputes the corrected area; editing the corrected
        area decouples it from its components, which stay visible as
        documentation of where it came from. A corrected area the analyst set by
        hand is therefore never overwritten by a later component edit either --
        only ``Zelle zurücksetzen`` gives the coupling back.
        """
        if field_name in {"raw_area", "blank_area"}:
            self.apply_area_components(row)

    def apply_area_components(self, row: M.PeakRow) -> bool:
        """``area = raw_area - blank_area`` unless the area is overridden.

        An ISTD-protected row is never blank-subtracted (SS 11.4) and keeps
        ``area = raw_area``. Negative results are clipped to zero, exactly as
        ``AutoLib.subtract_blank_signals`` does.
        """
        if row.is_manual("area") or row.raw_area is None:
            return False
        blank = (0.0 if row.protected_standard else
                 max(row.blank_area or 0.0, row.derived.get("blank_istd_area") or 0.0))
        area = max(0.0, row.raw_area - blank)
        if M._same(area, row.area):
            return False
        row.area = area
        if M._same(area, row.original.get("area")):
            row.edited.discard("area")
        else:
            row.edited.add("area")
        return True

    # -- requantification --------------------------------------------------

    def recalculate(self) -> None:
        # SS V.1 order: effective areas -> factor_i -> mean_factor/deviation_i
        # -> every row's mg/dm2 and mg/kg -> traffic light -> reporting limit.
        if getattr(self, "istd_defs", None) is not None:
            # First, so a peak that just became (or stopped being) an ISTD is
            # blank-subtracted accordingly in the loop below.
            self.link_standards()
        for row in self.rows:
            if row.is_manual("area"):
                continue                      # sticky: never written over
            if (row.is_manual("raw_area") or row.is_manual("blank_area")
                    or row.integration_touched
                    or self._protection_changed(row)):
                # A re-integrated row's ``raw_area`` is *edited but not manual*
                # (SS V.1 / SS VI.13): the drag recomputed it, the analyst did
                # not type it. Without this branch the ``elif`` below would
                # restore the loaded area and the next drag would have nothing
                # left to move -- the feature would deadlock after one edit.
                self.apply_area_components(row)
            elif row.is_edited("area"):
                # Both components are back at their loaded values, so the
                # corrected area is too: this is the tail of a reset on
                # raw_area or blank_area, and re-deriving would use
                # ``raw - blank`` where AutoLib used
                # ``raw - max(blank, blank+ISTD)``. Restoring the snapshot
                # gives the engine's value back exactly.
                row.reset_field("area")
        self.recompute_factors()

        settings = getattr(self, "settings", None)
        factor = getattr(self, "mean_factor", None)
        ov_ratio = getattr(settings, "ov_ratio", 1.0) if settings else 1.0
        limit = getattr(settings, "reporting_limit", 0.01) if settings else 0.01

        for row in self.rows:
            # AutoLib already applied quality_limit and wrote the status; taking
            # it verbatim keeps grid, workbook and register in agreement.
            engine_status = row.derived.get("id_status")
            if engine_status:
                row.id_status = engine_status
            else:
                row.reclassify(self.quality_limit)
            mg_dm2 = row.area * factor if (row.area is not None and factor) else None
            mg_kg = mg_dm2 * ov_ratio if mg_dm2 is not None else None
            info = self.cas_lookup.get(M.display_cas(row.cas)) or {}
            sml = M.sml_limit(info.get("sml"))
            text, fill, font = M.report_status(row.is_unknown, sml, mg_kg)

            row.derived.update({
                "mg_dm2": mg_dm2,
                "mg_kg": mg_kg,
                "sml": sml,
                "reference": info.get("reference", ""),
                "status": text,
                "status_fill": fill,
                "status_font": font,
                "below_limit": mg_kg is not None and mg_kg < limit,
            })

    def apply_settings(self, settings) -> None:
        self.settings = settings
        self.quality_limit = getattr(settings, "quality_limit",
                                     M.DEFAULT_QUALITY_LIMIT)
        self.recalculate()


# --------------------------------------------------------------------------
# Manual integration: the four operations the command objects call (SS VI.7/8)
# --------------------------------------------------------------------------
#
# The commands in ``gc_integrate`` own undo, the history and the arithmetic on
# the trace. What they must not own is identification, blank correction, peak
# numbering and the standards -- those are this module's, and reimplementing
# them there would give the workspace a second opinion on every one of them.

#: Suffixes a split fragment's FID peak number may take: 74 -> 74a, 74b, ...
#: The number stays readable and the trace back into ``RESULTS.CSV`` survives
#: every further split (SS VI.7).
_FRAGMENT_SUFFIXES = "abcdefghijklmnopqrstuvwxyz"


def fid_peak_base(value: Any) -> Any:
    """The ``RESULTS.CSV`` peak number behind a possibly split FID peak.

    ``74`` -> ``74``, ``"74a"`` -> ``74``. Everything that has to join a row back
    onto the instrument's own FID table goes through here, so a split never
    orphans a standard, a blank match or a report row.
    """
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    digits = ""
    for ch in text:
        if not ch.isdigit():
            break
        digits += ch
    if digits and digits == text:
        return int(digits)
    return int(digits) if digits else value


def _next_fragment_number(sample: M.Sample, parent_fid_peak: Any) -> Optional[str]:
    """The next free ``74a``/``74b`` label under one ``RESULTS.CSV`` peak."""
    base = fid_peak_base(parent_fid_peak)
    if not isinstance(base, int):
        return None
    taken = {str(r.derived.get("fid_peak")) for r in sample.rows}
    for suffix in _FRAGMENT_SUFFIXES:
        label = f"{base}{suffix}"
        if label not in taken:
            return label
    return None


def _promote_parent_to_fragment(sample: M.Sample, parent: M.PeakRow) -> None:
    """Turn the split parent's ``74`` into ``74a`` before its child becomes ``74b``.

    SS VI.7 step 3: after a split neither side is peak 74 any more, both are
    fragments of it, and the sheet has to say so. The original number is kept in
    ``fid_peak_original`` so an undo can put it back without the command having
    to know that FID numbering is a thing.

    Splitting a fragment again leaves it alone -- ``74b`` stays ``74b`` and the
    new piece takes the next free letter, so every fragment keeps one name for
    its whole life and the audit trail never has to re-explain one.
    """
    current = parent.derived.get("fid_peak")
    if current is None or fid_peak_base(current) != current:
        return                              # already a fragment, or unnumbered
    parent.derived.setdefault("fid_peak_original", current)
    parent.derived["fid_peak"] = f"{current}{_FRAGMENT_SUFFIXES[0]}"


def renumber_rows(sample: M.Sample) -> None:
    """Renumber ``peak_no`` by retention time (SS VI.7 step 3).

    ``row_id`` is untouched -- it is the identity every edit flag, colour and
    register entry is keyed on and is never reused (SS 4). ``peak_no`` is a
    display number and may move.
    """
    sample.rows.sort(key=lambda r: (r.rt if r.rt is not None else 0.0, r.row_id))
    for number, row in enumerate(sample.rows, 1):
        row.peak_no = number


def _allocate_row_id(sample: M.Sample) -> int:
    """Next row id from the sample's own source; never a reused one."""
    ids = getattr(sample, "ids", None)
    if ids is None:
        # A sample that was not built by load_determination still has to be able
        # to grow a row; continue above the highest id it holds.
        start = max((r.row_id for r in sample.rows), default=0) + 1
        ids = M.allocate_ids(start)
        sample.ids = ids
    return next(ids)


def _quality_limit(sample: M.Sample) -> int:
    return int(getattr(sample, "quality_limit", M.DEFAULT_QUALITY_LIMIT))


def _settings_value(sample: M.Sample, key: str, default: float) -> float:
    value = getattr(getattr(sample, "settings", None), key, None)
    return float(value) if value is not None else default


def reassign_row(sample: M.Sample, row: M.PeakRow) -> None:
    """Re-derive one row's identification from the MS side (SS VI.7 step 4).

    The determination's own delay is applied, the PBM peaks whose corrected RT
    falls inside the row's bounds are the candidates, the nearest apex within
    ``rt_tolerance`` wins, and the winner's hit list decides the name through
    ``AutoLib.display_identification`` -- **the single SS 8.1 rule**. There is no
    second threshold and no special case for a split row: a fragment is
    identified exactly as the instrument would have identified a peak with those
    bounds. No candidate at all means ``unknown (m/z unavailable)``.
    """
    bounds = row.fid_bounds
    delay = sample.meta.get("delay") or 0.0
    tolerance = _settings_value(sample, "rt_tolerance", 0.035)
    table = sample.meta.get("pbm_hits") or []

    candidates: list[tuple[float, dict[str, Any]]] = []
    if bounds is not None:
        start, end = bounds
        for entry in table:
            ms_rt = entry.get("rt")
            if ms_rt is None:
                continue
            corrected = ms_rt + delay
            if not (start <= corrected <= end):
                continue
            distance = abs(corrected - row.rt)
            if distance > tolerance:
                continue
            candidates.append((distance, entry))

    if not candidates:
        _apply_no_match(row)
    else:
        # Nearest apex wins; the PBM peak number breaks a tie, so the outcome
        # does not depend on dictionary order.
        candidates.sort(key=lambda c: (c[0], c[1].get("peak") or 0))
        _apply_pbm_match(sample, row, candidates[0][1], delay)

    row.reclassify(_quality_limit(sample))
    # AutoLib's own status is authoritative wherever it exists, exactly as at
    # load time (SS 8.1): ``recalculate`` reads it back out of ``derived``, so
    # grid, workbook and register keep saying the same thing.
    engine_status = row.derived.get("id_status")
    if engine_status:
        row.id_status = engine_status


def _apply_no_match(row: M.PeakRow) -> None:
    """A fragment with no PBM peak inside its bounds is an unknown."""
    row.name = M.UNKNOWN_DISPLAY_NAME
    row.cas = "0"
    row.si = None
    row.alt_hits = []
    row.id_status = M.ID_UNKNOWN
    row.derived.update({
        "pbm_hits": [], "pbm_peak": None, "ms_rt": None,
        "corrected_ms_rt": None, "secondary": [],
        "id_status": M.ID_UNKNOWN, "match_status": "No MS match",
    })


def _apply_pbm_match(sample: M.Sample, row: M.PeakRow, entry: dict[str, Any],
                     delay: float) -> None:
    """Write one PBM peak's identification onto the row.

    ``display_identification`` is called on a rebuilt ``PBMPeak`` rather than
    reimplemented: the name, the status and the CAS a re-assigned row carries
    then come out of the same function that produced every other row's, so a
    split cannot introduce a second identification rule.
    """
    mod = engine()
    hits = [tuple(h) for h in (entry.get("hits") or [])]
    pbm = mod.PBMPeak(
        entry.get("peak") or 0, float(entry.get("rt") or row.rt),
        float(entry.get("area_pct") or 0.0),
        [mod.Hit(name, "", cas, quality) for name, cas, quality in hits],
    )
    name, id_status, cas = mod.display_identification(pbm, _quality_limit(sample))

    row.name = name
    row.cas = M.clean_cas(cas) if cas else "0"
    row.si = hits[0][2] if hits else None
    row.alt_hits = hits[1:]
    row.derived.update({
        "pbm_hits": hits,
        "pbm_peak": entry.get("peak"),
        "ms_rt": entry.get("rt"),
        "corrected_ms_rt": (entry.get("rt") + delay
                            if entry.get("rt") is not None else None),
        "secondary": [],
        "id_status": id_status,
        "match_status": "Matched",
    })


def rematch_blank(sample: M.Sample, row: M.PeakRow) -> None:
    """Re-match one row against the sample's blank references (SS VI.7 step 6).

    ``blank_rt_tolerance`` (0.04 min) and the same preference AutoLib applies --
    an overlapping integration window before apex distance -- but for one row
    rather than the whole one-to-one assignment: the rest of the determination
    keeps the matches it already has, and a blank peak another row already
    claimed is not handed out twice.

    An ISTD-protected row is never blank-subtracted (SS 11.4) and keeps its raw
    area. A row without a partner reads ``kein passendes Blanksignal``, which is
    the status the reference sample carries 37 times.
    """
    tolerance = _settings_value(sample, "blank_rt_tolerance", 0.04)
    protected = row.protected_standard

    blank = None if protected else _best_blank(sample, row, "blank_rows",
                                               "blank_ref", tolerance)
    blank_istd = None if protected else _best_blank(sample, row,
                                                    "blank_istd_rows",
                                                    "blank_istd_ref", tolerance)
    blank_area = float(getattr(blank, "area", 0.0) or 0.0) if blank else 0.0
    istd_area = float(getattr(blank_istd, "area", 0.0) or 0.0) if blank_istd else 0.0

    # Both references describe the same background, so it is subtracted once --
    # AutoLib's rule, repeated here rather than re-derived.
    subtracted = 0.0 if protected else max(blank_area, istd_area)
    raw = row.raw_area if row.raw_area is not None else (row.area or 0.0)
    corrected = raw if protected else max(0.0, raw - subtracted)

    if protected:
        status = "ISTD geschützt; keine Blanksubtraktion"
    elif not subtracted:
        status = "kein passendes Blanksignal"
    else:
        source = "Blank" if blank_area >= istd_area else "Blank+ISTD"
        status = f"{source} subtrahiert (Maximum aus Blank/Blank+ISTD)"
        if corrected == 0.0:
            status += "; auf 0 begrenzt"

    row.derived.update({
        "blank_area": blank_area, "blank_istd_area": istd_area,
        "subtracted_area": subtracted, "blank_status": status,
        "blank_ref": getattr(blank, "peak", None) if blank else None,
        "blank_istd_ref": (getattr(blank_istd, "peak", None)
                           if blank_istd else None),
    })
    if not row.is_manual("area"):
        row.area = corrected
        row.mark_edited("area")


def _best_blank(sample: M.Sample, row: M.PeakRow, attribute: str,
                claim_key: str, tolerance: float):
    """The unclaimed blank FID peak best matching one row, or None."""
    reference = blank_reference_rows(getattr(sample, attribute, None))
    if not reference:
        return None
    # Internal standards are ignored in a blank reference exactly as
    # ``subtract_blank_signals`` ignores them: the standard is in the blank+ISTD
    # by design and subtracting it would delete the peak it quantifies against.
    try:
        standards = engine().internal_standard_peak_ids(reference)
    except Exception:
        standards = set()
    claimed = {r.derived.get(claim_key) for r in sample.rows
               if r is not row and r.derived.get(claim_key) is not None}
    bounds = row.fid_bounds
    best = None
    best_key: Optional[tuple[int, float]] = None
    for item in reference:
        peak = item[0] if isinstance(item, (tuple, list)) else item
        if peak is None or getattr(peak, "peak", None) in standards:
            continue
        if getattr(peak, "peak", None) in claimed:
            continue
        delta = abs(peak.rt - row.rt)
        if delta > tolerance:
            continue
        overlaps = bounds is not None and not (bounds[1] < peak.start
                                               or peak.end < bounds[0])
        key = (0 if overlaps else 1, delta)
        if best_key is None or key < best_key:
            best, best_key = peak, key
    return best


def new_row(sample: M.Sample, start: float, end: float,
            origin: str = M.ORIGIN_MANUAL,
            parent: Optional[M.PeakRow] = None) -> M.PeakRow:
    """Create a peak row for a manual integration or a split fragment.

    The row id comes from the sample's own source and is never a reused one
    (SS 4), the sample is renumbered by retention time, and a split fragment
    takes the ``74a``/``74b`` form of its parent's FID peak so the trace back
    into ``RESULTS.CSV`` survives. Both a fragment and an added peak carry
    ``Manuelle Integration; Prüfung``, which is what puts them in front of a
    reviewer (SS VI.7 step 5).

    Identification, blank match and quantification are **not** done here: the
    caller sets the fragment's area first -- only it knows the trace -- and then
    runs :func:`reassign_row`, :func:`rematch_blank` and
    ``sample.recalculate()``, in that order.
    """
    if start >= end:
        raise ValueError("Der Integrationsbeginn muss vor dem Integrationsende "
                         "liegen.")
    row = M.PeakRow(
        row_id=_allocate_row_id(sample),
        peak_no=len(sample.rows) + 1,
        source="FID+PBM",
        rt=(start + end) / 2.0,
        area=None,
    )
    row.fid_start = float(start)
    row.fid_end = float(end)
    row.integration_origin = origin
    row.name = M.UNKNOWN_DISPLAY_NAME
    row.cas = "0"
    row.id_status = M.ID_UNKNOWN

    fid_peak: Any = None
    if parent is not None:
        row.fid_baseline = parent.fid_baseline
        row.fid_anchor_l = parent.fid_anchor_l
        row.fid_anchor_r = parent.fid_anchor_r
        # The parent's scale, so the fragments' areas sum to the parent's
        # exactly: both sides of the drop line use one conversion (SS VI.7).
        row.fid_scale = parent.fid_scale
        row.fid_pk_ty = parent.fid_pk_ty
        row.apex_scan = parent.apex_scan
        row.bg_scan = parent.bg_scan
        row.bounds_rule = parent.bounds_rule
        _promote_parent_to_fragment(sample, parent)
        fid_peak = _next_fragment_number(sample, parent.derived.get("fid_peak"))
        row.derived["split_parent"] = parent.row_id

    row.derived.update({
        "pbm_hits": [], "raw_area": None, "blank_area": 0.0,
        "blank_istd_area": 0.0, "subtracted_area": 0.0,
        "blank_status": "kein passendes Blanksignal",
        "protected_standard": "Nein",
        "id_status": M.ID_UNKNOWN, "match_status": "No MS match",
        "review": M.MANUAL_INTEGRATION_REVIEW,
        "fid_peak": fid_peak, "pbm_peak": None, "ms_rt": None,
        "corrected_ms_rt": None, "secondary": [],
    })
    row.snapshot()
    sample.rows.append(row)
    renumber_rows(sample)
    return row


def drop_row(sample: M.Sample, row: M.PeakRow) -> None:
    """Remove a row the analyst created, and renumber what is left.

    Only rows the analyst created are ever removed. A peak that came from
    ChemStation is **disabled** instead -- area 0, status ``Peak verworfen``,
    row kept -- so the report still documents that it was rejected (SS VI.8).
    That policy is the ``DeletePeak``/``DisablePeak`` decision and is not
    enforced here, because undo has to be able to put back whatever was taken
    away; what is enforced is that the id does not come back with it.
    """
    sample.rows = [r for r in sample.rows if r.row_id != row.row_id]
    sample.invalidate_spectrum(row.row_id)
    renumber_rows(sample)
    if isinstance(sample, NiasSample):
        sample.link_standards()


# --------------------------------------------------------------------------
# Doppelbestimmung
# --------------------------------------------------------------------------

def combine(sample1: M.Sample, sample2: M.Sample,
            tolerance: Optional[float] = None) -> list[dict[str, Any]]:
    """Merge two determinations into the Doppelbestimmung rows.

    Delegates the pairing to AutoLib: identity is primary, retention time is used
    for unknowns and for detecting conflicts.
    """
    settings = getattr(sample1, "settings", None)
    tol = tolerance if tolerance is not None else getattr(
        settings, "rt_tolerance", 0.035)
    return engine().combine_determinations(
        [_as_engine_peak(r) for r in report_rows(sample1)],
        [_as_engine_peak(r) for r in report_rows(sample2)],
        tol)


def report_rows(sample):
    """Live result rows; rejected peaks remain only in session/audit storage."""
    return [r for r in sample.rows if r.integration_origin != M.ORIGIN_DISABLED]


def _as_engine_peak(row: M.PeakRow) -> dict[str, Any]:
    """Rebuild the dict shape AutoLib works with, including manual edits.

    The v3.0 fields travel with it -- ``combine_determinations`` hangs the whole
    dict on the merged row as ``source1``/``source2``, so the workbook writer
    reads the integration state off exactly this (SS VI.13). AutoLib itself
    looks at a fixed set of keys and ignores the rest, so the additions cannot
    change how the two determinations merge.
    """
    d = row.derived
    return {
        # The workspace maps the merged row back onto the PeakRow through this
        # (SS VI.4): AutoLib reads a fixed set of keys and hangs the whole dict
        # on the merged row as source1/source2, so carrying it changes nothing
        # about how the two determinations pair.
        "row_id": row.row_id, "edited": tuple(sorted(row.edited)),
        "fid_start": row.fid_start, "fid_end": row.fid_end,
        "fid_baseline": row.fid_baseline, "fid_scale": row.fid_scale,
        "fid_pk_ty": row.fid_pk_ty,
        "integration_origin": row.integration_origin,
        "integration_label": row.integration_label,
        "integration_touched": row.integration_touched,
        "sn": row.sn, "purity": row.purity, "ri": row.ri,
        "loq_mgkg": d.get("loq_mgkg"),
        "rt": row.rt, "name": row.name, "cas": M.display_cas(row.cas),
        "quality": row.si, "area": row.area,
        "mg_dm2": d.get("mg_dm2"), "mg_kg": d.get("mg_kg"),
        "id_status": d.get("id_status", ""), "match_status": d.get("match_status", ""),
        "review": d.get("review", ""), "fid_peak": d.get("fid_peak"),
        "pbm_peak": d.get("pbm_peak"), "ms_rt": d.get("ms_rt"),
        "corrected_ms_rt": d.get("corrected_ms_rt"), "secondary": d.get("secondary"),
        "raw_area": d.get("raw_area"), "blank_area": d.get("blank_area"),
        "blank_istd_area": d.get("blank_istd_area"),
        "subtracted_area": d.get("subtracted_area"),
        "blank_status": d.get("blank_status", ""),
        "protected_standard": d.get("protected_standard") == "Ja",
    }

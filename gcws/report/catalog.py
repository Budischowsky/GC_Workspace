"""Every column a report template can show (no Qt).

Each :class:`Field` is a value of the Peaks / substances panel or of the double determination (DD) and
Replicates panels. ``per_det`` fields have a value in every determination, which a template column
shows as the mean (or the merged text), each determination, both, or "A / B" (see
:data:`gcws.report.template.VIEWS`); the others belong to the merged row. :func:`availability` says
whether the current quantification can fill a field, and why not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

GROUPS = ("Substance", "Peak", "Concentration", "Double determination", "Regulatory")
#: modes with one concentration unit only (no Conc. 2)
SINGLE_UNIT_MODES = ("istd_conc", "area_pct")


@dataclass(frozen=True)
class Field:
    key: str
    label: str                      # name in the template window
    group: str
    header: str                     # default report header ("{unit}" is the field's unit)
    kind: str = "number"            # "number" | "text"
    decimals: Optional[int] = None  # default decimals of a number
    per_det: bool = True            # a value per determination (else one for the merged row)
    agg: str = "mean"               # several determinations: mean | min | first | join
    summable: bool = False          # added up in the NIAS sum rows
    needs: str = ""                 # "dd" | "features" | "fid" | "ms" | "mgkg" | ""
    modes: tuple = ()               # the quantification modes that fill it (empty: every mode)
    tip: str = ""
    width: float = 9.0              # relative column width

    @property
    def numeric(self) -> bool:
        return self.kind == "number"


def _f(key, label, group, header=None, **kw) -> Field:
    return Field(key, label, group, header if header is not None else label, **kw)


S, P, C, D, R = GROUPS
_FIELDS = [
    # -- substance ----------------------------------------------------------------------------
    _f("rt", "RT", S, "RT (min)", decimals=2, width=7, tip="Retention time (mean of the determinations)"),
    _f("name", "Name", S, kind="text", agg="first", width=30,
       tip="Substance name; each determination: its own library hit (Hit A / Hit B)"),
    _f("cas", "CAS", S, "CAS-No.", kind="text", agg="first", width=11),
    _f("score", "Score", S, "% match", decimals=0, width=6, tip="Library match score of the chosen hit"),
    _f("id_status", "ID status", S, kind="text", agg="first", width=12),
    _f("library", "Library", S, kind="text", agg="join", width=14),
    _f("ri", "RI", S, decimals=0, width=6, tip="Retention index (needs a ladder)"),
    _f("rrt", "RRT", S, decimals=4, width=7, modes=("nias_mgkg",),
       tip="RT ÷ RT of the NIAS reference ISTD"),
    _f("class_hint", "Class hint", S, kind="text", agg="first", needs="ms", width=16,
       tip="Substance-class clue from the MS interpreter"),
    _f("flags", "Flags", S, kind="text", per_det=False, width=12,
       tip="new: in none of the learned evaluations, the register of reported substances and CASINFO.xlsx; "
           "element symbols: the formula has elements other than C, H, O, N"),
    # -- peak ---------------------------------------------------------------------------------
    _f("num", "Peak #", P, "#", decimals=0, agg="first", width=5),
    _f("ms_rt", "RT MS", P, "RT MS (min)", decimals=3, needs="fid", width=7,
       tip="Assigned component MS time, otherwise delay-corrected apex (FID)"),
    _f("type", "Type", P, kind="text", agg="join", width=6, tip="Peak type code (B, V, P, H, S, T, ...)"),
    _f("start", "Start", P, "Start (min)", decimals=3, width=7),
    _f("end", "End", P, "End (min)", decimals=3, width=7),
    _f("area", "Area", P, decimals=0, width=10, tip="Integrated peak area (raw)"),
    _f("area_pct", "Area %", P, decimals=3, width=7),
    _f("height", "Height", P, decimals=0, width=10),
    _f("w50", "W½", P, "W½ (s)", decimals=2, width=6),
    _f("sym", "Symmetry", P, decimals=2, width=7, tip="USP tailing factor"),
    _f("sn", "S/N", P, decimals=0, width=6),
    _f("origin", "Integration", P, kind="text", agg="join", width=10),
    _f("istd", "ISTD", P, kind="text", agg="first", width=6, tip="ISTD code bound to the peak"),
    _f("raw_area", "Raw area", P, decimals=0, width=10, tip="Area before the blank correction"),
    _f("blank_area", "Blank area", P, decimals=0, width=10, tip="Blank area subtracted in the calculation"),
    _f("corr_area", "Corr. area", P, decimals=0, width=10,
       tip="Area after the blank correction (the double determination's Area A / B)"),
    _f("in_blank", "In blank", P, kind="text", agg="join", width=10,
       tip="Peak also found in the assigned blank"),
    _f("blank_ratio", "Blank ratio", P, decimals=1, width=7, tip="Sample area ÷ matching blank peak area"),
    _f("area_minus_blank", "Area − blank", P, decimals=0, width=10),
    # -- concentration ------------------------------------------------------------------------
    _f("conc", "Conc. (mode unit)", C, "Conc. {unit}", decimals=4, summable=True, width=9,
       tip="Concentration in the unit of the quantification mode"),
    _f("report_unit_1", "Conc. 1", C, "Conc. 1 [{unit}]", summable=True, width=9,
       tip="The mode's Conc. 1 (NIAS mg/kg, HS / extraction: the report unit 1)"),
    _f("report_unit_2", "Conc. 2", C, "Conc. 2 [{unit}]", summable=True, width=9,
       tip="The mode's Conc. 2 (NIAS mg/dm², HS / extraction: the report unit 2)"),
    # -- double determination -----------------------------------------------------------------
    _f("reldiff", "Diff. %", D, decimals=1, per_det=False, needs="dd", width=6,
       tip="|A − B| ÷ mean × 100 (N-fold: relative difference)"),
    _f("sd", "SD", D, decimals=4, per_det=False, needs="dd", width=7),
    _f("rsd", "RSD %", D, decimals=1, per_det=False, needs="dd", width=6),
    _f("found_in", "Found in", D, kind="text", per_det=False, needs="dd", width=6,
       tip="Determinations the substance was found in (2/2, 2/3, ...)"),
    _f("verdict", "Verdict", D, kind="text", per_det=False, needs="dd", width=16),
    _f("notes", "Notes", D, kind="text", per_det=False, needs="dd", width=20,
       tip="Why the verdict (feature pairing) or the review notes"),
    _f("comment", "Comment", D, kind="text", per_det=False, needs="dd", width=16,
       tip="The analyst's comment in the double determination"),
    _f("outlier", "Outlier", D, kind="text", per_det=False, needs="dd", width=6,
       tip="The determination dismissed as an outlier"),
    _f("edited", "Changed by analyst", D, kind="text", per_det=False, needs="dd", width=10),
    _f("feature", "Feature", D, kind="text", per_det=False, needs="features", width=7),
    _f("similarity", "Similarity", D, decimals=2, per_det=False, needs="features", width=7),
    # -- regulatory ---------------------------------------------------------------------------
    _f("sml", "SML", R, "SML (mg/kg)", kind="text", per_det=False, width=8,
       tip="Specific migration limit from CASINFO.xlsx"),
    _f("ref", "Ref.", R, kind="text", per_det=False, width=8,
       tip="Regulation reference from CASINFO.xlsx, with its footnote marker"),
    _f("footnote", "Footnote", R, kind="text", per_det=False, width=20, tip="CASINFO.xlsx footnote as text"),
    _f("sml_check", "SML check", R, kind="text", per_det=False, needs="mgkg", width=8,
       tip="≤ SML, > SML or no SML (the mg/kg result)"),
    _f("qstatus", "SML status", R, "Status", kind="text", agg="first", modes=("nias_mgkg",), width=14,
       tip="The peak table's NIAS status"),
    _f("learned", "Learned rule", R, kind="text", agg="first", per_det=False, width=30,
       tip="The learned report rule that changed the row (rule, evidence, former name)"),
]


def _unit_fields() -> list[Field]:
    from gcws.quant import units as U
    from gcws.quant.conversion import UNIT_KEYS
    out = []
    for unit, key in UNIT_KEYS.items():
        out.append(Field(f"conc:{key}", unit, C, "Conc. {unit}", decimals=U.DECIMALS.get(unit, 4),
                         summable=True, width=9, tip=f"Concentration in {unit}"))
    return out


_AFTER = next(i for i, f in enumerate(_FIELDS) if f.key == "report_unit_2") + 1
FIELDS: dict[str, Field] = {f.key: f for f in _FIELDS[:_AFTER] + _unit_fields() + _FIELDS[_AFTER:]}


def get(key: str) -> Optional[Field]:
    return FIELDS.get(key)


def by_group() -> dict[str, list[Field]]:
    out: dict[str, list[Field]] = {g: [] for g in GROUPS}
    for f in FIELDS.values():
        out[f.group].append(f)
    return out


def views_for(field: Field) -> list[str]:
    """The double-determination views a field offers."""
    if not field.per_det:
        return ["mean"]
    if field.numeric:
        return ["mean", "each", "each_mean", "merged"]
    return ["mean", "each", "merged"]


@dataclass
class Info:
    """What the current quantification and determinations can fill."""
    mode: str = "nias_mgkg"
    units: tuple = ()                  # unit labels the mode gives (conversion.available_units)
    detector: str = "FID"
    features: bool = False             # feature pairing
    n: Optional[int] = None            # number of determinations (None: not known)
    has_ms: Optional[bool] = None
    has_cas: Optional[bool] = None

    @classmethod
    def of_quant(cls, quant: Optional[dict], n: Optional[int] = None, has_ms: Optional[bool] = None,
                 has_cas: Optional[bool] = None) -> "Info":
        from gcws.quant import conversion as CV
        from gcws.quant.service import quant_detector
        q = quant or {}
        pairing = (q.get("features") or {}).get("pairing")
        return cls(mode=q.get("mode", "nias_mgkg"), units=tuple(CV.available_units(q)),
                   detector=quant_detector(q), features=pairing == "features", n=n, has_ms=has_ms,
                   has_cas=has_cas)


def unit_label(key: str) -> Optional[str]:
    """The unit of a ``conc:<key>`` field."""
    from gcws.quant.conversion import UNIT_KEYS
    if not key.startswith("conc:"):
        return None
    return next((u for u, k in UNIT_KEYS.items() if k == key[5:]), None)


def mode_label(mode: str) -> str:
    from gcws.quant.service import MODES
    return MODES.get(mode, mode)


def availability(field: Field, info: Info) -> tuple[bool, str]:
    """``(ok, reason)``: whether ``info``'s quantification and determinations can fill ``field``."""
    if field.modes and info.mode not in field.modes:
        return False, "Only in " + " / ".join(mode_label(m) for m in field.modes)
    unit = unit_label(field.key)
    if unit is not None and unit not in info.units:
        return False, f"{mode_label(info.mode)} gives no {unit}"
    if field.key == "report_unit_2" and info.mode in SINGLE_UNIT_MODES:
        return False, f"{mode_label(info.mode)} has one unit only"
    if field.needs == "dd" and info.n is not None and info.n < 2:
        return False, "Needs two or more determinations"
    if field.needs == "features" and not info.features:
        return False, "Feature pairing only (Replicates panel)"
    if field.needs == "fid" and info.detector != "FID":
        return False, "Only with the FID detector"
    if field.needs == "ms" and info.has_ms is False:
        return False, "Needs MS data"
    if field.needs == "mgkg" and "mg/kg" not in info.units:
        return False, f"Needs a mg/kg result ({mode_label(info.mode)} gives none)"
    if field.group == "Regulatory" and field.key != "qstatus" and info.has_cas is False:
        return False, "Needs CASINFO.xlsx (Edit > Preferences)"
    return True, ""


def unit_of(key: str, quant: Optional[dict]) -> str:
    """The unit a concentration field is in ("" for the others)."""
    from gcws.quant import conversion as CV
    from gcws.quant.service import mode_unit
    if key == "conc":
        return mode_unit(quant or {})
    if key in ("report_unit_1", "report_unit_2"):
        units = CV.report_units(quant or {})
        i = int(key[-1]) - 1
        return units[i] if i < len(units) else ""
    return unit_label(key) or ""


def header_of(field: Field, header: str, quant: Optional[dict]) -> str:
    """The column header (``header`` or the field's default) with ``{unit}`` resolved."""
    text = header if header else field.header
    return text.replace("{unit}", unit_of(field.key, quant))

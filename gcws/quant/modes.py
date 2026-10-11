"""The quantification mode of a method: what each mode answers for itself (no Qt).

``of(quant)`` gives the mode of the method ``quant`` (``ws.quant``): NIAS mg/kg, NIAS total extraction
(µg/L), internal standard concentration, area percent, HS screening or extraction (quant method). Callers
ask the mode instead of comparing ``quant["mode"]`` with a string:

- ``unit``: the mode's own unit (Conc.); ``detector``: the signal it quantifies from;
- ``unit_keys()``, ``available_units()``, ``report_units()``: the further units (row keys), every unit it
  gives a result in, and Conc. 1 / Conc. 2 of its report;
- ``ratio(settings, members)`` and ``convert(value, settings, run_id)``: a value in the mode's unit in
  the other units;
- ``limits()``: the double determination's limits; ``edits_key(unit)``: where a replicate group keeps
  the analyst's edits;
- ``hs`` (MS only, its own settings under ``quant["hs"]``), ``nias`` (the AutoLib migration engine
  computes the mg/kg itself, with SML status, RRT and CASINFO), ``report_kind``, ``template_preset``.

A new mode is one more adapter here. ``quant["mode"]`` stays the stored key (missing = NIAS mg/kg)."""
from __future__ import annotations

from typing import Callable, Optional

from gcws.core.model import FID, TIC
from gcws.quant import limits as QL
from gcws.quant import service as QS

DEFAULT = "nias_mgkg"

Ratio = Callable[[int, str], Optional[float]]


class QuantMode:
    """A mode with no further units (internal standard concentration, and any unknown key): its unit
    is the one entered with the method (``quant["unit"]``)."""
    key = "istd_conc"
    hs = False
    nias = False
    report_kind: Optional[str] = None      # the report only this mode makes (None: NIAS and the others)
    template_preset = "NIAS"

    def __init__(self, quant: Optional[dict] = None, key: Optional[str] = None):
        self.quant = quant or {}
        if key is not None:
            self.key = key

    def __repr__(self) -> str:
        return f"<QuantMode {self.key}>"

    @property
    def label(self) -> str:
        return QS.MODES.get(self.key, self.key)

    @property
    def unit(self) -> str:
        return self.quant.get("unit", "µg/L")

    @property
    def detector(self) -> str:
        """TIC or FID: the quant panel's choice (HS screening: always TIC)."""
        return TIC if self.quant.get("detector") == TIC else FID

    def settings(self):
        """AutoLib ``Settings`` of the method (``gcws.quant.nias_bridge.make_settings``)."""
        from gcws.quant.nias_bridge import make_settings
        return make_settings(self.quant.get("settings"))

    def unit_keys(self) -> list[str]:
        """The further units (``QS.CONC_UNITS`` keys) the mode computes."""
        return []

    def available_units(self) -> list[str]:
        """The units the mode gives a result in: its own unit first, then the further units."""
        return list(dict.fromkeys([self.unit] + self._extra_units()))

    def _extra_units(self) -> list[str]:
        return [QS.CONC_UNITS[k] for k in self.unit_keys()]

    def report_units(self) -> list[str]:
        """Conc. 1 and Conc. 2 of the report (one unit where the mode has no other)."""
        return [self.unit]

    def convert(self, value, settings, run_id: str = "") -> dict:
        """``value`` in the mode's unit in the further units: ``QS.CONC_UNITS`` key -> value, and ``calc``
        (key -> the calculation in numbers). ``run_id``: the determination (extraction: its amount)."""
        return QS.unit_values(self.key, settings, conc=value)

    def ratio(self, settings, members) -> Ratio:
        """``ratio(k, unit)``: the factor from determination k's concentration (the mode's unit) to
        ``unit``; None when the unit cannot be computed (another mode's unit, a missing parameter)."""
        own = self.unit
        one = self.convert(1.0, settings) if self.unit_keys() else {}
        keys = {u: k for k, u in QS.CONC_UNITS.items()}

        def ratio(_k, unit):
            if unit == own:
                return 1.0
            key = keys.get(unit)
            return one.get(key) if key else None
        return ratio

    def limits(self) -> QL.QuantLimits:
        """The limits a double determination is compared with: only NIAS mg/kg has a reporting limit."""
        lim = QL.of_settings(self.settings())
        return lim if self.nias else QL.QuantLimits(lim.difference, 0.0, lim.rt_tolerance)

    @property
    def default_difference(self) -> float:
        """The difference limit (%) a new method starts with."""
        import gc_duplicate as GD
        return float(GD.DEFAULT_MAX_RELDIFF)

    def edits_key(self, unit: str) -> str:
        """Where a replicate group keeps the analyst's edits (``edits`` for the NIAS modes; per unit
        where an edited value belongs to its unit)."""
        return "edits"


class NiasMgkg(QuantMode):
    """NIAS screening: mg/kg food simulant from the ISTD factor and the migration conditions."""
    key = "nias_mgkg"
    nias = True

    @property
    def unit(self) -> str:
        return "mg/kg"

    def unit_keys(self) -> list[str]:
        return list(QS.NIAS_UNITS)

    def _extra_units(self) -> list[str]:
        return ["mg/kg"] + super()._extra_units()

    def report_units(self) -> list[str]:
        return ["mg/kg", "mg/dm²"]

    def convert(self, value, settings, run_id: str = "") -> dict:
        if value is None:
            return QS.unit_values(self.key, settings, conc=value)
        ov = QS._positive(getattr(settings, "ov_ratio", None))
        if not ov:
            out = QS.unit_values(self.key, settings)
            out["calc"] = dict.fromkeys(QS.CONC_UNITS, "Needs the surface/volume ratio (NIAS parameters)")
            return out
        mg_dm2 = value / ov
        out = QS.unit_values(self.key, settings, mg_dm2=mg_dm2, conc=value)
        out["calc"]["mg_dm2"] = (f"mg/dm² = mg/kg ÷ surface/volume = {QS._g(value)} ÷ {QS._g(ov)} dm²/kg = "
                                 f"{QS._g(mg_dm2)}")
        out["calc"].pop("conc", None)
        return out


class TotalUgl(QuantMode):
    """NIAS total extraction: µg/L in the extract."""
    key = "total_ugl"

    @property
    def unit(self) -> str:
        return "µg/L"

    def unit_keys(self) -> list[str]:
        return ["ug_l", "mg_l", "mg_ml"]

    def report_units(self) -> list[str]:
        return ["µg/L", "mg/L"]


class AreaPct(QuantMode):
    key = "area_pct"

    @property
    def unit(self) -> str:
        return "%"


class HsScreening(QuantMode):
    """HS screening: MS only (TIC peak areas), µg per headspace and per sample input."""
    key = "hs_screening"
    hs = True
    report_kind = "hs_screening"
    template_preset = "HS-Screening"

    @property
    def unit(self) -> str:
        return self.quant.get("hs", {}).get("unit", "µg/HS")

    @property
    def detector(self) -> str:
        return TIC

    def settings(self):
        from gcws.quant.nias_bridge import make_settings
        return make_settings(self.quant.get("hs"))

    def _extra_units(self) -> list[str]:
        from gcws.quant.hs import UNITS
        return list(UNITS)

    def report_units(self) -> list[str]:
        from gcws.quant.hs import report_units
        return report_units(self.quant.get("hs"))

    def ratio(self, settings, members) -> Ratio:
        from gcws.quant.hs import UNITS, per_ug
        own, members = self.unit, list(members)
        inputs = (self.quant.get("hs") or {}).get("samples") or {}

        def ratio(k, unit):
            if unit == own:
                return 1.0
            if unit not in UNITS or own not in UNITS:
                return None
            rid = members[k] if k < len(members) else ""
            a, b = per_ug(unit, inputs.get(rid)), per_ug(own, inputs.get(rid))
            return a / b if (a is not None and b) else None
        return ratio

    def limits(self) -> QL.QuantLimits:
        """HS screening has its own difference limit and RT tolerance and no reporting limit."""
        hs = self.quant.get("hs") or {}
        return QL.QuantLimits(difference=QL._number(hs.get("duplicate_max_reldiff"), QL.DIFFERENCE),
                              reporting=0.0, rt_tolerance=QL.of_settings(self.settings()).rt_tolerance)

    @property
    def default_difference(self) -> float:
        return 30.0

    def edits_key(self, unit: str) -> str:
        return "hs_edits:" + unit


class Extraction(QuantMode):
    """Extraction (quant method): a solid or a foil extracted into a volume, two units of choice."""
    key = "extraction"
    report_kind = "quant"
    template_preset = "Quantification"

    @property
    def unit(self) -> str:
        from gcws.quant import extraction as EX
        return EX.unit(self.quant)

    def unit_keys(self) -> list[str]:
        from gcws.quant import extraction as EX
        return EX.unit_keys(self.quant)

    def _extra_units(self) -> list[str]:
        from gcws.quant import extraction as EX
        from gcws.quant import units as U
        return U.allowed(EX.of(self.quant)["sample_type"])

    def report_units(self) -> list[str]:
        from gcws.quant import extraction as EX
        return list(EX.of(self.quant)["units"])

    def convert(self, value, settings, run_id: str = "") -> dict:
        from gcws.quant import extraction as EX
        from gcws.quant import units as U
        out: dict = dict.fromkeys(QS.CONC_UNITS)
        calc: dict = {}
        b, u1 = EX.basis(self.quant, run_id), EX.unit(self.quant)
        for k in EX.unit_keys(self.quant):
            u = U.label_of(k)
            out[k] = EX.convert_value(self.quant, run_id, value, u)
            r = U.ratio(u1, u, b)
            calc[k] = U.missing(u, b) or (f"{u} = {QS._g(value)} {u1} × {QS._g(r)} = {QS._g(out[k])}"
                                          if value is not None and r is not None else "")
        out["calc"] = calc
        return out

    def ratio(self, settings, members) -> Ratio:
        from gcws.quant import extraction as EX
        from gcws.quant import units as U
        own, u1, members = self.unit, EX.unit(self.quant), list(members)

        def ratio(k, unit):
            if unit == own:
                return 1.0
            rid = members[k] if k < len(members) else ""
            return U.ratio(u1, unit, EX.basis(self.quant, rid)) if unit in U.UNITS else None
        return ratio

    def edits_key(self, unit: str) -> str:
        return "extraction_edits:" + unit


ADAPTERS: dict[str, type[QuantMode]] = {c.key: c for c in (NiasMgkg, TotalUgl, QuantMode, AreaPct,
                                                            HsScreening, Extraction)}


def of(quant: Optional[dict]) -> QuantMode:
    """The mode of the method ``quant`` (NIAS mg/kg when it names none)."""
    quant = quant or {}
    key = quant.get("mode", DEFAULT)
    cls = ADAPTERS.get(key)
    return cls(quant) if cls is not None else QuantMode(quant, key)


def named(key: str, quant: Optional[dict] = None) -> QuantMode:
    """The mode ``key`` with the rest of the method ``quant`` (e.g. a mode chosen but not applied yet)."""
    return of({**(quant or {}), "mode": key})


#: mode key -> the report only that mode makes
REPORT_KINDS = {k: c.report_kind for k, c in ADAPTERS.items() if c.report_kind}

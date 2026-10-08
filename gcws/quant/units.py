"""Concentration units of an extraction: a substance mass (mg) per extract volume, sample mass or area.

``factor(unit, basis)`` is the number a mass in mg is multiplied by to give the unit, so two units of
one determination differ by a constant ratio (:func:`ratio`); ``basis`` holds the determination's
``volume_ml``, ``mass_g`` and ``area_dm2``."""
from __future__ import annotations

import math
from typing import Optional

#: unit -> (row key, basis key, multiplier of mg ÷ basis)
UNITS = {
    "mg/mL": ("mg_ml", "volume_ml", 1.0),
    "µg/L": ("ug_l", "volume_ml", 1e6),
    "mg/g": ("mg_g", "mass_g", 1.0),
    "mg/kg": ("mg_kg", "mass_g", 1e3),
    "µg/g": ("ug_g", "mass_g", 1e3),
    "µg/kg": ("ug_kg", "mass_g", 1e6),
    "mg/dm²": ("mg_dm2", "area_dm2", 1.0),
    "µg/dm²": ("ug_dm2", "area_dm2", 1e3),
    "mg/m²": ("mg_m2", "area_dm2", 100.0),
}
#: decimals of each unit in the reports and tables
DECIMALS = {"mg/mL": 6, "µg/L": 2, "mg/g": 6, "mg/kg": 4, "µg/g": 4, "µg/kg": 2, "mg/dm²": 4, "µg/dm²": 3,
            "mg/m²": 4, "µg/HS": 4}
#: what each basis is called when it is missing
BASIS_LABELS = {"volume_ml": "extract volume (mL)", "mass_g": "sample mass (g)", "area_dm2": "sample area (dm²)"}
SAMPLE_TYPES = {"solid": "Solid (g)", "foil": "Foil (dm²)"}
#: the basis key the sample amount fills, per sample type
AMOUNT_BASIS = {"solid": "mass_g", "foil": "area_dm2"}
#: the units the extraction method offers (mg/m² is the HS report's)
METHOD_UNITS = ("mg/mL", "µg/L", "mg/g", "mg/kg", "µg/g", "µg/kg", "mg/dm²", "µg/dm²")


def key(unit: str) -> str:
    return UNITS[unit][0]


def label_of(row_key: str) -> Optional[str]:
    return next((u for u, (k, _b, _m) in UNITS.items() if k == row_key), None)


def allowed(sample_type: str) -> list[str]:
    """The method units for a solid (volume and mass units) or a foil (volume and area units)."""
    keep = {"volume_ml", AMOUNT_BASIS.get(sample_type, "mass_g")}
    return [u for u in METHOD_UNITS if UNITS[u][1] in keep]


def positive(value) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def factor(unit: str, basis: dict) -> Optional[float]:
    """Multiplier from mg to ``unit``; ``None`` for an unknown unit or a missing basis."""
    if unit not in UNITS:
        return None
    _k, b, mult = UNITS[unit]
    value = positive((basis or {}).get(b))
    return mult / value if value else None


def convert(mg: Optional[float], unit: str, basis: dict) -> Optional[float]:
    f = factor(unit, basis)
    return mg * f if (mg is not None and f is not None) else None


def ratio(unit: str, to: str, basis: dict) -> Optional[float]:
    """``to`` ÷ ``unit`` for one determination: a value in ``unit`` times this is the value in ``to``."""
    a, b = factor(unit, basis), factor(to, basis)
    return b / a if (a and b is not None) else None


def missing(unit: str, basis: dict) -> str:
    """Why ``unit`` cannot be computed, or ""."""
    if unit not in UNITS:
        return f"Unknown unit {unit}"
    b = UNITS[unit][1]
    return "" if positive((basis or {}).get(b)) else f"Needs the {BASIS_LABELS[b]}"


def _g(v: float) -> str:
    return f"{v:.6g}"


def calc_text(unit: str, mg: float, basis: dict) -> str:
    """The calculation of ``unit`` from the mass in numbers (a cell's tooltip)."""
    why = missing(unit, basis)
    if why:
        return why
    _k, b, mult = UNITS[unit]
    value = float(basis[b])
    sym = {"volume_ml": "mL", "mass_g": "g", "area_dm2": "dm²"}[b]
    times = "" if mult == 1 else f" × {_g(mult)}"
    return f"{unit} = mass [mg]{times} ÷ {BASIS_LABELS[b].split(' (')[0]} = {_g(mg)} mg{times} ÷ {_g(value)} {sym} " \
           f"= {_g(convert(mg, unit, basis))}"

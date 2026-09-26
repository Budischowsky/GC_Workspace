"""Migration conditions: the texts the report needs, derived from the numbers.

The analyst enters the cell area (dm²) and the coverage factor; the report and the
legacy validator (``validate_migration_metadata``, unchanged) still want the texts
"Migrationszelle" and "Belegung". They follow from the numbers here, so the two can
never disagree. The calculation inputs are always taken from the current NIAS
parameters (``quant["settings"]``), which the parameter table edits.
"""
from __future__ import annotations

#: migration cells of the lab: area (dm²) -> name
CELLS = [(0.51, "Zelle groß"), (0.34, "Zelle klein"), (0.44, "Glaszelle")]
#: coverage factor -> occupancy text
OCCUPANCY = {1.0: "einfach", 2.0: "doppelt"}
#: simulants offered in the dialog; anything else is typed in ("Other...")
SIMULANTS = ["EtOH 95%", "EtOH 50%", "EtOH 20%", "Tenax"]
OTHER = "Other..."

CELL_NOTE = ("Cell sizes: large steel cell 0.51 dm², small steel cell 0.34 dm², glass cell 0.44 dm². "
             "Coverage factor 1 = one side in contact (einfach), 2 = both sides (doppelt).")


def _number(value):
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))


def cell_text(area) -> str:
    """``0.51`` -> "Zelle groß (0.51 dm²)"; an unknown area -> "Zelle (0.6 dm²)"."""
    a = _number(area)
    if a is None or a <= 0:
        return ""
    name = next((n for size, n in CELLS if abs(size - a) < 0.005), "Zelle")
    return f"{name} ({a:.2f} dm²)" if name != "Zelle" else f"Zelle ({_fmt(a)} dm²)"


def occupancy_text(factor) -> str:
    """``1`` -> "einfach", ``2`` -> "doppelt", ``1.5`` -> "1.5-fach"."""
    f = _number(factor)
    if f is None or f <= 0:
        return ""
    return next((t for v, t in OCCUPANCY.items() if abs(v - f) < 1e-9), f"{_fmt(f)}-fach")


def complete(meta: dict) -> dict:
    """``meta`` with "migration_cell" and "occupancy" derived from the cell area and coverage."""
    out = dict(meta or {})
    out["migration_cell"] = cell_text(out.get("cell_area_dm2")) or out.get("migration_cell", "")
    out["occupancy"] = occupancy_text(out.get("occupancy_factor")) or out.get("occupancy", "")
    return out


def current(quant: dict) -> dict:
    """The stored migration conditions with today's calculation inputs (parameter table).

    Empty when no conditions were entered yet. Validated by the legacy code, so a report
    gets exactly what the dialog would have produced."""
    meta = dict((quant or {}).get("migration") or {})
    if not meta:
        return {}
    from gcws.quant.nias_bridge import make_settings
    from gcws.report.legacy_api import main_script
    main = main_script()
    meta.update(main.migration_metadata_from_settings(make_settings((quant or {}).get("settings"))))
    try:
        return main.validate_migration_metadata(complete(meta))
    except ValueError:
        return complete(meta)

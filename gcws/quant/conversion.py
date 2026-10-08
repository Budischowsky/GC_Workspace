"""A replicate row's concentration in other units, for every quantification mode (no Qt).

The double determination page and the template report read the same numbers from here: each
determination k is converted with its own ratio (its own sample amount or HS input) and the mean with
:func:`gcws.quant.extraction.mean_in`, so the analyst's edits and a dismissed outlier count everywhere.

Units are named by their label ("mg/kg", "µg/L", ...); ``UNIT_KEYS`` gives the row key of each."""
from __future__ import annotations

from typing import Callable, Optional

from gcws.quant import service as QS

#: unit label -> row key (the peak table's and the double determination's column keys)
UNIT_KEYS = {**{u: k for k, u in QS.CONC_UNITS.items()}, "µg/HS": "ug_hs", "mg/m²": "mg_m2"}
#: the double determination's further-unit columns: A, B and the mean of each unit
UNIT_SIDES = (("a", "c1"), ("b", "c2"), ("mean", "mean"))

Ratio = Callable[[int, str], Optional[float]]


def letter(k: int) -> str:
    return chr(ord("A") + k) if k < 26 else str(k + 1)


def labels(ws, members) -> list[str]:
    """Each determination's label: its replicate letter from the folder name when unique, else A, B, C..."""
    from gcws.io.sequence import replicate_label
    out: list[str] = []
    for k, m in enumerate(members):
        lab = replicate_label(ws.runs[m].run.path.name) if m in ws.runs else ""
        out.append(lab if lab and lab not in out else letter(k))
    return out


def mode_of(quant) -> str:
    return (quant or {}).get("mode", "nias_mgkg")


def available_units(quant) -> list[str]:
    """The units the mode gives a result in: its own unit first, then the further units."""
    mode = mode_of(quant)
    own = QS.mode_unit(quant or {})
    if mode == "hs_screening":
        from gcws.quant.hs import UNITS
        extra = list(UNITS)
    elif mode == "extraction":
        from gcws.quant import extraction as EX
        from gcws.quant import units as U
        extra = U.allowed(EX.of(quant)["sample_type"])
    else:
        extra = [QS.CONC_UNITS[k] for k in QS.unit_keys(mode, quant)]
        if mode == "nias_mgkg":
            extra = ["mg/kg"] + extra
    return list(dict.fromkeys([own] + extra))


def report_units(quant) -> list[str]:
    """Conc. 1 and Conc. 2 of the mode (one unit where the mode has no other)."""
    mode = mode_of(quant)
    if mode == "nias_mgkg":
        return ["mg/kg", "mg/dm²"]
    if mode == "total_ugl":
        return ["µg/L", "mg/L"]
    if mode == "extraction":
        from gcws.quant import extraction as EX
        return list(EX.of(quant)["units"])
    if mode == "hs_screening":
        from gcws.quant.hs import report_units as hs_units
        return hs_units((quant or {}).get("hs"))
    return [QS.mode_unit(quant or {})]


def ratio_fn(quant, settings, members) -> Ratio:
    """``ratio(k, unit)``: the factor from determination k's concentration (the mode's unit) to ``unit``;
    None when the unit cannot be computed (another mode's unit, a missing amount or parameter)."""
    quant = quant or {}
    mode = mode_of(quant)
    own = QS.mode_unit(quant)
    members = list(members)

    if mode == "extraction":
        from gcws.quant import extraction as EX
        from gcws.quant import units as U
        u1 = EX.unit(quant)

        def ratio(k, unit):
            if unit == own:
                return 1.0
            rid = members[k] if k < len(members) else ""
            return U.ratio(u1, unit, EX.basis(quant, rid)) if unit in U.UNITS else None
        return ratio

    if mode == "hs_screening":
        from gcws.quant.hs import UNITS, per_ug
        inputs = (quant.get("hs") or {}).get("samples") or {}

        def ratio(k, unit):
            if unit == own:
                return 1.0
            if unit not in UNITS or own not in UNITS:
                return None
            rid = members[k] if k < len(members) else ""
            a, b = per_ug(unit, inputs.get(rid)), per_ug(own, inputs.get(rid))
            return a / b if (a is not None and b) else None
        return ratio

    # the NIAS modes: every further unit is a constant multiple of the mode's unit
    one = QS.from_mode_unit(mode, settings, 1.0) if mode in ("nias_mgkg", "total_ugl") else {}

    def ratio(_k, unit):
        if unit == own:
            return 1.0
        key = UNIT_KEYS.get(unit)
        return one.get(key) if key else None
    return ratio


def det_values(row: dict, n: int):
    """``(cs, mean, dismissed)``: each determination's concentration (a double determination's edited
    values included), the result and the dismissed determination (1-based, 0 = none)."""
    if n == 2 or not row.get("cs"):
        cs = [row.get("c1"), row.get("c2")][:max(1, min(n, 2))]
    else:
        cs = list(row.get("cs"))
    return cs, row.get("mean"), int(row.get("dismissed") or 0)


def in_unit(row: dict, n: int, unit: str, ratio: Ratio):
    """``(each, mean)``: the determinations' values and the result of ``row`` in ``unit``."""
    from gcws.quant import extraction as EX
    cs, mean, dismissed = det_values(row, n)
    each = []
    for k, c in enumerate(cs):
        r = ratio(k, unit) if c is not None else None
        each.append(c * r if (c is not None and r is not None) else None)
    return each, EX.mean_in(None, list(range(len(cs))), cs, mean, unit, dismissed, ratio=ratio)


def unit_cells(quant, settings, row: dict, members, units: list[str], labs) -> list[tuple]:
    """``(value, calculation)`` of the double determination's further-unit columns of ``row``: A, B and
    the mean of every ``QS.CONC_UNITS`` key (None outside ``units``), the analyst's edits included."""
    mode = mode_of(quant)
    if mode == "extraction":
        return _extraction_cells(quant, row, members, units, labs)
    conv = {side: QS.from_mode_unit(mode, settings, row.get(field)) if units else None
            for side, field in UNIT_SIDES}
    return [(conv[side][unit], conv[side]["calc"].get(unit)) if unit in units else (None, None)
            for unit in QS.CONC_UNITS for side, _field in UNIT_SIDES]


def _extraction_cells(quant, row: dict, members, units: list[str], labs) -> list[tuple]:
    """Extraction: A and B converted with their own run's sample amount, the mean from both (without a
    dismissed outlier)."""
    from gcws.quant import extraction as EX
    from gcws.quant import units as U
    ids = list(members[:2])
    ids += [""] * (2 - len(ids))
    u1 = EX.unit(quant)
    out = []
    for key in QS.CONC_UNITS:
        u = U.label_of(key)
        for (side, field), rid in zip(UNIT_SIDES, ids + [None]):
            if key not in units:
                out.append((None, None))
            elif side == "mean":
                v = EX.mean_in(quant, ids, [row.get("c1"), row.get("c2")], row.get("mean"), u,
                               int(row.get("dismissed") or 0))
                out.append((v, f"Mean of {labs[0]} and {labs[1]} in {u}, each with its own sample amount"
                            if v is not None else ""))
            else:
                v = EX.convert_value(quant, rid, row.get(field), u)
                r = U.ratio(u1, u, EX.basis(quant, rid))
                out.append((v, U.missing(u, EX.basis(quant, rid)) or
                            (f"{u} = {row.get(field):.6g} {u1} × {r:.6g}" if v is not None else "")))
    return out

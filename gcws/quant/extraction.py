"""Extraction quantification ("quant method"): a solid (g) or a foil (dm²) extracted into a volume (mL),
spiked with internal standards (the NIAS standards by default, stock concentration in mg/mL), the result
in two units of the analyst's choice (Conc. 1 and Conc. 2 of the report).

The standards are the Quant panel's internal standards (``quant["istd_defs"]``, concentration = stock
mg/mL) and their options (``istd_options``: mean area or one reference); the rest of the method is
``quant["method"]``. Per run, ``quant["method_samples"][run id]["amount"]`` replaces the method's sample
amount.

    standard amount [mg] = stock [mg/mL] × spiked volume [µL] ÷ 1000
    factor [mg/area]     = mean(stock) × spiked volume ÷ 1000 ÷ mean(standard area)   (or one reference)
    substance [mg]       = corrected area × factor
    unit                 = substance [mg] × units.factor(unit, basis)
"""
from __future__ import annotations

import copy
from typing import Optional

from gcws.quant import units as U

MODE = "extraction"


def default_method() -> dict:
    """Solid, 1 g, 10 mL, 10 µL of the NIAS standards; mg/kg and µg/L."""
    return {"name": "", "sample_type": "solid", "amount": 1.0, "extract_volume_ml": 10.0, "spike_ul": 10.0,
            "units": ["mg/kg", "µg/L"], "reporting_limit": 0.0}


def nias_standards() -> list[dict]:
    """The NIAS internal standards (IS1–IS3 quantifying, IS4 QC) with their stock concentrations in mg/mL."""
    import gc_fid
    from gcws.quant.nias_bridge import make_settings
    return gc_fid.default_istd_defs(make_settings({}))


def normalise(method: Optional[dict]) -> dict:
    """``method`` with every field present and valid (the units allowed for its sample type)."""
    out = default_method()
    for k, v in (method or {}).items():
        if k in out:
            out[k] = copy.deepcopy(v)
    if out["sample_type"] not in U.SAMPLE_TYPES:
        out["sample_type"] = "solid"
    for k in ("amount", "extract_volume_ml", "spike_ul", "reporting_limit"):
        try:
            out[k] = float(out[k])
        except (TypeError, ValueError):
            out[k] = default_method()[k]
    allowed = U.allowed(out["sample_type"])
    units = [u for u in (out["units"] if isinstance(out["units"], (list, tuple)) else []) if u in allowed]
    while len(units) < 2:
        units.append(next(u for u in allowed if u not in units))
    out["units"] = units[:2]
    out["name"] = str(out["name"] or "")
    return out


def of(quant: dict) -> dict:
    return normalise((quant or {}).get("method"))


def unit(quant: dict) -> str:
    """Conc. 1: the unit of the peak table's Conc. column and the double determination."""
    return of(quant)["units"][0]


def unit_keys(quant: dict) -> list[str]:
    return [U.key(u) for u in U.allowed(of(quant)["sample_type"])]


def amount(quant: dict, run_id: str) -> Optional[float]:
    """The run's sample amount (g or dm²): its own, else the method's."""
    own = U.positive(((quant or {}).get("method_samples") or {}).get(run_id, {}).get("amount"))
    return own if own is not None else U.positive(of(quant)["amount"])


def basis(quant: dict, run_id: str) -> dict:
    m = of(quant)
    out = {"volume_ml": U.positive(m["extract_volume_ml"]), "mass_g": None, "area_dm2": None}
    out[U.AMOUNT_BASIS[m["sample_type"]]] = amount(quant, run_id)
    return out


def factor(standards: list[dict], method: dict, options: Optional[dict] = None):
    """``(mg per area count, text, problem)`` from the run's standards (``NiasSample.standards``)."""
    import gc_fid
    spike = U.positive(method.get("spike_ul"))
    if not spike:
        return None, "", "Enter the spiked volume of the standards (µL)"
    usable = [s for s in standards if s.get("role") == gc_fid.ROLE_QUANTIFICATION
              and U.positive(s.get("concentration")) and U.positive(s.get("fid_area"))]
    if not usable:
        return None, "", "No quantifying standard found (identify or bind the internal standards)"
    options = options or {}
    if not options.get("use_mean_area", True):
        wanted = str(options.get("reference") or "").upper()
        usable = [next((s for s in usable if str(s.get("code") or "").upper() == wanted), usable[0])]
    conc = sum(float(s["concentration"]) for s in usable) / len(usable)
    area = sum(float(s["fid_area"]) for s in usable) / len(usable)
    f = conc * spike / 1000.0 / area
    which = "mean of " + ", ".join(str(s.get("code")) for s in usable) if len(usable) > 1 else str(usable[0].get("code"))
    text = (f"factor = stock × spiked volume ÷ 1000 ÷ area ({which}) = {conc:.6g} mg/mL × {spike:.6g} µL ÷ 1000 ÷ "
            f"{area:.6g} = {f:.6g} mg per area count")
    return f, text, ""


def rows(sample, quant: dict, run_id: str, conc_units: dict) -> tuple[dict, dict]:
    """``(values by gcws peak index, run info)``. Each value dict has ``amount_mg``, ``conc`` (Conc. 1) and
    every ``conc_units`` key (None where the sample type or a missing basis gives none), with ``calc``."""
    m = of(quant)
    b = basis(quant, run_id)
    options = (quant or {}).get("istd_options") or {}
    f, f_text, problem = factor(getattr(sample, "standards", []) or [], m, options)
    allowed = U.allowed(m["sample_type"])
    u1 = m["units"][0]
    info = {"factor": f, "factor_text": f_text, "problem": problem, "basis": b, "method": m,
            "missing": "; ".join(dict.fromkeys(x for x in (U.missing(u, b) for u in m["units"]) if x))}
    out = {}
    for row in sample.rows:
        idx = row.derived.get("gcws_index")
        if idx is None:
            continue
        corr = row.area
        mg = corr * f if (corr is not None and f) else None
        d: dict = dict.fromkeys(conc_units)
        calc: dict = {}
        for u in allowed:
            k = U.key(u)
            if k in d or k == U.key(u1):
                d[k] = U.convert(mg, u, b)
                calc[k] = U.calc_text(u, mg, b) if mg is not None else (problem or "No corrected area")
        d["amount_mg"] = mg
        d["conc"] = U.convert(mg, u1, b)
        if mg is not None:
            calc["conc"] = f"mass = corrected area × factor = {corr:.6g} × {f:.6g} = {mg:.6g} mg; " + \
                U.calc_text(u1, mg, b)
        else:
            calc["conc"] = problem or "No corrected area"
        d["calc"] = calc
        out[idx] = d
    return out, info


def convert_value(quant: dict, run_id: str, value: Optional[float], to: str) -> Optional[float]:
    """A value in Conc. 1's unit of ``run_id`` in the unit ``to``."""
    if value is None:
        return None
    r = U.ratio(unit(quant), to, basis(quant, run_id))
    return value * r if r is not None else None

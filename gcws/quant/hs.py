"""MS-only headspace screening. Amounts are micrograms per headspace vial."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from gcws.core.model import TIC

UNITS = ("µg/HS", "µg/dm²", "µg/g")


def default_defs():
    return [dict(code=f"HS{i}", name="", concentration=1.0, target_rt=None, quantify=True)
            for i in range(1, 8)]


def positive(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a positive {label}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"Enter a positive {label}")
    return number


def matched_standards(st, config):
    """Unique TIC peak bindings; ambiguous automatic matches require analyst binding."""
    defs = config.get("istd_defs", default_defs())
    if len(defs) != 7 or len({d.get("code") for d in defs}) != 7 or any(not d.get("code") for d in defs):
        raise ValueError("HS-Screening requires seven standards with unique nonempty codes")
    peaks = st.results[TIC].peaks
    ids, _ = st.ident_set(TIC).bind(peaks)
    bindings = config.get("istd_bindings", {}).get(st.id, {})
    tolerance = positive(config.get("rt_tolerance", 0.05), "ISTD RT tolerance")
    result, used = [], set()
    for d in defs:
        code, name = d["code"], str(d.get("name") or "").strip()
        candidates = []
        if code in bindings:
            if bindings[code] is not None:
                rt = float(bindings[code])
                candidates = [i for i, p in enumerate(peaks) if abs(p.apex_rt - rt) <= 0.01]
                if candidates:
                    distance = min(abs(peaks[i].apex_rt - rt) for i in candidates)
                    candidates = [i for i in candidates if abs(abs(peaks[i].apex_rt - rt) - distance) < 1e-9]
        else:
            target = d.get("target_rt")
            if target not in (None, ""):
                rt = float(target)
                candidates = [i for i, p in enumerate(peaks) if abs(p.apex_rt - rt) <= tolerance]
            elif name:
                candidates = [i for i, ident in ids.items() if ident.name.strip().casefold() == name.casefold()]
        index = candidates[0] if len(candidates) == 1 else None
        status = "Found" if index is not None else ("Ambiguous: bind peak" if candidates else "Not found")
        if index in used:
            index, status = None, "Peak already bound to another ISTD"
        if index is not None:
            used.add(index)
        result.append(dict(d, index=index, status=status,
                           rt=peaks[index].apex_rt if index is not None else None,
                           area=peaks[index].area if index is not None else None))
    return result


#: the calibration of the HS standards: in each sample, in calibration runs (role Standard), or entered
CALIBRATIONS = ("internal", "external", "manual")
#: what is missing when the external calibration has no calibration run
NO_CALIBRATION_RUN = "Tick a calibration run in the HS panel (role Standard), or enter the areas"
METHODS = {"internal": "Headspace; TIC; internal standard response",
           "external": "Headspace; TIC; external standard (calibration runs)",
           "manual": "Headspace; TIC; external standard (entered areas)"}


def external(cfg) -> bool:
    """External 1-point calibration (from calibration runs or entered areas): the samples hold no ISTD."""
    return cfg.get("calibration") in ("external", "manual")


def manual(cfg) -> bool:
    """External calibration from the TIC areas the analyst entered (``istd_defs[i]["area"]``)."""
    return cfg.get("calibration") == "manual"


def calibration_mode(cfg) -> str:
    return "manual" if manual(cfg) else "external" if external(cfg) else "internal"


def standards_in(cfg, role: str) -> bool:
    """Whether a run of ``role`` holds the HS standards (they are matched, bound and kept out of the blank
    correction there): every run with internal standards, only the calibration runs (role Standard) with
    external calibration runs, none with entered areas."""
    if manual(cfg):
        return False
    return role == "standard" if external(cfg) else True


def factor(active, cfg, missing="Identify or bind every activated HS standard"):
    """µg per area count from the activated standards: Σ amount ÷ Σ area, and the problems that prevent it."""
    problems = []
    if not active:
        problems.append("Activate at least one HS internal standard")
    if any(s["area"] is None for s in active):
        problems.append(missing)
    if not cfg.get("use_mean_area", True) and len(active) != 1:
        problems.append("Single-standard calculation requires exactly one activated HS standard")
    if problems:
        return None, problems
    amounts = [positive(s.get("concentration"), f"amount for {s['code']} (µg/HS)") for s in active]
    areas = [positive(s["area"], f"TIC area for {s['code']}") for s in active]
    return sum(amounts) / sum(areas), []


def entered(cfg):
    """The external calibration from entered areas: (standards, factor, problems, [])."""
    standards, missing = [], []
    for d in cfg.get("istd_defs", default_defs()):
        area = d.get("area")
        ok = area not in (None, "")
        standards.append(dict(d, index=None, rt=None, runs=0, areas=[], area=float(area) if ok else None,
                              status="Entered" if ok else "Enter the TIC area"))
        if not ok and d.get("quantify", True):
            missing.append(d["code"])
    active = [s for s in standards if s.get("quantify", True)]
    value, problems = factor(active, cfg, f"Enter the TIC area of {', '.join(missing)} (HS standards)")
    return standards, value, problems, []


def calibration(ws, cfg):
    """The external calibration: (standards, factor, problems, run names). From calibration runs, each
    HS standard's TIC area is averaged over every Standard run (``areas``: [(run name, area or None)]);
    with entered areas (:func:`manual`) the definitions carry them."""
    if manual(cfg):
        return entered(cfg)
    runs = [st for st in ws.states() if st.role == "standard"]
    problems, matched = [], []
    for st in runs:
        if TIC not in st.results:
            problems.append(f"Standard run {st.name} has no integrated TIC")
        else:
            matched.append((st.name, matched_standards(st, cfg)))
    if not runs:
        problems.append(NO_CALIBRATION_RUN)
    standards, missing = [], []
    for k, d in enumerate(cfg.get("istd_defs", default_defs())):
        found = [m[k] for _, m in matched if m[k]["index"] is not None]
        lost = [name for name, m in matched if m[k]["index"] is None]
        complete = bool(found) and not lost
        n = len(found)
        status = (f"Mean of {n} Standard run{'s' if n != 1 else ''}" if complete else
                  f"Not found in {', '.join(lost)}" if lost else
                  "No calibration run" if not runs else "No Standard run with a TIC")
        standards.append(dict(d, index=None, status=status, runs=n,
                              areas=[(name, m[k]["area"] if m[k]["index"] is not None else None)
                                     for name, m in matched],
                              rt=sum(s["rt"] for s in found) / n if complete else None,
                              area=sum(s["area"] for s in found) / n if complete else None))
        if lost and d.get("quantify", True):
            missing.append(f"Identify or bind {d['code']} in Standard run {', '.join(lost)}")
    if not runs:
        return standards, None, problems, []
    active = [s for s in standards if s.get("quantify", True)]
    value, more = factor(active, cfg, "; ".join(missing) or "Identify or bind every activated HS standard")
    return standards, value if not problems else None, problems + more, [st.name for st in runs]


@dataclass
class HSRow:
    rt: float
    name: str
    cas: str
    area: float
    derived: dict
    score: float | None = None


@dataclass
class HSSample:
    name: str
    rows: list = field(default_factory=list)
    standards: list = field(default_factory=list)
    mean_factor: float | None = None
    meta: dict = field(default_factory=dict)
    mode: str = "hs_screening"


def compute(ws):
    from gcws.quant.service import QuantResult
    out = QuantResult()
    cfg = ws.quant.get("hs", {})
    unit = cfg.get("unit", UNITS[0])
    if external(cfg):
        try:
            cal = calibration(ws, cfg)
        except (ValueError, TypeError, KeyError) as exc:
            cal = [], None, [str(exc)], []
    for st in ws.states():
        if st.role not in ("sample", "standard"):
            continue
        try:
            if TIC not in st.results:
                raise ValueError("HS-Screening requires an integrated TIC")
            sample = HSSample(st.name)
            out.samples[st.id] = sample
            if external(cfg):
                # A Standard run shows its own standards (to check and bind); a sample has none of them.
                averaged, sample.mean_factor, problems, cal_runs = cal
                problems = list(problems)
                own = st.role == "standard" and not manual(cfg)
                sample.standards = matched_standards(st, cfg) if own else averaged
            else:
                sample.standards = matched_standards(st, cfg)
                active = [s for s in sample.standards if s.get("quantify", True)]
                sample.mean_factor, problems = factor(active, cfg)
            amount_valid = not problems
            inputs = cfg.get("samples", {}).get(st.id, {})
            normalizers = {}
            for input_key in ("area_dm2", "mass_g"):
                try:
                    normalizers[input_key] = positive(inputs.get(input_key), input_key)
                except ValueError:
                    normalizers[input_key] = None
            denominator = 1.0
            if unit not in UNITS:
                problems.append("Unsupported HS result unit")
            elif unit != UNITS[0]:
                key, label = ("area_dm2", "sample area (dm²)") if unit == UNITS[1] else ("mass_g", "sample mass (g)")
                try:
                    denominator = positive(inputs.get(key), label)
                except ValueError as exc:
                    problems.append(str(exc))
            sample.meta = dict(unit=unit, inputs=dict(inputs), source=str(st.run.path),
                               factor=sample.mean_factor, denominator=denominator,
                               calculation=calculation(cfg, cal_runs if external(cfg) else ()),
                               method=METHODS[calibration_mode(cfg)],
                               blank_correction=cfg.get("blank_correction", True),
                               blanks=[ws.runs[b].name for b in st.blanks + st.blanks_istd if b in ws.runs])
            if problems:
                out.errors[st.id] = "; ".join(problems)
            standards = {s["index"]: s for s in sample.standards if s["index"] is not None}
            peaks = st.results[TIC].peaks
            ids, _ = st.ident_set(TIC).bind(peaks)
            blank_areas = {}
            if cfg.get("blank_correction", True):
                tol = positive(cfg.get("blank_rt_tolerance", 0.04), "blank RT tolerance")
                for rid in dict.fromkeys(st.blanks + st.blanks_istd):
                    ref = ws.runs.get(rid)
                    if ref is None or TIC not in ref.results:
                        raise ValueError("Every assigned HS blank must have a TIC integration")
                    # One-to-one matching per blank, closest apex first.
                    pairs = sorted((abs(p.apex_rt - b.apex_rt), i, j) for i, p in enumerate(peaks)
                                   if i not in standards for j, b in enumerate(ref.results[TIC].peaks)
                                   if abs(p.apex_rt - b.apex_rt) <= tol)
                    used_i, used_j = set(), set()
                    for _, i, j in pairs:
                        if i not in used_i and j not in used_j:
                            used_i.add(i)
                            used_j.add(j)
                            blank_areas[i] = max(blank_areas.get(i, 0), ref.results[TIC].peaks[j].area)
            rows = {}
            for i, p in enumerate(peaks):
                ident = ids.get(i)
                correction = blank_areas.get(i, 0.0)
                area = max(0.0, p.area - correction)
                amount = area * sample.mean_factor if sample.mean_factor and amount_valid else None
                ug_dm2 = amount / normalizers["area_dm2"] if amount is not None and normalizers["area_dm2"] else None
                ug_g = amount / normalizers["mass_g"] if amount is not None and normalizers["mass_g"] else None
                d = dict(gcws_index=i, istd=standards[i]["code"] if i in standards else "",
                         raw_area=p.area, blank_area=correction, corr_area=area,
                         amount_ug=amount, ug_hs=amount, ug_dm2=ug_dm2, ug_g=ug_g,
                         conc=amount / denominator if amount is not None and not problems else None,
                         status="; ".join(problems), id_status=getattr(ident, "status", "") or "Unknown",
                         blank_status="Blank corrected" if correction else "", review="")
                rows[i] = d
                sample.rows.append(HSRow(p.apex_rt, getattr(ident, "name", "") or "Unknown",
                                        getattr(ident, "cas", ""), area, d, getattr(ident, "score", None)))
            out.rows[st.id] = rows
        except (ValueError, TypeError, KeyError) as exc:
            out.errors[st.id] = str(exc)
    return out


def calculation(cfg, runs=()):
    """The calculation as the report's HS calculation sheet names it."""
    text = "Mean ISTD areas" if cfg.get("use_mean_area", True) else "Single ISTD"
    if manual(cfg):
        return f"External 1-point calibration ({text}) from entered areas"
    if external(cfg):
        return f"External 1-point calibration ({text}) from {len(runs)} Standard run" +             ("s" if len(runs) != 1 else "") + (f": {', '.join(runs)}" if runs else "")
    return text


def engine_peaks(sample, value=None):
    return [dict(rt=r.rt, name=r.name, cas=r.cas,
                 mg_kg=value(r) if value else r.derived.get("conc"),
                 id_status=r.derived["id_status"], review="",
                 score=r.score, area=r.area,
                 blank_area=r.derived["blank_area"], blank_istd_area=0.0)
            for r in sample.rows if not r.derived.get("istd")]

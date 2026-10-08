"""The Peaks / substances panel's values for any run (no Qt).

The peak table shows them for the active run; the template report asks for them per determination,
so each getter takes the run and the signal explicitly: ``VALUES[key](row, ws, run_id, signal_key)``."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gcws.core.keys import is_fid


@dataclass
class Row:
    index: int
    peak: Any
    ident: Any
    quant: dict


def rows_for(ws, run_id: str, key: str) -> list[Row]:
    """The table rows of one run and signal, built like ``PeakTableModel.reload``."""
    st = ws.runs.get(run_id)
    res = ws.result(run_id, key) if st is not None else None
    if res is None:
        return []
    idents, _ = st.ident_set(key).bind(res.peaks)
    quant = ws.quant_rows(run_id, key) if hasattr(ws, "quant_rows") else {}
    return [Row(i, p, idents.get(i), quant.get(i, {})) for i, p in enumerate(res.peaks)]


def ms_rt(r: Row, ws, run_id, key):
    """The assigned component's MS time, otherwise the delay-corrected apex (FID tables only)."""
    st = ws.runs.get(run_id) if run_id else None
    if st is None or not is_fid(key):
        return None
    component = r.peak.extra.get("deconv_component")
    return component["rt"] if component else r.peak.apex_rt - st.delay_value


def blank_match(r: Row, ws, run_id, key):
    f = getattr(ws, "blank_matches", None)
    if f is None or not run_id or run_id not in ws.runs:
        return None
    return f(run_id, key).get(r.index)


def area_minus_blank(r: Row, ws, run_id, key):
    m = blank_match(r, ws, run_id, key)
    if m is None:
        return None
    return max(0.0, r.peak.area - ws.blank_options().scale * m.blank_area)


def compute_hint(st, key, peak) -> tuple[str, str]:
    """``(short text, details)`` of the MS interpreter for ``peak``'s spectrum ("", "" without MS data)."""
    if st is None or st.run.ms is None:
        return ("", "")
    from gcws.ms.assignment import override_for
    from gcws.ms.interpret import Context, interpret
    from gcws.ms.spectra import extract
    try:
        spec = extract(st.run, peak, key, st.delay_value, "average_bg", override=override_for(st, key, peak))
        ms = st.run.ms
        r = interpret(spec.mz, spec.ab, Context(mass_range=ms.mass_range(), min_abundance=ms.min_abundance()))
    except Exception:  # noqa: BLE001 - a hint is optional
        return ("", "")
    if r.classes:
        c = r.classes[0]
        text = f"{c.label} ({c.level})"
    elif r.compounds:
        text = f"like {r.compounds[0].name}"
    else:
        text = "-"
    return (text, r.summary)


def class_hint(ws, run_id, key, peak) -> tuple[str, str]:
    """The class hint now (the workspace's hint cache when it already knows it)."""
    st = ws.runs.get(run_id) if run_id else None
    if st is None or st.run.ms is None:
        return ("", "")
    cache = getattr(ws, "hints", None)
    hit = cache.known(st, key, peak) if cache is not None and hasattr(cache, "known") else None
    if hit is None:
        hit = compute_hint(st, key, peak)
        if cache is not None and hasattr(cache, "store"):
            cache.store(st, key, peak, hit)
    return hit


def _q(name, default=None):
    return lambda r, ws, rid, key: r.quant.get(name, default)


def _ident(name, default=""):
    return lambda r, ws, rid, key: getattr(r.ident, name) if r.ident else default


#: every peak-table column key -> ``getter(row, ws, run_id, signal_key)``
VALUES: dict[str, Callable] = {
    "num": lambda r, ws, rid, key: r.peak.number,
    "rt": lambda r, ws, rid, key: r.peak.apex_rt,
    "ms_rt": ms_rt,
    "type": lambda r, ws, rid, key: r.peak.type_code,
    "start": lambda r, ws, rid, key: r.peak.start,
    "end": lambda r, ws, rid, key: r.peak.end,
    "area": lambda r, ws, rid, key: r.peak.area,
    "area_pct": lambda r, ws, rid, key: r.peak.area_pct,
    "height": lambda r, ws, rid, key: r.peak.height,
    "w50": lambda r, ws, rid, key: r.peak.width50 * 60 if r.peak.width50 else None,
    "sym": lambda r, ws, rid, key: r.peak.symmetry,
    "sn": lambda r, ws, rid, key: r.peak.sn,
    "name": _ident("name"),
    "cas": _ident("cas"),
    "score": _ident("score", None),
    "status": _ident("status"),
    "library": _ident("library"),
    "ri": _q("ri"),
    "rrt": _q("rrt"),
    "istd": _q("istd", ""),
    "blank_area": _q("blank_area"),
    "corr_area": _q("corr_area"),
    "in_blank": lambda r, ws, rid, key: (lambda m: m.text if m else "")(blank_match(r, ws, rid, key)),
    "blank_ratio": lambda r, ws, rid, key: (lambda m: m.ratio if m else None)(blank_match(r, ws, rid, key)),
    "area_minus_blank": area_minus_blank,
    **{k: _q(k) for k in ("mg_dm2", "ug_dm2", "mg_m2", "mg_g", "mg_kg", "ug_kg", "ug_l", "mg_l", "mg_ml",
                          "ug_hs", "ug_g", "conc")},
    "sml": _q("sml", ""),
    "qstatus": _q("status", ""),
    "origin": lambda r, ws, rid, key: r.peak.origin,
    "class_hint": lambda r, ws, rid, key: class_hint(ws, rid, key, r.peak)[0],
}

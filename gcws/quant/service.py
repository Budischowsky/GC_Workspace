"""Quantification of all loaded sample runs (FID), in the selected mode."""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

from gcws.core.model import FID
from gcws.io.sequence import BLANK, BLANK_ISTD, LADDER, SAMPLE, STANDARD
from gcws.quant import nias_bridge as NB

MODES = {
    "nias_mgkg": "NIAS screening (mg/kg via ISTD, migration)",
    "istd_conc": "Internal standard concentration",
    "total_ugl": "NIAS total extraction (µg/L)",
    "area_pct": "Area percent",
    "hs_screening": "HS-Screening (MS only)",
}
UNITS = ["µg/L", "mg/L", "mg/mL", "µg/mL", "ng/mL", "mg/kg", "µg/g", "%"]
log = logging.getLogger(__name__)


def mode_unit(quant: dict) -> str:
    mode = quant.get("mode", "nias_mgkg")
    if mode == "hs_screening":
        return quant.get("hs", {}).get("unit", "µg/HS")
    if mode == "nias_mgkg":
        return "mg/kg"
    if mode == "total_ugl":
        return "µg/L"
    if mode == "area_pct":
        return "%"
    return quant.get("unit", "µg/L")


_CAS = {"path": None, "lookup": {}}


def cas_lookup() -> dict:
    """CASINFO.xlsx (SML) through the vendored NIAS reader, cached."""
    from gcws.ui.dialogs.preferences import load_settings
    from gcws import paths
    from pathlib import Path
    raw = load_settings().get("standard_cas_path", "CASINFO.xlsx")
    p = Path(raw)
    if not p.is_absolute():
        for base in (paths.RESOURCES, paths.ROOT, paths.DATA):
            if (base / p).exists():
                p = base / p
                break
    if not p.exists():
        return {}
    if _CAS["path"] != str(p):
        try:
            import warnings
            from gcws.report.legacy_api import main_script
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _CAS["lookup"] = main_script().load_cas_lookup(p)
            for w in caught:
                log.info("CASINFO: %s", w.message)
            _CAS["path"] = str(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("CASINFO not readable: %s", exc)
            _CAS["lookup"] = {}
            _CAS["path"] = str(p)
    return _CAS["lookup"]


class QuantResult:
    def __init__(self):
        self.samples: dict[str, Any] = {}          # run id -> NiasSample
        self.rows: dict[str, dict[int, dict]] = {}  # run id -> peak index -> values
        self.errors: dict[str, str] = {}


def rrt_reference(sample, quant, st, key="FID"):
    """Measured reference RT and status, without target-RT or alternate-ISTD fallback."""
    from gcws.core.keys import is_fid
    code = quant.get("rrt_reference")
    status = "Select an RRT reference ISTD in the NIAS quantification panel"
    reference = None
    if code:
        status = f"RRT reference {code} is not found or bound in this sample"
        matches = [s for s in getattr(sample, "standards", []) if s.get("code") == code]
        if len(matches) == 1 and matches[0].get("row_id") is not None:
            try:
                reference = float(matches[0]["fid_rt"])
                if not math.isfinite(reference) or reference <= 0:
                    raise ValueError("Invalid measured FID RT")
                if not is_fid(key):
                    reference -= st.delay_value
                if not math.isfinite(reference) or reference <= 0:
                    reference = None
                    status = f"RRT reference {code} has no positive measured RT on this detector"
                else:
                    status = f"RRT reference: {code}, measured RT {reference:.4f} min"
            except (KeyError, TypeError, ValueError):
                reference = None
                status = f"RRT reference {code} has no positive measured RT"
    return reference, status


def rrt_rows(sample, quant, st, peaks, key):
    """RRT on the displayed detector axis."""
    if quant.get("mode", "nias_mgkg") != "nias_mgkg":
        return {}
    reference, status = rrt_reference(sample, quant, st, key)
    return {i: {"rrt": p.apex_rt / reference if reference is not None else None,
                "rrt_status": status} for i, p in enumerate(peaks)}


def compute(ws) -> QuantResult:
    if (ws.quant or {}).get("mode") == "hs_screening":
        from gcws.quant.hs import compute as compute_hs
        return compute_hs(ws)
    out = QuantResult()
    quant = ws.quant or {}
    settings = NB.make_settings(quant.get("settings"))
    import gc_fid
    defs = gc_fid.normalise_istd_defs(quant.get("istd_defs")) if quant.get("istd_defs") else \
        gc_fid.default_istd_defs(settings)
    options = gc_fid.normalise_istd_options(quant.get("istd_options") or {})
    mode = quant.get("mode", "nias_mgkg")
    lookup = cas_lookup() if mode == "nias_mgkg" else {}
    dets: dict[str, Any] = {}

    def det_of(rid):
        if rid not in dets:
            st = ws.runs.get(rid)
            dets[rid] = NB.convert(st) if st is not None and FID in st.results else None
        return dets[rid]

    for st in ws.states():
        if st.role not in (SAMPLE, STANDARD) or FID not in st.results:
            continue
        try:
            det = det_of(st.id)
            if det is None:
                continue
            blank_rows = NB.merge_references([det_of(b) for b in st.blanks if b in ws.runs], settings)
            blank_istd_rows = NB.merge_references([det_of(b) for b in st.blanks_istd if b in ws.runs], settings)
            result = NB.analyse(det, settings, blank_rows, blank_istd_rows)
            bindings = (quant.get("istd_bindings") or {}).get(st.id) or {}
            sample = NB.build_sample(st, det, result, settings, istd_defs=defs, istd_options=options,
                                     cas_lookup=lookup, istd_bindings=bindings)
            ladder = {int(k): float(v) for k, v in ((quant.get("ri") or {}).get("ladder") or {}).items()}
            if ladder:
                import gc_qc
                for row in sample.rows:
                    row.ri = gc_qc.retention_index(row.rt, ladder)
            out.samples[st.id] = sample
            out.rows[st.id] = rows_for(sample, st, mode, quant, settings, defs, options)
        except Exception as exc:  # noqa: BLE001 - one run must not stop the others
            log.exception("quantification of %s failed", st.name)
            out.errors[st.id] = str(exc)
    return out


def istd_reference_area(sample, options) -> Optional[float]:
    """ISTD area for the "Internal standard concentration" mode: the mean area of the
    quantifying ISTDs found in the run (or the reference ISTD's area when the factor is not
    formed from the mean). Unlike the NIAS factor it needs no concentration in the ISTD table:
    the concentration is the one entered for the mode."""
    import gc_fid
    found = []
    for s in sample.standards:
        try:
            area = float(s.get("fid_area") or 0)
        except (TypeError, ValueError):
            area = 0.0
        if s.get("role") == gc_fid.ROLE_QUANTIFICATION and area > 0:
            found.append((str(s.get("code") or "").upper(), area))
    if not found:
        return None
    if not (options or {}).get("use_mean_area", True):
        wanted = str((options or {}).get("reference") or "").upper()
        return next((a for c, a in found if c == wanted), found[0][1])
    return sum(a for _c, a in found) / len(found)


def rows_for(sample, st, mode, quant, settings, defs, options) -> dict[int, dict]:
    import gc_fid
    istd_of = {}
    for std in sample.standards:
        if std.get("row_id") is not None:
            istd_of[std["row_id"]] = std.get("code") or std.get("name")
    mean_area = sample.meta.get("istd_mean_area")
    c_istd = None
    if mode == "total_ugl":
        c_istd = gc_fid.derived_istd_concentration_ugl(settings, defs, options)
    elif mode == "istd_conc":
        try:
            c_istd = float(quant.get("istd_conc_value") or 0) or None
        except (TypeError, ValueError):
            c_istd = None
        mean_area = istd_reference_area(sample, options)
    out = {}
    res = st.results.get(FID)
    for row in sample.rows:
        idx = row.derived.get("gcws_index")
        if idx is None:
            continue
        corr = row.area
        d = row.derived
        if mode == "nias_mgkg":
            conc = d.get("mg_kg")
        elif mode in ("istd_conc", "total_ugl"):
            conc = (corr / mean_area * c_istd) if (corr is not None and mean_area and c_istd) else None
        else:
            conc = res.peaks[idx].area_pct if res is not None else None
        out[idx] = {
            "istd": istd_of.get(row.row_id, ""),
            "ri": getattr(row, "ri", None),
            "raw_area": d.get("raw_area"),
            "blank_area": max(d.get("blank_area") or 0.0, d.get("blank_istd_area") or 0.0) or None,
            "corr_area": corr,
            "mg_dm2": d.get("mg_dm2") if mode == "nias_mgkg" else None,
            "conc": conc,
            "sml": d.get("sml") if d.get("sml") is not None else "",
            "status": d.get("status", "") if mode == "nias_mgkg" else "",
            "blank_status": d.get("blank_status", ""),
            "review": d.get("review", ""),
        }
    return out

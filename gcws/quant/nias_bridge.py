"""NIAS quantification from our own integration (no RESULTS.CSV).

AutoLib's arithmetic -- blank subtraction, standard detection, the ISTD factor
chain, identification rules -- is reused unchanged. Only its input changes:
instead of parsing ChemStation's RESULTS.CSV/LIB, the FID peaks of our
integrator and the identifications of our library search are converted into
AutoLib's ``FIDPeak``/``PBMPeak`` objects. From there on the path is the
one ``gc_fid.load_determination`` takes, producing the same ``NiasSample``
the NIAS exporters and reports consume.
"""
from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from gcws.core.model import FID

#: quality given to a name the analyst entered by hand, so AutoLib's
#: ``display_identification`` accepts it like a library hit above the limit
MANUAL_QUALITY = 100


def engine():
    import gc_fid
    return gc_fid.engine()


def make_settings(values: dict | None = None):
    """AutoLib ``Settings`` (with the NIAS extras) from a plain dict."""
    import gc_fid
    s = gc_fid.default_settings()
    for key, value in (values or {}).items():
        if hasattr(s, key) or key in gc_fid.PARAMETER_BOUNDS:
            try:
                setattr(s, key, type(getattr(s, key, value))(value) if value is not None else value)
            except (TypeError, ValueError):
                setattr(s, key, value)
    return s


def settings_dict(s) -> dict:
    import gc_fid
    return gc_fid.settings_as_dict(s)


@dataclass
class Determination:
    """Our peaks of one FID run, converted for AutoLib."""
    run_id: str
    fid: list                        # engine FIDPeak, peak = index + 1
    pbm: dict                        # peak number -> engine PBMPeak (only peaks with MS/ident)
    delay: float
    idents: dict                     # peak number -> Identification
    peaks: dict                      # peak number -> our Peak
    index: dict                      # peak number -> index in the result
    spectra_scans: dict = field(default_factory=dict)
    key: str = FID                   # signal the peaks come from (FID or TIC)


def convert(st, key: str = FID) -> Optional[Determination]:
    """FID (or TIC) peaks + identifications of one run as AutoLib objects.

    The engine works in FID time (ISTD target RTs, bindings, solvent end), so TIC peaks enter
    it shifted by the FID-MS delay (FID time = MS time + delay)."""
    from gcws.core.keys import is_fid
    res = st.results.get(key)
    if res is None:
        return None
    eng = engine()
    idents, _ = st.ident_set(key).bind(res.peaks)
    delay = st.delay_value
    fid_axis = is_fid(key)
    shift = 0.0 if fid_axis else delay
    ms_start = float(st.run.ms.rt[0]) if st.run.ms is not None and st.run.ms.n_scans else None
    ms_end = float(st.run.ms.rt[-1]) if st.run.ms is not None and st.run.ms.n_scans else None
    fid, pbm, id_map, peaks, index = [], {}, {}, {}, {}
    for i, p in enumerate(res.peaks):
        if p.negative:
            continue
        n = i + 1
        fid.append(eng.FIDPeak(peak=n, rt=float(p.apex_rt) + shift, start=float(p.start) + shift,
                               end=float(p.end) + shift, pk_type=(p.type_start + p.type_end),
                               height=float(p.height), area=float(p.area)))
        peaks[n] = p
        index[n] = i
        ident = idents.get(i)
        component = p.extra.get("deconv_component")
        ms_rt = component["rt"] if component else (p.apex_rt - delay if fid_axis else p.apex_rt)
        has_ms = (not fid_axis) or (ms_start is not None and ms_start <= ms_rt <= ms_end)
        if ident is not None:
            id_map[n] = ident
        if ident is not None and ident.name:
            if ident.manual:
                hits = [eng.Hit(ident.name, "", ident.cas or "", MANUAL_QUALITY)]
            else:
                hits = [eng.Hit(str(h.get("name") or ""), str(h.get("library") or ""),
                                str(h.get("cas") or ""),
                                int(h["score"]) if h.get("score") is not None else None)
                        for h in ident.hits]
                if not hits:
                    hits = [eng.Hit(ident.name, "", ident.cas or "",
                                    int(ident.score) if ident.score is not None else None)]
            pbm[n] = eng.PBMPeak(n, float(ms_rt), float(p.area_pct), hits)
        elif has_ms:
            pbm[n] = eng.PBMPeak(n, float(ms_rt), float(p.area_pct), [])
    return Determination(st.id, fid, pbm, delay, id_map, peaks, index, key=key)


def assigned_rows(det: Determination, settings) -> tuple[list, list]:
    """AutoLib ``assign`` rows (1:1 by construction) and the FID peaks without MS."""
    rows, unmatched = [], []
    for f in det.fid:
        if f.rt <= settings.solvent_end:
            continue
        p = det.pbm.get(f.peak)
        if p is None:
            rows.append((f, None, [], "No MS match"))
            unmatched.append(f)
        else:
            rows.append((f, p, [], "Matched"))
    return rows, unmatched


def analyse(det: Determination, settings, blank_rows=None, blank_istd_rows=None) -> dict:
    """Body of ``AutoLib.analyse_determination`` without the file parsing."""
    eng = engine()
    assigned_raw, _ = assigned_rows(det, settings)
    assigned, blank_audit = eng.subtract_blank_signals(
        assigned_raw, blank_rows, blank_istd_rows, settings.blank_rt_tolerance)
    conc_lookup = {"fc17_conc": settings.fc17_conc, "bbp_conc": settings.bbp_conc,
                   "dnnp_conc": settings.dnnp_conc}
    standards, factors = [], []
    for name, rt, role, conc_attr in eng.IS_DEFS:
        f, p = eng.find_standard(assigned, rt, name)
        conc = conc_lookup.get(conc_attr) if conc_attr else None
        factor = None
        if role == "Quantification" and f and f.area and settings.cell_area_dm2 and settings.coverage:
            factor = conc * settings.is_amount / 1000 / f.area / settings.cell_area_dm2 / settings.coverage
            factors.append(factor)
        status = "Found" if f else "Not found"
        if role == "QC" and f and settings.qc_min_area > 0 and f.area < settings.qc_min_area:
            status = "Below QC minimum"
        standards.append({"name": name, "target_rt": rt, "role": role, "fid": f, "pbm": p,
                          "concentration": conc, "factor": factor, "status": status})
    mean_factor = statistics.mean(factors) if factors else None
    peaks = []
    for f, p, secondary, match_status in assigned:
        correction = blank_audit[f.peak]
        if p is not None:
            name, id_status, cas = eng.display_identification(p, settings.quality_limit)
            qual = p.hits[0].quality if p.hits else None
        else:
            name, id_status, cas, qual = None, "Unknown", "", None
        review = []
        if p is not None and id_status != "Accepted":
            review.append("unsichere Identifikation")
        if p is None:
            review.append("keine MS-Zuordnung; Prüfung")
        peaks.append({
            "rt": f.rt, "name": name, "cas": cas, "quality": qual, "area": f.area,
            "mg_dm2": f.area * mean_factor if mean_factor is not None else None,
            "mg_kg": f.area * mean_factor * settings.ov_ratio if mean_factor is not None else None,
            "id_status": id_status, "match_status": match_status, "review": "; ".join(review),
            "fid_peak": f.peak, "pbm_peak": p.peak if p is not None else None,
            "ms_rt": p.rt if p is not None else None,
            "corrected_ms_rt": p.rt + det.delay if p is not None else None,
            "secondary": secondary, "raw_area": correction.raw_area,
            "blank_area": correction.blank_area, "blank_istd_area": correction.blank_istd_area,
            "subtracted_area": correction.subtracted_area, "blank_status": correction.status,
            "protected_standard": correction.protected_standard,
        })
    return {"peaks": peaks, "standards": standards, "pbm": list(det.pbm.values()), "delay": det.delay,
            "mean_factor": mean_factor, "fid_count": len(det.fid), "pbm_count": len(det.pbm),
            "blank_audit": blank_audit,
            "blank_corrected": sum(1 for x in blank_audit.values() if x.subtracted_area)}


def reference_rows(det: Optional[Determination], settings) -> list:
    """Assigned rows of a blank run (AutoLib ``load_blank_reference`` equivalent)."""
    if det is None:
        return []
    return assigned_rows(det, settings)[0]


def merge_references(dets: list, settings) -> list:
    """Several blanks of one kind: the one-to-one matcher sees their union."""
    out = []
    for d in dets:
        out.extend(reference_rows(d, settings))
    return out


def _origin(p) -> str:
    import gc_model as M
    return {"auto": M.ORIGIN_CHEMSTATION, "split": M.ORIGIN_SPLIT, "merged": M.ORIGIN_MERGED,
            "deconvoluted": M.ORIGIN_DECONV}.get(
        p.origin, M.ORIGIN_MANUAL if "M" in p.flags else M.ORIGIN_CHEMSTATION)


def build_sample(st, det: Determination, result: dict, settings, *, label: str = "",
                 istd_defs=None, istd_options=None, cas_lookup=None, istd_bindings=None,
                 spectrum_scans=None):
    """A ``gc_fid.NiasSample`` exactly as ``load_determination`` would build it."""
    import gc_fid
    import gc_model as M
    from gcws.core.keys import is_fid
    ids = M.allocate_ids()
    rows = []
    fid_axis = is_fid(det.key)
    shift = 0.0 if fid_axis else det.delay          # bounds in FID time, like the RT
    for peak in sorted(result["peaks"], key=lambda x: x["rt"]):
        n = peak["fid_peak"]
        ours = det.peaks[n]
        ident = det.idents.get(n)
        name = peak.get("name")
        cas = peak.get("cas") or ""
        status = peak.get("id_status", "")
        if ident is not None and ident.name:
            if ident.manual:
                # the analyst's name wins; it was entered on purpose
                name, cas, status = ident.name, ident.cas or "", M.ID_ACCEPTED
            elif name is None or (str(name).startswith("unknown") and ident.name.startswith("unknown")):
                # keep our more informative "unknown (m/z 149, 57, 71)"
                name = ident.name
        if name is None:
            name = M.UNKNOWN_DISPLAY_NAME
            status = M.ID_UNKNOWN
        row = M.PeakRow(row_id=next(ids), peak_no=0, source="FID+PBM" if fid_axis else "TIC",
                        rt=float(peak["rt"]), area=peak.get("area"), name=name,
                        cas=M.clean_cas(cas) or "0", si=peak.get("quality"))
        row.height = float(ours.height)
        row.fid_start, row.fid_end = float(ours.start) + shift, float(ours.end) + shift
        row.fid_pk_ty = ours.type_code
        row.fid_baseline = M.BASELINE_DROP if ours.type_start == "V" or ours.type_end == "V" else M.BASELINE_ENDPOINT
        row.integration_origin = _origin(ours)
        row.area_pct = ours.area_pct
        if spectrum_scans and n in spectrum_scans:
            row.apex_scan, row.bg_scan = spectrum_scans[n]
            row.bounds_rule = "GCWS"
        hits = []
        if ident is not None:
            hits = [(str(h.get("name") or ""), str(h.get("cas") or ""),
                     int(h["score"]) if h.get("score") is not None else None) for h in ident.hits[:3]]
        row.alt_hits = hits[1:]
        row.derived = {
            "pbm_hits": hits,
            "mg_dm2": peak.get("mg_dm2"), "mg_kg": peak.get("mg_kg"),
            "raw_area": peak.get("raw_area"), "blank_area": peak.get("blank_area"),
            "blank_istd_area": peak.get("blank_istd_area"), "subtracted_area": peak.get("subtracted_area"),
            "blank_status": peak.get("blank_status", ""),
            "protected_standard": "Ja" if peak.get("protected_standard") else "Nein",
            "id_status": status, "match_status": peak.get("match_status", ""),
            "review": peak.get("review", ""), "fid_peak": n, "pbm_peak": peak.get("pbm_peak"),
            "ms_rt": peak.get("ms_rt"), "corrected_ms_rt": peak.get("corrected_ms_rt"),
            "secondary": peak.get("secondary") or [],
            "gcws_index": det.index[n],
            "id_source": ident.source if ident is not None else "",
        }
        if ours.extra.get("deconv_component"):
            row.derived["deconv_component"] = dict(ours.extra["deconv_component"])
            note = ours.extra.get("area_note", "Area allocated from MS deconvolution")
            row.derived["review"] = "; ".join(filter(None, [row.derived["review"], note]))
        rows.append(row)
    for number, row in enumerate(rows, 1):
        row.peak_no = number
    for row in rows:
        row.snapshot()
    meta = {
        "sample": st.run.path.name[:-2] if st.run.path.name.lower().endswith(".d") else st.run.path.name,
        "delay": det.delay, "mean_factor": result.get("mean_factor"),
        "fid_count": result.get("fid_count"), "pbm_count": result.get("pbm_count"),
        "blank_corrected": result.get("blank_corrected"), "unmatched_fid": sum(
            1 for p in result["peaks"] if p.get("pbm_peak") is None),
        "use_library": True, "standards": [],
        "engine_standards": gc_fid.standards_table(result.get("standards") or [], result.get("mean_factor")),
        "pbm_hits": gc_fid.pbm_hit_table(result.get("pbm")),
        "integrator": "GC Workspace",
    }
    sample = gc_fid.NiasSample(label or st.name, st.run.path, rows, st.run.ms_source, meta)
    sample.settings = settings
    sample.quality_limit = getattr(settings, "quality_limit", M.DEFAULT_QUALITY_LIMIT)
    sample.mean_factor = result.get("mean_factor")
    sample.istd_defs = istd_defs if istd_defs is not None else gc_fid.default_istd_defs(settings)
    sample.istd_options = istd_options if istd_options is not None else dict(gc_fid.DEFAULT_ISTD_OPTIONS)
    sample.ids = ids
    if cas_lookup:
        sample.set_cas_lookup(cas_lookup)
    sample.link_standards()
    for code, rt in (istd_bindings or {}).items():
        row = min(rows, key=lambda r: abs(r.rt - rt), default=None) if rt is not None else None
        if rt is None:
            sample.set_istd_row(code, None)
        elif row is not None and abs(row.rt - rt) <= 0.02:
            sample.set_istd_row(code, row)
    sample.recalculate()
    return sample

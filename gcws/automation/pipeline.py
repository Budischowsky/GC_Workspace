"""Processing one sample unattended: the job a watcher hands to a child process.

``run_job(spec)`` loads the sample's determinations and the blanks of its batch into a private
workspace, applies the processing method, searches the libraries, finds the ISTDs, quantifies,
writes the reports into the job folder, saves a project the analyst can open, and judges the
result with the Report² rules. Nothing is written into the raw-data folder.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from gcws.automation import rules as RU
from gcws.automation import store

log = logging.getLogger(__name__)

# result states (the journal's)
CONTROL, ACCEPTED_AUTO, FAILED, NOT_PROCESSED, RETRY = "control", "accepted_auto", "failed", "not_processed", "retry"


@dataclass
class JobResult:
    state: str
    reason: str = ""
    files: dict = field(default_factory=dict)          # report node -> {format: path}
    project: str = ""
    evidence: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def checked_state(spec: dict, state: str) -> str:
    """A report made again from the analyst's (edited) project ("rereport": Update report, Report again) is
    checked by the analyst: never accepted by itself, whatever the rules and the review step say."""
    return CONTROL if spec.get("mode") == "rereport" and state == ACCEPTED_AUTO else state


def write_result(out_dir: Path, result: JobResult) -> Path:
    return store.atomic_write_json(Path(out_dir) / "result.json", result.to_dict())


def read_result(out_dir: Path) -> Optional[dict]:
    return store.read_json(Path(out_dir) / "result.json")


# -- helpers ------------------------------------------------------------------------------------

def _pdf(docx: Path, pdf: Path, timeout: float = 180.0) -> tuple[Optional[Path], str]:
    """Word -> PDF in a thread, so a hanging Word cannot stop the job."""
    from gcws.report import service as RS
    box: dict = {}

    def work():
        try:
            box["pdf"] = RS.docx_to_pdf(docx, pdf)
        except Exception as exc:  # noqa: BLE001
            box["error"] = str(exc) or type(exc).__name__

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, f"PDF not written: Word did not answer within {timeout:.0f} s"
    if "error" in box:
        return None, f"PDF not written: {box['error']}"
    return box.get("pdf"), ""


def _slim_rows(rows: list, cas_info: dict) -> list[dict]:
    """The merged rows of a report with their SML (for the rules and Report²)."""
    out = []
    for r in rows or []:
        d = {k: r.get(k) for k in ("rt", "name", "cas", "mean", "c1", "c2", "reldiff", "status", "id_status",
                                   "review", "n")}
        sml = None
        if cas_info and d.get("cas"):
            from gcws.report.legacy_api import main_script
            sml = (main_script().cas_lookup_match(cas_info, d["cas"]) or {}).get("sml")
        try:
            d["sml"] = float(sml) if sml not in (None, "") else None
        except (TypeError, ValueError):
            d["sml"] = None
        out.append(d)
    return out


def _standards(sample) -> list[dict]:
    keep = ("code", "name", "role", "status", "fid_rt", "fid_area", "factor", "deviation", "target_rt")
    out = []
    for s in getattr(sample, "standards", None) or []:
        d = {k: s.get(k) for k in keep}
        if getattr(sample, "mode", "") == "hs_screening":         # HS: TIC standards, "Active" instead of a role
            d.update(fid_rt=s.get("rt"), fid_area=s.get("area"),
                     role=d["role"] or ("Quantification" if s.get("quantify", True) else "QC"))
        out.append(d)
    return out


def _paths(folder: Path, names: list[str]) -> list[Path]:
    return [folder / n for n in names]


# -- the job -----------------------------------------------------------------------------------

def run_job(spec: dict, *, progress: Callable[[str], None] = log.info,
            identify: Optional[Callable] = None, inspect: Optional[Callable] = None) -> JobResult:
    """Process one sample (see the module text). ``identify(ws, run_ids)`` replaces the library search
    (tests); ``inspect(ws, run_ids)`` returns extra evidence from the processed workspace (gcws.learn),
    kept as ``evidence["inspect"]``; ``spec`` is written by the watcher (``spec.json`` in the job folder)."""
    t0 = time.time()
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    timings: dict = {}
    warnings: list = []
    detection: dict = {}
    from gcws.automation import headless as H
    from gcws.core import proc_method as PM
    from gcws.core import project as P
    ws = H.new_workspace()
    batch = Path(spec["batch_folder"])
    group = spec["group"]
    mode = spec.get("mode", "full")
    if mode == "rereport":
        progress("opening the analyst's project")
        notes = H.open_project(ws, spec["project_path"])
        warnings += [n for n in notes if "raw data not found" in n]
        ws.log("Automation: report again from the edited project", "", spec["project_path"])
        members = [st.id for st in ws.states() if st.run.path.name in group["members"]]
    else:
        method = spec.get("method") or {}
        PM.apply_to_workspace(ws, method, persist=False)
        ws.log("Automation: processing method", "", method.get("name", ""))
        blanks = spec.get("blanks") or {}
        names = list(dict.fromkeys(group["members"] + blanks.get("blank", []) + blanks.get("blank_istd", [])))
        if spec.get("source_folder"):                  # processed from the local copy
            from gcws.automation import localcopy as LC
            LC.ensure_runs(spec["source_folder"], batch, names, progress)
        progress(f"loading {len(names)} runs")
        errors = H.add_runs(ws, _paths(batch, names), progress)
        timings["load"] = round(time.time() - t0, 1)
        by_name = {st.run.path.name.casefold(): st for st in ws.states()}
        missing = [m for m in group["members"] if m.casefold() not in by_name]
        if missing:
            return JobResult(RETRY, "could not read " + ", ".join(missing) + (": " + "; ".join(errors) if errors
                                                                              else ""), timings=timings)
        members = [by_name[m.casefold()].id for m in group["members"]]
        # blanks: from the same batch folder, else not processed (unless the analyst allowed it)
        require = spec.get("require_blank", "auto")
        allow = bool((spec.get("override") or {}).get("allow_no_blank"))
        checks = {rid: ws.blank_readiness(ws.runs[rid], require) for rid in members}
        bad = {rid: c for rid, c in checks.items() if not c.ok}
        if bad and not allow:
            text = "; ".join(f"{ws.runs[r].name}: {c.text}" for r, c in bad.items())
            return JobResult(NOT_PROCESSED, text, timings=timings)
        # library search
        if spec.get("search", True):
            t1 = time.time()
            try:
                if identify is not None:
                    identify(ws, members)
                else:
                    cfg = PM.search_config(method, ws.runs[members[0]].run.meta.method if ws.runs[members[0]].run.meta
                                           else "")
                    H.identify(ws, members, cfg, progress, timeout=float(spec.get("search_timeout", 1800)))
            except Exception as exc:  # noqa: BLE001 - reported as a finding
                warnings.append(f"Library search failed: {exc}")
            timings["search"] = round(time.time() - t1, 1)
        # automatic ISTD detection
        if spec.get("istd_detect", True):
            from gcws.quant import istd_detect as ID
            minimum = spec.get("min_confidence", "high")
            found = {}
            for rid in members:
                try:
                    res = ID.detect(ws, rid)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"ISTD detection failed for {ws.runs[rid].name}: {exc}")
                    continue
                ok = res.confident(minimum)
                found[rid] = {c: k.rt for c, k in ok.items()}
                detection[rid] = {c: {"rt": k.rt if k else None, "confidence": k.confidence if k else "none",
                                      "applied": c in ok, "text": "; ".join(k.evidence) if k else "not found"}
                                  for c, k in res.best.items()}
            if any(found.values()):
                ws.push_quant("Automation: ISTDs detected", ID.with_bindings(ws, found), "ISTD bindings")
    ws.replicate_groups = [{"id": group.get("key") or "g", "name": group["name"], "members": members,
                            "policy": group.get("policy", "all")}]
    dd: dict = {"members": [ws.runs[m].name for m in members], "planned": list(group.get("members") or []),
                "pairing": "single" if len(members) < 2 else ""}
    ws.recompute_quant()
    # the feature double determination: gap fills and one name per substance, as the analyst's Compare
    if len(members) >= 2 and mode != "rereport":
        t1 = time.time()
        try:
            dd = run_features(ws, members)
            if dd.get("note"):
                warnings.append(dd["note"])
        except Exception as exc:  # noqa: BLE001 - reported as a finding, the reports are still made
            warnings.append(f"Double determination (features) failed: {exc}")
            dd = {"members": [ws.runs[m].name for m in members], "pairing": "failed", "error": str(exc)}
        timings["features"] = round(time.time() - t1, 1)
    if detection:
        from gcws.quant import istd_detect as ID
        for rid, codes in detection.items():
            for code, d in codes.items():
                try:
                    d["used_rt"] = ID.bound_rt(ws, rid, code)
                except Exception:  # noqa: BLE001
                    d["used_rt"] = None
    # reports
    from gcws.report import assemble as AS
    from gcws.report import service as RS
    files: dict = {}
    errors_ev: list = []
    rows, summary = [], {}
    sample_warn = []
    reported: dict = {}
    names = [ws.runs[m].name for m in members]
    stem = RS.report_stem(names)
    try:
        from gcws.quant.service import cas_lookup
        cas_info = cas_lookup()
    except Exception:  # noqa: BLE001
        cas_info = {}
    for rep in spec.get("reports") or []:
        kind = rep["kind"]
        t1 = time.time()
        g = ws.replicate_groups[0]
        target = out_dir / store.safe_name(rep["node"]) / f"{stem}{RS.suffix(kind, ws.quant)}.xlsx"
        try:
            mem, samples = AS.prepare(ws, kind, g)
            job = AS.build_job(ws, kind, g, target, members=mem, samples=samples, record_seen=False,
                               keep_middle=bool(rep.get("keep_middle")),
                               batch_workbook="dd" in (rep.get("formats") or []),
                               method_name=(spec.get("method") or {}).get("name", ""))
            res = RS.generate(job, progress)
        except AS.ReportNotPossible as exc:
            errors_ev.append(f"{RS.KINDS[kind]} not made: {exc.message}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors_ev.append(f"{RS.KINDS[kind]} failed: {exc}")
            log.error(traceback.format_exc())
            continue
        produced = {"xlsx": str(res.target)}
        if res.word is not None:
            produced["docx"] = str(res.word)
        if res.batch is not None:
            produced["dd"] = str(res.batch)
        if "pdf" in (rep.get("formats") or []) and res.word is not None:
            pdf, err = _pdf(res.word, res.word.with_suffix(".pdf"), float(spec.get("pdf_timeout", 180)))
            if pdf is not None:
                produced["pdf"] = str(pdf)
            else:
                res.warnings.append(err)
        wanted = set(rep.get("formats") or []) | {"xlsx"}          # the Excel report is always kept
        files[rep["node"]] = {f: p for f, p in produced.items() if f in wanted}
        blank_notes = set(AS.blank_warnings(ws, mem))        # the "no blank" rule reports these
        sample_warn += [w for w in res.warnings if w not in blank_notes]
        reported[rep["node"]] = {"kind": kind, "rows": res.reported, "target": str(res.target),
                                 "sample_key": job.sample_key}
        if not rows:
            rows = _slim_rows(getattr(res, "combined", []) or [], cas_info)
            summary = getattr(res, "summary", {}) or {}
        timings[f"report {kind}"] = round(time.time() - t1, 1)
    # project for the analyst
    project = Path(spec.get("project_out") or (out_dir / f"{stem}.gcws"))
    ws.automation = {"job_id": spec.get("job_id", ""), "revision": spec.get("revision", 1),
                     "workflow_id": spec.get("workflow_id", ""), "sample": group["name"]}
    ws.log("Automation: processed", ", ".join(names), f"job {spec.get('job_id', '')} revision "
           f"{spec.get('revision', 1)}")
    try:
        P.save(ws, project)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Project not saved: {exc}")
        project = None
    inspected = None
    if inspect is not None:
        try:
            inspected = _json_safe(inspect(ws, members))
        except Exception as exc:  # noqa: BLE001 - the job's result stands without the extra evidence
            warnings.append(f"Learning evidence failed: {exc}")
    # evidence and the Report² rules
    evidence = evidence_for(ws, members, kind=(spec.get("reports") or [{}])[0].get("kind"),
                            require=spec.get("require_blank", "auto"), detection=detection)
    evidence.update(rows=rows, summary=summary, warnings=warnings + sample_warn, errors=errors_ev,
                    reported=reported, double_determination=dd)
    if inspect is not None:
        evidence["inspect"] = inspected
    if spec.get("has_review", True):
        ev = RU.evaluate(RU.from_list(spec.get("rules")) if spec.get("rules") is not None else RU.default_rules(),
                         evidence, bool(spec.get("auto_accept", True)))
        state, findings = ev.status, ev.to_list()
    else:
        state, findings = ACCEPTED_AUTO, []
    state = checked_state(spec, state)
    if not files and spec.get("reports"):
        state = CONTROL if state == ACCEPTED_AUTO else state
        if not any(f["rule"] == "processing_warnings" for f in findings):
            findings.append({"rule": "processing_warnings", "level": "control", "text": "no report was made",
                             "member": "", "substance": "", "cas": "", "rt": None, "value": None})
    timings["total"] = round(time.time() - t0, 1)
    return JobResult(state, "", files, str(project) if project else "", _json_safe(evidence), findings,
                     warnings + sample_warn + errors_ev, timings)


def run_features(ws, members: list) -> dict:
    """The double determination of an unattended job: pair the determinations by features, make
    the automatic changes (gap fills, names) and harmonise the boundaries (which the panel only
    proposes), then compare. Returns what was done for Report² (``note``: a line for the job)."""
    from gcws.features import service as SV
    from gcws.features.model import PAIRING_FEATURES
    names = [ws.runs[m].name for m in members if m in ws.runs]
    out = {"members": names, "pairing": SV.pairing(ws)}
    if SV.pairing(ws) != PAIRING_FEATURES or (ws.quant or {}).get("mode") == "hs_screening":
        return out
    stack = ws.project_undo
    before = stack.count()
    table = SV.run(ws, members, stack=stack, apply_boundaries=True)
    if stack.count() != before:
        ws.recompute_quant()
    applied = getattr(table, "applied", {}) or {}
    gap = sum(1 for f in table.features for m in f.members if m.origin == "gapfill")
    counts = table.counts()
    out.update(features=len(table.features), gap_fills=gap, splits=applied.get("split", 0),
               names=applied.get("identity", 0),
               boundaries=applied.get("boundary", 0), lights=counts, notes=list(table.notes))
    text = (f"{' + '.join(names)}: {len(table.features)} features, "
            + (f"{applied.get('split', 0)} split(s) carried over, " if applied.get("split") else "")
            + f"{gap} gap fill(s), "
            f"{applied.get('identity', 0)} name(s), {applied.get('boundary', 0)} boundaries harmonised; "
            f"{counts.get('red', 0)} red, {counts.get('yellow', 0)} yellow")
    ws.log("Automation: double determination (features)", " / ".join(names), text + "; " + "; ".join(table.notes))
    out["text"] = text
    out["note"] = next((n for n in table.notes if n.startswith("consensus")), "")
    return out


def feature_evidence(ws, members: list) -> list[dict]:
    """The feature rows of the determinations (all of them, also those not reported) for Report²."""
    from gcws.quant import duplicate_view as DV
    if len(members) < 2 or DV.features_table(ws, members) is None:
        return []
    rows, _problems = DV.compute(ws, members, "all")
    out = []
    for r in rows:
        out.append({"feature_id": r.get("feature_id"), "rt": r.get("rt"), "name": r.get("name"),
                    "cas": r.get("cas"), "light": r.get("light"), "verdict": r.get("verdict"),
                    "reasons": "; ".join(r.get("reasons") or []), "c1": r.get("c1"), "c2": r.get("c2"),
                    "mean": r.get("mean"), "status": r.get("status")})
    return out


def evidence_for(ws, members: list, *, kind: str = "nias", require: str = "auto",
                 detection: Optional[dict] = None) -> dict:
    """What the Report² rules look at for the determinations ``members`` of one sample (without the
    report's rows, summary and warnings, which the caller adds)."""
    from gcws.quant.nias_bridge import make_settings
    s = make_settings((ws.quant or {}).get("settings"))
    out = []
    for rid in members:
        st = ws.runs[rid]
        sample = ws.nias_sample(rid)
        chk = ws.blank_readiness(st, require)
        out.append({
            "name": st.name, "standards": _standards(sample), "mean_factor": getattr(sample, "mean_factor", None),
            "quantified": sample is not None,
            "quant_error": (ws.quant_result.errors.get(rid) if ws.quant_result else "") or "",
            "blank_ok": chk.ok, "blank_text": chk.text, "blanks": chk.assigned,
            "istd_detection": (detection or {}).get(rid, {}),
            "deconvolution": _safe(deconvolution_evidence, ws, rid, sample)})
    try:
        features = feature_evidence(ws, members)
    except Exception:  # noqa: BLE001 - the other rules still work
        features = []
    return {"kind": kind, "mode": (ws.quant or {}).get("mode", "nias_mgkg"), "members": out, "rows": [],
            "summary": {}, "warnings": [], "errors": [], "features": features,
            "settings": {"reporting_limit": getattr(s, "reporting_limit", 0.01),
                         "duplicate_max_reldiff": getattr(s, "duplicate_max_reldiff", 30.0)}}


def _safe(fn, *args) -> dict:
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 - the other evidence still counts
        log.error(traceback.format_exc())
        return {}


def deconvolution_evidence(ws, run_id: str, sample=None) -> dict:
    """What the automatic deconvolution split did in ``run_id`` (quantification signal): per split
    parent its time, area basis, fragment names, the highest fragment concentration and a split
    internal standard."""
    from gcws.ms.assignment import fragment_id
    from gcws.quant.service import quant_detector
    st = ws.runs.get(run_id)
    key = quant_detector(ws.quant)
    plan = (getattr(st, "auto_split", None) or {}).get(key) if st is not None else None
    res = ws.result(run_id, key) if st is not None else None
    if plan is None or res is None or not plan.events:
        return {}
    idents, _ = st.ident_set(key).bind(res.peaks)
    conc = {}
    for row in getattr(sample, "rows", None) or []:
        d = getattr(row, "derived", None) or {}
        if d.get("gcws_index") is not None and d.get("mg_kg") is not None:
            conc[d["gcws_index"]] = d["mg_kg"]
    standards = [(s.get("code") or s.get("name"), float(s["fid_rt"]))
                 for s in getattr(sample, "standards", None) or [] if s.get("fid_rt")]
    splits, fragments = [], 0
    for event, p in zip(plan.events, plan.plans):
        parts = [i for i, pk in enumerate(res.peaks) if fragment_id(pk).startswith(event.uid + ":")]
        fragments += len(parts)
        entry = {"rt": p.peak.apex_rt, "basis": p.basis, "note": p.basis_note, "fragments": len(parts),
                 "names": " / ".join((idents[i].name if i in idents and idents[i].name else "?") for i in parts),
                 "max_conc": max((conc[i] for i in parts if conc.get(i) is not None), default=None)}
        for i in parts:
            pk = res.peaks[i]
            code = next((c for c, rt in standards if abs(rt - pk.apex_rt) <= 0.02), None)
            if code:
                entry["istd"], entry["istd_share"] = code, pk.extra["deconv_component"].get("weight")
        splits.append(entry)
    return {"splits": splits, "fragments": fragments}


def slim_rows(result, cas_info: Optional[dict] = None) -> list[dict]:
    """The merged rows of a report result with their SML (for the rules)."""
    if cas_info is None:
        try:
            from gcws.quant.service import cas_lookup
            cas_info = cas_lookup()
        except Exception:  # noqa: BLE001
            cas_info = {}
    return _slim_rows(getattr(result, "combined", []) or [], cas_info)


def _json_safe(obj):
    return json.loads(json.dumps(obj, default=str))

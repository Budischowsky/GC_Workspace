"""A report job from the workspace: the checks and the ``ReportJob`` of a replicate group.

Shared by the Report menu (which shows :class:`ReportNotPossible` as a message and asks for
the file name) and by unattended processing (which records it as a finding).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from gcws.report import service as RS


class ReportNotPossible(Exception):
    """The report cannot be made; ``message`` says why (shown to the analyst as it is)."""

    def __init__(self, code: str, message: str, level: str = "warning"):
        super().__init__(message)
        self.code, self.message, self.level = code, message, level


def cas_path() -> Optional[Path]:
    """CASINFO.xlsx of the preferences, resolved like the NIAS main script does it."""
    from gcws import paths
    from gcws.ui.dialogs.preferences import load_settings
    raw = Path(load_settings().get("standard_cas_path", "CASINFO.xlsx"))
    if raw.is_absolute():
        return raw if raw.exists() else None
    return next((b / raw for b in (paths.RESOURCES, paths.ROOT, paths.DATA) if (b / raw).exists()), None)


def prepare(ws, kind: str, group: Optional[dict]) -> tuple[list[str], list]:
    """``(members, samples)`` of ``group`` for a ``kind`` report, or :class:`ReportNotPossible`.

    Recomputes the quantification first."""
    hs = ws.quant.get("mode") == "hs_screening"
    if not RS.kind_fits(kind, ws.quant):
        raise ReportNotPossible("mode", "Select HS-Screening mode and its report together, and Extraction (quant "
                                "method) with the Quantification report. For other reports, select the "
                                "corresponding quantification mode.", "information")
    if group is None or not group.get("members"):
        raise ReportNotPossible("no_group", "Choose a replicate group (Replicates panel) or activate a "
                                "sample chromatogram.", "information")
    ws.recompute_quant()
    members = [m for m in group["members"] if m in ws.runs]
    samples = [ws.nias_sample(m) for m in members]
    if hs:
        errors = [ws.quant_result.errors[m] for m in members if m in ws.quant_result.errors]
        if errors:
            raise ReportNotPossible("hs_errors", "\n".join(errors))
    if not samples or any(s is None for s in samples):
        from gcws.quant.service import quant_detector
        errs = [ws.quant_result.errors.get(m, "") for m in members]
        raise ReportNotPossible("no_samples", "Every determination needs role Sample and an "
                                + quant_detector(ws.quant) + " integration.\n" + "\n".join(e for e in errs if e))
    if kind == "quant":
        problems = []
        for m, s in zip(members, samples):
            info = (s.meta or {}).get("extraction") or {}
            why = info.get("problem") or info.get("missing")
            if why or not info.get("factor"):
                problems.append(f"{ws.runs[m].name}: {why or 'no standard factor'}")
        if problems:
            raise ReportNotPossible("no_factor", "The Quantification report needs a standard factor and the "
                                    "sample amount of every determination:\n" + "\n".join(problems))
    if kind == "nias" and not any(s.mean_factor for s in samples):
        raise ReportNotPossible("no_factor", "No ISTD factor: identify or bind the internal standards first.")
    if kind == "nias":
        cas = cas_path()
        if cas is None or not cas.exists():
            raise ReportNotPossible("no_cas", "The NIAS report needs the CAS reference CASINFO.xlsx "
                                    "(Edit > Preferences).")
        if not ws.quant.get("migration"):
            raise ReportNotPossible("no_migration", "The NIAS report needs the migration conditions "
                                    "(Quantification panel > Migration conditions...).")
    return members, samples


def blank_warnings(ws, members: list[str]) -> list[str]:
    """Determinations without a blank from their own batch folder (the report is still made)."""
    out = []
    for m in members:
        st = ws.runs.get(m)
        if st is None:
            continue
        chk = ws.blank_readiness(st)
        if not chk.ok or chk.foreign:
            out.append(f"{st.name}: {chk.text}")
    return out


def default_target(ws, kind: str, members: list[str]) -> Path:
    """``<batch folder>/<stem>_<Kind>_Report.xlsx``."""
    names = [ws.runs[m].name for m in members]
    return ws.runs[members[0]].run.path.parent / f"{RS.report_stem(names)}{RS.SUFFIXES[kind]}.xlsx"


def feature_table(ws, members: list[str], group: dict):
    """The feature table of a double (N-fold) determination when the group is paired by features
    (``ws.quant["features"]["pairing"]``); None for AutoLib's pairing or a single determination.
    Only what is already applied counts; library searches are not started here (cached only)."""
    from gcws.features import service as SV
    from gcws.features.model import PAIRING_FEATURES
    if len(members) < 2 or SV.pairing(ws) != PAIRING_FEATURES:
        return None
    return SV.build(ws, members, group, search=False)


def build_job(ws, kind: str, group: dict, target: Path, *, members: Optional[list] = None,
              samples: Optional[list] = None, preview: bool = False, keep_middle: bool = False,
              record_seen: Optional[bool] = None, batch_workbook: bool = True) -> RS.ReportJob:
    """The ``ReportJob`` of ``group`` (after :func:`prepare`) writing ``target``."""
    from gcws.quant import duplicate_view as DV
    from gcws.quant import migration as MG
    from gcws.quant.nias_bridge import make_settings
    if members is None or samples is None:
        members, samples = prepare(ws, kind, group)
    hs = kind == "hs_screening"
    target = Path(target)
    names = [ws.runs[m].name for m in members]
    stem = RS.report_stem(names)
    blank_ids = [b for m in members for b in ws.runs[m].blanks]
    blank_istd_ids = [b for m in members for b in ws.runs[m].blanks_istd]
    # every blank used by any determination of the group (display only in the report)
    bname = lambda ids: "; ".join(dict.fromkeys(str(ws.runs[i].run.path) for i in ids if i in ws.runs))
    return RS.ReportJob(
        kind=kind, samples=samples, names=names,
        settings=make_settings(ws.quant.get("hs" if hs else "settings")),
        target=target, word=target.with_suffix(".docx"), cas_path=cas_path() if kind == "nias" else None,
        migration={} if hs else MG.current(ws.quant), blank_names=(bname(blank_ids), bname(blank_istd_ids)),
        audit=[r for r in ws.audit.records if r.run in names or not r.run],
        policy=group.get("policy", "all"),
        batch_target=(target.parent / f"{stem}_Doppelbestimmung.xlsx") if kind == "nias" and batch_workbook
        else None,
        keep_middle=target.with_name(target.stem + "_intermediate.xlsx") if keep_middle and not preview else None,
        sample_key=stem, record_seen=(not preview) if record_seen is None else record_seen,
        ri_options={k: bool((ws.quant.get("ri") or {}).get(k)) for k in ("report_ri", "replace_rt")},
        edits=dict(group.get(DV.edits_key(ws.quant, ws.quant_unit())) or {}),
        notes=[] if hs and not ws.quant.get("hs", {}).get("blank_correction", True) else blank_warnings(ws, members),
        features=None if hs else feature_table(ws, members, group))

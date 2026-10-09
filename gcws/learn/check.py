"""Corpus check: parse every evaluation workbook under a training root, store one JSON record per workbook and
write a Markdown report of what was found and what could not be read. Writes only to the output folder."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from gcws.learn.corpus import CorpusEntry, scan
from gcws.learn.model import (AlkanePoint, EvalRow, Header, HumanEvaluation, IstdEntry, RawPeak, ReportRow)
from gcws.learn.workbook import parse_workbook, removed_peaks, report_is_draft


def entry_id(entry: CorpusEntry, root: Path) -> str:
    """Stable, filesystem-safe id: batch, run and workbook name plus a short hash of the relative path."""
    wb = Path(entry.workbook)
    rel = wb.relative_to(root) if wb.is_relative_to(root) else wb
    readable = f"{Path(entry.batch_dir).name}_{Path(entry.run_dir).name}_{wb.stem}"
    slug = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_.-]", "_", readable)).strip("_")[:120]
    digest = hashlib.sha1(rel.as_posix().encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def load_record(path: Path) -> tuple[CorpusEntry, HumanEvaluation]:
    """Read back one JSON record written by run_check."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    e = data["evaluation"]
    h = dict(e["header"])
    h["istd"] = [IstdEntry(**i) for i in h["istd"]]
    h["alkanes"] = [AlkanePoint(**a) for a in h["alkanes"]]
    ev = HumanEvaluation(**{**e, "header": Header(**h), "raw": [RawPeak(**p) for p in e["raw"]],
                            "pre_clean": None if e["pre_clean"] is None else [EvalRow(**r) for r in e["pre_clean"]],
                            "final": [EvalRow(**r) for r in e["final"]],
                            "report": [ReportRow(**r) for r in e["report"]]})
    return CorpusEntry(**data["entry"]), ev


def run_check(root: Path, out_dir: Path) -> dict:
    root, out_dir = Path(root), Path(out_dir)
    corpus = out_dir / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    entries = scan(root)
    templates, classes = Counter(), Counter()
    summary = {"workbooks": len(entries), "parsed": 0, "failed": 0, "templates": {}, "row_classes": {},
               "reported_rows": 0, "manual_integrations": 0, "area_formulas": 0, "removed_peaks": 0,
               "analyst_pairs": [], "no_blank": [], "problems": {}}
    per_run = Counter(e.run_dir for e in entries)
    for entry in entries:
        eid = entry_id(entry, root)
        ev = parse_workbook(Path(entry.workbook))
        if any(p.startswith("cannot open") for p in ev.problems):
            summary["failed"] += 1
        else:
            summary["parsed"] += 1
            templates[ev.template] += 1
            classes.update(r.row_class for r in ev.final)
            summary["reported_rows"] += len(ev.report)
            summary["manual_integrations"] += sum(p.manual for p in ev.raw if p.signal == "FID")
            summary["area_formulas"] += sum(bool(r.area_formula) for r in ev.final)
            summary["removed_peaks"] += len(removed_peaks(ev))
        problems = entry.problems + ev.problems
        if report_is_draft(ev):
            unnamed = sum(1 for r in ev.report if not r.label)
            problems = problems + [f"client report looks like a draft ({unnamed} of {len(ev.report)} lines without a name)"]
        if problems:
            summary["problems"][eid] = problems
        if not entry.blanks:
            summary["no_blank"].append(entry.run_dir)
        record = {"id": eid, "entry": asdict(entry), "evaluation": asdict(ev)}
        (corpus / f"{eid}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    summary["templates"] = dict(sorted(templates.items()))
    summary["row_classes"] = dict(sorted(classes.items()))
    summary["analyst_pairs"] = sorted(run for run, n in per_run.items() if n >= 2)
    summary["no_blank"] = sorted(set(summary["no_blank"]))
    (out_dir / "corpus_check.md").write_text(_markdown(root, summary), encoding="utf-8")
    return summary


def _markdown(root: Path, s: dict) -> str:
    def rel(p: str) -> str:
        path = Path(p)
        return path.relative_to(root).as_posix() if path.is_relative_to(root) else p

    lines = [f"# Corpus check: {root}", "", "## Summary", "", "| | |", "|---|---|"]
    for key in ("workbooks", "parsed", "failed", "reported_rows", "manual_integrations", "area_formulas",
                "removed_peaks"):
        lines.append(f"| {key.replace('_', ' ')} | {s[key]} |")
    lines += ["", "## Templates", ""] + [f"- {t}: {n}" for t, n in s["templates"].items()]
    lines += ["", "## Row classes (final worksheets)", ""] + [f"- {c}: {n}" for c, n in s["row_classes"].items()]
    lines += ["", "## Analyst pairs", "", "Runs evaluated by more than one analyst:", ""]
    lines += [f"- {rel(r)}" for r in s["analyst_pairs"]] or ["- none"]
    lines += ["", "## Runs without a blank in their batch", ""]
    lines += [f"- {rel(r)}" for r in s["no_blank"]] or ["- none"]
    lines += ["", "## Problems", ""]
    lines += [f"- `{eid}`: " + "; ".join(p) for eid, p in s["problems"].items()] or ["- none"]
    return "\n".join(lines) + "\n"

"""Baseline: the current program on every evaluated run, matched to the analyst's evaluation and scored
(spec §12 phase 2). Writes baseline.json and baseline.md to the output folder."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from gcws.learn.corpus import scan
from gcws.learn.match import human_items, match_run
from gcws.learn.review import run_verdicts
from gcws.learn.score import DISAGREEMENTS, score_run

#: the NIAS reporting limit (report footnote: peaks >= 10 ppb), in mg/kg food
REPORTING_LIMIT_MGKG = 0.01

SCORE_KEYS = ("total", "client_f1", "client_precision", "client_recall", "name_agreement", "conc_dev_median",
              "worksheet_agreement")


def run_baseline(root: Path, out_dir: Path, method_name: str = "NIAS", *, force: bool = False,
                 limit: Optional[int] = None, process_fn: Optional[Callable] = None,
                 progress: Callable[[str], None] = print) -> dict:
    from gcws.learn.runner import migration_from_header, process
    from gcws.learn.workbook import parse_workbook
    process_fn = process_fn or process
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = scan(root)[:limit] if limit else scan(root)
    programs: dict = {}
    runs = []
    for n, entry in enumerate(entries, 1):
        run_name = Path(entry.run_dir).name
        progress(f"[{n}/{len(entries)}] {Path(entry.batch_dir).name} / {run_name} ({entry.analyst})")
        row = {"workbook": entry.workbook, "run_dir": entry.run_dir, "batch": Path(entry.batch_dir).name,
               "run": run_name, "analyst": entry.analyst, "state": "", "reason": "", "score": None,
               "problems": list(entry.problems), "notes": []}
        ev = parse_workbook(Path(entry.workbook))
        if any(p.startswith("cannot open") for p in ev.problems) or not ev.final:
            row.update(state="failed", reason="; ".join(ev.problems) or "no worksheet rows")
            runs.append(row)
            continue
        if entry.run_dir not in programs:
            programs[entry.run_dir] = process_fn(entry, out_dir, method_name,
                                                 migration=migration_from_header(ev.header), force=force)
        prog = programs[entry.run_dir]
        row.update(state=prog.state, reason=prog.reason)
        if prog.state != "failed":
            items = human_items(ev)
            score = score_run(items, prog, match_run(ev, prog, items=items),
                              verdicts=run_verdicts(out_dir, row["batch"], run_name, entry.analyst))
            if not ev.report and nothing_above_limit(ev):
                row["notes"].append("nothing above the reporting limit")
            elif not ev.report:     # client report not made in this workbook: no client scores
                score.client_f1 = score.client_precision = score.client_recall = score.total = None
                score.name_agreement = None
                for k in ("missing_peak", "not_reported", "extra_reported", "name_differs"):
                    score.disagreements[k] = 0
                row["notes"].append("no client report in the workbook")
            row["score"] = asdict(score)
        runs.append(row)
    result = {"method": method_name, "root": str(root), "runs": runs, "aggregate": _aggregate(runs)}
    (out_dir / "baseline.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "baseline.md").write_text(_markdown(result), encoding="utf-8")
    return result


def nothing_above_limit(ev, limit_mgkg: float = REPORTING_LIMIT_MGKG) -> bool:
    """An empty client report means "nothing above the reporting limit" when the worksheet was quantified
    (ISTD areas present, a mg/kg column with values) and no kept peak reaches the limit; ISTDs and peaks the
    analyst marked as blank/background do not count. Otherwise the report was simply not made."""
    unit = next((u for u in ev.header.conc_units if "mg/kg" in u.casefold()), None)
    if unit is None or any(p.startswith("ISTD area missing") for p in ev.problems):
        return False
    values = [r.conc.get(unit) for r in ev.final
              if r.rt is not None and r.row_class not in ("istd", "background", "sum")]
    values = [v for v in values if v is not None]
    return bool(values) and any(v > 0 for v in values) and max(values) < limit_mgkg


def _stats(values: list) -> dict:
    vals = [v for v in values if v is not None]
    return {"mean": statistics.mean(vals) if vals else None, "median": statistics.median(vals) if vals else None}


def _aggregate(runs: list[dict]) -> dict:
    scored = [r for r in runs if r["score"] is not None]
    agg: dict = {"scored": len(scored), "failed": sum(r["state"] == "failed" for r in runs), "mean": {}, "median": {},
                 "no_client_report": sum("no client report in the workbook" in r.get("notes", []) for r in runs)}
    for k in SCORE_KEYS:
        st = _stats([r["score"][k] for r in scored])
        agg["mean"][k], agg["median"][k] = st["mean"], st["median"]
    totals = Counter()
    for r in scored:
        totals.update(r["score"]["disagreements"])
    agg["disagreements"] = {k: totals.get(k, 0) for k in DISAGREEMENTS}
    batches = defaultdict(list)
    for r in scored:
        batches[r["batch"]].append(r)
    agg["per_batch"] = {b: {"runs": len(rs), **{k: _stats([r["score"][k] for r in rs])["mean"]
                                                for k in ("total", "client_f1", "worksheet_agreement")}}
                        for b, rs in sorted(batches.items())}
    return agg


def _fmt(v) -> str:
    return "–" if v is None else f"{v:.2f}"


def _markdown(result: dict) -> str:
    agg = result["aggregate"]
    lines = [f"# Baseline: method {result['method']}", "", f"Training root: `{result['root']}`", "", "## Summary", "",
             f"{agg['scored']} runs scored, {agg['failed']} failed, {agg['no_client_report']} without a client "
             "report in the workbook (client scores left out).", "", "| score | mean | median |", "|---|---|---|"]
    lines += [f"| {k} | {_fmt(agg['mean'][k])} | {_fmt(agg['median'][k])} |" for k in SCORE_KEYS]
    lines += ["", "## Per batch", "", "| batch | runs | total | client F1 | worksheet |", "|---|---|---|---|---|"]
    lines += [f"| {b} | {v['runs']} | {_fmt(v['total'])} | {_fmt(v['client_f1'])} | {_fmt(v['worksheet_agreement'])} |"
              for b, v in agg["per_batch"].items()]
    lines += ["", "## Disagreements", "", "| type | count |", "|---|---|"]
    lines += [f"| {k.replace('_', ' ')} | {n} |" for k, n in agg["disagreements"].items()]
    lines += ["", "## Runs", "", "| batch | run | analyst | state | total | client F1 | names | conc dev | worksheet | notes |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in result["runs"]:
        s = r["score"] or {}
        lines.append(f"| {r['batch']} | {r['run']} | {r['analyst']} | {r['state']} | {_fmt(s.get('total'))} | "
                     f"{_fmt(s.get('client_f1'))} | {_fmt(s.get('name_agreement'))} | {_fmt(s.get('conc_dev_median'))} | "
                     f"{_fmt(s.get('worksheet_agreement'))} | {'; '.join(r.get('notes', []) + r.get('problems', []))} |")
    failed = [r for r in result["runs"] if r["state"] == "failed"]
    if failed:
        lines += ["", "## Failed runs", ""] + [f"- {r['batch']} / {r['run']}: {r['reason']}" for r in failed]
    return "\n".join(lines) + "\n"

"""Fit proposals: run a fit on the training corpus and write what it proposes for the user to review.
A proposal never changes the processing method; accepting it is a separate step."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from gcws.learn.fit import FitResult, fit

TARGETS = ("detection", "background", "report")


def _v(x) -> str:
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:g}"
    return str(x)


def _s(x) -> str:
    return "–" if x is None else f"{x:.3f}"


def write_proposal(result: FitResult, out_dir: Path, *, method_name: str, extra_notes=()) -> Path:
    folder = Path(out_dir) / "proposals"
    folder.mkdir(parents=True, exist_ok=True)
    data = {"method": method_name, **asdict(result), "extra_notes": list(extra_notes)}
    (folder / f"{result.target}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# Proposal: {result.target} (method {method_name})", "",
             "Nothing was applied: this is a proposal. Accepting it is a separate, explicit step.", "",
             "## Proposal", "", "| parameter | current | proposed |", "|---|---|---|"]
    lines += [f"| {k} | {_v(result.current.get(k))} | {_v(result.best.get(k))} |"
              for k in dict.fromkeys([*result.current, *result.best])]
    lines += ["", f"Accept recommended: {'yes' if result.accept_recommended else 'no'}", "",
              "## Scores", "", "| | current | proposed |", "|---|---|---|",
              f"| cross-validation | {_s(result.cv_current)} | {_s(result.cv_best)} |",
              f"| locked test batches | {_s(result.test_current)} | {_s(result.test_best)} |"]
    notes = list(result.notes) + list(extra_notes)
    if notes:
        lines += ["", "Notes:", ""] + [f"- {n}" for n in notes]
    lines += ["", "## Per batch", "", "| batch | test set | current | proposed |", "|---|---|---|---|"]
    lines += [f"| {b} | {'yes' if v.get('test') else ''} | {_s(v.get('current'))} | {_s(v.get('best'))} |"
              for b, v in result.per_batch.items()]
    lines += ["", "## All candidates", "", "| parameters | cross-validation |", "|---|---|"]
    lines += [f"| {', '.join(f'{k}={_v(v)}' for k, v in row['params'].items())} | {_s(row['cv'])} |"
              for row in sorted(result.table, key=lambda r: -(r["cv"] if r["cv"] is not None else -1))]
    md = folder / f"{result.target}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md


def _mean(values) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def load_cached_runs(root: Path, out_dir: Path, method_name: str, process_fn=None, progress=print) -> dict:
    """batch -> [CachedRun]: every evaluated run with its (cached) program result and pairs."""
    from gcws.learn.corpus import scan
    from gcws.learn.match import human_items, match_run
    from gcws.learn.rules import CachedRun
    from gcws.learn.runner import migration_from_header, process
    from gcws.learn.workbook import parse_workbook
    process_fn = process_fn or process
    batches: dict = {}
    programs: dict = {}
    for entry in scan(Path(root)):
        ev = parse_workbook(Path(entry.workbook))
        if not ev.final:
            continue
        if entry.run_dir not in programs:
            progress(f"program result: {Path(entry.run_dir).name}")
            programs[entry.run_dir] = process_fn(entry, out_dir, method_name,
                                                 migration=migration_from_header(ev.header))
        prog = programs[entry.run_dir]
        if prog.state == "failed":
            continue
        items = human_items(ev)
        sums = [r.label for r in ev.report if r.label.casefold().startswith("sum of")]
        run = CachedRun(Path(entry.batch_dir).name, items, prog, match_run(ev, prog, items=items),
                        footnotes=list(ev.footnotes) + sums)
        batches.setdefault(run.batch, []).append(run)
    return batches


def run_fit(target: str, root: Path, out_dir: Path, method_name: str = "NIAS", *, process_fn=None,
            progress: Callable[[str], None] = print) -> FitResult:
    if target not in TARGETS:
        raise ValueError(f"unknown fit target {target!r} (one of {', '.join(TARGETS)})")
    notes: list[str] = []
    if target == "detection":
        from gcws.core import proc_method as PM
        from gcws.learn import detection as D
        method = PM.load(method_name)
        runs, skipped = D.load_detection_runs(Path(root), Path(out_dir))
        notes += [f"not scored: {s}" for s in skipped]
        notes.append("detection is fitted on FID-only integration with deconvolution split off (current and "
                     "proposed alike)")
        fid = method["sections"]["integration"]["FID"]
        on = next((e["time"] for e in fid.get("timed_events", []) if e.get("kind") == "INTEGRATOR_ON"), None)
        current = {k: (on if k == "integrator_on" else fid.get(k)) for k in D.DETECTION_SPACE}
        batches: dict = {}
        for r in runs:
            batches.setdefault(r.batch, []).append(r)
        result = fit(target, D.DETECTION_SPACE, current, batches,
                     lambda p, rs: _mean(D.detection_score(r, D.integrate_peaks(r, method, p)) for r in rs),
                     progress=progress)
    else:
        from gcws.learn import rules as R
        batches = load_cached_runs(Path(root), Path(out_dir), method_name, process_fn, progress)
        if target == "background":
            result = fit(target, R.BACKGROUND_SPACE, {"ratio_limit": 3.0}, batches,
                         lambda p, rs: _mean(R.worksheet_agreement(r, p) for r in rs), progress=progress)
        else:
            result = fit(target, R.REPORT_SPACE, {"limit": 0.01}, batches,
                         lambda p, rs: _mean(R.report_agreement(r, p) for r in rs), progress=progress)
    write_proposal(result, out_dir, method_name=method_name, extra_notes=notes)
    return result

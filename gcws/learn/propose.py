"""Fit proposals: run a fit on the training corpus and write what it proposes for the user to review.
A proposal never changes the processing method; accepting it is a separate step."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from gcws.learn.fit import FitResult, fit

TARGETS = ("detection", "background", "report", "naming")


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
    families = result.model if isinstance(result.model, list) else None
    if families is not None:
        data["families"] = families
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
    if families is not None:
        lines += ["", "## Learned families", "",
                  "Learned from the cross-validation batches; a program peak with one of these names or class hints "
                  "is reported in the family's sum line.", "",
                  "| label | sum line | program names | class hints | analyst peaks | batches |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {f['label']} | {f['sum_text']} | {'; '.join(f['names'])} | {'; '.join(f['hints'])} | "
                  f"{f['support']} | {f['batches']} |" for f in families] or ["| (none learned) | | | | | |"]
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
    from gcws.learn.workbook import parse_workbook, report_is_draft
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
                        footnotes=list(ev.footnotes) + sums,
                        client_report=bool(ev.report) and not report_is_draft(ev))
        batches.setdefault(run.batch, []).append(run)
    return batches


def _fit_naming(root: Path, out_dir: Path, method_name: str, process_fn, progress, notes: list) -> FitResult:
    """Families 3-5: the client report simulated from the cached evidence, scored by client F1; the family
    table is learned on the training folds only."""
    import copy
    from gcws.learn.families import learn_families
    from gcws.learn.fit import fit_learned
    from gcws.learn.score import score_run
    from gcws.learn.simulate import NAMING_SPACE, peak_conc, simulate_report
    batches = load_cached_runs(root, out_dir, method_name, process_fn, progress)
    left_out = sum(1 for runs in batches.values() for r in runs if not r.client_report)
    if left_out:
        notes.append(f"{left_out} workbooks without a (non-draft) client report are not scored")
    notes.append("the client report is simulated from the cached program evidence (ISTD, background ratio 3, "
                 "limit 0.01 mg/kg, families, naming, unknowns)")

    def evaluate(params, families, runs):
        scores = []
        for r in runs:
            if not r.client_report or all(c is None for c in peak_conc(r.prog)):
                continue                           # the same runs for every candidate
            if params.get("as_today"):            # the program's real client report
                scores.append(score_run(r.items, r.prog, r.pairs).client_f1)
                continue
            lines = simulate_report(r.prog, params, families or [])
            if lines is None:
                continue
            prog = copy.copy(r.prog)
            prog.report_lines = lines
            scores.append(score_run(r.items, prog, r.pairs).client_f1)
        return _mean(scores)

    space = {**NAMING_SPACE, "family_min_share": [0.6, 0.5, 0.75]}
    current = {"as_today": True}
    result = fit_learned("naming", space, current, batches,
                         lambda p, runs: None if p.get("as_today") else learn_families(
                             [r for r in runs if r.client_report], min_share=p["family_min_share"]),
                         evaluate, progress=progress)
    if result.model is None:      # today's report won: still show the table the best simulated setting learns
        sims = [row for row in result.table if not row["params"].get("as_today") and row["cv"] is not None]
        if sims:
            top = max(sims, key=lambda row: row["cv"])
            from gcws.learn.folds import make_split
            split = make_split(list(batches))
            cv_runs = [r for b in batches if b not in split.test for r in batches[b] if r.client_report]
            result.model = learn_families(cv_runs, min_share=top["params"]["family_min_share"])
            notes.append(f"the learned families below belong to the best simulated setting {top['params']} "
                         f"(cross-validation {top['cv']:.3f}), which is not recommended")
    return result


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
    elif target == "naming":
        result = _fit_naming(Path(root), Path(out_dir), method_name, process_fn, progress, notes)
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

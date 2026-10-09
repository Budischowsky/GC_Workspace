"""gcws.learn.propose: fit proposals (files the user reviews; nothing is applied) and the fit CLI."""
import json

from test_learn_baseline import _perfect


def _result(accept=True):
    from gcws.learn.fit import FitResult
    return FitResult(target="detection", current={"area_reject": 500000.0, "min_sn": 3.0},
                     best={"area_reject": 250000.0, "min_sn": 3.0}, cv_current=0.80, cv_best=0.86,
                     test_current=0.78, test_best=0.84,
                     per_batch={"B1": {"current": 0.8, "best": 0.9, "test": False},
                                "B2": {"current": 0.7, "best": 0.75, "test": True}},
                     table=[{"params": {"area_reject": 500000.0, "min_sn": 3.0}, "cv": 0.80},
                            {"params": {"area_reject": 250000.0, "min_sn": 3.0}, "cv": 0.86}],
                     accept_recommended=accept, notes=["only 2 batches for cross-validation"])


def test_write_proposal(tmp_path):
    from gcws.learn.propose import write_proposal
    md = write_proposal(_result(), tmp_path, method_name="NIAS", extra_notes=["FID only, deconvolution off"])
    assert md == tmp_path / "proposals" / "detection.md"
    text = md.read_text(encoding="utf-8")
    for heading in ("## Proposal", "## Scores", "## Per batch", "## All candidates"):
        assert heading in text
    assert "| area_reject | 500000 | 250000 |" in text and "| min_sn | 3 | 3 |" in text
    assert "Accept recommended: yes" in text and "Nothing was applied" in text
    assert "FID only, deconvolution off" in text and "only 2 batches" in text
    saved = json.loads((tmp_path / "proposals" / "detection.json").read_text(encoding="utf-8"))
    assert saved["best"] == {"area_reject": 250000.0, "min_sn": 3.0} and saved["method"] == "NIAS"
    assert "Accept recommended: no" in write_proposal(_result(False), tmp_path, method_name="NIAS").read_text(
        encoding="utf-8")


def test_run_fit_report_on_synthetic_corpus(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.propose import run_fit
    root = tmp_path / "root"
    for b in ("B1", "B2", "B3"):
        make_workbook(root / b / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    r = run_fit("report", root, tmp_path / "out", process_fn=_perfect, progress=lambda t: None)
    # the fixture keeps three rows >= 0.01 mg/kg unreported (7.189, 20.439, 28.862): 6 of 9 agree at 0.01
    assert r.current == {"limit": 0.01} and r.cv_current == r.cv_best == 6 / 9
    assert (tmp_path / "out" / "proposals" / "report.md").is_file()


def test_run_fit_background_on_synthetic_corpus(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.propose import run_fit
    root = tmp_path / "root"
    for b in ("B1", "B2", "B3"):
        make_workbook(root / b / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    r = run_fit("background", root, tmp_path / "out", process_fn=_perfect, progress=lambda t: None)
    assert r.current == {"ratio_limit": 3.0} and r.cv_best == 1


def test_cli_fit_parses_args(tmp_path, monkeypatch):
    from gcws.learn import __main__ as cli
    from gcws.learn import propose
    got = {}

    def fake(target, root, out, method_name="NIAS", **kw):
        got.update(target=target, root=root, out=out, method_name=method_name)
        return _result()

    monkeypatch.setattr(propose, "run_fit", fake)
    monkeypatch.setattr(cli, "_ensure_app", lambda: None)
    assert cli.main(["fit", "detection", str(tmp_path), "--out", str(tmp_path / "o"), "--method", "X"]) == 0
    assert got == {"target": "detection", "root": tmp_path, "out": tmp_path / "o", "method_name": "X"}


def _named_program(entry, out_dir, method_name="NIAS", **kw):
    """A program whose peaks carry the analyst's labels as names (group rows share a class hint)."""
    from pathlib import Path
    from gcws.learn.match import human_items
    from gcws.learn.runner import ProgramResult
    from gcws.learn.workbook import parse_workbook
    items = human_items(parse_workbook(Path(entry.workbook)))
    peaks, reported = [], []
    for it in items:
        peaks.append({"rt": it.rt, "start": it.rt - 0.004, "end": it.rt + 0.004, "area": it.area or 1.0,
                      "name": it.label, "cas": it.cas, "score": 95.0, "istd": "IS1" if it.decision == "istd" else "",
                      "in_blank": "", "blank_ratio": None,
                      "class_hint": "oligomer hint" if it.decision == "reported_group" else "-"})
        if it.conc_mgkg is not None and it.area:
            reported.append({"rt": it.rt, "mean_mgkg": it.conc_mgkg, "cas": it.cas, "name": it.label})
    return ProgramResult(run_dir=entry.run_dir, method=method_name, state="control", peaks=peaks, reported=reported)


def test_run_fit_naming_learns_families(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.propose import run_fit
    root = tmp_path / "root"
    for b in ("B1", "B2", "B3", "B4", "B5"):
        make_workbook(root / b / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    r = run_fit("naming", root, tmp_path / "out", process_fn=_named_program, progress=lambda t: None)
    # "current" is the program's real client report, not a simulation
    assert r.current == {"as_today": True}
    assert r.cv_best is not None
    assert any(f["label"] == "styrene oligomer" for f in r.model)
    md = (tmp_path / "out" / "proposals" / "naming.md").read_text(encoding="utf-8")
    assert "## Learned families" in md and "styrene oligomer" in md and "oligomer hint" in md
    saved = json.loads((tmp_path / "out" / "proposals" / "naming.json").read_text(encoding="utf-8"))
    assert saved["families"] and saved["families"][0]["label"] == "styrene oligomer"


def test_cli_fit_naming_parses(tmp_path, monkeypatch):
    from gcws.learn import __main__ as cli
    from gcws.learn import propose
    got = {}
    monkeypatch.setattr(propose, "run_fit", lambda target, *a, **k: got.update(target=target) or _result())
    monkeypatch.setattr(cli, "_ensure_app", lambda: None)
    assert cli.main(["fit", "naming", str(tmp_path), "--out", str(tmp_path / "o")]) == 0
    assert got["target"] == "naming"


def test_naming_fit_skips_runs_that_cannot_be_simulated_for_every_candidate(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.propose import run_fit
    root = tmp_path / "root"
    for b in ("B1", "B2", "B3", "B4", "B5"):
        make_workbook(root / b / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    make_workbook(root / "B0" / "07_Y_A.D/Auswertung/NIAS-Screening-SYN2_BDa_ 07_Y_A.xlsx")   # a CV batch

    def no_conc_for_y(entry, out_dir, method_name="NIAS", **kw):
        prog = _named_program(entry, out_dir, method_name, **kw)
        if "07_Y_A" in entry.run_dir:
            prog.reported = [{k: v for k, v in r.items() if k != "mean_mgkg"} for r in prog.reported]
        return prog

    r = run_fit("naming", root, tmp_path / "out", process_fn=no_conc_for_y, progress=lambda t: None)
    assert all(row["cv"] is not None for row in r.table)


def test_naming_proposal_shows_families_when_today_wins(tmp_path):
    """The real report already equals the analyst's: 'as today' wins, the learned table is still shown."""
    from learn_fixtures import make_workbook
    from gcws.learn.propose import run_fit
    root = tmp_path / "root"
    for b in ("B1", "B2", "B3", "B4", "B5"):
        make_workbook(root / b / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")

    def perfect_report(entry, out_dir, method_name="NIAS", **kw):
        prog = _named_program(entry, out_dir, method_name, **kw)
        prog.report_lines = [{"rt": r["rt"], "name": r["name"], "cas": r["cas"], "kind": "line"}
                             for r in _perfect(entry, out_dir, method_name).reported]
        return prog

    r = run_fit("naming", root, tmp_path / "out", process_fn=perfect_report, progress=lambda t: None)
    assert r.best == {"as_today": True}
    md = (tmp_path / "out" / "proposals" / "naming.md").read_text(encoding="utf-8")
    assert "## Learned families" in md and "styrene oligomer" in md


def _proposal(tmp_path, families):
    p = tmp_path / "naming.json"
    p.write_text(json.dumps({"target": "naming", "families": families}), encoding="utf-8")
    return p


def test_apply_families_to_named_method_only(tmp_path, monkeypatch):
    from gcws import paths
    from gcws.core import proc_method as PM
    from gcws.learn.propose import apply_families_to_method
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    PM.save({"name": "M", "sections": {"quant": {"mode": "nias_mgkg"}}})
    PM.save({"name": "Other", "sections": {"quant": {"mode": "nias_mgkg"}}})
    fam = {"label": "hydrocarbon", "row_name": "Hydrocarbon", "sum_text": "Sum of hydrocarbons", "names": ["Hydrocarbon"],
           "hints": [], "support": 9, "batches": 2}
    apply_families_to_method(_proposal(tmp_path, [fam]), "M")
    m = PM.load("M")
    assert m["sections"]["learned_rules"]["families"] == [fam] and m["sections"]["learned_rules"]["version"] == 1
    assert m["sections"]["quant"] == {"mode": "nias_mgkg"}
    assert "learned_rules" not in PM.load("Other")["sections"]


def test_apply_families_needs_families(tmp_path, monkeypatch):
    import pytest
    from gcws import paths
    from gcws.core import proc_method as PM
    from gcws.learn.propose import apply_families_to_method
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    PM.save({"name": "M", "sections": {}})
    with pytest.raises(ValueError):
        apply_families_to_method(_proposal(tmp_path, []), "M")


def test_cli_apply_families_requires_method(tmp_path):
    import pytest
    from gcws.learn.__main__ import main
    with pytest.raises(SystemExit):
        main(["apply-families", str(_proposal(tmp_path, []))])


def test_cli_apply_families_writes_the_method(tmp_path, monkeypatch):
    from gcws import paths
    from gcws.core import proc_method as PM
    from gcws.learn.__main__ import main
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    PM.save({"name": "M (copy)", "sections": {}})
    fam = {"label": "x", "row_name": "X", "sum_text": "Sum of x", "names": ["X"], "hints": [], "support": 5, "batches": 2}
    assert main(["apply-families", str(_proposal(tmp_path, [fam])), "--method", "M (copy)"]) == 0
    assert PM.load("M (copy)")["sections"]["learned_rules"]["families"][0]["label"] == "x"

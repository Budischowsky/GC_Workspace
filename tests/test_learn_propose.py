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

"""gcws.learn.baseline: every evaluated run processed (cached), matched and scored; report files."""
import json
from pathlib import Path

import pytest


def _perfect(entry, out_dir, method_name="NIAS", *, migration=None, force=False):
    """A program that finds exactly the analyst's peaks and reports exactly the analyst's lines."""
    from gcws.learn.match import human_items
    from gcws.learn.runner import ProgramResult
    from gcws.learn.workbook import parse_workbook
    items = human_items(parse_workbook(Path(entry.workbook)))
    peaks, reported = [], []
    for it in items:
        removed = it.source == "removed" or it.decision == "background"
        peaks.append({"rt": it.rt, "start": it.rt - 0.004, "end": it.rt + 0.004, "area": it.area or 1.0,
                      "in_blank": "blank" if removed else "", "blank_ratio": 0.5 if removed else None})
        if it.decision.startswith("reported_"):
            reported.append({"rt": it.rt, "cas": it.cas, "name": it.label})
    return ProgramResult(run_dir=entry.run_dir, method=method_name, state="control", peaks=peaks, reported=reported)


@pytest.fixture
def root(tmp_path):
    from learn_fixtures import make_workbook
    root = tmp_path / "root"
    (root / "B1" / "10_EtOH.D").mkdir(parents=True)
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    make_workbook(root / "B2/07_Y_A.D/Auswertung/NIAS-Screening-SYN2_BDa_ 07_Y_A.xlsx")
    return root


def test_baseline_perfect_program(root, tmp_path):
    from gcws.learn.baseline import run_baseline
    out = tmp_path / "out"
    result = run_baseline(root, out, process_fn=_perfect, progress=lambda t: None)
    assert [r["state"] for r in result["runs"]] == ["control", "control"]
    agg = result["aggregate"]
    assert agg["scored"] == 2 and agg["failed"] == 0
    assert agg["mean"]["client_f1"] == 1 and agg["mean"]["worksheet_agreement"] == 1
    assert agg["mean"]["total"] == 1
    assert set(agg["per_batch"]) == {"B1", "B2"}
    saved = json.loads((out / "baseline.json").read_text(encoding="utf-8"))
    assert saved["method"] == "NIAS" and len(saved["runs"]) == 2
    md = (out / "baseline.md").read_text(encoding="utf-8")
    for heading in ("## Summary", "## Per batch", "## Disagreements", "## Runs"):
        assert heading in md


def test_baseline_failed_run(root, tmp_path):
    from gcws.learn.baseline import run_baseline
    from gcws.learn.runner import ProgramResult

    def half(entry, out_dir, method_name="NIAS", **kw):
        if "07_Y_A" in entry.run_dir:
            return ProgramResult(run_dir=entry.run_dir, method=method_name, state="failed", reason="no data")
        return _perfect(entry, out_dir, method_name, **kw)

    result = run_baseline(root, tmp_path / "out", process_fn=half, progress=lambda t: None)
    agg = result["aggregate"]
    assert agg["scored"] == 1 and agg["failed"] == 1
    failed = next(r for r in result["runs"] if r["state"] == "failed")
    assert failed["reason"] == "no data" and failed["score"] is None
    assert agg["mean"]["client_f1"] == 1
    assert "no data" in (tmp_path / "out" / "baseline.md").read_text(encoding="utf-8")


def test_baseline_processes_run_once(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.baseline import run_baseline
    root = tmp_path / "root"
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BlM_ 05_X_A.xlsx")
    calls = []

    def counting(entry, out_dir, method_name="NIAS", **kw):
        calls.append(entry.run_dir)
        return _perfect(entry, out_dir, method_name, **kw)

    result = run_baseline(root, tmp_path / "out", process_fn=counting, progress=lambda t: None)
    assert len(calls) == 1 and len(result["runs"]) == 2
    assert [r["analyst"] for r in result["runs"]] == ["BDa", "BlM"]


def test_baseline_passes_migration_and_limit(root, tmp_path):
    from gcws.learn.baseline import run_baseline
    seen = []

    def spy(entry, out_dir, method_name="NIAS", *, migration=None, force=False):
        seen.append((method_name, migration["simulant"], force))
        return _perfect(entry, out_dir, method_name)

    run_baseline(root, tmp_path / "out", method_name="X", force=True, limit=1, process_fn=spy, progress=lambda t: None)
    assert seen == [("X", "EtOH 95%", True)]


def test_cli_baseline_parses_args(root, tmp_path, monkeypatch):
    from gcws.learn import __main__ as cli
    from gcws.learn import baseline as BL
    got = {}

    def fake(root_, out_, method_name="NIAS", *, force=False, limit=None, **kw):
        got.update(root=root_, out=out_, method_name=method_name, force=force, limit=limit)
        return {"runs": [], "aggregate": {"scored": 0, "failed": 0, "mean": {}}}

    monkeypatch.setattr(BL, "run_baseline", fake)
    monkeypatch.setattr(cli, "_ensure_app", lambda: None)
    assert cli.main(["baseline", str(root), "--limit", "1", "--method", "X", "--out", str(tmp_path / "o"),
                     "--force"]) == 0
    assert got == {"root": root, "out": tmp_path / "o", "method_name": "X", "force": True, "limit": 1}


def test_workbook_without_client_report_is_not_scored_on_it(tmp_path):
    """An empty externerBericht means the client report was not made in that workbook (seen in second
    evaluations), not 'nothing to report': the client scores are left out, the worksheet is still scored."""
    from learn_fixtures import make_workbook
    from gcws.learn.baseline import run_baseline
    root = tmp_path / "root"
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx", report_rows=[])
    result = run_baseline(root, tmp_path / "out", process_fn=_perfect, progress=lambda t: None)
    (run,) = result["runs"]
    assert run["score"]["client_f1"] is None and run["score"]["total"] is None
    assert run["score"]["worksheet_agreement"] == 1
    assert "no client report in the workbook" in run["notes"]
    assert result["aggregate"]["no_client_report"] == 1
    assert "no client report" in (tmp_path / "out" / "baseline.md").read_text(encoding="utf-8")


def test_empty_client_report_with_nothing_above_the_limit_is_scored(tmp_path):
    """User rule: an empty client report means 'nothing above the reporting limit' when no kept worksheet peak
    (ISTDs and blank-marked peaks excluded) reaches the limit (10 ppb = 0.01 mg/kg); then the program's lines
    count as extra."""
    from learn_fixtures import make_workbook
    from gcws.learn.baseline import run_baseline
    root = tmp_path / "root"
    low = [{"A": 7.0, "F": 10, "G": 0.0001, "H": 0.009}, {"A": 14.33, "B": "IS1", "F": 100, "G": 0.02, "H": 0.12},
           {"A": 9.0, "B": "im blank", "F": 50, "G": 0.01, "H": 0.06}]
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx", report_rows=[], final_rows=low)

    def one_line(entry, out_dir, method_name="NIAS", **kw):
        prog = _perfect(entry, out_dir, method_name, **kw)
        prog.reported = [{"rt": 7.0, "cas": "", "name": "x"}]
        return prog

    (run,) = run_baseline(root, tmp_path / "out", process_fn=one_line, progress=lambda t: None)["runs"]
    assert "nothing above the reporting limit" in run["notes"]
    assert run["score"]["client_precision"] == 0 and run["score"]["disagreements"]["extra_reported"] == 1


def test_empty_client_report_with_peaks_above_the_limit_is_not_made(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.baseline import run_baseline
    root = tmp_path / "root"
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx", report_rows=[],
                  final_rows=[{"A": 7.0, "F": 10, "G": 0.002, "H": 0.011}])
    (run,) = run_baseline(root, tmp_path / "out", process_fn=_perfect, progress=lambda t: None)["runs"]
    assert "no client report in the workbook" in run["notes"] and run["score"]["client_f1"] is None


def test_empty_client_report_without_mgkg_is_not_made(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.baseline import run_baseline
    root = tmp_path / "root"
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx", report_rows=[],
                  conc_headers=("Conc. mg/13 cm",), final_rows=[{"A": 7.0, "F": 10, "G": 0.0001}])
    (run,) = run_baseline(root, tmp_path / "out", process_fn=_perfect, progress=lambda t: None)["runs"]
    assert "no client report in the workbook" in run["notes"]

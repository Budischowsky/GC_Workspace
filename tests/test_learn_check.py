"""gcws.learn.check: JSON corpus + Markdown corpus-check report, and the CLI."""
from pathlib import Path

import pytest


@pytest.fixture
def corpus_root(tmp_path):
    from learn_fixtures import make_workbook
    root = tmp_path / "root"
    batch = root / "B"
    (batch / "10_EtOH.D").mkdir(parents=True)
    make_workbook(batch / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    make_workbook(batch / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BlM_ 05_X_A.xlsx", template="v1",
                  pre_clean=False)
    bad = batch / "07_Y_A.D/Auswertung/NIAS-Screening-SYN2_BDa_ 07_Y_A.xlsm"
    bad.parent.mkdir(parents=True)
    bad.write_text("not a workbook")
    return root


def _files(root: Path) -> set:
    return {(p, p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def test_run_check_synthetic(corpus_root, tmp_path):
    from gcws.learn.check import load_record, run_check
    out = tmp_path / "out"
    s = run_check(corpus_root, out)
    assert (s["workbooks"], s["parsed"], s["failed"]) == (3, 2, 1)
    assert s["templates"] == {"v1": 1, "v2": 1}
    assert s["row_classes"]["group"] == 4 and s["row_classes"]["istd"] == 4
    assert s["manual_integrations"] == 4 and s["area_formulas"] == 2 and s["removed_peaks"] == 4
    assert s["reported_rows"] == 10
    assert len(s["analyst_pairs"]) == 1 and s["analyst_pairs"][0].endswith("05_X_A.D")
    assert s["no_blank"] == []
    assert len(s["problems"]) == 1
    records = sorted((out / "corpus").glob("*.json"))
    assert len(records) == 3
    entry, ev = load_record(next(p for p in records if "BDa_05_X_A" in p.name))
    assert entry.analyst == "BDa" and entry.blanks == ["10_EtOH.D"]
    assert ev.template == "v2" and ev.header.alkanes[1].ri == 1000
    assert ev.final[1].label == "Butyl methacrylate" and ev.raw[0].signal == "TIC"
    assert ev.report[0].conc == {"mg/dm²": 0.243, "mg/kg": 1.458}
    md = (out / "corpus_check.md").read_text(encoding="utf-8")
    for heading in ("## Summary", "## Problems", "## Analyst pairs"):
        assert heading in md
    assert "cannot open" in md


def test_entry_id_is_stable_and_unique(corpus_root):
    from gcws.learn.check import entry_id
    from gcws.learn.corpus import scan
    ids = [entry_id(e, corpus_root) for e in scan(corpus_root)]
    assert len(set(ids)) == 3
    assert ids == [entry_id(e, corpus_root) for e in scan(corpus_root)]
    assert all(c.isalnum() or c in "_-." for i in ids for c in i)


def test_cli(corpus_root, tmp_path):
    from gcws.learn.__main__ import main
    out = tmp_path / "cli"
    assert main(["check", str(corpus_root), "--out", str(out)]) == 0
    assert (out / "corpus_check.md").is_file() and len(list((out / "corpus").glob("*.json"))) == 3


def test_nothing_written_to_root(corpus_root, tmp_path):
    from gcws.learn.check import run_check
    before = _files(corpus_root)
    run_check(corpus_root, tmp_path / "out")
    assert _files(corpus_root) == before

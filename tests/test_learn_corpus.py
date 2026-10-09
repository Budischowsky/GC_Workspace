"""gcws.learn.corpus: finding evaluation workbooks, their run and the batch blanks."""
from pathlib import Path


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _batch(root: Path, *runs: str) -> Path:
    batch = root / "B"
    for r in runs:
        (batch / r).mkdir(parents=True, exist_ok=True)
    return batch


def test_find_workbooks(tmp_path):
    from gcws.learn.corpus import find_workbooks
    batch = _batch(tmp_path, "05_X_A.D")
    wb = _touch(batch / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsm")
    _touch(batch / "05_X_A.D/Auswertung/~$NIAS-Screening-SYN1_BDa_ 05_X_A.xlsm")
    _touch(batch / "05_X_A.D/AcqData/M.M/rptdef.xls")
    assert find_workbooks(tmp_path) == [wb]


def test_scan_blanks(tmp_path):
    from gcws.learn.corpus import scan
    batch = _batch(tmp_path, "05_X_A.D", "10_EtOH.D", "12_Blank_EtOAc.D", "03_EtOH_ISTD.D", "07_Y_B.D")
    _touch(batch / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsm")
    (e,) = scan(tmp_path)
    assert e.blanks == ["03_EtOH_ISTD.D", "10_EtOH.D", "12_Blank_EtOAc.D"]
    assert (e.run_role, e.analyst, e.problems) == ("sample", "BDa", [])
    assert Path(e.run_dir).name == "05_X_A.D" and Path(e.batch_dir) == batch


def test_scan_no_blank(tmp_path):
    from gcws.learn.corpus import scan
    batch = _batch(tmp_path, "05_X_A.D", "07_Y_B.D")
    _touch(batch / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsm")
    (e,) = scan(tmp_path)
    assert e.blanks == [] and e.problems == ["no blank run in batch"]


def test_two_analysts(tmp_path):
    from gcws.learn.corpus import scan
    batch = _batch(tmp_path, "05_X_A.D", "10_EtOH.D")
    _touch(batch / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsm")
    _touch(batch / "05_X_A.D/Auswertung/NIAS-Screening-SYN1_BlM_ 05_X_A.xlsm")
    entries = scan(tmp_path)
    assert [e.analyst for e in entries] == ["BDa", "BlM"]
    assert len({e.run_dir for e in entries}) == 1


def test_blank_evaluated(tmp_path):
    from gcws.learn.corpus import scan
    batch = _batch(tmp_path, "05_Blank_EtOH.D", "07_X_A.D")
    _touch(batch / "05_Blank_EtOH.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_Blank_EtOH.xlsm")
    (e,) = scan(tmp_path)
    assert e.run_role == "blank"
    assert "evaluated run is a blank" in e.problems


def test_analyst_from_name():
    from gcws.learn.corpus import analyst_from_name
    assert analyst_from_name("NIAS-Screening-SYN25011889_BlM_ 05_25011889_Zipper_A.xlsm") == "BlM"
    assert analyst_from_name("whatever.xlsm") == ""

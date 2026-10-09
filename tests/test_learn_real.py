"""gcws.learn on the real human evaluations (skipped when the training data is not on this PC)."""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEARN_ROOT = Path(os.environ.get("GCWS_LEARN_ROOT", ROOT.parent / "Testsample"))


def _gio_workbook() -> Path:
    found = sorted((LEARN_ROOT / "25011662_GIO_Diary").glob("05_*_A.D/Auswertung/NIAS-Screening-*.xlsm"))
    if not found:
        pytest.skip("GIO training workbook not available (set GCWS_LEARN_ROOT)")
    return found[0]


def test_gio_workbook():
    from gcws.learn.workbook import parse_workbook, removed_peaks
    ev = parse_workbook(_gio_workbook())
    assert ev.template == "v2" and ev.pre_clean is not None
    assert ev.header.simulant == "EtOH 95%" and ev.header.sv_ratio == 6
    bma = next(r for r in ev.final if r.rt == 7.07)
    assert (bma.label, bma.area, bma.area_original, bma.row_class) == (
        "Butyl methacrylate", 118978514, 119966715, "named")
    assert sum(r.row_class == "istd" for r in ev.final) == 4
    assert sum(r.row_class == "group" and r.label == "Styrene Oligomer" for r in ev.final) >= 30
    assert sum(r.row_class == "sum" for r in ev.final) == 1
    fid = {p.rt: p for p in ev.raw if p.signal == "FID"}
    assert fid[7.07].manual
    assert any(r.cas == "97-88-1" for r in ev.report)
    assert not any(r.row_class == "unnamed" for r in ev.report)
    assert removed_peaks(ev)


def test_scan_testsample():
    from gcws.learn.corpus import scan
    root = LEARN_ROOT / "2025"
    if not root.is_dir():
        pytest.skip("training data not available (set GCWS_LEARN_ROOT)")
    entries = scan(root)
    assert len(entries) >= 31
    assert not any("rptdef" in e.workbook for e in entries)
    coffee = [e for e in entries if "25026244_coffeecapsule" in e.batch_dir]
    assert coffee and all(e.blanks for e in coffee)


def _snapshot(folder: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}


def test_process_gio_run(tmp_path, monkeypatch):
    """The real baseline path: the user's NIAS method and library search (the app's own data folder)."""
    from PySide6.QtWidgets import QApplication
    from gcws import paths
    from gcws.learn.corpus import scan
    from gcws.learn.runner import process
    _gio_workbook()
    if not (ROOT / "data" / "processing_methods").is_dir():
        pytest.skip("the app's data folder (methods, libraries) is not available")
    monkeypatch.setattr(paths, "DATA", ROOT / "data")
    QApplication.instance() or QApplication([])
    entry = next(e for e in scan(LEARN_ROOT / "25011662_GIO_Diary") if e.workbook.endswith("_A.xlsm"))
    before = _snapshot(Path(entry.batch_dir))
    from gcws.learn.runner import migration_from_header
    from gcws.learn.workbook import parse_workbook
    migration = migration_from_header(parse_workbook(Path(entry.workbook)).header)
    prog = process(entry, tmp_path / "learn", migration=migration)
    assert prog.state in ("accepted_auto", "control"), prog.reason
    assert len(prog.peaks) > 50 and prog.reported
    assert _snapshot(Path(entry.batch_dir)) == before

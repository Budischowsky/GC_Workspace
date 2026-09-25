"""End-to-end reports from our own integration (identifications from LIB as test data)."""
from pathlib import Path

import pytest

from gcws.core.ident import Identification


def _ws_with(samples, prefixes, qapp):
    from gcws.core.model import FID
    from gcws.io.run_loader import load_run
    from gcws.signal.delay import estimate_delay
    from gcws.ui.workspace import Workspace
    from gcws.integration.engine import integrate
    from gcws.quant.nias_bridge import engine
    ws = Workspace()
    eng = engine()
    for prefix in prefixes:
        d = next(samples.glob(prefix + "*.D"))
        run = load_run(d)
        st = ws.add_run(run, {FID: integrate(run.fid, ws.methods.get(ws.methods.default_name(FID)))},
                        delay=estimate_delay(run.fid, run.signal("TIC")))
        if not (d / "RESULTS.CSV").exists():
            continue
        # test oracle: library hits of the old ChemStation LIB, bound by RT
        _tic, _fid, pbm = eng.parse_results(str(d / "RESULTS.CSV"))
        lib = eng.parse_library_report(str(d / "LIB"))
        res = st.results[FID]
        for p in pbm:
            hits = lib.get(p.peak) or p.hits
            if not hits:
                continue
            peak = min(res.peaks, key=lambda x: abs(x.apex_rt - (p.rt + st.delay_value)))
            if abs(peak.apex_rt - (p.rt + st.delay_value)) > 0.02:
                continue
            st.ident_set(FID).set(Identification(
                apex_rt=peak.apex_rt, name=hits[0].name, cas=hits[0].cas, score=hits[0].quality,
                hits=[{"name": h.name, "cas": h.cas, "score": h.quality} for h in hits], source="LIB (test)"))
    ws.recompute_quant()
    return ws


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtCore import QCoreApplication
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.mark.parametrize("kind", ["nias", "fingerprint", "total_extraction"])
def test_duplicate_reports(samples, qapp, tmp_path, kind):
    from openpyxl import load_workbook
    from gcws.report.service import ReportJob, SUFFIXES, generate
    from gcws.quant.service import cas_lookup
    from gcws import paths
    ws = _ws_with(samples, ["06_", "07_", "08_", "11_"], qapp)
    ids = [s.id for s in ws.states() if s.role == "sample"]
    assert len(ids) == 2
    for rid in ids:
        sample = ws.nias_sample(rid)
        assert sample is not None and sample.mean_factor
        assert ws.runs[rid].blanks and ws.runs[rid].blanks_istd
    from gcws.quant.nias_bridge import make_settings
    settings = make_settings(ws.quant.get("settings"))
    target = tmp_path / f"26016606{SUFFIXES[kind]}.xlsx"
    job = ReportJob(kind=kind, samples=[ws.nias_sample(r) for r in ids], names=[ws.runs[r].name for r in ids],
                    settings=settings, target=target, word=target.with_suffix(".docx"),
                    cas_path=paths.RESOURCES / "CASINFO.xlsx",
                    migration={"analyst": "Test", "migration_cell": "cell", "occupancy": "1",
                               "simulant": "EtOH 95 %", "temperature": "60 C", "duration": "10 d",
                               "cell_area_dm2": 0.51, "occupancy_factor": 1, "volume_ml": 100,
                               "ov_ratio": 6.0},
                    batch_target=tmp_path / "26016606_Doppelbestimmung.xlsx" if kind == "nias" else None,
                    keep_middle=tmp_path / "middle.xlsx")
    res = generate(job)
    assert res.target.exists() and res.rows > 0
    assert res.word is not None and res.word.exists()
    wb = load_workbook(tmp_path / "middle.xlsx")
    if kind == "nias":
        assert "Bestimmung_2" in wb.sheetnames
        assert (tmp_path / "26016606_Doppelbestimmung.xlsx").exists()


def test_triplicate_nias_report(samples, qapp, tmp_path):
    from openpyxl import load_workbook
    from gcws.report.service import ReportJob, generate
    from gcws import paths
    from gcws.quant.nias_bridge import make_settings
    ws = _ws_with(samples, ["06_", "07_", "08_", "09_", "11_"], qapp)
    ids = [s.id for s in ws.states() if s.role == "sample"]
    assert len(ids) == 3
    target = tmp_path / "triple_NIAS_Report.xlsx"
    job = ReportJob(kind="nias", samples=[ws.nias_sample(r) for r in ids], names=[ws.runs[r].name for r in ids],
                    settings=make_settings(ws.quant.get("settings")), target=target,
                    word=target.with_suffix(".docx"), cas_path=paths.RESOURCES / "CASINFO.xlsx",
                    policy="majority", keep_middle=tmp_path / "middle.xlsx")
    res = generate(job)
    assert res.target.exists()
    wb = load_workbook(tmp_path / "middle.xlsx")
    ws3 = wb["Doppelbestimmung"]
    headers = [c.value for c in ws3[1]]
    assert "Concentration 3 [mg/kg]" in headers and "SD [mg/kg]" in headers
    assert headers[3] == "mg/kg (mean)"
    means = [r[3] for r in ws3.iter_rows(min_row=2, values_only=True) if isinstance(r[3], (int, float))]
    assert means, "N-fold mean must be written as values"
    assert "Bestimmung_3" in wb.sheetnames

"""P54: the report job assembled without the main window (checks with their messages, the job's
fields) and the report's own summary kept in the result."""
import pytest

from test_report import _ws_with, qapp  # noqa: F401  (fixture)
from test_ui import win  # noqa: F401  (fixture)

MIGRATION = {"analyst": "Test", "migration_cell": "cell", "occupancy": "1", "simulant": "EtOH 95 %",
             "temperature": "60 C", "duration": "10 d", "cell_area_dm2": 0.51, "occupancy_factor": 1,
             "volume_ml": 100, "ov_ratio": 6.0}


@pytest.fixture(scope="module")
def ws(samples, qapp):
    return _ws_with(samples, ["06_", "07_", "08_", "11_"], qapp)


def _group(ws):
    ids = [s.id for s in ws.states() if s.role == "sample"]
    return {"id": "g", "name": "26016606", "members": ids, "policy": "all"}


def test_prepare_says_why_a_report_cannot_be_made(ws, monkeypatch):
    from gcws.report import assemble as AS
    ws.quant.pop("migration", None)
    cases = {"mode": ("hs_screening", _group(ws)), "no_group": ("nias", None),
             "no_samples": ("nias", {"id": "b", "name": "blank", "members": [ws.states()[0].id]}),
             "no_migration": ("nias", _group(ws))}
    for code, (kind, group) in cases.items():
        with pytest.raises(AS.ReportNotPossible) as exc:
            AS.prepare(ws, kind, group)
        assert exc.value.code == code, (code, exc.value.message)
    monkeypatch.setattr(AS, "cas_path", lambda: None)
    with pytest.raises(AS.ReportNotPossible, match="CASINFO"):
        AS.prepare(ws, "nias", _group(ws))
    monkeypatch.undo()
    ws.quant["migration"] = dict(MIGRATION)
    members, samples = AS.prepare(ws, "nias", _group(ws))
    assert members == _group(ws)["members"] and all(s is not None for s in samples)


def test_build_job_fields(ws, tmp_path):
    from gcws.report import assemble as AS
    ws.quant["migration"] = dict(MIGRATION)
    g = _group(ws)
    target = tmp_path / "x_NIAS_Report.xlsx"
    job = AS.build_job(ws, "nias", g, target)
    names = [ws.runs[m].name for m in g["members"]]
    assert job.names == names and job.target == target and job.word == target.with_suffix(".docx")
    assert job.batch_target == tmp_path / "26016606_Doppelbestimmung.xlsx" and job.sample_key == "26016606"
    assert job.record_seen and job.keep_middle is None and job.cas_path.name == "CASINFO.xlsx"
    assert "08_EtOH" in job.blank_names[0] and "06_EtOH_ISTD" in job.blank_names[1]
    assert job.migration["simulant"] == "EtOH 95 %"
    preview = AS.build_job(ws, "nias", g, target, preview=True, keep_middle=True)
    assert not preview.record_seen and preview.keep_middle is None
    kept = AS.build_job(ws, "nias", g, target, keep_middle=True, batch_workbook=False)
    assert kept.keep_middle.name == "x_NIAS_Report_intermediate.xlsx" and kept.batch_target is None
    assert AS.default_target(ws, "nias", g["members"]).name == "26016606_NIAS_Report.xlsx"


def test_report_result_keeps_the_summary(ws, tmp_path):
    from gcws.report import assemble as AS
    from gcws.report.service import generate
    ws.quant["migration"] = dict(MIGRATION)
    job = AS.build_job(ws, "nias", _group(ws), tmp_path / "s_NIAS_Report.xlsx", record_seen=False,
                       batch_workbook=False)
    res = generate(job)
    assert isinstance(res.summary.get("sml_exceedances"), list) and "unidentified_count" in res.summary
    assert res.combined and {"rt", "name", "mean", "status", "id_status"} <= set(res.combined[0])
    import json
    json.dumps(res.summary)
    json.dumps(res.combined)


def test_report_menu_messages_unchanged(qtbot, win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    w = win
    shown = []
    with monkeypatch.context() as m:
        m.setattr(QMessageBox, "information", lambda *a: shown.append(("information", a[1], a[2])))
        m.setattr(QMessageBox, "warning", lambda *a: shown.append(("warning", a[1], a[2])))
        w.report("nias")
        w.report("hs_screening")
    assert shown[0][:2] == ("information", "Report") and "replicate group" in shown[0][2]
    assert shown[1][:2] == ("information", "Report") and "HS-Screening mode" in shown[1][2]


def test_peaks_panel_reports_the_active_run_alone(qtbot, win, samples, monkeypatch):
    """The Peaks panel's report button: a single determination, even when the run is in a replicate group."""
    from PySide6.QtWidgets import QMessageBox
    from gcws.report import assemble as AS
    from test_ui import _load
    w = win
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: shown.append(a[2]))
    w.table.reportRequested.emit("nias", True)                 # nothing loaded: said, nothing written
    assert shown and "on its own" in shown[0]
    _load(qtbot, w, samples, ["07_", "08_"], process=False)
    ids = [s.id for s in w.ws.states()]
    for s in w.ws.states():
        s.run.role = "sample"
    w.ws.replicate_groups = [{"id": "g", "name": "pair", "members": ids, "policy": "all"}]
    w.ws.set_active(ids[1])
    seen = []

    def prepare(ws, kind, group):
        seen.append(group)
        raise AS.ReportNotPossible("stop", "stop", "information")
    monkeypatch.setattr(AS, "prepare", prepare)
    w.table.reportRequested.emit("nias", True)
    assert seen[-1]["members"] == [ids[1]]
    w.report("nias")                                            # the Report menu still takes the group
    assert seen[-1]["members"] == ids

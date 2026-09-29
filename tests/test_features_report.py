"""Feature double determination, P67: traffic light, AutoLib-shaped rows, panel and report."""
import numpy as np
import pytest

from gcws.features import combine as FC
from gcws.features import triage as TR
from gcws.features.model import (DETECTED, GAPFILL, NOT_DETECTABLE, Feature, FeatureTable, Identity, Member,
                                 PeakInfo, Settings)
from tests.test_report import MIGRATION, _report_table, _ws_with


def peak(i, rt=10.0, **kw):
    return PeakInfo(index=i, rt=rt, start=rt - 0.02, end=rt + 0.02, area=1e5, height=5e3, **kw)


def feature(members, sim=0.9, ident=None, **kw):
    f = Feature(members=members, rt=10.0, id="F-007", sim=sim, **kw)
    f.identity = ident or Identity("Xylene", "95-47-6", "Accepted", "A")
    return f


def pair(origin_b=DETECTED, note=""):
    return [Member("a", "A", peak(0), DETECTED, 10.0),
            Member("b", "B", peak(3) if origin_b in (DETECTED, GAPFILL) else None, origin_b, 10.001, note)]


def engine(n, rt=10.0, mg=1.0, name="Xylene", status="Accepted", **kw):
    return dict({"fid_peak": n, "rt": rt, "name": name, "cas": "95-47-6", "mg_kg": mg, "area": 1e5,
                 "id_status": status, "review": ""}, **kw)


def row(c1=1.0, c2=1.1):
    m = (c1 + c2) / 2 if c1 is not None and c2 is not None else None
    return {"mean": m, "c1": c1, "c2": c2, "reldiff": abs(c1 - c2) / m * 100 if m else None}


S = Settings()


def test_green_confirmed():
    light, text, reasons = TR.classify(feature(pair()), row(), 30.0, 0.01, S)
    assert (light, text) == ("green", "Confirmed")
    assert any("similarity 0.90" in r for r in reasons)


def test_grey_below_limit_and_blank_level():
    assert TR.classify(feature(pair()), row(0.001, 0.002), 30.0, 0.01, S)[0] == "grey"
    f = feature(pair())
    f.members[0].peak.blank_level = True
    assert TR.classify(f, row(), 30.0, 0.01, S)[:2] == ("grey", "Blank level")


def test_red_cases():
    lone = feature(pair(NOT_DETECTABLE, "not detectable: m/z 91 shows no peak"))
    light, text, reasons = TR.classify(lone, {"mean": None, "c1": 0.5, "c2": None, "reldiff": None}, 30.0, 0.01, S)
    assert (light, text) == ("red", "Only in A") and "m/z 91" in reasons[0]
    assert TR.classify(feature(pair(), sim=0.3, mismatch=True), row(), 30.0, 0.01, S)[:2] == ("red", "Spectra differ")
    assert TR.classify(feature(pair()), row(1.0, 2.0), 30.0, 0.01, S)[0] == "red"          # 67 % > 45 %
    assert TR.classify(feature(pair(), split=True), row(), 30.0, 0.01, S)[:2] == ("red", "Check pairing")


def test_yellow_cases():
    light, text, _ = TR.classify(feature(pair(GAPFILL, "ions 91/105 co-elute")), row(), 30.0, 0.01, S)
    assert (light, text) == ("yellow", "Check: gap fill")
    c = Identity("Xylene / Ethylbenzene", "95-47-6 / 100-41-4", "Manual review", "C")
    assert TR.classify(feature(pair(), ident=c), row(), 30.0, 0.01, S)[:2] == ("yellow", "Check: 2 candidates")
    assert TR.classify(feature(pair()), row(1.0, 1.4), 30.0, 0.01, S)[:2] == ("yellow", "Deviation 33 % > 30 %")
    assert TR.classify(feature(pair(), sim=0.65), row(), 30.0, 0.01, S)[0] == "yellow"


def _table(features, n=2):
    return FeatureTable([c for c in "ab"[:n]], ["A", "B"][:n], "FID", features, settings=S)


def test_rows_have_autolib_shape():
    f1 = feature(pair())
    f2 = feature([Member("a", "A", peak(1, 12.0), DETECTED, 12.0),
                  Member("b", "B", None, NOT_DETECTABLE, None, "not detectable: S/N 1.2 < 3")])
    f2.id, f2.rt = "F-009", 12.0
    lists = [[engine(1, 10.0, 1.0), engine(2, 12.0, 0.4, review="unsichere Identifikation")],
             [engine(4, 10.001, 1.2)]]
    rows = FC.rows(_table([f1, f2]), lists, 30.0, 0.01)
    assert [r["feature_id"] for r in rows] == ["F-007", "F-009"]
    a, b = rows
    for key in ("rt", "name", "cas", "mean", "c1", "c2", "reldiff", "status", "id_status", "review",
                "source1", "source2", "cs", "sources", "sd", "rsd", "n_detected", "n"):
        assert key in a
    assert a["status"] == "Valid duplicate" and a["mean"] == pytest.approx(1.1)
    assert a["reldiff"] == pytest.approx(0.2 / 1.1 * 100)
    assert a["id_status"] == "Accepted" and a["light"] == "green" and a["edit_key"] == "F-007"
    assert b["status"] == "Artefact: only determination 1" and b["mean"] is None
    assert "Nur in Bestimmung 1 detektiert" in b["review"] and "nicht nachweisbar" in b["review"]
    assert b["light"] == "red"


def test_candidates_are_both_names_and_cas():
    c = Identity("Xylene / Ethylbenzene", "95-47-6 / 100-41-4", "Manual review", "C",
                 candidates=[{"name": "Xylene", "score": 86}, {"name": "Ethylbenzene", "score": 85}])
    rows = FC.rows(_table([feature(pair(), ident=c)]), [[engine(1)], [engine(4, name="Ethylbenzene")]], 30.0, 0.01)
    (r,) = rows
    assert r["name"] == "Xylene / Ethylbenzene" and r["cas"] == "95-47-6 / 100-41-4"
    assert r["id_status"] == "Manual review" and "Identifikation uneindeutig" in r["review"]


def test_edits_found_by_feature_id():
    from gcws.quant import duplicate_view as DV
    rows = FC.rows(_table([feature(pair())]), [[engine(1)], [engine(4, mg=1.2)]], 30.0, 0.01)
    verdicts = [DV.plain_verdict(r, 30.0, 0.01) for r in rows]
    assert verdicts[0].level == "ok" and verdicts[0].text == "Confirmed"
    # the feature moved by 0.2 min after a re-integration: the id still finds its edit, the RT would not
    edits = {"F-007": {"rt": 10.2, "mean": 1.234, "comment": "checked"}, "10.000": {"rt": 10.0, "report": False}}
    (r,) = DV.apply_edits(rows, verdicts, edits, 0.035)
    assert r["mean"] == pytest.approx(1.234) and r["comment"] == "checked" and r["report"] is True


def test_processing_method_carries_the_settings():
    from gcws.core import proc_method as PM
    assert PM.QUANT_SECTIONS["features"] == "features"
    assert "features" in PM.SECTIONS and "features" in PM.WORKSPACE_SECTIONS


# -- real data -----------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def quant_ws(samples, qapp):
    ws = _ws_with(samples, ["06_", "07_", "08_", "11_"], qapp)
    return ws, [s.id for s in ws.states() if s.role == "sample"]


def test_real_classic_pairing_is_autolib(quant_ws):
    from gcws.quant import duplicate_view as DV
    from gcws.quant.replicates import combine, engine_peaks
    ws, ids = quant_ws
    ws.quant["features"] = {"pairing": "classic"}
    try:
        rows, problems = DV.compute(ws, ids, "all")
        raw = combine([engine_peaks(ws.nias_sample(r)) for r in ids], 0.035)
        assert not problems
        assert [(r["rt"], r["name"], r["mean"], r["status"]) for r in rows] == \
               [(r["rt"], r["name"], r["mean"], r["status"]) for r in raw]
        assert not any("feature_id" in r for r in rows)
    finally:
        ws.quant.pop("features", None)


def test_real_feature_rows_keep_the_nias_numbers(quant_ws):
    from gcws.quant import duplicate_view as DV
    ws, ids = quant_ws
    rows, problems = DV.compute(ws, ids, "all")
    assert not problems and rows
    lights = {r["light"] for r in rows}
    assert lights <= {"green", "yellow", "red", "grey"} and "green" in lights
    for r in rows:
        for k, src in ((1, r["source1"]), (2, r["source2"])):
            if src is not None:
                assert r[f"c{k}"] == src["mg_kg"]          # the concentration of each determination is NIAS'
        if r["source1"] is not None and r["source2"] is not None and r["c1"] is not None and r["c2"] is not None:
            assert r["mean"] == pytest.approx((r["c1"] + r["c2"]) / 2)


def test_real_nias_report_with_feature_pairing(quant_ws, tmp_path):
    from gcws import paths
    from gcws.features import service as SV
    from gcws.quant.nias_bridge import make_settings
    from gcws.report.service import ReportJob, combined_rows, generate
    ws, ids = quant_ws
    table = SV.build(ws, ids, search=False)

    def job(name, edits):
        t = tmp_path / f"{name}_NIAS_Report.xlsx"
        return ReportJob(kind="nias", samples=[ws.nias_sample(r) for r in ids], names=[ws.runs[r].name for r in ids],
                         settings=make_settings(ws.quant.get("settings")), target=t, word=t.with_suffix(".docx"),
                         cas_path=paths.RESOURCES / "CASINFO.xlsx", migration=MIGRATION, record_seen=False,
                         edits=edits, features=table)
    rows = combined_rows(job("rows", {}))
    assert rows and all("feature_id" in r for r in rows)
    assert not any(str(r["status"]).startswith("Artefact") for r in rows)       # not reported by default
    res = generate(job("plain", {}))
    rep = _report_table(res.target)
    assert rep
    istd = {f.id for f in table.features if any(m.peak.istd for m in f.found)}
    target = next(r for r in rows if r["light"] == "green" and r["mean"] and r["mean"] > 0.05
                  and r["feature_id"] not in istd)                   # standards are not in the result table
    edits = {target["feature_id"]: {"rt": target["rt"] + 0.3, "mean": 1.234}}      # found by id, not by RT
    rep2 = _report_table(generate(job("edited", edits)).target)
    near = [r for r in rep2 if abs(r[0] - target["rt"]) < 0.02]
    assert near and near[0][2] == pytest.approx(1.234, abs=1e-3)

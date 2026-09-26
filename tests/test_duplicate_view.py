"""Double determination verdicts (pure rules, no GUI)."""
import pytest

from gcws.quant import duplicate_view as DV


def row(status, c1=None, c2=None, mean=None, reldiff=None, id_status="Accepted", review="", s1=True, s2=True):
    return {"rt": 10.0, "name": "X", "cas": "", "status": status, "c1": c1, "c2": c2, "mean": mean,
            "reldiff": reldiff, "id_status": id_status, "review": review,
            "source1": {"rt": 10.0, "name": "X"} if s1 else None,
            "source2": {"rt": 10.01, "name": "Y"} if s2 else None}


@pytest.mark.parametrize("r, level, text", [
    (row("Valid duplicate", 1.0, 1.1, 1.05, 9.5), "ok", "Confirmed"),
    (row("Valid duplicate, also in Blank", 1.0, 1.1, 1.05, 9.5), "ok", "Confirmed (in blank)"),
    (row("Valid duplicate", 1.0, 2.0, 1.5, 66.7), "warn", "Deviation 67 % > 30 %"),
    (row("Valid duplicate", 0.001, 0.004, 0.0025, 120.0), "neutral", "Below reporting limit"),
    (row("Valid duplicate", 1.0, 1.1, 1.05, 9.5, id_status="Manual review"), "warn", "Confirmed, check identification"),
    (row("Identification conflict", 1.0, 1.1, 1.05, 9.5, id_status="Conflict; manual review"), "bad",
     "Identifications differ"),
    (row("Artefact: only determination 1", 1.0, s2=False), "bad", "Only in A"),
    (row("Artefact: only determination 2, also in Blank", None, 1.0, s1=False), "bad", "Only in B"),
    (row("Einzelbestimmung", 1.0, None, 1.0), "neutral", "Single determination"),
    (row("Valid duplicate", None, None, None, None), "info", "Confirmed, no concentration"),
])
def test_plain_verdict(r, level, text):
    v = DV.plain_verdict(r, 30.0, 0.01, ("A", "B"), "mg/kg")
    assert v.level == level and v.text == text
    assert v.detail


def test_blank_suffix_reaches_detail():
    v = DV.plain_verdict(row("Valid duplicate, also in Blank", 1.0, 1.1, 1.05, 9.5), 30.0, 0.01)
    assert "blank" in v.detail.lower()


def test_summary_counts_and_text():
    rows = [row("Valid duplicate", 1.0, 1.1, 1.05, 9.5), row("Valid duplicate", 1.0, 2.0, 1.5, 66.7),
            row("Artefact: only determination 1", 1.0, s2=False), row("Identification conflict", 1, 1, 1, 0)]
    vs = [DV.plain_verdict(r, 30.0, 0.01) for r in rows]
    s = DV.summarize(rows, vs, 30.0)
    assert (s.confirmed, s.deviating, s.only_a, s.only_b, s.conflicts) == (1, 1, 1, 0, 1)
    assert s.mean_reldiff == pytest.approx((9.5 + 66.7) / 2)
    assert s.level == "bad" and "review before reporting" in s.text
    ok = DV.summarize(rows[:1], vs[:1], 30.0)
    assert ok.level == "ok" and "consistent" in ok.text


def test_english_display_only():
    assert DV.english("Nur in Bestimmung 2 detektiert; als Artefakt bewertet") == \
        "only detected in determination 2; treated as an artefact"
    assert DV.english("ISTD geschützt; keine Blanksubtraktion") == "ISTD protected; no blank subtraction"
    assert DV.english("Einzelbestimmung, also in Blank") == "single determination, also in Blank"
    assert DV.english("unchanged text") == "unchanged text"


def test_suggest_partner_and_problems(samples):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from gcws.core.model import FID
    from gcws.integration.engine import integrate
    from gcws.io.run_loader import load_run
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    ids = {}
    for prefix in ("07_", "08_", "11_"):
        run = load_run(next(samples.glob(prefix + "*.D")))
        st = ws.add_run(run, {FID: integrate(run.fid, ws.methods.get(ws.methods.default_name(FID)))})
        ids[prefix] = st.id
    assert DV.suggest_partner(ws, ids["07_"]) == ids["11_"]
    assert DV.suggest_partner(ws, ids["11_"]) == ids["07_"]
    assert DV.suggest_partner(ws, ids["08_"]) is None
    probs = DV.member_problems(ws, [ids["07_"], ids["08_"]])
    assert len(probs) == 1 and "08_EtOH" in probs[0] and "Blank" in probs[0]


def _pair(rt, c1, c2, status="Doppelbestimmung bestätigt", a1=1000.0, a2=1100.0, name="X"):
    s1 = None if c1 is None else {"rt": rt, "mg_kg": c1, "area": a1, "name": name}
    s2 = None if c2 is None else {"rt": rt + 0.001, "mg_kg": c2, "area": a2, "name": name}
    vals = [v for v in (c1, c2) if v is not None]
    mean = sum(vals) / 2 if len(vals) == 2 else None
    return {"rt": rt, "name": name, "cas": "", "c1": c1, "c2": c2, "mean": mean,
            "reldiff": abs(c1 - c2) / mean * 100 if mean else None, "status": status, "id_status": "Accepted",
            "source1": s1, "source2": s2}


def test_analyst_edits_and_report_rows():
    from gcws.quant import duplicate_view as DV
    rows = [_pair(10.0, 0.10, 0.12), _pair(12.0, 0.05, None, status="Artefact: only determination 1"),
            _pair(14.0, 0.30, 0.34), _pair(16.0, 0.002, 0.003)]
    verdicts = [DV.plain_verdict(r, 30.0, 0.01) for r in rows]
    # defaults: no artefacts, nothing below the reporting limit
    plain = DV.apply_edits(rows, verdicts, {}, 0.035)
    assert [r["report"] for r in plain] == [True, False, True, False]
    assert not any(r["edited"] for r in plain)
    edits = {"10.000": {"rt": 10.0, "a1": 2000.0},              # area A doubled -> conc A doubles
             "12.001": {"rt": 12.001, "report": True},          # the analyst keeps the artefact
             "14.010": {"rt": 14.01, "mean": 0.5, "comment": "checked"},
             "16.000": {"rt": 16.0, "report": False}}
    out = DV.apply_edits(rows, verdicts, edits, 0.035)
    assert out[0]["c1"] == pytest.approx(0.20) and out[0]["mean"] == pytest.approx(0.16)
    assert out[0]["reldiff"] == pytest.approx(abs(0.20 - 0.12) / 0.16 * 100)
    assert out[0]["edited"]["a1"] == 1000.0 and out[0]["edited"]["c1"] == pytest.approx(0.10)
    assert out[1]["report"] and out[1]["mean"] == pytest.approx(0.05)
    assert out[2]["mean"] == 0.5 and out[2]["comment"] == "checked" and out[2]["c1"] == 0.30
    keep, over = DV.rows_for_report(rows, edits, 30.0, 0.01, 0.035)
    assert [r["rt"] for r in keep] == [10.0, 12.0, 14.0]
    assert over[0]["c1"] == pytest.approx(0.20) and over[0]["mean"] == pytest.approx(0.16)
    assert over[1]["mean"] == pytest.approx(0.05) and over[2]["mean"] == 0.5
    assert "checked" in keep[2]["review"]
    # without edits: the rows AutoLib reports, their numbers untouched
    keep, over = DV.rows_for_report(rows, {}, 30.0, 0.01, 0.035)
    assert [r["rt"] for r in keep] == [10.0, 14.0] and over == {}
    assert keep[0] is not rows[0] and keep[0]["mean"] == rows[0]["mean"]
    # an edit is found again after a small RT shift (re-integration), but only within the tolerance
    assert DV.find_edit({"10.000": {"rt": 10.0}}, 10.02, 0.035) == "10.000"
    assert DV.find_edit({"10.000": {"rt": 10.0}}, 10.05, 0.035) is None

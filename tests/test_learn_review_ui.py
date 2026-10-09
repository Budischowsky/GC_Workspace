"""Report² > View > Learning review...: the review list as a window (verdicts saved at once, nothing modal)."""
import pytest

pytest.importorskip("pytestqt")


def _items():
    from gcws.learn.review import ReviewItem, item_id
    rows = [("not_reported", 7.07, {"decision": "reported_named", "label": "Butyl methacrylate", "cas": "97-88-1",
                                    "conc_mgkg": 1.46}, {"name": "Butyl methacrylate", "score": 81.0, "hits": []}),
            ("extra_reported", 8.0, {}, {"report_line": "Ethanol, 2-butoxy-", "report_cas": "111-76-2"}),
            ("name_differs", 10.85, {"label": "Benzaldehyde, 2,4,6-trimethyl-", "cas": "487-68-3"},
             {"name": "Benzaldehyde, 2,4,5-trimethyl-", "cas": "5779-72-6", "score": 94.0,
              "hits": [{"name": "Benzaldehyde, 2,4,6-trimethyl-", "cas": "487-68-3", "score": 93.0}]})]
    return [ReviewItem(id=item_id("B1", "05_X_A.D", "BlM", t, rt), batch="B1", run="05_X_A.D", analyst="BlM", type=t,
                       rt=rt, analyst_side=a, program_side=p) for t, rt, a, p in rows]


@pytest.fixture
def learn_dir(tmp_path, monkeypatch):
    from gcws import paths
    from gcws.learn.review import write_review
    monkeypatch.setattr(paths, "DATA", tmp_path)
    write_review(_items(), tmp_path / "learn")
    return tmp_path / "learn"


def test_window_lists_items_and_shows_evidence(qtbot, learn_dir):
    from gcws.ui.dialogs.learn_review import LearningReview
    w = LearningReview()
    qtbot.addWidget(w)
    assert w.table.rowCount() == 3
    w.table.selectRow(2)
    text = w.evidence.toPlainText()
    assert "Benzaldehyde, 2,4,5-trimethyl-" in text and "487-68-3" in text and "93" in text


def test_verdict_saved_at_once(qtbot, learn_dir):
    from gcws.learn.review import load_verdicts
    from gcws.ui.dialogs.learn_review import LearningReview
    w = LearningReview()
    qtbot.addWidget(w)
    w.table.selectRow(0)
    w.note.setText("program line is fine")
    w.b_program.click()
    (v,) = load_verdicts(learn_dir).values()
    assert v["verdict"] == "program" and v["note"] == "program line is fine" and v["type"] == "not_reported"
    assert "program" in w.table.item(0, 0).text().casefold()


def test_type_filter_and_open_only(qtbot, learn_dir):
    from gcws.ui.dialogs.learn_review import LearningReview
    w = LearningReview()
    qtbot.addWidget(w)
    w.type_filter.setCurrentIndex(w.type_filter.findData("name_differs"))
    assert w.table.rowCount() == 1
    w.type_filter.setCurrentIndex(0)
    w.table.selectRow(0)
    w.b_analyst.click()
    w.open_only.setChecked(True)
    assert w.table.rowCount() == 2


def test_hint_without_items(qtbot, tmp_path, monkeypatch):
    from gcws import paths
    from gcws.ui.dialogs.learn_review import LearningReview
    monkeypatch.setattr(paths, "DATA", tmp_path)
    w = LearningReview()
    qtbot.addWidget(w)
    assert not w.hint.isHidden() and "review" in w.hint.text() and w.table.rowCount() == 0


def test_report2_view_menu_has_learning_review():
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "gcws" / "ui" / "docks" / "report2.py").read_text(encoding="utf-8")
    assert '"Learning review..."' in src
    ast.parse(src)

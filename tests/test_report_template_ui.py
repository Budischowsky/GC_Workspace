"""The Report template window: columns, header, rules, named store, method, preview (no modal dialog waits)."""
from types import SimpleNamespace

import pytest

from gcws import paths
from gcws.report import template as TP
from test_ui import _load, win  # noqa: F401  (fixture)

pytest.importorskip("pytestqt")


@pytest.fixture
def dlg(qtbot, win, tmp_path, monkeypatch):
    from gcws.ui.dialogs.report_template import ReportTemplateDialog
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    d = ReportTemplateDialog(win.ws, win, group_for_preview=lambda: None, method_name=lambda: "")
    qtbot.addWidget(d)
    return d


def test_starts_from_the_layout_of_the_mode_and_edits_columns(dlg):
    assert dlg.tpl["columns"][0]["field"] == "rt"                     # NIAS layout (NIAS mode)
    # every setting of the layout survives filling the window (rows, limit, sums, extras)
    assert not TP.differs(dlg.tpl, TP.preset("NIAS")) and dlg.name == ""
    assert dlg.skip_istd.isChecked() and dlg.category_sums.isChecked() and dlg.limit_mode.currentData() == "value"
    assert dlg.changed_chip.text() == "not saved"
    assert dlg.cols.topLevelItemCount() == len(TP.preset("NIAS")["columns"])
    dlg.new_from("Empty")
    assert dlg.cols.topLevelItemCount() == 0 and dlg.name == ""
    for key in ("name", "conc:ug_l", "reldiff", "area"):
        dlg.add_field(key)
    assert [c["field"] for c in dlg.tpl["columns"]] == ["name", "conc:ug_l", "reldiff", "area"]
    dlg.cols.clearSelection()
    dlg.cols.topLevelItem(3).setSelected(True)
    dlg.move(-1)
    assert [c["field"] for c in dlg.tpl["columns"]] == ["name", "conc:ug_l", "area", "reldiff"]
    dlg.set_column(1, header="µg/L ({unit})", decimals=1, view="each_mean")
    assert dlg.tpl["columns"][1] == {"field": "conc:ug_l", "header": "µg/L ({unit})", "decimals": 1,
                                     "view": "each_mean"}
    dlg.cols.clearSelection()
    dlg.cols.topLevelItem(0).setSelected(True)
    dlg.remove_selected()
    assert [c["field"] for c in dlg.tpl["columns"]] == ["conc:ug_l", "area", "reldiff"]
    # a grey palette entry says why the mode cannot fill it
    item = next(dlg.fields_tree.topLevelItem(2).child(j) for j in range(dlg.fields_tree.topLevelItem(2).childCount())
                if dlg.fields_tree.topLevelItem(2).child(j).data(0, 256) == "conc:ug_hs")
    assert "Not in this quantification" in item.toolTip(0)
    dlg.filter.setText("blank")
    shown = [dlg.fields_tree.topLevelItem(1).child(j).text(0) for j in range(dlg.fields_tree.topLevelItem(1).childCount())
             if not dlg.fields_tree.topLevelItem(1).child(j).isHidden()]
    assert shown and all("blank" in s.lower() for s in shown)


def test_header_rows_and_extras_reach_the_template(dlg):
    dlg.title_edit.setText("My lab")
    dlg.title_edit.textEdited.emit("My lab")
    dlg.add_header_field(new_line=True, label="Customer:", value="ACME")
    assert dlg.tpl["header"]["title"] == "My lab"
    assert dlg.tpl["header"]["lines"][-1] == [{"label": "Customer:", "value": "ACME"}]
    dlg._focus_edit = dlg.subtitle_edit
    dlg.insert_placeholder("date")
    assert dlg.tpl["header"]["subtitle"].endswith("{date}")
    dlg.limit_mode.setCurrentIndex(dlg.limit_mode.findData("value"))
    dlg.limit_mode.activated.emit(dlg.limit_mode.currentIndex())
    dlg.limit_value.setValue(0.5)
    assert dlg.tpl["rows"]["limit"]["value"] == 0.5 and not dlg.tpl["rows"]["limit"]["use_method"]
    dlg.orientation.setCurrentIndex(dlg.orientation.findData("portrait"))
    dlg.orientation.activated.emit(dlg.orientation.currentIndex())
    dlg.on_method_run.setChecked(False)
    assert dlg.tpl["extras"]["orientation"] == "portrait" and not dlg.tpl["extras"]["on_method_run"]


def test_a_placeholder_goes_where_the_analyst_is(dlg):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent
    from PySide6.QtWidgets import QApplication
    dlg.add_header_field(new_line=True, label="Customer:", value="")
    title = dlg.title_edit.text()
    QApplication.sendEvent(dlg.title_edit, QFocusEvent(QEvent.FocusIn))     # the title had the focus ...
    QApplication.sendEvent(dlg.lines, QFocusEvent(QEvent.FocusIn))          # ... then the header lines
    r = dlg.lines.rowCount() - 1
    dlg.lines.setCurrentCell(r, 2)
    dlg.insert_placeholder("date")
    assert dlg.title_edit.text() == title and dlg.lines.item(r, 2).text() == "{date}"
    assert dlg.tpl["header"]["lines"][-1] == [{"label": "Customer:", "value": "{date}"}]


def test_only_headers_views_and_the_decimals_of_numbers_are_edited(dlg):
    from PySide6.QtWidgets import QSpinBox, QStyleOptionViewItem
    from gcws.ui.dialogs.report_template import C_DEC, C_FIELD, C_HEADER
    delegate, model, parent = dlg.cols.itemDelegate(), dlg.cols.model(), dlg.cols.viewport()
    fields = [c["field"] for c in dlg.tpl["columns"]]
    rt, name = fields.index("rt"), fields.index("name")
    editor = lambda row, col: delegate.createEditor(parent, QStyleOptionViewItem(), model.index(row, col))
    assert editor(rt, C_FIELD) is None and editor(name, C_DEC) is None
    for w in (editor(rt, C_DEC), editor(rt, C_HEADER)):
        assert w is not None
        w.deleteLater()
    assert isinstance(editor(rt, C_DEC), QSpinBox)


def test_a_mode_change_greys_what_the_mode_cannot_fill(dlg, win):
    def palette_item(key):
        for i in range(dlg.fields_tree.topLevelItemCount()):
            top = dlg.fields_tree.topLevelItem(i)
            for j in range(top.childCount()):
                if top.child(j).data(0, 256) == key:
                    return top.child(j)
    row = [c["field"] for c in dlg.tpl["columns"]].index("conc:mg_dm2")
    assert "Not in this quantification" not in palette_item("conc:mg_dm2").toolTip(0)
    win.ws.quant["mode"] = "area_pct"
    win.ws.quantChanged.emit()
    assert "Not in this quantification" in palette_item("conc:mg_dm2").toolTip(0)
    assert dlg.cols.topLevelItem(row).text(0).startswith("⚠")
    assert dlg.sml_bold.findData("conc:mg_dm2") < 0 or dlg.sml_bold.currentData() == "conc:mg_kg"


def test_columns_moved_back_are_not_a_change(dlg):
    assert dlg.save_as("Lab") and not dlg.is_modified()
    dlg._sync_columns()                          # what a drag and drop that ends where it began does
    assert not dlg.is_modified() and dlg.tpl["columns"][0]["header"] == "RT (min)"


def test_a_failing_preview_leaves_the_window_usable(dlg, monkeypatch):
    from gcws.report import table as TB

    def boom(*_a, **_k):
        raise ValueError("boom")
    monkeypatch.setattr(dlg, "report_data", lambda: SimpleNamespace(values={}))
    monkeypatch.setattr(TB, "build", boom)
    dlg._render()
    assert "boom" in dlg.warnings.text()


def test_saving_a_preview_counts_substances_only_when_the_template_asks(win, monkeypatch, tmp_path):
    from gcws.report import service as RS
    from gcws.ui.dialogs import report_preview as RP
    calls = []

    class Preview:
        def __init__(self, *_a, **_k):
            self.saved_to = tmp_path / "saved.xlsx"

        def exec(self):
            return 1
    monkeypatch.setattr(RS, "record_seen", lambda *a: calls.append(a) or "")
    monkeypatch.setattr(RP, "ReportPreview", Preview)
    tpl = TP.preset("NIAS")
    tpl["extras"]["record_seen"] = False
    job = SimpleNamespace(names=["x"], template=tpl, sample_key="x")
    res = SimpleNamespace(target=tmp_path / "t.xlsx", word=None, batch=None, warnings=[], reported=[{"name": "a"}])
    win._report_done("template", job, res, [], True)
    assert calls == []
    tpl["extras"]["record_seen"] = True
    win._report_done("template", job, res, [], True)
    assert len(calls) == 1


def test_named_store_and_the_method(dlg, win):
    assert dlg.save_as("Customer A") and TP.names() == ["Customer A"]
    assert not dlg.is_modified() and dlg.templates.currentText() == "Customer A"
    dlg.add_field("comment")
    assert dlg.is_modified() and dlg.changed_chip.text().startswith("●")
    assert dlg.save() and not dlg.is_modified()
    assert not dlg.in_method()
    dlg.use_in_method()
    assert dlg.in_method() and TP.of(win.ws.quant)["name"] == "Customer A"
    (win.ws.undo_group.activeStack() or win.ws.project_undo).undo()        # one step
    assert TP.of(win.ws.quant) is None and not dlg.in_method()
    assert dlg.delete(confirm=False) and TP.names() == []


def test_save_to_method_writes_the_method_file(dlg, win, monkeypatch):
    from gcws.core import proc_method as PM
    PM.save({"format": "gcws-processing-method", "version": 1, "name": "M1", "sections": {"quant": {}}})
    assert dlg.save_to_method("M1")
    assert PM.load("M1")["sections"][TP.QUANT_KEY]["columns"] == dlg.tpl["columns"]
    assert dlg.in_method()


def test_preview_follows_the_active_run_and_edits(qtbot, dlg, win, samples):
    _load(qtbot, win, samples, ["07_"])
    win.ws.recompute_quant()
    dlg.r_run.setChecked(True)
    dlg.refresh_preview(now=True)
    assert "NIAS-Screening" in dlg.preview.toHtml()
    assert dlg.last_table is not None and dlg.last_table.subtitle.endswith("single determination")
    dlg.title_edit.setText("Changed title")
    dlg.title_edit.textEdited.emit("Changed title")
    qtbot.waitUntil(lambda: "Changed title" in dlg.preview.toHtml(), timeout=3000)


def test_run_method_writes_the_template_report(qtbot, win, samples, tmp_path, monkeypatch):
    from gcws.report import assemble as AS
    from gcws.ui import main_window as MW
    from gcws.ui import workers
    _load(qtbot, win, samples, ["06_", "07_", "08_", "11_"])
    ws = win.ws
    ids = list(ws.order)
    for st in ws.states():
        if st.name.startswith(("07_", "11_")):
            st.run.role = "sample"
    ws.replicate_groups = [{"id": "g", "name": "pair", "members": [s.id for s in ws.states()
                                                                    if s.name.startswith(("07_", "11_"))],
                            "policy": "all"}]
    ws.recompute_quant()
    monkeypatch.setattr(AS, "default_target", lambda ws, kind, members, template=None:
                        tmp_path / f"{len(members)}{'_'.join(ws.runs[m].name[:2] for m in members)}.xlsx")
    monkeypatch.setattr(workers, "submit", lambda fn, *a, on_done=None, on_error=None, **k: on_done(fn(*a)))
    assert win._method_reports(ids) == ""                               # no template: no report
    tpl = TP.stamped(TP.preset("NIAS"), "Lab")
    tpl["extras"]["on_method_run"] = False
    ws.quant[TP.QUANT_KEY] = tpl
    assert win._method_reports(ids) == ""                               # the template does not ask for it
    tpl["extras"]["on_method_run"] = True
    ws.quant[TP.QUANT_KEY] = TP.normalise(tpl)
    text = win._method_reports(ids)
    assert text.startswith("1 Template Report(s) being written")
    assert (tmp_path / "207_11.xlsx").exists() and (tmp_path / "207_11.docx").exists()
    assert any(r.action == "Template Report" for r in ws.audit.records)
    assert "1 written next to the data" in win.statusBar().currentMessage()
    # a report that cannot be written does not stop the others
    out = MW.write_reports([SimpleNamespace(names=["x"], kind="template", target=tmp_path / "missing" / "x.xlsx",
                                            word=None, table=None, template=None)])
    assert out[0]["names"] == ["x"] and "error" in out[0]

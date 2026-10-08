"""The Report template window: columns, header, rules, named store, method, preview (no modal dialog waits)."""
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

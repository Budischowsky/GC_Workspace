"""Ctrl+C in the Replicates and Peaks panels copies every digit of a number (as Excel does); the tables
still show it rounded."""
import pytest

from gcws.core.text import copy_number
from test_ui import _load, win  # noqa: F401  (fixture)

pytest.importorskip("pytestqt")


@pytest.fixture
def clip(monkeypatch):
    """A clipboard of the test's own (the system clipboard is shared with the user and other processes)."""
    from types import SimpleNamespace
    from PySide6.QtGui import QGuiApplication
    box = {"text": ""}
    fake = SimpleNamespace(setText=lambda t: box.update(text=t), text=lambda: box["text"])
    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: fake))
    return fake


def test_copy_number_keeps_what_excel_keeps():
    assert copy_number(0.016264440035032253) == "0.0162644400350323"        # 15 significant digits
    assert copy_number(1.234e-05) == "0.00001234"                           # no exponent
    assert copy_number(123456.789) == "123456.789"                          # no thousands separator
    assert copy_number(2.0) == "2" and copy_number(-0.0) == "0" and copy_number(7) == "7"
    assert copy_number(None) == "" and copy_number(float("nan")) == ""
    assert copy_number("Benzene") == "Benzene" and copy_number(True) == "True"


def test_the_sheet_copies_the_full_value_it_shows_rounded(qtbot, clip):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem
    from gcws.ui.widgets.sheet_table import COPY_ROLE, SheetTable
    t = SheetTable(1, 2)
    qtbot.addWidget(t)
    t.setItem(0, 0, QTableWidgetItem("Benzene"))
    it = QTableWidgetItem()
    it.setData(Qt.DisplayRole, 0.0163)
    it.setData(COPY_ROLE, 0.016264440035032253)
    t.setItem(0, 1, it)
    t.selectAll()
    t.copy_selection()
    assert clip.text() == "Benzene\t0.0162644400350323"
    assert t.item(0, 1).text() == "0.0163"


def test_the_double_determination_copies_every_digit(qtbot, win, samples, clip):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QTableWidgetSelectionRange
    from gcws.ui.docks.duplicate import C_C1, C_MEAN, C_NAME, C_RT
    _load(qtbot, win, samples, ["07_", "11_"])
    ws = win.ws
    a = next(s.id for s in ws.states() if s.name.startswith("07_"))
    b = next(s.id for s in ws.states() if s.name.startswith("11_"))
    win.loaded_samples.pairRequested.emit(a, b)
    page = win.replicates.duplicate
    t = page.table
    row_of = lambda r: page.rows[t.item(r, 0).data(Qt.UserRole)]
    r = next(r for r in range(t.rowCount()) if all(isinstance(row_of(r).get(k), float) for k in ("c1", "mean")))
    row = row_of(r)
    t.clearSelection()
    t.setRangeSelected(QTableWidgetSelectionRange(r, C_RT, r, C_NAME), True)
    t.setFocus()
    QTest.keyClick(t, Qt.Key_C, Qt.ControlModifier)
    assert clip.text() == copy_number(row["rt"]) + "\t" + row["name"]
    assert len(t.item(r, C_RT).text()) < len(copy_number(row["rt"]))       # shown rounded
    page.copy_row(row)                                                      # right-click > Copy row
    cells = clip.text().split("\t")
    assert copy_number(row["c1"]) in cells and copy_number(row["mean"]) in cells
    assert t.item(r, C_C1).text() not in cells and t.item(r, C_MEAN).text() not in cells


def test_the_nfold_worksheet_copies_rows_with_every_digit(qtbot, win, samples, clip):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    _load(qtbot, win, samples, ["06_", "07_", "11_"])
    ws = win.ws
    for st in ws.states():
        st.run.role = "sample"
    ws.replicate_groups = [{"id": "g3", "name": "three", "members": list(ws.order), "policy": "all"}]
    ws.recompute_quant()
    rep = win.replicates
    rep.show_group("g3")
    sheet = rep.sheet
    row_of = lambda r: rep.rows[sheet.item(r, 1).data(Qt.UserRole)]
    r = next(r for r in range(sheet.rowCount()) if isinstance(row_of(r)["mean"], float))
    row = row_of(r)
    sheet.clearSelection()
    sheet.selectRow(r)
    next(a for a in sheet.actions() if a.shortcut() == QKeySequence(QKeySequence.Copy)).trigger()     # Ctrl+C
    head, line = clip.text().split("\n")
    assert head.split("\t")[:3] == ["RT", "Name", "CAS"]
    cells = line.split("\t")
    assert cells[0] == copy_number(row["rt"]) and cells[1] == row["name"]
    assert cells[3] == copy_number(row["mean"]) != sheet.item(r, 4).text()


def test_the_peaks_panel_copies_every_digit(qtbot, win, samples, clip):
    from gcws.ui.models.peak_table import COLUMN_KEYS
    _load(qtbot, win, samples, ["07_"])
    table = win.table
    table.view.selectRow(0)
    table.copy()
    head, line = clip.text().split("\n")
    keys = [COLUMN_KEYS[c] for c in table._visible_columns()]
    values = dict(zip(keys, line.split("\t")))
    row = table.model.rows[table.proxy.mapToSource(table.proxy.index(0, 0)).row()]
    assert head.split("\t")[:2] == ["#", "RT [min]"]
    assert values["rt"] == copy_number(row.peak.apex_rt) and len(values["rt"]) > 6
    assert values["area"] == copy_number(row.peak.area) and "," not in values["area"]
    assert table.proxy.index(0, COLUMN_KEYS.index("rt")).data() == f"{row.peak.apex_rt:.3f}"   # shown rounded

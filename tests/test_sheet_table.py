"""SheetTable follows the order the columns are shown in (moved and hidden columns)."""
import pytest

pytest.importorskip("pytestqt")


def _table(qtbot):
    from PySide6.QtWidgets import QTableWidgetItem
    from gcws.ui.widgets.sheet_table import SheetTable
    t = SheetTable(2, 4)
    qtbot.addWidget(t)
    for r in range(2):
        for c in range(4):
            t.setItem(r, c, QTableWidgetItem(f"{r}{c}"))
    t.horizontalHeader().moveSection(3, 0)        # shown order 3, 0, 1, 2
    t.setColumnHidden(1, True)                    # shown: 3, 0, 2
    return t


def test_copy_follows_the_shown_order(qtbot):
    from PySide6.QtGui import QGuiApplication
    t = _table(qtbot)
    t.selectAll()
    t.copy_selection()
    assert QGuiApplication.clipboard().text() == "03\t00\t02\n13\t10\t12"


def test_a_pasted_block_goes_into_the_next_shown_columns(qtbot):
    t = _table(qtbot)
    edits = []
    t.bulkEdit.connect(edits.extend)
    t.setCurrentCell(0, 3)                         # shown first
    t.paste("a\tb\tc")
    assert edits == [(0, 3, "a"), (0, 0, "b"), (0, 2, "c")]


def test_the_fill_handle_repeats_a_marking_of_moved_columns(qtbot):
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    t = _table(qtbot)
    edits = []
    t.bulkEdit.connect(edits.extend)
    m = t.model()
    sel = QItemSelection(m.index(0, 3), m.index(0, 3))
    sel.select(m.index(0, 0), m.index(0, 0))       # shown next to each other
    t.selectionModel().select(sel, QItemSelectionModel.Select)
    assert t._handle_rect() is not None
    t.fill_range(t._block(), 1)
    assert sorted(edits) == [(1, 0, "00"), (1, 3, "03")]
    assert {(i.row(), i.column()) for i in t.selectedIndexes()} == {(0, 3), (0, 0), (1, 3), (1, 0)}

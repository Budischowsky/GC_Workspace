"""A QTableWidget that edits like a spreadsheet.

* arrow keys move the current cell, Shift+arrows / mouse drag mark a range;
* **Backspace** / **Enter** emit ``markRequested(rows, False / True)`` (e.g. the Report box);
* **Delete** emits ``deleteRequested(rows)`` (the owner deletes or restores the rows);
* **Space** on a check box emits ``toggleRequested(rows)``: the owner switches every marked row;
* **Ctrl+C** copies the marked cells as tab-separated text, **Ctrl+V** pastes one value into
  every marked cell or a block from the current cell on;
* **Ctrl+D** fills the marked cells of each column with the value of its top cell;
* the **fill handle** (small square at the bottom right of the marking) can be dragged down
  or up: the marked values are repeated into the cells passed over, as in Excel.

The table never changes cells itself: it emits ``bulkEdit([(row, column, value)])`` with
the visual row, the column and the new text (``bool`` for check boxes), and the owner
applies all of it as one undoable change.

Columns count in the order they are shown: moved columns are copied, pasted and filled as
they stand on the screen, and hidden columns are left out.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal as QtSignal
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter
from PySide6.QtWidgets import QAbstractItemView, QTableWidget

HANDLE = 7          # px, the fill handle's size


class SheetTable(QTableWidget):
    markRequested = QtSignal(list, bool)          # visual rows, on
    toggleRequested = QtSignal(list)              # visual rows: Space on a check box
    deleteRequested = QtSignal(list)              # visual rows: Delete
    bulkEdit = QtSignal(list)                     # [(visual row, column, value)]

    def __init__(self, rows: int = 0, columns: int = 0, parent=None):
        super().__init__(rows, columns, parent)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                             | QAbstractItemView.AnyKeyPressed)
        self._fill = None                         # (source ranges, current end row) while dragging the handle

    # -- helpers -------------------------------------------------------------------------

    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.selectedIndexes()} or
                      ({self.currentRow()} if self.currentRow() >= 0 else set()))

    def shown_columns(self, first: int = 0, last: int | None = None) -> list[int]:
        """The shown columns from screen position ``first`` to ``last`` (inclusive), left to right."""
        hh = self.horizontalHeader()
        last = self.columnCount() - 1 if last is None else last
        return [c for c in (hh.logicalIndex(v) for v in range(first, last + 1)) if not self.isColumnHidden(c)]

    def _block(self):
        """``(top, left, bottom, right)`` of the marking, the columns as screen positions, or None
        when it is not one rectangle."""
        idx = self.selectedIndexes()
        if not idx:
            return None
        hh = self.horizontalHeader()
        rows = {i.row() for i in idx}
        pos = {hh.visualIndex(i.column()) for i in idx}
        top, bottom, left, right = min(rows), max(rows), min(pos), max(pos)
        if len(idx) != (bottom - top + 1) * len(self.shown_columns(left, right)):
            return None
        return top, left, bottom, right

    @staticmethod
    def is_check(it) -> bool:
        """A check-box cell (QTableWidgetItem is user-checkable by default, so ask for a check state)."""
        return it is not None and it.data(Qt.CheckStateRole) is not None \
            and bool(it.flags() & Qt.ItemIsUserCheckable)

    def _editable(self, row: int, col: int) -> bool:
        it = self.item(row, col)
        if it is None or not it.flags() & Qt.ItemIsEnabled:
            return False
        return self.is_check(it) or bool(it.flags() & Qt.ItemIsEditable)

    def cell_value(self, row: int, col: int):
        it = self.item(row, col)
        if it is None:
            return ""
        if self.is_check(it):
            return it.checkState() == Qt.Checked
        return it.text()

    def _emit(self, changes):
        out = [(r, c, v) for r, c, v in changes if self._editable(r, c) and self.cell_value(r, c) != v]
        if out:
            self.bulkEdit.emit(out)

    # -- keys -------------------------------------------------------------------------------

    def keyPressEvent(self, ev):
        if self.state() == QAbstractItemView.EditingState:
            super().keyPressEvent(ev)
            return
        if ev.matches(QKeySequence.Copy):
            self.copy_selection()
        elif ev.matches(QKeySequence.Paste):
            self.paste()
        elif ev.key() == Qt.Key_D and ev.modifiers() == Qt.ControlModifier:
            self.fill_down()
        elif ev.key() == Qt.Key_Delete and not ev.modifiers():
            self.deleteRequested.emit(self.selected_rows())
        elif ev.key() == Qt.Key_Backspace and not ev.modifiers():
            self.markRequested.emit(self.selected_rows(), False)
        elif ev.key() in (Qt.Key_Return, Qt.Key_Enter) and not (ev.modifiers() & ~Qt.KeypadModifier):
            self.markRequested.emit(self.selected_rows(), True)
        elif ev.key() == Qt.Key_Space and not ev.modifiers() and self.is_check(self.currentItem()):
            self.toggleRequested.emit(self.selected_rows())
        else:
            super().keyPressEvent(ev)
            return
        ev.accept()

    def copy_selection(self):
        idx = self.selectedIndexes()
        if not idx:
            return
        rows = sorted({i.row() for i in idx})
        cols = sorted({i.column() for i in idx}, key=self.horizontalHeader().visualIndex)
        marked = {(i.row(), i.column()) for i in idx}

        def text(r, c):
            if (r, c) not in marked:
                return ""
            v = self.cell_value(r, c)
            return ("x" if v else "") if isinstance(v, bool) else str(v)
        QGuiApplication.clipboard().setText("\n".join("\t".join(text(r, c) for c in cols) for r in rows))

    def paste(self, text: str | None = None):
        text = QGuiApplication.clipboard().text() if text is None else text
        if not text:
            return
        lines = [ln.split("\t") for ln in text.rstrip("\r\n").replace("\r\n", "\n").split("\n")]
        if len(lines) == 1 and len(lines[0]) == 1:           # one value: into every marked cell
            targets = [(i.row(), i.column()) for i in self.selectedIndexes()] or \
                [(self.currentRow(), self.currentColumn())]
            self._emit([(r, c, self._coerce(r, c, lines[0][0])) for r, c in targets if r >= 0 and c >= 0])
            return
        r0, first = self.currentRow(), self.horizontalHeader().visualIndex(self.currentColumn())
        if self._block() is not None:
            r0, first = self._block()[:2]
        cols = self.shown_columns(first)
        changes = []
        for dr, cells in enumerate(lines):
            for dc, v in enumerate(cells):
                r = r0 + dr
                if r < self.rowCount() and dc < len(cols):
                    changes.append((r, cols[dc], self._coerce(r, cols[dc], v)))
        self._emit(changes)

    def _coerce(self, row, col, text):
        if self.is_check(self.item(row, col)):
            return text.strip().lower() not in ("", "0", "no", "nein", "false", "-")
        return text.strip()

    def fill_down(self):
        """Ctrl+D: every marked cell takes the value of the top marked cell of its column."""
        by_col: dict[int, list[int]] = {}
        for i in self.selectedIndexes():
            by_col.setdefault(i.column(), []).append(i.row())
        changes = []
        for c, rows in by_col.items():
            rows.sort()
            top = self.cell_value(rows[0], c)
            changes += [(r, c, top) for r in rows[1:]]
        self._emit(changes)

    # -- fill handle --------------------------------------------------------------------------

    def _handle_rect(self) -> QRect | None:
        b = self._block()
        if b is None:
            return None
        top, left, bottom, right = b
        rect = self.visualRect(self.model().index(bottom, self.horizontalHeader().logicalIndex(right)))
        if not rect.isValid():
            return None
        return QRect(rect.right() - HANDLE // 2, rect.bottom() - HANDLE // 2, HANDLE, HANDLE)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        h = self._handle_rect()
        if h is None:
            return
        p = QPainter(self.viewport())
        p.fillRect(h.adjusted(-1, -1, 1, 1), QColor(self.palette().base().color()))
        p.fillRect(h, self.palette().highlight().color())
        if self._fill is not None:
            b = self._fill[0]
            end = self._fill[1]
            lo, hi = min(b[0], end), max(b[2], end)
            hh = self.horizontalHeader()
            a = self.visualRect(self.model().index(lo, hh.logicalIndex(b[1])))
            z = self.visualRect(self.model().index(hi, hh.logicalIndex(b[3])))
            pen = p.pen()
            pen.setColor(self.palette().highlight().color())
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawRect(a.united(z).adjusted(0, 0, -1, -1))
        p.end()

    def mousePressEvent(self, ev):
        h = self._handle_rect()
        if ev.button() == Qt.LeftButton and h is not None and h.adjusted(-3, -3, 3, 3).contains(ev.position().toPoint()):
            b = self._block()
            self._fill = (b, b[2])
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._fill is not None:
            row = self.rowAt(ev.position().toPoint().y())
            if row < 0:
                row = self.rowCount() - 1 if ev.position().y() > 0 else 0
            b = self._fill[0]
            self._fill = (b, row if row < b[0] else max(row, b[2]))
            self.viewport().update()
            ev.accept()
            return
        h = self._handle_rect()
        over = h is not None and h.adjusted(-3, -3, 3, 3).contains(ev.position().toPoint())
        self.viewport().setCursor(Qt.CrossCursor if over else Qt.ArrowCursor)
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._fill is not None:
            b, end = self._fill
            self._fill = None
            self.fill_range(b, end)
            self.viewport().update()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def fill_range(self, block, end_row: int):
        """Repeat the rows of ``block`` (top, left, bottom, right; columns as screen positions) down
        (or up) to ``end_row``."""
        top, left, bottom, right = block
        n = bottom - top + 1
        if top <= end_row <= bottom:
            return
        targets = range(bottom + 1, end_row + 1) if end_row > bottom else range(end_row, top)
        cols = self.shown_columns(left, right)
        changes = []
        for r in targets:
            src = top + (r - top) % n
            for c in cols:
                changes.append((r, c, self.cell_value(src, c)))
        self._emit(changes)
        lo, hi = min(top, end_row), max(bottom, end_row)
        self.clearSelection()
        from PySide6.QtWidgets import QTableWidgetSelectionRange
        for c in cols:
            self.setRangeSelected(QTableWidgetSelectionRange(lo, c, hi, c), True)

"""The workflow chart editor: steps as cards, arrows between them, a filter on every arrow.

Steps are added from the palette (click, or drag onto the chart), moved by dragging, connected
by dragging from a step's right port onto the next step; double-click edits a step or an arrow,
Delete removes the selection. The checks list says what is still missing before the workflow
can run. Saving writes the workflow file the watcher reads.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QPainter, QPen, QColor, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (QCheckBox, QGraphicsLineItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QSplitter,
                               QToolBar, QVBoxLayout, QWidget)

from gcws.automation import workflow as W
from gcws.ui import theme
from gcws.ui.automation.items import EdgeItem, NodeItem, type_color

PALETTE_HELP = {
    "source": "The folder the instrument writes into. Checked every few minutes.",
    "method": "Processing method: integration, library search, ISTDs, quantification.",
    "report2": "Report²: accepted automatically, or control needed by the analyst.",
    "report": "The report and its files (Excel, Word, PDF, batch).",
    "folder": "Where the files go. Arrows into it may filter by format or status.",
}


class _Snapshot(QUndoCommand):
    """Undo step: the whole workflow before and after an edit."""

    def __init__(self, editor, before: dict, after: dict, text: str):
        super().__init__(text)
        self.editor, self.before, self.after = editor, before, after
        self._first = True

    def redo(self):
        if self._first:
            self._first = False
            return
        self.editor._load(W.Workflow.from_dict(self.after))

    def undo(self):
        self.editor._load(W.Workflow.from_dict(self.before))


class ChartView(QGraphicsView):
    dropped = Signal(str, QPointF)
    edge_double_clicked = Signal(str)

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setAcceptDrops(True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasText():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        ev.acceptProposedAction()

    def dropEvent(self, ev):
        kind = ev.mimeData().text()
        if kind in W.NODE_TYPES:
            self.dropped.emit(kind, self.mapToScene(ev.position().toPoint()))
            ev.acceptProposedAction()

    def mouseDoubleClickEvent(self, ev):
        it = self.itemAt(ev.position().toPoint())
        owner = getattr(it, "_edge", None) or (getattr(it.parentItem(), "_edge", None) if it is not None and
                                                it.parentItem() is not None else None)
        if owner is not None:
            it = owner
        if isinstance(it, EdgeItem):
            self.edge_double_clicked.emit(it.edge.id)
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
            self.scale(f, f)
        else:
            super().wheelEvent(ev)

    def drawBackground(self, qp, rect):
        qp.fillRect(rect, QColor(theme.BG))
        pen = QPen(QColor(theme.BORDER))
        pen.setWidthF(0)
        qp.setPen(pen)
        step = 40
        x0 = int(rect.left()) - int(rect.left()) % step
        y0 = int(rect.top()) - int(rect.top()) % step
        for x in range(x0, int(rect.right()) + step, step):
            for y in range(y0, int(rect.bottom()) + step, step):
                qp.drawPoint(x, y)


class PaletteList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        for kind, title in W.NODE_TYPES.items():
            it = QListWidgetItem(title)
            it.setData(Qt.UserRole, kind)
            it.setToolTip(PALETTE_HELP[kind])
            it.setForeground(type_color(kind))
            self.addItem(it)

    def mimeData(self, items):
        from PySide6.QtCore import QMimeData
        m = QMimeData()
        m.setText(items[0].data(Qt.UserRole) if items else "")
        return m


class WorkflowEditor(QMainWindow):
    saved = Signal(str)                                   # workflow id

    def __init__(self, wf: W.Workflow, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(f"Workflow - {wf.name}")
        self.resize(1280, 720)
        self.wf = wf
        self.dirty = False
        self.undo = QUndoStack(self)
        self.nodes: dict[str, NodeItem] = {}
        self.edges: dict[str, EdgeItem] = {}
        self._rubber: Optional[QGraphicsLineItem] = None
        self._rubber_src = ""
        from gcws.core import proc_method as PM
        self.method_names = PM.names()

        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(QRectF(-400, -300, 2800, 1600))
        self.view = ChartView(self.scene)
        self.view.dropped.connect(lambda kind, pos: self.add_node(kind, pos.x() - 100, pos.y() - 46))
        self.view.edge_double_clicked.connect(self.edit_edge)
        self.palette = PaletteList()
        self.palette.itemDoubleClicked.connect(self._add_from_palette)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("<b>Steps</b>"))
        ll.addWidget(self.palette, 1)
        ll.addWidget(theme.hint("Drag a step onto the chart (or double-click it). Connect steps by dragging from "
                                "the dot on the right of a step onto the next one. Double-click a step or an "
                                "arrow to set it up; Delete removes the selection."))
        self.checks = QListWidget()
        self.checks.setWordWrap(True)
        self.checks.itemClicked.connect(self._select_issue)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("<b>Checks</b>"))
        rl.addWidget(self.checks, 1)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.view)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([170, 900, 240])
        self.setCentralWidget(split)

        tb = QToolBar("Workflow")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" Name "))
        self.name = QLineEdit(wf.name)
        self.name.setMinimumWidth(260)
        self.name.editingFinished.connect(self._rename)
        tb.addWidget(self.name)
        self.enabled = QCheckBox("Active (the watcher runs it)")
        self.enabled.setChecked(wf.enabled)
        self.enabled.toggled.connect(self._toggle_enabled)
        tb.addWidget(self.enabled)
        tb.addSeparator()
        self.a_save = QAction("Save", self)
        self.a_save.setShortcut(QKeySequence.Save)
        self.a_save.triggered.connect(self.save)
        tb.addAction(self.a_save)
        a_undo = self.undo.createUndoAction(self, "Undo")
        a_undo.setShortcut(QKeySequence.Undo)
        a_redo = self.undo.createRedoAction(self, "Redo")
        a_redo.setShortcut(QKeySequence.Redo)
        tb.addAction(a_undo)
        tb.addAction(a_redo)
        a_del = QAction("Delete", self)
        a_del.setShortcut(QKeySequence.Delete)
        a_del.triggered.connect(self.delete_selected)
        tb.addAction(a_del)
        a_fit = QAction("Fit", self)
        a_fit.triggered.connect(self.fit)
        tb.addAction(a_fit)
        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        theme.notifier().changed.connect(self._restyle)
        self._load(wf)
        self.fit()

    def _restyle(self, *_):
        self.palette.clear()
        for kind, title in W.NODE_TYPES.items():
            it = QListWidgetItem(title)
            it.setData(Qt.UserRole, kind)
            it.setToolTip(PALETTE_HELP[kind])
            it.setForeground(type_color(kind))
            self.palette.addItem(it)
        self.scene.update()
        self.view.viewport().update()

    # -- building the scene --------------------------------------------------------------------------

    def _load(self, wf: W.Workflow):
        self.scene.clear()
        self.nodes.clear()
        self.edges.clear()
        self._rubber = None
        self.wf = wf
        for n in wf.nodes:
            self._node_item(n)
        for e in wf.edges:
            self._edge_item(e)
        self.validate()

    def _node_item(self, node: W.Node) -> NodeItem:
        it = NodeItem(node)
        it.moved.connect(self._moved)
        it.edit.connect(self.edit_node)
        it.connect_from.connect(self._start_connect)
        self.scene.addItem(it)
        self.nodes[node.id] = it
        return it

    def _edge_item(self, e: W.Edge) -> Optional[EdgeItem]:
        a, b = self.nodes.get(e.src), self.nodes.get(e.dst)
        if a is None or b is None:
            return None
        it = EdgeItem(e, a, b)
        self.scene.addItem(it)
        self.scene.addItem(it.tag)
        self.edges[e.id] = it
        return it

    def _change(self, text: str, fn):
        before = self.wf.to_dict()
        fn()
        after = self.wf.to_dict()
        if before != after:
            self.undo.push(_Snapshot(self, before, after, text))
            self.dirty = True
            self._load(W.Workflow.from_dict(after))

    # -- public API (also used by the tests) ----------------------------------------------------------

    def add_node(self, node_type: str, x: float = 0.0, y: float = 0.0, **params) -> W.Node:
        box = {}
        self._change(f"add {W.NODE_TYPES[node_type]}",
                     lambda: box.setdefault("n", self.wf.add_node(node_type, x, y, **params)))
        return self.wf.node(box["n"].id)

    def connect_nodes(self, src: str, dst: str) -> str:
        """Connect two steps; returns "" or why it is not allowed."""
        why = self.wf.can_connect(src, dst)
        if why:
            self.status.setText(why)
            return why
        self._change("connect", lambda: self.wf.connect(src, dst))
        return ""

    def set_node_params(self, node_id: str, params: dict):
        node = self.wf.node(node_id)
        if node is not None and params != node.params:
            self._change(f"edit {node.title}", lambda: node.params.update(params) or None)

    def set_edge_filter(self, edge_id: str, filt: dict):
        edge = self.wf.edge(edge_id)
        if edge is not None and filt != edge.filter:
            def fn():
                edge.filter = dict(filt)
            self._change("arrow filter", fn)

    def delete_selected(self):
        ids = [it.node.id for it in self.scene.selectedItems() if isinstance(it, NodeItem)]
        ids += [it.edge.id for it in self.scene.selectedItems() if isinstance(it, EdgeItem)]
        if ids:
            def fn():
                for i in ids:
                    self.wf.remove(i)
            self._change("delete", fn)

    def edit_node(self, node_id: str, dialog=None):
        from gcws.ui.automation.node_dialogs import NodeDialog
        node = self.wf.node(node_id)
        if node is None:
            return
        src = self.wf.source.p("folder") if self.wf.source else ""
        dlg = dialog or NodeDialog(node, self, self.method_names, src)
        if dialog is not None or dlg.exec():
            self.set_node_params(node_id, dlg.values())

    def edit_edge(self, edge_id: str, dialog=None):
        from gcws.ui.automation.node_dialogs import EdgeFilterDialog
        edge = self.wf.edge(edge_id)
        if edge is None:
            return
        dlg = dialog or EdgeFilterDialog(edge, self.wf, self)
        if dialog is not None or dlg.exec():
            self.set_edge_filter(edge_id, dlg.values())

    def validate(self) -> list:
        issues = W.validate(self.wf, method_names=self.method_names)
        self.checks.clear()
        for it in self.nodes.values():
            it.issue = ""
        for it in self.edges.values():
            it.issue = ""
        for i in issues:
            item = QListWidgetItem(("Error: " if i.level == "error" else "Note: ") + i.text)
            item.setData(Qt.UserRole, i.item)
            item.setForeground(theme.status_color("bad" if i.level == "error" else "warn"))
            self.checks.addItem(item)
            target = self.nodes.get(i.item) or self.edges.get(i.item)
            if target is not None and target.issue != "error":
                target.issue = i.level
        if not issues:
            ok = QListWidgetItem("The workflow is complete.")
            ok.setForeground(theme.status_color("ok"))
            self.checks.addItem(ok)
        self.scene.update()
        n_err = len(W.errors(issues))
        self.status.setText(f"{n_err} error(s), {len(issues) - n_err} note(s)" if issues else "Ready to run")
        return issues

    def save(self) -> bool:
        self._rename()
        issues = self.validate()
        if self.wf.enabled and W.errors(issues):
            self.wf.enabled = False
            self.enabled.blockSignals(True)
            self.enabled.setChecked(False)
            self.enabled.blockSignals(False)
            self.status.setText("Saved inactive: correct the errors first")
        self.wf.save()
        self.dirty = False
        self.saved.emit(self.wf.id)
        return True

    def fit(self):
        r = self.scene.itemsBoundingRect()
        if r.isValid() and not r.isEmpty():
            self.view.fitInView(r.adjusted(-60, -60, 60, 60), Qt.KeepAspectRatio)
            scale = self.view.transform().m11()
            if scale > 1.2 or scale < 0.72:          # readable cards: scroll rather than shrink
                self.view.resetTransform()
                if scale < 0.72:
                    self.view.scale(0.72, 0.72)
                self.view.centerOn(r.center())

    # -- interaction ---------------------------------------------------------------------------------

    def _add_from_palette(self, item):
        c = self.view.mapToScene(self.view.viewport().rect().center())
        self.add_node(item.data(Qt.UserRole), c.x() - 100, c.y() - 46)

    def _rename(self):
        name = self.name.text().strip() or "Workflow"
        if name != self.wf.name:
            def fn():
                self.wf.name = name
            self._change("rename", fn)
            self.setWindowTitle(f"Workflow - {name}")

    def _toggle_enabled(self, on):
        if on and W.errors(self.validate()):
            QMessageBox.information(self, "Workflow", "Correct the errors in the checks list first.")
            self.enabled.blockSignals(True)
            self.enabled.setChecked(False)
            self.enabled.blockSignals(False)
            return
        self.wf.enabled = bool(on)
        self.dirty = True

    def _moved(self, node_id, x, y):
        node = self.wf.node(node_id)
        if node is None:
            return
        before = self.wf.to_dict()
        node.x, node.y = x, y
        self.undo.push(_Snapshot(self, before, self.wf.to_dict(), "move"))
        self.dirty = True

    def _select_issue(self, item):
        target = self.nodes.get(item.data(Qt.UserRole)) or self.edges.get(item.data(Qt.UserRole))
        if target is not None:
            self.scene.clearSelection()
            target.setSelected(True)
            self.view.centerOn(target)

    def _start_connect(self, node_id, pos):
        self._rubber_src = node_id
        self._rubber = QGraphicsLineItem(pos.x(), pos.y(), pos.x(), pos.y())
        pen = QPen(QColor(theme.ACCENT), 2, Qt.DashLine)
        self._rubber.setPen(pen)
        self.scene.addItem(self._rubber)
        self.scene.installEventFilter(self)

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.scene and self._rubber is not None:
            if ev.type() == QEvent.GraphicsSceneMouseMove:
                line = self._rubber.line()
                self._rubber.setLine(line.x1(), line.y1(), ev.scenePos().x(), ev.scenePos().y())
                return True
            if ev.type() == QEvent.GraphicsSceneMouseRelease:
                target = next((it for it in self.scene.items(ev.scenePos()) if isinstance(it, NodeItem)), None)
                self.scene.removeItem(self._rubber)
                self._rubber = None
                self.scene.removeEventFilter(self)
                if target is not None and target.node.id != self._rubber_src:
                    self.connect_nodes(self._rubber_src, target.node.id)
                return True
        return super().eventFilter(obj, ev)

    def closeEvent(self, ev):
        if self.dirty:
            r = QMessageBox.question(self, "Workflow", f"Save the changes to '{self.wf.name}'?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                ev.ignore()
                return
            if r == QMessageBox.Save:
                self.save()
        super().closeEvent(ev)

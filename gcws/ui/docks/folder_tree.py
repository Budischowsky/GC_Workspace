"""Folder tree that recognises GC analysis folders and run (.D) folders."""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QSettings, QSortFilterProxyModel, Qt, Signal as QtSignal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFileSystemModel, QHBoxLayout,
                               QMenu, QToolButton, QTreeView, QVBoxLayout, QWidget, QCheckBox)

from gcws.io import folders
from gcws.io.metadata import read_metadata
from gcws.io.sequence import ROLE_LABELS, classify_role
from gcws.ui.icons import icon

ROLE_COLORS = {"sample": "#1F6F8B", "blank": "#6B7780", "blank_istd": "#11A579",
               "standard": "#7F3C8D", "ladder": "#D55E00"}


@lru_cache(maxsize=4096)
def _run_info(path: str, mtime: float) -> tuple[str, str]:
    try:
        meta = read_metadata(path)
        src = folders.sources(path)
        return meta.display_name, " / ".join(f"{k}: {v}" for k, v in src.items())
    except Exception:  # noqa: BLE001
        return Path(path).stem, ""


def _is_run(path: str) -> bool:
    return path.lower().endswith((".d", ".qgd")) and folders.is_run_dir(path)


class GCFileModel(QFileSystemModel):
    def data(self, index, role=Qt.DisplayRole):
        if index.column() == 0 and role in (Qt.DecorationRole, Qt.ToolTipRole):
            path = self.filePath(index)
            if _is_run(path):
                if role == Qt.DecorationRole:
                    r = classify_role(Path(path).name)
                    return icon("run", ROLE_COLORS.get(r, "#1F6F8B"))
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    mtime = 0
                name, src = _run_info(path, mtime)
                role_name = ROLE_LABELS.get(classify_role(Path(path).name), "")
                return f"{name}\nRole: {role_name}\n{src}\n{path}"
            if role == Qt.DecorationRole and self.isDir(index):
                return icon("analysis") if _has_runs(path) else icon("folder")
        return super().data(index, role)

    def hasChildren(self, parent=QModelIndex()):
        if parent.isValid() and self.filePath(parent).lower().endswith(".d"):
            return False
        return super().hasChildren(parent)


@lru_cache(maxsize=2048)
def _has_runs_cached(path: str, mtime: float) -> bool:
    return folders.is_analysis_folder(path)


def _has_runs(path: str) -> bool:
    try:
        return _has_runs_cached(path, os.path.getmtime(path))
    except OSError:
        return False


class DirsOnly(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.only_analysis = False

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        idx = model.index(row, 0, parent)
        if not model.isDir(idx):
            return model.fileName(idx).lower().endswith(".qgd")
        parent_path = model.filePath(parent)
        if parent_path.lower().endswith(".d"):
            return False
        name = model.fileName(idx)
        if name.startswith(".") or name.lower() in ("__pycache__", "acqdata"):
            return False
        return True

    def hasChildren(self, parent=QModelIndex()):
        src = self.mapToSource(parent)
        if src.isValid() and self.sourceModel().filePath(src).lower().endswith(".d"):
            return False
        return super().hasChildren(parent)


def default_root() -> str:
    s = QSettings()
    root = s.value("tree/root", "")
    if root and Path(root).is_dir():
        return root
    guess = Path(__file__).resolve().parents[4] / "NIAS Working" / "samples"
    return str(guess if guess.is_dir() else Path.home())


class FolderTree(QWidget):
    loadRequested = QtSignal(list, str)        # paths, role ("" = automatic)
    automationRequested = QtSignal(list)       # runs and folders for the automation queue

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = GCFileModel(self)
        self.model.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot | QDir.Drives)
        self.model.setReadOnly(True)
        self.proxy = DirsOnly(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setDynamicSortFilter(True)
        self.proxy.sort(0, Qt.AscendingOrder)

        self.view = QTreeView()
        self.view.setModel(self.proxy)
        for c in (1, 2, 3):
            self.view.hideColumn(c)
        self.view.setHeaderHidden(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        self.view.doubleClicked.connect(self._double)
        self.view.setAnimated(True)
        self.view.setUniformRowHeights(True)

        self.root_box = QComboBox()
        self.root_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.root_box.setMinimumContentsLength(10)
        self.root_box.setEditable(True)
        self.root_box.setToolTip("Root folder of the tree")
        self.root_box.activated.connect(lambda *_: self.set_root(self.root_box.currentText()))
        browse = QToolButton()
        browse.setIcon(icon("folder"))
        browse.setToolTip("Choose the root folder...")
        browse.clicked.connect(self._browse)
        up = QToolButton()
        up.setText("↑")
        up.setToolTip("Parent folder")
        up.clicked.connect(lambda: self.set_root(str(Path(self.root).parent)))
        self.load_btn = QToolButton()
        self.load_btn.setText("Load")
        self.load_btn.setToolTip("Load the selected runs")
        self.load_btn.clicked.connect(lambda: self._emit(self.selected_runs(), ""))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.root_box, 1)
        top.addWidget(up)
        top.addWidget(browse)
        top.addWidget(self.load_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)
        self.root = ""
        self.set_root(default_root())

    # -- root -----------------------------------------------------------------

    def set_root(self, path: str) -> None:
        path = str(Path(path))
        if not Path(path).is_dir():
            return
        self.root = path
        src_root = self.model.setRootPath(path)
        self.view.setRootIndex(self.proxy.mapFromSource(src_root))
        QSettings().setValue("tree/root", path)
        hist = [self.root_box.itemText(i) for i in range(self.root_box.count())]
        if path in hist:
            hist.remove(path)
        hist.insert(0, path)
        self.root_box.blockSignals(True)
        self.root_box.clear()
        self.root_box.addItems(hist[:12])
        self.root_box.setCurrentIndex(0)
        self.root_box.blockSignals(False)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Root folder", self.root)
        if d:
            self.set_root(d)

    def reveal(self, path) -> None:
        src = self.model.index(str(path))
        if src.isValid():
            idx = self.proxy.mapFromSource(src)
            self.view.scrollTo(idx)
            self.view.setCurrentIndex(idx)

    # -- selection and actions ---------------------------------------------------

    def _path(self, proxy_index) -> str:
        return self.model.filePath(self.proxy.mapToSource(proxy_index))

    def selected_paths(self) -> list[str]:
        rows = self.view.selectionModel().selectedRows(0)
        return [self._path(i) for i in rows]

    def selected_runs(self) -> list[str]:
        out = []
        for p in self.selected_paths():
            if _is_run(p):
                out.append(p)
            elif Path(p).is_dir():
                out.extend(str(r) for r in folders.list_runs(p))
        return out

    def _emit(self, paths, role):
        if paths:
            self.loadRequested.emit(list(paths), role)

    def _double(self, index):
        path = self._path(index)
        if _is_run(path):
            self._emit([path], "")

    def _menu(self, pos):
        paths = self.selected_paths()
        if not paths:
            return
        runs = [p for p in paths if _is_run(p)]
        m = QMenu(self)
        if runs:
            a = m.addAction(f"Load {len(runs)} chromatogram(s)" if len(runs) > 1 else "Load")
            a.triggered.connect(lambda: self._emit(runs, ""))
            sub = m.addMenu("Load as")
            for role, label in ROLE_LABELS.items():
                sub.addAction(label).triggered.connect(lambda _=False, r=role: self._emit(runs, r))
        folders_sel = [p for p in paths if not _is_run(p) and Path(p).is_dir()]
        if folders_sel:
            all_runs = [str(r) for f in folders_sel for r in folders.list_runs(f)]
            a = m.addAction(f"Load all runs in folder ({len(all_runs)})")
            a.setEnabled(bool(all_runs))
            a.triggered.connect(lambda: self._emit(all_runs, ""))
            m.addAction("Set as tree root").triggered.connect(lambda: self.set_root(folders_sel[0]))
        if runs or folders_sel:
            m.addSeparator()
            a = m.addAction("Send to Automation...")
            a.setToolTip("Put the selected samples into the automation queue; you choose the workflow")
            a.triggered.connect(lambda: self.automationRequested.emit(list(runs + folders_sel)))
        m.addSeparator()
        m.addAction("Open in Explorer").triggered.connect(lambda: self._explorer(paths[0]))
        m.exec(self.view.viewport().mapToGlobal(pos))

    @staticmethod
    def _explorer(path):
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(Path(path))])

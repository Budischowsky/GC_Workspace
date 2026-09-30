"""Inventory of everything the user manual has to explain.

Three sources, written to ``docs/manual/INVENTORY.md``:

* the running main window (offscreen, isolated settings): menus, toolbars and the controls of every panel;
* the source of ``gcws/ui`` (AST): texts of dialogs and right-click menus, which only exist while open;
* the settings dataclasses with their defaults, and the ``QSettings`` keys.

    .venv/Scripts/python.exe tools/ui_inventory.py
"""
from __future__ import annotations

import ast
import dataclasses
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "manual" / "INVENTORY.md"

#: dataclasses whose fields are settings the user can change
SETTINGS = [
    ("gcws.integration.method", "IntegrationMethod", "Integration method panel"),
    ("gcws.signal.blank", "BlankOptions", "Quantify > Blank subtraction settings"),
    ("gcws.ms.deconv", "DeconvSettings", "Deconvolution"),
    ("gcws.features.model", "Settings", "Double determination - settings"),
    ("gcws.core.proc_method", "SearchConfig", "Library search (processing method)"),
    ("gcws.automation.rules", "Rule", "Report² rule"),
]

#: calls whose string arguments are texts the user sees: name -> indices of the text arguments
TEXT_CALLS = {
    "addAction": (0, 1), "addMenu": (0, 1), "addRow": (0,), "addTab": (1,), "addItem": (0,), "addItems": (0,),
    "setToolTip": (0,), "setStatusTip": (0,), "setWindowTitle": (0,), "setPlaceholderText": (0,),
    "setHorizontalHeaderLabels": (0,), "setTabText": (1,), "setTitle": (0,), "setText": (0,),
    "QPushButton": (0, 1), "QCheckBox": (0,), "QRadioButton": (0,), "QToolButton": (0,), "QGroupBox": (0,),
    "QLabel": (0,), "QAction": (0, 1), "QMenu": (0,), "A": (0, 4), "_action": (0, 4), "hint": (0,),
    "button": (0, 1), "_button": (0, 1), "tool_button": (0, 1), "chip": (0,),
}


def clean(text) -> str:
    return " ".join(str(text or "").replace("&", "").split()).replace("|", "\\|")


def bare(text) -> str:
    return clean(text).rstrip(".… ").lower()


def tip_of(text, tip) -> str:
    """A tooltip that only repeats the text says nothing."""
    return "" if bare(tip) == bare(text) else clean(tip)


# -- the running window ----------------------------------------------------------------------

def menu_rows(menu, depth=0, seen=None):
    seen = set() if seen is None else seen
    if id(menu) in seen:
        return
    seen.add(id(menu))
    menu.aboutToShow.emit()                     # menus filled on opening (ISTDs, recent projects, layouts)
    for a in menu.actions():
        if a.isSeparator() or not clean(a.text()):
            continue
        tip = tip_of(a.text(), a.statusTip() or a.toolTip())
        flags = "check" if a.isCheckable() else ""
        if a.menu() is not None:
            flags = "submenu"
        yield depth, clean(a.text()), a.shortcut().toString(), flags, tip
        if a.menu() is not None:
            yield from menu_rows(a.menu(), depth + 1, seen)


def label_of(widget) -> str:
    """The form label in front of an input that has no text of its own."""
    from PySide6.QtWidgets import QFormLayout, QLabel
    parent = widget.parentWidget()
    while parent is not None:
        lay = parent.layout()
        for form in ([lay] if isinstance(lay, QFormLayout) else []) + parent.findChildren(QFormLayout):
            lab = form.labelForField(widget)
            if isinstance(lab, QLabel):
                return clean(lab.text())
        parent = parent.parentWidget()
    return ""


def control_rows(root):
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QComboBox, QGroupBox, QLineEdit, QTabWidget,
                                   QWidget)
    seen, texts = set(), {"", bare(root.windowTitle())}
    for w in root.findChildren(QWidget):
        kind = text = value = ""
        if isinstance(w, QAbstractButton):
            kind, text = type(w).__name__.replace("Q", "", 1), clean(w.text())
            if w.isCheckable():
                value = "on" if w.isChecked() else "off"
        elif isinstance(w, QComboBox):
            kind, text = "ComboBox", label_of(w)
            items = [clean(w.itemText(i)) for i in range(min(w.count(), 12))]
            value = " / ".join(items) + (" ..." if w.count() > 12 else "")
            if ":\\" in value or ":/" in value:
                value = "(folders of this computer)"
        elif isinstance(w, QAbstractSpinBox):
            if w.parentWidget() is not None and w.parentWidget().metaObject().className() == "QAbstractItemView":
                continue
            kind, text, value = "Number", label_of(w), clean(w.text())
        elif isinstance(w, QLineEdit):
            if isinstance(w.parentWidget(), (QAbstractSpinBox, QComboBox)):
                continue
            kind, text, value = "Text", label_of(w) or clean(w.placeholderText()), ""
        elif isinstance(w, QGroupBox):
            kind, text = "Group", clean(w.title())
        elif isinstance(w, QTabWidget):
            kind, text = "Tabs", " / ".join(clean(w.tabText(i)) for i in range(w.count()))
        else:
            continue
        tip = clean(w.toolTip())
        if not (text or tip):
            continue
        row = (kind, text, value, tip_of(text, tip))
        if row not in seen:
            seen.add(row)
            texts.add(bare(text))
            yield row
    for a in root.findChildren(QAction):
        if a.isSeparator() or bare(a.text()) in texts:       # a button of the panel already shows it
            continue
        texts.add(bare(a.text()))
        yield "Menu item", clean(a.text()), a.shortcut().toString(), tip_of(a.text(), a.toolTip())


def live(out):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    tmp = tempfile.mkdtemp(prefix="gcws_inventory_")
    os.environ["GCWS_DATA"] = tmp
    sys.path.insert(0, str(ROOT))
    import gcws  # noqa: F401
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QToolBar
    QCoreApplication.setOrganizationName("GCWorkspaceInventory")
    QCoreApplication.setApplicationName("inventory")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp)
    app = QApplication.instance() or QApplication([])
    from gcws.ui.main_window import DOCKS, MainWindow
    win = MainWindow()

    out.append("## 1 Menus\n")
    for top in win.menuBar().actions():
        if top.menu() is None:
            continue
        out.append(f"### Menu: {clean(top.text())}\n")
        out.append("| Item | Shortcut | Kind | Tip |\n|---|---|---|---|")
        for depth, text, key, flags, tip in menu_rows(top.menu()):
            out.append(f"| {'&nbsp;&nbsp;→ ' * depth}{text} | {key} | {flags} | {tip} |")
        out.append("")

    out.append("## 2 Toolbars\n")
    for tb in win.findChildren(QToolBar):
        if tb.parentWidget() is not win:
            continue
        out.append(f"### Toolbar: {clean(tb.windowTitle())}\n")
        out.append("| Button | Shortcut | Tip |\n|---|---|---|")
        for a in tb.actions():
            if not a.isSeparator() and clean(a.text()):
                out.append(f"| {clean(a.text())} | {a.shortcut().toString()} | {tip_of(a.text(), a.toolTip())} |")
        out.append("")

    out.append("## 3 Panels\n")
    for key, title in DOCKS:
        dock = win.docks[key]
        out.append(f"### Panel: {title}\n")
        out.append("| Control | Label | State / choices | Tip |\n|---|---|---|---|")
        for kind, text, value, tip in control_rows(dock):
            out.append(f"| {kind} | {text} | {value} | {tip} |")
        out.append("")

    out.append("## 4 Tools (mouse modes)\n")
    from gcws.ui.plot.tools import TOOLS
    out.append("| Tool | Key | What it does |\n|---|---|---|")
    for name, label, key, tip in TOOLS:
        out.append(f"| {label} | {key} | {clean(tip)} |")
    out.append("")
    win.ws.dirty = False
    win.close()
    app.processEvents()


# -- the source ------------------------------------------------------------------------------

def strings(node):
    """String constants of an expression (also inside a list, tuple, f-string or a + b)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        yield "".join(v.value if isinstance(v, ast.Constant) else "{…}" for v in node.values)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for e in node.elts:
            yield from strings(e)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        parts = list(strings(node.left)) + list(strings(node.right))
        if parts:
            yield "".join(parts)


class Texts(ast.NodeVisitor):
    def __init__(self):
        self.scope: list[str] = []
        self.rows: list[tuple[str, str, str, int]] = []

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = _scoped

    def visit_Call(self, node):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        for i in TEXT_CALLS.get(name, ()):
            if i < len(node.args):
                for s in strings(node.args[i]):
                    if len(clean(s)) > 1:
                        self.rows.append((".".join(self.scope[:2]), name, clean(s), node.lineno))
        self.generic_visit(node)


def static(out):
    out.append("## 5 Texts in the source (dialogs, right-click menus, panels)\n")
    out.append("Every text a user can see, by file and class. Dialogs and right-click menus only appear here.\n")
    for path in sorted((ROOT / "gcws" / "ui").rglob("*.py")):
        v = Texts()
        v.visit(ast.parse(path.read_text(encoding="utf-8")))
        if not v.rows:
            continue
        out.append(f"### {path.relative_to(ROOT).as_posix()}\n")
        out.append("| Where | Call | Text | Line |\n|---|---|---|---|")
        for scope, call, text, line in v.rows:
            out.append(f"| {scope} | {call} | {text} | {line} |")
        out.append("")


def settings(out):
    import importlib
    out.append("## 6 Settings and their defaults\n")
    for module, name, where in SETTINGS:
        cls = getattr(importlib.import_module(module), name)
        out.append(f"### {name} ({module}) - {where}\n")
        out.append("| Field | Default |\n|---|---|")
        for f in dataclasses.fields(cls):
            if f.default is not dataclasses.MISSING:
                value = f.default
            elif f.default_factory is not dataclasses.MISSING:
                value = f.default_factory()
            else:
                value = "(required)"
            out.append(f"| {f.name} | {clean(repr(value))[:160]} |")
        out.append("")

    out.append("### Remembered choices (QSettings)\n")
    out.append("| Key | Default | File |\n|---|---|---|")
    rows = set()
    for path in sorted((ROOT / "gcws").rglob("*.py")):
        if "nias_legacy" in path.parts or "vendor" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "value"
                    and node.args and isinstance(node.args[0], (ast.Constant, ast.JoinedStr))):
                key = next(strings(node.args[0]), "")
                if "/" not in key:
                    continue
                default = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
                rows.add((key, clean(default), path.relative_to(ROOT).as_posix()))
    for key, default, file in sorted(rows):
        out.append(f"| {key} | {default} | {file} |")
    out.append("")


def main():
    out = ["# Inventory for the user manual\n",
           "Generated by `tools/ui_inventory.py` - do not edit; run the tool again after a change of the "
           "interface.\n"]
    live(out)
    static(out)
    settings(out)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"{OUT.relative_to(ROOT)}: {len(out)} lines")


if __name__ == "__main__":
    main()

"""The manual explains the whole interface: a new menu item, button or setting without an entry fails here.

What the user can see is collected the way ``tools/ui_inventory.py`` does it (the running window and
the source of the dialogs) and looked up in the text of the manual.
"""
import ast
import dataclasses
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ui_inventory as INV  # noqa: E402
from gcws import manual as M  # noqa: E402

pytest.importorskip("pytestqt")

#: menus filled with the user's own things (projects, layouts, standards, libraries)
DYNAMIC_MENUS = {"Recent projects", "Saved layouts", "Delete saved layout", "Set selected peak as ISTD",
                 "Own library selection and options"}
#: texts every user knows, and texts that only repeat a state
PLAIN = {"ok", "cancel", "close", "apply", "save", "delete", "browse", "search", "load", "stopped", "and", "value",
         "chosen", "name", "automatic", "remove", "edit", "export", "new", "options", "find", "zoom", "library"}
#: calls of the dialogs' source whose text is a control the manual has to name
CONTROLS = {"QPushButton", "QCheckBox", "QRadioButton", "QGroupBox", "addRow", "addTab", "setWindowTitle",
            "addAction", "addMenu", "QAction", "QMenu"}
#: settings that have no control: a new field has to be added here or to the manual
NOT_IN_A_WINDOW = {
    "IntegrationMethod": {"name", "description", "smoothing_order", "min_width_fraction", "exp_skim_r2",
                          "baseline_window", "baseline_tolerance", "solvent_height_factor", "timed_events",
                          "version"},
    "BlankOptions": set(),
    "DeconvSettings": set(),
    "Features": {"pairing", "w_rt", "noise_floor", "ambiguity", "gap_shape_r", "gap_min_points", "gap_int_tol",
                 "id_topk"},
}


def words(text: str) -> str:
    """Lower case, letters and digits only: how a label is looked up."""
    text = re.sub(r"\(\d+\)", "", str(text))                      # a count in a tab title
    return " ".join(re.findall(r"[a-z0-9²µ½%]+", text.lower().replace("&", "")))


@pytest.fixture(scope="module")
def manual_text() -> str:
    return " " + words(" ".join(M.plain(c.markdown) for c in M.chapters())) + " "


def missing(labels, manual_text: str) -> list[str]:
    """The labels the manual does not name (an explanation in brackets after a label may be left out)."""
    out = []
    for label in labels:
        w = words(label)
        if not w or w in PLAIN or len(w) < 2 or "{…}" in label:
            continue
        short = words(re.sub(r"\s*\([^)]*\)\s*$", "", label))
        if f" {w} " not in manual_text and not (short and f" {short} " in manual_text):
            out.append(label)
    return sorted(set(out))


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QMessageBox
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    QSettings().clear()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    from gcws.ui.main_window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    yield w
    w.ws.dirty = False
    w.close()


def _menu_labels(menu, inside_dynamic=False):
    for a in menu.actions():
        text = INV.clean(a.text())
        if a.isSeparator() or not text:
            continue
        if not inside_dynamic:
            yield text
        if a.menu() is not None:
            yield from _menu_labels(a.menu(), inside_dynamic or text in DYNAMIC_MENUS)


def test_every_menu_item_is_explained(win, manual_text):
    labels = []
    for top in win.menuBar().actions():
        labels.append(INV.clean(top.text()))
        labels += list(_menu_labels(top.menu()))
    assert len(labels) > 100
    assert missing(labels, manual_text) == []


def test_every_toolbar_button_and_tool_is_explained(win, manual_text):
    from PySide6.QtWidgets import QToolBar
    from gcws.ui.plot.tools import TOOLS
    labels = [INV.clean(a.text()) for tb in win.findChildren(QToolBar) if tb.parentWidget() is win
              for a in tb.actions() if not a.isSeparator()]
    labels += [label for _name, label, _key, _tip in TOOLS]
    assert len(labels) > 15
    assert missing(labels, manual_text) == []
    keys = [key for _name, _label, key, _tip in TOOLS]
    text = " ".join(c.markdown for c in M.chapters())
    assert [k for k in keys if f"`{k}`" not in text] == []


def test_every_panel_control_is_explained(win, manual_text):
    from gcws.ui.main_window import DOCKS
    labels = [title for _key, title in DOCKS]
    for key, _title in DOCKS:
        for kind, text, _value, _tip in INV.control_rows(win.docks[key]):
            if kind in ("Tabs",):
                labels += text.split(" / ")
            elif kind in ("Number", "Text", "ComboBox"):
                labels.append(text)                       # the form label in front of it
            else:
                labels.append(text.split(" · ")[0])       # "Chromatogram 1 · FID": the signal is a state
    assert len(labels) > 120
    assert missing(labels, manual_text) == []


def test_every_dialog_control_is_explained(manual_text):
    labels = []
    for folder in ("dialogs", "automation"):
        for path in sorted((ROOT / "gcws" / "ui" / folder).glob("*.py")):
            if path.name == "manual.py":                   # the manual's own window
                continue
            v = INV.Texts()
            v.visit(ast.parse(path.read_text(encoding="utf-8")))
            labels += [text for _scope, call, text, _line in v.rows if call in CONTROLS]
    assert len(labels) > 200
    assert missing(labels, manual_text) == []


def test_every_right_click_menu_item_is_explained(manual_text):
    placeholders = {"(no library - add one under Identify > Libraries...)", "No internal standards defined",
                    "no other sample loaded"}                  # shown instead of an empty menu
    labels = []
    for path in sorted((ROOT / "gcws" / "ui").rglob("*.py")):
        if path.parent.name in ("dialogs", "automation"):
            continue
        v = INV.Texts()
        v.visit(ast.parse(path.read_text(encoding="utf-8")))
        labels += [text for _scope, call, text, _line in v.rows
                   if call in ("addAction", "addMenu", "QAction", "QMenu") and text not in placeholders]
    labels += ["Name: ...", "Remove the gap fill", "Harmonise the boundaries",      # double determination rows
               "Delete row", "Restore row"]
    assert len(labels) > 80
    assert missing(labels, manual_text) == []


def test_every_setting_with_a_control_has_its_default_in_the_manual():
    used = {(name, attr) for c in M.chapters() for name, attr in M.placeholders(c.source) if attr}
    for name, cls in M._sources().items():
        fields = {f.name for f in dataclasses.fields(cls)}
        assert NOT_IN_A_WINDOW[name] <= fields, f"{name}: a setting named here no longer exists"
        absent = sorted(fields - NOT_IN_A_WINDOW[name] - {attr for n, attr in used if n == name})
        assert absent == [], f"{name}: no default in the manual for {absent}"


def test_links_open_their_section(qtbot, tmp_path):
    from PySide6.QtCore import QCoreApplication, QSettings, QUrl
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    from gcws.ui import theme
    theme.ensure_applied()
    from gcws.ui.dialogs.manual import ManualWindow
    w = ManualWindow()
    qtbot.addWidget(w)
    seen = 0
    for ch in M.chapters():
        w.open(ch.slug)
        hrefs, block = [], w.view.document().begin()
        while block.isValid():                                   # the links as the text browser has them
            it = block.begin()
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                if fmt.isAnchor() and fmt.anchorHref() and not fmt.anchorHref().startswith("http"):
                    hrefs.append(fmt.anchorHref())
                it += 1
            block = block.next()
        for href in dict.fromkeys(hrefs):
            w.open(ch.slug, remember=False)
            w._link(QUrl(href))
            slug, section = w.current
            assert (slug, section) == M.split_target(href, ch.slug), f"{ch.slug}: {href}"
            assert M.chapter(slug) is not None, f"{ch.slug}: {href}"
            assert not section or w.view.heading_block(section) is not None, f"{ch.slug}: {href}"
            seen += 1
    assert seen > 40
    w.close()

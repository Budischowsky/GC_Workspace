"""Layout presets and named user layouts."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings, Qt
from PySide6.QtGui import QGuiApplication

L, R, T, B = (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea, Qt.TopDockWidgetArea, Qt.BottomDockWidgetArea)
H, V = Qt.Horizontal, Qt.Vertical
LAYOUT_VERSION = 3

PRESETS = {
    "Chromatogram top": "Chromatogram 1 and 2 wide on top, peak table and spectrum below",
    "Table left (classic)": "Peak table on the left, Chromatogram 1 and 2 and the spectrum stacked on the right",
    "Integration": "Chromatogram 1 and 2 with the integration events for working on the integration",
    "Review": "Chromatogram 1 and 2, table, spectrum and replicates for reviewing results; tree hidden",
    "Dual monitor - table detached": "Chromatogram here; peak table and spectrum on the second screen",
}


#: small reference panels share the Folders column; the work panels share the peak table's room
SIDE = ("events", "props", "audit")
CORE = ("tree", "chrom", "zoom", "table", "spectrum")


def wide_keys(win) -> list[str]:
    """Quantification, Replicates, Automation, Report² and any later panel: they need width."""
    return [k for k in win.docks if k not in CORE and k not in SIDE]


def home(key: str) -> str:
    """The panel a dock is tabbed with in the presets."""
    return "tree" if key in SIDE else "table"


def _reset(win):
    for d in win.docks.values():
        d.setFloating(False)
        win.removeDockWidget(d)


def _show(*docks):
    for d in docks:
        d.show()


def _tab_group(win, anchor, docks):
    prev = anchor
    for x in docks:
        win.tabifyDockWidget(prev, x)
        prev = x


def apply_preset(win, name: str) -> None:
    d = win.docks
    tree, chrom, zoom, table, spec = d["tree"], d["chrom"], d["zoom"], d["table"], d["spectrum"]
    events, props, audit = d["events"], d["props"], d["audit"]
    side = [events, props, audit]
    wide = [d[k] for k in wide_keys(win)]
    _reset(win)
    w, h = win.width(), win.height()
    if name == "Table left (classic)":
        win.addDockWidget(L, tree)
        win.addDockWidget(R, table)
        win.splitDockWidget(table, chrom, H)
        win.splitDockWidget(chrom, zoom, V)
        win.splitDockWidget(zoom, spec, V)
        _tab_group(win, tree, side)
        _tab_group(win, table, wide)
        _show(tree, table, chrom, zoom, spec, *side, *wide)
        tree.raise_()
        table.raise_()
        win.resizeDocks([tree, table, chrom], [int(w * 0.16), int(w * 0.38), int(w * 0.46)], H)
        win.resizeDocks([chrom, zoom, spec], [int(h * 0.36), int(h * 0.30), int(h * 0.34)], V)
    elif name == "Integration":
        win.addDockWidget(L, tree)
        win.addDockWidget(R, chrom)
        win.splitDockWidget(chrom, zoom, V)
        win.splitDockWidget(zoom, table, V)
        win.splitDockWidget(chrom, events, H)
        _tab_group(win, tree, [props, audit])
        _tab_group(win, table, [spec, *wide])
        _show(tree, chrom, zoom, events, table, spec, props, audit, *wide)
        tree.hide()
        props.hide()
        audit.hide()
        table.raise_()
        win.resizeDocks([chrom, zoom, table], [int(h * 0.35), int(h * 0.35), int(h * 0.3)], V)
        win.resizeDocks([chrom, events], [int(w * 0.68), int(w * 0.32)], H)
    elif name == "Review":
        win.addDockWidget(R, chrom)
        win.splitDockWidget(chrom, zoom, V)
        win.splitDockWidget(zoom, table, V)
        win.splitDockWidget(table, spec, H)
        win.addDockWidget(L, tree)
        _tab_group(win, spec, side)
        _tab_group(win, table, wide)
        _show(chrom, table, spec, zoom, *side, *wide)
        tree.hide()
        for x in side:
            x.hide()
        spec.raise_()
        (d.get("replicates") or table).raise_()
        win.resizeDocks([chrom, zoom, table], [int(h * 0.3), int(h * 0.25), int(h * 0.45)], V)
        win.resizeDocks([table, spec], [int(w * 0.62), int(w * 0.38)], H)
    elif name == "Dual monitor - table detached":
        apply_preset(win, "Chromatogram top")
        screens = QGuiApplication.screens()
        other = next((s for s in screens if s != win.screen()), None)
        geo = (other or win.screen()).availableGeometry()
        for dock, rect in ((table, (geo.x() + 20, geo.y() + 40, int(geo.width() * 0.62) - 30, geo.height() - 80)),
                           (spec, (geo.x() + int(geo.width() * 0.62), geo.y() + 40,
                                   int(geo.width() * 0.38) - 20, geo.height() - 80))):
            dock.setFloating(True)
            dock.setGeometry(*rect)
            dock.show()
        return
    else:  # "Chromatogram top" (default)
        win.addDockWidget(L, tree)
        win.addDockWidget(R, chrom)
        win.splitDockWidget(chrom, zoom, V)
        win.splitDockWidget(zoom, table, V)
        win.splitDockWidget(table, spec, H)
        _tab_group(win, tree, side)
        _tab_group(win, table, wide)
        _show(tree, chrom, table, zoom, spec, *side, *wide)
        tree.raise_()
        table.raise_()
        win.resizeDocks([tree, chrom], [int(w * 0.15), int(w * 0.85)], H)
        win.resizeDocks([chrom, zoom, table], [int(h * 0.3), int(h * 0.26), int(h * 0.44)], V)
        win.resizeDocks([table, spec], [int(w * 0.5), int(w * 0.35)], H)


# -- named user layouts ---------------------------------------------------------

def saved_layouts() -> list[str]:
    s = QSettings()
    s.beginGroup("layouts")
    names = s.childGroups()
    s.endGroup()
    return sorted(names, key=str.casefold)


def save_layout(win, name: str) -> None:
    s = QSettings()
    s.setValue(f"layouts/{name}/state", win.saveState(LAYOUT_VERSION))
    s.setValue(f"layouts/{name}/geometry", win.saveGeometry())
    win.save_view_preferences(f"layouts/{name}")


def restore_layout(win, name: str) -> bool:
    s = QSettings()
    state = s.value(f"layouts/{name}/state")
    if not isinstance(state, QByteArray):
        return False
    geo = s.value(f"layouts/{name}/geometry")
    if isinstance(geo, QByteArray):
        win.restoreGeometry(geo)
    win.sidebar.expand_all()
    restored = bool(win.restoreState(state, LAYOUT_VERSION))
    if restored:
        place_missing(win, state)
        ensure_on_screen(win)
        win.restore_view_preferences(f"layouts/{name}")
    return restored


def place_missing(win, state) -> list[str]:
    """Panels the saved state does not know (added in a later version) go to their usual tab group;
    Qt would leave them wherever the previous arrangement had them."""
    raw = bytes(state)
    placed = []
    for key, dock in win.docks.items():
        if dock.objectName().encode("utf-16-be") in raw:
            continue
        anchor = win.docks.get(home(key))
        dock.setFloating(False)
        if anchor is not None and anchor is not dock and not anchor.isFloating() and not anchor.isHidden():
            group = [anchor] + [d for d in win.tabifiedDockWidgets(anchor) if d is not dock]
            front = next((d for d in group if d.isVisible()), anchor)
            win.tabifyDockWidget(anchor, dock)
            dock.show()
            front.raise_()                       # the new panel waits behind its tab
        else:
            win.addDockWidget(R, dock)
            dock.show()
        placed.append(key)
    return placed


def ensure_on_screen(win) -> None:
    """A detached panel saved on a screen that is no longer there comes back onto the window's screen."""
    screens = [s.availableGeometry() for s in QGuiApplication.screens()]
    target = (win.screen() or QGuiApplication.primaryScreen()).availableGeometry()
    for dock in win.docks.values():
        if not dock.isFloating():
            continue
        frame = dock.frameGeometry()
        if any(frame.intersected(g).width() >= 100 and frame.intersected(g).height() >= 50 for g in screens):
            continue
        w = min(dock.width(), target.width() - 80)
        h = min(dock.height(), target.height() - 120)
        dock.setGeometry(target.x() + 40, target.y() + 60, w, h)


def delete_layout(name: str) -> None:
    s = QSettings()
    s.remove(f"layouts/{name}")
    if current() == ("saved", name):
        set_current(None, "")


def current() -> tuple[str, str] | None:
    """The layout chosen last: ("preset" | "saved", name), or None when it no longer exists."""
    s = QSettings()
    kind, name = s.value("layout/current_kind", "", type=str), s.value("layout/current_name", "", type=str)
    if (kind == "preset" and name in PRESETS) or (kind == "saved" and name in saved_layouts()):
        return kind, name
    return None


def set_current(kind: str | None, name: str) -> None:
    s = QSettings()
    if kind is None:
        s.remove("layout/current_kind")
        s.remove("layout/current_name")
    else:
        s.setValue("layout/current_kind", kind)
        s.setValue("layout/current_name", name)

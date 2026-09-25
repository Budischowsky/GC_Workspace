"""Layout presets and named user layouts."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings, Qt
from PySide6.QtGui import QGuiApplication

L, R, T, B = (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea, Qt.TopDockWidgetArea, Qt.BottomDockWidgetArea)
H, V = Qt.Horizontal, Qt.Vertical
LAYOUT_VERSION = 3

PRESETS = {
    "Chromatogram top": "Wide chromatogram on top, peak table with peak zoom and spectrum below",
    "Table left (classic)": "Peak table on the left, chromatogram, peak zoom and spectrum stacked on the right",
    "Integration": "Chromatogram, peak zoom and integration events for working on the integration",
    "Review": "Chromatogram, table, spectrum and replicates for reviewing results; tree hidden",
    "Dual monitor - table detached": "Chromatogram here; peak table and spectrum on the second screen",
}


def _reset(win):
    for d in win.docks.values():
        d.setFloating(False)
        win.removeDockWidget(d)


def _show(*docks):
    for d in docks:
        d.show()


def apply_preset(win, name: str) -> None:
    d = win.docks
    tree, chrom, zoom, table, spec = d["tree"], d["chrom"], d["zoom"], d["table"], d["spectrum"]
    events, props, audit = d["events"], d["props"], d["audit"]
    extra = [x for k, x in d.items() if k not in ("tree", "chrom", "zoom", "table", "spectrum",
                                                    "events", "props", "audit")]
    _reset(win)
    w, h = win.width(), win.height()
    if name == "Table left (classic)":
        win.addDockWidget(L, tree)
        win.addDockWidget(R, table)
        win.splitDockWidget(table, chrom, H)
        win.splitDockWidget(chrom, zoom, V)
        win.splitDockWidget(zoom, spec, V)
        win.tabifyDockWidget(tree, events)
        win.tabifyDockWidget(events, props)
        win.tabifyDockWidget(props, audit)
        for x in extra:
            win.tabifyDockWidget(audit, x)
        _show(tree, table, chrom, zoom, spec, events, props, audit, *extra)
        tree.raise_()
        win.resizeDocks([tree, table, chrom], [int(w * 0.16), int(w * 0.38), int(w * 0.46)], H)
        win.resizeDocks([chrom, zoom, spec], [int(h * 0.36), int(h * 0.30), int(h * 0.34)], V)
    elif name == "Integration":
        win.addDockWidget(L, tree)
        win.addDockWidget(R, chrom)
        win.splitDockWidget(chrom, zoom, V)
        win.splitDockWidget(zoom, events, H)
        win.splitDockWidget(chrom, table, V)
        win.tabifyDockWidget(tree, props)
        win.tabifyDockWidget(table, spec)
        win.tabifyDockWidget(spec, audit)
        for x in extra:
            win.tabifyDockWidget(audit, x)
        _show(tree, chrom, zoom, events, table, spec, props, audit, *extra)
        tree.hide()
        props.hide()
        table.raise_()
        win.resizeDocks([chrom, zoom, table], [int(h * 0.4), int(h * 0.35), int(h * 0.25)], V)
        win.resizeDocks([zoom, events], [int(w * 0.6), int(w * 0.4)], H)
    elif name == "Review":
        win.addDockWidget(R, chrom)
        win.splitDockWidget(chrom, table, V)
        win.splitDockWidget(table, spec, H)
        win.addDockWidget(L, tree)
        win.tabifyDockWidget(spec, zoom)
        for x in [events, props, audit] + extra:
            win.tabifyDockWidget(zoom, x)
        _show(chrom, table, spec, zoom, events, props, audit, *extra)
        tree.hide()
        events.hide()
        props.hide()
        spec.raise_()
        rep = d.get("replicates")
        if rep is not None:
            rep.raise_()
        win.resizeDocks([chrom, table], [int(h * 0.4), int(h * 0.6)], V)
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
        win.splitDockWidget(chrom, table, V)
        win.splitDockWidget(table, zoom, H)
        win.splitDockWidget(zoom, spec, H)
        win.tabifyDockWidget(tree, events)
        win.tabifyDockWidget(events, props)
        win.tabifyDockWidget(props, audit)
        for x in extra:
            win.tabifyDockWidget(audit, x)
        _show(tree, chrom, table, zoom, spec, events, props, audit, *extra)
        tree.raise_()
        win.resizeDocks([tree, chrom], [int(w * 0.15), int(w * 0.85)], H)
        win.resizeDocks([chrom, table], [int(h * 0.42), int(h * 0.58)], V)
        win.resizeDocks([table, zoom, spec], [int(w * 0.36), int(w * 0.22), int(w * 0.27)], H)


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


def restore_layout(win, name: str) -> bool:
    s = QSettings()
    state = s.value(f"layouts/{name}/state")
    if not isinstance(state, QByteArray):
        return False
    geo = s.value(f"layouts/{name}/geometry")
    if isinstance(geo, QByteArray):
        win.restoreGeometry(geo)
    return bool(win.restoreState(state, LAYOUT_VERSION))


def delete_layout(name: str) -> None:
    s = QSettings()
    s.remove(f"layouts/{name}")

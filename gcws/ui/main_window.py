"""Main window: dock panels, loaded samples, menus and workflows."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDockWidget, QFileDialog, QInputDialog, QLabel,
                               QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QToolBar, QWidget)

import gcws
from gcws.core import project as P
from gcws.core.model import FID, TIC, eic_key
from gcws.io.sequence import ROLE_LABELS
from gcws.ui import workers
from gcws.ui.docks.audit import AuditDock
from gcws.ui.docks.events import EventsDock
from gcws.ui.docks.folder_tree import FolderTree
from gcws.ui.docks.peak_table import PeakTable
from gcws.ui.docks.properties import PropertiesDock
from gcws.ui.docks.quant import QuantDock
from gcws.ui.docks.replicates import ReplicatesDock
from gcws.ui.docks.spectrum import SpectrumDock
from gcws.ui.icons import icon
from gcws.ui.layout import presets
from gcws.ui.layout.drop_overlay import DropOverlay
from gcws.ui.plot.chrom import ChromPanel, ViewLink
from gcws.ui.plot.tools import TOOLS, ToolController
from gcws.ui.run_tabs import LoadedSamples
from gcws.ui.undo import IdentCommand, ManualEventsCommand, ValueCommand, add_event
from gcws.ui.workspace import Workspace

DOCKS = [  # key, title
    # dock keys "chrom" / "zoom" are kept so saved layouts still restore
    ("tree", "Folders"), ("chrom", "Chromatogram 1"), ("zoom", "Chromatogram 2"),
    ("table", "Peaks / substances"), ("spectrum", "Mass spectrum"), ("events", "Integration method"),
    ("props", "Properties"), ("audit", "Audit trail"), ("quant", "Quantification"),
    ("replicates", "Replicates / results"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        from gcws.ui import theme
        theme.ensure_applied()
        super().__init__()
        self.setWindowTitle("GC Workspace")
        self.setWindowIcon(icon("integrate"))
        self.resize(1600, 950)
        self.ws = Workspace(self)
        self.tools = ToolController(self.ws)
        self.docks: dict[str, QDockWidget] = {}
        self.loading = 0
        self._pending_project = None
        self._fresh_folders: set = set()      # batch folders of runs loaded (not from a project) in this load
        self._maximized = None          # (dock, saved main-window state | floating geometry)
        central = QWidget()
        central.hide()
        self.setCentralWidget(central)
        self.setDockOptions(QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks
                            | QMainWindow.AnimatedDocks | QMainWindow.GroupedDragging)
        self.setDockNestingEnabled(True)
        self.setTabPosition(Qt.AllDockWidgetAreas, QTabWidgetNorth())

        # panels
        self.tree = FolderTree()
        self.loaded_samples = LoadedSamples(self.ws)
        from PySide6.QtWidgets import QVBoxLayout, QSplitter
        loaded = QWidget()
        loaded_layout = QVBoxLayout(loaded)
        loaded_layout.setContentsMargins(2, 2, 2, 2)
        loaded_layout.addWidget(QLabel("Loaded samples"))
        loaded_layout.addWidget(self.loaded_samples)
        self.folder_split = QSplitter(Qt.Vertical)
        self.folder_split.addWidget(self.tree)
        self.folder_split.addWidget(loaded)
        self.folder_split.setSizes([400, 220])
        self.folder_split.setChildrenCollapsible(False)
        self._restore_panels()
        self.chrom = ChromPanel(self.ws, self.tools, 0)
        self.chrom2 = ChromPanel(self.ws, self.tools, 1)
        self.chroms = [self.chrom, self.chrom2]
        self.view_link = ViewLink(self.chroms)
        self.table = PeakTable(self.ws)
        self.table.deleteRequested.connect(self.delete_peaks)
        self.spectrum = SpectrumDock(self.ws)
        self.events = EventsDock(self.ws)
        self.props = PropertiesDock(self.ws)
        self.audit = AuditDock(self.ws)
        self.quant = QuantDock(self.ws)
        self.replicates = ReplicatesDock(self.ws)
        widgets = {"tree": self.folder_split, "chrom": self.chrom, "zoom": self.chrom2, "table": self.table,
                   "spectrum": self.spectrum, "events": self.events, "props": self.props, "audit": self.audit,
                   "quant": self.quant, "replicates": self.replicates}
        self.overlay = DropOverlay(self)
        for key, title in DOCKS:
            self._add_dock(key, title, widgets[key])

        from gcws.ui.layout.sidebar import SidebarController
        self.sidebar = SidebarController(self)
        self._build_actions()
        self._build_toolbars()
        self._build_menus()
        self._build_status()
        self._connect()
        self._autosave = QTimer(self)
        self._autosave.setInterval(120_000)
        self._autosave.timeout.connect(self.autosave)
        self._autosave.start()
        presets.apply_preset(self, "Chromatogram top")
        if not self._restore_session_state():
            # dock sizes only take effect once the window has its real size
            QTimer.singleShot(0, lambda: (presets.apply_preset(self, "Chromatogram top"),
                                         self.restore_view_preferences("window")))
        else:
            self.restore_view_preferences("window")

    # -- construction ----------------------------------------------------------

    def _add_dock(self, key, title, widget):
        from gcws.ui.layout.title_bar import RightTitleDock
        d = RightTitleDock(title, self) if key in ("chrom", "zoom", "spectrum") else QDockWidget(title, self)
        d.setObjectName("dock." + key)
        if key == "events":
            # form-heavy panels scroll instead of forcing a wide minimum on the whole dock column
            from PySide6.QtWidgets import QScrollArea
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QScrollArea.NoFrame)
            area.setWidget(widget)
            widget = area
        d.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable
                      | QDockWidget.DockWidgetFloatable)
        from gcws.ui.layout.title_bar import DockTitleBar
        if isinstance(d, RightTitleDock):
            d.set_panel(widget, self.toggle_maximize)
        else:
            d.setWidget(widget)
            d.setTitleBarWidget(DockTitleBar(d, self.toggle_maximize))
        self.overlay.watch(d)
        self.docks[key] = d
        return d

    def _action(self, text, slot=None, shortcut=None, ic=None, tip=None, checkable=False):
        a = QAction(text, self)
        if ic is not None:
            a.setIcon(ic)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        a.setCheckable(checkable)
        if slot is not None:
            a.triggered.connect(slot)
        return a

    def _build_actions(self):
        A = self._action
        self.a_open_folder = A("Open folder...", self.open_folder, "Ctrl+Shift+O", icon("folder"),
                               "Show a folder in the tree")
        self.a_load = A("Load chromatograms...", self.load_dialog, "Ctrl+L", icon("run"))
        self.a_open = A("Open project...", self.open_project, "Ctrl+O")
        self.a_save = A("Save project", self.save_project, "Ctrl+S")
        self.a_save_as = A("Save project as...", lambda: self.save_project(True), "Ctrl+Shift+S")
        self.a_close_all = A("Close all chromatograms", self.close_all)
        self.a_quit = A("Exit", self.close, "Ctrl+Q")
        self.a_undo = A("Undo", self.ws.undo_group.undo)
        self.a_undo.setShortcut(QKeySequence.Undo)
        self.a_redo = A("Redo", self.ws.undo_group.redo)
        self.a_redo.setShortcut(QKeySequence.Redo)
        self.a_undo.setEnabled(self.ws.undo_group.canUndo())
        self.a_redo.setEnabled(self.ws.undo_group.canRedo())
        self.ws.undo_group.canUndoChanged.connect(self.a_undo.setEnabled)
        self.ws.undo_group.canRedoChanged.connect(self.a_redo.setEnabled)
        self.ws.undo_group.undoTextChanged.connect(
            lambda text: self.a_undo.setToolTip(f"Undo {text}" if text else "Undo"))
        self.ws.undo_group.redoTextChanged.connect(
            lambda text: self.a_redo.setToolTip(f"Redo {text}" if text else "Redo"))
        self.a_integrate = A("Integrate", lambda: self.integrate(False), "F5", icon("integrate"),
                             "Re-integrate the active chromatogram")
        self.a_integrate_all = A("Integrate all", lambda: self.integrate(True), "Shift+F5",
                                 icon("integrate", "#8e44ad"), "Re-integrate all loaded chromatograms")
        self.a_search = A("Library search...", self.library_search, "Ctrl+F", icon("search"),
                          "Automatic library search of all integrated peaks (your libraries)")
        self.a_search_method = A("Search methods...", self.edit_search_methods)
        self.a_subtract = A("Subtract baseline", self.spectrum.toggle_subtraction, None, icon("subtract"),
                            "Mass spectrum minus a baseline scan: click, then right-click the apex and then "
                            "the baseline in a chromatogram (Escape clears)", True)
        self.a_cancel_subtract = A("Cancel baseline subtraction", self.spectrum.back_to_peak, "Escape")
        self.a_cancel_subtract.setEnabled(False)
        self.addAction(self.a_cancel_subtract)
        self.spectrum.subtractionChanged.connect(self.a_subtract.setChecked)
        self.spectrum.subtractionChanged.connect(self.a_cancel_subtract.setEnabled)
        self.spectrumSearchAtlasAction = A("Library hit list (selected peak)", self.atlas_selected, "Ctrl+E")
        self.a_libraries = A("Libraries...", self.manage_libraries)
        self.spectrumSearchNistAction = A("Search selected peak in NIST", self.nist_selected, "Ctrl+N")
        self.atlasResearchAction = A("Investigate selected peak in SpectrAtlas...", self.atlas_research, "Ctrl+Shift+E")
        self.a_eic = A("Extracted ion chromatogram...", self.ask_eic, "Ctrl+I")
        self.istd_menu = QMenu("Set selected peak as ISTD", self)     # IS1, IS2, ... filled on opening
        self.istd_menu.setToolTipsVisible(True)
        self.istd_menu.aboutToShow.connect(self._fill_istd_menu)
        self.setIstdAction = self.istd_menu.menuAction()
        self.registerUnknownAction = A("Register selected peak as unknown...", self.register_unknown)
        self.a_deconv = A("Deconvolution...", lambda: self.deconvolution("peak"), "Ctrl+K", None,
                          "Split the selected FID or TIC peak into its deconvoluted components, or list the "
                          "components of the visible range or the whole run")
        self.splitDeconvAction = A("Split by deconvolution...", lambda: self.deconvolution("peak"), None, None,
                                   "Fit the deconvoluted components to the peak and split it into them")
        self.tool_actions = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        for name, label, key, tip in TOOLS:
            a = A(label, lambda _=False, n=name: self.tools.set_tool(n), key, icon(name), f"{tip}  [{key}]", True)
            group.addAction(a)
            self.tool_actions[name] = a
        self.tool_actions["select"].setChecked(True)

    def _build_toolbars(self):
        tb = QToolBar("Main")
        tb.setObjectName("tb.main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        for a in (self.a_load, self.a_open, self.a_save):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.a_undo)
        tb.addAction(self.a_redo)
        tb.addSeparator()
        tb.addAction(self.a_integrate)
        tb.addAction(self.a_search)
        tb.addAction(self.a_subtract)
        from gcws.ui import theme
        for a in (self.a_integrate, self.a_search, self.a_subtract):
            theme.set_primary(tb.widgetForAction(a))
        self.addToolBar(Qt.TopToolBarArea, tb)

        tools = QToolBar("Integration tools")
        tools.setObjectName("tb.tools")
        tools.setToolButtonStyle(Qt.ToolButtonIconOnly)
        for a in self.tool_actions.values():
            tools.addAction(a)
        self.addToolBar(Qt.TopToolBarArea, tools)

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for a in (self.a_open_folder, self.a_load):
            m.addAction(a)
        m.addAction("Load Shimadzu QGD files...", self.load_qgd_dialog)
        m.addSeparator()
        for a in (self.a_open, self.a_save, self.a_save_as):
            m.addAction(a)
        self.recent_menu = m.addMenu("Recent projects")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addAction("Recover autosave...", self.recover_autosave)
        m.addSeparator()
        m.addAction("Export peak table...", self.table.export)
        m.addAction("Export chromatogram...", lambda: self.export_chromatogram(0))
        m.addSeparator()
        m.addAction(self.a_close_all)
        m.addAction(self.a_quit)

        m = mb.addMenu("&Edit")
        m.addAction(self.a_undo)
        m.addAction(self.a_redo)
        m.addSeparator()
        m.addAction("Preferences...", self.preferences)

        m = mb.addMenu("&Method")                  # processing methods: all settings under one name
        m.addAction("Save current settings as Method...", self.save_method)
        m.addAction("Load Method...", self.load_method)
        self.method_menu = m

        self.view_menu = mb.addMenu("&View")
        for key, d in self.docks.items():
            self.view_menu.addAction(d.toggleViewAction())
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.a_eic)

        from gcws.ui.layout.plot_menus import ChromatogramMenu
        self.chrom_menu = ChromatogramMenu(self)
        mb.addMenu(self.chrom_menu)
        self.ms_menu = mb.addMenu("Mass Spectrum")
        self.spectrum.populate_menu(self.ms_menu)

        m = mb.addMenu("&Integration")
        m.addAction(self.a_integrate)
        m.addAction(self.a_integrate_all)
        m.addSeparator()
        for a in self.tool_actions.values():
            m.addAction(a)
        m.addSeparator()
        m.addAction("Integration method panel", lambda: self._show_dock("events"))
        m.addAction("Solvent cut...", self.edit_solvent_cut)

        m = mb.addMenu("I&dentify")
        m.addAction(self.a_search)
        m.addAction(self.a_search_method)
        m.addAction(self.a_libraries)
        m.addAction("Own library search options...", self.own_search_options)
        m.addSeparator()
        m.addAction(self.spectrumSearchAtlasAction)
        m.addAction(self.spectrumSearchNistAction)
        m.addAction(self.atlasResearchAction)
        m.addSeparator()
        m.addAction(self.registerUnknownAction)
        m.addAction("Unknown register...", self.open_register)
        m.addAction("Edit library...", lambda: self.edit_library(False))
        m.addAction("Add current spectrum to library...", lambda: self.edit_library(True))
        m.addSeparator()
        m.addAction("Retention index (alkane ladder)...", self.retention_index)
        m.addAction(self.a_deconv)
        m.addAction("Deconvolution of the whole run...", lambda: self.deconvolution("run"))
        self.identify_menu = m

        self.quant_menu = mb.addMenu("&Quantify")
        roles = self.quant_menu.addMenu("Role of active chromatogram")
        for role, label in ROLE_LABELS.items():
            roles.addAction(label, lambda r=role: self.ws.active_id and self.set_role(self.ws.active_id, r))
        self.quant_menu.addAction("Assign blanks...", lambda: self.ws.active_id and self.assign_blanks(self.ws.active_id))
        self.quant_menu.addAction("Blank subtraction settings...", self.edit_blank_options)
        self.quant_menu.addAction(self.setIstdAction)
        self.quant_menu.addSeparator()
        self.quant_menu.addAction("Quantification panel", lambda: self._show_dock("quant"))
        self.quant_menu.addAction("Double determination...", lambda: self.open_double_determination())
        self.quant_menu.addAction("Replicate groups (N-fold)", lambda: (self._show_dock("replicates"),
                                                                        self.replicates.tabs.setCurrentIndex(1)))

        self.report_menu = mb.addMenu("&Report")
        from gcws.report.service import KINDS
        for kind, label in KINDS.items():
            self.report_menu.addAction(label + "...", lambda k=kind: self.report(k))
            self.report_menu.addAction(label + " - preview", lambda k=kind: self.report(k, preview=True))
            self.report_menu.addSeparator()
        a = self.report_menu.addAction("Keep intermediate workbook")
        self.a_keep_middle = a
        a.setCheckable(True)
        a.setChecked(QSettings().value("report/keep_middle", False, type=bool))
        a.toggled.connect(lambda on: QSettings().setValue("report/keep_middle", on))

        m = mb.addMenu("&Layout")
        for name, tip in presets.PRESETS.items():
            a = m.addAction(name, lambda n=name: self.apply_preset(n))
            a.setStatusTip(tip)
        m.addSeparator()
        m.addAction("Save layout...", self.save_layout)
        self.saved_layouts_menu = m.addMenu("Saved layouts")
        self.saved_layouts_menu.aboutToShow.connect(self._fill_layouts)
        self.delete_layouts_menu = m.addMenu("Delete saved layout")
        self.delete_layouts_menu.aboutToShow.connect(self._fill_delete_layouts)
        m.addSeparator()
        a = m.addAction("Suggest docking position when dragging panels")
        a.setCheckable(True)
        a.setChecked(True)
        a.toggled.connect(lambda on: setattr(self.overlay, "enabled", on))
        a = m.addAction("Lock panels")
        a.setCheckable(True)
        a.toggled.connect(self._lock)
        m.addSeparator()
        from gcws.ui import theme
        self.theme_group = QActionGroup(self)          # exclusive: one look at a time
        self.theme_actions = {}
        for name, label in theme.THEME_LABELS.items():
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(name == theme.MODE)
            self.theme_group.addAction(a)
            a.triggered.connect(lambda _=False, n=name: self.set_theme(n))
            self.theme_actions[name] = a
        self.a_next_theme = m.addAction("Next theme", self.next_theme)
        self.a_next_theme.setShortcut("Ctrl+Shift+D")

        m = mb.addMenu("&Help")
        m.addAction("Keyboard shortcuts", self.show_shortcuts)
        m.addAction("About GC Workspace", self.about)

    def _build_status(self):
        sb = self.statusBar()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.hide()
        from gcws.ui import theme
        self.run_chips = [theme.chip("", "accent"), theme.chip("", "neutral"), theme.chip("", "neutral")]
        for c in self.run_chips:
            sb.addPermanentWidget(c)
        self.tool_label = QLabel()
        sb.addPermanentWidget(self.tool_label)
        self.method_label = QLabel()
        self.method_label.setObjectName("hint")
        self.method_label.setToolTip("The processing method loaded or saved last (Method menu)")
        sb.addPermanentWidget(self.method_label)
        self._show_method_name()
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.cancel_btn)

    def _connect(self):
        self.tree.loadRequested.connect(self.load_runs)
        self.tools.eventCreated.connect(self._manual_event)
        self.tools.toolChanged.connect(self._tool_changed)
        self.ws.message.connect(lambda t: self.statusBar().showMessage(t, 8000))
        self.ws.panelsChanged.connect(self._save_panels)
        self.ws.peakFocusRequested.connect(lambda *_: self.chrom.zoom_to_selected())
        for sig in (self.ws.activeRunChanged, self.ws.runChanged, self.ws.runRemoved):
            sig.connect(lambda *_: self._refresh_run_chips())
        self.loaded_samples.closeRequested.connect(self.close_run)
        self.loaded_samples.roleRequested.connect(self.set_role)
        self.loaded_samples.blanksRequested.connect(self.assign_blanks)
        self.loaded_samples.revealRequested.connect(lambda rid: (self._show_dock("tree"),
                                                           self.tree.reveal(self.ws.runs[rid].run.path)))
        self.loaded_samples.eicRequested.connect(self.ask_eic)
        self.loaded_samples.replicateRequested.connect(self.open_double_determination)
        self.loaded_samples.pairRequested.connect(self.open_double_determination)
        self.props.assignBlanksRequested.connect(self.assign_blanks)
        self.props.roleRequested.connect(self.set_role)
        self.table.set_context_actions([self.spectrumSearchNistAction, self.spectrumSearchAtlasAction,
                                        self.registerUnknownAction, self.setIstdAction, self.splitDeconvAction])
        self.table.searchRequested.connect(self.library_search)
        self.table.integrateRequested.connect(self.integrate)
        for plot in self.chroms:
            self.spectrum.regionsChanged.connect(plot.set_ms_regions)
            plot.spectrumRequested.connect(self._scan_spectrum)
            plot.componentClicked.connect(self._show_component)
            plot.resetRequested.connect(self.reset_views)
            plot.exportRequested.connect(self.export_chromatogram)
            for other in self.chroms:
                if other is not plot:
                    plot.cursorMoved.connect(other.set_cursor)
        self.spectrum.nistRequested.connect(self.nist_search)
        self.spectrum.atlasRequested.connect(self.atlas_hits)
        self.spectrum.ownSearchRequested.connect(self.own_library_search)
        self.spectrum.registerRequested.connect(self.register_unknown)
        self.spectrum.investigateRequested.connect(self.atlas_research)
        self.spectrum.ionClicked.connect(self.show_ion_eic)
        self.spectrum.libraryRequested.connect(lambda: self.edit_library(True))
        self.replicates.reportRequested.connect(lambda kind, gid: self.report(kind, gid))
        self.replicates.previewRequested.connect(lambda kind, gid: self.report(kind, gid, preview=True))
        self._tool_changed("select")

    # -- helpers -------------------------------------------------------------

    def _show_dock(self, key):
        d = self.docks[key]
        if self.sidebar.contains(d):
            self.sidebar.expand()
        d.show()
        d.raise_()

    def _tool_changed(self, name):
        label = next((t[1] for t in TOOLS if t[0] == name), name)
        self.tool_label.setText(f"Tool: {label}")
        if name in self.tool_actions:
            self.tool_actions[name].setChecked(True)
        mode = {"pan": "PanMode"}.get(name, "RectMode")
        import pyqtgraph as pg
        for plot in self.chroms:
            plot.vb.setMouseMode(getattr(pg.ViewBox, mode))

    def _refresh_run_chips(self):
        """Status bar: role, blank(s) and FID-MS delay of the active chromatogram."""
        from gcws.ui import theme
        st = self.ws.active
        role_chip, blank_chip, delay_chip = self.run_chips
        if st is None:
            for c in self.run_chips:
                theme.set_chip(c, "", "neutral")
            return
        theme.set_chip(role_chip, ROLE_LABELS.get(st.role, st.role), "accent")
        names = [self.ws.runs[b].name for b in st.blanks + st.blanks_istd if b in self.ws.runs]
        if st.role in ("sample", "standard"):
            theme.set_chip(blank_chip, "Blank: " + (", ".join(names) if names else "none"), "neutral" if names
                           else "warn")
        else:
            theme.set_chip(blank_chip, "", "neutral")
        if st.run.fid is not None and st.run.ms is not None:
            reliable = st.delay_override is not None or (st.delay is not None and st.delay.reliable)
            theme.set_chip(delay_chip, f"FID−MS {st.delay_value:+.4f} min", "neutral" if reliable else "warn")
        else:
            theme.set_chip(delay_chip, "", "neutral")

    # -- Chromatogram 1 / 2 ---------------------------------------------------------------

    def _restore_panels(self):
        s = QSettings()
        keys = s.value("panels/keys")
        if isinstance(keys, (list, tuple)) and len(keys) == 2:
            blanks = s.value("panels/blank") or [False, False]
            blanks = [str(b).lower() in ("true", "1") for b in (blanks if isinstance(blanks, (list, tuple))
                                                                 else [blanks])]
            self.ws.set_panels(list(keys), blanks, s.value("panels/table", 0, type=int))

    def _save_panels(self):
        s = QSettings()
        s.setValue("panels/keys", list(self.ws.panel_keys))
        s.setValue("panels/blank", [bool(b) for b in self.ws.panel_blank])
        s.setValue("panels/table", self.ws.table_panel)

    def ms_panel(self):
        """The chromatogram showing an MS trace (for an EIC), else Chromatogram 2."""
        from gcws.core.keys import is_fid
        return next((p for p in reversed(self.chroms) if not is_fid(p.key)), self.chrom2)

    def export_chromatogram(self, which: int = 0):
        from gcws.ui.dialogs.export_chrom import ExportChromatogramDialog
        if not self.ws.states():
            QMessageBox.information(self, "Export chromatogram", "Load a chromatogram first.")
            return
        ExportChromatogramDialog(self, which).exec()

    def reset_views(self):
        """Double-click in a chromatogram: the whole run in both, intensity fitted in each."""
        self.view_link.reset()

    def edit_solvent_cut(self):
        from gcws.ui.dialogs.solvent_cut import SolventCutDialog
        SolventCutDialog(self.ws, self).exec()

    def edit_blank_options(self):
        import copy
        from gcws.ui.dialogs.blank import BlankOptionsDialog
        dlg = BlankOptionsDialog(self.ws.blank_options(), self)
        if dlg.exec() != BlankOptionsDialog.Accepted:
            return
        q = copy.deepcopy(self.ws.quant)
        q["blank_sub"] = dlg.options().to_dict()
        if q.get("blank_sub") != self.ws.quant.get("blank_sub"):
            self.ws.push_quant("blank subtraction settings", q, "blank subtraction")
        if dlg.subtract_now:
            self.ws.subtract_blank()

    def ask_eic(self):
        """Ctrl+I: an extracted ion chromatogram in the MS chromatogram panel."""
        panel = self.ms_panel()
        key = panel.ask_eic()
        if key:
            panel.set_signal(key)
            self._show_dock("zoom" if panel is self.chrom2 else "chrom")

    # -- loading -----------------------------------------------------------------

    def load_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "Run folder (.D) or analysis folder", self.tree.root)
        if not d:
            return
        from gcws.io import folders
        paths = [d] if folders.is_run_dir(d) else [str(p) for p in folders.list_runs(d)]
        if not paths:
            QMessageBox.information(self, "Load", "No GC runs (.D folders or .qgd files) found there.")
            return
        self.load_runs(paths, "")

    def load_qgd_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Load Shimadzu GC-MS runs", self.tree.root,
                                                "Shimadzu GC-MS (*.qgd *.QGD)")
        if paths:
            self.load_runs(paths, "")

    def open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Show folder in the tree", self.tree.root)
        if d:
            self.tree.set_root(d)
            self._show_dock("tree")

    def load_runs(self, paths, role="", after=None):
        todo = []
        for p in paths:
            if self.ws.find_by_path(p) is not None:
                st = self.ws.find_by_path(p)
                if role:
                    self.set_role(st.id, role)
                self.ws.set_active(st.id)
                continue
            todo.append(p)
        for p in todo:
            self._loading(+1)
            workers.submit(workers.load_and_integrate, p, role, self.ws.methods,
                           on_done=lambda res, p=p: self._loaded(res, after),
                           on_error=lambda err, p=p: self._load_failed(p, err))

    def _loading(self, delta):
        self.loading += delta
        if self.loading > 0:
            self.progress.setRange(0, 0)
            self.progress.setFormat(f"loading {self.loading}")
            self.progress.show()
        else:
            self.progress.hide()

    def _loaded(self, result, after=None):
        run, results, delay = result
        entry = after(run) if after else None
        st = self.ws.add_run(run, results, delay=delay)
        if entry is not None:
            notes = P.apply_run_state(st, entry)
            for n in notes:
                self.ws.message.emit(n)
            for key in list(st.results):
                self.ws.integrate(st.id, key, emit=False)
            self.ws.runChanged.emit(st.id)
        else:
            self._fresh_folders.add(self.ws.folder_key(st))
        self.ws.log("Chromatogram loaded", st.name, str(run.path))
        # Finish the initial fit before reporting the load complete; later gestures win.
        self.view_link.reset()
        self._loading(-1)
        self.statusBar().showMessage(f"Loaded {st.name}", 4000)
        self._load_finished()

    def _load_failed(self, path, err):
        self._loading(-1)
        QMessageBox.warning(self, "Load failed", f"{Path(path).name}\n\n{err.splitlines()[0]}")
        self._load_finished()

    def _load_finished(self):
        if self.loading:
            return
        if self._pending_project is not None:
            self._finish_project_load()
        elif self._fresh_folders:
            # runs finish loading in any order: suggest the blanks again from the complete batch
            folders, self._fresh_folders = self._fresh_folders, set()
            self.ws.resuggest_blanks(folders)

    def close_run(self, run_id):
        st = self.ws.runs.get(run_id)
        if st is None:
            return
        self.ws.log("Chromatogram closed", st.name)
        self.ws.remove_run(run_id)

    def close_all(self):
        for rid in list(self.ws.order):
            self.ws.remove_run(rid)

    # -- integration -------------------------------------------------------------

    def integrate(self, all_runs=False):
        """Re-integrate the signals of both chromatograms (active run, or all runs)."""
        ids = list(self.ws.order) if all_runs else ([self.ws.active_id] if self.ws.active_id else [])
        for rid in ids:
            st = self.ws.runs[rid]
            for key in dict.fromkeys(self.ws.effective_key(st, self.ws.panel_key(i)) for i in (0, 1)):
                self.ws.integrate(rid, key)
        self.statusBar().showMessage(f"Integrated {len(ids)} chromatogram(s)", 3000)

    def delete_peaks(self, rts):
        from gcws.core.events import ManualEvent, ManualKind
        st = self.ws.active
        if st is None or not rts:
            return
        rts = list(dict.fromkeys(rts))
        text = f"delete {len(rts)} peak(s)"
        reason = ""
        if QSettings().value("prefs/require_reason", False, type=bool):
            reason, ok = QInputDialog.getText(self, "Reason", f"Reason for: {text}")
            if not ok:
                return
        key = self.ws.active_key
        events = st.events(key) + [ManualEvent(ManualKind.DELETE, rt) for rt in rts]
        st.undo.push(ManualEventsCommand(self.ws, st.id, key, events, text, reason))
        self.ws.select_peak(-1)
        self.statusBar().showMessage(f"Deleted {len(rts)} peak(s). Ctrl+Z to undo", 5000)

    def _manual_event(self, event, key=None):
        st = self.ws.active
        if st is None:
            return
        reason = ""
        if QSettings().value("prefs/require_reason", False, type=bool):
            reason, ok = QInputDialog.getText(self, "Reason", f"Reason for: {event.describe()}")
            if not ok:
                return
        key = self.ws.effective_key(st, key or self.ws.signal_key)
        st.undo.push(add_event(self.ws, st.id, key, event, reason))
        if self.tools.tool not in ("select", "pan") and not QSettings().value("prefs/sticky_tools", True, type=bool):
            self.tools.set_tool("select")

    # -- roles and blanks ------------------------------------------------------------

    def set_role(self, run_id, role):
        st = self.ws.runs.get(run_id)
        if st is None or st.role == role:
            return

        def setter(v, rid=run_id):
            s = self.ws.runs.get(rid)
            if s is not None:
                s.run.role = v
                self.ws._suggest_blanks()
                self.ws.invalidate_blank([rid])
                self.ws.runChanged.emit(rid)
                self.ws.quantChanged.emit()

        st.undo.push(ValueCommand(f"role of {st.name} = {ROLE_LABELS.get(role, role)}",
                                  lambda: self.ws.runs[run_id].role, setter, role,
                                  lambda t, o, n: self.ws.log(t, st.name, "", str(o), str(n))))

    def assign_blanks(self, run_id):
        from gcws.ui.dialogs.identify import BlanksDialog
        if not run_id or run_id not in self.ws.runs:
            return
        dlg = BlanksDialog(self.ws, run_id, self)
        if dlg.exec() != BlanksDialog.Accepted:
            return
        new = dlg.values()
        st = self.ws.runs[run_id]

        def getter(rid=run_id):
            s = self.ws.runs[rid]
            return (list(s.blanks), list(s.blanks_istd), s.blanks_manual)

        def setter(v, rid=run_id):
            s = self.ws.runs.get(rid)
            if s is not None:
                s.blanks, s.blanks_istd = list(v[0]), list(v[1])
                s.blanks_manual = bool(v[2]) if len(v) > 2 else True
                self.ws.invalidate_blank([rid])
                self.ws.runChanged.emit(rid)
                self.ws.quantChanged.emit()

        names = lambda ids: ", ".join(self.ws.runs[i].name for i in ids if i in self.ws.runs)
        new = (list(new[0]), list(new[1]), True)          # the analyst's choice: never re-suggested
        st.undo.push(ValueCommand(f"blanks of {st.name}", getter, setter, new,
                                  lambda t, o, n: self.ws.log(t, st.name, "", f"{names(o[0])} | {names(o[1])}",
                                                              f"{names(n[0])} | {names(n[1])}")))

    # -- identification --------------------------------------------------------------

    def library_search(self):
        from gcws.identify.service import LibrarySearchWorker, build_items
        from gcws.ui.dialogs.identify import SearchStartDialog
        if not self.ws.states():
            return
        dlg = SearchStartDialog(self.ws, self, filter_text=self.table.info.text()
                                if self.table.filter_state().active else "")
        if dlg.exec() != SearchStartDialog.Accepted:
            return
        v = dlg.values()
        ids = list(self.ws.order) if v["all"] else [self.ws.active_id]
        v["key"] = self.search_key(v.get("target", "TIC"))
        only = self.shown_peaks(ids, v["key"]) if v.get("only_shown") else None
        items, protected = build_items(self.ws, ids, v["key"], v["mode"], v["rescan"], v["skip"], only)
        if not items:
            QMessageBox.information(self, "Library search", "No peaks with MS data to search.")
            return
        self._search = LibrarySearchWorker(items, v["method"], self)
        self.progress.setRange(0, len(items))
        self.progress.setValue(0)
        self.progress.setFormat("%v / %m peaks")
        self.progress.show()
        self.cancel_btn.show()
        self.cancel_btn.clicked.connect(self._search.cancel)
        self._search.progress.connect(lambda t: self.statusBar().showMessage(t))
        self._search.hit.connect(lambda i: self.progress.setValue(self._search.done))
        self._search.failed.connect(self._search_failed)
        self._search.finished.connect(lambda cancelled: self._search_done(items, v, protected, cancelled))
        self._search.start()

    def shown_peaks(self, run_ids, key: str) -> dict:
        """``{run id: peak indices of key}`` that the peak table's filters let through.

        The filters work on the table's signal (quantities live on the FID); when the search
        runs on the other detector the shown peaks are mapped over through the FID-MS delay."""
        from gcws.ui.models.peak_filter import map_indices, visible_indices
        state, table_key = self.table.filter_state(), self.ws.signal_key
        out = {}
        for rid in run_ids:
            if rid == self.ws.active_id:
                shown = self.table.shown_indices()        # exactly what the analyst sees
            else:
                shown = visible_indices(self.ws, rid, table_key, state)
            out[rid] = map_indices(self.ws, rid, table_key, key, shown)
        return out

    def _search_failed(self, text: str):
        from gcws.libsearch import store
        if not any(s.enabled for s in store.load()):
            if QMessageBox.question(self, "Library search", text + "\n\nOpen the library list now?") \
                    == QMessageBox.Yes:
                self.manage_libraries()
            return
        QMessageBox.warning(self, "Library search", text)

    # -- look ------------------------------------------------------------------------------

    def set_theme(self, name: str):
        """Layout > Light Mode / Dark Mode / Dark Mode - Neon: switch live and remember it; run
        colours follow (the same place in the new theme's palette, visible on its background)."""
        from gcws.ui import theme
        if name not in theme.THEMES:
            return
        if name != theme.MODE:
            old = list(theme.RUN_COLORS)
            theme.set_theme(name)
            QSettings().setValue("prefs/theme", name)
            for st in self.ws.states():
                if st.color in old:
                    st.color = theme.RUN_COLORS[old.index(st.color)]
            for panel in (self.chrom, self.chrom2):
                panel.refresh()
            self.table.reload()
            self.spectrum.refresh()
            self.replicates.duplicate._reapply()
            self._refresh_run_chips()
            self.loaded_samples.sync()
        if not self.theme_actions[name].isChecked():
            self.theme_actions[name].setChecked(True)

    def next_theme(self):
        """Ctrl+Shift+D: Light -> Dark -> Neon -> Light."""
        from gcws.ui import theme
        names = list(theme.THEMES)
        self.set_theme(names[(names.index(theme.MODE) + 1) % len(names)])

    # -- processing methods --------------------------------------------------------------

    def _show_method_name(self):
        name = QSettings().value("method/current", "") or ""
        self.method_label.setText(f"Method: {name}" if name else "")

    def save_method(self):
        from gcws.ui.dialogs.proc_method import SaveMethodDialog
        dlg = SaveMethodDialog(self)
        if dlg.exec() == SaveMethodDialog.Accepted:
            self._show_method_name()
            self.statusBar().showMessage(f"Method saved: {dlg.saved}", 8000)

    def load_method(self):
        from gcws.ui.dialogs.proc_method import LoadMethodDialog
        dlg = LoadMethodDialog(self)
        if dlg.exec() == LoadMethodDialog.Accepted and dlg.applied:
            self._show_method_name()
            self.statusBar().showMessage(f"Method '{dlg.method['name']}' loaded: {len(dlg.applied)} parts applied",
                                         8000)

    def manage_libraries(self):
        from gcws.ui.dialogs.libraries import LibraryManagerDialog
        LibraryManagerDialog(self).exec()

    def search_key(self, target: str) -> str:
        """The TIC or FID key to search: the one a chromatogram shows (with its blank switch)."""
        from gcws.core.keys import base_key
        return next((self.ws.panel_key(i) for i in (0, 1) if base_key(self.ws.panel_key(i)) == target), target)

    def _search_done(self, items, v, protected, cancelled):
        from gcws.identify.service import apply_search_results
        from gcws.ui.dialogs.identify import CompoundReview
        self.progress.hide()
        self.cancel_btn.hide()
        try:
            self.cancel_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        done = [it for it in items if it.job.done and not it.job.error]
        if not done:
            self.statusBar().showMessage("Library search: nothing found" + (" (cancelled)" if cancelled else ""))
            return
        method = v["method"]
        if v["review"]:
            dlg = CompoundReview(done, method.min_score, self)
            if dlg.exec() != CompoundReview.Accepted:
                return
        hits = apply_search_results(self.ws, done, method, transfer=bool(v.get("transfer")),
                                    fid_key=self.search_key("FID"))
        copied, n = hits.copied, hits.identified
        msg = f"Library search: {n} peaks identified" + (f", {protected} protected" if protected else "")
        if v.get("transfer"):
            msg += f"; {copied['copied']} names copied to FID peaks"
            if copied["unmatched"]:
                msg += f" ({copied['unmatched']} TIC peaks without an FID peak"
                msg += f", {copied['protected']} FID names kept)" if copied["protected"] else ")"
        self.statusBar().showMessage(msg + (" (cancelled)" if cancelled else ""), 12000)

    def edit_library(self, from_spectrum: bool = True):
        """Identify > Edit library: add the spectrum on display to a library, or browse one."""
        from gcws.ui.dialogs.library_edit import EditLibraryDialog, entry_from_spectrum
        entry = entry_from_spectrum(self) if from_spectrum else {}
        if from_spectrum and not entry.get("peaks"):
            self.statusBar().showMessage("No spectrum on display: select a peak or right-click a chromatogram", 6000)
        dlg = EditLibraryDialog(self, entry)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        self._library_dialog = dlg          # not modal: peaks can be picked while it is open
        dlg.show()

    def edit_search_methods(self):
        from gcws.ui.dialogs.search_method import SearchMethodDialog
        SearchMethodDialog(self).exec()

    def atlas_selected(self):
        self.spectrum._emit(self.spectrum.atlasRequested)

    def nist_selected(self):
        self.spectrum._emit(self.spectrum.nistRequested)

    def show_ion_eic(self, mz: int):
        """Click on an ion in the spectrum: its EIC in the MS chromatogram panel."""
        key = eic_key([mz])
        st = self.ws.active
        if st is None or st.run.ms is None:
            return
        st.run.signal(key)
        panel = self.ms_panel()
        panel.set_signal(key)
        self.statusBar().showMessage(f"EIC m/z {mz} shown in Chromatogram {panel.index + 1}", 5000)

    def _show_component(self, run_id, comp):
        self.spectrum.show_component(run_id, comp)
        self._show_dock("spectrum")

    def _scan_spectrum(self, req):
        """Right-click / right-drag in a chromatogram: show that spectrum."""
        if self.ws.runs.get(req.run_id) is None:
            return
        self.spectrum.show_range(req)
        d = self.docks["spectrum"]
        if not d.isVisible():
            d.show()
        d.raise_()

    def own_library_search(self, points, name):
        """The spectrum on display, searched in the one library chosen for "Own library"."""
        from gcws.ui.dialogs import own_search as OS
        opts = OS.load_options()
        if opts["library"] not in OS.libraries():
            dlg = OS.OwnSearchOptionsDialog(self)
            if dlg.exec() != QDialog.Accepted or not dlg.values()["library"]:
                return
            opts = OS.load_options()
        self.atlas_hits(points, name, OS.method_from_options(opts))

    def own_search_options(self):
        from gcws.ui.dialogs.own_search import OwnSearchOptionsDialog
        OwnSearchOptionsDialog(self).exec()

    def atlas_hits(self, points, name, method=None):
        from gcws.identify.service import search_methods
        from gcws.ui.dialogs.identify import AtlasHitsDialog
        st, peak = self.spectrum.target_peak()
        source_key = self.ws.signal_key
        source_mode = self.spectrum.spec.mode if self.spectrum.spec else self.spectrum.current_mode()
        if method is None:
            store = search_methods()
            method = store.for_gc_method(st.run.meta.method if st and st.run.meta else "")

        def assign(hits, index, st=st, peak=peak):
            from gcws.identify.service import SearchItem, identification_from_hits
            import gc_identify as GI
            if st is None or peak is None:
                return
            job = GI.PeakJob(label=st.name, row_id=0, peak_no=peak.number, rt=peak.apex_rt,
                             before=("", "", None), spectrum=points)
            job.hits = hits
            from gcws.ms.assignment import fragment_id
            item = SearchItem(st.id, source_key, 0, peak.apex_rt, job, source_mode, [], fragment_id(peak))
            prev = st.ident_set(source_key).for_peak(peak)
            ident = identification_from_hits(item, hits, index, method, prev)
            ident.manual = True
            st.undo.push(IdentCommand(self.ws, st.id, source_key, [(peak.apex_rt, ident)],
                                      f"peak {peak.apex_rt:.3f}: library hit {ident.name}"))

        dlg = AtlasHitsDialog(points, name, method, self, on_assign=assign if peak is not None else None,
                              on_library=lambda: self.edit_library(True))
        dlg.show()

    def atlas_research(self):
        """The full SpectrAtlas investigation (native window, research tab)."""
        from gcws.identify.atlas_bridge import AtlasBridge
        from gcws.ui.dialogs.register import db_path
        st, peak = self.spectrum.target_peak()
        points = self.spectrum.points()
        if st is None or not points:
            QMessageBox.information(self, "SpectrAtlas", "Select a peak with a mass spectrum (or right-click a "
                                                      "chromatogram) first.")
            return
        spec = self.spectrum.spec
        mig = self.ws.quant.get("migration") or {}
        snapshot = {"spectrum": points, "rt": spec.rt, "name": self.spectrum.spectrum_name(),
                    "apex_scan": spec.apex_scans[len(spec.apex_scans) // 2] if spec.apex_scans else None,
                    "bg_scan": spec.bg_scans[0] if spec.bg_scans else None}
        ms = st.run.ms
        sl = ms.scans_between(spec.rt - 0.6, spec.rt + 0.6)
        context = {"sample": st.run.path.name, "sample_name": st.name, "report_type": "GC Workspace",
                   "date": (st.run.meta.acquired or "")[:10], "analyst": mig.get("analyst", ""),
                   "simulant": mig.get("simulant", ""), "source_file": str(st.run.path), "label": st.name,
                   "peak_no": peak.number if peak else None, "peak_rt": peak.apex_rt if peak else spec.rt,
                   "area_pct": peak.area_pct if peak else None,
                   "method": "GC Workspace", "tic": ([float(x) for x in ms.rt[sl]], [int(v) for v in ms.stored_tic[sl]])}
        bridge = AtlasBridge.instance()
        if not getattr(self, "_atlas_connected", False):
            bridge.error.connect(lambda e: QMessageBox.warning(self, "SpectrAtlas", e))
            bridge.saved.connect(lambda rec: self.statusBar().showMessage(
                f"SpectrAtlas investigation saved as {rec.get('unknown_id', '')}", 8000))
            bridge.registerRequested.connect(lambda *_: self.open_register())
            self._atlas_connected = True
        try:
            from gcws.ui import theme
            bridge.open_research(snapshot, context, db_path(), int(self.winId()), theme.MODE)
            self.statusBar().showMessage("Opening SpectrAtlas ...", 5000)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "SpectrAtlas", str(exc))

    def nist_search(self, points, name):
        import gc_nist
        p = self.ws.selected_peak()
        try:
            msg = gc_nist.search_spectrum(points, name, p.apex_rt if p else None)
            self.statusBar().showMessage("Spectrum sent to NIST MS Search", 6000)
        except Exception as exc:  # noqa: BLE001 - shown to the analyst
            QMessageBox.warning(self, "NIST MS Search", str(exc))

    # -- quantification helpers --------------------------------------------------------

    def _istd_choices(self) -> tuple[bool, list[tuple[str, str]], dict]:
        """(HS screening?, [(code, name)], {code: bound RT} of the active run) for the ISTD menu."""
        q = self.ws.quant
        hs = q.get("mode") == "hs_screening"
        st = self.ws.active
        if hs:
            codes = self.quant.hs_panel.istd_codes()
            bound = (q.get("hs") or {}).get("istd_bindings", {})
        else:
            codes = [(d["code"], d.get("name") or "") for d in self.quant._defs()]
            bound = q.get("istd_bindings") or {}
        return hs, codes, (bound.get(st.id) or {}) if st is not None else {}

    def _fill_istd_menu(self):
        m = self.istd_menu
        m.clear()
        p = self.ws.selected_peak()
        hs, codes, bound = self._istd_choices()
        apex = None if p is None else (p.apex_rt if hs else self.quant.binding_rt(p))   # binding axis
        for code, name in codes:
            a = m.addAction(f"{code}  {name}".rstrip())
            rt = bound.get(code)
            a.setCheckable(True)
            a.setChecked(apex is not None and rt is not None and abs(float(rt) - apex) <= self.ws.ISTD_BOUND_TOL)
            if rt is not None and not a.isChecked():
                a.setToolTip(f"now bound to {float(rt):.3f} min")
            a.setEnabled(p is not None)
            a.triggered.connect(lambda _=False, c=code: self.set_istd_selected(c))
        if not codes:
            m.addAction("No internal standards defined").setEnabled(False)

    def set_istd_selected(self, code: str):
        """Bind the selected peak as internal standard ``code`` (IS1, IS2, ...) of the active run.

        The Quantification panel takes the binding over; it is not opened."""
        p = self.ws.selected_peak()
        if p is None or self.ws.active is None:
            QMessageBox.information(self, "ISTD", "Select a peak first.")
            return
        hs, _codes, _bound = self._istd_choices()
        if hs:
            if not self.quant.hs_panel.bind(code):
                return
        else:
            self.quant._set_binding(code, self.quant.binding_rt(p))
        self.statusBar().showMessage(f"Peak {p.apex_rt:.3f} min set as {code}", 6000)

    def open_register(self):
        from gcws.ui.dialogs.register import RegisterWindow
        try:
            RegisterWindow(self).show()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Unknown register", str(exc))

    def retention_index(self):
        from gcws.ui.dialogs.ri import RetentionIndexDialog
        RetentionIndexDialog(self).exec()

    def deconvolution(self, scope: str = "peak"):
        from gcws.ui.dialogs.deconv import DeconvolutionDialog
        if self.ws.active is None or self.ws.active.run.ms is None:
            QMessageBox.information(self, "Deconvolution", "Activate a chromatogram with MS data.")
            return
        if scope == "peak" and self.ws.selected_peak() is None:
            scope = "run"
        old = getattr(self, "_deconv_dialog", None)
        if old is not None:
            try:
                old.close()          # one deconvolution window: an older one would be stale
            except RuntimeError:
                pass
        self._deconv_dialog = dlg = DeconvolutionDialog(self, scope)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()

    def register_unknown(self):
        from gcws.ui.dialogs.register import save_unknown
        save_unknown(self)

    # -- reports ---------------------------------------------------------------------------

    def open_double_determination(self, run_id=None, partner=None):
        """Double-determination page for ``run_id`` (with ``partner`` or the suggested one)."""
        self._show_dock("replicates")
        run_id = run_id or self.ws.active_id
        if run_id:
            self.replicates.show_pair(run_id, partner)

    def _group_for_report(self, group_id=None):
        groups = self.ws.replicate_groups
        if group_id:
            return next((g for g in groups if g["id"] == group_id), None)
        g = self.replicates.current_group()
        if g is not None:
            return g
        rid = self.ws.active_id
        g = next((g for g in groups if rid in g["members"]), None)
        if g is not None:
            return g
        if rid and self.ws.runs[rid].role == "sample":
            return {"id": "", "name": self.ws.runs[rid].name, "members": [rid], "policy": "all"}
        return None

    def report(self, kind, group_id=None, preview=False):
        from gcws.report import assemble as AS
        from gcws.report import service as RS
        g = self._group_for_report(group_id)
        try:
            try:
                members, samples = AS.prepare(self.ws, kind, g)
            except AS.ReportNotPossible as exc:
                if exc.code != "no_migration":
                    raise
                self.quant.edit_migration()
                if not self.ws.quant.get("migration"):
                    return
                members, samples = AS.prepare(self.ws, kind, g)
        except AS.ReportNotPossible as exc:
            title = "HS-Screening report" if exc.code == "hs_errors" else "Report"
            (QMessageBox.information if exc.level == "information" else QMessageBox.warning)(self, title, exc.message)
            if exc.code == "no_group":
                self._show_dock("replicates")
            return
        default = AS.default_target(self.ws, kind, members)
        if preview:
            import tempfile
            target = Path(tempfile.mkdtemp(prefix="gcws_preview_")) / default.name
        else:
            fn, _ = QFileDialog.getSaveFileName(self, f"Save {RS.KINDS[kind]}", str(default), "Excel (*.xlsx)")
            if not fn:
                return
            target = Path(fn)
        job = AS.build_job(self.ws, kind, g, target, members=members, samples=samples, preview=preview,
                           keep_middle=QSettings().value("report/keep_middle", False, type=bool))
        self.progress.setRange(0, 0)
        self.progress.setFormat(RS.KINDS[kind])
        self.progress.show()
        self.statusBar().showMessage(f"{RS.KINDS[kind]}: generating ...")

        def work():
            res = RS.generate(job)
            pages = []
            if preview and res.word is not None:
                try:
                    pdf = RS.docx_to_pdf(res.word, res.word.with_suffix(".pdf"))
                    pages = RS.render_pages(pdf)
                except Exception as exc:  # noqa: BLE001 - preview without pages
                    res.warnings.append(f"Preview pages not rendered: {exc}")
            return res, pages

        workers.submit(work, on_done=lambda r: self._report_done(kind, job, r[0], r[1], preview),
                       on_error=lambda e: self._report_failed(e))

    def _report_failed(self, err):
        self.progress.hide()
        QMessageBox.warning(self, "Report", err.splitlines()[0] + "\n\n" + "\n".join(err.splitlines()[1:6]))

    def _report_done(self, kind, job, res, pages, preview):
        from gcws.report import service as RS
        self.progress.hide()
        self.ws.log(f"{RS.KINDS[kind]}{' preview' if preview else ''}", ", ".join(job.names), str(res.target))
        if preview:
            from gcws.ui.dialogs.report_preview import ReportPreview
            dlg = ReportPreview(f"{RS.KINDS[kind]} - preview", {"xlsx": res.target, "docx": res.word,
                                                               "batch": res.batch, "default": res.target.name},
                                pages, self, warnings=res.warnings)
            if dlg.exec() and dlg.saved_to is not None:
                err = RS.record_seen(kind, res.reported, dlg.saved_to, job.sample_key)
                self.statusBar().showMessage(f"Saved {dlg.saved_to}" + (f" ({err})" if err else ""), 8000)
            return
        lines = [f"{RS.KINDS[kind]} written:", "", str(res.target)]
        if res.word:
            lines.append(str(res.word))
        if res.batch:
            lines.append(str(res.batch))
        lines += ["", f"{res.rows} substance(s) reported."] + res.warnings + ["", "Open the folder?"]
        if QMessageBox.question(self, "Report", "\n".join(lines)) == QMessageBox.Yes:
            import os
            os.startfile(str(res.target.parent))

    # -- projects ------------------------------------------------------------------------

    def save_project(self, ask=False):
        if not self.ws.states():
            return
        path = self.ws.project_path
        if ask or path is None:
            first = self.ws.states()[0].run.path.parent
            default = str(first / (first.name + P.SUFFIX))
            fn, _ = QFileDialog.getSaveFileName(self, "Save project", default, "GC Workspace project (*.gcws)")
            if not fn:
                return
            path = Path(fn)
        try:
            path = P.save(self.ws, path)
        except OSError as exc:
            QMessageBox.warning(self, "Save project", str(exc))
            return
        self.ws.project_path = path
        self.ws.dirty = False
        self._add_recent(path)
        self.setWindowTitle(f"GC Workspace - {path.name}")
        self.statusBar().showMessage(f"Saved {path}", 5000)

    def open_project(self, path=None):
        if not isinstance(path, (str, Path)):        # QAction.triggered passes its checked state
            path = None
        if path is None:
            fn, _ = QFileDialog.getOpenFileName(self, "Open project", self.tree.root,
                                                "GC Workspace project (*.gcws)")
            if not fn:
                return
            path = fn
        path = Path(path)
        try:
            data = P.read(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Open project", str(exc))
            return
        if self.ws.states() and QMessageBox.question(
                self, "Open project", "Close the loaded chromatograms?") != QMessageBox.Yes:
            return
        self.close_all()
        self.ws.audit.load(data.get("audit"))
        self.audit.reload()
        self.ws.replicate_groups = data.get("replicate_groups", [])
        self.ws.quant = data.get("quant", {})
        panels = data.get("panels") or {}
        if panels.get("keys"):
            self.ws.set_panels(panels["keys"], panels.get("blank") or [False, False], panels.get("table", 0))
        else:                                      # older projects: one working signal
            self.ws.set_panels([data.get("signal_key", FID), self.ws.panel_keys[1]], [False, False], 0)
            self.ws.set_signal_key(data.get("signal_key", FID))
        self._pending_project = (path, data)
        entries = {}
        missing = []
        for entry in data.get("runs", []):
            rp = P.resolve_run_path(entry, path)
            if rp is None:
                missing.append(entry.get("name", "?"))
                continue
            entries[str(rp)] = entry

        def after(run, entries=entries):
            entry = entries.get(str(run.path.resolve()))
            if entry is not None:
                run.id = entry["id"]
            return entry

        if missing:
            QMessageBox.warning(self, "Open project", "Raw data not found for:\n" + "\n".join(missing))
        if not entries:
            self._pending_project = None
            return
        self.ws.project_path = path
        self.load_runs(list(entries), "", after=after)

    def _finish_project_load(self):
        path, data = self._pending_project
        self._pending_project = None
        order = [e["id"] for e in data.get("runs", []) if e["id"] in self.ws.runs]
        self.ws.reorder(order + [i for i in self.ws.order if i not in order])
        # runs load in parallel: blank-subtracted traces are rebuilt once every blank is in
        self.ws.invalidate_blank(None)
        changed = []
        for st in self.ws.states():
            for key, dig in st.saved_digests.items():
                res = self.ws.result(st.id, key)
                if res is not None and res.digest != dig:
                    changed.append(f"{st.name} ({key})")
        if data.get("active") in self.ws.runs:
            self.ws.set_active(data["active"])
        self.ws.signalKeyChanged.emit(self.ws.signal_key)
        self.ws.quantChanged.emit()
        self.ws.replicatesChanged.emit()
        self.ws.dirty = False
        self._add_recent(path)
        self.setWindowTitle(f"GC Workspace - {path.name}")
        if changed:
            QMessageBox.information(self, "Open project",
                                    "The integration now differs from the saved state for:\n" + "\n".join(changed)
                                    + "\n\n(raw data or integrator version changed)")

    def autosave_path(self) -> Path:
        from gcws import paths
        return paths.DATA / "projects" / "autosave.gcws"

    def autosave(self):
        """Every two minutes while something changed: a copy of the project for recovery."""
        if not self.ws.dirty or not self.ws.states() or self.loading:
            return
        import json
        target = self.autosave_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = P.to_dict(self.ws, self.ws.project_path or target)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
        except (OSError, ValueError):
            pass

    def recover_autosave(self):
        target = self.autosave_path()
        if not target.exists():
            QMessageBox.information(self, "Recover", "There is no autosaved session.")
            return
        self.open_project(target)

    def _add_recent(self, path):
        s = QSettings()
        rec = [p for p in (s.value("recent", []) or []) if p != str(path)]
        s.setValue("recent", [str(path)] + rec[:9])

    def _fill_recent(self):
        self.recent_menu.clear()
        for p in QSettings().value("recent", []) or []:
            self.recent_menu.addAction(p, lambda p=p: self.open_project(p))

    # -- layouts -----------------------------------------------------------------------------

    def save_layout(self):
        name, ok = QInputDialog.getText(self, "Save layout", "Name:")
        if ok and name.strip():
            self.restore_maximized()
            presets.save_layout(self, name.strip())
            self.statusBar().showMessage(f"Layout '{name.strip()}' saved", 4000)

    def _fill_layouts(self):
        self.saved_layouts_menu.clear()
        for n in presets.saved_layouts():
            self.saved_layouts_menu.addAction(n, lambda n=n: (self.restore_maximized(),
                                                              presets.restore_layout(self, n)))

    def _fill_delete_layouts(self):
        self.delete_layouts_menu.clear()
        for n in presets.saved_layouts():
            self.delete_layouts_menu.addAction(n, lambda n=n: presets.delete_layout(n))

    def apply_preset(self, name):
        self.sidebar.expand()
        self.restore_maximized()
        presets.apply_preset(self, name)

    def toggle_maximize(self, dock):
        """Double-click on a panel title: the panel alone fills the window (a detached one its
        screen); again restores the previous layout."""
        if self._maximized is not None:
            self.restore_maximized()
            return
        if dock.isFloating():
            self._maximized = (dock, dock.geometry())
            screen = dock.screen() or self.screen()
            dock.setGeometry(screen.availableGeometry())
        else:
            self._maximized = (dock, self.saveState(presets.LAYOUT_VERSION))
            for d in self.docks.values():
                if d is not dock and not d.isFloating() and d.isVisible():
                    d.hide()
            dock.show()
            dock.raise_()
        from gcws.ui.layout.title_bar import title_bar
        bar = title_bar(dock)
        if hasattr(bar, "set_maximized"):
            bar.set_maximized(True)
        self.statusBar().showMessage(f"{dock.windowTitle()} maximized - double-click its title to restore", 4000)

    def restore_maximized(self):
        if self._maximized is None:
            return
        dock, saved = self._maximized
        self._maximized = None
        if isinstance(saved, QByteArray):
            self.restoreState(saved, presets.LAYOUT_VERSION)
        else:
            dock.setGeometry(saved)
        from gcws.ui.layout.title_bar import title_bar
        bar = title_bar(dock)
        if hasattr(bar, "set_maximized"):
            bar.set_maximized(False)

    def _lock(self, locked):
        for d in self.docks.values():
            f = QDockWidget.DockWidgetClosable
            if not locked:
                f |= QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable
            d.setFeatures(f)

    def save_view_preferences(self, prefix):
        settings = QSettings()
        settings.setValue(prefix + "/folders_split", self.folder_split.saveState())
        settings.setValue(prefix + "/ms_details_height", self.spectrum.details_height)
        self.sidebar.save(prefix)

    def restore_view_preferences(self, prefix):
        settings = QSettings()
        state = settings.value(prefix + "/folders_split")
        if isinstance(state, QByteArray):
            self.folder_split.restoreState(state)
        self.spectrum.restore_details_height(settings.value(prefix + "/ms_details_height", 200, type=int))
        self.sidebar.restore(prefix)

    def _restore_session_state(self):
        s = QSettings()
        geo, state = s.value("window/geometry"), s.value("window/state")
        if geo is not None:
            self.restoreGeometry(geo)
        if state is not None:
            return bool(self.restoreState(state, presets.LAYOUT_VERSION))
        return False

    def closeEvent(self, ev):
        if self.ws.dirty and self.ws.states():
            r = QMessageBox.question(self, "GC Workspace", "Save the project before closing?",
                                     QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                ev.ignore()
                return
            if r == QMessageBox.Yes:
                self.save_project()
        try:
            self.autosave_path().unlink(missing_ok=True)       # a clean exit needs no recovery
        except OSError:
            pass
        self.restore_maximized()
        s = QSettings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("window/state", self.saveState(presets.LAYOUT_VERSION))
        self.save_view_preferences("window")
        self.table.save_columns()
        super().closeEvent(ev)

    # -- misc ------------------------------------------------------------------------------

    def preferences(self):
        from gcws.ui.dialogs.preferences import PreferencesDialog
        PreferencesDialog(self).exec()

    def show_shortcuts(self):
        lines = [f"{key:>6}   {label}" for name, label, key, tip in TOOLS]
        lines += ["", "    F5   Integrate active", "Shift+F5   Integrate all", "Ctrl+F   Library search",
                  "Ctrl+E   Library hit list", "Ctrl+N   NIST search", "Ctrl+I   Extracted ion chromatogram",
                  "Ctrl+K   Deconvolution: split the selected peak into its components",
                  "Ctrl+Z / Ctrl+Y   Undo / Redo",
                  "Ctrl+Shift+D   Next theme (Light / Dark / Neon)", "",
                  "Mouse in a chromatogram:",
                  "  right-click              mass spectrum at that time",
                  "  right-drag               mean spectrum over the range",
                  "  Shift+right-drag         background range (subtracted from scan spectra)",
                  "  left-drag (Select tool)  zoom into the box (time and intensity)",
                  "  double-click             the whole run in both chromatograms",
                  "  wheel                    zoom the time around the cursor; on an axis: that axis",
                  "  left-drag on an axis     pan time horizontally / intensity vertically",
                  "  intensity right-drag / wheel   scale both panels with the bottom fixed",
                  "  double-click intensity axis    fit both intensities, keep the time window",
                  "  manual intensity stays on later time zooms; double-click fits it again",
                  "  Shift disables snapping of integration tools",
                  "  Delete in Peaks / substances   delete marked peaks (one undo step)",
                  "  Chromatogramm > FID / TIC-MS solvent cut    independent detector cuts",
                  "  Subtract baseline    right-click apex, then baseline; Escape clears",
                  "  a peak picked in the table zooms both chromatograms to it",
                  "",
                  "Panels: double-click a title to maximize the panel, again to restore the layout.",
                  "Spectrum panel: ← / → step one scan, Esc returns to the peak."]
        QMessageBox.information(self, "Keyboard shortcuts", "\n".join(lines))

    def about(self):
        QMessageBox.about(self, "GC Workspace",
                          f"<b>GC Workspace {gcws.__version__}</b><br>Integrator version {gcws.INTEGRATOR_VERSION}"
                          "<br><br>Standalone GC-FID / GC-MS integration, SpectrAtlas / NIST identification and NIAS "
                          "reporting. Reads Agilent data.ms, *.ch and MassHunter AcqData directly.")


def QTabWidgetNorth():
    from PySide6.QtWidgets import QTabWidget
    return QTabWidget.North

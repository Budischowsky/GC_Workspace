"""Workspace-backed plot settings in the main menu."""
from PySide6.QtCore import QLocale, QSignalBlocker
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QMenu, QWidget, QWidgetAction


class RetentionTimeSpinBox(QDoubleSpinBox):
    """Accept either decimal mark, never interpret a typed dot as thousands."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLocale(QLocale.c())

    def validate(self, text, pos):
        return super().validate(text.replace(",", "."), pos)

    def valueFromText(self, text):
        return super().valueFromText(text.replace(",", "."))


class ChromatogramMenu(QMenu):
    def __init__(self, win):
        super().__init__("Chromatogramm", win)
        self.win = win
        self.ws = win.ws
        self.cut = self.addAction("FID solvent cut")
        self.cut.setCheckable(True)
        self.cut.toggled.connect(lambda on: self.ws.set_solvent_cut(on))
        row = QWidget(self)
        layout = QHBoxLayout(row)
        self.end_label = QLabel("Solvent end RT [min, FID time]")
        layout.addWidget(self.end_label)
        self.end = RetentionTimeSpinBox(row)
        self.end.setRange(0, 10000)
        self.end.setDecimals(3)
        self.end.setKeyboardTracking(False)
        self.end.setToolTip("Solvent end in the indicated detector's time")
        self.end.editingFinished.connect(self._commit)
        layout.addWidget(self.end)
        field = QWidgetAction(self)
        field.setDefaultWidget(row)
        self.addAction(field)
        self.ms_cut = self.addAction("TIC/MS solvent cut")
        self.ms_cut.setCheckable(True)
        self.ms_cut.toggled.connect(lambda on: self.ws.set_solvent_cut(on, key="TIC"))
        ms_row = QWidget(self)
        ms_layout = QHBoxLayout(ms_row)
        ms_layout.addWidget(QLabel("Solvent end RT [min, MS time]"))
        self.ms_end = RetentionTimeSpinBox(ms_row)
        self.ms_end.setRange(-10000, 10000)
        self.ms_end.setDecimals(3)
        self.ms_end.setKeyboardTracking(False)
        self.ms_end.editingFinished.connect(
            lambda: self.ws.set_solvent_cut(self.ms_cut.isChecked(), self.ms_end.value(), key="TIC"))
        ms_layout.addWidget(self.ms_end)
        self.ms_field = QWidgetAction(self)
        self.ms_field.setDefaultWidget(ms_row)
        self.addAction(self.ms_field)
        self.addSeparator()
        self.blanks = []
        for i in range(2):
            menu = self.addMenu(f"Chromatogram {i + 1}")
            action = menu.addAction("subtract blank")
            action.setCheckable(True)
            action.toggled.connect(lambda on, n=i: self.ws.set_panel(n, blank=on))
            self.blanks.append(action)
        for signal in (self.ws.solventCutChanged, self.ws.panelsChanged, self.ws.runChanged,
                       self.ws.runAdded, self.ws.runRemoved, self.ws.activeRunChanged):
            signal.connect(self.sync)
        self.aboutToShow.connect(self.sync)
        self.sync()

    def _commit(self):
        self.ws.set_solvent_cut(self.cut.isChecked(), self.end.value())

    def sync(self, *_):
        hs = self.ws.quant.get("mode") == "hs_screening"
        self.cut.setText("HS TIC/MS solvent cut" if hs else "FID solvent cut")
        self.end_label.setText("Solvent end RT [min, MS time]" if hs else "Solvent end RT [min, FID time]")
        self.ms_cut.setVisible(not hs)
        self.ms_field.setVisible(not hs)
        enabled, end = self.ws.solvent_cut_settings()
        with QSignalBlocker(self.cut), QSignalBlocker(self.end):
            self.cut.setChecked(enabled)
            # A background load must not discard an RT the user is still typing.
            if getattr(self, "_last_end", None) != end or not self.end.hasFocus():
                self.end.setValue(end)
            self._last_end = end
        ms_enabled, ms_end = self.ws.solvent_cut_settings("TIC")
        with QSignalBlocker(self.ms_cut), QSignalBlocker(self.ms_end):
            self.ms_cut.setChecked(ms_enabled)
            if getattr(self, "_last_ms_end", None) != ms_end or not self.ms_end.hasFocus():
                self.ms_end.setValue(ms_end)
            self._last_ms_end = ms_end
        linked = not hs and "ms_solvent" not in self.ws.quant
        self.ms_end.setToolTip("Legacy linked cut: FID end minus this sample's delay. Editing makes MS independent."
                               if linked else "TIC, BPC and EIC solvent end in MS minutes")
        has_blank = any(self.ws.blank_ids(st) for st in self.ws.states())
        for i, action in enumerate(self.blanks):
            with QSignalBlocker(action):
                action.setChecked(self.ws.panel_blank[i])
                action.setEnabled(self.ws.panel_blank[i] or has_blank)

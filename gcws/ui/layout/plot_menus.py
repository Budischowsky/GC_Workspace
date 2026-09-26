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
        self.cut = self.addAction("Solvent cut")
        self.cut.setCheckable(True)
        self.cut.toggled.connect(lambda on: self.ws.set_solvent_cut(on))
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.addWidget(QLabel("Solvent end RT [min, FID time]"))
        self.end = RetentionTimeSpinBox(row)
        self.end.setRange(0, 10000)
        self.end.setDecimals(3)
        self.end.setKeyboardTracking(False)
        self.end.setToolTip("Shared NIAS solvent end; MS follows the FID–MS delay")
        self.end.editingFinished.connect(self._commit)
        layout.addWidget(self.end)
        field = QWidgetAction(self)
        field.setDefaultWidget(row)
        self.addAction(field)
        self.addSeparator()
        self.blanks = []
        for i in range(2):
            menu = self.addMenu(f"Chromatogram {i + 1}")
            action = menu.addAction("subtract blank")
            action.setCheckable(True)
            action.toggled.connect(lambda on, n=i: self.ws.set_panel(n, blank=on))
            self.blanks.append(action)
        for signal in (self.ws.solventCutChanged, self.ws.panelsChanged, self.ws.runChanged,
                       self.ws.runAdded, self.ws.runRemoved):
            signal.connect(self.sync)
        self.aboutToShow.connect(self.sync)
        self.sync()

    def _commit(self):
        self.ws.set_solvent_cut(self.cut.isChecked(), self.end.value())

    def sync(self, *_):
        enabled = bool(self.ws.quant.get("solvent_cut", False))
        end = float((self.ws.quant.get("settings") or {}).get("solvent_end", 5.5))
        with QSignalBlocker(self.cut), QSignalBlocker(self.end):
            self.cut.setChecked(enabled)
            # A background load must not discard an RT the user is still typing.
            if getattr(self, "_last_end", None) != end or not self.end.hasFocus():
                self.end.setValue(end)
            self._last_end = end
        has_blank = any(self.ws.blank_ids(st) for st in self.ws.states())
        for i, action in enumerate(self.blanks):
            with QSignalBlocker(action):
                action.setChecked(self.ws.panel_blank[i])
                action.setEnabled(self.ws.panel_blank[i] or has_blank)

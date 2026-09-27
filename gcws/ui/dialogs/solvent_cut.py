"""Independent detector solvent cuts, with an isolated HS configuration."""
import copy
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QFormLayout, QLabel)
from gcws.ui.layout.plot_menus import RetentionTimeSpinBox


class SolventCutDialog(QDialog):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.hs = ws.quant.get("mode") == "hs_screening"
        self.setWindowTitle("Solvent cut")
        form = QFormLayout(self)
        def row(key, label):
            enabled, end = ws.solvent_cut_settings(key)
            check = QCheckBox(f"{label} solvent cut")
            check.setChecked(enabled)
            spin = RetentionTimeSpinBox()
            spin.setRange(-10000 if key == "TIC" else 0, 10000)
            spin.setDecimals(3)
            spin.setValue(end)
            form.addRow(check)
            form.addRow(f"Solvent end [min, {'MS' if key == 'TIC' else 'FID'} time]", spin)
            return check, spin

        self.enabled, self.end = row("TIC" if self.hs else "FID", "HS TIC/MS" if self.hs else "FID")
        if not self.hs:
            self.ms_enabled, self.ms_end = row("TIC", "TIC/MS")
            self._initial_ms = (self.ms_enabled.isChecked(), self.ms_end.value())
        note = QLabel("Earlier data are excluded from plots, integration and whole-run deconvolution.")
        if not self.hs and "ms_solvent" not in ws.quant:
            note.setText(note.text() + "\nThe legacy MS cut follows each sample's FID–MS delay until edited.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        q = copy.deepcopy(self.ws.quant)
        if self.hs:
            q.setdefault("hs", {}).update(solvent_cut=self.enabled.isChecked(), solvent_end=self.end.value())
        else:
            q["solvent_cut"] = self.enabled.isChecked()
            q.setdefault("settings", {})["solvent_end"] = self.end.value()
            ms_values = (self.ms_enabled.isChecked(), self.ms_end.value())
            if "ms_solvent" in q or ms_values != self._initial_ms:
                q["ms_solvent"] = dict(enabled=ms_values[0], end=ms_values[1])
        if q != self.ws.quant:
            self.ws.push_quant("Solvent cuts", q)
        self.accept()

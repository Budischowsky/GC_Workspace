"""One solvent end shared with the NIAS settings, expressed in FID time."""
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QLabel)


class SolventCutDialog(QDialog):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.setWindowTitle("Solvent cut")
        form = QFormLayout(self)
        self.enabled = QCheckBox("Cut solvent")
        self.enabled.setChecked(bool(ws.quant.get("solvent_cut", False)))
        self.end = QDoubleSpinBox()
        self.end.setRange(0, 10000)
        self.end.setDecimals(3)
        self.end.setValue(float((ws.quant.get("settings") or {}).get("solvent_end", 5.5)))
        form.addRow(self.enabled)
        form.addRow("Solvent end [min] (FID time)", self.end)
        note = QLabel("This is also the NIAS solvent end. MS time follows the FID–MS delay.\n"
                      "Earlier data are excluded from plots, integration and whole-run deconvolution.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        self.ws.set_solvent_cut(self.enabled.isChecked(), self.end.value())
        self.accept()

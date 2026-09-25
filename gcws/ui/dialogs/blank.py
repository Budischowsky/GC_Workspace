"""Settings of the blank subtraction (chromatogram and peak level)."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QVBoxLayout)

from gcws.signal.blank import BlankOptions
from gcws.ui import theme

SOURCES = {"blank": "Blank", "blank_istd": "Blank + ISTD", "both": "Blank and Blank + ISTD (averaged)"}
MODES = {"peaks": "Blank peaks only (keep the sample baseline)", "full": "Whole blank trace (also removes bleed rise)"}


def _combo(items: dict, value: str) -> QComboBox:
    c = QComboBox()
    for k, v in items.items():
        c.addItem(v, k)
    c.setCurrentIndex(max(0, c.findData(value)))
    return c


def _spin(value, lo, hi, decimals, suffix="", step=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    if step:
        s.setSingleStep(step)
    s.setValue(value)
    return s


class BlankOptionsDialog(QDialog):
    def __init__(self, opts: BlankOptions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Blank subtraction")
        self.source = _combo(SOURCES, opts.source)
        self.mode_fid = _combo(MODES, opts.mode_fid)
        self.mode_ms = _combo(MODES, opts.mode_ms)
        self.align = QCheckBox("Align the blank in time (cross-correlation)")
        self.align.setChecked(opts.align == "auto")
        self.max_shift = _spin(opts.max_shift, 0.001, 0.5, 3, " min", 0.005)
        self.scale = _spin(opts.scale, 0.0, 10.0, 3, " ×", 0.05)
        self.clip = QCheckBox("Never below the sample's own baseline")
        self.clip.setChecked(opts.clip)
        self.env = _spin(opts.env_window, 0.05, 5.0, 2, " min", 0.05)
        self.ratio = _spin(opts.ratio_limit, 1.0, 100.0, 1, " ×", 0.5)
        self.spectral = _spin(opts.spectral_min, 0.0, 1.0, 2, "", 0.05)
        self.rt_tol = _spin(opts.rt_tol or 0.0, 0.0, 0.5, 3, " min", 0.005)
        self.rt_tol.setSpecialValueText("automatic (NIAS blank RT tolerance / 0.03 min for MS)")

        trace = QGroupBox("Chromatogram (\"FID − Blank\", \"TIC − Blank\")")
        f = QFormLayout(trace)
        f.addRow("Subtract", self.source)
        f.addRow("FID traces", self.mode_fid)
        f.addRow("MS traces", self.mode_ms)
        f.addRow(self.align)
        f.addRow("Alignment search range", self.max_shift)
        f.addRow("Blank factor", self.scale)
        f.addRow("Blank baseline window", self.env)
        f.addRow(self.clip)
        peaks = QGroupBox("Peak list (\"In blank\" column, hiding blank peaks)")
        g = QFormLayout(peaks)
        g.addRow("Blank level when sample < ", self.ratio)
        g.addRow("MS: spectra must agree (cosine ≥)", self.spectral)
        g.addRow("RT tolerance", self.rt_tol)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        theme.set_primary(bb.button(QDialogButtonBox.Ok))
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint("The NIAS mg/kg quantification keeps its own peak-area blank correction and is "
                                 "not affected by these settings."))
        lay.addWidget(trace)
        lay.addWidget(peaks)
        lay.addWidget(bb)

    def options(self) -> BlankOptions:
        return BlankOptions(source=self.source.currentData(), mode_fid=self.mode_fid.currentData(),
                            mode_ms=self.mode_ms.currentData(), align="auto" if self.align.isChecked() else "off",
                            max_shift=self.max_shift.value(), scale=self.scale.value(), clip=self.clip.isChecked(),
                            ratio_limit=self.ratio.value(), spectral_min=self.spectral.value(),
                            rt_tol=self.rt_tol.value() or None, env_window=self.env.value())

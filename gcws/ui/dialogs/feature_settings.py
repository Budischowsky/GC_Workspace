"""Settings of the feature double determination (``ws.quant["features"]``)."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QSpinBox, QVBoxLayout)

from gcws.features import similarity as SIM
from gcws.features.model import PAIRING_CLASSIC, PAIRING_FEATURES, Settings
from gcws.ui import theme


def _spin(value: float, lo: float, hi: float, step: float, decimals: int = 3, suffix: str = "") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


class FeatureSettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Double determination - settings")
        self.base = settings
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint(
            "Features: the determinations are paired by retention time (after the drift between them) and "
            "by their spectra; a peak found in one determination is searched for in the other (gap filling); "
            "one name per substance from both hit lists. Classic: AutoLib's pairing by name and retention "
            "time, as the NIAS reports always did.", True))
        self.pairing = QComboBox()
        self.pairing.addItem("Features (retention time + spectrum, gap filling, consensus name)", PAIRING_FEATURES)
        self.pairing.addItem("Classic (AutoLib: name, then retention time)", PAIRING_CLASSIC)
        self.pairing.setCurrentIndex(0 if settings.pairing == PAIRING_FEATURES else 1)
        top = QFormLayout()
        top.addRow("Pairing", self.pairing)
        lay.addLayout(top)

        pair = QGroupBox("Pairing")
        f = QFormLayout(pair)
        self.rt_tol = _spin(settings.rt_tol, 0.005, 0.5, 0.005, 3, " min")
        self.rt_tol.setToolTip("After the drift between the determinations is removed")
        self.max_shift = _spin(settings.max_shift, 0.0, 2.0, 0.05, 2, " min")
        self.min_sim = _spin(settings.min_sim, 0.0, 1.0, 0.05, 2)
        self.min_sim.setToolTip("Below: not the same substance (unless the retention times agree exactly: "
                                "then 'spectra differ', red)")
        self.green_sim = _spin(settings.green_sim, 0.0, 1.0, 0.05, 2)
        self.min_ions = QSpinBox()
        self.min_ions.setRange(2, 30)
        self.min_ions.setValue(settings.min_ions)
        self.min_ions.setToolTip("Spectra with fewer co-eluting ions are too weak to compare: the retention "
                                 "time decides")
        self.weights = QComboBox()
        for w in SIM.WEIGHTS.values():
            self.weights.addItem(f"{w.name}  (I^{w.intensity:g} x m/z^{w.mz:g})", w.name)
        self.weights.setCurrentIndex(max(0, self.weights.findData(settings.weights)))
        f.addRow("Retention time tolerance", self.rt_tol)
        f.addRow("Largest drift", self.max_shift)
        f.addRow("Same substance from similarity", self.min_sim)
        f.addRow("Green from similarity", self.green_sim)
        f.addRow("Co-eluting ions to compare", self.min_ions)
        f.addRow("Spectrum weights", self.weights)
        lay.addWidget(pair)

        gap = QGroupBox("Gap filling")
        gap.setCheckable(True)
        gap.setChecked(settings.gap_fill)
        self.gap_box = gap
        g = QFormLayout(gap)
        self.gap_sn = _spin(settings.gap_min_sn, 1.0, 100.0, 0.5, 1)
        self.gap_ions = QSpinBox()
        self.gap_ions.setRange(1, 10)
        self.gap_ions.setValue(settings.gap_min_ions)
        self.gap_cos = _spin(settings.gap_min_cos, 0.0, 1.0, 0.05, 2)
        self.gap_frac = _spin(settings.gap_min_fraction * 100, 0.0, 100.0, 5.0, 0, " %")
        g.addRow("Signal-to-noise at least", self.gap_sn)
        g.addRow("Co-eluting characteristic ions", self.gap_ions)
        g.addRow("Spectrum similarity at least", self.gap_cos)
        g.addRow("Without spectrum: share of the expected height", self.gap_frac)
        lay.addWidget(gap)

        ident = QGroupBox("Identification")
        i = QFormLayout(ident)
        self.search = QCheckBox("Search the consensus spectrum in the libraries")
        self.search.setChecked(settings.consensus_search)
        self.margin = _spin(settings.id_margin, 0.0, 50.0, 0.5, 1, " points")
        self.margin.setToolTip("How far the chosen candidate must lead the next one")
        i.addRow(self.search)
        i.addRow("A name must lead by", self.margin)
        lay.addWidget(ident)

        self.auto = QCheckBox("Apply gap fills and names automatically (one undo step)")
        self.auto.setChecked(settings.apply_auto)
        self.harmonise = QCheckBox("Propose harmonised integration boundaries")
        self.harmonise.setChecked(settings.harmonise)
        self.split_sync = QCheckBox("Carry deconvolution splits over to the other determination")
        self.split_sync.setToolTip("A peak split by deconvolution in one determination is split the same way "
                                   "in the other when the components fit its trace")
        self.split_sync.setChecked(settings.split_sync)
        lay.addWidget(self.auto)
        lay.addWidget(self.harmonise)
        lay.addWidget(self.split_sync)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._defaults)
        lay.addWidget(bb)

    def _defaults(self):
        d = Settings()
        self.pairing.setCurrentIndex(0)
        for w, v in ((self.rt_tol, d.rt_tol), (self.max_shift, d.max_shift), (self.min_sim, d.min_sim),
                     (self.green_sim, d.green_sim), (self.gap_sn, d.gap_min_sn), (self.gap_cos, d.gap_min_cos),
                     (self.gap_frac, d.gap_min_fraction * 100), (self.margin, d.id_margin)):
            w.setValue(v)
        self.min_ions.setValue(d.min_ions)
        self.gap_ions.setValue(d.gap_min_ions)
        self.weights.setCurrentIndex(max(0, self.weights.findData(d.weights)))
        self.gap_box.setChecked(d.gap_fill)
        self.search.setChecked(d.consensus_search)
        self.auto.setChecked(d.apply_auto)
        self.harmonise.setChecked(d.harmonise)
        self.split_sync.setChecked(d.split_sync)

    def settings(self) -> Settings:
        d = self.base.to_dict()
        d.update(pairing=self.pairing.currentData(), rt_tol=self.rt_tol.value(), max_shift=self.max_shift.value(),
                 min_sim=self.min_sim.value(), green_sim=self.green_sim.value(), min_ions=self.min_ions.value(),
                 weights=self.weights.currentData(), gap_fill=self.gap_box.isChecked(),
                 gap_min_sn=self.gap_sn.value(), gap_min_ions=self.gap_ions.value(),
                 gap_min_cos=self.gap_cos.value(), gap_min_fraction=self.gap_frac.value() / 100.0,
                 consensus_search=self.search.isChecked(), id_margin=self.margin.value(),
                 apply_auto=self.auto.isChecked(), harmonise=self.harmonise.isChecked(),
                 split_sync=self.split_sync.isChecked())
        return Settings.from_dict(d)

"""Search in own library: one chosen library with its own search options.

The Mass spectrum panel's *Own library* button searches the spectrum on display in the
library picked here (its arrow menu switches the library); Identify > Own library search
options... sets the same options. They are kept in ``QSettings("ownsearch/...")``.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QSpinBox, QVBoxLayout)

DEFAULTS = {"library": "", "algorithm": "similarity", "min_score": 0, "top_n": 10, "mz_auto": True,
            "min_mz": 35, "max_mz": 600, "threshold": 0.0, "dedupe": True}
ALGORITHMS = {"similarity": "Similarity (NIST style, match factor)", "pbm": "PBM (Agilent, Qual 0-99)"}


def load_options() -> dict:
    s = QSettings()
    out = {}
    for k, v in DEFAULTS.items():
        raw = s.value(f"ownsearch/{k}", v)
        out[k] = (raw in (True, "true", "1", 1)) if isinstance(v, bool) else type(v)(raw)
    return out


def save_options(opts: dict) -> None:
    s = QSettings()
    for k in DEFAULTS:
        if k in opts:
            s.setValue(f"ownsearch/{k}", opts[k])


def libraries() -> list[str]:
    """Names of the libraries in use (Identify > Libraries...)."""
    from gcws.libsearch import store
    return [x.name for x in store.load() if x.enabled]


def method_from_options(opts: dict):
    """A one-library search method with the own-library options."""
    import gc_search_method as SM
    m = SM.SearchMethod(name=f"Own library: {opts.get('library') or '-'}")
    m.libraries = [SM.LibraryEntry(opts["library"], True, int(opts.get("min_score", 0)))] \
        if opts.get("library") else []
    m.algorithm = opts.get("algorithm", "similarity")
    m.min_score = int(opts.get("min_score", 0))
    m.top_n = max(1, min(int(opts.get("top_n", 10)), SM.MAX_TOP_N))
    m.mz_auto = bool(opts.get("mz_auto", True))
    m.min_mz, m.max_mz = int(opts.get("min_mz", 35)), int(opts.get("max_mz", 600))
    m.threshold = float(opts.get("threshold", 0.0))
    m.dedupe = bool(opts.get("dedupe", True))
    return m


class OwnSearchOptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Own library search - options")
        o = load_options()
        self.library = QComboBox()
        names = libraries()
        self.library.addItems(names)
        if o["library"] in names:
            self.library.setCurrentText(o["library"])
        self.algorithm = QComboBox()
        for k, v in ALGORITHMS.items():
            self.algorithm.addItem(v, k)
        self.algorithm.setCurrentIndex(max(0, self.algorithm.findData(o["algorithm"])))
        self.min_score = QSpinBox()
        self.min_score.setRange(0, 99)
        self.min_score.setValue(o["min_score"])
        self.min_score.setToolTip("Hits below this score are not listed")
        self.top_n = QSpinBox()
        self.top_n.setRange(1, 50)
        self.top_n.setValue(o["top_n"])
        self.mz_auto = QCheckBox("From the spectrum")
        self.mz_auto.setChecked(o["mz_auto"])
        self.min_mz, self.max_mz = QSpinBox(), QSpinBox()
        for w, v in ((self.min_mz, o["min_mz"]), (self.max_mz, o["max_mz"])):
            w.setRange(1, 2000)
            w.setValue(v)
        self.mz_auto.toggled.connect(lambda on: (self.min_mz.setEnabled(not on), self.max_mz.setEnabled(not on)))
        self.mz_auto.toggled.emit(self.mz_auto.isChecked())
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0, 20)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(o["threshold"])
        self.threshold.setToolTip("Ions below this relative abundance are left out of the search")
        self.dedupe = QCheckBox("Each compound only once in the hit list")
        self.dedupe.setChecked(o["dedupe"])
        mz = QHBoxLayout()
        mz.addWidget(self.mz_auto)
        mz.addWidget(self.min_mz)
        mz.addWidget(QLabel("-"))
        mz.addWidget(self.max_mz)
        f = QFormLayout()
        f.addRow("Library", self.library)
        f.addRow("Algorithm", self.algorithm)
        f.addRow("Minimum score", self.min_score)
        f.addRow("Hits", self.top_n)
        f.addRow("m/z range", mz)
        f.addRow("Intensity threshold", self.threshold)
        f.addRow("", self.dedupe)
        note = QLabel("No library here? Add it under Identify > Libraries..." if not names else
                      "The Own library button of the Mass spectrum panel searches the spectrum on display in "
                      "this library.")
        note.setObjectName("hint")
        note.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(note)
        lay.addWidget(bb)

    def values(self) -> dict:
        return {"library": self.library.currentText(), "algorithm": self.algorithm.currentData(),
                "min_score": self.min_score.value(), "top_n": self.top_n.value(), "mz_auto": self.mz_auto.isChecked(),
                "min_mz": self.min_mz.value(), "max_mz": self.max_mz.value(), "threshold": self.threshold.value(),
                "dedupe": self.dedupe.isChecked()}

    def _ok(self):
        save_options(self.values())
        self.accept()

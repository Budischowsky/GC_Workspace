"""Properties of the active chromatogram: metadata, role, blanks, FID-MS delay."""
from __future__ import annotations

from PySide6.QtCore import Signal as QtSignal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from gcws.io.folders import sources
from gcws.io.sequence import ROLE_LABELS
from gcws.ui.undo import ValueCommand


def _label(text=""):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextInteractionFlags(lab.textInteractionFlags() | lab.textInteractionFlags().TextSelectableByMouse)
    return lab


class PropertiesDock(QScrollArea):
    # handled by the main window; a floating dock's window() is the dock itself
    assignBlanksRequested = QtSignal(str)        # run id
    roleRequested = QtSignal(str, str)           # run id, role

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)

        info = QGroupBox("Chromatogram")
        f = QFormLayout(info)
        self.name = _label()
        self.path = _label()
        self.acq = _label()
        self.method = _label()
        self.src = _label()
        self.notes = _label()
        for k, w in (("Sample", self.name), ("Folder", self.path), ("Acquired", self.acq),
                     ("Acq. method", self.method), ("Raw data", self.src), ("Notes", self.notes)):
            f.addRow(k, w)
        lay.addWidget(info)

        roles = QGroupBox("Role and blanks")
        g = QFormLayout(roles)
        self.role = QComboBox()
        for k, v in ROLE_LABELS.items():
            self.role.addItem(v, k)
        self.role.activated.connect(self._set_role)
        self.blanks = _label()
        edit = QPushButton("Assign blanks...")
        edit.clicked.connect(lambda: self.ws.active_id and self.assignBlanksRequested.emit(self.ws.active_id))
        g.addRow("Role", self.role)
        g.addRow("Blanks", self.blanks)
        g.addRow("", edit)
        lay.addWidget(roles)

        delay = QGroupBox("FID → MS retention time offset")
        d = QFormLayout(delay)
        self.delay_est = _label()
        self.delay_override = QCheckBox("Manual value")
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setDecimals(4)
        self.delay_spin.setRange(-1, 1)
        self.delay_spin.setSingleStep(0.0005)
        self.delay_spin.setSuffix(" min")
        apply_delay = QPushButton("Apply")
        apply_delay.clicked.connect(self._set_delay)
        d.addRow("Estimated", self.delay_est)
        d.addRow(self.delay_override, self.delay_spin)
        d.addRow("", apply_delay)
        lay.addWidget(delay)

        integ = QGroupBox("Integration")
        h = QFormLayout(integ)
        self.noise = _label()
        self.resolved = _label()
        self.digest = _label()
        h.addRow("Noise", self.noise)
        h.addRow("Parameters used", self.resolved)
        h.addRow("Result digest", self.digest)
        lay.addWidget(integ)
        lay.addStretch(1)

        for sig in (ws.activeRunChanged, ws.runChanged, ws.signalKeyChanged):
            sig.connect(lambda *_: self.refresh())
        ws.resultChanged.connect(lambda rid, key: self.refresh() if rid == ws.active_id else None)
        self.refresh()

    def refresh(self):
        st = self.ws.active
        if st is None:
            for w in (self.name, self.path, self.acq, self.method, self.src, self.notes, self.blanks,
                      self.delay_est, self.noise, self.resolved, self.digest):
                w.setText("")
            return
        run = st.run
        self.name.setText(st.name)
        self.path.setText(str(run.path))
        self.acq.setText(run.meta.acquired.replace("T", " ")[:19] if run.meta else "")
        self.method.setText(run.meta.method if run.meta else "")
        self.src.setText(", ".join(f"{k}: {v}" for k, v in sources(run.path).items()))
        self.notes.setText("; ".join(run.load_notes) or "-")
        self.role.setCurrentIndex(max(0, self.role.findData(st.role)))
        names = lambda ids: ", ".join(self.ws.runs[i].name for i in ids if i in self.ws.runs) or "-"
        self.blanks.setText(f"Blank: {names(st.blanks)}\nBlank+ISTD: {names(st.blanks_istd)}")
        if st.delay is not None:
            self.delay_est.setText(f"{st.delay.value:.4f} min ({st.delay.method}, quality {st.delay.quality:.2f})")
        else:
            self.delay_est.setText("no MS or FID signal" if run.ms is None or run.fid is None else "-")
        self.delay_override.setChecked(st.delay_override is not None)
        self.delay_spin.setValue(st.delay_value)
        res = st.results.get(self.ws.effective_key(st))
        if res is not None:
            r = res.resolved
            self.noise.setText(f"σ {r.noise.sigma:.4g}, peak-to-peak {r.noise.pp:.4g} "
                               f"({r.noise.t0:.2f}-{r.noise.t1:.2f} min)")
            auto = set(r.auto_fields)
            mark = lambda k: " (auto)" if k in auto else ""
            self.resolved.setText(f"peak width {r.peak_width * 60:.2f} s{mark('peak_width')}, "
                                  f"smoothing {r.window} pts{mark('smoothing_window')}, "
                                  f"slope {r.slope_mult:.1f}×σ{mark('slope_sensitivity')}, "
                                  f"threshold {r.threshold:.4g}{mark('threshold')}")
            self.digest.setText(f"{res.method_name}  •  {len(res.peaks)} peaks  •  {res.digest}")

    def _set_role(self, *_):
        st = self.ws.active
        if st is not None:
            self.roleRequested.emit(st.id, self.role.currentData())

    def _set_delay(self):
        st = self.ws.active
        if st is None:
            return
        new = self.delay_spin.value() if self.delay_override.isChecked() else None
        rid = st.id

        def setter(v):
            s = self.ws.runs.get(rid)
            if s is not None:
                s.delay_override = v
                self.ws.runChanged.emit(rid)
                self.ws.selectionChanged.emit(rid, self.ws.selected)

        st.undo.push(ValueCommand(f"FID-MS offset of {st.name}", lambda: self.ws.runs[rid].delay_override,
                                  setter, new, lambda t, o, n: self.ws.log(t, st.name, "", str(o), str(n))))

"""Preferences: shared folders, external programs, working rules."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from gcws import paths


def _settings_file() -> Path:
    return paths.DATA / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    _settings_file().parent.mkdir(parents=True, exist_ok=True)
    _settings_file().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _path_row(edit: QLineEdit, folder=True, filt=""):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, 1)
    b = QPushButton("...")
    b.setMaximumWidth(32)

    def pick():
        if folder:
            d = QFileDialog.getExistingDirectory(w, "Folder", edit.text())
        else:
            d, _ = QFileDialog.getOpenFileName(w, "File", edit.text(), filt)
        if d:
            edit.setText(d)
    b.clicked.connect(pick)
    h.addWidget(b)
    return w


class PreferencesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.data = load_settings()
        qs = QSettings()
        self.register = QLineEdit(self.data.get("unknown_register_dir", str(paths.DATA)))
        nias_reg = self.data.get("nias_unknown_register_dir")
        share = QPushButton("Share the NIAS register")
        share.setEnabled(bool(nias_reg))
        share.setToolTip(nias_reg or "no NIAS register found")
        share.clicked.connect(lambda: self.register.setText(nias_reg or ""))
        own = QPushButton("Own register")
        own.clicked.connect(lambda: self.register.setText(str(paths.DATA)))
        cas = self.data.get("standard_cas_path", "CASINFO.xlsx")
        self.cas = QLineEdit(cas)
        atlas_cfg = paths.LEGACY / "ei_atlas_config.json"
        try:
            self.atlas_data = json.loads(atlas_cfg.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.atlas_data = {}
        self.atlas = QLineEdit(self.atlas_data.get("atlas_root", ""))
        self.atlas.setPlaceholderText("automatic (sibling folder UnknownEvaluation)")
        self.nist = QLineEdit()
        try:
            import gc_nist
            self.nist.setText(str(gc_nist.load_settings().get("mssearch_dir", "")))
        except Exception:  # noqa: BLE001
            pass
        self.nist.setPlaceholderText("automatic")
        self.lib2nist = QLineEdit(qs.value("prefs/lib2nist", "") or "")
        self.lib2nist.setPlaceholderText("automatic (NIST MS Search / SpectrAtlas Library\Software)")
        self.reason = QCheckBox("Ask for a reason for every manual integration (GLP)")
        self.reason.setChecked(qs.value("prefs/require_reason", False, type=bool))
        self.sticky = QCheckBox("Keep an integration tool active after use")
        self.sticky.setChecked(qs.value("prefs/sticky_tools", True, type=bool))
        f = QFormLayout()
        reg = QHBoxLayout()
        reg.addWidget(share)
        reg.addWidget(own)
        reg.addStretch(1)
        f.addRow("Unknown register folder", _path_row(self.register))
        f.addRow("", reg)
        f.addRow("CAS reference (CASINFO.xlsx)", _path_row(self.cas, False, "Excel (*.xlsx)"))
        f.addRow("SpectrAtlas folder", _path_row(self.atlas))
        f.addRow("NIST MSSEARCH folder", _path_row(self.nist))
        f.addRow("Lib2NIST (Edit library)", _path_row(self.lib2nist, False, "lib2nist.exe (lib2nist.exe)"))
        f.addRow("", self.reason)
        f.addRow("", self.sticky)
        note = QLabel(f"Data folder: {paths.DATA}")
        note.setObjectName("hint")
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(note)
        lay.addWidget(bb)
        self.resize(640, 300)

    def _ok(self):
        self.data["unknown_register_dir"] = self.register.text().strip() or str(paths.DATA)
        self.data["standard_cas_path"] = self.cas.text().strip() or "CASINFO.xlsx"
        save_settings(self.data)
        self.atlas_data["atlas_root"] = self.atlas.text().strip()
        try:
            (paths.LEGACY / "ei_atlas_config.json").write_text(json.dumps(self.atlas_data, indent=2),
                                                                encoding="utf-8")
        except OSError:
            pass
        if self.nist.text().strip():
            try:
                import gc_nist
                gc_nist.remember_mssearch_dir(self.nist.text().strip())
            except Exception:  # noqa: BLE001
                pass
        qs = QSettings()
        qs.setValue("prefs/require_reason", self.reason.isChecked())
        qs.setValue("prefs/sticky_tools", self.sticky.isChecked())
        qs.setValue("prefs/lib2nist", self.lib2nist.text().strip())
        self.accept()

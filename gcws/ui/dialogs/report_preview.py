"""Report preview: the Word report rendered page by page."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider,
                               QVBoxLayout, QWidget)


def _qimage(page):
    """A page as ``QImage`` (already one, or a PIL image)."""
    if isinstance(page, QImage):
        return page
    pil = page.convert("RGB")
    data = pil.tobytes("raw", "RGB")
    img = QImage(data, pil.width, pil.height, pil.width * 3, QImage.Format_RGB888)
    return img.copy()


class ReportPreview(QDialog):
    def __init__(self, title, files: dict, pages: list, parent=None, warnings=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 1000)
        self.files = files          # label -> temp path
        self.images = [_qimage(p) for p in pages]
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.body = QWidget()
        self.col = QVBoxLayout(self.body)
        self.col.setAlignment(Qt.AlignHCenter)
        self.area.setWidget(self.body)
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(40, 200)
        self.zoom.setValue(80)
        self.zoom.valueChanged.connect(self._render)
        save = QPushButton("Save report...")
        save.clicked.connect(self.save)
        word = QPushButton("Open in Word")
        word.clicked.connect(lambda: os.startfile(str(self.files["docx"])) if self.files.get("docx") else None)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Zoom"))
        bar.addWidget(self.zoom)
        bar.addStretch(1)
        bar.addWidget(word)
        bar.addWidget(save)
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        notes = list(warnings or [])
        if not self.images:
            notes.insert(0, "No page images: the preview needs Microsoft Word and pypdfium2. "
                            "Use 'Open in Word' to look at the report.")
        self.notes = QLabel("\n".join(notes))
        self.notes.setWordWrap(True)
        self.notes.setObjectName("warning")
        self.notes.setVisible(bool(notes))
        lay.addWidget(self.notes)
        lay.addWidget(self.area, 1)
        self._render()
        self.saved_to = None

    def _render(self):
        while self.col.count():
            w = self.col.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        f = self.zoom.value() / 100.0
        for img in self.images:
            lab = QLabel()
            lab.setPixmap(QPixmap.fromImage(img).scaledToWidth(int(img.width() * f), Qt.SmoothTransformation))
            lab.setStyleSheet("background:white; border:1px solid #bbb;")
            self.col.addWidget(lab)

    def save(self):
        xlsx = self.files.get("xlsx")
        if xlsx is None:
            return
        target, _ = QFileDialog.getSaveFileName(self, "Save report", self.files.get("default", xlsx.name),
                                                "Excel (*.xlsx)")
        if not target:
            return
        target = Path(target)
        shutil.copy2(xlsx, target)
        if self.files.get("docx") and Path(self.files["docx"]).exists():
            shutil.copy2(self.files["docx"], target.with_suffix(".docx"))
        if self.files.get("batch") and Path(self.files["batch"]).exists():
            shutil.copy2(self.files["batch"], target.parent / Path(self.files["batch"]).name)
        self.saved_to = target
        self.accept()

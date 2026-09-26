"""Export Chromatogram 1 / 2 as a picture (PNG, JPEG, TIFF, BMP, SVG or PDF).

The plots are rendered afresh at the requested size, not screen-grabbed: the
plot item is laid out at the target size for the moment of painting, so the
axes and labels are placed for that size. Raster images are drawn at a device
pixel ratio (text and layout scale evenly) and lines are thickened by the
same factor, so a 3x image looks like the screen, only sharper. The cursor
line, drag band and tool previews are left out.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QMarginsF, QRectF, QSettings, QSizeF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPageLayout, QPageSize, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout)

from gcws.ui import theme

FORMATS = {"PNG": ".png", "JPEG": ".jpg", "TIFF": ".tif", "BMP": ".bmp", "SVG": ".svg", "PDF": ".pdf"}
RASTER = {"PNG", "JPEG", "TIFF", "BMP"}
TITLE_H = 26


def _scaled_pen(pen, f: float) -> QPen:
    p = QPen(pen)
    p.setWidthF(max(p.widthF(), 1.0) * f)
    return p


@contextmanager
def export_state(panels, line_scale: float = 1.0):
    """Hide the interactive helpers and thicken the lines by ``line_scale`` while exporting."""
    hidden, restore = [], []
    for panel in panels:
        for it in (panel.cursor, panel.cursor_label, panel.vb.band, panel.vb.preview):
            if it.isVisible():
                it.hide()
                hidden.append(it)
        if line_scale != 1.0:
            for c in panel.curves.values():
                pen = QPen(c.opts["pen"])
                restore.append((c.setPen, pen))
                c.setPen(_scaled_pen(pen, line_scale))
            pi = panel.plot.getPlotItem()
            for name in ("left", "bottom"):
                ax = pi.getAxis(name)
                pen = QPen(ax.pen())
                restore.append((ax.setPen, pen))
                ax.setPen(_scaled_pen(pen, line_scale))
            for line in panel.event_lines:
                pen = QPen(line.pen)
                restore.append((line.setPen, pen))
                line.setPen(_scaled_pen(pen, line_scale))
            panel.peaks.pen_scale = line_scale
            restore.append((lambda _v, p=panel: setattr(p.peaks, "pen_scale", 1.0), None))
    try:
        yield
    finally:
        for setter, value in reversed(restore):
            setter(value)
        for it in hidden:
            it.show()


def _render_plot(panel, painter: QPainter, target: QRectF) -> None:
    """Lay the panel's plot out at ``target``'s size and paint it there."""
    pi = panel.plot.getPlotItem()
    scene = panel.plot.scene()
    old = QSizeF(pi.size())
    pi.resize(target.width(), target.height())
    pi.layout.activate()
    try:
        painter.fillRect(target, QColor(theme.PLOT["bg"]))
        scene.render(painter, target, pi.sceneBoundingRect(), Qt.IgnoreAspectRatio)
    finally:
        pi.resize(old)
        pi.layout.activate()


def render(panels, painter: QPainter, width: float, height_each: float, title: str = "") -> None:
    """Paint ``panels`` stacked (``height_each`` logical px each) under an optional title line.

    Pictures are always light (white paper), also in the dark theme."""
    with theme.light_plots():
        _render(panels, painter, width, height_each, title)


def _render(panels, painter: QPainter, width: float, height_each: float, title: str = "") -> None:
    y = 0.0
    painter.fillRect(QRectF(0, 0, width, total_height(len(panels), height_each, title)), QColor("white"))
    if title:
        f = painter.font()
        f.setPixelSize(14)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRectF(10, 0, width - 20, TITLE_H), Qt.AlignLeft | Qt.AlignVCenter, title)
        y = TITLE_H
    for p in panels:
        _render_plot(p, painter, QRectF(0, y, width, height_each))
        y += height_each


def total_height(n: int, height_each: float, title: str = "") -> float:
    return n * height_each + (TITLE_H if title else 0)


def image(panels, width: int, height_each: int, scale: float = 1.0, title: str = "") -> QImage:
    """The panels as an image of ``width x height`` logical px at ``scale`` device pixels per px."""
    h = total_height(len(panels), height_each, title)
    img = QImage(int(round(width * scale)), int(round(h * scale)), QImage.Format_RGB32)
    img.setDevicePixelRatio(scale)
    img.fill(QColor("white"))
    with export_state(panels, scale):
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        render(panels, painter, width, height_each, title)
        painter.end()
    # the resolution is file metadata only: set after painting, or point-sized fonts grow twice
    dpm = int(round(96 * scale / 0.0254))
    img.setDotsPerMeterX(dpm)
    img.setDotsPerMeterY(dpm)
    return img


def export(panels, path, width: int, height_each: int, scale: float = 1.0, title: str = "") -> Path:
    """Write the panels to ``path``; the format follows the extension."""
    path = Path(path)
    ext = path.suffix.lower()
    h = total_height(len(panels), height_each, title)
    if ext == ".svg":
        from PySide6.QtSvg import QSvgGenerator
        gen = QSvgGenerator()
        gen.setFileName(str(path))
        gen.setSize(QSizeF(width, h).toSize())
        gen.setViewBox(QRectF(0, 0, width, h))
        gen.setTitle(title or "Chromatogram")
        gen.setDescription("GC Workspace")
        with export_state(panels):
            painter = QPainter(gen)
            painter.setRenderHint(QPainter.Antialiasing)
            render(panels, painter, width, height_each, title)
            painter.end()
    elif ext == ".pdf":
        from PySide6.QtGui import QPdfWriter
        writer = QPdfWriter(str(path))
        writer.setResolution(96)
        mm = 25.4 / 96
        writer.setPageLayout(QPageLayout(QPageSize(QSizeF(width * mm, h * mm), QPageSize.Millimeter),
                                         QPageLayout.Portrait, QMarginsF(0, 0, 0, 0)))
        writer.setTitle(title or "Chromatogram")
        writer.setCreator("GC Workspace")
        with export_state(panels):
            painter = QPainter(writer)
            painter.setRenderHint(QPainter.Antialiasing)
            render(panels, painter, width, height_each, title)
            painter.end()
    else:
        img = image(panels, width, height_each, scale, title)
        fmt = {".jpg": "JPEG", ".jpeg": "JPEG", ".tif": "TIFF", ".tiff": "TIFF", ".bmp": "BMP"}.get(ext, "PNG")
        if not img.save(str(path), fmt, 95 if fmt == "JPEG" else -1):
            raise OSError(f"could not write {path}")
    if not path.is_file() or path.stat().st_size == 0:
        raise OSError(f"could not write {path}")
    return path


class ExportChromatogramDialog(QDialog):
    """File > Export chromatogram... and the Export button of each chromatogram."""

    def __init__(self, win, which: int = 0):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Export chromatogram")
        s = QSettings()
        self.what = QComboBox()
        self.what.addItems(["Chromatogram 1", "Chromatogram 2", "Both, stacked"])
        self.what.setCurrentIndex(which)
        self.fmt = QComboBox()
        self.fmt.addItems(list(FORMATS))
        self.fmt.setCurrentText(s.value("export_chrom/format", "PNG"))
        plot = win.chrom.plot
        self.width_px = QSpinBox()
        self.width_px.setRange(200, 20000)
        self.width_px.setSuffix(" px")
        self.width_px.setValue(s.value("export_chrom/width", max(1200, plot.width()), type=int))
        self.height_px = QSpinBox()
        self.height_px.setRange(100, 20000)
        self.height_px.setSuffix(" px")
        self.height_px.setValue(s.value("export_chrom/height", max(400, plot.height()), type=int))
        self.scale = QComboBox()
        for label, f in (("screen (1×, 96 dpi)", 1.0), ("2× (192 dpi)", 2.0), ("3× (288 dpi, print)", 3.0)):
            self.scale.addItem(label, f)
        self.scale.setCurrentIndex(max(0, self.scale.findData(s.value("export_chrom/scale", 2.0, type=float))))
        self.with_title = QCheckBox("Title line")
        self.with_title.setChecked(s.value("export_chrom/title", True, type=bool))
        self.title = QLineEdit()
        self.preview = QLabel()
        self.preview.setMinimumSize(520, 240)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(f"background: {theme.SURFACE_ALT}; border: 1px solid {theme.BORDER};")
        self.size_note = theme.hint("", False)
        form = QFormLayout()
        form.addRow("Chromatogram", self.what)
        form.addRow("Format", self.fmt)
        size = QHBoxLayout()
        size.addWidget(self.width_px)
        size.addWidget(QLabel("×"))
        size.addWidget(self.height_px)
        size.addWidget(theme.hint("per chromatogram", False))
        size.addStretch(1)
        form.addRow("Size", size)
        form.addRow("Resolution", self.scale)
        t = QHBoxLayout()
        t.addWidget(self.with_title)
        t.addWidget(self.title, 1)
        form.addRow("", t)
        form.addRow("", self.size_note)
        copy = QPushButton("Copy to clipboard")
        copy.clicked.connect(self.copy)
        save = QPushButton("Save...")
        save.setDefault(True)
        theme.set_primary(save)
        save.clicked.connect(self.save)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(copy)
        buttons.addStretch(1)
        buttons.addWidget(save)
        buttons.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.preview, 1)
        lay.addLayout(buttons)
        for w in (self.what, self.fmt, self.scale):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.width_px, self.height_px):
            w.valueChanged.connect(self._changed)
        self.with_title.toggled.connect(self._changed)
        self.title.textChanged.connect(self._update_preview)
        self.title.setText(self.default_title())
        self._changed()

    # -- choices ----------------------------------------------------------------------------

    def panels(self):
        i = self.what.currentIndex()
        return [self.win.chrom, self.win.chrom2] if i == 2 else [self.win.chroms[i]]

    def title_text(self) -> str:
        return self.title.text().strip() if self.with_title.isChecked() else ""

    def default_title(self) -> str:
        st = self.win.ws.active
        keys = " / ".join(p.key.replace(" - Blank", " − Blank") for p in self.panels())
        name = st.name if st is not None else ""
        return f"{name}   {keys}   {datetime.now():%Y-%m-%d}".strip()

    def default_name(self) -> str:
        import re
        st = self.win.ws.active
        keys = "_".join(p.key.replace(" - Blank", "-Blank").replace(" ", "") for p in self.panels())
        stem = f"{st.name if st is not None else 'chromatogram'}_{keys}_chromatogram"
        return re.sub(r'[<>:"/\\|?*]+', "_", stem) + FORMATS[self.fmt.currentText()]

    def _changed(self, *_):
        raster = self.fmt.currentText() in RASTER
        self.scale.setEnabled(raster)
        n = len(self.panels())
        w, h = self.width_px.value(), total_height(n, self.height_px.value(), self.title_text())
        if raster:
            f = self.scale.currentData()
            self.size_note.setText(f"Image: {int(w * f)} × {int(h * f)} pixels")
        else:
            self.size_note.setText(f"Vector graphic: {w} × {int(h)} px ({w * 25.4 / 96:.0f} × {h * 25.4 / 96:.0f} mm)")
        self.title.setEnabled(self.with_title.isChecked())
        self._update_preview()

    def _update_preview(self, *_):
        panels = self.panels()
        img = image(panels, self.width_px.value(), self.height_px.value(), 1.0, self.title_text())
        pm = QPixmap.fromImage(img).scaled(self.preview.width() - 8, self.preview.height() - 8,
                                           Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview.setPixmap(pm)

    def _remember(self, folder: str | None = None):
        s = QSettings()
        s.setValue("export_chrom/format", self.fmt.currentText())
        s.setValue("export_chrom/width", self.width_px.value())
        s.setValue("export_chrom/height", self.height_px.value())
        s.setValue("export_chrom/scale", self.scale.currentData())
        s.setValue("export_chrom/title", self.with_title.isChecked())
        if folder:
            s.setValue("export_chrom/dir", folder)

    # -- output -------------------------------------------------------------------------------

    def copy(self):
        img = image(self.panels(), self.width_px.value(), self.height_px.value(),
                    self.scale.currentData() or 1.0, self.title_text())
        QGuiApplication.clipboard().setImage(img)
        self._remember()
        self.win.statusBar().showMessage("Chromatogram copied to the clipboard", 4000)

    def save(self, path=None):
        fmt = self.fmt.currentText()
        if not path:
            st = self.win.ws.active
            folder = QSettings().value("export_chrom/dir", "") or (str(st.run.path.parent) if st else "")
            filters = ";;".join(f"{k} (*{v})" for k, v in FORMATS.items())
            path, chosen = QFileDialog.getSaveFileName(self, "Export chromatogram",
                                                       str(Path(folder) / self.default_name()), filters,
                                                       f"{fmt} (*{FORMATS[fmt]})")
            if not path:
                return None
        path = Path(path)
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".svg", ".pdf"}:
            path = path.with_suffix(FORMATS[fmt])
        try:
            export(self.panels(), path, self.width_px.value(), self.height_px.value(),
                   self.scale.currentData() or 1.0, self.title_text())
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "Export chromatogram", str(exc))
            return None
        self._remember(str(path.parent))
        self.win.statusBar().showMessage(f"Chromatogram saved: {path}", 6000)
        self.win.ws.log("Chromatogram exported", self.win.ws.active.name if self.win.ws.active else "", str(path))
        self.accept()
        return path

"""Small viewport controls: never part of the plot scene or its exports."""
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QPainter, QRegion
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QMenu,
                               QSizePolicy, QToolButton, QWidget)


class ElidedLabel(QLabel):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignVCenter | Qt.AlignLeft, text)


class PlotOverlay(QWidget):
    def __init__(self, plot, widgets, *, bottom=False):
        super().__init__(plot.viewport())
        self.plot = plot
        self.bottom = bottom
        self.widgets = list(widgets)
        self.suppressed = set()
        self.overflowed = []
        self.setObjectName("plotControls")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 1, 3, 1)
        layout.setSpacing(5)
        for w in self.widgets:
            w.setParent(self)
            if isinstance(w, QLabel) and w.objectName() != "chip":
                w.setMinimumWidth(0)
                w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            layout.addWidget(w, 1 if isinstance(w, QLabel) and w.objectName() != "chip" else 0)
        self.more = QToolButton(self)
        self.more.setText("⋯")
        self.more.setToolTip("More plot controls")
        self.more.setPopupMode(QToolButton.InstantPopup)
        self.menu = QMenu(self.more)
        self.menu.aboutToShow.connect(self._fill_menu)
        self.more.setMenu(self.menu)
        layout.addWidget(self.more)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.reposition)
        self.plot.viewport().installEventFilter(self)
        self.plot.getViewBox().sigResized.connect(self.reposition)
        self.show()
        self.reposition()

    def set_available(self, widget, available):
        if available:
            self.suppressed.discard(widget)
        else:
            self.suppressed.add(widget)
        self.reposition()

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest) and not self._timer.isActive():
            self._timer.start(0)
        return False

    def reposition(self, *_):
        rect = self.plot.mapFromScene(self.plot.getViewBox().sceneBoundingRect()).boundingRect()
        width = max(0, rect.width() - 8)
        available = [w for w in self.widgets if w not in self.suppressed
                     and not (isinstance(w, QLabel) and not w.text())]
        # Labels yield space first; controls then move into the overflow menu.
        def cost(w):
            if isinstance(w, QLabel) and w.objectName() != "chip":
                return min(100, w.sizeHint().width()) + 5
            return w.sizeHint().width() + 5
        remaining = sum(cost(w) for w in available) + 6
        self.overflowed = []
        while available and remaining > width - 28:
            w = available.pop()
            self.overflowed.insert(0, w)
            remaining -= cost(w)
        for w in self.widgets:
            w.setVisible(w in available)
            if isinstance(w, QLabel):
                w.setToolTip(w.text())
                w.setMaximumWidth(max(1, w.sizeHint().width()))
        self.more.setVisible(bool(self.overflowed))
        height = max([w.sizeHint().height() for w in available] + [24]) + 2
        y = rect.bottom() - height - 4 if self.bottom else rect.top() + 4
        self.setGeometry(rect.left() + 4, y, width, height)
        self.layout().activate()
        region = QRegion()
        for w in [*available, self.more]:
            if not w.isHidden():
                region |= QRegion(w.geometry())
        self.setMask(region)
        self.raise_()

    def _fill_menu(self):
        self.menu.clear()
        for w in self.overflowed:
            if isinstance(w, QCheckBox):
                a = self.menu.addAction(w.text())
                a.setCheckable(True)
                a.setChecked(w.isChecked())
                a.setEnabled(w.isEnabled())
                a.toggled.connect(w.setChecked)
            elif isinstance(w, QComboBox):
                m = self.menu.addMenu(w.toolTip() or "Choose")
                for i in range(w.count()):
                    a = m.addAction(w.itemText(i))
                    a.setCheckable(True)
                    a.setChecked(i == w.currentIndex())
                    a.triggered.connect(lambda _=False, c=w, n=i: self._choose(c, n))
            elif isinstance(w, QToolButton):
                if w.defaultAction() is not None:
                    self.menu.addAction(w.defaultAction())
                else:
                    a = self.menu.addAction(w.text(), w.click)
                    a.setEnabled(w.isEnabled())
            elif isinstance(w, QLabel) and w.text():
                self.menu.addAction(w.text()).setEnabled(False)

    @staticmethod
    def _choose(combo, index):
        combo.setCurrentIndex(index)
        combo.activated.emit(index)

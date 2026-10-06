"""The loaded samples as coloured squares in the collapsed Folders strip.

Each square carries a short code: the injection number the data file starts with
(``07_..._A.D`` -> ``07``). The colour is the run's trace colour, the style its role
(sample filled, blank outlined, blank + ISTD outlined with a dot, standard and
alkane ladder with a mark), a ring marks the active run. A click makes the run
active, a right-click opens the same menu as the Loaded samples list.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from gcws.io.sequence import BLANK, BLANK_ISTD, LADDER, ROLE_LABELS, STANDARD
from gcws.ui import theme

_NUMBER = re.compile(r"^(\d{1,3})[_\-\s]")
_REPLICATE = re.compile(r"[_\-\s]([A-Za-z])$")


def sample_code(name: str) -> str:
    """Two or three characters for a run: its injection number, else letters of its name."""
    stem = Path(name).stem if name.lower().endswith((".d", ".qgd")) else name
    m = _NUMBER.match(stem)
    if m:
        return m.group(1).zfill(2)
    word = re.sub(r"^[\d_\-\s]+", "", stem) or stem
    letters = [c for c in word if c.isalnum()]
    if not letters:
        return "?"
    rep = _REPLICATE.search(stem)
    if rep and len(letters) > 1:
        return letters[0].upper() + rep.group(1).upper()
    return (letters[0].upper() + "".join(letters[1:2]).lower())


def sample_codes(names: list[str]) -> list[str]:
    """``sample_code`` of every name; a code used again gets a letter (07, 07b, 07c ...)."""
    codes, seen = [], {}
    for name in names:
        code = sample_code(name)
        n = seen.get(code, 0) + 1
        seen[code] = n
        codes.append(code if n == 1 else code[:2] + "abcdefghijklmnopqrstuvwxyz"[min(n - 1, 25)])
    return codes


def _ink_on(color: QColor) -> QColor:
    """Black or white text, whichever reads better on ``color``."""
    lum = 0.2126 * color.redF() + 0.7152 * color.greenF() + 0.0722 * color.blueF()
    return QColor("#111111") if lum > 0.55 else QColor("#FFFFFF")


class SampleSquare(QAbstractButton):
    SIZE = QSize(28, 26)

    def __init__(self, run_id: str, parent=None):
        super().__init__(parent)
        self.run_id = run_id
        self.color = QColor("#888888")
        self.role = "sample"
        self.active = False
        self.shown = True
        self.setFixedSize(self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def set_state(self, code: str, color: str, role: str, active: bool, shown: bool, tip: str) -> None:
        self.setText(code)
        self.color, self.role, self.active, self.shown = QColor(color), role, active, shown
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.update()

    def sizeHint(self):
        return self.SIZE

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.shown:
            p.setOpacity(0.45)                      # not shown in the overlay
        box = QRectF(3, 3, self.width() - 6, self.height() - 6)
        if self.active:
            p.setPen(QPen(QColor(theme.ACCENT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(-2, -2, 2, 2), 5, 5)
        outlined = self.role in (BLANK, BLANK_ISTD)
        if outlined:
            tint = QColor(self.color)
            tint.setAlpha(40)
            p.setBrush(tint)
            p.setPen(QPen(self.color, 2))
            ink = QColor(theme.TEXT)
        else:
            p.setBrush(self.color)
            p.setPen(QPen(self.color.darker(140), 1))
            ink = _ink_on(self.color)
        p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        if self.role == BLANK_ISTD:
            p.setPen(Qt.NoPen)
            p.setBrush(self.color)
            p.drawEllipse(QPointF(box.right() - 4, box.top() + 4), 2, 2)
        elif self.role == STANDARD:                 # a corner mark top right
            path = QPainterPath(QPointF(box.right() - 7, box.top()))
            path.lineTo(box.right(), box.top())
            path.lineTo(box.right(), box.top() + 7)
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(ink)
            p.drawPath(path)
        elif self.role == LADDER:                   # a bar along the bottom
            p.setPen(Qt.NoPen)
            p.setBrush(ink)
            p.drawRect(QRectF(box.left() + 3, box.bottom() - 3, box.width() - 6, 1.5))
        f = QFont(self.font())
        f.setBold(True)
        f.setPointSizeF(max(6.5, f.pointSizeF() - 1.5) if len(self.text()) > 2 else max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        p.setPen(ink)
        p.drawText(box.adjusted(0, -1, 0, 0), Qt.AlignCenter, self.text())


class SampleRail(QScrollArea):
    """A column of ``SampleSquare``s that follows the workspace's loaded runs."""

    def __init__(self, ws, menu_for, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.menu_for = menu_for                    # run id -> QMenu
        self.setObjectName("sampleRail")
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # the wheel scrolls a long list
        self.setWidgetResizable(True)
        self.setFixedWidth(SampleSquare.SIZE.width())
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)      # fills the strip's height
        body = QWidget()
        body.setObjectName("sampleRailBody")
        self.box = QVBoxLayout(body)
        self.box.setContentsMargins(0, 4, 0, 4)
        self.box.setSpacing(2)
        self.box.addStretch(1)
        self.setWidget(body)
        self.squares: dict[str, SampleSquare] = {}
        for sig in (ws.runAdded, ws.runRemoved, ws.runChanged, ws.orderChanged, ws.activeRunChanged):
            sig.connect(self.sync)
        theme.notifier().changed.connect(self.sync)
        self.sync()

    def sync(self, *_):
        states = self.ws.states()
        ids = [st.id for st in states]
        for rid in list(self.squares):
            if rid not in ids:
                self.squares.pop(rid).deleteLater()
        codes = sample_codes([st.run.path.name or st.name for st in states])
        for i, (st, code) in enumerate(zip(states, codes)):
            sq = self.squares.get(st.id)
            if sq is None:
                sq = self.squares[st.id] = SampleSquare(st.id)
                sq.clicked.connect(lambda _=False, r=st.id: self.ws.set_active(r))
                sq.setContextMenuPolicy(Qt.CustomContextMenu)
                sq.customContextMenuRequested.connect(
                    lambda pos, s=sq: self.menu_for(s.run_id).exec(s.mapToGlobal(pos)))
            self.box.insertWidget(i, sq)            # keeps the workspace's order
            role = ROLE_LABELS.get(st.role, st.role)
            sq.set_state(code, st.color, st.role, st.id == self.ws.active_id, st.visible,
                         f"{st.name} — {role}")

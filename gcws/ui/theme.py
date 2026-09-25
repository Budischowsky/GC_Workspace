"""The application look: one light theme with a single calm accent.

Everything that has a colour takes it from here: the Qt palette and style
sheet, the plots, status colours in tables and the run colours. Widgets mark
muted helper text with ``setObjectName("hint")`` and primary actions with
:func:`set_primary` instead of carrying their own style sheets.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import QApplication, QDockWidget, QLabel

# -- tokens --------------------------------------------------------------------

ACCENT = "#1F6F8B"
ACCENT_HOVER = "#185A71"
ACCENT_PRESSED = "#124658"
ACCENT_SOFT = "#E3F0F4"
ACCENT_SOFT2 = "#C9E1EA"
BG = "#F3F6F8"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#F8FAFB"
BORDER = "#DCE3E8"
BORDER_STRONG = "#BFCBD4"
TEXT = "#1E2A32"
MUTED = "#5F6F7B"
FAINT = "#98A6B0"

OK, OK_SOFT = "#1E8E5A", "#E2F3E9"
WARN, WARN_SOFT = "#B7791F", "#FCF1DA"
BAD, BAD_SOFT = "#C0392B", "#FBE4E0"
INFO, INFO_SOFT = "#2563EB", "#E4ECFD"
NEUTRAL, NEUTRAL_SOFT = "#6B7780", "#EDF0F3"
ORANGE_SOFT = "#FCE9D6"

LEVELS = {"ok": (OK, OK_SOFT), "warn": (WARN, WARN_SOFT), "bad": (BAD, BAD_SOFT),
          "info": (INFO, INFO_SOFT), "neutral": (NEUTRAL, NEUTRAL_SOFT), "accent": (ACCENT, ACCENT_SOFT)}

#: run colours: colour-blind-safe (Okabe-Ito first), all dark enough for thin lines on white
RUN_COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#8C564B", "#6A3D9A",
              "#B8860B", "#E7298A", "#1F3A93", "#11A579", "#A6761D", "#7F3C8D", "#3969AC", "#666666"]

PLOT = {
    "bg": SURFACE,
    "fg": "#46555F",
    "grid_alpha": 0.12,
    "cursor": ACCENT,
    "cursor_text": "#35444E",
    "baseline": "#C0392B",
    "drop": (90, 102, 112, 160),
    "manual_fill": (230, 126, 34, 70),
    "blank_fill": (140, 150, 160, 70),
    "label": "#33424D",
    "label_selected": ACCENT_PRESSED,
    "event": "#7E57C2",
    "off_region": (120, 130, 140, 26),
    "spectrum": ACCENT,
    "reference": "#C0392B",
    "apex_region": ACCENT,
    "bg_region": "#C0392B",
    "secondary": "#46555F",
    "band": (31, 111, 139, 45),
    "band_bg": (192, 57, 43, 45),
}


def qcolor(value, alpha: int | None = None) -> QColor:
    c = QColor(*value) if isinstance(value, tuple) else QColor(value)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def status_brush(level: str) -> QBrush:
    """Soft background brush for a status level (ok, warn, bad, info, neutral, accent)."""
    return QBrush(QColor(LEVELS.get(level, LEVELS["neutral"])[1]))


def status_color(level: str) -> QColor:
    return QColor(LEVELS.get(level, LEVELS["neutral"])[0])


def chip_html(text: str, level: str = "neutral") -> str:
    fg, bg = LEVELS.get(level, LEVELS["neutral"])
    return (f'<span style="background:{bg}; color:{fg}; font-weight:600; '
            f'padding:1px 6px; border-radius:6px;">&nbsp;{text}&nbsp;</span>')


def chip(text: str = "", level: str = "neutral") -> QLabel:
    """A small coloured status label; change it later with :func:`set_chip`."""
    lab = QLabel()
    lab.setObjectName("chip")
    set_chip(lab, text, level)
    return lab


def set_chip(lab: QLabel, text: str, level: str = "neutral") -> None:
    lab.setText(text)
    lab.setProperty("level", level)
    lab.setVisible(bool(text))
    _repolish(lab)


def set_primary(button, on: bool = True) -> None:
    button.setProperty("primary", on)
    _repolish(button)


def hint(text: str = "", wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(wrap)
    return lab


def _repolish(w) -> None:
    st = w.style()
    st.unpolish(w)
    st.polish(w)
    w.update()


# -- style sheet ---------------------------------------------------------------

def _icon_url(name: str) -> str:
    from gcws import paths
    return (paths.RESOURCES / "icons" / name).as_posix()


def qss() -> str:
    down, up, close = _icon_url("chevron-down.svg"), _icon_url("chevron-up.svg"), _icon_url("close.svg")
    return f"""
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px 6px; }}

QMainWindow::separator {{ background: {BG}; width: 5px; height: 5px; }}
QMainWindow::separator:hover {{ background: {ACCENT_SOFT2}; }}
QDockWidget {{ titlebar-close-icon: none; titlebar-normal-icon: none; }}
QDockWidget::title {{
    background: {ACCENT_SOFT}; color: {ACCENT_PRESSED}; padding: 5px 8px 5px 10px;
    border-left: 3px solid {ACCENT_SOFT2}; font-weight: 600; text-align: left;
}}
QDockWidget[active="true"]::title {{ background: {ACCENT}; color: white; border-left: 3px solid {ACCENT_PRESSED}; }}
QDockWidget > QWidget {{ background: {SURFACE}; }}

QToolBar {{ background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER}; padding: 3px 4px; spacing: 3px; }}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 6px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 5px; padding: 3px 5px; background: transparent; }}
QToolButton:hover {{ background: {ACCENT_SOFT}; border-color: {ACCENT_SOFT2}; }}
QToolButton:pressed {{ background: {ACCENT_SOFT2}; }}
QToolButton:checked {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; color: {ACCENT_PRESSED}; }}
QToolButton[primary="true"] {{ background: {ACCENT_SOFT}; border: 1px solid {ACCENT_SOFT2}; color: {ACCENT_PRESSED};
    font-weight: 600; }}
QToolButton[primary="true"]:hover {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; }}

QMenuBar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 4px 9px; background: transparent; }}
QMenuBar::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_PRESSED}; border-radius: 4px; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 20px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_PRESSED}; }}
QMenu::item:disabled {{ color: {FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}

QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 5px 12px; border: none;
    border-bottom: 2px solid transparent; margin-right: 2px; }}
QTabBar::tab:hover {{ color: {TEXT}; background: {SURFACE_ALT}; }}
QTabBar::tab:selected {{ color: {ACCENT_PRESSED}; border-bottom: 2px solid {ACCENT}; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 4px; top: -1px; background: {SURFACE}; }}
QTabBar#runTabs::tab {{ padding: 5px 12px; min-width: 60px; }}
QTabBar::close-button {{ image: url("{close}"); subcontrol-position: right; border-radius: 3px; margin: 2px; }}
QTabBar::close-button:hover {{ background: {BAD_SOFT}; }}

QPushButton {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 5px; padding: 4px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; background: {ACCENT_SOFT}; }}
QPushButton:pressed {{ background: {ACCENT_SOFT2}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; }}
QPushButton:checked {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; }}
QPushButton[primary="true"] {{ background: {ACCENT}; color: white; border-color: {ACCENT}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:pressed {{ background: {ACCENT_PRESSED}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 5px; padding: 3px 6px;
    selection-background-color: {ACCENT_SOFT2}; selection-color: {TEXT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ border: 1px solid {BORDER_STRONG}; selection-background-color: {ACCENT_SOFT};
    selection-color: {ACCENT_PRESSED}; background: {SURFACE}; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 18px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 16px; border: none; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 16px; border: none; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{up}"); width: 8px; height: 8px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{down}"); width: 8px; height: 8px; }}

QCheckBox, QRadioButton {{ spacing: 6px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 14px; padding: 8px 6px 6px 6px;
    background: {SURFACE}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT_PRESSED}; font-weight: 600; }}

QTableView, QTreeView, QListView, QTableWidget, QListWidget, QTreeWidget {{
    background: {SURFACE}; alternate-background-color: {SURFACE_ALT}; border: 1px solid {BORDER};
    gridline-color: {BORDER}; selection-background-color: {ACCENT_SOFT2}; selection-color: {TEXT};
}}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background: {ACCENT_SOFT2}; color: {TEXT};
}}
QHeaderView::section {{ background: {ACCENT_SOFT}; color: {ACCENT_PRESSED}; font-weight: 600; padding: 4px 6px;
    border: none; border-right: 1px solid {BORDER}; border-bottom: 2px solid {ACCENT_SOFT2}; }}
QHeaderView::section:hover {{ background: {ACCENT_SOFT2}; }}
QTableCornerButton::section {{ background: {ACCENT_SOFT}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 4px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:hover {{ background: {ACCENT_SOFT2}; }}
QProgressBar {{ border: 1px solid {BORDER_STRONG}; border-radius: 5px; background: {SURFACE}; text-align: center;
    height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

QLabel#hint {{ color: {MUTED}; }}
QLabel#title {{ color: {TEXT}; font-weight: 600; }}
QLabel#warning {{ color: {BAD}; }}
QLabel#chip {{ border-radius: 7px; padding: 2px 8px; font-weight: 600; background: {NEUTRAL_SOFT}; color: {NEUTRAL}; }}
QLabel#chip[level="ok"] {{ background: {OK_SOFT}; color: {OK}; }}
QLabel#chip[level="warn"] {{ background: {WARN_SOFT}; color: {WARN}; }}
QLabel#chip[level="bad"] {{ background: {BAD_SOFT}; color: {BAD}; }}
QLabel#chip[level="info"] {{ background: {INFO_SOFT}; color: {INFO}; }}
QLabel#chip[level="accent"] {{ background: {ACCENT_SOFT}; color: {ACCENT_PRESSED}; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#card[level="ok"] {{ border-left: 4px solid {OK}; }}
QFrame#card[level="warn"] {{ border-left: 4px solid {WARN}; }}
QFrame#card[level="bad"] {{ border-left: 4px solid {BAD}; }}
QFrame#card[level="info"] {{ border-left: 4px solid {INFO}; }}
QFrame#card[level="accent"] {{ border-left: 4px solid {ACCENT}; }}
QTextBrowser {{ background: {SURFACE}; border: 1px solid {BORDER}; }}
"""


def palette() -> QPalette:
    p = QPalette()
    roles = {
        QPalette.Window: BG, QPalette.WindowText: TEXT, QPalette.Base: SURFACE,
        QPalette.AlternateBase: SURFACE_ALT, QPalette.Text: TEXT, QPalette.Button: SURFACE,
        QPalette.ButtonText: TEXT, QPalette.Highlight: ACCENT, QPalette.HighlightedText: "#FFFFFF",
        QPalette.ToolTipBase: SURFACE, QPalette.ToolTipText: TEXT, QPalette.PlaceholderText: FAINT,
        QPalette.Link: ACCENT, QPalette.BrightText: "#FFFFFF", QPalette.Light: SURFACE,
        QPalette.Midlight: SURFACE_ALT, QPalette.Mid: BORDER_STRONG, QPalette.Dark: FAINT, QPalette.Shadow: MUTED,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(FAINT))
    return p


def configure_plots() -> None:
    import pyqtgraph as pg
    pg.setConfigOptions(antialias=True, background=PLOT["bg"], foreground=PLOT["fg"])


class ActiveDockTracker(QObject):
    """Draws the title of the dock that holds the keyboard focus in the accent colour."""

    def __init__(self, app):
        super().__init__(app)
        self._current = None
        app.focusChanged.connect(self._focus)

    def _focus(self, _old, new):
        dock = None
        w = new
        while w is not None:
            if isinstance(w, QDockWidget):
                dock = w
                break
            w = w.parentWidget()
        if dock is self._current:
            return
        for d, on in ((self._current, False), (dock, True)):
            if d is not None:
                try:
                    d.setProperty("active", on)
                    _repolish(d)
                except RuntimeError:          # dock already deleted
                    pass
        self._current = dock


def apply(app=None) -> None:
    """Apply the theme to ``app`` (idempotent)."""
    app = app or QApplication.instance()
    if app is None:
        return
    configure_plots()
    if app.property("gcws_theme"):
        return
    app.setStyle("Fusion")
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)   # ignore a dark Windows setting
    except (AttributeError, TypeError):
        pass
    app.setPalette(palette())
    app.setStyleSheet(qss())
    app._gcws_dock_tracker = ActiveDockTracker(app)
    app.setProperty("gcws_theme", True)


def ensure_applied() -> None:
    apply(QApplication.instance())

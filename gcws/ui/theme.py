"""The application look: a light, a dark and a neon theme, each with a single accent.

Everything that has a colour takes it from here: the Qt palette and style
sheet, the plots, status colours in tables and the run colours. Widgets mark
muted helper text with ``setObjectName("hint")`` and primary actions with
:func:`set_primary` instead of carrying their own style sheets.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal as QtSignal
from PySide6.QtGui import QBrush, QColor, QPalette, QPen
from PySide6.QtWidgets import QApplication, QDockWidget, QLabel

# -- tokens --------------------------------------------------------------------
# Sets with the same names: LIGHT (default), DARK and NEON. ``set_theme`` loads one into the
# module names below, so ``theme.ACCENT`` etc. always give the colour of the current theme.
# The run colours of all sets correspond by position (same hue family), so a switch keeps
# each chromatogram recognisable.

LIGHT = {
    "ACCENT": "#1F6F8B", "ACCENT_HOVER": "#185A71", "ACCENT_PRESSED": "#124658",
    "ACCENT_SOFT": "#E3F0F4", "ACCENT_SOFT2": "#C9E1EA",
    "ACCENT_TEXT": "#124658",          # text on the soft accent backgrounds
    "ON_ACCENT": "#FFFFFF",            # text on the accent colour
    "BG": "#F3F6F8", "SURFACE": "#FFFFFF", "SURFACE_ALT": "#F8FAFB",
    "BORDER": "#DCE3E8", "BORDER_STRONG": "#BFCBD4",
    "TEXT": "#1E2A32", "MUTED": "#5F6F7B", "FAINT": "#98A6B0",
    "OK": "#1E8E5A", "OK_SOFT": "#E2F3E9", "WARN": "#B7791F", "WARN_SOFT": "#FCF1DA",
    "BAD": "#C0392B", "BAD_SOFT": "#FBE4E0", "INFO": "#2563EB", "INFO_SOFT": "#E4ECFD",
    "NEUTRAL": "#6B7780", "NEUTRAL_SOFT": "#EDF0F3", "ORANGE_SOFT": "#FCE9D6",
    "INK": "#33424D",                  # icon outlines
    "PAPER": "#FFFFFF",                # icon fills
    "CHEVRON": "", "CLOSE_ICON": "",   # icon file suffix ("" or "-dark")
    #: run colours: colour-blind-safe (Okabe-Ito first), all dark enough for thin lines on white
    "RUN_COLORS": ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#8C564B", "#6A3D9A",
                   "#B8860B", "#E7298A", "#1F3A93", "#11A579", "#A6761D", "#7F3C8D", "#3969AC", "#666666"],
    "PLOT": {
        "bg": "#FFFFFF", "fg": "#46555F", "grid_alpha": 0.12, "cursor": "#1F6F8B", "cursor_text": "#35444E",
        "baseline": "#C0392B", "drop": (90, 102, 112, 160), "manual_fill": (230, 126, 34, 70),
        "blank_fill": (140, 150, 160, 70), "label": "#33424D", "label_selected": "#124658", "event": "#7E57C2",
        "off_region": (120, 130, 140, 26), "spectrum": "#1F6F8B", "reference": "#C0392B",
        "apex_region": "#1F6F8B", "bg_region": "#C0392B", "secondary": "#46555F", "band": (31, 111, 139, 45),
        "band_bg": (192, 57, 43, 45), "glow": False,
    },
}

DARK = {
    "ACCENT": "#4DB6D0", "ACCENT_HOVER": "#6CC6DC", "ACCENT_PRESSED": "#3A9DB6",
    "ACCENT_SOFT": "#173541", "ACCENT_SOFT2": "#1F4A59",
    "ACCENT_TEXT": "#A6E1F0", "ON_ACCENT": "#07161B",
    "BG": "#11171B", "SURFACE": "#192126", "SURFACE_ALT": "#1E272D",
    "BORDER": "#2B3740", "BORDER_STRONG": "#465661",
    "TEXT": "#E4EBEF", "MUTED": "#A0B0BA", "FAINT": "#6C7C86",
    "OK": "#4CC38A", "OK_SOFT": "#15352A", "WARN": "#E6AE4A", "WARN_SOFT": "#3A2E15",
    "BAD": "#F2766A", "BAD_SOFT": "#3E201C", "INFO": "#7BA4FF", "INFO_SOFT": "#1B2745",
    "NEUTRAL": "#A7B4BD", "NEUTRAL_SOFT": "#28333A", "ORANGE_SOFT": "#3C2B1A",
    "INK": "#D7E1E7", "PAPER": "#2A353C",
    "CHEVRON": "-dark", "CLOSE_ICON": "-dark",
    #: the same hues, lighter: thin lines stay visible on the dark plots
    "RUN_COLORS": ["#56B4E9", "#FF8A4C", "#2FD1A0", "#E890C6", "#F4C04F", "#9AD9FF", "#C99A86", "#B695F0",
                   "#E0BD55", "#FF6BB0", "#7F9CFF", "#43DDB0", "#D8A860", "#C68ADB", "#78A8FF", "#BDBDBD"],
    "PLOT": {
        "bg": "#151C20", "fg": "#B9C6CE", "grid_alpha": 0.16, "cursor": "#4DB6D0", "cursor_text": "#D6E0E6",
        "baseline": "#FF7A6B", "drop": (170, 182, 190, 160), "manual_fill": (240, 140, 50, 90),
        "blank_fill": (150, 162, 172, 70), "label": "#D3DDE3", "label_selected": "#A6E1F0", "event": "#B79CFF",
        "off_region": (200, 210, 220, 22), "spectrum": "#4DB6D0", "reference": "#FF7A6B",
        "apex_region": "#4DB6D0", "bg_region": "#FF7A6B", "secondary": "#B9C6CE", "band": (77, 182, 208, 55),
        "band_bg": (242, 118, 106, 55), "glow": False,
    },
}

#: black and dark grey with neon cyan as the accent; chromatograms glow in neon colours
NEON = {
    "ACCENT": "#00E5FF", "ACCENT_HOVER": "#5CF0FF", "ACCENT_PRESSED": "#00B8D4",
    "ACCENT_SOFT": "#062A30", "ACCENT_SOFT2": "#0B3F48",
    "ACCENT_TEXT": "#7DF9FF", "ON_ACCENT": "#00161A",
    "BG": "#08080A", "SURFACE": "#111114", "SURFACE_ALT": "#18181D",
    "BORDER": "#26262E", "BORDER_STRONG": "#42424F",
    "TEXT": "#ECECF4", "MUTED": "#A4A4B8", "FAINT": "#666676",
    "OK": "#39FF14", "OK_SOFT": "#0D2A08", "WARN": "#FFE600", "WARN_SOFT": "#2D2904",
    "BAD": "#FF3B6B", "BAD_SOFT": "#361019", "INFO": "#4DA3FF", "INFO_SOFT": "#0C1C35",
    "NEUTRAL": "#B4B4C8", "NEUTRAL_SOFT": "#222229", "ORANGE_SOFT": "#35200A",
    "INK": "#DCDCE8", "PAPER": "#1C1C22",
    "CHEVRON": "-dark", "CLOSE_ICON": "-dark",
    #: neon versions of the same hues, in the same order
    "RUN_COLORS": ["#00E5FF", "#FF7A1A", "#39FF14", "#FF2BD6", "#FFE600", "#6CCBFF", "#FF8A66", "#B967FF",
                   "#FFB300", "#FF4FA3", "#5C7CFF", "#00FFB3", "#D4FF3A", "#E07BFF", "#2FA8FF", "#E6F2FF"],
    "PLOT": {
        "bg": "#050506", "fg": "#AEAEC2", "grid_alpha": 0.14, "cursor": "#FF2BD6", "cursor_text": "#FF7AE6",
        "baseline": "#FF3B6B", "drop": (200, 200, 220, 150), "manual_fill": (255, 122, 26, 90),
        "blank_fill": (160, 160, 180, 70), "label": "#E6E6F0", "label_selected": "#00E5FF", "event": "#B967FF",
        "off_region": (190, 190, 220, 20), "spectrum": "#00E5FF", "reference": "#FF2BD6",
        "apex_region": "#00E5FF", "bg_region": "#FF2BD6", "secondary": "#D4FF3A", "band": (0, 229, 255, 50),
        "band_bg": (255, 43, 214, 50),
        "glow": True,                  # traces get a soft halo of their own colour
    },
}

THEMES = {"light": LIGHT, "dark": DARK, "neon": NEON}
#: menu names (Layout menu)
THEME_LABELS = {"light": "Light Mode", "dark": "Dark Mode", "neon": "Dark Mode - Neon"}

MODE = "light"
RUN_COLORS: list = []
PLOT: dict = {}
LEVELS: dict = {}


def _load(tokens: dict) -> None:
    """Make ``tokens`` the current colours (lists and dicts keep their identity)."""
    g = globals()
    for k, v in tokens.items():
        if k == "PLOT":
            PLOT.clear()
            PLOT.update(v)
        elif k == "RUN_COLORS":
            RUN_COLORS[:] = v
        else:
            g[k] = v
    LEVELS.clear()
    LEVELS.update({"ok": (g["OK"], g["OK_SOFT"]), "warn": (g["WARN"], g["WARN_SOFT"]),
                   "bad": (g["BAD"], g["BAD_SOFT"]), "info": (g["INFO"], g["INFO_SOFT"]),
                   "neutral": (g["NEUTRAL"], g["NEUTRAL_SOFT"]), "accent": (g["ACCENT"], g["ACCENT_SOFT"])})
    from gcws.ui import icons
    icons.set_colors(g["INK"], g["PAPER"], g["ACCENT"])


_load(LIGHT)


def is_dark() -> bool:
    """True for every theme on a dark background (Dark Mode and Dark Mode - Neon)."""
    return MODE != "light"


def paper_color(color) -> QColor:
    """``color`` for a picture on white paper: a run colour of the current theme becomes the
    light theme's colour at the same place (alpha kept); any other colour stays as it is."""
    c = QColor(color)
    names = [n.lower() for n in RUN_COLORS]
    name = c.name().lower()
    if MODE == "light" or name not in names:
        return c
    out = QColor(LIGHT["RUN_COLORS"][names.index(name)])
    out.setAlpha(c.alpha())
    return out


def qcolor(value, alpha: int | None = None) -> QColor:
    c = QColor(*value) if isinstance(value, tuple) else QColor(value)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def glow_pen(color, width: float = 4.0, alpha: int = 50) -> QPen | None:
    """The halo drawn behind a trace (a wide, faint pen of its colour) in a theme whose traces
    glow (Dark Mode - Neon), else None - for pyqtgraph's ``shadowPen``."""
    if not PLOT["glow"]:
        return None
    pen = QPen(qcolor(color, alpha))
    pen.setWidthF(width)
    pen.setCosmetic(True)
    return pen


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
    down, up = _icon_url(f"chevron-down{CHEVRON}.svg"), _icon_url(f"chevron-up{CHEVRON}.svg")
    close = _icon_url(f"close{CLOSE_ICON}.svg")
    return f"""
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER_STRONG}; padding: 4px 6px; }}

QMainWindow::separator {{ background: {BG}; width: 5px; height: 5px; }}
QMainWindow::separator:hover {{ background: {ACCENT_SOFT2}; }}
QWidget#dockTitle {{ background: {ACCENT_SOFT}; border-left: 3px solid {ACCENT_SOFT2}; }}
QWidget#dockTitle[active="true"] {{ background: {ACCENT}; border-left: 3px solid {ACCENT_PRESSED}; }}
QWidget#dockTitle[tabbed="true"] {{ border-left: none; }}
QLabel#dockTitleText {{ color: {ACCENT_TEXT}; font-weight: 600; background: transparent; }}
QWidget#dockTitle[active="true"] QLabel#dockTitleText {{ color: {ON_ACCENT}; }}
QToolButton#dockButton {{ border: 1px solid transparent; border-radius: 4px; padding: 2px; margin: 0;
    background: transparent; }}
QToolButton#dockButton:hover {{ background: {ACCENT_SOFT2}; border-color: {ACCENT_SOFT2}; }}
QToolButton#dockButton[role="close"]:hover {{ background: {BAD_SOFT}; border-color: {BAD_SOFT}; }}
QWidget#dockTitle[active="true"] QToolButton#dockButton:hover {{ background: {ACCENT_HOVER};
    border-color: {ACCENT_SOFT2}; }}
QWidget#dockTitle[active="true"] QToolButton#dockButton[role="close"]:hover {{ background: {BAD}; }}
QDockWidget::title {{
    background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; padding: 5px 8px 5px 10px;
    border-left: 3px solid {ACCENT_SOFT2}; font-weight: 600; text-align: left;
}}
QDockWidget[active="true"]::title {{ background: {ACCENT}; color: {ON_ACCENT}; border-left: 3px solid {ACCENT_PRESSED}; }}
QDockWidget > QWidget {{ background: {SURFACE}; }}

QToolBar {{ background: {SURFACE}; border: none; border-bottom: 1px solid {BORDER}; padding: 3px 4px; spacing: 3px; }}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 6px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 5px; padding: 3px 5px; background: transparent; }}
QToolButton:hover {{ background: {ACCENT_SOFT}; border-color: {ACCENT_SOFT2}; }}
QToolButton:pressed {{ background: {ACCENT_SOFT2}; }}
QToolButton:checked {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; color: {ACCENT_TEXT}; }}
QToolButton#segment {{ border: 1px solid {BORDER_STRONG}; border-radius: 0; padding: 3px 9px; background: {SURFACE}; }}
QToolButton#segment:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: {ON_ACCENT}; font-weight: 600; }}
QToolButton#segment:hover:!checked {{ background: {ACCENT_SOFT}; }}
QToolButton[primary="true"] {{ background: {ACCENT_SOFT}; border: 1px solid {ACCENT_SOFT2}; color: {ACCENT_TEXT};
    font-weight: 600; }}
QToolButton[primary="true"]:hover {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; }}
QToolButton[primary="true"]:checked {{ background: {ACCENT}; border-color: {ACCENT_PRESSED}; color: {ON_ACCENT}; }}

QMenuBar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 4px 9px; background: transparent; }}
QMenuBar::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; border-radius: 4px; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 20px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; }}
QMenu::item:disabled {{ color: {FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}

QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 5px 12px; border: none;
    border-bottom: 2px solid transparent; margin-right: 2px; }}
QTabBar::tab:hover {{ color: {TEXT}; background: {SURFACE_ALT}; }}
QTabBar::tab:selected {{ color: {ACCENT_TEXT}; border-bottom: 2px solid {ACCENT}; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 4px; top: -1px; background: {SURFACE}; }}
QTabBar#runTabs::tab {{ padding: 5px 12px; min-width: 60px; }}
QTabBar::close-button {{ image: url("{close}"); subcontrol-position: right; border-radius: 3px; margin: 2px; }}
QTabBar::close-button:hover {{ background: {BAD_SOFT}; }}

QPushButton {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 5px; padding: 4px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; background: {ACCENT_SOFT}; }}
QPushButton:pressed {{ background: {ACCENT_SOFT2}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; }}
QPushButton:checked {{ background: {ACCENT_SOFT2}; border-color: {ACCENT}; }}
QPushButton[primary="true"] {{ background: {ACCENT}; color: {ON_ACCENT}; border-color: {ACCENT}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:pressed {{ background: {ACCENT_PRESSED}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: 5px; padding: 3px 6px;
    selection-background-color: {ACCENT_SOFT2}; selection-color: {TEXT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit[invalid="true"] {{ border-color: {BAD}; background: {BAD_SOFT}; }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url("{down}"); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ border: 1px solid {BORDER_STRONG}; selection-background-color: {ACCENT_SOFT};
    selection-color: {ACCENT_TEXT}; background: {SURFACE}; }}
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
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT_TEXT}; font-weight: 600; }}

QTableView, QTreeView, QListView, QTableWidget, QListWidget, QTreeWidget {{
    background: {SURFACE}; alternate-background-color: {SURFACE_ALT}; border: 1px solid {BORDER};
    gridline-color: {BORDER}; selection-background-color: {ACCENT_SOFT2}; selection-color: {TEXT};
}}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background: {ACCENT_SOFT2}; color: {TEXT};
}}
QHeaderView::section {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; font-weight: 600; padding: 4px 6px;
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
QLabel#chip {{ border-radius: 7px; padding: 2px 8px; font-weight: 600; background: {NEUTRAL_SOFT}; color: {NEUTRAL};
    border: 1px solid transparent; }}
QLabel#chip[level="ok"] {{ background: {OK_SOFT}; color: {OK}; }}
QLabel#chip[level="warn"] {{ background: {WARN_SOFT}; color: {WARN}; }}
QLabel#chip[level="bad"] {{ background: {BAD_SOFT}; color: {BAD}; }}
QLabel#chip[level="info"] {{ background: {INFO_SOFT}; color: {INFO}; }}
QLabel#chip[level="accent"] {{ background: {ACCENT_SOFT}; color: {ACCENT_TEXT}; }}
QLabel#chip[selected="true"] {{ border: 1px solid {ACCENT}; }}
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
        QPalette.ButtonText: TEXT, QPalette.Highlight: ACCENT, QPalette.HighlightedText: ON_ACCENT,
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
                    bar = getattr(d, "panel_title_bar", None) or d.titleBarWidget()
                    if hasattr(bar, "set_active"):
                        bar.set_active(on)
                except RuntimeError:          # dock already deleted
                    pass
        self._current = dock


def saved_theme() -> str:
    """The theme chosen last (``prefs/theme``); the older on/off dark mode setting still counts."""
    from PySide6.QtCore import QSettings
    s = QSettings()
    name = s.value("prefs/theme", "", type=str)
    if name in THEMES:
        return name
    return "dark" if s.value("prefs/dark_mode", False, type=bool) else "light"


def apply(app=None) -> None:
    """Apply the theme to ``app`` (idempotent); the theme is the saved preference."""
    app = app or QApplication.instance()
    if app is None:
        return
    if app.property("gcws_theme"):
        configure_plots()
        return
    app.setStyle("Fusion")
    set_theme(saved_theme(), app)
    app._gcws_dock_tracker = ActiveDockTracker(app)
    app.setProperty("gcws_theme", True)


def ensure_applied() -> None:
    apply(QApplication.instance())


# -- switching the theme -------------------------------------------------------

class _Notifier(QObject):
    changed = QtSignal(str)                     # theme name


_notifier = None
_plots: list = []                                # [(weakref to plot widget, callback or None)]


def notifier() -> _Notifier:
    global _notifier
    if _notifier is None:
        _notifier = _Notifier()
    return _notifier


def set_theme(name: str, app=None) -> None:
    """Switch to the theme ``name`` (a key of THEMES), live: palette, style sheet, plots, icons."""
    global MODE
    if name not in THEMES:
        name = "light"
    _load(THEMES[name])
    MODE = name
    configure_plots()
    app = app or QApplication.instance()
    if app is not None:
        try:
            app.styleHints().setColorScheme(Qt.ColorScheme.Dark if is_dark() else Qt.ColorScheme.Light)
        except (AttributeError, TypeError):
            pass
        app.setPalette(palette())
        app.setStyleSheet(qss())
    _restyle_plots()
    notifier().changed.emit(name)


def register_plot(widget, on_change=None) -> None:
    """Keep a plot widget in the current colours; ``on_change()`` redraws its items."""
    import weakref
    _plots.append((weakref.ref(widget), on_change))
    restyle_plot(widget)


def restyle_plot(widget) -> None:
    widget.setBackground(PLOT["bg"])
    item = widget.getPlotItem()
    for name in ("left", "bottom", "right", "top"):
        ax = item.getAxis(name) if name in item.axes else None
        if ax is not None:
            ax.setPen(PLOT["fg"])
            ax.setTextPen(PLOT["fg"])
    try:
        item.showGrid(x=item.ctrl.xGridCheck.isChecked(), y=item.ctrl.yGridCheck.isChecked(),
                      alpha=PLOT["grid_alpha"])
    except (AttributeError, RuntimeError):
        pass


def _restyle_plots(callbacks: bool = True) -> None:
    alive = []
    for ref, cb in _plots:
        w = ref()
        if w is None or w.getPlotItem() is None:      # collected, or closed (pyqtgraph drops the item)
            continue
        try:
            restyle_plot(w)
            if cb is not None and callbacks:
                cb()
        except RuntimeError:                      # the C++ widget is gone
            continue
        alive.append((ref, cb))
    _plots[:] = alive


class light_plots:
    """``with theme.light_plots():`` - pictures for reports and exports are always light."""

    def __enter__(self):
        self.mode = MODE
        if self.mode != "light":                  # colours only: the items read them when painted
            _load(LIGHT)
            _restyle_plots(callbacks=False)
        return self

    def __exit__(self, *exc):
        if self.mode != "light":
            _load(THEMES[self.mode])
            _restyle_plots(callbacks=False)
        return False

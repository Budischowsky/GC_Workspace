"""Table model of the active run's peaks with identification and quantification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont

from gcws.core.text import copy_number
from gcws.quant import peak_values as PV
from gcws.quant.peak_values import Row  # noqa: F401 (re-export)
from gcws.ui import theme
from gcws.ui.widgets.sheet_table import COPY_ROLE


def _f(v, fmt):
    if v is None or v == "":
        return ""
    try:
        return format(v, fmt)
    except (TypeError, ValueError):
        return str(v)


@dataclass
class Column:
    key: str
    header: str
    get: Callable[[Row, Any], Any]
    fmt: str = ""
    editable: bool = False
    tip: str = ""
    default: bool = True
    numeric: bool = True

    def text(self, row: Row, ws) -> str:
        v = self.get(row, ws)
        return _f(v, self.fmt) if self.fmt else ("" if v is None else str(v))


def _v(key: str):
    """The column's getter for the active run (``peak_values`` has them for any run)."""
    get = PV.VALUES[key]
    return lambda r, ws: get(r, ws, getattr(ws, "active_id", None), getattr(ws, "signal_key", None))


def _hint(r: Row, ws):
    cache = getattr(ws, "hints", None)
    if cache is None:
        return ""
    v = cache.get(ws.active, ws.signal_key, r.peak)
    return "…" if v is None else v[0]


COLUMNS: list[Column] = [
    Column("num", "#", _v("num"), "d"),
    Column("rt", "RT [min]", _v("rt"), ".3f"),
    Column("ms_rt", "RT MS [min]", _v("ms_rt"), ".3f",
           tip="Assigned component MS time, otherwise delay-corrected peak apex", default=False),
    Column("type", "Type", _v("type"), numeric=False,
           tip="B baseline, V valley, P penetration, H hold; S solvent, T tangent, X exp. skim, "
               "F/R shoulder, N negative, M manual, + area sum"),
    Column("start", "Start", _v("start"), ".3f", default=False),
    Column("end", "End", _v("end"), ".3f", default=False),
    Column("area", "Area", _v("area"), ",.0f"),
    Column("area_pct", "Area %", _v("area_pct"), ".3f"),
    Column("height", "Height", _v("height"), ",.0f"),
    Column("w50", "W½ [s]", _v("w50"), ".2f", default=False),
    Column("sym", "Symmetry", _v("sym"), ".2f", default=False, tip="USP tailing factor"),
    Column("sn", "S/N", _v("sn"), ".0f", default=False),
    Column("name", "Name", _v("name"), editable=True, numeric=False),
    Column("cas", "CAS", _v("cas"), editable=True, numeric=False),
    Column("score", "Score", _v("score"), ".0f",
           tip="Library match score of the chosen hit"),
    Column("status", "ID status", _v("status"), numeric=False),
    Column("library", "Library", _v("library"), numeric=False, default=False),
    Column("ri", "RI", _v("ri"), ".0f", default=False),
    Column("rrt", "RRT", _v("rrt"), ".4f", default=False,
           tip="RT / measured RT of the selected NIAS reference ISTD, on this detector's time axis"),
    Column("istd", "ISTD", _v("istd"), numeric=False),
    Column("blank_area", "Blank area", _v("blank_area"), ",.0f", default=False,
           tip="NIAS quantification: blank area subtracted in the mg/kg calculation"),
    Column("corr_area", "Corr. area", _v("corr_area"), ",.0f",
           tip="NIAS quantification: area after its blank correction"),
    Column("in_blank", "In blank", _v("in_blank"), numeric=False,
           tip="Peak also found in the assigned blank (aligned RT, and similar spectrum with MS data): "
               "blank level = sample area below the ratio limit x blank area"),
    Column("blank_ratio", "Blank ratio", _v("blank_ratio"), ".1f",
           default=False, tip="Sample area / blank area of the matching blank peak"),
    Column("area_minus_blank", "Area − blank", _v("area_minus_blank"), ",.0f", default=False,
           tip="Peak area minus the matching blank peak's area (peak-level blank check)"),
    Column("mg_dm2", "mg/dm²", _v("mg_dm2"), ".4f", default=False,
           tip="NIAS: corrected area × mean ISTD factor. Extraction (foil): substance mass ÷ sample area "
               "(hover a value for its calculation)"),
    Column("ug_dm2", "µg/dm²", _v("ug_dm2"), ".4f", default=False,
           tip="NIAS: mg/dm² × 1000. HS: HS amount divided by sample area; requires a positive area in dm². "
               "Extraction (foil): µg substance ÷ sample area"),
    Column("mg_m2", "mg/m²", _v("mg_m2"), ".4f", default=False,
           tip="HS: µg/dm² ÷ 10 (mg per m² of sample area)"),
    Column("mg_g", "mg/g", _v("mg_g"), ".6f", default=False,
           tip="Extraction (solid): substance mass ÷ sample mass"),
    Column("mg_kg", "mg/kg", _v("mg_kg"), ".4f", default=False,
           tip="NIAS: the mg/kg of the mode. Extraction (solid): mg substance per kg sample"),
    Column("ug_kg", "µg/kg", _v("ug_kg"), ".2f", default=False,
           tip="Extraction (solid): µg substance per kg sample"),
    Column("ug_l", "µg/L", _v("ug_l"), ".2f", default=False,
           tip="NIAS and extraction: substance mass per litre of extract (the extract volume); "
               "hover a value for its calculation"),
    Column("mg_l", "mg/L", _v("mg_l"), ".4f", default=False, tip="NIAS: µg/L ÷ 1000"),
    Column("mg_ml", "mg/mL", _v("mg_ml"), ".6f", default=False,
           tip="NIAS: µg/L ÷ 1 000 000"),
    Column("ug_hs", "µg/HS", _v("ug_hs"), ".4f", default=False,
           tip="HS amount per vial relative to the activated internal standards"),
    Column("ug_g", "µg/g", _v("ug_g"), ".4f", default=False,
           tip="HS and extraction (solid): substance amount divided by sample mass; requires a positive mass in g"),
    Column("conc", "Conc.", _v("conc"), ".4f",
           tip="Concentration in the unit of the quantification mode"),
    Column("sml", "SML", _v("sml"), numeric=False, default=False),
    Column("qstatus", "Status", _v("qstatus"), numeric=False),
    Column("flags", "Flags", _v("flags"), numeric=False,
           tip="new: in none of the learned evaluations, the register of reported substances and CASINFO.xlsx; "
               "element symbols: the formula has elements other than C, H, O, N. Hover a cell for details"),
    Column("learned", "Learned rule", _v("learned"), numeric=False, default=False,
           tip="The learned report rule that changed this peak's report row, with its evidence"),
    Column("origin", "Integration", _v("origin"), numeric=False, default=False),
    Column("class_hint", "Class hint", _hint, numeric=False, default=False,
           tip="Substance-class clue from the MS interpreter (spectrum of the peak); hover for details"),
]
COLUMN_KEYS = [c.key for c in COLUMNS]
#: concentration columns whose cells show their calculation as the tooltip
CALC_KEYS = ("conc", "mg_dm2", "ug_dm2", "ug_l", "mg_l", "mg_ml", "mg_g", "mg_kg", "ug_g", "ug_kg")


class PeakTableModel(QAbstractTableModel):
    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self.rows: list[Row] = []
        self.orphans = []
        self.conc_header = "Conc."
        self.on_edit: Optional[Callable[[Row, str, str], None]] = None

    def reload(self):
        self.beginResetModel()
        self.rows = []
        st = self.ws.active
        res = self.ws.active_result() if st else None
        if st is not None and res is not None:
            idents, self.orphans = st.ident_set(self.ws.signal_key).bind(res.peaks)
            quant = self.ws.quant_rows(st.id) if hasattr(self.ws, "quant_rows") else {}
            for i, p in enumerate(res.peaks):
                self.rows.append(Row(i, p, idents.get(i), quant.get(i, {})))
        unit = getattr(self.ws, "quant_unit", lambda: "")()
        self.conc_header = f"Conc. [{unit}]" if unit else "Conc."
        self.endResetModel()

    def refresh_column(self, key: str) -> None:
        if not self.rows or key not in COLUMN_KEYS:
            return
        c = COLUMN_KEYS.index(key)
        self.dataChanged.emit(self.index(0, c), self.index(len(self.rows) - 1, c))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            col = COLUMNS[section]
            if role == Qt.DisplayRole:
                return self.conc_header if col.key == "conc" else col.header
            if role == Qt.ToolTipRole:
                return col.tip or None
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = COLUMNS[index.column()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            return col.text(row, self.ws)
        if role == COPY_ROLE:            # Ctrl+C: a number with every digit (it is shown rounded)
            return copy_number(col.get(row, self.ws)) if col.numeric and col.fmt else col.text(row, self.ws)
        if role == Qt.UserRole:          # sort key
            v = col.get(row, self.ws)
            return v if v is not None else (-1e300 if col.numeric else "")
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignRight | Qt.AlignVCenter) if col.numeric else int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.BackgroundRole:
            if row.ident is not None and row.ident.istd:
                return theme.status_brush("ok")
            if col.key in ("name", "cas", "status") and row.ident is not None:
                if row.ident.manual:
                    return theme.status_brush("warn")
                st = (row.ident.status or "").lower()
                if st.startswith("uncertain"):
                    return theme.status_brush("bad")
                if st.startswith("unknown"):
                    return theme.status_brush("neutral")
            if col.key == "in_blank":
                m = PV.blank_match(row, self.ws, getattr(self.ws, "active_id", None),
                                   getattr(self.ws, "signal_key", None))
                if m is not None:
                    return theme.status_brush(m.level)
            if "M" in row.peak.flags and col.key in ("num", "type", "area"):
                return QBrush(QColor(theme.ORANGE_SOFT))
            if "S" in row.peak.flags:
                return QBrush(QColor(theme.SURFACE_ALT))
        if role == Qt.ForegroundRole and row.peak.negative:
            return QBrush(QColor(theme.INFO))
        if role == Qt.FontRole and index.row() == self.ws.selected:
            f = QFont()
            f.setBold(True)
            return f
        if role == Qt.ToolTipRole and col.key in ("area", "area_pct", "type", "origin"):
            return row.peak.extra.get("area_note")
        if role == Qt.ToolTipRole and col.key == "rrt":
            return row.quant.get("rrt_status", col.tip)
        if role == Qt.ToolTipRole and col.key in CALC_KEYS:
            return (row.quant.get("calc") or {}).get(col.key)
        if col.key == "flags" and role in (Qt.ToolTipRole, Qt.BackgroundRole):
            text, tip = PV.substance_flags(row)
            if not text:
                return None
            return tip if role == Qt.ToolTipRole else theme.status_brush("warn")
        if role == Qt.ToolTipRole and col.key == "class_hint":
            cache = getattr(self.ws, "hints", None)
            v = cache.get(self.ws.active, self.ws.signal_key, row.peak) if cache is not None else None
            return v[1] if v else None
        if role == Qt.ToolTipRole and col.key == "name" and row.ident is not None and row.ident.hits:
            lines = [f"{h.get('name', '')}  ({h.get('cas', '') or '-'})  {h.get('score', '')}"
                     for h in row.ident.hits[:6]]
            return "Library hits:\n" + "\n".join(lines)
        return None

    def flags(self, index):
        f = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if COLUMNS[index.column()].editable:
            f |= Qt.ItemIsEditable
        return f

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        col = COLUMNS[index.column()]
        if not col.editable or self.on_edit is None:
            return False
        self.on_edit(self.rows[index.row()], col.key, str(value).strip())
        return True

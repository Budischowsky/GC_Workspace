"""The peak table's filters as plain predicates, usable for any run (e.g. "search only shown peaks")."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from gcws.core.keys import is_fid
from gcws.ui.models.peak_table import COLUMN_KEYS, COLUMNS, Row

#: columns whose value is computed for the *active* run only; the text filter skips them elsewhere
ACTIVE_ONLY = {"ms_rt", "in_blank", "blank_ratio", "area_minus_blank", "class_hint"}


def _decimals(fmt: str) -> int:
    """Decimals a column shows (``".4f"`` -> 4), for "=" comparisons at display precision."""
    import re
    m = re.search(r"\.(\d+)f", fmt or "")
    return int(m.group(1)) if m else 6


def value_test(op: str, a: float, b: float | None = None, decimals: int = 6):
    """``f(v) -> bool`` for one operator; "=" compares at the shown precision."""
    tol = 0.5 * 10 ** -decimals
    lo, hi = (min(a, b), max(a, b)) if b is not None else (a, a)
    return {
        "<": lambda v: v < a,
        "≤": lambda v: v <= a + tol,
        "=": lambda v: abs(v - a) <= tol + 1e-12 * abs(a),
        "≥": lambda v: v >= a - tol,
        ">": lambda v: v > a,
        "between": lambda v: lo - tol <= v <= hi + tol,
        "outside": lambda v: v < lo or v > hi,
    }[op]


@dataclass
class FilterState:
    """What the peak table hides: value condition, free text and blank-level peaks."""
    column: str = ""
    op: str = ""
    a: Optional[float] = None
    b: Optional[float] = None
    text: str = ""
    hide_blank: bool = False

    @property
    def value_active(self) -> bool:
        two = self.op in ("between", "outside")
        return bool(self.column) and self.a is not None and (self.b is not None or not two)

    @property
    def active(self) -> bool:
        return self.value_active or bool(self.text.strip()) or self.hide_blank


def value_predicate(state: FilterState, ws) -> Optional[Callable[[Row], bool]]:
    """``keep(row)`` of the value condition, or None when no condition is set."""
    if not state.value_active or state.column not in COLUMN_KEYS:
        return None
    col = COLUMNS[COLUMN_KEYS.index(state.column)]
    two = state.op in ("between", "outside")
    test = value_test(state.op, state.a, state.b if two else None, _decimals(col.fmt))

    def keep(row: Row) -> bool:
        v = col.get(row, ws)
        if v is None or v == "":
            return False                          # no value: cannot satisfy the condition
        try:
            return test(float(v))
        except (TypeError, ValueError):
            return False
    return keep


def rows_for(ws, run_id: str, key: str) -> list[Row]:
    """The table rows of one run and signal, built like ``PeakTableModel.reload``."""
    st = ws.runs.get(run_id)
    res = ws.result(run_id, key) if st is not None else None
    if res is None:
        return []
    idents, _ = st.ident_set(key).bind(res.peaks)
    quant = ws.quant_rows(run_id, key) if hasattr(ws, "quant_rows") else {}
    return [Row(i, p, idents.get(i), quant.get(i, {})) for i, p in enumerate(res.peaks)]


def visible_indices(ws, run_id: str, key: str, state: FilterState) -> set[int]:
    """Peak indices of ``run_id`` / ``key`` the peak table would show with ``state``."""
    rows = rows_for(ws, run_id, key)
    keep = value_predicate(state, ws)
    text = state.text.strip().lower()
    blank = ws.blank_matches(run_id) if state.hide_blank and hasattr(ws, "blank_matches") else {}
    active = run_id == getattr(ws, "active_id", None)
    cols = [c for c in COLUMNS if active or c.key not in ACTIVE_ONLY]
    out = set()
    for r in rows:
        if keep is not None and not keep(r):
            continue
        m = blank.get(r.index) if blank else None
        if m is not None and m.status == "blank":
            continue
        if text and not any(text in c.text(r, ws).lower() for c in cols):
            continue
        out.add(r.index)
    return out


def map_indices(ws, run_id: str, from_key: str, to_key: str, indices: set[int], tol: float = 0.03) -> set[int]:
    """The peaks of ``to_key`` at the same time as ``indices`` of ``from_key`` (FID <-> TIC via the delay)."""
    if from_key == to_key:
        return set(indices)
    st = ws.runs.get(run_id)
    src, dst = ws.result(run_id, from_key), ws.result(run_id, to_key)
    if st is None or src is None or dst is None or not dst.peaks:
        return set()
    shift = 0.0
    if is_fid(from_key) != is_fid(to_key):
        shift = -st.delay_value if is_fid(from_key) else st.delay_value
    apexes = [p.apex_rt for p in dst.peaks]
    out = set()
    for i in indices:
        if not 0 <= i < len(src.peaks):
            continue
        t = src.peaks[i].apex_rt + shift
        j = min(range(len(apexes)), key=lambda k: abs(apexes[k] - t))
        if abs(apexes[j] - t) <= tol:
            out.add(j)
    return out

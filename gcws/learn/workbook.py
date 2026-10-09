"""Parse one human NIAS-Screening evaluation workbook into a HumanEvaluation."""
from __future__ import annotations

import re
from typing import Optional

from gcws.learn.model import RawPeak


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def _norm_col(v) -> str:
    return re.sub(r"\s+", " ", _text(v)).upper()


def _section_signal(title: str) -> str:
    inner = title.strip()[1:-1].strip()
    if inner.upper().startswith("INT TIC"):
        return "TIC"
    if "FID" in inner.upper():
        return "FID"
    return inner


def parse_rohdaten(rows: list[tuple]) -> list[RawPeak]:
    """The integration tables of the 'Rohdaten' sheet (INI-like '[INT …]' sections, a 'Header=' row and
    'N=' peak rows). Columns are located from each section's own header, so TIC (scan numbers) and FID
    (start/end in minutes) layouts both work and empty cells do not shift values."""
    peaks: list[RawPeak] = []
    signal = ""
    cols: dict[str, int] = {}
    for row in rows:
        cells = list(row)
        first_i = next((i for i, v in enumerate(cells) if v not in (None, "")), None)
        if first_i is None:
            continue
        first = _text(cells[first_i])
        if first.startswith("[") and first.endswith("]"):
            signal = "" if first.lower() == "[contents]" else _section_signal(first)
            cols = {}
            continue
        if not signal:
            continue
        if first == "Header=":
            cols = {_norm_col(v): i for i, v in enumerate(cells) if i > first_i and v not in (None, "")}
            continue
        if not cols or not re.fullmatch(r"\d+=", first):
            continue

        def get(*names):
            for n in names:
                i = cols.get(n)
                if i is not None and i < len(cells):
                    return cells[i]
            return None

        rt, area = _num(get("R.T.", "RT")), _num(get("AREA"))
        if rt is None or area is None:
            continue
        ptype = _text(get("PK TY", "PK  TY", "TYPE"))
        peaks.append(RawPeak(
            signal=signal, number=int(_num(get("PEAK")) or first[:-1]), rt=rt, area=area,
            height=_num(get("HEIGHT")),
            start=_num(get("START")) if "START" in cols else None,
            end=_num(get("END")) if "END" in cols else None,
            peak_type=ptype, manual="M" in ptype))
    return peaks

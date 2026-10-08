"""Text that every output can take.

Some library files carry control characters inside their names (a Shimadzu ``.nam`` entry has a
NUL byte before "Tridecan-1-ol"). Excel refuses them (openpyxl: "... cannot be used in
worksheets"), and in the tables they are invisible. They are removed where names enter the
workspace and again before a workbook is written.
"""
from __future__ import annotations

import math
import numbers
import re
from decimal import Decimal

#: the characters an Excel cell cannot hold (openpyxl's ILLEGAL_CHARACTERS_RE)
ILLEGAL = re.compile(r"[\000-\010\013\014\016-\037]")


def copy_number(value) -> str:
    """``value`` as Ctrl+C puts it on the clipboard: a number with every digit Excel keeps (15 significant),
    without exponent or thousands separator and with a point as decimal mark; anything else as its text."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return str(value)
    if isinstance(value, numbers.Integral):
        return str(int(value))
    v = float(value)
    if not math.isfinite(v):
        return ""
    if v == 0:
        return "0"
    return format(Decimal(format(v, ".15g")), "f")


def excel_safe(value):
    """``value`` without the characters Excel refuses; other types unchanged."""
    if isinstance(value, str) and ILLEGAL.search(value):
        return ILLEGAL.sub("", value)
    return value


def clean_name(value):
    """A name or CAS number without control characters and outer blanks."""
    if not isinstance(value, str):
        return value
    return ILLEGAL.sub("", value).strip() if ILLEGAL.search(value) else value


def clean_rows(rows) -> None:
    """Clean every string value of a list of dicts in place."""
    for r in rows or []:
        if isinstance(r, dict):
            for k, v in r.items():
                if isinstance(v, str) and ILLEGAL.search(v):
                    r[k] = clean_name(v)

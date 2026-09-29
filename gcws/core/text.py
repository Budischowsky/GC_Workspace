"""Text that every output can take.

Some library files carry control characters inside their names (a Shimadzu ``.nam`` entry has a
NUL byte before "Tridecan-1-ol"). Excel refuses them (openpyxl: "... cannot be used in
worksheets"), and in the tables they are invisible. They are removed where names enter the
workspace and again before a workbook is written.
"""
from __future__ import annotations

import re

#: the characters an Excel cell cannot hold (openpyxl's ILLEGAL_CHARACTERS_RE)
ILLEGAL = re.compile(r"[\000-\010\013\014\016-\037]")


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

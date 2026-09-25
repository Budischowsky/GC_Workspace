"""Substance sighting history: "Schon berichtet" (spec v3.1 SS VII.5).

``gc_register`` keeps a register of *unknowns* -- m/z fingerprints that nobody
has named yet. Identified substances are never recorded there, so the question
an analyst actually asks in front of a Doppelbestimmung sheet -- "have we
reported this before, and how often?" -- has had no answer.

This module adds two tables to the **existing** register database rather than
opening a second file. Two reasons: the register is already the one file a team
shares on a network drive and already carries the "a report was just generated"
event, and a second database would have to be found, backed up and migrated
separately. The tables are created with ``CREATE TABLE IF NOT EXISTS``, so an
old register upgrades simply by being opened -- there is no migration step and
no schema version to bump.

Deliberately **headless and pure SQLite**, like ``gc_qc`` and ``gc_duplicate``:
no Tk, no openpyxl, no numpy, and no module-level connection. Every function
takes the connection it works on, so the whole module is testable against a
``tempfile`` database and a broken share can never take the workspace down with
it -- :func:`counts` answers ``{}`` instead of raising.

Counting rule (SS VII.0, decision 4): **distinct earlier reports**, keyed on CAS
*and* on a normalised name. A substance with a usable CAS is counted over its
CAS alone, so two spellings of one name merge; a substance without one is
counted over its name key alone, so ``unknown (m/z 73)`` never merges with a
named compound that happens to sit at the same retention time.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "DB_FILENAME",
    "FALLBACK_DIR",
    "SCHEMA",
    "counts",
    "keys",
    "open_db",
    "record",
]

#: Same filename ``gc_register.DB_FILENAME`` and the main script's
#: ``UNKNOWN_REGISTER_DB_FILENAME`` use -- this module writes into that very
#: database, it does not create one of its own.
DB_FILENAME = "unknown_register.sqlite"

#: Where the register lands when the main script cannot be imported (a headless
#: test, a stripped install). Never the working directory: a per-user directory
#: keeps a stray run from scattering databases through the analyst's data.
FALLBACK_DIR = ".nias_gc"

#: The two tables of SS VII.5, verbatim. ``IF NOT EXISTS`` throughout is the
#: whole upgrade path: opening an old register creates what is missing and
#: leaves ``entries``/``sightings`` untouched.
SCHEMA = """
CREATE TABLE IF NOT EXISTS reported_substances (
    substance_id INTEGER PRIMARY KEY,
    cas_key      TEXT NOT NULL DEFAULT '',
    name_key     TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    first_seen   TEXT, last_seen TEXT,
    UNIQUE (cas_key, name_key)
);
CREATE TABLE IF NOT EXISTS report_substance_events (
    event_id     INTEGER PRIMARY KEY,
    substance_id INTEGER NOT NULL REFERENCES reported_substances(substance_id)
                 ON DELETE CASCADE,
    report_id    TEXT NOT NULL,
    report_type  TEXT NOT NULL,
    sample_key   TEXT NOT NULL DEFAULT '',
    reported_at  TEXT NOT NULL,
    UNIQUE (substance_id, report_id)
);
CREATE INDEX IF NOT EXISTS ix_rs_cas  ON reported_substances(cas_key);
CREATE INDEX IF NOT EXISTS ix_rs_name ON reported_substances(name_key);
CREATE INDEX IF NOT EXISTS ix_rse_sub ON report_substance_events(substance_id);
"""

#: Report types the workspace writes. Free text in the table on purpose -- a new
#: report kind must not need a schema change -- but named here so callers and
#: tests agree on the spelling.
REPORT_NIAS = "NIAS"
REPORT_FINGERPRINT = "Fingerprint"
REPORT_TOTAL_EXTRACTION = "Total extraction"
REPORT_DIN_SPEC = "DIN SPEC"

#: Syntax-only CAS check, the lax NIAS rule (``is_valid_cas_number`` in the main
#: script), not the DIN SPEC mod-10 rule of ``gc_model.valid_cas``. SS VII.5
#: says "syntactically valid", and deliberately so: a substance whose CAS the
#: library got one digit wrong should still be counted under that CAS rather
#: than silently falling back to its name.
_CAS_RE = re.compile(r"0*(\d{1,7})-0*(\d{2})-(\d)")

#: Cell values that mean "no CAS". ``"0"`` is ``gc_model.clean_cas``'s sentinel;
#: the others are what analysts and libraries type instead.
_NO_CAS = {"", "0", "0-0-0", "0-00-0", "n/a", "na", "n.a.", "none", "-", "--",
           "keine", "unbekannt", "unknown", "null"}

_MAIN_SCRIPT = "NIAS Reporting v27.py"

#: Cached result of :func:`_main_script`, including the failure. Importing the
#: main script means executing 8000 lines; retrying that on every rebuild of the
#: duplicate sheet would be felt.
_MAIN_CACHE: list[Any] = []


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def keys(cas: Any, name: Any) -> tuple[str, str]:
    """``(cas_key, name_key)`` for one substance.

    ``cas_key`` is ``''`` for ``'0'``, ``''``, ``'n/a'`` and anything that is
    not a syntactically valid CAS number; a valid one is normalised the way
    the main script's ``normalize_cas`` does it, so ``0000108-88-3`` and
    ``108-88-3`` are one substance. ``name_key`` is the name casefolded with
    runs of whitespace collapsed, which is what makes ``Bis(2-ethylhexyl)
    phthalate`` and ``Bis(2-ethylhexyl)  phthalate`` meet.

    Pure and total: any input, including ``None`` and a datetime Excel produced
    from a CAS cell, gives a tuple of two strings.
    """
    return (_cas_key(cas), _name_key(name))


def _cas_key(value: Any) -> str:
    if value is None:
        return ""
    # Excel turns e.g. 4860-03-1 into a date; keep the digits it kept.
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        text = f"{value.year}-{value.month:02d}-{value.day}"
    else:
        text = str(value)
    text = text.strip().replace("/", "-")
    # A cell listing alternatives is keyed on the first of them, as the main
    # script's matching does.
    text = text.split(";")[0].split(",")[0].strip()
    if text.casefold() in _NO_CAS:
        return ""
    m = _CAS_RE.fullmatch(text)
    if not m:
        return ""
    return f"{int(m.group(1))}-{m.group(2)}-{m.group(3)}"


def _name_key(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _display_name(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _pair(row: Any) -> tuple[str, str]:
    """``keys()`` of one report row, mapping or object.

    The contract types ``record``'s rows as mappings, which is what the report
    writers hand over. ``gc_duplicate.DuplicateRow`` exposes ``cas``/``name`` as
    properties instead, and a caller that already has those rows should not have
    to build dicts from them, so both shapes are read.
    """
    if isinstance(row, Mapping):
        return keys(row.get("cas"), row.get("name"))
    return keys(getattr(row, "cas", None), getattr(row, "name", None))


def _display_of(row: Any) -> str:
    if isinstance(row, Mapping):
        return _display_name(row.get("name"))
    return _display_name(getattr(row, "name", None))


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def _main_script():
    """The main NIAS script as a module, or ``None``.

    It is where ``unknown_register_dir()`` lives, and its filename contains
    spaces, so it cannot be imported by name -- the same ``spec_from_file_location``
    dance ``gc_export.load_main_script`` does. Already-loaded copies are reused
    under both the name ``gc_export`` registers (``nias_main``) and the name the
    launcher runs under, so a running application never pays for a second
    execution and never gets a second set of module-level state.
    """
    if _MAIN_CACHE:
        return _MAIN_CACHE[0]
    module = None
    for name in ("nias_main", "__main__"):
        candidate = sys.modules.get(name)
        if candidate is not None and hasattr(candidate, "unknown_register_dir"):
            module = candidate
            break
    if module is None:
        try:
            import importlib.util

            path = Path(__file__).resolve().parent / _MAIN_SCRIPT
            if path.is_file():
                spec = importlib.util.spec_from_file_location("nias_main", path)
                if spec is not None and spec.loader is not None:
                    module = importlib.util.module_from_spec(spec)
                    sys.modules.setdefault("nias_main", module)
                    spec.loader.exec_module(module)
        except Exception:
            # A missing or unimportable main script is not an error here: the
            # fallback directory keeps this module usable on its own.
            module = None
    _MAIN_CACHE.append(module)
    return module


def default_path() -> Path:
    """Where the register lives, resolved the way the main script resolves it."""
    module = _main_script()
    if module is not None:
        for attr in ("unknown_register_db_path", "unknown_register_dir"):
            fn = getattr(module, attr, None)
            if fn is None:
                continue
            try:
                value = Path(fn())
            except Exception:
                continue
            return value if attr == "unknown_register_db_path" else value / DB_FILENAME
    from nias_paths import DATA
    return DATA / DB_FILENAME


def open_db(path: Optional[Any] = None) -> sqlite3.Connection:
    """Open the register and make sure the two SS VII.5 tables exist.

    The connection settings are ``gc_register.connect``'s, and for its reasons:
    the register is meant to sit on an SMB share, where WAL's ``-shm`` file is
    unsafe, so ``journal_mode=DELETE`` with a generous ``busy_timeout`` instead.
    ``isolation_level=None`` leaves transaction control to the caller and to
    :func:`record`, which needs one transaction for the whole report.

    Raises whatever ``sqlite3`` raises -- the caller decides whether a register
    it cannot open is worth a status line (it is) or an aborted export (it is
    not). :func:`counts` swallows it so the common read path needs no ``try``.
    """
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(target), timeout=15.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("PRAGMA synchronous  = FULL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def counts(con: Any,
           pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """Distinct earlier reports per substance, one query for the whole sheet.

    ``pairs`` are :func:`keys` tuples; the returned mapping is keyed the same
    way, so ``gc_duplicate.build`` can look a row up without touching SQL. A
    substance with a CAS is counted over its CAS alone -- any spelling of the
    name -- and one without over its name key, which is what keeps
    ``unknown (m/z 73)`` apart from a named compound.

    Two queries, not one per row: with 49 rows on the sheet and a register on a
    share, a per-row round trip is the difference between instant and visible.

    **Never raises.** A missing, locked or unreadable register returns ``{}``
    and the column stays empty -- a statistic must not cost an analyst their
    sheet. That is why the ``con`` is typed loosely: ``None`` is a legitimate
    argument and means "no register".
    """
    wanted = [p for p in pairs or ()]
    if con is None or not wanted:
        return {}
    cas_keys = sorted({c for c, _n in wanted if c})
    name_keys = sorted({n for c, n in wanted if not c and n})
    by_cas: dict[str, int] = {}
    by_name: dict[str, int] = {}
    try:
        by_cas = _count_over(con, "cas_key", cas_keys)
        by_name = _count_over(con, "name_key", name_keys)
    except Exception:
        return {}
    out: dict[tuple[str, str], int] = {}
    for pair in wanted:
        cas, name = pair
        if cas:
            out[pair] = by_cas.get(cas, 0)
        elif name:
            out[pair] = by_name.get(name, 0)
        else:
            # No CAS and no name is not a substance; counting it would merge
            # every nameless row into one bogus history.
            out[pair] = 0
    return out


def _count_over(con: Any, column: str, values: list[str]) -> dict[str, int]:
    """``{key: distinct report_id count}`` for one keying column.

    Chunked at 400 placeholders because SQLite's default
    ``SQLITE_MAX_VARIABLE_NUMBER`` is 999 on older builds; a Fingerprint sheet
    can carry more rows than that.
    """
    out: dict[str, int] = {}
    if not values:
        return out
    for start in range(0, len(values), 400):
        chunk = values[start:start + 400]
        marks = ",".join("?" * len(chunk))
        sql = (f"SELECT s.{column} AS k, "
               "       COUNT(DISTINCT e.report_id) AS n "
               "  FROM reported_substances s "
               "  JOIN report_substance_events e "
               "    ON e.substance_id = s.substance_id "
               f" WHERE s.{column} IN ({marks}) "
               f" GROUP BY s.{column}")
        for row in con.execute(sql, chunk):
            out[row[0]] = int(row[1])
    return out


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def record(con: Any, rows: Iterable[Any], *, report_id: str,
           report_type: str, sample_key: str = "",
           reported_at: Optional[str] = None) -> int:
    """Register one generated report. Returns the number of rows recorded.

    ``rows`` is exactly the list the report writer wrote into the substance
    table (SS VII.10 step 6) -- not the rows below the reporting limit, not the
    artefacts, not what the analyst removed. Counting what was not published
    would make the column lie.

    **Idempotent per ``report_id``.** The output path is the identity of a
    report, so writing the same file twice leaves every count where it was.
    That is the ``UNIQUE (substance_id, report_id)`` constraint plus
    ``INSERT OR IGNORE``; the return value is the number of *new* events, which
    is 0 on a rewrite.

    One transaction for the whole report: a half-recorded report would show a
    substance as reported once more than the report next to it.
    """
    if con is None or not report_id:
        return 0
    stamp = reported_at or datetime.now().replace(microsecond=0).isoformat(" ")
    report_id = str(report_id)

    # Deduplicate inside the report first: one substance appearing on two lines
    # of one sheet is still one sighting, and the UNIQUE constraint would only
    # hide that rather than make the count right.
    seen: dict[tuple[str, str], str] = {}
    for row in rows or ():
        pair = _pair(row)
        if not pair[0] and not pair[1]:
            continue
        seen.setdefault(pair, _display_of(row))

    if not seen:
        return 0

    written = 0
    con.execute("BEGIN IMMEDIATE")
    try:
        for (cas_key, name_key), display in seen.items():
            con.execute(
                "INSERT INTO reported_substances "
                "       (cas_key, name_key, display_name, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (cas_key, name_key) DO UPDATE SET "
                # A later spelling is the better one to show: it is what the
                # current library calls the substance. ``first_seen`` never moves.
                "  display_name = CASE WHEN excluded.display_name <> '' "
                "                      THEN excluded.display_name "
                "                      ELSE reported_substances.display_name END, "
                "  last_seen = excluded.last_seen",
                (cas_key, name_key, display, stamp, stamp))
            sid = con.execute(
                "SELECT substance_id FROM reported_substances "
                " WHERE cas_key = ? AND name_key = ?",
                (cas_key, name_key)).fetchone()[0]
            cur = con.execute(
                "INSERT OR IGNORE INTO report_substance_events "
                "       (substance_id, report_id, report_type, sample_key, "
                "        reported_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(sid), report_id, str(report_type or ""),
                 str(sample_key or ""), stamp))
            written += int(cur.rowcount or 0)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return written

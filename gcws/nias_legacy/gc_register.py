#!/usr/bin/env python3
"""SQLite backend for the unknown register.

Replaces the single ``unknown_register.json`` that is rewritten in full on every
change. That design breaks down once measured spectra are attached: the write
cost grows with the *history* rather than with the change, and the lock's 60
second stale-break becomes a data-loss window on a slow share.

Design constraints
------------------
* **No WAL.** The ``-shm`` file is unsafe on SMB shares. ``journal_mode=DELETE``
  plus ``BEGIN IMMEDIATE`` and a generous ``busy_timeout`` instead.
* **Readers never block and never see a torn state.** The JSON design gets this
  from ``os.replace``; migration keeps it by building into a temp file in the
  same directory and publishing atomically.
* **Spectra as BLOBs, significant ions as rows.** A spectrum is 50-500 ions;
  stored per-ion it would reach millions of rows. The arrays go in as two
  parallel BLOBs, and only the significant ions are additionally indexed so
  similarity prefiltering and the ``bp:``/``+mz``/``-mz`` query terms stay fast.
* **Every id stays stable.** ``entry_id`` is the integer inside ``UNK-nnnn``, so
  the existing ``next_id`` counter keeps working after migration.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 4

#: An ion counts as significant at or above this fraction of the base peak.
#: Full spectra would otherwise wreck the set-based scoring: every real spectrum
#: contains a delta-14 ladder, so the homologue detectors would fire on
#: everything, and Jaccard overlap would collapse toward zero for every pair.
SIGNIFICANT_ION_FRACTION = 0.01
SIGNIFICANT_ION_LIMIT = 20

#: How many of the strongest ions define an entry's identity.
#: The legacy register keys an unknown on the four masses in its name, so the
#: same count is used here. Keying on the full significant set instead would
#: fragment the register: two measurements of one substance almost never share
#: all twenty ions, so nothing would ever merge or cluster.
ENTRY_KEY_IONS = 4

DB_FILENAME = "unknown_register.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS register_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    entry_id         INTEGER PRIMARY KEY,
    unknown_id       TEXT    NOT NULL UNIQUE,
    mz_key           TEXT    NOT NULL UNIQUE,
    -- The strongest four masses (§10). Deliberately NOT unique: two entries
    -- answering to one identity key is a fact the register has to show, not
    -- something a constraint may silently reject or resolve.
    identity_key     TEXT    NOT NULL DEFAULT '',
    label            TEXT    NOT NULL DEFAULT '',
    canonical_mz     TEXT    NOT NULL DEFAULT '',
    ranked_mz        TEXT    NOT NULL DEFAULT '',
    base_peak        INTEGER,
    rank_unknown     INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'offen',
    assigned_name    TEXT    NOT NULL DEFAULT '',
    assigned_cas     TEXT    NOT NULL DEFAULT '',
    note             TEXT    NOT NULL DEFAULT '',
    linked_to        TEXT,
    cluster          TEXT    NOT NULL DEFAULT '',
    n_sightings      INTEGER NOT NULL DEFAULT 0,
    n_samples        INTEGER NOT NULL DEFAULT 0,
    rt_mean REAL, rt_min REAL, rt_max REAL, rt_sd REAL,
    conc_min REAL, conc_median REAL, conc_max REAL,
    conc_max_sample  TEXT    NOT NULL DEFAULT '',
    area_pct_max     REAL,
    ttc_flag         TEXT    NOT NULL DEFAULT '',
    homologue_series TEXT    NOT NULL DEFAULT '',
    first_seen       TEXT    NOT NULL DEFAULT '',
    last_seen        TEXT    NOT NULL DEFAULT '',
    first_seen_iso   TEXT,
    last_seen_iso    TEXT,
    search_blob      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_entries_identity  ON entries(identity_key);
CREATE INDEX IF NOT EXISTS ix_entries_status    ON entries(status);
CREATE INDEX IF NOT EXISTS ix_entries_base_peak ON entries(base_peak);
CREATE INDEX IF NOT EXISTS ix_entries_rt_mean   ON entries(rt_mean);
CREATE INDEX IF NOT EXISTS ix_entries_conc_max  ON entries(conc_max);
CREATE INDEX IF NOT EXISTS ix_entries_cluster   ON entries(cluster);
CREATE INDEX IF NOT EXISTS ix_entries_n         ON entries(n_sightings DESC);

CREATE TABLE IF NOT EXISTS entry_ions (
    entry_id INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    mz       INTEGER NOT NULL,
    rank     INTEGER,
    PRIMARY KEY (entry_id, mz)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_entry_ions_mz ON entry_ions(mz);

CREATE TABLE IF NOT EXISTS entry_class_hints (
    entry_id INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    hint TEXT NOT NULL, ord INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (entry_id, hint)
);
CREATE TABLE IF NOT EXISTS entry_samples (
    entry_id INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    sample TEXT NOT NULL, PRIMARY KEY (entry_id, sample)
);
CREATE INDEX IF NOT EXISTS ix_entry_samples_sample ON entry_samples(sample);
CREATE TABLE IF NOT EXISTS entry_simulants (
    entry_id INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    simulant TEXT NOT NULL, PRIMARY KEY (entry_id, simulant)
);
CREATE TABLE IF NOT EXISTS entry_materials (
    entry_id INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    material TEXT NOT NULL, PRIMARY KEY (entry_id, material)
);
CREATE TABLE IF NOT EXISTS entry_mz_variants (
    entry_id INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    variant TEXT NOT NULL, votes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (entry_id, variant)
);

CREATE TABLE IF NOT EXISTS sightings (
    sighting_id     INTEGER PRIMARY KEY,
    entry_id        INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    sample          TEXT NOT NULL DEFAULT '',
    sample_key      TEXT NOT NULL DEFAULT '',
    sample_name     TEXT NOT NULL DEFAULT '',
    report_type     TEXT NOT NULL DEFAULT '',
    report_type_key TEXT NOT NULL DEFAULT '',
    rt              REAL,
    rt_key          REAL,
    conc_kg         REAL,
    conc_area       REAL,
    area_pct        REAL,
    match_pct       REAL,
    db              TEXT NOT NULL DEFAULT '',
    simulant        TEXT NOT NULL DEFAULT '',
    temperature     TEXT NOT NULL DEFAULT '',
    duration        TEXT NOT NULL DEFAULT '',
    migration_cell  TEXT NOT NULL DEFAULT '',
    analyst         TEXT NOT NULL DEFAULT '',
    source_file     TEXT NOT NULL DEFAULT '',
    output_file     TEXT NOT NULL DEFAULT '',
    date_text       TEXT NOT NULL DEFAULT '',
    date_iso        TEXT,
    script_version  TEXT NOT NULL DEFAULT '',
    ranked_mz       TEXT NOT NULL DEFAULT '',
    name_raw        TEXT NOT NULL DEFAULT '',
    UNIQUE (entry_id, sample_key, report_type_key, rt_key)
);
CREATE INDEX IF NOT EXISTS ix_sightings_entry  ON sightings(entry_id);
CREATE INDEX IF NOT EXISTS ix_sightings_sample ON sightings(sample_key);
CREATE INDEX IF NOT EXISTS ix_sightings_rt     ON sightings(rt);

CREATE TABLE IF NOT EXISTS spectra (
    spectrum_id     INTEGER PRIMARY KEY,
    sighting_id     INTEGER REFERENCES sightings(sighting_id) ON DELETE CASCADE,
    entry_id        INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    kind            TEXT NOT NULL DEFAULT 'measured',
    source_path     TEXT NOT NULL DEFAULT '',
    apex_scan       INTEGER, bg_scan INTEGER, bounds_rule TEXT,
    apex_tic        INTEGER, bg_tic  INTEGER,
    rt              REAL,
    mz_unit         REAL NOT NULL DEFAULT 1.0,
    normalised      INTEGER NOT NULL DEFAULT 0,
    n_ions          INTEGER NOT NULL DEFAULT 0,
    base_peak_mz    INTEGER,
    base_peak_int   REAL,
    total_intensity REAL,
    mz_blob         BLOB NOT NULL,
    intensity_blob  BLOB NOT NULL,
    checksum        TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_spectra_entry    ON spectra(entry_id);
CREATE INDEX IF NOT EXISTS ix_spectra_sighting ON spectra(sighting_id);

CREATE TABLE IF NOT EXISTS spectrum_top_ions (
    spectrum_id INTEGER NOT NULL REFERENCES spectra(spectrum_id) ON DELETE CASCADE,
    mz          INTEGER NOT NULL,
    rel_int     REAL    NOT NULL,
    rank        INTEGER NOT NULL,
    PRIMARY KEY (spectrum_id, mz)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_top_ions_mz ON spectrum_top_ions(mz, rel_int DESC);

CREATE TABLE IF NOT EXISTS tic_slices (
    tic_id         INTEGER PRIMARY KEY,
    sighting_id    INTEGER REFERENCES sightings(sighting_id) ON DELETE CASCADE,
    spectrum_id    INTEGER REFERENCES spectra(spectrum_id)   ON DELETE CASCADE,
    rt_start       REAL NOT NULL,
    rt_end         REAL NOT NULL,
    apex_rt        REAL,
    apex_index     INTEGER,
    scan_start     INTEGER, scan_end INTEGER,
    n_points       INTEGER NOT NULL DEFAULT 0,
    rt_blob        BLOB NOT NULL,
    intensity_blob BLOB NOT NULL,
    created_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_tic_sighting ON tic_slices(sighting_id);

-- Observed Kovats retention indices per substance and method (SS VI.12).
-- There is no RI library in the data: neither PBM nor CASINFO supplies one, so
-- the comparison value is accumulated here instead. A running mean rather than
-- one row per observation, because the only question ever asked of it is
-- "does this RI fit what this CAS usually does on this method" -- and a mean
-- plus a count answers it without the table growing with the workload.
-- The method is part of the key: an RI is meaningless without the column it
-- was measured on.
CREATE TABLE IF NOT EXISTS ri_observed (
    cas     TEXT    NOT NULL,
    method  TEXT    NOT NULL DEFAULT '',
    ri      REAL    NOT NULL,
    n       INTEGER NOT NULL DEFAULT 1,
    updated TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (cas, method)
);
CREATE INDEX IF NOT EXISTS ix_ri_observed_cas ON ri_observed(cas);
"""


# --------------------------------------------------------------------------
# Array encoding
# --------------------------------------------------------------------------

def _le(arr: array.array) -> bytes:
    """Little-endian bytes, regardless of host byte order."""
    if sys.byteorder != "little":
        arr = array.array(arr.typecode, arr)
        arr.byteswap()
    return arr.tobytes()


def _from_le(blob: bytes, typecode: str) -> list:
    arr = array.array(typecode)
    arr.frombytes(blob)
    if sys.byteorder != "little":
        arr.byteswap()
    return list(arr)


def encode_spectrum(spectrum: Iterable[tuple[float, float]]) -> dict[str, Any]:
    """Pack ``[(m/z, intensity), ...]`` into the columns of ``spectra``.

    The encoding is stored alongside the arrays -- unit, normalisation flag and
    a checksum -- so a blob written today stays readable after a code change.
    """
    pairs = sorted((float(mz), float(it)) for mz, it in spectrum)
    mz_arr = array.array("f", [p[0] for p in pairs])
    it_arr = array.array("i", [int(round(p[1])) for p in pairs])
    mz_blob, it_blob = _le(mz_arr), _le(it_arr)
    base = max(pairs, key=lambda p: p[1]) if pairs else (None, None)
    return {
        "mz_blob": mz_blob,
        "intensity_blob": it_blob,
        "n_ions": len(pairs),
        "base_peak_mz": int(round(base[0])) if base[0] is not None else None,
        "base_peak_int": base[1],
        "total_intensity": sum(p[1] for p in pairs) if pairs else 0.0,
        "checksum": hashlib.sha256(mz_blob + it_blob).hexdigest(),
    }


def decode_spectrum(mz_blob: bytes, intensity_blob: bytes) -> list[tuple[float, int]]:
    return list(zip(_from_le(mz_blob, "f"), _from_le(intensity_blob, "i")))


def encode_tic(rts: Iterable[float], intensities: Iterable[float]) -> dict[str, Any]:
    rt_arr = array.array("f", [float(x) for x in rts])
    it_arr = array.array("i", [int(round(x)) for x in intensities])
    return {
        "rt_blob": _le(rt_arr),
        "intensity_blob": _le(it_arr),
        "n_points": len(rt_arr),
    }


def significant_ions(spectrum: Iterable[tuple[float, float]],
                     fraction: float = SIGNIFICANT_ION_FRACTION,
                     limit: int = SIGNIFICANT_ION_LIMIT) -> list[tuple[int, float, int]]:
    """``[(nominal m/z, per-mille of base peak, rank), ...]``, strongest first.

    This subset -- not the full spectrum -- is what the set-based scoring runs
    on. Feeding it 50-500 raw ions makes the homologue detectors fire on every
    entry and drives ion overlap toward zero for every pair.
    """
    pairs = [(float(mz), float(it)) for mz, it in spectrum if it > 0]
    if not pairs:
        return []
    peak = max(p[1] for p in pairs)
    if peak <= 0:
        return []
    keep = [p for p in pairs if p[1] >= peak * fraction]
    keep.sort(key=lambda p: p[1], reverse=True)
    out: list[tuple[int, float, int]] = []
    seen: set[int] = set()
    for mz, it in keep:
        nominal = int(round(mz))
        if nominal in seen:
            continue
        seen.add(nominal)
        out.append((nominal, it / peak * 1000.0, len(out) + 1))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def identity_key(ions: Iterable[tuple[int, float, int]],
                 count: int = ENTRY_KEY_IONS) -> tuple[str, list[int]]:
    """``(mz_key, ranked masses)`` identifying an unknown.

    ``ions`` comes from :func:`significant_ions`, strongest first. The key is
    the strongest ``count`` masses sorted ascending -- the same shape as the
    legacy ``canonical_mz_key``, so old and new entries live in one namespace.
    """
    ranked = [mz for mz, _, _ in ions]
    key_masses = sorted(ranked[:count])
    return "/".join(str(m) for m in key_masses), ranked


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Open the register with settings safe for a network share."""
    path = Path(path)
    if not create and not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = DELETE")   # never WAL on a share
    con.execute("PRAGMA synchronous  = FULL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 15000")
    return con


class writing:
    """``with writing(con):`` -- an immediate transaction, rolled back on error."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __enter__(self) -> sqlite3.Connection:
        self.con.execute("BEGIN IMMEDIATE")
        return self.con

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.con.execute("COMMIT")
        else:
            self.con.execute("ROLLBACK")
        return False


def create_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    upgrade_schema(con)
    # Additive extension: old entries, sightings and spectra remain intact.
    from gc_atlas_store import SCHEMA as ei_schema
    con.executescript(ei_schema)


#: Columns added after schema v2. A register built by an earlier build of this
#: module is upgraded in place rather than rebuilt: it may already hold measured
#: spectra that the JSON backup does not have, so a rebuild would lose data.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("entries", "identity_key", "TEXT NOT NULL DEFAULT ''"),
)


def upgrade_schema(con: sqlite3.Connection) -> None:
    """Add columns a v2 database is missing, then backfill what can be derived."""
    changed = False
    for table, column, decl in _ADDED_COLUMNS:
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            changed = True
    if changed:
        con.executescript(
            "CREATE INDEX IF NOT EXISTS ix_entries_identity ON entries(identity_key);")
    backfill_identity_keys(con)


def backfill_identity_keys(con: sqlite3.Connection) -> int:
    """Derive ``identity_key`` for every entry that has none yet."""
    rows = con.execute(
        "SELECT entry_id, ranked_mz, canonical_mz, mz_key FROM entries "
        "WHERE identity_key = '' OR identity_key IS NULL").fetchall()
    updates = []
    for row in rows:
        key = entry_identity_key({
            "ranked_mz": row["ranked_mz"],
            "canonical_mz": row["canonical_mz"] or row["mz_key"],
        })
        if key:
            updates.append((key, row["entry_id"]))
    if updates:
        con.executemany("UPDATE entries SET identity_key = ? WHERE entry_id = ?",
                        updates)
    return len(updates)


def meta_get(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT value FROM register_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT INTO register_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------
# Observed retention indices (spec v3.0 SS VI.12)
# --------------------------------------------------------------------------
#
# There is no RI library in the data -- neither PBM nor CASINFO carries one --
# so the comparison value is the one the laboratory accumulates itself. This is
# the cheapest available check against a spectrally plausible but
# chromatographically impossible hit: a spectrum can look like a phthalate and
# still elute 200 index units away from every phthalate ever measured here.
#
# Writes follow the register's existing rule without exception: only on
# explicit confirmation (SS 10), and only for ``Accepted`` rows. Nothing in this
# section writes unless the analyst confirmed the sighting it came from.


def ensure_ri_table(con: sqlite3.Connection) -> None:
    """Create ``ri_observed`` if this register predates it.

    A register written by an earlier build has every other table and none of
    this one. ``CREATE TABLE IF NOT EXISTS`` adds it without touching a single
    existing row, which is why the migration is a no-op for everybody who
    already has it.
    """
    # Two ``execute`` calls, deliberately not ``executescript``: the latter
    # commits any open transaction before it runs, which would tear a
    # ``BEGIN IMMEDIATE`` in half. DDL inside a transaction is fine in SQLite.
    con.execute(
        "CREATE TABLE IF NOT EXISTS ri_observed ("
        " cas TEXT NOT NULL, method TEXT NOT NULL DEFAULT '',"
        " ri REAL NOT NULL, n INTEGER NOT NULL DEFAULT 1,"
        " updated TEXT NOT NULL DEFAULT '', PRIMARY KEY (cas, method))")
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_ri_observed_cas ON ri_observed(cas)")


# GCWS-PATCH: removed the unused private RI conversion helper.
def _ri_key(cas: Any, method: Any = "") -> tuple[str, str]:
    """Normalised ``(cas, method)``. Both sides of every query go through here."""
    return (str(cas or "").strip(), str(method or "").strip())


def ri_observation(con: sqlite3.Connection, cas: Any,
                   method: Any = "") -> Optional[dict[str, Any]]:
    """``{"ri", "n", "updated"}`` for one CAS and method, or None.

    Returns None rather than raising on a register that has no ``ri_observed``
    table yet: a read must never be the thing that fails on an old file.
    """
    key = _ri_key(cas, method)
    if not key[0]:
        return None
    try:
        row = con.execute(
            "SELECT ri, n, updated FROM ri_observed WHERE cas = ? AND method = ?",
            key).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {"cas": key[0], "method": key[1], "ri": row["ri"],
            "n": int(row["n"]), "updated": row["updated"]}


# --------------------------------------------------------------------------
# Migration from the JSON register
# --------------------------------------------------------------------------

def _iso_date(text: Any) -> Optional[str]:
    """``dd.mm.yyyy`` -> ``yyyy-mm-dd``.

    The JSON register sorts these strings lexically, so 27.08.2026 sorts before
    12.09.2026. Storing an ISO column alongside fixes first/last seen.
    """
    s = str(text or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _seen_iso(entry: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """``(first_seen_iso, last_seen_iso)`` ordered by real date, not lexically.

    Spec defect 6: ``recompute_unknown_entry`` sorts the ``dd.mm.yyyy`` strings,
    so ``27.08.2026`` lands *after* ``12.09.2026`` and the two fields come out
    swapped. Converting the already-wrong ``first_seen`` would only restate the
    defect in ISO form -- the order is fixed by re-deriving it from the
    sightings' own dates. The text columns still hold the JSON values verbatim,
    so nothing is rewritten behind the analyst's back; the ISO columns are the
    ones to sort on.
    """
    dates = sorted(d for d in (_iso_date(s.get("date"))
                               for s in entry.get("sightings") or []) if d)
    if dates:
        return dates[0], dates[-1]
    first, last = _iso_date(entry.get("first_seen")), _iso_date(entry.get("last_seen"))
    if first and last and first > last:
        first, last = last, first
    return first, last


def _entry_number(unknown_id: Any, fallback: int) -> int:
    s = str(unknown_id or "")
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else fallback


def _mz_list(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)):
        return [int(v) for v in value if str(v).strip().lstrip("-").isdigit()]
    out = []
    for part in str(value or "").replace(",", "/").split("/"):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _join_mz(values: Iterable[int]) -> str:
    return "/".join(str(v) for v in values)


def load_json_register(json_path: Path) -> dict[str, Any]:
    """Read the JSON register and reject anything that is not one."""
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("unknowns"), dict):
        raise RuntimeError(f"{json_path} hat nicht das Format des Unknown-Registers.")
    return payload


def _temp_db_path(db_path: Path, tag: str = "") -> Path:
    """A temp name beside the destination -- same directory, same filesystem."""
    parts = [p for p in (db_path.stem, tag, str(os.getpid())) if p]
    tmp = db_path.with_name("." + "_".join(parts) + ".tmp")
    tmp.unlink(missing_ok=True)
    return tmp


def _discard(tmp: Path) -> None:
    """Remove a temp database and the rollback journal it may have left."""
    for path in (tmp, tmp.with_name(tmp.name + "-journal")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _build_into(tmp: Path, json_path: Path, payload: dict[str, Any]
                ) -> tuple[sqlite3.Connection, dict[str, int]]:
    """Assemble the whole register into ``tmp`` and hand back the open handle.

    This is the one build path: :func:`migrate_json` publishes what it produces,
    :func:`dry_run_migration` inspects and then deletes it. Keeping them on the
    same code means the rehearsal exercises the real thing.
    """
    unknowns: dict[str, Any] = payload.get("unknowns") or {}
    counts = {"entries": 0, "sightings": 0}
    con = connect(tmp)
    try:
        create_schema(con)
        with writing(con):
            meta_set(con, "schema_version", SCHEMA_VERSION)
            meta_set(con, "next_id", payload.get("next_id", len(unknowns) + 1))
            meta_set(con, "updated_at", payload.get("updated_at", ""))
            meta_set(con, "migrated_from", str(json_path))
            meta_set(con, "migrated_at", datetime.now().isoformat(timespec="seconds"))
            meta_set(con, "source_sha256",
                     hashlib.sha256(Path(json_path).read_bytes()).hexdigest())
            meta_set(con, "significant_ion_fraction", SIGNIFICANT_ION_FRACTION)
            meta_set(con, "significant_ion_limit", SIGNIFICANT_ION_LIMIT)

            for n, (mz_key, entry) in enumerate(sorted(unknowns.items()), 1):
                eid = _entry_number(entry.get("id"), n)
                _insert_entry(con, eid, mz_key, entry)
                counts["entries"] += 1
                for sighting in entry.get("sightings") or []:
                    _insert_sighting(con, eid, sighting)
                    counts["sightings"] += 1
                for research in entry.get('ei_investigations') or []:
                    con.execute('INSERT OR IGNORE INTO ei_investigations VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (research['investigation_id'], eid, None, research['created_at'],
                         research['decision'], research['candidate_name'], research['candidate_cas'],
                         research['note'], research['next_steps'], research['payload_json']))
    except BaseException:
        try:
            con.close()
        finally:
            _discard(tmp)
        raise
    return con, counts


def migrate_json(json_path: Path, db_path: Path, *,
                 backup: bool = True) -> dict[str, Any]:
    """Build a SQLite register from ``unknown_register.json``.

    The database is assembled in a temp file in the same directory, verified,
    and only then moved into place, so a half-written database never becomes
    visible to a reader. On any verification failure the temp file is removed
    and the JSON stays authoritative.

    Rehearse it with :func:`dry_run_migration` before running it for real.
    """
    json_path, db_path = Path(json_path), Path(db_path)
    payload = load_json_register(json_path)
    unknowns: dict[str, Any] = payload.get("unknowns") or {}

    report: dict[str, Any] = {
        "entries": 0, "sightings": 0, "backup": None, "db": str(db_path),
    }

    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = json_path.with_name(f"{json_path.stem}.{stamp}.backup.json")
        shutil.copy2(json_path, target)          # abort the migration if this fails
        report["backup"] = str(target)

    tmp = _temp_db_path(db_path)
    con, counts = _build_into(tmp, json_path, payload)
    report.update(counts)
    try:
        problems = _verify(con, unknowns)
        if problems:
            raise RuntimeError(
                "Migration abgebrochen, das JSON-Register bleibt maßgeblich:\n  "
                + "\n  ".join(problems)
            )
        con.execute("PRAGMA integrity_check")
    except BaseException:
        con.close()
        _discard(tmp)
        raise
    con.close()

    os.replace(tmp, db_path)
    return report


def _insert_entry(con: sqlite3.Connection, eid: int, mz_key: str,
                  entry: dict[str, Any]) -> None:
    canonical = _mz_list(entry.get("canonical_mz") or mz_key)
    ranked = _mz_list(entry.get("ranked_mz"))
    first_iso, last_iso = _seen_iso(entry)
    blob = " ".join(str(entry.get(k, "")) for k in
                    ("id", "label", "assigned_name", "assigned_cas", "note", "cluster"))

    con.execute(
        """INSERT INTO entries (
            entry_id, unknown_id, mz_key, identity_key, label, canonical_mz,
            ranked_mz,
            base_peak, rank_unknown, status, assigned_name, assigned_cas, note,
            linked_to, cluster, n_sightings, n_samples,
            rt_mean, rt_min, rt_max, rt_sd,
            conc_min, conc_median, conc_max, conc_max_sample, area_pct_max,
            ttc_flag, homologue_series, first_seen, last_seen,
            first_seen_iso, last_seen_iso, search_blob)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            eid, entry.get("id") or f"UNK-{eid:04d}", mz_key,
            entry_identity_key(entry) or "/".join(str(m) for m in sorted(
                (ranked or canonical)[:ENTRY_KEY_IONS])),
            entry.get("label", ""), _join_mz(canonical), _join_mz(ranked),
            entry.get("base_peak"), 1 if entry.get("rank_unknown") else 0,
            entry.get("status", "offen"), entry.get("assigned_name", ""),
            entry.get("assigned_cas", ""), entry.get("note", ""),
            entry.get("linked_to"), entry.get("cluster", ""),
            entry.get("n_sightings", 0), entry.get("n_samples", 0),
            entry.get("rt_mean"), entry.get("rt_min"), entry.get("rt_max"),
            entry.get("rt_sd"), entry.get("conc_min"), entry.get("conc_median"),
            entry.get("conc_max"), entry.get("conc_max_sample", ""),
            entry.get("area_pct_max"), entry.get("ttc_flag", ""),
            entry.get("homologue_series", ""), entry.get("first_seen", ""),
            entry.get("last_seen", ""), first_iso, last_iso, blob.casefold(),
        ),
    )

    rank_of = {mz: i + 1 for i, mz in enumerate(ranked)}
    con.executemany(
        "INSERT OR IGNORE INTO entry_ions(entry_id, mz, rank) VALUES (?,?,?)",
        [(eid, mz, rank_of.get(mz)) for mz in sorted(set(canonical) | set(ranked))],
    )
    for table, column, values in (
        ("entry_samples", "sample", entry.get("samples")),
        ("entry_simulants", "simulant", entry.get("simulants")),
        ("entry_materials", "material", entry.get("materials")),
    ):
        con.executemany(
            f"INSERT OR IGNORE INTO {table}(entry_id, {column}) VALUES (?,?)",
            [(eid, str(v)) for v in (values or []) if str(v).strip()],
        )
    con.executemany(
        "INSERT OR IGNORE INTO entry_class_hints(entry_id, hint, ord) VALUES (?,?,?)",
        [(eid, str(h), i) for i, h in enumerate(entry.get("class_hint") or [])],
    )
    con.executemany(
        "INSERT OR IGNORE INTO entry_mz_variants(entry_id, variant, votes) VALUES (?,?,?)",
        [(eid, str(k), int(v)) for k, v in (entry.get("ranked_variants") or {}).items()],
    )


def _insert_sighting(con: sqlite3.Connection, eid: int, s: dict[str, Any]) -> None:
    rt = s.get("rt")
    con.execute(
        """INSERT OR IGNORE INTO sightings (
            entry_id, sample, sample_key, sample_name, report_type, report_type_key,
            rt, rt_key, conc_kg, conc_area, area_pct, match_pct, db,
            simulant, temperature, duration, migration_cell, analyst,
            source_file, output_file, date_text, date_iso, script_version,
            ranked_mz, name_raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            eid, s.get("sample", ""), str(s.get("sample", "")).casefold(),
            s.get("sample_name", ""), s.get("report_type", ""),
            str(s.get("report_type", "")).casefold(),
            rt, round(rt, 4) if isinstance(rt, (int, float)) else None,
            s.get("conc_kg"), s.get("conc_area"), s.get("area_pct"),
            s.get("match"), s.get("db", ""), s.get("simulant", ""),
            s.get("temperature", ""), s.get("duration", ""),
            s.get("migration_cell", ""), s.get("analyst", ""),
            s.get("source_file", ""), s.get("output_file", ""),
            s.get("date", ""), _iso_date(s.get("date")),
            s.get("script_version", ""), _join_mz(_mz_list(s.get("ranked_mz"))),
            s.get("name_raw", ""),
        ),
    )


def _verify(con: sqlite3.Connection, unknowns: dict[str, Any]) -> list[str]:
    """Counts and ids must match the source exactly before publishing."""
    problems: list[str] = []
    n_entries = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    if n_entries != len(unknowns):
        problems.append(f"Einträge: {n_entries} in der DB, {len(unknowns)} im JSON")

    expected_sightings = sum(len(e.get("sightings") or []) for e in unknowns.values())
    n_sightings = con.execute("SELECT COUNT(*) c FROM sightings").fetchone()["c"]
    if n_sightings > expected_sightings:
        problems.append(
            f"Sichtungen: {n_sightings} in der DB, {expected_sightings} im JSON")

    have = {r["unknown_id"] for r in con.execute("SELECT unknown_id FROM entries")}
    want = {e.get("id") for e in unknowns.values() if e.get("id")}
    missing = want - have
    if missing:
        problems.append(f"fehlende IDs: {sorted(missing)[:5]}")
    return problems


# --------------------------------------------------------------------------
# Dry run and post-migration verification
#
# The migration itself is a one-way door on a shared drive, so it gets a
# rehearsal: build the whole database into a temp file, check it against the
# JSON it came from, print what was found, and delete it again. The same
# comparison runs against a real database afterwards, which is the part
# ``_verify`` above deliberately does not do -- it only guards the publish step
# and must stay cheap.
# --------------------------------------------------------------------------

#: Everything the schema keeps. A key of the JSON that is in none of these sets
#: is silently discarded by the migration, which is exactly what a dry run has
#: to say out loud before the analyst runs it for real.
KEPT_PAYLOAD_FIELDS = frozenset({"unknowns", "next_id", "updated_at"})
KEPT_ENTRY_FIELDS = frozenset({
    "id", "label", "canonical_mz", "ranked_mz", "ranked_variants", "base_peak",
    "rank_unknown", "status", "assigned_name", "assigned_cas", "note",
    "linked_to", "cluster", "sightings", "samples", "simulants", "materials",
    "class_hint", "n_sightings", "n_samples", "rt_mean", "rt_min", "rt_max",
    "rt_sd", "conc_min", "conc_median", "conc_max", "conc_max_sample",
    "area_pct_max", "ttc_flag", "homologue_series", "first_seen", "last_seen",
})
KEPT_SIGHTING_FIELDS = frozenset({
    "sample", "sample_name", "report_type", "simulant", "temperature",
    "duration", "migration_cell", "analyst", "source_file", "output_file",
    "date", "script_version", "ranked_mz", "name_raw", "rt", "conc_kg",
    "conc_area", "area_pct", "match", "db",
})
#: Dropped on purpose: the JSON's own version number is replaced by
#: ``SCHEMA_VERSION``, which describes the SQLite schema, not the JSON one.
EXPECTED_DROPPED_PAYLOAD_FIELDS = frozenset({"schema_version"})

#: ``(db column, json key)`` pairs compared per entry.
_ENTRY_TEXT_FIELDS = (
    ("label", "label"), ("status", "status"),
    ("assigned_name", "assigned_name"), ("assigned_cas", "assigned_cas"),
    ("note", "note"), ("cluster", "cluster"),
    ("conc_max_sample", "conc_max_sample"), ("ttc_flag", "ttc_flag"),
    ("homologue_series", "homologue_series"),
    ("first_seen", "first_seen"), ("last_seen", "last_seen"),
)
_ENTRY_NUMBER_FIELDS = (
    ("base_peak", "base_peak"), ("n_sightings", "n_sightings"),
    ("n_samples", "n_samples"), ("rt_mean", "rt_mean"), ("rt_min", "rt_min"),
    ("rt_max", "rt_max"), ("rt_sd", "rt_sd"), ("conc_min", "conc_min"),
    ("conc_median", "conc_median"), ("conc_max", "conc_max"),
    ("area_pct_max", "area_pct_max"),
)
_SIGHTING_TEXT_FIELDS = (
    ("sample", "sample"), ("sample_name", "sample_name"),
    ("report_type", "report_type"), ("simulant", "simulant"),
    ("temperature", "temperature"), ("duration", "duration"),
    ("migration_cell", "migration_cell"), ("analyst", "analyst"),
    ("source_file", "source_file"), ("output_file", "output_file"),
    ("date_text", "date"), ("script_version", "script_version"),
    ("name_raw", "name_raw"), ("db", "db"),
)
_SIGHTING_NUMBER_FIELDS = (
    ("rt", "rt"), ("conc_kg", "conc_kg"), ("conc_area", "conc_area"),
    ("area_pct", "area_pct"), ("match_pct", "match"),
)

#: What §10 mandates on a share. Checked on the file, not assumed from the code.
REQUIRED_PRAGMAS = {
    "journal_mode": "delete",
    "synchronous": 2,          # FULL
    "busy_timeout": 15000,
    "foreign_keys": 1,
}


def pragma_snapshot(con: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in REQUIRED_PRAGMAS:
        row = con.execute(f"PRAGMA {name}").fetchone()
        value = row[0] if row is not None else None
        out[name] = value.lower() if isinstance(value, str) else value
    return out


def check_pragmas(con: sqlite3.Connection) -> tuple[dict[str, Any], list[str]]:
    """The durability settings §10 requires, read back off the open database."""
    snapshot = pragma_snapshot(con)
    problems = [f"PRAGMA {name}: {snapshot.get(name)!r}, erwartet {want!r}"
                for name, want in REQUIRED_PRAGMAS.items()
                if snapshot.get(name) != want]
    return snapshot, problems


def entry_identity_key(entry: dict[str, Any],
                       count: int = ENTRY_KEY_IONS) -> str:
    """The dedup key a JSON entry will answer to once it is in the register.

    :func:`identity_key` derives it from a measured spectrum; a migrated entry
    has none (§10: JSON entries arrive without spectra), so it is derived from
    the ranked list instead. Both end up as the strongest four masses sorted
    ascending, which is the point -- old and new entries share one namespace.
    """
    ranked = _mz_list(entry.get("ranked_mz")) or _mz_list(entry.get("canonical_mz"))
    return "/".join(str(m) for m in sorted(ranked[:count]))


def _sighting_key(entry_id: int, s: dict[str, Any]) -> tuple:
    """The uniqueness key of ``sightings``, in Python semantics.

    SQLite treats NULLs in a UNIQUE index as distinct, Python does not. Counting
    the expected rows this way is what makes a duplicate-key surplus in the
    database visible instead of silently doubling the sighting count.
    """
    rt = s.get("rt")
    return (entry_id, str(s.get("sample", "")).casefold(),
            str(s.get("report_type", "")).casefold(),
            round(rt, 4) if isinstance(rt, (int, float)) and not isinstance(rt, bool)
            else None)


def _text_equal(db_value: Any, json_value: Any) -> bool:
    return str(db_value if db_value is not None else "") == \
        str(json_value if json_value is not None else "")


def _number_equal(db_value: Any, json_value: Any) -> bool:
    if db_value is None or json_value is None:
        return db_value is None and json_value is None
    try:
        left, right = float(db_value), float(json_value)
    except (TypeError, ValueError):
        return str(db_value) == str(json_value)
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)


def _count_fields(seen: dict[str, int], obj: dict[str, Any],
                  kept: frozenset) -> None:
    for key in obj:
        if key not in kept:
            seen[key] = seen.get(key, 0) + 1


def verify_migration(con: sqlite3.Connection,
                     payload: dict[str, Any]) -> dict[str, Any]:
    """Compare a migrated database against the JSON it came from.

    Returns a structured pass/fail report rather than raising, so it can be used
    both as the rehearsal in :func:`dry_run_migration` and as an after-the-fact
    audit of a database that was already published. ``problems`` are
    discrepancies -- something the database says that the JSON does not.
    ``warnings`` are things the analyst has to know but that do not make the
    migration wrong: dropped keys, dedup collisions, dates that will not parse.
    """
    unknowns: dict[str, Any] = payload.get("unknowns") or {}
    problems: list[str] = []
    warnings: list[str] = []

    # ---- counts ----------------------------------------------------------
    db_entries = con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    db_sightings = con.execute("SELECT COUNT(*) c FROM sightings").fetchone()["c"]
    json_sightings = sum(len(e.get("sightings") or []) for e in unknowns.values())

    rows_by_key: dict[str, sqlite3.Row] = {}
    for row in con.execute("SELECT * FROM entries"):
        rows_by_key[row["mz_key"]] = row

    expected_keys: set[tuple] = set()
    for mz_key, entry in unknowns.items():
        row = rows_by_key.get(mz_key)
        eid = row["entry_id"] if row is not None else None
        for s in entry.get("sightings") or []:
            expected_keys.add(_sighting_key(eid, s))
    unique_sightings = len(expected_keys)

    if db_entries != len(unknowns):
        problems.append(f"Einträge: {db_entries} in der DB, {len(unknowns)} im JSON")
    if db_sightings != unique_sightings:
        problems.append(f"Sichtungen: {db_sightings} in der DB, {unique_sightings} "
                        f"eindeutige im JSON ({json_sightings} insgesamt)")
    if json_sightings != unique_sightings:
        warnings.append(f"{json_sightings - unique_sightings} Sichtung(en) im JSON "
                        f"teilen sich einen Dedup-Schlüssel und werden zusammengefasst")

    # ---- ids -------------------------------------------------------------
    db_ids = {r["unknown_id"]: r["entry_id"] for r in
              con.execute("SELECT unknown_id, entry_id FROM entries")}
    json_ids = {e.get("id") for e in unknowns.values() if e.get("id")}
    missing = sorted(json_ids - set(db_ids))
    extra = sorted(set(db_ids) - json_ids)
    if missing:
        problems.append(f"fehlende IDs: {missing[:10]}")
    if extra:
        problems.append(f"zusätzliche IDs: {extra[:10]}")

    numbers = sorted(db_ids.values())
    duplicates: list[int] = []
    for entry in unknowns.values():
        pass
    seen_numbers: dict[int, int] = {}
    for n, (mz_key, entry) in enumerate(sorted(unknowns.items()), 1):
        num = _entry_number(entry.get("id"), n)
        seen_numbers[num] = seen_numbers.get(num, 0) + 1
    duplicates = sorted(num for num, c in seen_numbers.items() if c > 1)
    if duplicates:
        problems.append(f"doppelte entry_id aus UNK-Nummern: {duplicates[:10]}")

    next_id = int(meta_get(con, "next_id", 0) or 0)
    if numbers and next_id <= numbers[-1]:
        warnings.append(f"next_id {next_id} liegt nicht über der höchsten "
                        f"entry_id {numbers[-1]} -- der Zähler würde kollidieren")

    ids = {
        "count": len(numbers),
        "min": numbers[0] if numbers else None,
        "max": numbers[-1] if numbers else None,
        "missing": missing, "extra": extra, "duplicate_numbers": duplicates,
        "next_id": next_id,
    }

    # ---- spectra ---------------------------------------------------------
    with_spectrum = con.execute(
        "SELECT COUNT(DISTINCT entry_id) c FROM spectra").fetchone()["c"]
    spectra = {
        "spectra_rows": con.execute("SELECT COUNT(*) c FROM spectra").fetchone()["c"],
        "tic_rows": con.execute("SELECT COUNT(*) c FROM tic_slices").fetchone()["c"],
        "entries_with_spectrum": with_spectrum,
        "entries_without_spectrum": db_entries - with_spectrum,
    }

    # ---- dedup-key collisions -------------------------------------------
    by_identity: dict[str, list[str]] = {}
    for mz_key, entry in unknowns.items():
        key = entry_identity_key(entry)
        if key:
            by_identity.setdefault(key, []).append(entry.get("id") or mz_key)
    collisions = [{"identity_key": k, "entries": sorted(v)}
                  for k, v in sorted(by_identity.items()) if len(v) > 1]
    if collisions:
        warnings.append(
            f"{len(collisions)} Dedup-Schlüssel (stärkste {ENTRY_KEY_IONS} Massen) "
            f"werden von je mehreren Einträgen belegt -- neue Sichtungen landen "
            f"beim erstbesten Eintrag")

    # ---- dates -----------------------------------------------------------
    bad_entry_dates: list[dict[str, Any]] = []
    bad_sighting_dates: list[dict[str, Any]] = []
    reordered: list[dict[str, Any]] = []
    for mz_key, entry in unknowns.items():
        ident = entry.get("id") or mz_key
        for field in ("first_seen", "last_seen"):
            raw = entry.get(field)
            if str(raw or "").strip() and _iso_date(raw) is None:
                bad_entry_dates.append({"entry": ident, "field": field, "value": raw})
        for s in entry.get("sightings") or []:
            raw = s.get("date")
            if _iso_date(raw) is None:
                bad_sighting_dates.append({"entry": ident, "sample": s.get("sample"),
                                           "value": raw})
        first_iso, last_iso = _seen_iso(entry)
        lexical = (_iso_date(entry.get("first_seen")), _iso_date(entry.get("last_seen")))
        if first_iso and last_iso and lexical != (first_iso, last_iso):
            reordered.append({
                "entry": ident,
                "json_first_seen": entry.get("first_seen"),
                "json_last_seen": entry.get("last_seen"),
                "iso_first_seen": first_iso, "iso_last_seen": last_iso,
            })

    inverted = [dict(r) for r in con.execute(
        "SELECT unknown_id, first_seen, last_seen, first_seen_iso, last_seen_iso "
        "FROM entries WHERE first_seen_iso IS NOT NULL "
        "AND last_seen_iso IS NOT NULL AND first_seen_iso > last_seen_iso")]
    if inverted:
        problems.append(f"{len(inverted)} Eintrag/Einträge mit first_seen_iso > "
                        f"last_seen_iso: {[r['unknown_id'] for r in inverted][:5]}")
    if reordered:
        warnings.append(f"{len(reordered)} Eintrag/Einträge hatten first/last_seen "
                        f"lexikalisch vertauscht (Defekt 6); die ISO-Spalten stehen "
                        f"in der richtigen Reihenfolge")

    dates = {
        "unparseable_entry_dates": bad_entry_dates,
        "unparseable_sighting_dates": bad_sighting_dates,
        "iso_corrected": reordered,
        "iso_inverted_in_db": inverted,
    }

    # ---- fields the schema drops ----------------------------------------
    payload_dropped: dict[str, int] = {}
    entry_dropped: dict[str, int] = {}
    sighting_dropped: dict[str, int] = {}
    _count_fields(payload_dropped, payload, KEPT_PAYLOAD_FIELDS)
    for entry in unknowns.values():
        _count_fields(entry_dropped, entry, KEPT_ENTRY_FIELDS)
        for s in entry.get("sightings") or []:
            _count_fields(sighting_dropped, s, KEPT_SIGHTING_FIELDS)
    unexpected = (set(payload_dropped) - EXPECTED_DROPPED_PAYLOAD_FIELDS
                  ) | set(entry_dropped) | set(sighting_dropped)
    if unexpected:
        warnings.append("Felder im JSON, die das Schema verwirft: "
                        + ", ".join(sorted(unexpected)))
    dropped = {"payload": payload_dropped, "entry": entry_dropped,
               "sighting": sighting_dropped,
               "unexpected": sorted(unexpected)}

    # ---- per-entry field equality ---------------------------------------
    mismatches: list[dict[str, Any]] = []
    checked_entries = 0
    for mz_key, entry in sorted(unknowns.items()):
        row = rows_by_key.get(mz_key)
        ident = entry.get("id") or mz_key
        if row is None:
            problems.append(f"mz_key {mz_key} fehlt in der DB")
            continue
        checked_entries += 1

        def note(field: str, db_value: Any, json_value: Any) -> None:
            mismatches.append({"entry": ident, "scope": "entry", "field": field,
                               "db": db_value, "json": json_value})

        if not _text_equal(row["unknown_id"], entry.get("id") or row["unknown_id"]):
            note("id", row["unknown_id"], entry.get("id"))
        for column, key in _ENTRY_TEXT_FIELDS:
            if not _text_equal(row[column], entry.get(key, "")):
                note(key, row[column], entry.get(key))
        for column, key in _ENTRY_NUMBER_FIELDS:
            if not _number_equal(row[column], entry.get(key)):
                note(key, row[column], entry.get(key))
        if bool(row["rank_unknown"]) != bool(entry.get("rank_unknown")):
            note("rank_unknown", bool(row["rank_unknown"]), entry.get("rank_unknown"))
        if not _text_equal(row["linked_to"], entry.get("linked_to")):
            note("linked_to", row["linked_to"], entry.get("linked_to"))
        if _mz_list(row["canonical_mz"]) != _mz_list(
                entry.get("canonical_mz") or mz_key):
            note("canonical_mz", row["canonical_mz"], entry.get("canonical_mz"))
        if _mz_list(row["ranked_mz"]) != _mz_list(entry.get("ranked_mz")):
            note("ranked_mz", row["ranked_mz"], entry.get("ranked_mz"))

        eid = row["entry_id"]
        for table, column, key in (("entry_samples", "sample", "samples"),
                                   ("entry_simulants", "simulant", "simulants"),
                                   ("entry_materials", "material", "materials")):
            have = {r[0] for r in con.execute(
                f"SELECT {column} FROM {table} WHERE entry_id = ?", (eid,))}
            want = {str(v) for v in (entry.get(key) or []) if str(v).strip()}
            if have != want:
                note(key, sorted(have), sorted(want))
        have_hints = [r[0] for r in con.execute(
            "SELECT hint FROM entry_class_hints WHERE entry_id = ? ORDER BY ord",
            (eid,))]
        want_hints = [str(h) for h in (entry.get("class_hint") or [])]
        if have_hints != want_hints:
            note("class_hint", have_hints, want_hints)
        have_var = {r[0]: r[1] for r in con.execute(
            "SELECT variant, votes FROM entry_mz_variants WHERE entry_id = ?", (eid,))}
        want_var = {str(k): int(v) for k, v in
                    (entry.get("ranked_variants") or {}).items()}
        if have_var != want_var:
            note("ranked_variants", have_var, want_var)

        # ---- sightings of this entry -------------------------------------
        db_sight = {}
        for r in con.execute("SELECT * FROM sightings WHERE entry_id = ?", (eid,)):
            db_sight[(eid, r["sample_key"], r["report_type_key"], r["rt_key"])] = r
        for s in entry.get("sightings") or []:
            key = _sighting_key(eid, s)
            r = db_sight.get(key)
            if r is None:
                mismatches.append({"entry": ident, "scope": "sighting",
                                   "field": "missing", "db": None, "json": key})
                continue
            for column, jkey in _SIGHTING_TEXT_FIELDS:
                if not _text_equal(r[column], s.get(jkey, "")):
                    mismatches.append({"entry": ident, "scope": "sighting",
                                       "field": jkey, "db": r[column],
                                       "json": s.get(jkey)})
            for column, jkey in _SIGHTING_NUMBER_FIELDS:
                if not _number_equal(r[column], s.get(jkey)):
                    mismatches.append({"entry": ident, "scope": "sighting",
                                       "field": jkey, "db": r[column],
                                       "json": s.get(jkey)})
            if _mz_list(r["ranked_mz"]) != _mz_list(s.get("ranked_mz")):
                mismatches.append({"entry": ident, "scope": "sighting",
                                   "field": "ranked_mz", "db": r["ranked_mz"],
                                   "json": s.get("ranked_mz")})
            if r["date_iso"] != _iso_date(s.get("date")):
                mismatches.append({"entry": ident, "scope": "sighting",
                                   "field": "date_iso", "db": r["date_iso"],
                                   "json": _iso_date(s.get("date"))})

    if mismatches:
        problems.append(f"{len(mismatches)} Feldabweichung(en) zwischen DB und JSON")

    pragmas, pragma_problems = check_pragmas(con)
    problems.extend(pragma_problems)

    return {
        "ok": not problems,
        "counts": {
            "json_entries": len(unknowns), "db_entries": db_entries,
            "json_sightings": json_sightings,
            "json_sightings_unique": unique_sightings,
            "db_sightings": db_sightings,
            "entries_compared": checked_entries,
        },
        "ids": ids, "spectra": spectra, "dedup_collisions": collisions,
        "dates": dates, "dropped_fields": dropped,
        "field_mismatches": mismatches, "pragmas": pragmas,
        "problems": problems, "warnings": warnings,
        "schema_version": int(meta_get(con, "schema_version", SCHEMA_VERSION)),
    }


def dry_run_migration(json_path: Path, db_path: Optional[Path] = None
                      ) -> dict[str, Any]:
    """Rehearse the migration: build, verify, report, then throw it all away.

    Nothing is written to ``db_path`` and no backup is taken -- the only file
    touched is a temp database beside the destination, which is deleted before
    this returns whatever happens. That the destination is untouched is checked
    rather than assumed and reported as ``destination_untouched``.
    """
    json_path = Path(json_path)
    db_path = Path(db_path) if db_path else json_path.with_name(DB_FILENAME)
    payload = load_json_register(json_path)

    existed = db_path.exists()
    before = db_path.stat().st_mtime_ns if existed else None

    tmp = _temp_db_path(db_path, tag="dryrun")
    con: Optional[sqlite3.Connection] = None
    try:
        con, counts = _build_into(tmp, json_path, payload)
        report = verify_migration(con, payload)
        report["built"] = counts
        report["integrity_check"] = con.execute(
            "PRAGMA integrity_check").fetchone()[0]
        report["temp_db_bytes"] = tmp.stat().st_size
        report["gate_verdict"] = _verify(con, payload.get("unknowns") or {})
    finally:
        if con is not None:
            con.close()
        _discard(tmp)

    after = db_path.stat().st_mtime_ns if db_path.exists() else None
    report["mode"] = "dry-run"
    report["json"] = str(json_path)
    report["db"] = str(db_path)
    report["temp_db"] = str(tmp)
    report["destination_exists"] = db_path.exists()
    report["destination_untouched"] = (db_path.exists() == existed and before == after)
    report["leftovers"] = sorted(
        p.name for p in db_path.parent.glob(f".{db_path.stem}*dryrun*"))
    if not report["destination_untouched"] or report["leftovers"]:
        report["problems"].append(
            "Der Probelauf hat das Ziel verändert oder Dateien hinterlassen")
        report["ok"] = False
    if report["gate_verdict"]:
        report["problems"].extend(report["gate_verdict"])
        report["ok"] = False
    return report


def verify_database(json_path: Path, db_path: Path) -> dict[str, Any]:
    """Audit an already published register against the JSON it was built from."""
    json_path, db_path = Path(json_path), Path(db_path)
    payload = load_json_register(json_path)
    con = connect(db_path, create=False)
    try:
        report = verify_migration(con, payload)
        report["integrity_check"] = con.execute(
            "PRAGMA integrity_check").fetchone()[0]
        stored = meta_get(con, "source_sha256", "")
        actual = hashlib.sha256(json_path.read_bytes()).hexdigest()
        report["source_sha256"] = {"stored": stored, "json": actual,
                                   "match": bool(stored) and stored == actual}
        if stored and stored != actual:
            report["warnings"].append(
                "Das JSON hat sich seit der Migration geändert -- Abweichungen "
                "unten können daher aus dem JSON stammen, nicht aus der DB")
    finally:
        con.close()
    report["mode"] = "verify"
    report["json"] = str(json_path)
    report["db"] = str(db_path)
    return report


def format_report(report: dict[str, Any], *, verbose: bool = False) -> str:
    """Render a verification report as the block the analyst reads in a console."""
    counts, ids = report["counts"], report["ids"]
    spectra, dates = report["spectra"], report["dates"]
    lines = [
        f"=== Unknown-Register {report.get('mode', 'verify')} ===",
        f"JSON        : {report.get('json', '')}",
        f"Ziel-DB     : {report.get('db', '')}",
        f"Schema      : v{report.get('schema_version')}   "
        f"integrity_check: {report.get('integrity_check', 'n/a')}",
        "",
        f"Einträge    : {counts['db_entries']} DB / {counts['json_entries']} JSON"
        f"   (verglichen: {counts['entries_compared']})",
        f"Sichtungen  : {counts['db_sightings']} DB / "
        f"{counts['json_sightings_unique']} eindeutig / "
        f"{counts['json_sightings']} im JSON",
        f"entry_id    : {ids['min']}..{ids['max']}   next_id={ids['next_id']}"
        + (f"   doppelt: {ids['duplicate_numbers']}" if ids["duplicate_numbers"] else ""),
        f"Spektren    : {spectra['entries_with_spectrum']} Einträge mit, "
        f"{spectra['entries_without_spectrum']} ohne "
        f"({spectra['spectra_rows']} Spektren, {spectra['tic_rows']} TIC-Slices)",
        f"Dedup-Kolli.: {len(report['dedup_collisions'])} "
        f"(Schlüssel = stärkste {ENTRY_KEY_IONS} Massen)",
        f"Datumsfehler: {len(dates['unparseable_entry_dates'])} Eintragsfelder, "
        f"{len(dates['unparseable_sighting_dates'])} Sichtungen unlesbar; "
        f"{len(dates['iso_corrected'])} durch ISO-Spalten korrigiert",
        f"Feldabw.    : {len(report['field_mismatches'])}",
        "PRAGMA      : " + ", ".join(f"{k}={v}" for k, v in report["pragmas"].items()),
    ]
    if report.get("mode") == "dry-run":
        lines.append(f"Ziel unberührt: {report.get('destination_untouched')} "
                     f"(existiert: {report.get('destination_exists')}, "
                     f"Reste: {report.get('leftovers') or 'keine'})")
    if report.get("source_sha256"):
        lines.append(f"JSON-SHA256 : {'passt' if report['source_sha256']['match'] else 'weicht ab'}")

    for collision in report["dedup_collisions"]:
        lines.append(f"  ! Dedup-Schlüssel {collision['identity_key']}: "
                     + ", ".join(collision["entries"]))
    for item in dates["iso_corrected"]:
        lines.append(f"  ~ {item['entry']}: JSON {item['json_first_seen']} .. "
                     f"{item['json_last_seen']}  ->  ISO {item['iso_first_seen']} .. "
                     f"{item['iso_last_seen']}")
    for item in dates["unparseable_entry_dates"] + dates["unparseable_sighting_dates"]:
        # Which field is unreadable matters: first_seen/last_seen sit on the
        # entry, every other one belongs to a named sighting.
        where = item.get("field") or f"Sichtung {item.get('sample') or '?'}"
        lines.append(f"  ? unlesbares Datum {item.get('entry')} {where}: "
                     f"{item.get('value')!r}")
    if report["dropped_fields"]["unexpected"]:
        lines.append("  ! verworfene Felder: "
                     + ", ".join(report["dropped_fields"]["unexpected"]))

    shown = report["field_mismatches"] if verbose else report["field_mismatches"][:20]
    for item in shown:
        lines.append(f"  x {item['entry']} {item['scope']}.{item['field']}: "
                     f"DB={item['db']!r} JSON={item['json']!r}")
    if len(report["field_mismatches"]) > len(shown):
        lines.append(f"  x ... {len(report['field_mismatches']) - len(shown)} weitere")

    if report["warnings"]:
        lines.append("")
        lines.extend(f"  WARNUNG: {w}" for w in report["warnings"])
    if report["problems"]:
        lines.append("")
        lines.extend(f"  FEHLER : {p}" for p in report["problems"])
    lines.append("")
    lines.append("ERGEBNIS: " + ("BESTANDEN" if report["ok"] else "NICHT BESTANDEN"))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Compatibility shim
# --------------------------------------------------------------------------

def as_register_dict(con: sqlite3.Connection) -> dict[str, Any]:
    """Materialise the nested dict the existing code expects.

    The Excel export, the search syntax and the register tab all consume the
    JSON shape. Serving it from SQL means none of them has to change on day one.
    """
    unknowns: dict[str, Any] = {}
    by_id: dict[int, dict[str, Any]] = {}

    for row in con.execute("SELECT * FROM entries ORDER BY entry_id"):
        entry = {
            "id": row["unknown_id"], "label": row["label"],
            "canonical_mz": _mz_list(row["canonical_mz"]),
            "ranked_mz": _mz_list(row["ranked_mz"]),
            "ranked_variants": {}, "base_peak": row["base_peak"],
            "rank_unknown": bool(row["rank_unknown"]), "status": row["status"],
            "assigned_name": row["assigned_name"], "assigned_cas": row["assigned_cas"],
            "note": row["note"], "linked_to": row["linked_to"],
            "cluster": row["cluster"], "sightings": [],
            "samples": [], "simulants": [], "materials": [], "class_hint": [],
            "n_sightings": row["n_sightings"], "n_samples": row["n_samples"],
            "rt_mean": row["rt_mean"], "rt_min": row["rt_min"],
            "rt_max": row["rt_max"], "rt_sd": row["rt_sd"],
            "conc_min": row["conc_min"], "conc_median": row["conc_median"],
            "conc_max": row["conc_max"], "conc_max_sample": row["conc_max_sample"],
            "area_pct_max": row["area_pct_max"], "ttc_flag": row["ttc_flag"],
            "homologue_series": row["homologue_series"],
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        }
        unknowns[row["mz_key"]] = entry
        by_id[row["entry_id"]] = entry

    for table, column, key in (
        ("entry_samples", "sample", "samples"),
        ("entry_simulants", "simulant", "simulants"),
        ("entry_materials", "material", "materials"),
    ):
        for row in con.execute(f"SELECT entry_id, {column} v FROM {table} ORDER BY v"):
            target = by_id.get(row["entry_id"])
            if target is not None:
                target[key].append(row["v"])

    for row in con.execute(
            "SELECT entry_id, hint FROM entry_class_hints ORDER BY ord, hint"):
        target = by_id.get(row["entry_id"])
        if target is not None:
            target["class_hint"].append(row["hint"])

    for row in con.execute("SELECT entry_id, variant, votes FROM entry_mz_variants"):
        target = by_id.get(row["entry_id"])
        if target is not None:
            target["ranked_variants"][row["variant"]] = row["votes"]

    for row in con.execute("SELECT * FROM sightings ORDER BY sighting_id"):
        target = by_id.get(row["entry_id"])
        if target is None:
            continue
        target["sightings"].append({
            "sample": row["sample"], "sample_name": row["sample_name"],
            "report_type": row["report_type"], "simulant": row["simulant"],
            "temperature": row["temperature"], "duration": row["duration"],
            "migration_cell": row["migration_cell"], "analyst": row["analyst"],
            "source_file": row["source_file"], "output_file": row["output_file"],
            "date": row["date_text"], "script_version": row["script_version"],
            "ranked_mz": _mz_list(row["ranked_mz"]), "name_raw": row["name_raw"],
            "rt": row["rt"], "conc_kg": row["conc_kg"], "conc_area": row["conc_area"],
            "area_pct": row["area_pct"], "match": row["match_pct"], "db": row["db"],
        })

    if con.execute("SELECT 1 FROM sqlite_master WHERE name='ei_investigations'").fetchone():
        for row in con.execute("SELECT * FROM ei_investigations ORDER BY created_at"):
            target = by_id.get(row['entry_id'])
            if target is not None:
                target.setdefault('ei_investigations', []).append(dict(row))

    return {
        "schema_version": int(meta_get(con, "schema_version", SCHEMA_VERSION)),
        "updated_at": meta_get(con, "updated_at", ""),
        "next_id": int(meta_get(con, "next_id", len(unknowns) + 1)),
        "unknowns": unknowns,
    }


# --------------------------------------------------------------------------
# Writing the legacy dict back
#
# The report path and the register tab still work on the nested dict shape.
# Rather than rewrite them, the round trip is closed here: read with
# ``as_register_dict``, edit in memory, write back with ``save_register_dict``
# inside one ``writing()`` transaction per report run.
# --------------------------------------------------------------------------

#: Entries and sightings are upserted, never deleted. Deleting an entry would
#: cascade its measured spectra and TIC slices away, and the callers of this
#: function -- the report run and the register tab -- never remove anything.
_ENTRY_SCALARS: tuple[tuple[str, str], ...] = (
    ("label", "label"), ("base_peak", "base_peak"), ("status", "status"),
    ("assigned_name", "assigned_name"), ("assigned_cas", "assigned_cas"),
    ("note", "note"), ("cluster", "cluster"),
    ("n_sightings", "n_sightings"), ("n_samples", "n_samples"),
    ("rt_mean", "rt_mean"), ("rt_min", "rt_min"), ("rt_max", "rt_max"),
    ("rt_sd", "rt_sd"), ("conc_min", "conc_min"), ("conc_median", "conc_median"),
    ("conc_max", "conc_max"), ("conc_max_sample", "conc_max_sample"),
    ("area_pct_max", "area_pct_max"), ("ttc_flag", "ttc_flag"),
    ("homologue_series", "homologue_series"),
    ("first_seen", "first_seen"), ("last_seen", "last_seen"),
)

#: Text columns the schema declares NOT NULL while the JSON shape allows None.
_ENTRY_TEXT_COLUMNS = frozenset({
    "label", "status", "assigned_name", "assigned_cas", "note", "cluster",
    "conc_max_sample", "ttc_flag", "homologue_series", "first_seen", "last_seen",
})

#: ``(sqlite column, json key)`` of a sighting, without the derived keys.
_SIGHTING_WRITE_FIELDS: tuple[tuple[str, str], ...] = (
    ("sample", "sample"), ("sample_name", "sample_name"),
    ("report_type", "report_type"), ("simulant", "simulant"),
    ("temperature", "temperature"), ("duration", "duration"),
    ("migration_cell", "migration_cell"), ("analyst", "analyst"),
    ("source_file", "source_file"), ("output_file", "output_file"),
    ("date_text", "date"), ("script_version", "script_version"),
    ("name_raw", "name_raw"), ("db", "db"),
    ("rt", "rt"), ("conc_kg", "conc_kg"), ("conc_area", "conc_area"),
    ("area_pct", "area_pct"), ("match_pct", "match"),
)
_SIGHTING_NUMERIC_COLUMNS = frozenset({
    "rt", "conc_kg", "conc_area", "area_pct", "match_pct"})


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _rt_key(value: Any) -> Optional[float]:
    return (round(float(value), 4)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None)


def save_register_dict(con: sqlite3.Connection,
                       register: dict[str, Any]) -> dict[str, int]:
    """Write a legacy register dict back into an open SQLite register.

    Must be called inside :class:`writing`: the caller owns the transaction, so
    one report run is one ``BEGIN IMMEDIATE``. That single transaction is what
    replaces the old ``unknown_register.lock`` file -- running both would
    deadlock on a share.
    """
    unknowns: dict[str, Any] = register.get("unknowns") or {}
    written = {"entries": 0, "sightings": 0}

    for fallback, (mz_key, entry) in enumerate(sorted(unknowns.items()), 1):
        eid = _entry_number(entry.get("id"), fallback)
        canonical = _mz_list(entry.get("canonical_mz") or mz_key)
        ranked = _mz_list(entry.get("ranked_mz"))
        first_iso, last_iso = _seen_iso(entry)
        blob = " ".join(_as_text(entry.get(k)) for k in
                        ("id", "label", "assigned_name", "assigned_cas",
                         "note", "cluster")).casefold()

        columns = ["unknown_id", "mz_key", "identity_key", "canonical_mz",
                   "ranked_mz", "rank_unknown", "linked_to", "first_seen_iso",
                   "last_seen_iso", "search_blob"]
        values: list[Any] = [
            entry.get("id") or f"UNK-{eid:04d}", mz_key,
            entry_identity_key(entry), _join_mz(canonical), _join_mz(ranked),
            1 if entry.get("rank_unknown") else 0, entry.get("linked_to"),
            first_iso, last_iso, blob,
        ]
        for column, key in _ENTRY_SCALARS:
            value = entry.get(key)
            if column in _ENTRY_TEXT_COLUMNS:
                value = _as_text(value)
            elif column in ("n_sightings", "n_samples"):
                value = int(value or 0)
            columns.append(column)
            values.append(value)

        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{c} = excluded.{c}" for c in columns
                                if c != "mz_key")
        con.execute(
            f"INSERT INTO entries (entry_id, {', '.join(columns)}) "
            f"VALUES (?, {placeholders}) "
            f"ON CONFLICT(entry_id) DO UPDATE SET {assignments}",
            [eid] + values,
        )
        written["entries"] += 1

        rank_of = {mz: i + 1 for i, mz in enumerate(ranked)}
        con.execute("DELETE FROM entry_ions WHERE entry_id = ?", (eid,))
        con.executemany(
            "INSERT OR IGNORE INTO entry_ions(entry_id, mz, rank) VALUES (?,?,?)",
            [(eid, mz, rank_of.get(mz))
             for mz in sorted(set(canonical) | set(ranked))])
        for table, column, items in (
            ("entry_samples", "sample", entry.get("samples")),
            ("entry_simulants", "simulant", entry.get("simulants")),
            ("entry_materials", "material", entry.get("materials")),
        ):
            con.execute(f"DELETE FROM {table} WHERE entry_id = ?", (eid,))
            con.executemany(
                f"INSERT OR IGNORE INTO {table}(entry_id, {column}) VALUES (?,?)",
                [(eid, str(v)) for v in (items or []) if str(v).strip()])
        con.execute("DELETE FROM entry_class_hints WHERE entry_id = ?", (eid,))
        con.executemany(
            "INSERT OR IGNORE INTO entry_class_hints(entry_id, hint, ord) "
            "VALUES (?,?,?)",
            [(eid, str(h), i)
             for i, h in enumerate(entry.get("class_hint") or [])])
        con.execute("DELETE FROM entry_mz_variants WHERE entry_id = ?", (eid,))
        con.executemany(
            "INSERT OR IGNORE INTO entry_mz_variants(entry_id, variant, votes) "
            "VALUES (?,?,?)",
            [(eid, str(k), int(v))
             for k, v in (entry.get("ranked_variants") or {}).items()])

        for sighting in entry.get("sightings") or []:
            written["sightings"] += _upsert_sighting(con, eid, sighting)

        for research in entry.get('ei_investigations') or []:
            spectrum_id = research.get('spectrum_id')
            if not con.execute('SELECT 1 FROM spectra WHERE spectrum_id=? AND entry_id=?', (spectrum_id, eid)).fetchone():
                spectrum_id = None  # JSON restores still retain the exact search input in payload_json.
            con.execute('INSERT OR IGNORE INTO ei_investigations VALUES(?,?,?,?,?,?,?,?,?,?)',
                (research['investigation_id'], eid, spectrum_id, research['created_at'],
                 research['decision'], research['candidate_name'], research['candidate_cas'],
                 research['note'], research['next_steps'], research['payload_json']))

    meta_set(con, "schema_version", SCHEMA_VERSION)
    meta_set(con, "next_id", int(register.get("next_id", len(unknowns) + 1)))
    meta_set(con, "updated_at",
             datetime.now().astimezone().isoformat(timespec="seconds"))
    return written


def _upsert_sighting(con: sqlite3.Connection, eid: int,
                     s: dict[str, Any]) -> int:
    """Insert or update one sighting; returns 1 only when a row was added.

    SQLite's UNIQUE index treats NULL ``rt_key`` values as distinct, so a plain
    ``ON CONFLICT`` would append a fresh row for every sighting without a
    retention time. The row is therefore looked up with ``IS`` first -- exactly
    the rule ``_sighting_key`` describes, which is what makes re-running the
    same report update its sightings instead of appending duplicates.
    """
    sample_key = _as_text(s.get("sample")).casefold()
    type_key = _as_text(s.get("report_type")).casefold()
    rt_key = _rt_key(s.get("rt"))

    columns = [column for column, _ in _SIGHTING_WRITE_FIELDS]
    values = [s.get(key) if column in _SIGHTING_NUMERIC_COLUMNS
              else _as_text(s.get(key))
              for column, key in _SIGHTING_WRITE_FIELDS]
    columns += ["sample_key", "report_type_key", "rt_key", "date_iso", "ranked_mz"]
    values += [sample_key, type_key, rt_key, _iso_date(s.get("date")),
               _join_mz(_mz_list(s.get("ranked_mz")))]

    found = con.execute(
        "SELECT sighting_id FROM sightings WHERE entry_id = ? AND sample_key = ? "
        "AND report_type_key = ? AND rt_key IS ?",
        (eid, sample_key, type_key, rt_key)).fetchone()
    if found is None:
        con.execute(
            f"INSERT INTO sightings (entry_id, {', '.join(columns)}) "
            f"VALUES ({', '.join('?' * (len(columns) + 1))})",
            [eid] + values)
        return 1
    con.execute(
        "UPDATE sightings SET " + ", ".join(f"{c} = ?" for c in columns)
        + " WHERE sighting_id = ?", values + [found["sighting_id"]])
    return 0


def dump_register_json(con: sqlite3.Connection, path: Path) -> Path:
    """Write the register out as JSON in the legacy shape.

    The JSON is no longer the system of record, but it stays the format
    ``--verify`` reads and the one a rollback restores from, so producing a
    fresh backup has to remain possible.
    """
    path = Path(path)
    payload = as_register_dict(con)
    payload["schema_version"] = 1          # the JSON schema, not the SQL one
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------
# Migration gate
#
# After the cutover the JSON stops receiving writes. Silently starting an empty
# SQLite register next to a populated JSON would orphan the whole team knowledge
# base, and nobody would notice until the register tab came up empty. The gate
# makes the ordering of the cutover enforceable in code instead of by
# discipline.
# --------------------------------------------------------------------------

class MigrationRequired(RuntimeError):
    """The JSON register holds entries that have not been migrated yet."""


def count_db_entries(db_path: Path) -> Optional[int]:
    """Entries in the SQLite register; ``None`` when there is no usable file."""
    db_path = Path(db_path)
    try:
        if not db_path.is_file() or db_path.stat().st_size == 0:
            return None
        con = connect(db_path, create=False)
    except (sqlite3.DatabaseError, OSError):
        return None
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "entries" not in tables:
            return 0
        return int(con.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()


def count_json_entries(json_path: Path) -> Optional[int]:
    """Entries in the JSON register; ``None`` when it is absent or unreadable."""
    json_path = Path(json_path)
    if not json_path.is_file():
        return None
    try:
        return len(load_json_register(json_path).get("unknowns") or {})
    except (OSError, ValueError, RuntimeError):
        return None


def migration_state(json_path: Path, db_path: Path) -> dict[str, Any]:
    """How far the JSON -> SQLite cutover has got in one register directory.

    ``state`` is ``ok`` (SQLite carries the register), ``fresh`` (there is
    nothing to migrate) or ``unmigrated`` (the JSON has entries and SQLite has
    none) -- the one case that must stop a write.
    """
    json_entries = count_json_entries(json_path)
    db_entries = count_db_entries(db_path)
    if json_entries:
        state = "ok" if db_entries else "unmigrated"
    else:
        state = "ok" if db_entries else "fresh"
    return {
        "state": state, "json": str(json_path), "db": str(db_path),
        "json_entries": json_entries, "db_entries": db_entries,
        "json_exists": Path(json_path).is_file(),
        "db_exists": Path(db_path).is_file(),
    }


def migration_command(json_path: Any, db_path: Any) -> str:
    """The command line the analyst runs, ready to copy out of a dialog."""
    return f'python "{Path(__file__).name}" --migrate "{json_path}" "{db_path}"'


def dry_run_command(json_path: Any) -> str:
    return f'python "{Path(__file__).name}" --dry-run "{json_path}"'


def migration_required_message(state: dict[str, Any]) -> str:
    """The German text shown when the gate stops a write."""
    return "\n".join([
        "Das Unknown-Register wurde noch nicht nach SQLite migriert.",
        "",
        f"JSON-Register   : {state['json']}",
        f"                  {state['json_entries']} Eintrag/Einträge",
        f"SQLite-Register : {state['db']}",
        "                  leer oder nicht vorhanden",
        "",
        "Seit der Umstellung schreibt das Programm ausschließlich in die "
        "SQLite-Datei. Würde jetzt ein leeres Register angelegt, blieben die "
        "bisherigen Einträge unbeachtet im JSON zurück.",
        "",
        "Bitte zuerst den Probelauf ansehen und dann migrieren:",
        f"    {dry_run_command(state['json'])}",
        f"    {migration_command(state['json'], state['db'])}",
    ])


def require_migrated(json_path: Path, db_path: Path) -> dict[str, Any]:
    """Raise :class:`MigrationRequired` when the JSON has not been migrated."""
    state = migration_state(json_path, db_path)
    if state["state"] == "unmigrated":
        raise MigrationRequired(migration_required_message(state))
    return state


def identity_collisions(register: dict[str, Any]) -> list[dict[str, Any]]:
    """Entries of a register dict that answer to one identity key.

    A collision is reported, never resolved: new sightings land on the first
    entry and the register tab says so. Merging two entries automatically would
    merge two substances the analyst never agreed to merge.
    """
    by_key: dict[str, list[tuple[str, str]]] = {}
    for mz_key, entry in (register.get("unknowns") or {}).items():
        key = entry_identity_key(entry)
        if key:
            by_key.setdefault(key, []).append((entry.get("id") or mz_key, mz_key))
    out: list[dict[str, Any]] = []
    for key, members in sorted(by_key.items()):
        if len(members) > 1:
            members.sort()
            out.append({"identity_key": key,
                        "ids": [i for i, _ in members],
                        "keys": [k for _, k in members],
                        "winner": members[0][0]})
    return out


def entries_with_spectra(con: sqlite3.Connection) -> set[int]:
    """``entry_id`` of every entry that carries at least one measured spectrum."""
    return {int(r[0]) for r in con.execute("SELECT DISTINCT entry_id FROM spectra")}



# --------------------------------------------------------------------------
# Writing spectra
# --------------------------------------------------------------------------

def attach_spectrum(con: sqlite3.Connection, entry_id: int,
                    sighting_id: Optional[int],
                    spectrum: Iterable[tuple[float, float]], *,
                    source_path: str = "", apex_scan: Optional[int] = None,
                    bg_scan: Optional[int] = None, bounds_rule: str = "",
                    rt: Optional[float] = None, kind: str = "measured") -> int:
    """Store one measured spectrum and its significant-ion index."""
    packed = encode_spectrum(spectrum)
    cur = con.execute(
        """INSERT INTO spectra (
            sighting_id, entry_id, kind, source_path, apex_scan, bg_scan,
            bounds_rule, rt, mz_unit, normalised, n_ions, base_peak_mz,
            base_peak_int, total_intensity, mz_blob, intensity_blob,
            checksum, created_at)
           VALUES (?,?,?,?,?,?,?,?,1.0,0,?,?,?,?,?,?,?,?)""",
        (sighting_id, entry_id, kind, source_path, apex_scan, bg_scan,
         bounds_rule, rt, packed["n_ions"], packed["base_peak_mz"],
         packed["base_peak_int"], packed["total_intensity"],
         packed["mz_blob"], packed["intensity_blob"], packed["checksum"],
         datetime.now().isoformat(timespec="seconds")),
    )
    spectrum_id = int(cur.lastrowid)
    con.executemany(
        "INSERT OR REPLACE INTO spectrum_top_ions(spectrum_id, mz, rel_int, rank) "
        "VALUES (?,?,?,?)",
        [(spectrum_id, mz, rel, rank) for mz, rel, rank in significant_ions(spectrum)],
    )
    return spectrum_id


def attach_tic_slice(con: sqlite3.Connection, sighting_id: Optional[int],
                     spectrum_id: Optional[int], rts: Iterable[float],
                     intensities: Iterable[float], *,
                     apex_rt: Optional[float] = None,
                     scan_start: Optional[int] = None,
                     scan_end: Optional[int] = None) -> int:
    """Store the chromatogram around a peak so it can be redrawn without the .D."""
    rts, intensities = list(rts), list(intensities)
    packed = encode_tic(rts, intensities)
    apex_index = None
    if apex_rt is not None and rts:
        apex_index = min(range(len(rts)), key=lambda i: abs(rts[i] - apex_rt))
    cur = con.execute(
        """INSERT INTO tic_slices (
            sighting_id, spectrum_id, rt_start, rt_end, apex_rt, apex_index,
            scan_start, scan_end, n_points, rt_blob, intensity_blob, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sighting_id, spectrum_id, rts[0] if rts else 0.0, rts[-1] if rts else 0.0,
         apex_rt, apex_index, scan_start, scan_end, packed["n_points"],
         packed["rt_blob"], packed["intensity_blob"],
         datetime.now().isoformat(timespec="seconds")),
    )
    return int(cur.lastrowid)


def load_spectrum(con: sqlite3.Connection,
                  spectrum_id: int) -> list[tuple[float, int]]:
    row = con.execute(
        "SELECT mz_blob, intensity_blob FROM spectra WHERE spectrum_id = ?",
        (spectrum_id,)).fetchone()
    if row is None:
        return []
    return decode_spectrum(row["mz_blob"], row["intensity_blob"])


def spectrum_for_entry(con: sqlite3.Connection,
                       entry_id: int) -> Optional[list[tuple[float, int]]]:
    """Most recent measured spectrum of an entry, or None for a legacy entry.

    Entries migrated from the JSON register have no spectrum. Callers fall back
    to the ranked-list scoring rather than failing.
    """
    row = con.execute(
        "SELECT spectrum_id FROM spectra WHERE entry_id = ? "
        "ORDER BY spectrum_id DESC LIMIT 1", (entry_id,)).fetchone()
    return load_spectrum(con, row["spectrum_id"]) if row else None


#: How the *best* spectrum of an entry is picked, everywhere it is picked.
#: Most ions first -- the richest description of the substance; the total
#: intensity and finally the youngest spectrum break the tie, so the choice is
#: deterministic and does not depend on the row order SQLite happens to return.
_BEST_SPECTRUM_ORDER = "n_ions DESC, total_intensity DESC, spectrum_id DESC"


def best_spectrum_ids(con: sqlite3.Connection) -> dict[int, int]:
    """``{entry_id: spectrum_id}`` of the ion-richest spectrum of each entry.

    One query for the whole register, because the MSP library needs the choice
    for every entry at once: a round trip per entry over a file share is the
    difference between one second and thirty.
    """
    rows = con.execute(
        f"""SELECT p.entry_id, p.spectrum_id FROM spectra p
             WHERE p.spectrum_id = (SELECT q.spectrum_id FROM spectra q
                                     WHERE q.entry_id = p.entry_id
                                     ORDER BY {_BEST_SPECTRUM_ORDER} LIMIT 1)""",
    ).fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


def best_spectrum_for_entry(con: sqlite3.Connection,
                            entry_id: int) -> Optional[list[tuple[float, int]]]:
    """The ion-richest measured spectrum of one entry, or None if it has none.

    The counterpart of :func:`spectrum_for_entry`, which answers with the most
    recent one. Both stay: the workspace shows what was filed last, the register
    describes an entry by the best spectrum anyone ever filed for it.
    """
    row = con.execute(
        f"SELECT spectrum_id FROM spectra WHERE entry_id = ? "
        f"ORDER BY {_BEST_SPECTRUM_ORDER} LIMIT 1", (int(entry_id),)).fetchone()
    return load_spectrum(con, row["spectrum_id"]) if row else None


# --------------------------------------------------------------------------
# Reading the register back (the register window)
# --------------------------------------------------------------------------
#
# Everything below is read-only apart from :func:`update_entry` and
# :func:`delete_entry`. It exists because the register is no longer only
# written by the export: since the spectrum panel can file a substance by hand,
# the analyst has to be able to look at what was filed -- the measured spectrum
# with its real m/z values and intensities, not a ranked mass list.

#: Fields :func:`update_entry` accepts. A whitelist rather than ``**kwargs``
#: straight into the UPDATE, so a typo in a caller cannot silently write a
#: column that is derived (``n_sightings``) or is the entry's identity
#: (``mz_key``, ``identity_key``).
EDITABLE_ENTRY_FIELDS = frozenset({
    "label", "assigned_name", "assigned_cas", "status", "note",
    "cluster", "homologue_series", "ttc_flag",
})

#: ``status`` values the register window offers. The register itself takes any
#: text -- these are the four words the laboratory actually uses.
ENTRY_STATUSES = ("offen", "in Arbeit", "identifiziert", "verworfen")

_BROWSE_SQL = """
SELECT e.entry_id, e.unknown_id, e.mz_key, e.identity_key, e.label,
       e.canonical_mz, e.ranked_mz, e.base_peak, e.status, e.note,
       e.assigned_name, e.assigned_cas, e.cluster, e.homologue_series,
       e.first_seen, e.last_seen, e.first_seen_iso, e.last_seen_iso,
       (SELECT COUNT(*) FROM sightings s WHERE s.entry_id = e.entry_id)
           AS n_sightings,
       (SELECT COUNT(*) FROM spectra p WHERE p.entry_id = e.entry_id)
           AS n_spectra,
       (SELECT AVG(s.rt) FROM sightings s
         WHERE s.entry_id = e.entry_id AND s.rt IS NOT NULL) AS rt_mean
  FROM entries e
"""


def browse_entries(con: sqlite3.Connection, *, search: str = "",
                   with_spectra_only: bool = False,
                   limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Every entry, in the shape the register window's list needs.

    ``rt_mean`` and the two counts are computed from ``sightings`` and
    ``spectra`` rather than read from the denormalised columns of ``entries``:
    a hand-filed sighting updates ``n_sightings`` but nothing maintains
    ``rt_mean``, and a list that disagrees with the detail view underneath it
    is worse than one that costs a sub-select.
    """
    sql, params = _BROWSE_SQL, []
    where: list[str] = []
    text = str(search or "").strip().casefold()
    if text:
        where.append(
            "(LOWER(e.unknown_id) LIKE ? OR LOWER(e.label) LIKE ? "
            "OR LOWER(e.assigned_name) LIKE ? OR LOWER(e.assigned_cas) LIKE ? "
            "OR LOWER(e.note) LIKE ? OR e.ranked_mz LIKE ? OR e.mz_key LIKE ?)")
        params += [f"%{text}%"] * 7
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='ei_investigations'").fetchone():
            where[-1] = '(' + where[-1] + " OR EXISTS (SELECT 1 FROM ei_investigations i WHERE i.entry_id=e.entry_id " \
                "AND (LOWER(i.candidate_name) LIKE ? OR LOWER(i.candidate_cas) LIKE ? OR LOWER(i.note) LIKE ? OR LOWER(i.next_steps) LIKE ?)))"
            params += [f"%{text}%"] * 4
    if with_spectra_only:
        where.append("EXISTS (SELECT 1 FROM spectra p WHERE p.entry_id = e.entry_id)")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.entry_id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in con.execute(sql, params)]


def entry_row(con: sqlite3.Connection, entry_id: int) -> Optional[dict[str, Any]]:
    """One browse row, or ``None`` when the entry was deleted meanwhile."""
    row = con.execute(_BROWSE_SQL + " WHERE e.entry_id = ?",
                      (int(entry_id),)).fetchone()
    return dict(row) if row is not None else None


def entry_spectra(con: sqlite3.Connection, entry_id: int) -> list[dict[str, Any]]:
    """Every stored spectrum of one entry, newest first, with its sighting.

    The blobs are deliberately not decoded here: an entry with a hundred
    sightings would decode a hundred spectra to fill a list in which the
    analyst then looks at one. :func:`load_spectrum` does that, on demand.
    """
    rows = con.execute(
        """SELECT p.spectrum_id, p.sighting_id, p.entry_id, p.kind,
                  p.source_path, p.apex_scan, p.bg_scan, p.bounds_rule,
                  p.rt, p.n_ions, p.base_peak_mz, p.base_peak_int,
                  p.total_intensity, p.checksum, p.created_at,
                  s.sample, s.sample_name, s.report_type, s.date_text,
                  s.conc_kg, s.area_pct, s.match_pct, s.name_raw, s.analyst,
                  s.simulant,
                  (SELECT t.tic_id FROM tic_slices t
                    WHERE t.spectrum_id = p.spectrum_id
                    ORDER BY t.tic_id DESC LIMIT 1) AS tic_id
             FROM spectra p
             LEFT JOIN sightings s ON s.sighting_id = p.sighting_id
            WHERE p.entry_id = ?
            ORDER BY p.spectrum_id DESC""", (int(entry_id),)).fetchall()
    return [dict(row) for row in rows]


def update_entry(con: sqlite3.Connection, entry_id: int, **fields: Any) -> int:
    """Write the analyst's own columns of one entry. Returns rows changed.

    ``search_blob`` follows whatever was written, so the register window's
    filter finds an entry under the name that was just given to it.
    """
    sets = {k: ("" if v is None else str(v))
            for k, v in fields.items() if k in EDITABLE_ENTRY_FIELDS}
    if not sets:
        return 0
    assignment = ", ".join(f"{k} = ?" for k in sets)
    with writing(con):
        con.execute(f"UPDATE entries SET {assignment} WHERE entry_id = ?",
                    [*sets.values(), int(entry_id)])
        row = con.execute(
            "SELECT unknown_id, label, assigned_name, assigned_cas, note "
            "FROM entries WHERE entry_id = ?", (int(entry_id),)).fetchone()
        if row is not None:
            con.execute(
                "UPDATE entries SET search_blob = ? WHERE entry_id = ?",
                (" ".join(str(row[k] or "") for k in row.keys()).casefold(),
                 int(entry_id)))
        meta_set(con, "updated_at", datetime.now().isoformat(timespec="seconds"))
    return 1


def delete_entry(con: sqlite3.Connection, entry_id: int) -> int:
    """Remove one entry with its sightings, spectra and chromatogram slices.

    ``PRAGMA foreign_keys = ON`` is set by :func:`connect`, so the cascades of
    the schema do the rest. Called only after an explicit confirmation.
    """
    with writing(con):
        cur = con.execute("DELETE FROM entries WHERE entry_id = ?",
                          (int(entry_id),))
        meta_set(con, "updated_at", datetime.now().isoformat(timespec="seconds"))
    return int(cur.rowcount or 0)


def delete_spectrum(con: sqlite3.Connection, spectrum_id: int) -> int:
    """Remove one stored spectrum. Its entry and its sighting stay."""
    with writing(con):
        cur = con.execute("DELETE FROM spectra WHERE spectrum_id = ?",
                          (int(spectrum_id),))
        meta_set(con, "updated_at", datetime.now().isoformat(timespec="seconds"))
    return int(cur.rowcount or 0)


def register_counts(con: sqlite3.Connection) -> dict[str, int]:
    """``entries`` / ``sightings`` / ``spectra`` row counts, for the status line."""
    def count(table: str) -> int:
        try:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:            # a register older than the table
            return 0
    return {"entries": count("entries"), "sightings": count("sightings"),
            "spectra": count("spectra"), "tics": count("tic_slices")}


def default_db_path() -> Path:
    """Where the register lives, resolved the way the main script resolves it.

    Delegates to ``gc_seen.default_path`` -- it already carries the
    ``unknown_register_dir()`` lookup and the ``~/.nias_gc`` fallback, and two
    copies of that resolution would be two places for the register to move to.
    """
    try:
        import gc_seen

        return Path(gc_seen.default_path())
    except Exception:
        from nias_paths import DATA
        return DATA / DB_FILENAME


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _cli(argv: Optional[list[str]] = None) -> int:
    """``--dry-run`` rehearses a migration, ``--migrate`` runs it, ``--verify``
    audits a finished one, ``--status`` says which of the two files leads.

    ``--dry-run`` and ``--verify`` print the same report block and exit non-zero
    when it does not pass, so the analyst can rehearse against the production
    register without writing any code and without risking the file. ``--verify``
    keeps working against the JSON backup after the cutover, which is how a
    suspicion is checked against the pre-cutover state.
    """
    parser = argparse.ArgumentParser(
        prog="gc_register",
        description="Unknown-Register: Migration proben, ausfuehren und pruefen.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", metavar="REGISTER.JSON",
                      help="Migration proben, ohne irgendetwas zu schreiben")
    mode.add_argument("--migrate", nargs=2, metavar=("REGISTER.JSON", "REGISTER.DB"),
                      help="Migration ausfuehren (legt vorher eine JSON-Sicherung an)")
    mode.add_argument("--verify", nargs=2, metavar=("REGISTER.JSON", "REGISTER.DB"),
                      help="Eine bereits migrierte Datenbank gegen ihr JSON pruefen")
    mode.add_argument("--status", nargs=2, metavar=("REGISTER.JSON", "REGISTER.DB"),
                      help="Zeigen, ob die Migration noch aussteht")
    mode.add_argument("--json-backup", nargs=2, metavar=("REGISTER.DB", "BACKUP.JSON"),
                      help="Das SQLite-Register als JSON-Sicherung schreiben")
    parser.add_argument("--db", metavar="REGISTER.DB",
                        help="Ziel-DB des Probelaufs (Vorgabe: %s neben dem JSON)"
                             % DB_FILENAME)
    parser.add_argument("--force", action="store_true",
                        help="--migrate auch ausfuehren, wenn die Ziel-DB schon "
                             "Eintraege hat (ueberschreibt sie vollstaendig)")
    parser.add_argument("--verbose", action="store_true",
                        help="Alle Feldabweichungen zeigen, nicht nur die ersten 20")
    args = parser.parse_args(argv)
    # The report is German; a legacy console codepage must not abort the run.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        if args.status:
            state = migration_state(Path(args.status[0]), Path(args.status[1]))
            print(f"JSON   : {state['json']}  ->  {state['json_entries']} Eintraege")
            print(f"SQLite : {state['db']}  ->  {state['db_entries']} Eintraege")
            print(f"Status : {state['state']}")
            if state["state"] == "unmigrated":
                print()
                print(migration_required_message(state))
            return 1 if state["state"] == "unmigrated" else 0

        if args.json_backup:
            con = connect(Path(args.json_backup[0]), create=False)
            try:
                written = dump_register_json(con, Path(args.json_backup[1]))
            finally:
                con.close()
            print(f"JSON-Sicherung geschrieben: {written}")
            return 0

        if args.migrate:
            json_path, db_path = Path(args.migrate[0]), Path(args.migrate[1])
            # The destination is checked before anything is written: overwriting
            # a register that already holds measured spectra would throw them
            # away, and the JSON backup cannot bring them back.
            existing = count_db_entries(db_path)
            if existing and not args.force:
                print(f"Die Ziel-DB {db_path} enthaelt bereits {existing} "
                      "Eintraege. Migration abgebrochen (--force ueberschreibt "
                      "sie vollstaendig).", file=sys.stderr)
                return 2
            result = migrate_json(json_path, db_path)
            print(f"Migriert: {result['entries']} Eintraege, "
                  f"{result['sightings']} Sichtungen -> {result['db']}")
            print(f"JSON-Sicherung: {result['backup']}")
            report = verify_database(json_path, db_path)
            print()
            print(format_report(report, verbose=args.verbose))
            return 0 if report["ok"] else 1

        if args.dry_run:
            report = dry_run_migration(Path(args.dry_run),
                                       Path(args.db) if args.db else None)
        else:
            report = verify_database(Path(args.verify[0]), Path(args.verify[1]))
    except FileNotFoundError as exc:
        print(f"Datei nicht gefunden: {exc.filename}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        print(f"Register nicht lesbar: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(format_report(report, verbose=args.verbose))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli())

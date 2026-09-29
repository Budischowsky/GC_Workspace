"""The automation journal: batches, runs, jobs (one per sample) and their review, in sqlite.

Shared by the watcher (writes processing results), the job processes (nothing) and the GUI
(Report²: accept, reject, reprocess). WAL mode lets the GUI read while the watcher writes;
state changes are compare-and-set (``UPDATE ... WHERE state IN (...)``), so an analyst's
click and the watcher never overwrite each other.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from gcws.automation import store

SCHEMA = 1

# job states
WAITING = "waiting"                     # runs or blanks still being acquired
QUEUED = "queued"
PROCESSING = "processing"
CONTROL = "control"                     # Report²: the analyst must check it
ACCEPTED_AUTO = "accepted_auto"
ACCEPTED_MANUAL = "accepted_manual"
REJECTED = "rejected"
FAILED = "failed"
NOT_PROCESSED = "not_processed"         # e.g. no blank in the batch

STATE_LABELS = {WAITING: "Waiting", QUEUED: "Queued", PROCESSING: "Processing", CONTROL: "Control needed",
                ACCEPTED_AUTO: "Accepted (automatic)", ACCEPTED_MANUAL: "Accepted (analyst)",
                REJECTED: "Rejected", FAILED: "Failed", NOT_PROCESSED: "Not processed"}
ACCEPTED = (ACCEPTED_AUTO, ACCEPTED_MANUAL)
DONE = (CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL, REJECTED)          # processed (a report exists)
TRANSITIONS = {
    WAITING: {QUEUED, NOT_PROCESSED, WAITING},
    QUEUED: {PROCESSING, WAITING},
    PROCESSING: {CONTROL, ACCEPTED_AUTO, FAILED, NOT_PROCESSED, QUEUED},
    FAILED: {QUEUED, WAITING},
    NOT_PROCESSED: {QUEUED, WAITING},
    CONTROL: {ACCEPTED_MANUAL, REJECTED, QUEUED},
    ACCEPTED_AUTO: {REJECTED, QUEUED, CONTROL},
    ACCEPTED_MANUAL: {REJECTED, QUEUED, CONTROL},
    REJECTED: {QUEUED, CONTROL},
}
BATCH_KEY = "__batch__"                 # the job row of a batch report

_DDL = """
CREATE TABLE IF NOT EXISTS watcher(id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, host TEXT, user TEXT,
    state TEXT, heartbeat REAL, current_job TEXT, message TEXT, started REAL);
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, workflow_id TEXT, folder TEXT, folder_key TEXT,
    name TEXT, first_seen REAL, last_change REAL, has_log INTEGER DEFAULT 0, seq_completed INTEGER DEFAULT 0,
    state TEXT DEFAULT 'open', plan_json TEXT, UNIQUE(workflow_id, folder_key));
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, batch_id INTEGER, path TEXT, stem TEXT, role TEXT,
    fingerprint TEXT, stable_count INTEGER DEFAULT 0, first_seen REAL, last_change REAL, state TEXT,
    marker INTEGER DEFAULT 0, baseline INTEGER DEFAULT 0, UNIQUE(batch_id, stem));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, workflow_id TEXT, method_node TEXT, batch_id INTEGER,
    group_key TEXT, group_name TEXT, revision INTEGER DEFAULT 1, members_json TEXT, blanks_json TEXT,
    input_fp TEXT, state TEXT, reason TEXT, attempts INTEGER DEFAULT 0, not_before REAL DEFAULT 0,
    created REAL, queued_at REAL, started REAL, finished REAL, pid INTEGER, job_dir TEXT, project_path TEXT,
    files_json TEXT, summary_json TEXT, evidence_json TEXT, findings_json TEXT, reviewer TEXT, comment TEXT,
    reviewed_at REAL, export_state TEXT DEFAULT 'none', export_pending INTEGER DEFAULT 0, override_json TEXT,
    mode TEXT DEFAULT 'full', UNIQUE(workflow_id, method_node, batch_id, group_key));
CREATE TABLE IF NOT EXISTS exports(id INTEGER PRIMARY KEY, job_id TEXT, revision INTEGER, folder_node TEXT,
    report_node TEXT, fmt TEXT, src TEXT, dst TEXT, ts REAL, state TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, user TEXT, workflow_id TEXT,
    batch_id INTEGER, job_id TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS watched(workflow_id TEXT, source_key TEXT, first_scan REAL,
    PRIMARY KEY(workflow_id, source_key));
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
"""

_JSON = ("members", "blanks", "files", "summary", "evidence", "findings", "override", "plan")


def _user() -> str:
    from gcws.core.audit import current_user
    return current_user()


def _network(path: Path) -> bool:
    p = str(path)
    if p.startswith("\\\\"):
        return True
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(p))[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4          # DRIVE_REMOTE
    except Exception:  # noqa: BLE001
        return False


@dataclass
class Job:
    """One row of ``jobs`` with its JSON columns decoded."""
    row: dict

    def __getattr__(self, name):
        row = self.__dict__["row"]
        if name in row:
            return row[name]
        if name + "_json" in row:
            raw = row[name + "_json"]
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return None
        raise AttributeError(name)

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def is_batch(self) -> bool:
        return self.group_key == BATCH_KEY


class Journal:
    def __init__(self, path: Optional[Path] = None, timeout: float = 10.0):
        self.path = Path(path) if path else store.journal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None,
                                   check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        mode = "DELETE" if _network(self.path) else "WAL"
        try:
            self.con.execute(f"PRAGMA journal_mode={mode}")
        except sqlite3.OperationalError:
            pass
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                self.con.execute(stmt)
        self.con.execute(f"PRAGMA user_version={SCHEMA}")

    def close(self):
        try:
            self.con.close()
        except sqlite3.Error:
            pass

    @contextmanager
    def tx(self):
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield self.con
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")

    def _rows(self, sql: str, args: Iterable = ()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, tuple(args)).fetchall()]

    # -- watcher ---------------------------------------------------------------------------

    def heartbeat(self, state: str, current_job: str = "", message: str = "", started: Optional[float] = None):
        import socket
        self.con.execute(
            "INSERT INTO watcher(id, pid, host, user, state, heartbeat, current_job, message, started) "
            "VALUES(1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET pid=excluded.pid, host=excluded.host, "
            "user=excluded.user, state=excluded.state, heartbeat=excluded.heartbeat, "
            "current_job=excluded.current_job, message=excluded.message, "
            "started=COALESCE(excluded.started, watcher.started)",
            (os.getpid(), socket.gethostname(), _user(), state, time.time(), current_job, message, started))

    def watcher_status(self) -> dict:
        rows = self._rows("SELECT * FROM watcher WHERE id=1")
        return rows[0] if rows else {}

    # -- batches and runs ------------------------------------------------------------------

    def first_scan(self, workflow_id: str, source) -> bool:
        """True the first time a workflow looks at its folder (what is there then is not processed)."""
        key = os.path.normcase(os.path.abspath(str(source)))
        cur = self.con.execute("INSERT OR IGNORE INTO watched(workflow_id, source_key, first_scan) VALUES(?,?,?)",
                               (workflow_id, key, time.time()))
        return cur.rowcount == 1

    def batch(self, workflow_id: str, folder: Path) -> dict:
        key = os.path.normcase(os.path.abspath(str(folder)))
        rows = self._rows("SELECT * FROM batches WHERE workflow_id=? AND folder_key=?", (workflow_id, key))
        if rows:
            return rows[0]
        now = time.time()
        self.con.execute("INSERT OR IGNORE INTO batches(workflow_id, folder, folder_key, name, first_seen, "
                         "last_change) VALUES(?,?,?,?,?,?)", (workflow_id, str(folder), key, Path(folder).name,
                                                               now, now))
        return self._rows("SELECT * FROM batches WHERE workflow_id=? AND folder_key=?", (workflow_id, key))[0]

    def update_batch(self, batch_id: int, **fields):
        if "plan" in fields:
            fields["plan_json"] = json.dumps(fields.pop("plan"), default=str)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.con.execute(f"UPDATE batches SET {sets} WHERE id=?", (*fields.values(), batch_id))

    def batches(self, workflow_id: Optional[str] = None) -> list[dict]:
        if workflow_id:
            return self._rows("SELECT * FROM batches WHERE workflow_id=? ORDER BY first_seen", (workflow_id,))
        return self._rows("SELECT * FROM batches ORDER BY first_seen")

    def batch_by_id(self, batch_id: int) -> dict:
        rows = self._rows("SELECT * FROM batches WHERE id=?", (batch_id,))
        return rows[0] if rows else {}

    def runs(self, batch_id: int) -> dict[str, dict]:
        return {r["stem"]: r for r in self._rows("SELECT * FROM runs WHERE batch_id=?", (batch_id,))}

    def upsert_run(self, batch_id: int, stem: str, **fields):
        cur = self._rows("SELECT id FROM runs WHERE batch_id=? AND stem=?", (batch_id, stem))
        if cur:
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                self.con.execute(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), cur[0]["id"]))
        else:
            fields = dict(fields, batch_id=batch_id, stem=stem)
            fields.setdefault("first_seen", time.time())
            cols = ", ".join(fields)
            self.con.execute(f"INSERT INTO runs({cols}) VALUES({', '.join('?' * len(fields))})",
                             tuple(fields.values()))

    # -- jobs --------------------------------------------------------------------------------

    def job(self, job_id: str) -> Optional[Job]:
        rows = self._rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        return Job(rows[0]) if rows else None

    def find_job(self, workflow_id: str, method_node: str, batch_id: int, group_key: str) -> Optional[Job]:
        rows = self._rows("SELECT * FROM jobs WHERE workflow_id=? AND method_node=? AND batch_id=? AND group_key=?",
                          (workflow_id, method_node, batch_id, group_key))
        return Job(rows[0]) if rows else None

    def ensure_job(self, workflow_id: str, method_node: str, batch_id: int, group_key: str, group_name: str,
                   members: list, blanks: dict, input_fp: str, state: str = WAITING, reason: str = "") -> Job:
        """The job of one sample; created waiting. A finished job whose input changed (a run was
        acquired again) gets a new revision and is processed again; its review is reset."""
        from gcws.automation.workflow import new_id
        now = time.time()
        with self.tx():
            cur = self.find_job(workflow_id, method_node, batch_id, group_key)
            if cur is None:
                jid = new_id("j")
                self.con.execute(
                    "INSERT INTO jobs(id, workflow_id, method_node, batch_id, group_key, group_name, members_json, "
                    "blanks_json, input_fp, state, reason, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (jid, workflow_id, method_node, batch_id, group_key, group_name, json.dumps(members),
                     json.dumps(blanks), input_fp, state, reason, now))
                what = "batch report" if group_key == BATCH_KEY else "new sample"
                self._event_raw("info", workflow_id, batch_id, jid, f"{group_name}: {what} ({reason or state})")
                return self.job(jid)
            if cur.input_fp != input_fp and cur.state not in (QUEUED, PROCESSING, WAITING):
                self.con.execute(
                    "UPDATE jobs SET revision=revision+1, members_json=?, blanks_json=?, input_fp=?, state=?, "
                    "reason=?, attempts=0, reviewer=NULL, comment=NULL, reviewed_at=NULL, export_pending=0, "
                    "not_before=0 WHERE id=?",
                    (json.dumps(members), json.dumps(blanks), input_fp, state, "input changed: " + (reason or ""),
                     cur.id))
                self._event_raw("warning", workflow_id, batch_id, cur.id,
                                f"{group_name}: the raw data changed; processed again (revision {cur.revision + 1})")
            elif cur.state == WAITING or (cur.state == NOT_PROCESSED and cur.input_fp != input_fp):
                self.con.execute("UPDATE jobs SET members_json=?, blanks_json=?, input_fp=?, group_name=?, "
                                 "reason=?, state=? WHERE id=?",
                                 (json.dumps(members), json.dumps(blanks), input_fp, group_name, reason,
                                  state if cur.state == WAITING else WAITING, cur.id))
            return self.job(cur.id)

    def transition(self, job_id: str, from_states, to: str, **fields) -> bool:
        """Set ``to`` if the job is in one of ``from_states`` (and ``to`` is allowed); True if done."""
        from_states = [from_states] if isinstance(from_states, str) else list(from_states)
        from_states = [s for s in from_states if to in TRANSITIONS.get(s, ())]
        if not from_states:
            return False
        for k in list(fields):
            if k.split("_json")[0] in _JSON and not k.endswith("_json"):
                fields[k + "_json"] = json.dumps(fields.pop(k), default=str)
        sets = ", ".join(["state=?"] + [f"{k}=?" for k in fields])
        marks = ", ".join("?" * len(from_states))
        cur = self.con.execute(f"UPDATE jobs SET {sets} WHERE id=? AND state IN ({marks})",
                               (to, *fields.values(), job_id, *from_states))
        return cur.rowcount == 1

    def update_job(self, job_id: str, **fields):
        for k in list(fields):
            if k in _JSON:
                fields[k + "_json"] = json.dumps(fields.pop(k), default=str)
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def next_queued(self, now: Optional[float] = None) -> Optional[Job]:
        rows = self._rows("SELECT * FROM jobs WHERE state=? AND not_before<=? ORDER BY queued_at, created LIMIT 1",
                          (QUEUED, now if now is not None else time.time()))
        return Job(rows[0]) if rows else None

    def jobs(self, *, workflow_id: Optional[str] = None, batch_id: Optional[int] = None,
             states: Optional[Iterable[str]] = None, include_batch: bool = True) -> list[Job]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if workflow_id:
            sql += " AND workflow_id=?"
            args.append(workflow_id)
        if batch_id is not None:
            sql += " AND batch_id=?"
            args.append(batch_id)
        if states:
            states = list(states)
            sql += f" AND state IN ({', '.join('?' * len(states))})"
            args += states
        if not include_batch:
            sql += " AND group_key<>?"
            args.append(BATCH_KEY)
        return [Job(r) for r in self._rows(sql + " ORDER BY created", args)]

    def counts(self, workflow_id: Optional[str] = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) AS n FROM jobs WHERE group_key<>?"
        args = [BATCH_KEY]
        if workflow_id:
            sql += " AND workflow_id=?"
            args.append(workflow_id)
        return {r["state"]: r["n"] for r in self._rows(sql + " GROUP BY state", args)}

    # -- review (Report²) -----------------------------------------------------------------------

    def review(self, job_id: str, accept: bool, comment: str = "", user: Optional[str] = None) -> bool:
        """The analyst accepts or rejects a processed report."""
        user = user or _user()
        j = self.job(job_id)
        if j is None:
            return False
        to = ACCEPTED_MANUAL if accept else REJECTED
        ok = self.transition(job_id, (CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL, REJECTED), to, reviewer=user,
                             comment=comment, reviewed_at=time.time(), export_pending=1 if accept else 0)
        if ok:
            self.event("info", f"{j.group_name}: {'accepted' if accept else 'rejected'} by {user}"
                       + (f" - {comment}" if comment else ""), job_id=job_id, workflow_id=j.workflow_id,
                       batch_id=j.batch_id, user=user)
        return ok

    def request(self, job_id: str, mode: str = "full", override: Optional[dict] = None,
                user: Optional[str] = None) -> bool:
        """Process a job again ("full") or report again from its edited project ("rereport")."""
        j = self.job(job_id)
        if j is None:
            return False
        fields = {"mode": mode, "attempts": 0, "not_before": 0, "queued_at": time.time(), "reviewer": None,
                  "comment": None, "reviewed_at": None, "export_pending": 0, "revision": int(j.revision or 1) + 1}
        if override is not None:
            fields["override"] = override
        ok = self.transition(job_id, (CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL, REJECTED, FAILED, NOT_PROCESSED,
                                      WAITING), QUEUED, **fields)
        if ok:
            self.event("info", f"{j.group_name}: {'report again' if mode == 'rereport' else 'process again'} "
                       f"requested", job_id=job_id, workflow_id=j.workflow_id, batch_id=j.batch_id, user=user)
        return ok

    # -- exports and events ------------------------------------------------------------------

    def exported(self, job_id: str, revision: int) -> set:
        return {(r["folder_node"], r["report_node"], r["fmt"]) for r in self._rows(
            "SELECT folder_node, report_node, fmt FROM exports WHERE job_id=? AND revision=? AND state='done'",
            (job_id, revision))}

    def add_export(self, job_id: str, revision: int, folder_node: str, report_node: str, fmt: str, src, dst,
                   state: str = "done", error: str = ""):
        self.con.execute("INSERT INTO exports(job_id, revision, folder_node, report_node, fmt, src, dst, ts, state, "
                         "error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (job_id, revision, folder_node, report_node, fmt, str(src), str(dst or ""), time.time(),
                          state, error))

    def exports(self, job_id: str) -> list[dict]:
        return self._rows("SELECT * FROM exports WHERE job_id=? ORDER BY ts", (job_id,))

    def _event_raw(self, level, workflow_id, batch_id, job_id, text, user=None):
        self.con.execute("INSERT INTO events(ts, level, user, workflow_id, batch_id, job_id, text) "
                         "VALUES(?,?,?,?,?,?,?)", (time.time(), level, user or _user(), workflow_id, batch_id,
                                                   job_id, text))

    def event(self, level: str, text: str, *, workflow_id: str = "", batch_id: Optional[int] = None,
              job_id: str = "", user: Optional[str] = None):
        self._event_raw(level, workflow_id, batch_id, job_id, text, user)

    def events(self, *, job_id: Optional[str] = None, after: int = 0, limit: int = 500) -> list[dict]:
        if job_id:
            return self._rows("SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                              (job_id, after, limit))
        return self._rows("SELECT * FROM (SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?) ORDER BY id",
                          (after, limit))


def when(ts) -> str:
    """A journal time stamp for display."""
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")

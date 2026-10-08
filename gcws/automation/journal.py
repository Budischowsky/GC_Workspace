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

SCHEMA = 4
#: seconds an analyst's accept waits before it is delivered (Report² offers "Undo" meanwhile)
UNDO_GRACE = 10.0

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
REMOVED = "removed"                     # the analyst took it out of the queue (skipped, restorable)

STATE_LABELS = {WAITING: "Waiting", QUEUED: "Queued", PROCESSING: "Processing", CONTROL: "Control needed",
                ACCEPTED_AUTO: "Accepted (automatic)", ACCEPTED_MANUAL: "Accepted (analyst)",
                REJECTED: "Rejected", FAILED: "Failed", NOT_PROCESSED: "Not processed", REMOVED: "Removed"}
ACCEPTED = (ACCEPTED_AUTO, ACCEPTED_MANUAL)
DONE = (CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL, REJECTED)          # processed (a report exists)
DELIVERABLE = (CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL)              # the states a report is delivered in
TRANSITIONS = {
    WAITING: {QUEUED, NOT_PROCESSED, WAITING, REMOVED},
    QUEUED: {PROCESSING, WAITING, REMOVED},
    PROCESSING: {CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL, FAILED, NOT_PROCESSED, QUEUED},
    FAILED: {QUEUED, WAITING, REMOVED},
    NOT_PROCESSED: {QUEUED, WAITING, REMOVED},
    REMOVED: {QUEUED},
    CONTROL: {ACCEPTED_MANUAL, REJECTED, QUEUED},
    ACCEPTED_AUTO: {REJECTED, QUEUED, CONTROL},
    ACCEPTED_MANUAL: {REJECTED, QUEUED, CONTROL},
    REJECTED: {QUEUED, CONTROL, ACCEPTED_MANUAL},
}
#: the states of the queue (not yet processed, or processing could not finish)
QUEUE = (WAITING, QUEUED, PROCESSING, FAILED, NOT_PROCESSED)
#: the states "Remove from queue" applies to (a job being processed is not taken away from the watcher)
REMOVABLE = (WAITING, QUEUED, FAILED, NOT_PROCESSED)
BATCH_KEY = "__batch__"                 # the job row of a batch report
#: the workflow id of the entries the analyst accepted outside any workflow (``gcws.automation.manual``)
MANUAL_WORKFLOW = "manual"

_DDL = """
CREATE TABLE IF NOT EXISTS watcher(id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, host TEXT, user TEXT,
    state TEXT, heartbeat REAL, current_job TEXT, message TEXT, started REAL);
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, workflow_id TEXT, folder TEXT, folder_key TEXT,
    name TEXT, first_seen REAL, last_change REAL, has_log INTEGER DEFAULT 0, seq_completed INTEGER DEFAULT 0,
    state TEXT DEFAULT 'open', plan_json TEXT, deleted INTEGER DEFAULT 0, reopened REAL DEFAULT 0,
    local_folder TEXT, copied_extras TEXT, UNIQUE(workflow_id, folder_key));
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, batch_id INTEGER, path TEXT, stem TEXT, role TEXT,
    fingerprint TEXT, stable_count INTEGER DEFAULT 0, first_seen REAL, last_change REAL, state TEXT,
    marker INTEGER DEFAULT 0, baseline INTEGER DEFAULT 0, copied_fp TEXT, copy_error TEXT,
    UNIQUE(batch_id, stem));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, workflow_id TEXT, method_node TEXT, batch_id INTEGER,
    group_key TEXT, group_name TEXT, revision INTEGER DEFAULT 1, members_json TEXT, blanks_json TEXT,
    input_fp TEXT, state TEXT, reason TEXT, attempts INTEGER DEFAULT 0, not_before REAL DEFAULT 0,
    created REAL, queued_at REAL, started REAL, finished REAL, pid INTEGER, job_dir TEXT, project_path TEXT,
    files_json TEXT, summary_json TEXT, evidence_json TEXT, findings_json TEXT, reviewer TEXT, comment TEXT,
    reviewed_at REAL, export_state TEXT DEFAULT 'none', export_pending INTEGER DEFAULT 0, override_json TEXT,
    mode TEXT DEFAULT 'full', edited INTEGER DEFAULT 0, review_pending_json TEXT, deleted INTEGER DEFAULT 0,
    deliver_after REAL DEFAULT 0, UNIQUE(workflow_id, method_node, batch_id, group_key));
CREATE TABLE IF NOT EXISTS exports(id INTEGER PRIMARY KEY, job_id TEXT, revision INTEGER, folder_node TEXT,
    report_node TEXT, fmt TEXT, src TEXT, dst TEXT, ts REAL, state TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, user TEXT, workflow_id TEXT,
    batch_id INTEGER, job_id TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS watched(workflow_id TEXT, source_key TEXT, first_scan REAL,
    PRIMARY KEY(workflow_id, source_key));
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
"""

_JSON = ("members", "blanks", "files", "summary", "evidence", "findings", "override", "plan",
         "review_pending")


def finding_key(finding: dict) -> tuple:
    """Stable identity of a finding, independent of its measured value or wording."""
    rt = finding.get("rt")
    unstructured = not any(finding.get(k) for k in ("member", "cas", "substance")) and rt is None
    return (finding.get("rule") or "", finding.get("member") or "", finding.get("cas") or "",
            finding.get("substance") or "", round(float(rt), 2) if rt is not None else None,
            finding.get("level") or "control", (finding.get("text") or "").strip().casefold() if unstructured else "")


def _user() -> str:
    from gcws.core.audit import current_user
    return current_user()


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
                value = json.loads(raw) if raw else None
            except ValueError:
                value = None
            self.__dict__[name] = value                 # decoded once (a report's evidence is ~30 KB)
            return value
        raise AttributeError(name)

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def is_batch(self) -> bool:
        return self.group_key == BATCH_KEY


def _ensure_changes_nothing(cur: "Job", members: list, blanks: dict, input_fp: str, group_name: str, state: str,
                            reason: str) -> bool:
    """True when :meth:`Journal.ensure_job` would leave the job ``cur`` as it is."""
    if cur.state == REMOVED or cur.deleted:
        return True
    if cur.input_fp != input_fp and cur.state not in (QUEUED, PROCESSING, WAITING):
        return False                                   # a new revision
    if cur.state == WAITING:
        return (state == WAITING and cur.input_fp == input_fp and (cur.members or []) == list(members)
                and (cur.blanks or {}) == dict(blanks) and cur.group_name == group_name
                and (cur.reason or "") == (reason or ""))
    return not (cur.state == NOT_PROCESSED and cur.input_fp != input_fp)


class Journal:
    def __init__(self, path: Optional[Path] = None, timeout: float = 10.0):
        self.path = Path(path) if path else store.journal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), timeout=timeout, isolation_level=None,
                                   check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        mode = "DELETE" if store.is_network(self.path) else "WAL"
        try:
            self.con.execute(f"PRAGMA journal_mode={mode}")
        except sqlite3.OperationalError:
            pass
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                self.con.execute(stmt)
        columns = {r["name"] for r in self.con.execute("PRAGMA table_info(jobs)")}
        if "edited" not in columns:
            self.con.execute("ALTER TABLE jobs ADD COLUMN edited INTEGER DEFAULT 0")
        if "review_pending_json" not in columns:
            self.con.execute("ALTER TABLE jobs ADD COLUMN review_pending_json TEXT")
        if "deleted" not in columns:
            self.con.execute("ALTER TABLE jobs ADD COLUMN deleted INTEGER DEFAULT 0")
        if "deliver_after" not in columns:
            self.con.execute("ALTER TABLE jobs ADD COLUMN deliver_after REAL DEFAULT 0")
        columns = {r["name"] for r in self.con.execute("PRAGMA table_info(batches)")}
        if "deleted" not in columns:
            self.con.execute("ALTER TABLE batches ADD COLUMN deleted INTEGER DEFAULT 0")
        if "reopened" not in columns:
            self.con.execute("ALTER TABLE batches ADD COLUMN reopened REAL DEFAULT 0")
        for col in ("local_folder", "copied_extras"):
            if col not in columns:
                self.con.execute(f"ALTER TABLE batches ADD COLUMN {col} TEXT")
        for col, kind in (("folder_birth", "REAL"), ("missing", "INTEGER DEFAULT 0"), ("force_json", "TEXT"),
                          ("manual", "INTEGER DEFAULT 0")):
            if col not in columns:
                self.con.execute(f"ALTER TABLE batches ADD COLUMN {col} {kind}")
        columns = {r["name"] for r in self.con.execute("PRAGMA table_info(runs)")}
        for col in ("copied_fp", "copy_error"):
            if col not in columns:
                self.con.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        # gone: when the run was found deleted from its folder
        for col, kind in (("birth", "REAL"), ("readded", "INTEGER DEFAULT 0"), ("gone", "REAL")):
            if col not in columns:
                self.con.execute(f"ALTER TABLE runs ADD COLUMN {col} {kind}")
        if "census" not in {r["name"] for r in self.con.execute("PRAGMA table_info(watched)")}:
            self.con.execute("ALTER TABLE watched ADD COLUMN census REAL")
        # what :meth:`stamp` reads, so that it never reads the reports' large columns
        self.con.execute("CREATE INDEX IF NOT EXISTS jobs_stamp ON jobs(state, finished, reviewed_at, created, "
                         "deleted, export_pending, export_state, revision, edited)")
        # jobs() lists in this order: without the index every report's large columns were sorted
        self.con.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created)")
        # the watcher asks every few seconds what waits for delivery: only those rows are read
        self.con.execute("CREATE INDEX IF NOT EXISTS jobs_deliver ON jobs(export_pending) WHERE export_pending=1")
        self.con.execute(f"PRAGMA user_version={SCHEMA}")
        self._data_version: dict = {}

    def close(self):
        try:
            self.con.close()
        except sqlite3.Error:
            pass

    def changed(self, who: str = "") -> bool:
        """True when anything was written since ``who`` last asked (by any program; nearly free). The
        panels look at :meth:`stamp` only then."""
        try:
            now = (self.con.execute("PRAGMA data_version").fetchone()[0], self.con.total_changes)
        except sqlite3.Error:
            return True
        old = self._data_version.get(who)
        self._data_version[who] = now
        return now != old

    def stamp(self) -> tuple:
        """Changes when a report, a batch or the log changes (not with the watcher's heartbeat). Read
        from an index: a fraction of a millisecond also with thousands of reports."""
        a = self.con.execute("SELECT COUNT(*), MAX(COALESCE(finished,0)), MAX(COALESCE(reviewed_at,0)), "
                             "MAX(COALESCE(created,0)), SUM(LENGTH(state) + 100 * UNICODE(state)), SUM(deleted), "
                             "SUM(export_pending), "
                             "SUM(LENGTH(COALESCE(export_state,''))), SUM(revision), SUM(edited) FROM jobs").fetchone()
        b = self.con.execute("SELECT COUNT(*), SUM(deleted), MAX(COALESCE(reopened,0)), SUM(missing), "
                             "SUM(LENGTH(COALESCE(force_json,''))), SUM(LENGTH(COALESCE(plan_json,''))) "
                             "FROM batches").fetchone()
        # the watcher rewrites why a sample waits without another change (the few rows of the queue)
        marks = ", ".join("?" * len(QUEUE))
        c = self.con.execute(f"SELECT GROUP_CONCAT(id || ':' || COALESCE(reason,'') || ':' || COALESCE(group_name,''), "
                             f"'|') FROM jobs WHERE state IN ({marks})", QUEUE).fetchone()[0]
        return tuple(a) + tuple(b) + (self.con.execute("SELECT MAX(id) FROM events").fetchone()[0], c)

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

    def census(self, workflow_id: str, source) -> bool:
        """True once per workflow and watched folder (at its first look, or the first look of a journal
        from before): the batch folders found then that are too old to look at are recorded, so that
        afterwards a folder the journal does not know is new data, however old its files are."""
        key = os.path.normcase(os.path.abspath(str(source)))
        self.con.execute("INSERT OR IGNORE INTO watched(workflow_id, source_key, first_scan) VALUES(?,?,?)",
                         (workflow_id, key, time.time()))
        cur = self.con.execute("UPDATE watched SET census=? WHERE workflow_id=? AND source_key=? AND census IS NULL",
                               (time.time(), workflow_id, key))
        return cur.rowcount == 1

    def batch_keys(self, workflow_id: str) -> set[str]:
        return {r["folder_key"] for r in self._rows("SELECT folder_key FROM batches WHERE workflow_id=?",
                                                    (workflow_id,))}

    def reset_batch(self, batch_id: int, reason: str = "the batch folder was put in again") -> list[str]:
        """The batch folder came back (removed and put in again, or deleted and copied in again): its
        samples are processed again as new revisions, deleted reports are shown again and the runs are
        looked at afresh. Entries accepted by hand and a job being processed are left as they are."""
        done = []
        with self.tx():
            for j in self.jobs(batch_id=batch_id):
                if j.workflow_id == MANUAL_WORKFLOW or j.state == PROCESSING:
                    continue
                self.con.execute(
                    "UPDATE jobs SET state=?, revision=revision+1, input_fp='', reason=?, attempts=0, not_before=0, "
                    "reviewer=NULL, comment=NULL, reviewed_at=NULL, export_pending=0, export_state='none', edited=0, "
                    "review_pending_json=NULL, deleted=0, deliver_after=0, mode='full', override_json=NULL "
                    "WHERE id=?", (WAITING, reason, j.id))
                done.append(j.id)
            self.con.execute("DELETE FROM runs WHERE batch_id=?", (batch_id,))
            self.con.execute("UPDATE batches SET deleted=0, missing=0, reopened=0, plan_json=NULL, seq_completed=0, "
                             "last_change=? WHERE id=?", (time.time(), batch_id))
        return done

    def request_samples(self, workflow_id: str, folder, runs: Optional[Iterable[str]] = None, *,
                        outside: bool = False, user: Optional[str] = None) -> dict:
        """The analyst adds samples to the queue: the runs ``runs`` of the batch folder (their samples), or
        all of it (``None``). The watcher processes them at its next look - without waiting for the quiet
        time, also runs there before watching started or processed already. ``outside``: the folder is not
        below the workflow's watched folder (the watcher then looks at it for this request only)."""
        user = user or _user()
        b = self.batch(workflow_id, Path(folder))
        stems = {"*"} if runs is None else {Path(str(r)).stem.casefold() if str(r).lower().endswith((".d", ".qgd"))
                                             else str(r).casefold() for r in runs}
        before = set(json.loads(b.get("force_json") or "[]") or [])
        force = sorted({"*"} if "*" in stems | before else stems | before)
        self.update_batch(b["id"], force_json=json.dumps(force), manual=1 if outside or b.get("manual") else 0,
                          deleted=0)
        # samples removed from the queue earlier come back: the analyst asked for them again
        for j in self.jobs(batch_id=b["id"], states=[REMOVED], include_batch=False):
            if "*" in stems or {Path(str(m)).stem.casefold() for m in j.members or []} & stems:
                self.con.execute("UPDATE jobs SET state=?, reason=? WHERE id=? AND state=?",
                                 (WAITING, "added to the queue again", j.id, REMOVED))
        what = "all samples" if force == ["*"] else f"{len(stems)} run(s)"
        self.event("info", f"{b['name']}: {what} added to the queue by {user}", workflow_id=workflow_id,
                   batch_id=b["id"], user=user)
        return self.batch_by_id(b["id"])

    def cancel_request(self, batch_id: int, user: Optional[str] = None) -> bool:
        """Samples added by hand that the watcher has not taken up yet are taken off the queue again."""
        user = user or _user()
        b = self.batch_by_id(batch_id)
        if not b or not self.forced(b):
            return False
        self.update_batch(batch_id, force_json=None)
        self.event("info", f"{b['name']}: request removed from the queue by {user}", workflow_id=b["workflow_id"],
                   batch_id=batch_id, user=user)
        return True

    def batch_gone(self, batch_id: int) -> list[str]:
        """The batch folder was deleted (or moved away): it is marked missing, a request for it is dropped
        and its samples leave the queue (putting the folder back processes them again)."""
        self.update_batch(batch_id, missing=1, force_json=None)
        return [j.id for j in self.jobs(batch_id=batch_id, states=REMOVABLE)
                if self.transition(j.id, REMOVABLE, REMOVED, reason="the batch folder was deleted")]

    def forced(self, batch: dict) -> set:
        try:
            return set(json.loads(batch.get("force_json") or "[]") or [])
        except ValueError:
            return set()

    def enqueue(self, job_id: str, user: Optional[str] = None) -> bool:
        """A sample the analyst added to the queue: queued now (a processed, removed or deleted one as a
        new revision, and shown again)."""
        j = self.job(job_id)
        if j is None:
            return False
        if j.deleted:
            self.con.execute("UPDATE jobs SET deleted=0 WHERE id=?", (job_id,))
        if j.state in (QUEUED, PROCESSING):
            return True
        if j.state == WAITING:
            return self.transition(job_id, WAITING, QUEUED, queued_at=time.time(), not_before=0,
                                   reason="added to the queue by hand")
        return self.request(job_id, "full", user=user)

    def put_in_again(self, job_ids: Iterable[str]) -> list[str]:
        """A run of these samples was deleted and copied in again: a sample deleted in Report² is shown
        again and one removed from the queue waits again, so that the watcher processes the new data."""
        done = []
        for j in (self.job(i) for i in job_ids):
            if j is None or not (j.deleted or j.state == REMOVED):
                continue
            self.con.execute("UPDATE jobs SET deleted=0, state=?, reason=? WHERE id=?",
                             (WAITING if j.state == REMOVED else j.state, "a run was put in again", j.id))
            done.append(j.id)
        return done

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

    def runs_by_batch(self, workflow_id: str) -> dict[int, dict[str, dict]]:
        """:meth:`runs` of every batch of a workflow, in one query."""
        out: dict = {}
        for r in self._rows("SELECT * FROM runs WHERE batch_id IN (SELECT id FROM batches WHERE workflow_id=?)",
                            (workflow_id,)):
            out.setdefault(r["batch_id"], {})[r["stem"]] = r
        return out

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
        acquired again) gets a new revision and is processed again; its review is reset. A job the
        analyst removed from the queue stays removed ("Process again" brings it back); one deleted in
        Report² stays as it is (hidden) until it is restored."""
        from gcws.automation.workflow import new_id
        now = time.time()
        # the watcher asks at every look: nothing to change is answered without a write transaction
        cur = self.find_job(workflow_id, method_node, batch_id, group_key)
        if cur is not None and _ensure_changes_nothing(cur, members, blanks, input_fp, group_name, state, reason):
            return cur
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
            if cur.state == REMOVED or cur.deleted:
                return cur                             # data put in again brings it back (put_in_again)
            if cur.input_fp != input_fp and cur.state not in (QUEUED, PROCESSING, WAITING):
                self.con.execute(
                    "UPDATE jobs SET revision=revision+1, members_json=?, blanks_json=?, input_fp=?, state=?, "
                    "reason=?, attempts=0, reviewer=NULL, comment=NULL, reviewed_at=NULL, export_pending=0, "
                    "edited=0, review_pending_json=NULL, "
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

    def record_manual(self, job_id: str, *, workflow_id: str, method_node: str, batch_id: int, group_key: str,
                      group_name: str, members: list, files: dict, project_path: str, job_dir: str,
                      evidence: dict, findings: list, summary: dict, reviewer: str,
                      replace: Optional[str] = None) -> Job:
        """An entry the analyst accepted outside any workflow (``gcws.automation.manual``): inserted as
        accepted, or ``replace`` (such an entry, also one with the same key) at its next revision.
        Nothing waits for delivery: no workflow delivers it."""
        now = time.time()
        values = dict(group_key=group_key, group_name=group_name, members_json=json.dumps(members),
                      files_json=json.dumps(files, default=str), evidence_json=json.dumps(evidence, default=str),
                      findings_json=json.dumps(findings, default=str), summary_json=json.dumps(summary, default=str),
                      project_path=project_path, job_dir=job_dir, state=ACCEPTED_MANUAL, reviewer=reviewer,
                      reviewed_at=now, started=now, finished=now, export_pending=0, export_state="none",
                      edited=0, review_pending_json=None, deleted=0, reason="accepted in Replicates")
        with self.tx():
            same = self.find_job(workflow_id, method_node, batch_id, group_key)
            old = self.job(replace) if replace else same
            if same is not None and old is not None and same.id != old.id:
                raise ValueError(f"{group_name} is listed already")
            if old is None:
                cols = dict(values, id=job_id, workflow_id=workflow_id, method_node=method_node, batch_id=batch_id,
                            blanks_json="{}", input_fp="manual", created=now)
                self.con.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                                 tuple(cols.values()))
                self._event_raw("info", workflow_id, batch_id, job_id, f"{group_name}: accepted by {reviewer} "
                                "in Replicates (no workflow)", reviewer)
                return self.job(job_id)
            sets = ", ".join(f"{k}=?" for k in values)
            self.con.execute(f"UPDATE jobs SET revision=revision+1, {sets} WHERE id=?", (*values.values(), old.id))
            self._event_raw("info", workflow_id, batch_id, old.id, f"{group_name}: accepted again by {reviewer} "
                            f"in Replicates (revision {old.revision + 1})", reviewer)
            return self.job(old.id)

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

    def to_deliver(self) -> list[Job]:
        """The reports waiting for delivery (from a partial index: not every report there is)."""
        return [Job(r) for r in self._rows(
            f"SELECT * FROM jobs WHERE export_pending=1 AND state IN ({', '.join('?' * len(DELIVERABLE))}) "
            "ORDER BY created", DELIVERABLE)]

    def counts(self, workflow_id: Optional[str] = None) -> dict[str, int]:
        sql = "SELECT state, COUNT(*) AS n FROM jobs WHERE group_key<>?"
        args = [BATCH_KEY]
        if workflow_id:
            sql += " AND workflow_id=?"
            args.append(workflow_id)
        return {r["state"]: r["n"] for r in self._rows(sql + " GROUP BY state", args)}

    # -- review (Report²) -----------------------------------------------------------------------

    def mark_edited(self, job_id: str, revision: int, user: Optional[str] = None) -> bool:
        """A saved analyst project now differs from the report of this revision."""
        with self.tx():
            job = self.job(job_id)
            if job is None or job.is_batch or job.revision != revision or job.state not in (CONTROL, *ACCEPTED, REJECTED):
                return False
            if job.state == REJECTED:
                # The rejection remains the visible decision until a new revision is queued.
                cur = self.con.execute(
                    "UPDATE jobs SET edited=1, export_pending=0, review_pending_json=NULL "
                    "WHERE id=? AND revision=? AND state=?",
                    (job_id, revision, REJECTED))
            else:
                cur = self.con.execute(
                    "UPDATE jobs SET state=?, edited=1, reviewer=NULL, comment=NULL, reviewed_at=NULL, "
                    "export_pending=0, review_pending_json=NULL WHERE id=? AND revision=? AND state=?",
                    (CONTROL, job_id, revision, job.state))
            if cur.rowcount:
                self._event_raw("info", job.workflow_id, job.batch_id, job_id,
                                f"{job.group_name}: analyst changes saved; report needs review", user)
            return cur.rowcount == 1

    def update_edited(self, job_id: str, comment: str = "", user: Optional[str] = None) -> bool:
        """Update report: the edited report is made again (queued as a new revision). It is never accepted
        by that - the updated report comes back as "control needed" for the analyst to check and accept."""
        user = user or _user()
        with self.tx():
            job = self.job(job_id)
            if job is None or job.state not in (CONTROL, REJECTED) or not job.edited or not job.project_path:
                return False
            intent = {"user": user, "comment": comment,
                      "findings": [finding_key(f) for f in (job.findings or []) if f.get("level", "control") == "control"]}
            ok = self.transition(job_id, job.state, QUEUED, mode="rereport", revision=job.revision + 1,
                                 queued_at=time.time(), not_before=0, attempts=0, export_pending=0,
                                 reviewer=None, comment=None, reviewed_at=None, review_pending=intent)
            if ok:
                self._event_raw("info", job.workflow_id, job.batch_id, job_id,
                                f"{job.group_name}: report update requested by {user}; making the edited report "
                                "again", user)
            return ok

    accept_edited = update_edited                  # the name before Oct 2026 (it accepted the result as well)

    def set_control(self, job_id: str, user: Optional[str] = None) -> bool:
        """Set status > Control needed: an accepted (or rejected) report is to be checked again. The decision
        is taken back (the history keeps it); files delivered already stay where they are, and accepting it
        again delivers what is missing."""
        user = user or _user()
        with self.tx():
            j = self.job(job_id)
            if j is None or j.is_batch or j.deleted or j.review_pending or j.state not in (*ACCEPTED, REJECTED):
                return False
            deliver = j.workflow_id != MANUAL_WORKFLOW
            ok = self.transition(job_id, j.state, CONTROL, reviewer=None, comment=None, reviewed_at=None,
                                 export_pending=1 if deliver else 0, deliver_after=0,
                                 reason=f"set back to control needed by {user}")
            if ok:
                self._event_raw("info", j.workflow_id, j.batch_id, job_id,
                                f"{j.group_name}: set back to control needed by {user} (was "
                                f"{STATE_LABELS.get(j.state, j.state).lower()})", user)
            return ok

    def review(self, job_id: str, accept: bool, comment: str = "", user: Optional[str] = None,
               grace: float = 0.0) -> bool:
        """The analyst accepts or rejects a processed report. ``grace``: seconds the watcher waits
        before it delivers an accepted report (so the analyst can still undo it)."""
        user = user or _user()
        j = self.job(job_id)
        if j is None or j.review_pending or (accept and j.edited):
            return False
        to = ACCEPTED_MANUAL if accept else REJECTED
        now = time.time()
        deliver = accept and j.workflow_id != MANUAL_WORKFLOW          # no workflow delivers an entry by hand
        ok = self.transition(job_id, (CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL, REJECTED), to, reviewer=user,
                             comment=comment, reviewed_at=now, export_pending=1 if deliver else 0,
                             deliver_after=now + grace if deliver and grace else 0)
        if ok:
            self.event("info", f"{j.group_name}: {'accepted' if accept else 'rejected'} by {user}"
                       + (f" - {comment}" if comment else ""), job_id=job_id, workflow_id=j.workflow_id,
                       batch_id=j.batch_id, user=user)
        return ok

    #: the fields a review changes, which :meth:`undo_review` puts back
    REVIEW_FIELDS = ("state", "reviewer", "comment", "reviewed_at", "export_pending", "deliver_after")

    def undo_review(self, job_id: str, before: dict, user: Optional[str] = None) -> bool:
        """Take back an accept or reject: ``before`` holds :attr:`REVIEW_FIELDS` as they were. Only
        while the job is still as the review left it (same revision, nothing delivered since)."""
        user = user or _user()
        with self.tx():
            j = self.job(job_id)
            if j is None or j.state not in (ACCEPTED_MANUAL, REJECTED) or j.revision != before.get("revision"):
                return False
            if any(e["revision"] == j.revision and float(e["ts"] or 0) >= float(j.reviewed_at or 0)
                   for e in self.exports(job_id)):
                return False                                # already delivered: reject it instead
            fields = {k: before.get(k) for k in self.REVIEW_FIELDS}
            sets = ", ".join(f"{k}=?" for k in fields)
            cur = self.con.execute(f"UPDATE jobs SET {sets} WHERE id=? AND state=? AND revision=?",
                                   (*fields.values(), job_id, j.state, j.revision))
            if cur.rowcount:
                self._event_raw("info", j.workflow_id, j.batch_id, job_id,
                                f"{j.group_name}: {'accept' if j.state == ACCEPTED_MANUAL else 'reject'} "
                                f"undone by {user}", user)
            return cur.rowcount == 1

    #: ``jobs.deleted`` of a job hidden with its batch (:meth:`delete_batch`); 1: hidden by itself
    DELETED_WITH_BATCH = 2

    def delete(self, job_ids: Iterable[str], user: Optional[str] = None, *, mark: int = 1) -> list[str]:
        """Hide jobs in Report² (nothing on disk is touched). A job still in the queue is removed from
        it as well, so the watcher does not process what nobody sees; :meth:`restore` shows it again."""
        user = user or _user()
        done = []
        for jid in job_ids:
            j = self.job(jid)
            if j is None or j.deleted:
                continue
            if j.state in REMOVABLE:
                self.transition(jid, REMOVABLE, REMOVED, reason=f"deleted in Report² by {user} "
                                f"(was: {j.label.lower()})", not_before=0)
            self.con.execute("UPDATE jobs SET deleted=? WHERE id=?", (mark, jid))
            done.append(jid)
            self.event("info", f"{j.group_name}: deleted in Report² by {user}", job_id=jid,
                       workflow_id=j.workflow_id, batch_id=j.batch_id, user=user)
        return done

    def restore(self, job_ids: Iterable[str], user: Optional[str] = None) -> list[str]:
        """Show deleted jobs again (a job removed from the queue stays removed: "Process again")."""
        user = user or _user()
        done = []
        for jid in job_ids:
            j = self.job(jid)
            if j is None or not j.deleted:
                continue
            self.con.execute("UPDATE jobs SET deleted=0 WHERE id=?", (jid,))
            done.append(jid)
            self.event("info", f"{j.group_name}: restored in Report² by {user}", job_id=jid,
                       workflow_id=j.workflow_id, batch_id=j.batch_id, user=user)
        return done

    def delete_batch(self, batch_id: int, user: Optional[str] = None) -> bool:
        """Hide a batch folder with all its reports; the watcher no longer looks at it."""
        user = user or _user()
        b = self.batch_by_id(batch_id)
        if not b or b.get("deleted"):
            return False
        self.delete([j.id for j in self.jobs(batch_id=batch_id)], user, mark=self.DELETED_WITH_BATCH)
        # 2: its reports were marked (a batch deleted before that version: 1)
        self.update_batch(batch_id, deleted=self.DELETED_WITH_BATCH)
        self.event("info", f"Batch {b['name']}: deleted in Report² by {user}", workflow_id=b["workflow_id"],
                   batch_id=batch_id, user=user)
        return True

    def restore_batch(self, batch_id: int, user: Optional[str] = None) -> bool:
        user = user or _user()
        b = self.batch_by_id(batch_id)
        if not b or not b.get("deleted"):
            return False
        self.update_batch(batch_id, deleted=0)
        jobs = self.jobs(batch_id=batch_id)
        # the reports deleted one by one before the batch stay deleted, also when that was every one of
        # them (a batch deleted by a version before: all come back)
        with_batch = [j.id for j in jobs if j.deleted == self.DELETED_WITH_BATCH]
        legacy = b.get("deleted") != self.DELETED_WITH_BATCH and not with_batch
        self.restore([j.id for j in jobs] if legacy else with_batch, user)
        self.event("info", f"Batch {b['name']}: restored in Report² by {user}", workflow_id=b["workflow_id"],
                   batch_id=batch_id, user=user)
        return True

    def reopen_batch(self, batch_id: int, user: Optional[str] = None) -> bool:
        """Bring an archived batch back to "To do" (until its reports are decided again)."""
        user = user or _user()
        b = self.batch_by_id(batch_id)
        if not b:
            return False
        self.update_batch(batch_id, reopened=time.time())
        self.event("info", f"Batch {b['name']}: reopened by {user}", workflow_id=b["workflow_id"],
                   batch_id=batch_id, user=user)
        return True

    def request(self, job_id: str, mode: str = "full", override: Optional[dict] = None,
                user: Optional[str] = None) -> bool:
        """Process a job again ("full") or report again from its edited project ("rereport")."""
        j = self.job(job_id)
        if j is None:
            return False
        fields = {"mode": mode, "attempts": 0, "not_before": 0, "queued_at": time.time(), "reviewer": None,
                  "comment": None, "reviewed_at": None, "export_pending": 0, "revision": int(j.revision or 1) + 1,
                  "edited": 0, "review_pending": None, "deliver_after": 0}
        if override is not None:
            fields["override"] = override
        ok = self.transition(job_id, (CONTROL, ACCEPTED_AUTO, ACCEPTED_MANUAL, REJECTED, FAILED, NOT_PROCESSED,
                                      WAITING, REMOVED), QUEUED, **fields)
        if ok:
            self.event("info", f"{j.group_name}: {'report again' if mode == 'rereport' else 'process again'} "
                       f"requested", job_id=job_id, workflow_id=j.workflow_id, batch_id=j.batch_id, user=user)
        return ok

    def recover_orphans(self, alive=None, keep: Iterable[str] = ()) -> list[str]:
        """Jobs left "processing" by a program that stopped meanwhile (a watcher or GC Workspace that
        crashed, a PC shut down) go back to the queue. ``alive(pid, started)`` tells whether the owner
        still runs; ``keep``: jobs the caller is processing itself."""
        alive = alive or store.alive
        keep = set(keep)
        done = []
        for j in self.jobs(states=[PROCESSING]):
            if j.id in keep or alive(j.pid, j.started):
                continue
            if self.transition(j.id, PROCESSING, QUEUED, reason="the program processing it stopped; "
                               "processed again", pid=None, not_before=0, queued_at=time.time()):
                done.append(j.id)
                self.event("warning", f"{j.group_name}: the program processing it stopped; it is processed "
                           "again", job_id=j.id, workflow_id=j.workflow_id, batch_id=j.batch_id)
        return done

    def remove(self, job_ids: Iterable[str], user: Optional[str] = None) -> list[str]:
        """Take jobs out of the queue (they cannot be processed and would hold the batch up): the
        watcher skips them, rescans do not add them again and the batch report does not wait for
        them. Returns the ids removed; a job being processed is not."""
        user = user or _user()
        done = []
        for jid in job_ids:
            j = self.job(jid)
            if j is None:
                continue
            if self.transition(jid, REMOVABLE, REMOVED, reason=f"removed from the queue by {user} "
                               f"(was: {j.label.lower()})", not_before=0):
                done.append(jid)
                self.event("info", f"{j.group_name}: removed from the queue by {user}", job_id=jid,
                           workflow_id=j.workflow_id, batch_id=j.batch_id, user=user)
        return done

    # -- exports and events ------------------------------------------------------------------

    def exported(self, job_id: str, revision: int) -> set:
        return {(r["folder_node"], r["report_node"], r["fmt"]) for r in self._rows(
            "SELECT folder_node, report_node, fmt FROM exports WHERE job_id=? AND revision=? AND state='done'",
            (job_id, revision))}

    def delivered(self, job_id: str, revision: int) -> dict:
        """``{(folder node, report node, format): path}`` of what this revision delivered last ("" when the
        file found there was kept)."""
        return {(r["folder_node"], r["report_node"], r["fmt"]): "" if r["error"] else r["dst"] for r in self._rows(
            "SELECT folder_node, report_node, fmt, dst, error FROM exports WHERE job_id=? AND revision=? AND "
            "state='done' ORDER BY ts, id", (job_id, revision))}

    def targets(self) -> dict[str, list[str]]:
        """``{job id: [file delivered, ...]}`` of each job's current revision (the Delivered column)."""
        out: dict = {}
        for r in self._rows("SELECT e.job_id, e.dst FROM exports e JOIN jobs j ON j.id=e.job_id "
                            "WHERE e.revision=j.revision AND e.state='done' AND e.fmt<>'register' AND e.dst<>'' "
                            "ORDER BY e.ts, e.id"):
            if r["dst"] not in out.setdefault(r["job_id"], []):
                out[r["job_id"]].append(r["dst"])
        return out

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


def ago(ts, now: Optional[float] = None) -> str:
    """A journal time stamp relative to now ("5 min ago", "yesterday"); the date when older."""
    if not ts:
        return ""
    now = time.time() if now is None else now
    d = max(0.0, now - float(ts))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    then, today = datetime.fromtimestamp(float(ts)).date(), datetime.fromtimestamp(now).date()
    if then == today:
        return f"{int(d // 3600)} h ago"
    if (today - then).days == 1:
        return "yesterday"
    if (today - then).days < 7:
        return f"{(today - then).days} days ago"
    return then.strftime("%Y-%m-%d")


def batch_closed(batch: dict, jobs: list) -> bool:
    """True when every report of the batch is accepted and delivered (Report² archives it). A
    reopened batch stays open until one of its reports is decided again."""
    live = [j for j in jobs if not j.deleted and j.state != REMOVED]
    if not live or any(j.state not in ACCEPTED or j.export_pending or j.export_state == "error" or j.review_pending
                       for j in live):
        return False
    try:
        plan = json.loads(batch.get("plan_json") or "null")
    except ValueError:
        plan = None
    if isinstance(plan, dict) and not plan.get("complete", True):
        return False
    reopened = float(batch.get("reopened") or 0)
    return not reopened or max(float(j.reviewed_at or j.finished or 0) for j in live) > reopened

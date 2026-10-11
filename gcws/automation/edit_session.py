"""The Report² edit session: a Report² report bound to the project open in GC Workspace, without Qt.

"Edit in GC Workspace" opens a report's project and binds it to the report (its job id, the journal
revision it was opened at, the project file). The session keeps what the report was made from (its
**baseline**: the project values that can change a report), so it knows when the analyst changed the
report: saving such a change writes the bound project and sends the report back to control in Report²
(:meth:`Journal.mark_edited`). A report that changed in Report² while it was open (a new revision) is
**stale**: the session refuses to write over it.

The main window keeps the dialogs and the status bar; it asks here what is bound, whether anything
changed, which Report² report a double determination belongs to (:meth:`EditSession.find_for`) and
which runs of the project a report's members are (:func:`runs_of`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable, Optional

from gcws.automation import journal as J
from gcws.automation import manual as MA
from gcws.core import project as P

#: the run values that can change a generated report (display state and audit timestamps cannot)
REPORT_RUN_FIELDS = ("id", "name", "role", "blanks", "blanks_istd", "blanks_manual", "methods", "manual",
                     "identifications", "delay", "delay_override", "spectrum_overrides")


class Stale(Exception):
    """The bound report changed in Report² while it was open here (another revision or project)."""


def report_inputs(data: dict) -> dict:
    """Project values that can change a generated report; omit display state and audit timestamps."""
    groups = [{k: v for k, v in g.items() if k != "signoff"} for g in data.get("replicate_groups") or []]
    inputs = {"quant": data.get("quant") or {}, "replicate_groups": groups,           # who accepted: no report change
              "runs": [{key: run.get(key) for key in REPORT_RUN_FIELDS} for run in data.get("runs") or []]}
    return json.loads(json.dumps(inputs, sort_keys=True, default=str))


def member_key(names: Iterable) -> list[str]:
    """Run file names as Report² compares them: case does not matter, order does not matter."""
    return sorted(str(n).casefold() for n in names or [])


def same_members(job: Optional[J.Job], names: Iterable) -> bool:
    """``job`` is a live report of exactly the runs ``names`` (run file names)."""
    return job is not None and not job.deleted and member_key(job.members) == member_key(names)


def runs_of(job: J.Job, files: dict) -> Optional[list]:
    """The project's run ids of a report's members, in the report's order, or None when one is missing.
    ``files``: run id -> run file name."""
    by_name = {str(name).casefold(): rid for rid, name in files.items()}
    ids = [by_name.get(str(name).casefold()) for name in job.members or []]
    return ids if ids and None not in ids else None


class EditSession:
    """The report bound to the open project, and what it was made from.

    ``journal``: gives the Report² journal when asked (it may be replaced). ``on_change``: called after
    the binding changed (bound, followed to a new revision, cleared)."""

    def __init__(self, journal: Callable[[], J.Journal], on_change: Optional[Callable[[], None]] = None):
        self._journal = journal
        self._on_change = on_change
        self.bound: Optional[tuple] = None     # (job id, journal revision, project path)
        self.baseline: Optional[dict] = None   # report_inputs of the project as the report was made

    # -- the binding ----------------------------------------------------------------------------------------

    @property
    def job_id(self) -> Optional[str]:
        return self.bound[0] if self.bound else None

    @property
    def path(self) -> Optional[Path]:
        return self.bound[2] if self.bound else None

    def bind(self, job_id: str, revision: int, path, data: dict):
        """Bind report ``job_id`` at ``revision`` to the project file ``path``, whose content is ``data``."""
        self.bound = (job_id, revision, Path(path))
        self.baseline = report_inputs(data)
        self._changed()

    def clear(self):
        self.bound = None
        self.baseline = None
        self._changed()

    def is_editing(self, job_id: str, project_path) -> bool:
        """Report ``job_id`` is bound, and its project is the one open."""
        return self.bound is not None and self.bound[0] == job_id and project_path is not None and \
            Path(project_path) == self.bound[2]

    def job(self) -> Optional[J.Job]:
        """The bound report as the journal has it now, or None (nothing bound, gone, no journal)."""
        if self.bound is None:
            return None
        try:
            return self._journal().job(self.bound[0])
        except Exception:  # noqa: BLE001 - no journal
            return None

    def follow(self, ws) -> bool:
        """The bound report was made again: bind the open project to its new revision and project file.
        False when that report or its project is gone (the binding is left as it was)."""
        job = self.job()
        if job is None or not job.project_path or not Path(job.project_path).is_file():
            return False
        path = Path(job.project_path)
        ws.project_path = path
        self.bound = (job.id, job.revision, path)
        try:
            self.baseline = report_inputs(P.to_dict(ws, path))
        except Exception:  # noqa: BLE001 - the earlier baseline stays
            pass
        self._changed()
        return True

    # -- changes ----------------------------------------------------------------------------------------------

    def changed(self, ws) -> bool:
        """The open project differs from the bound report in something that changes a report."""
        if self.bound is None:
            return False
        return report_inputs(P.to_dict(ws, self.bound[2])) != self.baseline

    def unsaved(self, ws) -> bool:
        """Anything not saved into the bound project yet: a report change, or other (display) changes."""
        try:
            changed = self.changed(ws)
        except Exception:  # noqa: BLE001
            changed = False
        return changed or bool(ws.dirty)

    def save(self, ws, job_id: Optional[str] = None) -> bool:
        """Save the open project into the bound report's project; a report change sends the report back to
        control in Report² (a report accepted by hand is made again when the pair is accepted again, so it
        is not marked). Returns whether the report changed. Nothing bound (or another ``job_id``): nothing
        is saved, False.

        Raises :class:`Stale` when the report changed in Report² meanwhile and there is something to
        save; any error of writing the project or of the journal."""
        if self.bound is None or (job_id is not None and job_id != self.bound[0]):
            return False
        jid, revision, path = self.bound
        journal = self._journal()
        job = journal.job(jid)
        snapshot = report_inputs(P.to_dict(ws, path))
        changed = snapshot != self.baseline
        if job is None or job.revision != revision or Path(job.project_path or "") != path:
            if not changed and not ws.dirty:
                return False   # Accept already queued a new revision; the reviewed project is safe to leave
            raise Stale("This report changed while it was open. Save a copy of your additional edits, then "
                        "reload the current revision.")
        if ws.dirty or changed:
            P.save(ws, path)
            ws.project_path = path
            ws.dirty = False
        if changed and not job.edited and not MA.is_manual(job) and not journal.mark_edited(jid, revision):
            raise RuntimeError("The report changed while it was being saved. Reload it before continuing.")
        self.baseline = snapshot
        return changed

    # -- which report -----------------------------------------------------------------------------------------

    def find_for(self, project_path, names: list) -> Optional[J.Job]:
        """The Report² report of a single or double determination of the runs ``names`` (run file names): the bound
        one when it has these runs, else (nothing bound) the newest live pair processed into this project
        file, else None."""
        if self.bound is not None:
            job = self.job()
            return job if same_members(job, names) else None
        if not project_path or not names:
            return None
        try:
            here = Path(project_path).resolve()
            jobs = self._journal().jobs(include_batch=False)
        except Exception:  # noqa: BLE001 - no journal: no Report² report
            return None
        found = []
        for job in jobs:
            try:
                if job.project_path and same_members(job, names) and Path(job.project_path).resolve() == here:
                    found.append(job)
            except OSError:
                continue
        return found[-1] if found else None              # the newest, in the order they were created

    def _changed(self):
        if self._on_change is not None:
            self._on_change()

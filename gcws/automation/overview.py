"""What is in the watched folders and in their local copies, and what became of it (the Folders tab of
the Automation panel).

Everything about the watched folder comes from the journal and from the listing the watcher writes at
each look (``store.listing_path``), so that GC Workspace never reads a slow network drive itself; only
the local copy folder, which lies on this PC, is listed directly.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gcws.automation import journal as J
from gcws.automation import localcopy as LC
from gcws.automation import scanner as SC
from gcws.automation import store

ROLES = {"blank": "blank", "blank_istd": "blank + ISTD", "sample": "sample", "ladder": "alkane ladder"}
LEVEL = {J.ACCEPTED_AUTO: "ok", J.ACCEPTED_MANUAL: "ok", J.CONTROL: "warn", J.REJECTED: "bad", J.FAILED: "bad",
         J.NOT_PROCESSED: "bad", J.WAITING: "neutral", J.QUEUED: "info", J.PROCESSING: "info", J.REMOVED: "neutral"}
WORST = ("bad", "warn", "info", "ok", "neutral")


@dataclass
class Item:
    kind: str                           # "workflow" | "batch" | "run" | "file"
    name: str
    watched: str = ""                   # what the watched folder holds / what the watcher made of it
    local: str = ""                     # the local copy
    sample: str = ""
    state: str = ""
    why: str = ""
    level: str = "neutral"              # colour of the state
    path: str = ""                      # in the watched folder
    local_path: str = ""
    workflow_id: str = ""
    batch_id: Optional[int] = None
    job_id: str = ""
    children: list = field(default_factory=list)


def _runs_in(folder) -> list[str]:
    try:
        with os.scandir(folder) as it:
            return sorted((e.name for e in it if SC._is_run(e)), key=str.casefold)
    except OSError:
        return []


def _worst(levels) -> str:
    levels = set(levels)
    return next((lv for lv in WORST if lv in levels), "neutral")


def _summary(jobs: list) -> str:
    counts: dict = {}
    for j in jobs:
        label = "deleted" if j.deleted else J.STATE_LABELS.get(j.state, j.state).lower()
        counts[label] = counts.get(label, 0) + 1
    return " · ".join(f"{n} {label}" for label, n in counts.items())


def overview(journal: J.Journal, workflows: list, now: Optional[float] = None) -> list[Item]:
    """One item per workflow with a watched folder, its batch folders below it and their runs."""
    now = time.time() if now is None else now
    all_jobs = journal.jobs()
    out = []
    for wf in workflows:
        src = wf.source
        if src is None:
            continue
        out.append(_workflow(journal, wf, [j for j in all_jobs if j.workflow_id == wf.id], now))
    return out


def _workflow(journal: J.Journal, wf, jobs: list, now: float) -> Item:
    src = wf.source
    root = src.p("folder") or ""
    cp = wf.copy_step
    local_root = (cp.p("folder") or "") if cp is not None else ""
    listing = store.read_json(store.listing_path(wf.id)) or {}
    checked = listing.get("checked")
    top = Item("workflow", wf.name, path=root, local_path=local_root, workflow_id=wf.id)
    top.watched = root or "(no folder)"
    if listing and not listing.get("reachable", True):
        top.watched += " - cannot be reached now"
        top.level = "warn"
    top.local = local_root or "no local copy"
    top.why = (f"the watcher last looked {J.ago(checked, now)}" if checked else
               "the watcher has not looked at it yet (start the watcher)")
    if not wf.enabled:
        top.state = "inactive"
    batches = {b["folder_key"]: b for b in journal.batches(wf.id)}
    entries = {SC.folder_key(f["path"]): f for f in listing.get("folders") or [] if f.get("path")}
    by_batch: dict = {}
    for j in jobs:
        by_batch.setdefault(j.batch_id, []).append(j)
    keys = list(entries) + [k for k in batches if k not in entries]
    local_seen = set()
    for key in keys:
        f, b = entries.get(key, {}), batches.get(key, {})
        if b.get("deleted") or (b.get("missing") and not f):
            # deleted (from the disk, or in Report²): not listed, nor is its local copy
            lp = b.get("local_folder") or (str(LC.local_batch(local_root, root, b["folder"])) if local_root else "")
            if lp:
                local_seen.add(SC.folder_key(lp))
            continue
        item = _batch(journal, wf, f, b, by_batch.get(b.get("id"), []), root, local_root, now)
        if item is None:
            continue
        if item.local_path:
            local_seen.add(SC.folder_key(item.local_path))
        top.children.append(item)
    # what lies in the local copy folder but in no batch the watcher knows (older copies)
    if local_root and os.path.isdir(local_root):
        for folder in SC.batch_folders(local_root, int(src.p("depth") or 0), "*", 0):
            if SC.folder_key(folder) in local_seen:
                continue
            item = Item("batch", folder.name, watched="—", local="only in the local copy", local_path=str(folder),
                        workflow_id=wf.id)
            item.children = [Item("run", n, watched="—", local="copied", local_path=str(folder / n),
                                  workflow_id=wf.id) for n in _runs_in(folder)]
            top.children.append(item)
    top.children.sort(key=lambda it: it.name.casefold())
    if not top.children:
        top.why += "; nothing found yet"
    return top


def _batch(journal: J.Journal, wf, f: dict, b: dict, jobs: list, root: str, local_root: str,
           now: float) -> Optional[Item]:
    src = wf.source
    path = f.get("path") or b.get("folder") or ""
    if not path:
        return None
    item = Item("batch", f.get("name") or b.get("name") or Path(path).name, path=path, workflow_id=wf.id,
                batch_id=b.get("id"))
    status = f.get("status")
    if status == "old":
        item.watched = f"not changed for more than {src.p('ignore_older_days')} days: not looked at"
    elif status == "name":
        item.watched = f"name does not match '{src.p('pattern')}': not looked at"
    elif status == "batch":
        item.watched = "watched" + (" (sequence log)" if f.get("has_log") else " (no sequence log)")
    else:
        item.watched = "seen at an earlier look"
    runs = journal.runs(b["id"]) if b.get("id") is not None else {}
    lp = b.get("local_folder") or (str(LC.local_batch(local_root, root, path)) if local_root else "")
    local_names = set(_runs_in(lp)) if lp and os.path.isdir(lp) else set()
    if local_root:
        item.local_path = lp
        copied = sum(1 for r in runs.values() if r.get("copied_fp") and r.get("copied_fp") == r.get("fingerprint"))
        finished = sum(1 for r in runs.values() if r.get("state") == "ready")
        if lp and os.path.isdir(lp):
            item.local = f"{copied} of {finished} finished run(s) copied" if finished else \
                f"{len(local_names)} run(s) in the local copy"
        else:
            item.local = "not copied yet" if runs else "—"
    samples = [j for j in jobs if not j.is_batch]
    live = [j for j in samples if not j.deleted]
    item.sample = f"{len(live)} sample(s)" if live else ""
    item.state = _summary(samples)
    item.level = _worst(LEVEL.get(j.state, "neutral") for j in live)
    report = next((j for j in jobs if j.is_batch and not j.deleted), None)
    if report is not None:
        item.job_id = report.id
    quiet_in = f.get("quiet_in")
    if quiet_in and any(j.state == J.WAITING for j in live):
        item.why = (f"no sequence log: processed when nothing changed for {src.p('quiet_min')} min "
                    f"(in about {max(1, math.ceil(quiet_in / 60))} min) - or add it to the queue")
    elif report is not None:
        item.why = f"batch report: {report.label.lower()}"
    # the runs
    by_member, by_blank = {}, {}
    for j in samples:
        for m in j.members or []:
            by_member.setdefault(str(m).casefold(), j)
        for names in (j.blanks or {}).values():
            for m in names:
                by_blank.setdefault(str(m).casefold(), []).append(j)
    names_seen = set()
    for stem, r in sorted(runs.items(), key=lambda kv: Path(kv[1].get("path") or kv[0]).name.casefold()):
        name = Path(r.get("path") or stem).name
        names_seen.add(name.casefold())
        run = Item("run", name, path=r.get("path") or "", workflow_id=wf.id, batch_id=b.get("id"))
        if r.get("baseline"):
            run.watched = "there before watching: not processed"
        elif r.get("state") == "ready":
            run.watched = "finished"
        elif r.get("state") == "baseline":
            run.watched = "there before watching: not processed"
        else:
            run.watched = "being written"
        if r.get("readded"):
            run.watched += ", put in again"
        if local_root:
            run.local_path = str(Path(lp) / name) if lp else ""
            if r.get("copied_fp") and r.get("copied_fp") == r.get("fingerprint"):
                run.local = "copied"
            elif r.get("copy_error"):
                run.local = f"not copied: {r['copy_error']}"
            elif name.casefold() in {n.casefold() for n in local_names}:
                run.local = "an older copy" if r.get("copied_fp") else "in the local copy"
            elif r.get("state") == "ready" and not r.get("baseline"):
                run.local = "waiting to be copied"
            else:
                run.local = "—"
        j = by_member.get(name.casefold())
        if j is not None:
            run.sample, run.job_id = j.group_name, j.id
            run.state = ("deleted - " if j.deleted else "") + J.STATE_LABELS.get(j.state, j.state)
            run.why = j.reason or ""
            run.level = "neutral" if j.deleted else LEVEL.get(j.state, "neutral")
        elif by_blank.get(name.casefold()):
            owners = by_blank[name.casefold()]
            run.sample = f"{ROLES.get(r.get('role'), 'blank')} of " + ", ".join(o.group_name for o in owners[:3]) + \
                (" ..." if len(owners) > 3 else "")
        else:
            run.sample = ROLES.get(r.get("role") or "", r.get("role") or "")
            if not r.get("baseline") and (r.get("role") or "sample") == "sample" and r.get("state") == "ready":
                run.why = "no sample planned for it yet"
        item.children.append(run)
    for name in sorted((n for n in local_names if n.casefold() not in names_seen), key=str.casefold):
        item.children.append(Item("run", name, watched="—", local="only in the local copy",
                                  local_path=str(Path(lp) / name), workflow_id=wf.id, batch_id=b.get("id")))
    for name in f.get("other") or []:
        item.children.append(Item("file", name, watched="other file", workflow_id=wf.id, batch_id=b.get("id"),
                                  path=str(Path(path) / name)))
    return item

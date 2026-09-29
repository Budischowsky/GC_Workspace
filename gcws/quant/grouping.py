"""Replicate groups suggested from the run names (A/B determinations of one sample).

Used by the Replicates panel and by unattended processing; works on plain data, so a batch
can be planned before its runs are loaded.
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Callable

from gcws.io import sequence


def group_name(first_stem: str) -> str:
    """The group's name from its first run: without injection prefix and replicate letter."""
    name = re.sub(r"^\d+[_\- ]+", "", first_stem)            # injection prefix
    return re.sub(r"[_\- ]+([A-Za-z]|\d{1,2})$", "", name)  # replicate letter


def suggest_groups(file_names: dict, labels: dict, roles: dict, order: list, existing: list | None = None,
                   new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:8]) -> tuple[list[dict], int]:
    """``existing`` groups plus the suggested ones; returns ``(groups, number added)``.

    ``file_names`` maps run id -> file or folder name (``07_..._A.D``), ``labels`` id -> display
    name, ``roles`` id -> role and ``order`` the ids in injection order. Runs whose names differ
    only by the replicate suffix form a group; a run waiting alone in a single-determination group
    joins its partner; every other sample becomes a single determination."""
    from pathlib import PurePath
    groups = copy.deepcopy(existing or [])
    taken = {m for g in groups for m in g["members"]}
    added = 0
    for ids in sequence.suggest_replicates(file_names):
        if any(len(g["members"]) == 1 and g["members"][0] in ids for g in groups):
            groups = [g for g in groups if not (len(g["members"]) == 1 and g["members"][0] in ids)]
            taken = {m for g in groups for m in g["members"]}
        ids = [i for i in order if i in ids and i not in taken]
        if len(ids) < 2:
            continue
        groups.append({"id": new_id(), "name": group_name(PurePath(file_names[ids[0]]).stem), "members": ids,
                       "policy": "all"})
        taken.update(ids)
        added += 1
    for rid in order:                                  # samples without partner: single determination
        if roles.get(rid) == sequence.SAMPLE and rid not in taken:
            groups.append({"id": new_id(), "name": labels.get(rid, rid), "members": [rid], "policy": "all"})
            added += 1
    return groups, added


def for_workspace(ws, existing: list | None = None) -> tuple[list[dict], int]:
    """:func:`suggest_groups` for the runs loaded in ``ws``."""
    states = ws.states()
    order = ws.ordered_ids_by_injection()
    order += [s.id for s in states if s.id not in order]
    return suggest_groups({s.id: s.run.path.name for s in states}, {s.id: s.name for s in states},
                          {s.id: s.role for s in states}, order,
                          ws.replicate_groups if existing is None else existing)

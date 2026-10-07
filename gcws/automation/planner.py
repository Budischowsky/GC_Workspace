"""Which samples of a batch folder can be processed now (pure; no files are read).

A sample is a replicate group (the A/B determinations of one sample number). It is ready when
every determination and every blank it will be compared with are finished. With a sequence log
the planned runs are known in advance (07 A waits for 11 B, then for the blank 13_EtOH after
it); without one, the folder must have been quiet for a while. Blanks come from the batch
folder only. A sample without the required blank is not processed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from gcws.io import sequence as SQ
from gcws.quant.grouping import group_name, suggest_groups

READY = "ready"
WAITING = "waiting"
NOT_PROCESSED = "not_processed"
BASELINE = "baseline"


@dataclass
class GroupPlan:
    key: str                         # stable sample key (sample number + name without replicate letter)
    name: str
    members: list[str]               # run stems, injection order (planned)
    present: list[str]
    blanks: dict                     # "blank" / "blank_istd" -> stems (all members)
    state: str
    reason: str = ""


@dataclass
class BatchPlan:
    groups: list[GroupPlan] = field(default_factory=list)
    finished: bool = False           # the sequence ended (log) or the folder has been quiet
    order: list[str] = field(default_factory=list)
    roles: dict = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        """Nothing more will arrive and no sample is waiting."""
        return self.finished and not any(g.state == WAITING for g in self.groups)


def sample_key(name: str) -> str:
    return (SQ.sample_number(name) + ":" + SQ.replicate_stem(name)).strip(":")


def _needed(require: str, b: list, bi: list) -> tuple[bool, str]:
    """Whether the suggested blanks satisfy ``require``; else the missing kind."""
    if require == "none":
        return True, ""
    if require == "blank":
        return bool(b), "Blank"
    if require == "blank_istd":
        return bool(bi), "Blank+ISTD"
    if require == "both":
        return bool(b and bi), "Blank and Blank+ISTD" if not (b or bi) else ("Blank" if not b else "Blank+ISTD")
    return bool(b or bi), "Blank or Blank+ISTD"


def plan_batch(present: dict, seq: Optional[SQ.SequenceInfo], *, quiet: bool, require: str = "either",
               baseline: frozenset | set = frozenset()) -> BatchPlan:
    """The samples of one batch folder and their state.

    ``present``: run stem -> {"name": file name, "ready": bool, "copying": bool (finished, but not yet
    in the local copy)}. ``seq``: the folder's sequence
    log (or None). ``quiet``: the folder has not changed for the quiet time. ``require``: the
    blanks a sample needs (blank | blank_istd | both | either | none). ``baseline``: stems that
    were there before the workflow started watching (their samples are not processed)."""
    seq = seq or SQ.SequenceInfo()
    finished = seq.finished or quiet
    planned_names = {ln.stem: ln.datafile for ln in seq.lines}
    if seq.lines and not finished:
        names = dict(planned_names)
    else:
        names = {s: planned_names.get(s, v["name"]) for s, v in present.items()} if seq.lines else \
            {s: v["name"] for s, v in present.items()}
    for s, v in present.items():
        names.setdefault(s, v["name"])
    order = [s for s in seq.stems if s in names] + sorted(
        (s for s in names if s not in planned_names), key=lambda s: SQ.order_key(names[s]))
    roles = {s: SQ.classify_role(names[s]) for s in order}
    groups, _ = suggest_groups(names, {s: group_name(names[s].rsplit(".", 1)[0]) for s in order}, roles, order,
                               new_id=lambda: "")
    plan = BatchPlan(finished=finished, order=order, roles=roles)
    ready = lambda s: s in present and bool(present[s].get("ready"))
    where = lambda s: (" (planned)" if s not in present else
                       " (being copied)" if present[s].get("copying") else " (acquiring)")
    for g in groups:
        members = g["members"]
        first = names[members[0]]
        b_all, bi_all = [], []
        for m in members:
            b, bi = SQ.suggest_blanks(m, names, roles, order)
            b_all += [x for x in b if x not in b_all]
            bi_all += [x for x in bi if x not in bi_all]
        gp = GroupPlan(sample_key(first), g["name"] or group_name(first.rsplit(".", 1)[0]), members,
                       [m for m in members if m in present], {"blank": b_all, "blank_istd": bi_all}, WAITING)
        if members and all(m in baseline for m in members):
            gp.state, gp.reason = BASELINE, "already there when the workflow started"
            plan.groups.append(gp)
            continue
        ok, missing = _needed(require, b_all, bi_all)
        waiting = [m for m in members if not ready(m)]
        waiting_blanks = [x for x in b_all + bi_all if not ready(x)]
        if waiting:
            m = waiting[0]
            gp.reason = (f"waiting for {names[m]}" + where(m)
                         + (f" and {len(waiting) - 1} more" if len(waiting) > 1 else ""))
        elif not ok:
            if finished or seq.lines:
                gp.state, gp.reason = NOT_PROCESSED, f"no {missing} in the batch folder"
            else:
                gp.reason = f"waiting for a {missing} (or until the folder is quiet)"
        elif waiting_blanks:
            x = waiting_blanks[0]
            gp.reason = f"waiting for the blank {names[x]}" + where(x)
        elif not seq.lines and not quiet:
            gp.reason = "waiting until the folder is quiet (no sequence log)"
        else:
            gp.state, gp.reason = READY, ""
        plan.groups.append(gp)
    return plan

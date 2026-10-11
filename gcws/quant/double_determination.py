"""A double determination: one replicate group of two runs, as the analyst reviews and accepts it.

The group is a plain dict saved with the project (``ws.replicate_groups``): ``id``, ``name``, ``members``,
``policy``, ``single`` (made a single determination by hand), ``reported`` (the run reported of a pair under
Single determination only, when the analyst switched it), the analyst's edits under the key of the quantification mode and unit
(:func:`gcws.quant.duplicate_view.edits_key`) and the ``signoff`` (who accepted, when, how many red rows
were left open). This module owns the changes to it, and the one rule that binds them: any change of
what was accepted (an edit, a reset, other members, another validity rule, a name of a substance)
makes the acceptance **stale**. The signoff is kept, to say who accepted.

Every change takes the list of groups and returns a new one (the caller pushes it as one undo step);
the lists passed in are never modified. Qt-free: the DD panel, the Replicates panel and the reports
are its callers.
"""
from __future__ import annotations

import copy
import uuid

from gcws.quant import duplicate_view as DV


# -- reading -----------------------------------------------------------------------------------------

def is_accepted(group: dict | None) -> bool:
    """Accepted and not changed since."""
    signoff = (group or {}).get("signoff") or {}
    return bool(signoff) and not signoff.get("stale")


def edits_of(group: dict | None, quant: dict, unit: str) -> dict:
    """The analyst's edits of ``group`` for the quantification ``quant`` in ``unit`` (a copy)."""
    return dict((group or {}).get(DV.edits_key(quant, unit)) or {})


def evaluate(ws, members: list, edits: dict, labels=("A", "B"), policy: str = "all"):
    """``(base rows, verdicts, rows with the edits, problems)`` of the determinations ``members``: the
    comparison the DD panel shows and a report of the pair reports."""
    rows, problems = DV.compute(ws, members, policy)
    limit, rl = DV.limits(ws)
    unit = ws.quant_unit()
    flags = _single_flags(ws, rows) if len(members) == 1 else [""] * len(rows)
    verdicts = [DV.plain_verdict(r, limit, rl, tuple(labels), unit, f) for r, f in zip(rows, flags)]
    return rows, verdicts, DV.apply_edits(rows, verdicts, edits, rt_tolerance(ws.quant)), problems


def _single_flags(ws, rows: list) -> list:
    """The flags of each row's substance (none for an ISTD): they colour a single determination's rows yellow."""
    from gcws.quant import substance_flags as SF
    index = SF.formula_index(ws)
    out = []
    for r in rows:
        if any((s or {}).get("istd") for s in (r.get("source1"), r.get("source2"))):
            out.append("")
            continue
        name, cas = r.get("name", ""), r.get("cas", "")
        out.append(SF.flags(name, cas, SF.formula_from(index, name, cas))[0])
    return out


def rt_tolerance(quant: dict) -> float:
    """The RT tolerance (min) that pairs an edit with its row: the one that paired the row."""
    from gcws.quant import limits as QL
    return QL.of_quant(quant).rt_tolerance


# -- changes -----------------------------------------------------------------------------------------

def _stale(group: dict) -> None:
    if group.get("signoff"):
        group["signoff"] = dict(group["signoff"], stale=True)


def _copy(groups: list, group_id: str) -> tuple[list, dict]:
    out = copy.deepcopy(groups)
    return out, next(g for g in out if g["id"] == group_id)


def stale(groups: list, group_id: str) -> list:
    """Something outside the group changed what was accepted (a name or CAS of a substance)."""
    out, g = _copy(groups, group_id)
    _stale(g)
    return out


def method_changed(groups: list, before: dict, after: dict, names: dict) -> list:
    """The method's quantification changed from ``before`` to ``after`` (``ws.quant``; ``names``: run id ->
    file name): an accepted group whose determination changed (Single determination only toggled: other
    runs reported, another kind) is stale."""
    from gcws.quant import determination as DT
    out = copy.deepcopy(groups)
    for g in out:
        old, new = DT.of(before, g, names), DT.of(after, g, names)
        if (old.kind, old.reported, old.control) != (new.kind, new.reported, new.control):
            _stale(g)
    return out


def pair(groups: list, members: list, name: str) -> list:
    """``groups`` with ``members`` as one group of their own: the group of ``members[0]`` (a pair or a
    single) is reused, else a new one named ``name``. A run belongs to one pair or single, so the
    other pairs and singles give the members up; groups of three or more keep theirs."""
    out = copy.deepcopy(groups)
    target = next((g for g in out if members[0] in g["members"] and len(g["members"]) <= 2), None)
    if target is None:
        target = {"id": uuid.uuid4().hex[:8], "name": name, "members": [], "policy": "all"}
        out.append(target)
    for g in out:
        if g is not target and len(g["members"]) <= 2:
            g["members"] = [m for m in g["members"] if m not in members]
    out = [g for g in out if g["members"] or g is target]
    if list(target["members"]) != list(members) or target.get("policy", "all") != "all" or target.get("single"):
        _stale(target)
    if list(target["members"]) != list(members):
        target.pop("reported", None)
    target["members"] = list(members)
    target["policy"] = "all"
    target.pop("single", None)
    return out


def make_single(groups: list, run: str, name: str) -> list:
    """``groups`` with ``run`` as a single determination made by hand: taken out of its pair (the
    group is reused, the partner stays unused) or a new group named ``name``. A suffixed run made
    single is a single determination, not an incomplete double determination."""
    was = next((g for g in groups if run in g["members"] and len(g["members"]) <= 2), None)
    if was is not None and list(was["members"]) == [run] and was.get("single"):
        return copy.deepcopy(groups)
    out = pair(groups, [run], name)
    g = next(g for g in out if g["members"] == [run])
    if was is not None and list(was["members"]) == [run]:
        _stale(g)                                # an incomplete double determination signed off before
    g["single"] = True
    return out


def set_reported(groups: list, group_id: str, run: str) -> list:
    """``run`` is the reported determination of the pair (Single determination only), the other one the
    control: another run in the report, so an acceptance no longer holds."""
    out, g = _copy(groups, group_id)
    if run not in g["members"]:
        raise ValueError(f"{run} is not a determination of {g['name']}")
    if g.get("reported") != run:
        g["reported"] = run
        _stale(g)
    return out


def set_edits(groups: list, group_id: str, key: str, changes: list, user: str, at: str) -> tuple[list, list]:
    """``(groups, log lines)`` after the ``(row, field, value)`` changes (``value`` None: the field's
    edit removed) of the edits under ``key``; each log line is ``(what, before, after)``."""
    out, g = _copy(groups, group_id)
    edits = g.setdefault(key, {})
    logs = []
    for row, field, value in changes:
        k = row.get("edit_key") or DV.edit_key(row["rt"])
        e = edits.setdefault(k, {"rt": float(row["rt"])})
        before = row.get(field) if field not in DV.NUMERIC_EDITS else \
            row.get("edited", {}).get(field, row.get(field))
        if value is None:
            e.pop(field, None)
        else:
            e[field] = value
        e["by"], e["at"] = user, at
        if not any(f in e for f in DV.NUMERIC_EDITS + DV.ROW_FLAGS):
            edits.pop(k, None)
        name = row.get("name") or f"RT {row['rt']:.3f}"
        if field == "deleted":
            logs.append((f"{name} (RT {row['rt']:.3f})", "", "row deleted" if value else "row restored"))
        else:
            logs.append((f"{name} (RT {row['rt']:.3f}): {field}", "" if before is None else str(before),
                         "reset" if value is None else str(value)))
    _stale(g)
    return out, logs


def reset_edits(groups: list, group_id: str, key: str, edit_key: str | None = None) -> list:
    """The analyst's edit ``edit_key`` under ``key`` removed (None: all of them)."""
    out, g = _copy(groups, group_id)
    if edit_key is None:
        g[key] = {}
    else:
        g.get(key, {}).pop(edit_key, None)
    _stale(g)
    return out


def accept(groups: list, group_id: str, user: str, at: str, n_open: int) -> list:
    """The analyst accepts the pair, ``n_open`` red rows left to the default rule."""
    out, g = _copy(groups, group_id)
    g["signoff"] = {"by": user, "at": at, "open": n_open}
    return out


def set_members(groups: list, group_id: str, members: list) -> list:
    """Other determinations in the group: an acceptance no longer holds."""
    out, g = _copy(groups, group_id)
    if list(g["members"]) != list(members):
        g["members"] = list(members)
        g.pop("reported", None)
        _stale(g)
    return out


def set_policy(groups: list, group_id: str, policy: str) -> list:
    """Another validity rule: an acceptance no longer holds."""
    out, g = _copy(groups, group_id)
    if g.get("policy", "all") != policy:
        g["policy"] = policy
        _stale(g)
    return out


def rename(groups: list, group_id: str, name: str) -> list:
    """Another name: nothing that was accepted changes."""
    out, g = _copy(groups, group_id)
    g["name"] = name
    return out

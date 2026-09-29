"""The feature double determination on the workspace (shared by the panel, the reports and the automation).

* :func:`build` reads the determinations and returns the feature table with its proposals
  (gap fills, ...); it changes nothing. The ids of the replicate group's previous table
  (``group["features"]["ids"]``) are kept.
* :func:`apply` makes the automatic proposals as one undoable, audited step (manual events and
  identifications of the runs, like the analyst's own edits).
* :func:`run` = build, apply, build again: the table as the reports see it.
"""
from __future__ import annotations

import copy
from typing import Optional

from gcws.features import align as AL
from gcws.features import gapfill as GF
from gcws.features import ids as IDS
from gcws.features.inputs import collect
from gcws.features.model import FeatureTable, Settings


def settings(ws) -> Settings:
    return Settings.from_dict((getattr(ws, "quant", None) or {}).get("features"))


def pairing(ws) -> str:
    return settings(ws).pairing


def quant_key(ws) -> str:
    from gcws.quant.service import quant_detector
    return quant_detector(ws.quant)


def group_of(ws, members: list[str]) -> Optional[dict]:
    want = [m for m in members if m in ws.runs]
    for g in ws.replicate_groups:
        if [m for m in g.get("members", []) if m in ws.runs] == want:
            return g
    return None


def noise_pp(ws, run_id: str, key: str) -> float:
    """Peak-to-peak noise of ``key`` in ``run_id`` (cached per integration)."""
    from gcws.signal.noise import estimate
    st = ws.runs[run_id]
    sig = st.run.signal(key)
    if sig is None or sig.n < 20:
        return 0.0
    t_from = ws.solvent_cut(st, key)
    cache = getattr(ws, "_feature_noise", None)
    if cache is None:
        cache = ws._feature_noise = {}
    ck = (run_id, key, t_from)
    if ck not in cache:
        cache[ck] = float(estimate(sig.rt, sig.y, t_from).pp)
    return cache[ck]


def build(ws, members: list[str], group: Optional[dict] = None, cfg: Optional[Settings] = None,
          key: Optional[str] = None, *, gapfill: bool = True) -> FeatureTable:
    """The feature table of the determinations ``members`` (the first is the reference)."""
    cfg = cfg or settings(ws)
    key = key or quant_key(ws)
    group = group if group is not None else group_of(ws, members)
    inputs = collect(ws, [m for m in members if m in ws.runs], key, cfg.noise_floor)
    table = AL.align(inputs, cfg)
    previous = ((group or {}).get("features") or {}).get("ids")
    IDS.stable_ids(table.features, previous, cfg.rt_tol)
    table.inputs = inputs
    if gapfill and cfg.gap_fill and len(inputs) > 1:
        runs = {r.run_id: ws.runs[r.run_id].run for r in inputs}
        noise = {r.run_id: noise_pp(ws, r.run_id, key) for r in inputs}
        GF.fill_table(table, runs, noise, cfg)
    return table


def proposals(table: FeatureTable, kinds=("gapfill", "identity"), auto_only: bool = True) -> list:
    return [p for f in table.features for p in f.proposals if p.kind in kinds and (p.auto or not auto_only)]


def commands(ws, props: list) -> list:
    """Undo commands for ``props``: one ManualEventsCommand per run and signal (the events are
    added to the run's list), one IdentCommand per run and signal."""
    from gcws.ui.undo import IdentCommand, ManualEventsCommand
    events: dict = {}
    idents: dict = {}
    for p in props:
        if p.event is not None:
            events.setdefault((p.run_id, p.key), []).append(p)
        if p.ident is not None:
            idents.setdefault((p.run_id, p.key), []).append(p)
    out = []
    for (rid, key), ps in events.items():
        st = ws.runs.get(rid)
        if st is None:
            continue
        text = ps[0].text if len(ps) == 1 else f"{len(ps)} gap fills (double determination)"
        out.append(ManualEventsCommand(ws, rid, key, st.events(key) + [p.event for p in ps], text))
    for (rid, key), ps in idents.items():
        if rid not in ws.runs:
            continue
        text = ps[0].text if len(ps) == 1 else f"{len(ps)} names (double determination)"
        out.append(IdentCommand(ws, rid, key, [(p.rt, p.ident) for p in ps], text))
    return out


def apply(ws, table: FeatureTable, props: Optional[list] = None, stack=None, label: str = "") -> int:
    """Make ``props`` (default: the table's automatic ones) as one undo step; returns their number."""
    from gcws.ui.undo import MultiCommand
    props = proposals(table) if props is None else props
    if not props:
        return 0
    cmds = commands(ws, props)
    if not cmds:
        return 0
    names = " / ".join(ws.runs[m].name for m in table.members if m in ws.runs)
    n_gap = sum(1 for p in props if p.kind == "gapfill")
    n_id = sum(1 for p in props if p.kind == "identity")
    parts = [f"{n_gap} gap fill(s)" if n_gap else "", f"{n_id} name(s)" if n_id else ""]
    label = label or "double determination: " + ", ".join(x for x in parts if x)
    stack = stack or ws.undo_group.activeStack() or ws.project_undo
    stack.push(MultiCommand(label, cmds))
    for p in props:
        ws.log("Double determination (features)", names, p.text)
    return len(props)


def remember_ids(ws, members: list[str], table: FeatureTable) -> None:
    """Store the table's ids in the replicate group (not an undo step: bookkeeping only)."""
    g = group_of(ws, members)
    if g is None:
        return
    records = table.id_records()
    if (g.get("features") or {}).get("ids") != records:
        g.setdefault("features", {})["ids"] = records
        if hasattr(ws, "dirty"):
            ws.dirty = True


def run(ws, members: list[str], cfg: Optional[Settings] = None, *, apply_auto: Optional[bool] = None,
        stack=None) -> FeatureTable:
    """Build the table, make its automatic proposals (``cfg.apply_auto``), build again, keep the ids."""
    cfg = cfg or settings(ws)
    group = group_of(ws, members)
    table = build(ws, members, group, cfg)
    auto = cfg.apply_auto if apply_auto is None else apply_auto
    if auto and apply(ws, table, stack=stack):
        remember_ids(ws, members, table)
        table = build(ws, members, group_of(ws, members), cfg)
    remember_ids(ws, members, table)
    return table


def copy_settings(ws) -> dict:
    return copy.deepcopy((ws.quant or {}).get("features") or {})

"""The feature double determination on the workspace (shared by the panel, the reports and the automation).

:func:`build` reads the determinations and returns the feature table; it changes nothing.
The ids of the previous table of the replicate group (``group["features"]["ids"]``) are kept.
"""
from __future__ import annotations

import copy
from typing import Optional

from gcws.features import align as AL
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


def build(ws, members: list[str], group: Optional[dict] = None, cfg: Optional[Settings] = None,
          key: Optional[str] = None) -> FeatureTable:
    """The feature table of the determinations ``members`` (the first is the reference)."""
    cfg = cfg or settings(ws)
    key = key or quant_key(ws)
    group = group if group is not None else group_of(ws, members)
    inputs = collect(ws, [m for m in members if m in ws.runs], key, cfg.noise_floor)
    table = AL.align(inputs, cfg)
    previous = ((group or {}).get("features") or {}).get("ids")
    IDS.stable_ids(table.features, previous, cfg.rt_tol)
    table.inputs = inputs
    return table


def remember_ids(group: dict, table: FeatureTable) -> dict:
    """``group`` with the table's ids stored (a copy; the caller pushes it undoably)."""
    g = copy.deepcopy(group)
    g.setdefault("features", {})["ids"] = table.id_records()
    return g

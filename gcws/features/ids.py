"""Stable feature ids (F-001, ...).

The analyst's decisions and edits refer to a feature by its id, so an id must survive a
re-integration or a new library search, which change peak indices and names but not the
substance. A new feature takes the id of the previous feature at the same reference RT
(within ``rt_tol``) with at least half of its main ions in common; everything else gets the
next free number. The first table is numbered in RT order.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

_NUM = re.compile(r"F-(\d+)$")


def fmt(n: int) -> str:
    return f"F-{n:03d}"


def number(fid: str) -> int:
    m = _NUM.match(fid or "")
    return int(m.group(1)) if m else 0


def _ions_agree(a: Iterable[int], b: Iterable[int]) -> bool:
    a, b = set(a or ()), set(b or ())
    if not a or not b:
        return True                      # no MS: the RT decides
    return len(a & b) >= 0.5 * min(len(a), len(b))


def stable_ids(features, previous: Optional[list[dict]], rt_tol: float = 0.05) -> None:
    """Set ``feature.id`` of every feature (in place)."""
    previous = [p for p in (previous or []) if number(p.get("id", ""))]
    pairs = []
    for k, f in enumerate(features):
        ions = f.top_ions()
        for q, p in enumerate(previous):
            d = abs(float(p.get("rt", 0.0)) - f.rt)
            if d <= rt_tol and _ions_agree(ions, p.get("ions")):
                pairs.append((d, k, q))
    pairs.sort()
    taken_f, taken_p = set(), set()
    for _d, k, q in pairs:
        if k in taken_f or q in taken_p:
            continue
        features[k].id = previous[q]["id"]
        taken_f.add(k)
        taken_p.add(q)
    nxt = max([number(p["id"]) for p in previous] + [0]) + 1
    for k, f in enumerate(features):
        if k not in taken_f:
            f.id = fmt(nxt)
            nxt += 1

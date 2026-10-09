"""Grid search with batch-grouped cross-validation (spec §8). The choice uses only the cross-validation
batches; the locked test batches are scored afterwards for the report. A proposal is recommended only when it
beats the current settings and no batch loses more than ``max_loss``."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable, Optional

from gcws.learn.folds import make_split


@dataclass
class FitResult:
    target: str
    current: dict
    best: dict
    cv_current: Optional[float] = None
    cv_best: Optional[float] = None
    test_current: Optional[float] = None
    test_best: Optional[float] = None
    per_batch: dict = field(default_factory=dict)       # batch -> {"current", "best", "test": bool}
    table: list = field(default_factory=list)           # [{"params", "cv"}] in grid order
    accept_recommended: bool = False
    notes: list = field(default_factory=list)


def grid(space: dict[str, list]) -> list[dict]:
    """All combinations; the first value of each parameter first (the preference order of the tie-break)."""
    keys = list(space)
    return [dict(zip(keys, values)) for values in itertools.product(*(space[k] for k in keys))]


def _weighted(scores: dict[str, Optional[float]], sizes: dict[str, int], batches: list[str]) -> Optional[float]:
    pairs = [(scores[b], sizes[b]) for b in batches if scores.get(b) is not None]
    n = sum(w for _, w in pairs)
    return sum(s * w for s, w in pairs) / n if n else None


def fit(target: str, space: dict[str, list], current: dict, batches: dict[str, list],
        evaluate: Callable[[dict, list], Optional[float]], *, tie: float = 0.005, max_loss: float = 0.02,
        progress: Callable[[str], None] = lambda t: None) -> FitResult:
    split = make_split(list(batches))
    cv_batches = [b for fold in split.folds for b in fold]
    sizes = {b: len(runs) for b, runs in batches.items()}
    candidates = grid(space)
    if current not in candidates:
        candidates = [current] + candidates
    per: list[dict[str, Optional[float]]] = []
    for i, params in enumerate(candidates, 1):
        progress(f"{target}: candidate {i}/{len(candidates)} {params}")
        scores = {}
        for b, runs in batches.items():
            try:
                scores[b] = evaluate(params, runs)
            except Exception:  # noqa: BLE001 - a parameter set that cannot be evaluated is never chosen
                scores[b] = None
        per.append(scores)

    def cv(scores) -> Optional[float]:
        folds = [_weighted(scores, sizes, f) for f in split.folds]
        folds = [f for f in folds if f is not None]
        if not folds or any(scores.get(b) is None for b in cv_batches):
            return None
        return sum(folds) / len(folds)

    cvs = [cv(s) for s in per]
    table = [{"params": p, "cv": c} for p, c in zip(candidates, cvs)]
    scored = [c for c in cvs if c is not None]
    result = FitResult(target, current, current, table=table)
    if not split.test:
        result.notes.append("only one batch: there is nothing to hold out, the scores are not a test")
    if len(split.folds) < 3:
        result.notes.append(f"only {len(cv_batches)} batches for cross-validation")
    if not scored:
        result.notes.append("no parameter set could be scored")
        return result
    top = max(scored)
    best_i = next(i for i, c in enumerate(cvs) if c is not None and c >= top - tie)
    cur_i = candidates.index(current)
    result.best = candidates[best_i]
    result.cv_current, result.cv_best = cvs[cur_i], cvs[best_i]
    result.test_current = _weighted(per[cur_i], sizes, split.test)
    result.test_best = _weighted(per[best_i], sizes, split.test)
    result.per_batch = {b: {"current": per[cur_i].get(b), "best": per[best_i].get(b), "test": b in split.test}
                        for b in sorted(batches)}
    losers = [b for b in cv_batches if per[cur_i].get(b) is not None and per[best_i].get(b) is not None
              and per[cur_i][b] - per[best_i][b] > max_loss]
    for b in losers:
        result.notes.append(f"batch {b} loses {per[cur_i][b] - per[best_i][b]:.3f} with the proposal")
    better = result.cv_current is None or result.cv_best > result.cv_current
    result.accept_recommended = bool(better and not losers and result.best != current)
    return result

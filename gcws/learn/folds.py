"""Splits for fitting (spec §8): whole batches only, a locked test set and k cross-validation folds.
Deterministic: the order comes from a hash of the batch name, not from the order the batches were found."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field


@dataclass
class Split:
    test: list[str] = field(default_factory=list)          # locked test batches (reported, never fitted on)
    folds: list[list[str]] = field(default_factory=list)   # each fold = the batches held out in one CV round


def make_split(batches: list[str], test_share: float = 0.2, k: int = 5) -> Split:
    order = sorted(set(batches), key=lambda b: hashlib.sha1(b.encode("utf-8")).hexdigest())
    n_test = max(1, math.ceil(test_share * len(order))) if len(order) >= 2 else 0
    test, rest = order[:n_test], order[n_test:]
    k = max(1, min(k, len(rest)))
    folds = [rest[i::k] for i in range(k)] if rest else []
    return Split(sorted(test), [sorted(f) for f in folds])

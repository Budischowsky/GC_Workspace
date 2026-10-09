"""Family 4: families learned from the analysts' group labels. A family is the program evidence (peak names and
class hints) that the analysts consistently put under one group label, plus the analysts' own sum wording.
Nothing here is a hand-written list: the table is learned, written into the proposal and approved by the user."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional

from gcws.learn.score import _words, same_family

_EMPTY = ("", "-")


def _evidence(peak: dict) -> list[tuple[str, str]]:
    out = []
    for kind, key in (("names", "name"), ("hints", "class_hint")):
        v = (peak.get(key) or "").strip()
        if v not in _EMPTY:
            out.append((kind, v))
    return out


def learn_families(runs: list, *, min_peaks: int = 5, min_share: float = 0.6,
                   min_batches: int = 2) -> list[dict]:
    """Families from ``runs`` (CachedRun with items, prog, pairs, footnotes). An evidence value becomes a member of
    a family when >= ``min_share`` of the labelled analyst peaks carrying it are that family's group label and
    there are >= ``min_peaks`` of them; a family must occur in >= ``min_batches`` batches."""
    by_label: dict[str, Counter] = defaultdict(Counter)
    total: Counter = Counter()
    support: Counter = Counter()
    batches: dict[str, set] = defaultdict(set)
    texts: Counter = Counter()
    for run in runs:
        texts.update(t for t in getattr(run, "footnotes", []) if t.casefold().startswith("sum of"))
        for p in run.pairs:
            if p.human is None or p.program is None or p.kind != "exact":
                continue
            it = run.items[p.human]
            if not it.label:
                continue
            ev = _evidence(run.prog.peaks[p.program])
            total.update(ev)
            if it.decision == "reported_group":
                label = _words(it.label)
                by_label[label].update(ev)
                support[label] += 1
                batches[label].add(run.batch)
    families = []
    for label in sorted(by_label):
        if len(batches[label]) < min_batches:
            continue
        members = {"names": [], "hints": []}
        for (kind, value), n in sorted(by_label[label].items()):
            if n >= min_peaks and n / total[(kind, value)] >= min_share:
                members[kind].append(value)
        if not members["names"] and not members["hints"]:
            continue
        wording = [t for t, _ in texts.most_common() if same_family(label, t)]
        families.append({"label": label, "sum_text": wording[0] if wording else f"Sum of {label}", **members,
                         "support": support[label], "batches": len(batches[label])})
    return families


def family_of(peak: dict, families) -> Optional[dict]:
    """The family a program peak belongs to (by its name or class hint); with several, the best-supported one,
    ties in table order."""
    name, hint = (peak.get("name") or "").strip(), (peak.get("class_hint") or "").strip()
    hits = [f for f in families if name in f.get("names", []) or hint in f.get("hints", [])]
    if not hits:
        return None
    best = max(f["support"] for f in hits)
    return next(f for f in hits if f["support"] == best)

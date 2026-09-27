"""Peak identifications (library search results or analyst entries)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Identification:
    apex_rt: float
    name: str = ""
    cas: str = ""
    score: Optional[float] = None
    status: str = ""                   # Accepted | Uncertain | Unknown | Manual ...
    formula: str = ""
    library: str = ""
    hits: list[dict] = field(default_factory=list)
    source: str = ""                   # "EI Atlas" | "manual" | ...
    method: str = ""
    searched_at: str = ""
    spectrum_mode: str = ""
    manual: bool = False               # analyst typed name/CAS: protected from searches
    istd: str = ""                     # ISTD code bound to this peak (quantification)
    peak_id: str = ""                  # replay-stable split fragment, empty for legacy RT binding

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Identification":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


class IdentificationSet:
    """Identifications of one run/signal, bound to peaks by apex retention time."""

    def __init__(self, items=None):
        self.items: list[Identification] = list(items or [])

    def for_peak(self, peak, tol: Optional[float] = None) -> Optional[Identification]:
        peak_id = (getattr(peak, "extra", None) or {}).get("spectrum_id", "")
        if peak_id:
            return next((i for i in self.items if i.peak_id == peak_id), None)
        items = [i for i in self.items if not i.peak_id]
        if not items:
            return None
        tol = tol if tol is not None else max(0.5 * (peak.width50 or 0.0), 0.01)
        best = min(items, key=lambda i: abs(i.apex_rt - peak.apex_rt))
        return best if abs(best.apex_rt - peak.apex_rt) <= tol else None

    def bind(self, peaks) -> tuple[dict[int, Identification], list[Identification]]:
        """Map peak index -> identification; unmatched identifications are orphans."""
        used, out = set(), {}
        for i, p in enumerate(peaks):
            ident = self.for_peak(p)
            if ident is not None and id(ident) not in used:
                used.add(id(ident))
                out[i] = ident
        orphans = [x for x in self.items if id(x) not in used]
        return out, orphans

    def set(self, ident: Identification, tol: float = 0.005) -> Optional[Identification]:
        """Insert or replace the identification at ``ident.apex_rt``; returns the old one."""
        for k, x in enumerate(self.items):
            if ((ident.peak_id and x.peak_id == ident.peak_id) or
                    (not ident.peak_id and not x.peak_id and abs(x.apex_rt - ident.apex_rt) <= tol)):
                self.items[k] = ident
                return x
        self.items.append(ident)
        return None

    def remove_at(self, rt: float, tol: float = 0.005, peak_id: str = "") -> Optional[Identification]:
        for k, x in enumerate(self.items):
            if ((peak_id and x.peak_id == peak_id) or
                    (not peak_id and not x.peak_id and abs(x.apex_rt - rt) <= tol)):
                return self.items.pop(k)
        return None

    def to_list(self) -> list[dict]:
        return [x.to_dict() for x in self.items]

    @classmethod
    def from_list(cls, items) -> "IdentificationSet":
        return cls(Identification.from_dict(d) for d in items or [])

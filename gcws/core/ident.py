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

    def __post_init__(self):
        from gcws.core.text import clean_name, clean_rows
        self.name, self.cas, self.formula = clean_name(self.name), clean_name(self.cas), clean_name(self.formula)
        clean_rows(self.hits)

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


# -- the analyst's identification ---------------------------------------------------------------------
# Every place where the analyst names a peak (Peaks name / CAS cell, Peaks "Use library hit", the
# library-hits window, Name / CAS in Double determination) builds the identification here.

#: status of a name or CAS typed by the analyst / of a library hit the analyst chose (the search's word for
#: an accepted hit: the reports and the double determination count it as identified; ``manual`` marks it)
TYPED, CHOSEN = "Manual", "Accepted"


def typed_by_analyst(old: Optional[Identification], apex_rt: float, *, name: Optional[str] = None,
                     cas: Optional[str] = None, source: str = "manual") -> Identification:
    """The analyst typed a name and/or CAS (``None`` keeps the old value). The library hits stay to choose
    from, and so do the ISTD binding and the fragment. Score, formula and library describe the top hit:
    they stay while the name is the old one (only the CAS corrected) and go with a new name."""
    same_name = name is None or (old is not None and name == old.name)
    keep = old if same_name else None
    return Identification(
        apex_rt=apex_rt, name=name if name is not None else (old.name if old else ""),
        cas=cas if cas is not None else (old.cas if old else ""),
        score=keep.score if keep else None, status=TYPED, formula=keep.formula if keep else "",
        library=keep.library if keep else "", hits=list(old.hits) if old else [], source=source,
        manual=True, istd=old.istd if old else "", peak_id=old.peak_id if old else "")


def hit_chosen_by_analyst(old: Optional[Identification], apex_rt: float, hit: dict) -> Identification:
    """The analyst chose ``hit`` (a library hit record): its name, CAS, score, formula and library; the
    search that found it (hits, source, method, time), the ISTD binding and the fragment are kept."""
    return Identification(
        apex_rt=apex_rt, name=hit.get("name", "") or "", cas=hit.get("cas", "") or "", score=hit.get("score"),
        status=CHOSEN, formula=hit.get("formula", "") or "", library=hit.get("library", "") or "",
        hits=list(old.hits) if old else [hit], source=old.source if old else "",
        method=old.method if old else "", searched_at=old.searched_at if old else "",
        spectrum_mode=old.spectrum_mode if old else "", manual=True, istd=old.istd if old else "",
        peak_id=old.peak_id if old else "")


def chosen_by_analyst(ident: Identification) -> Identification:
    """A search result the analyst assigned by hand (the library-hits window): accepted, and protected
    from searches."""
    ident.status, ident.manual = CHOSEN, True
    return ident


# -- unidentified ---------------------------------------------------------------------------------------

_UNKNOWN_PREFIXES = ("unknown", "unbekannt", "unidentified", "nicht identifiziert", "no hit", "kein treffer", "?")
_UNKNOWN_NAMES = ("-", "n/a")


def names_unknown(name) -> bool:
    """Whether ``name`` says the substance is not identified ("unknown (m/z 149, 57)", "Unbekannt", "no hit
    found", "Kein Treffer", "?"). An empty name is not such a name (a row may be reported by its CAS)."""
    from gcws.core.text import clean_name
    n = " ".join(clean_name(name or "").split()).casefold()
    return bool(n) and (n.startswith(_UNKNOWN_PREFIXES) or n in _UNKNOWN_NAMES)


def is_unidentified(name, status: str = "") -> bool:
    """A peak or substance without an identification: no name, a name of an unknown, or the library
    status Unknown. The Peaks count, the reports and the Report² rules all ask this."""
    return not str(name or "").strip() or names_unknown(name) or \
        str(status or "").strip().casefold().startswith("unknown")


# -- the peak at a retention time -------------------------------------------------------------------------

#: how far (min) a determination's peak apex may be from a substance's RT and still be its peak
NEAR_RT = 0.05


def peak_near(peaks, rt: float, tol: float = NEAR_RT) -> Optional[int]:
    """Index of the peak whose apex is nearest ``rt``, if it is within ``tol`` minutes; else None."""
    if not peaks:
        return None
    j = min(range(len(peaks)), key=lambda q: abs(peaks[q].apex_rt - rt))
    return j if abs(peaks[j].apex_rt - rt) <= tol + 1e-9 else None      # the limit itself counts

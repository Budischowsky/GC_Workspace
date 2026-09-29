"""Data of the feature double determination: settings, the per-run input, features."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

import numpy as np

#: how a determination contributes to a feature
DETECTED = "detected"            # integrated by the method (or by the analyst)
GAPFILL = "gapfill"              # integrated by the gap filler
NOT_DETECTABLE = "not_detectable"    # searched for at the expected time, not found
MISSING = "missing"              # not searched (gap filling off, no MS, ...)
ORIGINS = (DETECTED, GAPFILL, NOT_DETECTABLE, MISSING)

#: the event option that marks a gap-filled peak
GAPFILL_OPTION = "gapfill"

PAIRING_FEATURES = "features"
PAIRING_CLASSIC = "classic"


@dataclass
class Settings:
    """Parameters (``ws.quant["features"]``).

    The pairing follows mzmine's GC aligner. The similarity weights and limits were chosen on the
    two A/B pairs of the reference batch (26016605, 07/11 and 09/12; 298 mutually nearest pairs vs
    their neighbours 0.03-0.12 min away), comparing co-eluting-ion spectra: the composite cosine
    with the NIST11 weights (I^0.53 * mz^1.3) separated them best (Youden J 0.84; mzmine's GC default
    NIST GC, I^0.6 * mz^3, 0.77, because single weak high-mass ions vary between injections). At
    0.6, 90 % of the true pairs and 6 % of the neighbours pass; spectra with fewer than 5
    co-eluting ions do not separate at all and leave the decision to the retention time."""

    pairing: str = PAIRING_FEATURES       # features | classic (AutoLib's pairing, unchanged)
    rt_tol: float = 0.05                  # min, after the drift map (mzmine wizard: 0.1 without one)
    max_shift: float = 0.2                # largest drift between the determinations (min)
    w_rt: float = 0.5                     # mzmine GC aligner RT weight
    min_sim: float = 0.6                  # below: not the same substance (mzmine minCos)
    green_sim: float = 0.75               # at least this similar for green
    min_ions: int = 5                     # co-eluting ions a spectrum needs to be compared
    exact_rt: float = 0.01                # min: closer than this (after the drift map) it is the same
                                          # peak even when the spectra differ (then: case D, red)
    anchor_sim: float = 0.85              # drift-map anchors
    ambiguity: float = 0.05               # an alternative pair within this score: ambiguous
    weights: str = "NIST11 (LC)"
    noise_floor: float = 0.005            # ions below this share of the base peak are ignored when comparing
    gap_fill: bool = True
    gap_min_sn: float = 3.0
    gap_min_ions: int = 3
    gap_shape_r: float = 0.8
    gap_min_cos: float = 0.7
    gap_min_points: int = 5
    gap_min_fraction: float = 0.25        # FID-only gap fill: at least this share of the expected height
    gap_int_tol: float = 0.2              # mzmine Gap intTolerance
    consensus_search: bool = True
    id_margin: float = 3.0                # score points the consensus candidate must lead by
    id_topk: int = 5
    ri_tol: float = 20.0
    harmonise: bool = True                # propose harmonised boundaries
    apply_auto: bool = True               # apply gap fills and consensus names automatically

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Settings":
        d = d or {}
        known = {f.name: f for f in fields(cls)}
        kw = {}
        for k, v in d.items():
            if k in known:
                kw[k] = type(getattr(cls(), k))(v) if v is not None else getattr(cls(), k)
        return cls(**kw)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PeakInfo:
    """One integrated peak of a determination, as the feature workflow sees it."""

    index: int                            # index in the run's integration of the quantification signal
    rt: float                             # apex (detector time)
    start: float
    end: float
    area: float
    height: float = 0.0
    width50: float = 0.0
    sn: Optional[float] = None
    origin: str = "auto"
    gapfill: bool = False                 # made by the gap filler
    spectrum: Optional[tuple] = None      # (mz, ab) co-eluting ions only (compared between runs)
    full_spectrum: Optional[tuple] = None  # (mz, ab) nominal, background-subtracted, all ions
    ident: Any = None                     # Identification or None
    hits: list = field(default_factory=list)
    istd: bool = False
    blank_level: bool = False
    fragment: str = ""                    # split fragment id (Identification.peak_id)

    @property
    def name(self) -> str:
        return getattr(self.ident, "name", "") or ""

    @property
    def cas(self) -> str:
        return getattr(self.ident, "cas", "") or ""

    @property
    def manual(self) -> bool:
        return bool(getattr(self.ident, "manual", False))

    def top_ions(self, n: int = 5) -> list[int]:
        if self.spectrum is None or len(self.spectrum[0]) == 0:
            return []
        mz, ab = self.spectrum
        order = np.argsort(-np.asarray(ab, float), kind="stable")[:n]
        return [int(mz[i]) for i in order]


@dataclass
class RunInput:
    """The peaks of one determination on its quantification signal."""

    run_id: str
    name: str
    label: str
    key: str
    delay: float
    peaks: list[PeakInfo]
    has_ms: bool = True
    t_min: Optional[float] = None         # solvent cut (detector time)

    def peak(self, index: int) -> Optional[PeakInfo]:
        return next((p for p in self.peaks if p.index == index), None)


@dataclass
class Member:
    run_id: str
    label: str
    peak: Optional[PeakInfo] = None
    origin: str = MISSING
    rt_ref: Optional[float] = None        # apex on the reference determination's time axis
    note: str = ""                        # gap-fill evidence / why not found

    @property
    def found(self) -> bool:
        return self.peak is not None and self.origin in (DETECTED, GAPFILL)

    @property
    def rt(self) -> Optional[float]:
        return self.peak.rt if self.peak is not None else None

    @property
    def area(self) -> Optional[float]:
        return self.peak.area if self.peak is not None else None


@dataclass
class Proposal:
    """A change the workflow makes (``auto``) or suggests to the analyst."""

    kind: str                             # gapfill | identity | boundary
    run_id: str
    key: str
    text: str
    event: Any = None                     # ManualEvent (gapfill, boundary)
    ident: Any = None                     # Identification (identity)
    rt: Optional[float] = None            # the peak the identification belongs to
    auto: bool = True


@dataclass
class Identity:
    """The identification decided for a feature (see ``consensus``)."""

    name: str = ""
    cas: str = ""
    status: str = ""                      # Accepted | Manual review | Unknown | Conflict; manual review
    case: str = ""                        # A | B | C | D | manual | istd | none
    candidates: list = field(default_factory=list)   # [{name, cas, score, scores:{label: s}}]
    margin: Optional[float] = None
    basis: str = ""                       # human-readable: how it was decided
    hits: list = field(default_factory=list)          # the hit list to write back (consensus first)


@dataclass
class Feature:
    members: list[Member]
    rt: float                             # reference-axis RT (mean of the found members)
    id: str = ""
    sim: Optional[float] = None           # composite cosine between the determinations (lowest pair)
    score: Optional[float] = None         # alignment score (mzmine RowVsRowScore)
    ambiguous: bool = False
    mismatch: bool = False                # same peak (same RT) but different spectra: case D
    identity: Optional[Identity] = None
    consensus: Optional[tuple] = None     # consensus spectrum (mz, ab)
    consensus_hits: list = field(default_factory=list)
    rpd: Optional[float] = None
    light: str = ""                       # green | yellow | red | grey
    reasons: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    split: bool = False                   # one peak here covers two features in the other run

    @property
    def found(self) -> list[Member]:
        return [m for m in self.members if m.found]

    @property
    def detected(self) -> list[Member]:
        return [m for m in self.members if m.origin == DETECTED]

    def member(self, run_id: str) -> Optional[Member]:
        return next((m for m in self.members if m.run_id == run_id), None)

    def spectrum(self):
        """The spectrum of the largest found member."""
        best = max((m for m in self.found if m.peak.spectrum is not None), key=lambda m: m.peak.height,
                   default=None)
        return best.peak.spectrum if best is not None else None

    def top_ions(self, n: int = 5) -> list[int]:
        best = max((m for m in self.found if m.peak.spectrum is not None), key=lambda m: m.peak.height,
                   default=None)
        return best.peak.top_ions(n) if best is not None else []


@dataclass
class FeatureTable:
    members: list[str]
    labels: list[str]
    key: str
    features: list[Feature]
    maps: dict = field(default_factory=dict)          # run id -> TimeMap onto the reference
    settings: Settings = field(default_factory=Settings)
    notes: list = field(default_factory=list)
    inputs: list = field(default_factory=list)        # the RunInput of each determination

    def input(self, run_id: str):
        return next((r for r in self.inputs if r.run_id == run_id), None)

    def by_id(self, fid: str) -> Optional[Feature]:
        return next((f for f in self.features if f.id == fid), None)

    def counts(self) -> dict:
        out = {"green": 0, "yellow": 0, "red": 0, "grey": 0}
        for f in self.features:
            if f.light in out:
                out[f.light] += 1
        return out

    def id_records(self) -> list[dict]:
        """What :func:`gcws.features.ids.stable_ids` needs to keep the ids after a re-integration."""
        return [{"id": f.id, "rt": round(f.rt, 4), "ions": f.top_ions()} for f in self.features]

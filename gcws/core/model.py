"""Domain objects: runs, signals, peaks and baselines."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from gcws.core.keys import is_derived, split_key

FID = "FID"
TIC = "TIC"
BPC = "BPC"
EIC = "EIC"


# -- signals -----------------------------------------------------------------

def eic_key(masses) -> str:
    ms = [int(round(float(m))) for m in masses]
    return "EIC " + "+".join(str(m) for m in ms)


def parse_key(key: str) -> tuple[str, tuple[int, ...]]:
    """``(kind, masses)`` of a key; derived keys report their base kind."""
    key = split_key(key)[0]
    if key.upper().startswith("EIC"):
        masses = tuple(int(v) for v in re.findall(r"\d+", key[3:]))
        return EIC, masses
    return key.upper(), ()


@dataclass
class Signal:
    key: str                  # "FID", "TIC", "BPC", "EIC 149" ...
    rt: np.ndarray            # minutes
    y: np.ndarray
    source: str = ""
    label: str = ""
    y_unit: str = ""

    @property
    def kind(self) -> str:
        return parse_key(self.key)[0]

    @property
    def is_ms(self) -> bool:
        return self.kind != FID

    @property
    def n(self) -> int:
        return int(self.y.size)

    def dt_seconds(self) -> np.ndarray:
        """Local sampling interval (s) per point; works for irregular MS scans."""
        if self.rt.size < 2:
            return np.ones_like(self.rt)
        d = np.diff(self.rt) * 60.0
        return np.concatenate([d, d[-1:]])

    @property
    def hz(self) -> float:
        if self.rt.size < 2:
            return 1.0
        return 1.0 / (float(np.median(np.diff(self.rt))) * 60.0)

    def index_of(self, t: float) -> int:
        j = int(np.searchsorted(self.rt, t))
        return max(0, min(self.n - 1, j))

    def window(self, t0: float, t1: float) -> slice:
        lo = int(np.searchsorted(self.rt, min(t0, t1), side="left"))
        hi = int(np.searchsorted(self.rt, max(t0, t1), side="right"))
        return slice(lo, hi)


# -- peaks -------------------------------------------------------------------

@dataclass
class Baseline:
    """Baseline under a peak between ``t0`` and ``t1``.

    kind ``line``: straight line (y0 -> y1); ``exp``: exponential skim
    ``b + (y0 - b) * exp(-k (t - t0))``; ``hold``: horizontal at y0.
    """
    kind: str
    t0: float
    y0: float
    t1: float
    y1: float
    k: float = 0.0
    b: float = 0.0

    def eval(self, t) -> np.ndarray:
        t = np.asarray(t, dtype=float)
        if self.kind == "hold":
            return np.full_like(t, self.y0)
        if self.kind == "exp":
            return self.b + (self.y0 - self.b) * np.exp(-self.k * (t - self.t0))
        if self.t1 == self.t0:
            return np.full_like(t, self.y0)
        return self.y0 + (self.y1 - self.y0) * (t - self.t0) / (self.t1 - self.t0)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "t0": self.t0, "y0": self.y0, "t1": self.t1,
                "y1": self.y1, "k": self.k, "b": self.b}

    @classmethod
    def from_dict(cls, d: dict) -> "Baseline":
        return cls(**{k: d[k] for k in ("kind", "t0", "y0", "t1", "y1", "k", "b") if k in d})


@dataclass
class Peak:
    start: float
    end: float
    apex_rt: float
    baseline: Baseline
    area: float = 0.0
    area_raw: float = 0.0
    height: float = 0.0
    width50: float = 0.0
    width5: float = 0.0
    symmetry: Optional[float] = None
    asymmetry: Optional[float] = None
    sn: Optional[float] = None
    area_pct: float = 0.0
    type_start: str = "B"
    type_end: str = "B"
    flags: str = ""                 # S T X F R f r N M +
    origin: str = "auto"            # auto|manual|split|merged|added|skim
    parent: Optional[int] = None    # index of the skim parent
    number: int = 0                 # display number (1-based, by RT)
    negative: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def type_code(self) -> str:
        return self.type_start + self.type_end + ((" " + self.flags) if self.flags else "")

    @property
    def is_solvent(self) -> bool:
        return "S" in self.flags

    def key(self) -> float:
        """Identity used to bind identifications/overrides across re-integration."""
        return round(self.apex_rt, 4)


# -- runs --------------------------------------------------------------------

@dataclass
class Run:
    path: Path
    meta: Any
    fid: Optional[Signal] = None
    ms_source: Any = None          # DataMS-compatible reader (legacy code)
    ms: Any = None                 # MSMatrix
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    color: str = "#1f77b4"
    role: str = "sample"
    load_notes: list[str] = field(default_factory=list)
    _signals: dict[str, Signal] = field(default_factory=dict, repr=False)
    #: provider of derived traces (e.g. "FID - Blank"), installed by the workspace
    derive: Optional[Callable[["Run", str], Optional[Signal]]] = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.meta.display_name if self.meta is not None else self.path.stem

    @property
    def folder_name(self) -> str:
        return self.path.name

    def available_signals(self) -> list[str]:
        out = []
        if self.fid is not None:
            out.append(FID)
        if self.ms is not None:
            out += [TIC, BPC]
        return out

    def signal(self, key: str) -> Optional[Signal]:
        key = key.strip()
        if key in self._signals:
            return self._signals[key]
        if is_derived(key):
            sig = self.derive(self, key) if self.derive is not None else None
            if sig is not None:
                self._signals[key] = sig
            return sig
        kind, masses = parse_key(key)
        sig = None
        if kind == FID and self.fid is not None:
            sig = self.fid
        elif self.ms is not None:
            if kind == TIC:
                y = self.ms.stored_tic if self.ms.instrument_tic else self.ms.tic()
                sig = Signal(TIC, self.ms.rt, y, source="MS", label="TIC", y_unit="counts")
            elif kind == BPC:
                sig = Signal(BPC, self.ms.rt, self.ms.bpc(), source="MS", label="BPC", y_unit="counts")
            elif kind == EIC and masses:
                k = eic_key(masses)
                sig = Signal(k, self.ms.rt, self.ms.eic(masses), source="MS", label=k, y_unit="counts")
                key = k
        if sig is not None:
            self._signals[key] = sig
        return sig

    def drop_derived(self, key: Optional[str] = None) -> None:
        """Forget cached derived traces (all, or one key) so they are rebuilt."""
        for k in list(self._signals):
            if is_derived(k) and (key is None or k == key):
                del self._signals[k]

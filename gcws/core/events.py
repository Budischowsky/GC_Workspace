"""Manual integration events.

Manual changes are stored as events anchored to retention times (never to peak
numbers) and re-applied, in order, after every automatic integration. The
result is reproducible from raw data + method + event list, which is what an
audit needs, and a method change does not silently discard manual work: an
event that no longer finds its peak is reported as unresolved.
"""
from __future__ import annotations

import getpass
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional


class ManualKind(str, Enum):
    DRAW_BASELINE = "Draw baseline"
    SPLIT = "Split (drop line)"
    DELETE = "Delete peak"
    ADD_PEAK = "Add peak"
    MOVE_START = "Move start"
    MOVE_END = "Move end"
    MERGE = "Merge peaks"
    SKIM = "Skim"
    NEGATIVE_PEAK = "Negative peak"
    RESET_RANGE = "Reset range"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class ManualEvent:
    kind: ManualKind
    t0: float
    t1: Optional[float] = None
    y0: Optional[float] = None
    y1: Optional[float] = None
    ref_rt: Optional[float] = None
    option: str = ""
    enabled: bool = True
    comment: str = ""
    user: str = field(default_factory=_user)
    timestamp: str = field(default_factory=_now)
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def describe(self) -> str:
        k = self.kind
        if k in (ManualKind.SPLIT,):
            return f"{k.value} at {self.t0:.3f} min"
        if k in (ManualKind.MOVE_START, ManualKind.MOVE_END):
            return f"{k.value} of peak {self.ref_rt:.3f} to {self.t0:.3f} min"
        if k == ManualKind.SKIM:
            return f"{self.option or 'tangent'} skim of peak {self.t1:.3f} on {self.t0:.3f} min"
        if self.t1 is not None:
            return f"{k.value} {self.t0:.3f}-{self.t1:.3f} min"
        return f"{k.value} at {self.t0:.3f} min"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.name
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ManualEvent":
        d = dict(d)
        d["kind"] = ManualKind[d["kind"]]
        return cls(**d)

    def with_(self, **kw) -> "ManualEvent":
        return replace(self, **kw)

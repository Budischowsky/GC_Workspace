"""Integration methods and timed integration events.

The parameter set follows the vocabulary chromatographers know from Agilent
OpenLab/ChemStation (slope sensitivity, peak width, area/height reject,
skim ratios, timed events) while the detection itself uses the
derivative approach of MassHunter Agile2 / Chromeleon Cobra (Savitzky-Golay
smoothing, first/second derivative, noise-scaled thresholds, auto parameters).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class EventKind(str, Enum):
    INTEGRATOR_OFF = "Integrator off"
    INTEGRATOR_ON = "Integrator on"
    SLOPE_SENSITIVITY = "Slope sensitivity"
    THRESHOLD = "Threshold"
    PEAK_WIDTH = "Peak width"
    AREA_REJECT = "Area reject"
    HEIGHT_REJECT = "Height reject"
    MIN_SN = "Min. S/N"
    SHOULDERS = "Shoulders"
    SKIM_MODE = "Skim mode"
    TAIL_SKIM_RATIO = "Tail skim height ratio"
    FRONT_SKIM_RATIO = "Front skim height ratio"
    SKIM_VALLEY_RATIO = "Skim valley ratio"
    BASELINE_NOW = "Baseline now"
    BASELINE_NEXT_VALLEY = "Baseline next valley"
    BASELINE_HOLD_ON = "Baseline hold on"
    BASELINE_HOLD_OFF = "Baseline hold off"
    BASELINE_VALLEYS_ON = "Baseline at valleys on"
    BASELINE_VALLEYS_OFF = "Baseline at valleys off"
    BASELINE_BACKWARD = "Baseline backward"
    SPLIT_PEAK = "Split peak"
    AREA_SUM_ON = "Area sum on"
    AREA_SUM_OFF = "Area sum off"
    NEGATIVE_ON = "Negative peaks on"
    NEGATIVE_OFF = "Negative peaks off"
    SOLVENT_ON = "Solvent peak on"
    SOLVENT_OFF = "Solvent peak off"
    DECONV_SPLIT_OFF = "Deconvolution split off"
    DECONV_SPLIT_ON = "Deconvolution split on"


#: Events that carry a numeric value.
VALUE_EVENTS = {EventKind.SLOPE_SENSITIVITY, EventKind.THRESHOLD, EventKind.PEAK_WIDTH,
                EventKind.AREA_REJECT, EventKind.HEIGHT_REJECT, EventKind.MIN_SN,
                EventKind.TAIL_SKIM_RATIO, EventKind.FRONT_SKIM_RATIO,
                EventKind.SKIM_VALLEY_RATIO}
#: Events that carry a choice.
CHOICE_EVENTS = {EventKind.SHOULDERS: ("off", "drop", "tangent"),
                 EventKind.SKIM_MODE: ("none", "tangent", "exponential", "auto")}
#: on/off pairs describing ranges.
RANGE_PAIRS = {
    EventKind.INTEGRATOR_OFF: EventKind.INTEGRATOR_ON,
    EventKind.BASELINE_HOLD_ON: EventKind.BASELINE_HOLD_OFF,
    EventKind.BASELINE_VALLEYS_ON: EventKind.BASELINE_VALLEYS_OFF,
    EventKind.AREA_SUM_ON: EventKind.AREA_SUM_OFF,
    EventKind.NEGATIVE_ON: EventKind.NEGATIVE_OFF,
    EventKind.SOLVENT_ON: EventKind.SOLVENT_OFF,
    EventKind.DECONV_SPLIT_OFF: EventKind.DECONV_SPLIT_ON,
}

#: Model ions that never make a deconvoluted component its own fragment (column bleed;
#: mzmine's "Exclude m/z values" of the GC spectral deconvolution).
DECONV_EXCLUDE_MZ = (73, 207, 281, 355)


@dataclass(frozen=True)
class TimedEvent:
    time: float
    kind: EventKind
    value: Any = None
    enabled: bool = True
    note: str = ""

    def to_dict(self) -> dict:
        return {"time": self.time, "kind": self.kind.name, "value": self.value,
                "enabled": self.enabled, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "TimedEvent":
        kind = d["kind"]
        kind = EventKind[kind] if kind in EventKind.__members__ else EventKind(kind)
        return cls(time=float(d["time"]), kind=kind, value=d.get("value"),
                   enabled=bool(d.get("enabled", True)), note=d.get("note", ""))


@dataclass
class IntegrationMethod:
    name: str = "Default"
    description: str = ""
    #: None = determined automatically from the signal ("auto parameters").
    peak_width: Optional[float] = None          # FWHM of a typical peak, min
    slope_sensitivity: Optional[float] = None   # x derivative noise
    threshold: Optional[float] = None           # min height above baseline, signal units
    smoothing_window: Optional[int] = None      # points
    smoothing_order: int = 2
    area_reject: float = 0.0                    # reported area units
    height_reject: float = 0.0                  # signal units
    min_sn: float = 0.0
    min_width_fraction: float = 0.2             # reject spikes narrower than this x peak width
    #: two peaks are only separated by a valley at least this deep (signal units)
    #: below the lower apex; None = auto (a multiple of the threshold)
    min_valley_depth: Optional[float] = None
    shoulders: str = "off"                      # off | drop | tangent
    baseline_mode: str = "drop"                 # drop (common baseline) | valley
    skim_mode: str = "auto"                     # none | tangent | exponential | auto
    tail_skim_ratio: float = 5.0                # parent/child height for a tail rider
    front_skim_ratio: float = 12.0              # parent/child height for a front rider
    skim_valley_ratio: float = 20.0             # child height / valley height above baseline
    exp_skim_r2: float = 0.98
    negative_peaks: bool = False
    #: baseline tracking: a peak may only end close to the baseline envelope
    baseline_tracking: bool = False
    baseline_window: Optional[float] = None     # min; SNIP clipping window (auto: 40 x peak width)
    baseline_tolerance: Optional[float] = None  # signal units above the envelope (auto: 2 x threshold)
    solvent_height_factor: float = 0.0          # >0: peaks higher than f x median height are solvent
    area_unit_factor: float = 10.0              # counts*s -> reported area
    #: automatic deconvolution split (FID/TIC): "off" | "auto" (see gcws.integration.auto_deconv)
    deconv_split: str = "off"
    deconv_min_share: float = 0.01              # a component below this share of the fit stays with its neighbours
    deconv_min_sn: float = 20.0                 # ... and one below this MS S/N
    deconv_fit_r2: float = 0.97                 # below: areas from MS component proportions
    deconv_min_r: float = 0.8                   # shape correlation component profile <-> trace
    deconv_exclude_mz: list[int] = field(default_factory=lambda: list(DECONV_EXCLUDE_MZ))
    timed_events: list[TimedEvent] = field(default_factory=list)
    version: int = 1

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timed_events"] = [e.to_dict() for e in self.timed_events]
        d["format"] = "gcws-integration-method"
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "IntegrationMethod":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known and k != "timed_events"}
        m = cls(**kw)
        m.timed_events = [TimedEvent.from_dict(e) for e in d.get("timed_events", [])]
        return m

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path) -> "IntegrationMethod":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def copy(self, **changes) -> "IntegrationMethod":
        m = replace(self, **changes)
        m.timed_events = list(changes.get("timed_events", self.timed_events))
        m.deconv_exclude_mz = list(changes.get("deconv_exclude_mz", self.deconv_exclude_mz))
        return m

    def deconv_off_ranges(self, t_end: float = 1e9) -> list[tuple[float, float]]:
        """``(t0, t1)`` where the automatic deconvolution split is switched off by timed events."""
        out, start = [], None
        for e in self.events():
            if e.kind == EventKind.DECONV_SPLIT_OFF and start is None:
                start = e.time
            elif e.kind == EventKind.DECONV_SPLIT_ON and start is not None:
                out.append((start, e.time))
                start = None
        if start is not None:
            out.append((start, t_end))
        return out

    def events(self) -> list[TimedEvent]:
        return sorted((e for e in self.timed_events if e.enabled), key=lambda e: e.time)


def nias_fid_method() -> IntegrationMethod:
    """Default for the NIAS screening FID: nothing before the solvent end."""
    # Tuned on the reference batch against ChemStation's autoint1FID.e results:
    # drop lines only (ChemStation skimmed nothing), slope 8 x derivative noise,
    # rejects at the smallest peaks ChemStation still reported. ISTD areas agree
    # within 5 % (median ratio 1.01).
    return IntegrationMethod(
        name="NIAS-SCREENING FID",
        description="FID after the solvent (5.5 min), drop lines, tuned to match the "
                    "former ChemStation auto-integration.",
        slope_sensitivity=8.0,
        skim_mode="none",
        area_reject=70000.0,
        height_reject=5000.0,
        area_unit_factor=10.0,
        timed_events=[TimedEvent(0.0, EventKind.INTEGRATOR_OFF),
                      TimedEvent(5.5, EventKind.INTEGRATOR_ON)],
    )


def ms_method() -> IntegrationMethod:
    """Default for TIC/EIC: MS peaks are sampled with only a few scans."""
    return IntegrationMethod(name="MS TIC/EIC", description="TIC/EIC, auto parameters.",
                             area_unit_factor=1.0, smoothing_order=2)


def default_for(signal_kind: str) -> IntegrationMethod:
    return nias_fid_method() if signal_kind == "FID" else ms_method()

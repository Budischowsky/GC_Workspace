"""The limits of a quantification: the difference limit of a double determination, the reporting limit
and the RT tolerance that pairs the determinations' peaks.

One value, built from the method (:func:`of_quant`) or from AutoLib ``Settings`` (:func:`of_settings`),
written into the automation evidence (:meth:`QuantLimits.as_evidence`) and read back by the rules
(:func:`of_evidence`). The defaults live here and nowhere else.

A configured 0 is a limit: a difference limit of 0 % flags every difference, a reporting limit of 0
reports every substance. Only a missing or unreadable value falls back to the default. The RT
tolerance is the exception: 0 min would pair nothing, so a value that is not positive is the default.
"""
from __future__ import annotations

from dataclasses import dataclass

DIFFERENCE = 30.0       # % (AutoLib gc_duplicate.DEFAULT_MAX_RELDIFF)
REPORTING = 0.01        # mg/kg (AutoLib gc_duplicate.DEFAULT_REPORTING_LIMIT)
RT_TOLERANCE = 0.035    # min


@dataclass(frozen=True)
class QuantLimits:
    difference: float = DIFFERENCE      # difference limit (%) of a double determination
    reporting: float = REPORTING        # reporting limit; 0 = everything is reported
    rt_tolerance: float = RT_TOLERANCE  # min

    def as_evidence(self) -> dict:
        """The plain dict the automation evidence carries (``evidence["settings"]``)."""
        return {"reporting_limit": self.reporting, "duplicate_max_reldiff": self.difference,
                "rt_tolerance": self.rt_tolerance}


def _number(value, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f else default


def _tolerance(value) -> float:
    f = _number(value, RT_TOLERANCE)
    return f if f > 0 else RT_TOLERANCE


def of_settings(settings) -> QuantLimits:
    """The limits of AutoLib ``settings`` (``gcws.quant.nias_bridge.make_settings``)."""
    return QuantLimits(difference=_number(getattr(settings, "duplicate_max_reldiff", None), DIFFERENCE),
                       reporting=_number(getattr(settings, "reporting_limit", None), REPORTING),
                       rt_tolerance=_tolerance(getattr(settings, "rt_tolerance", None)))


def of_evidence(evidence: dict | None) -> QuantLimits:
    """The limits written by :meth:`QuantLimits.as_evidence` (an older evidence may lack some)."""
    s = (evidence or {}).get("settings") or {}
    return QuantLimits(difference=_number(s.get("duplicate_max_reldiff"), DIFFERENCE),
                       reporting=_number(s.get("reporting_limit"), REPORTING),
                       rt_tolerance=_tolerance(s.get("rt_tolerance")))


def of_quant(quant: dict | None) -> QuantLimits:
    """The limits a double determination of the method ``quant`` is compared with. HS screening takes
    its own difference limit and RT tolerance and has no reporting limit; only NIAS mg/kg applies one."""
    from gcws.quant.nias_bridge import make_settings
    quant = quant or {}
    mode = quant.get("mode", "nias_mgkg")
    if mode == "hs_screening":
        hs = quant.get("hs") or {}
        return QuantLimits(difference=_number(hs.get("duplicate_max_reldiff"), DIFFERENCE), reporting=0.0,
                           rt_tolerance=of_settings(make_settings(hs)).rt_tolerance)
    limits = of_settings(make_settings(quant.get("settings")))
    if mode != "nias_mgkg":
        limits = QuantLimits(limits.difference, 0.0, limits.rt_tolerance)
    return limits

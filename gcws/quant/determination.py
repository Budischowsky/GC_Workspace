"""Which determination a replicate group is: single, double (N-fold) or incomplete double (no Qt).

``of(quant, group, names)`` answers it for the group ``group`` (``ws.replicate_groups``) of the method
``quant`` (``ws.quant``), ``names`` mapping run id -> file name (``07_..._A.D``):

- ``kind``: :data:`SINGLE` (a run without a replicate suffix, or made single by hand: ``single`` in the
  group; with the method's **Single determination only** (``quant["single_only"]``) also a pair, of which
  one run is reported and the other is the control, and a lone run with a suffix), :data:`DOUBLE` (two or
  more runs, all reported) or :data:`INCOMPLETE` (a run with a suffix whose partner never came: processed
  alone, but it needs control);
- ``reported``: the runs whose values go into the report; ``control``: the run compared with the reported
  one but not reported (a single determination of a pair), else None. The reported run of such a pair is
  A unless the analyst switched it (``reported`` in the group, :func:`gcws.quant.double_determination.set_reported`);
- ``label``: how the report names it (``{determination}`` of a template).

The automation job, its Report² evidence, the reports and the Replicates panel ask this module instead
of counting members. Suffixes are recognised as the grouping recognises them (``_A``/``_B``,
``_1``/``_2``, ``repN``)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

SINGLE, DOUBLE, INCOMPLETE = "single", "double", "incomplete"

_SUFFIX = re.compile(r"[_\- ]+([A-Za-z]|\d{1,2}|rep\d+)$", re.IGNORECASE)


def suffix(name: str) -> str:
    """The replicate suffix of a run's file name (upper case: ``A``, ``2``, ``REP1``), else ""."""
    stem = re.sub(r"\.(?:d|qgd)$", "", str(name), flags=re.IGNORECASE)
    m = _SUFFIX.search(stem)
    return m.group(1).upper() if m else ""


@dataclass(frozen=True)
class Determination:
    kind: str
    members: tuple                     # the group's runs (as the group lists them)
    reported: tuple                    # the runs reported
    control: Optional[str] = None      # the run compared but not reported
    note: str = ""                     # an information line for Report²

    @property
    def single(self) -> bool:
        return self.kind == SINGLE

    @property
    def compared(self) -> bool:
        """Two or more runs are processed together and compared (a double determination's page)."""
        return len(self.members) >= 2

    @property
    def label(self) -> str:
        n = len(self.reported)
        return "single determination" if n <= 1 else "double determination" if n == 2 else \
            f"{n}-fold determination"


def single_only(quant: Optional[dict]) -> bool:
    """The method reports one determination of a pair (the Quantification panel's checkbox; missing = off)."""
    return bool((quant or {}).get("single_only"))


#: the suffixes of the reported determination of a pair (A)
_FIRST = {"A", "1", "01", "REP1"}


def _reported(members: tuple, names: dict) -> str:
    """The reported run of a pair: A (``_A`` / ``_1``), otherwise the first by injection."""
    first = [m for m in members if suffix(names[m]) in _FIRST]
    if len(first) == 1:
        return first[0]
    from gcws.io.sequence import order_key
    return min(members, key=lambda m: order_key(names[m]))


def of(quant: Optional[dict], group: Optional[dict], names: dict) -> Determination:
    """The determination of ``group`` (see the module text); runs missing from ``names`` do not count."""
    members = tuple(m for m in (group or {}).get("members") or [] if m in names)
    if len(members) == 2 and single_only(quant):
        chosen = (group or {}).get("reported")
        rep = chosen if chosen in members else _reported(members, names)
        return Determination(SINGLE, members, (rep,), next(m for m in members if m != rep))
    if len(members) >= 2:
        return Determination(DOUBLE, members, members)
    if not members:
        return Determination(SINGLE, (), ())
    suf = suffix(names[members[0]])
    if suf and single_only(quant):                     # the partner never came: no incomplete state
        return Determination(SINGLE, members, members, note="" if suf in _FIRST else "A missing, B reported")
    return Determination(INCOMPLETE if suf and not (group or {}).get("single") else SINGLE, members, members)


def of_workspace(ws, group: Optional[dict]) -> Determination:
    """:func:`of` for a group of the runs loaded in ``ws``."""
    return of(ws.quant, group, {rid: st.run.path.name for rid, st in ws.runs.items()})

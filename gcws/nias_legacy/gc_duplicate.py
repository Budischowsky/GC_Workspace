"""The Doppelbestimmung merge, as data.

The reference workbook's ``Doppelbestimmung`` sheet is a *live* sheet: every
number in it is a formula over ``Bestimmung_1``/``Bestimmung_2`` and
``Parameter``, and its two colours come from conditional formatting rather than
from static fills. This module is the workspace's side of exactly that sheet --
the same twelve values per row and the same two rules -- so the grid and the
workbook writer cannot drift apart.

Deliberately **headless and pure**, like ``gc_qc``: no Tk, no openpyxl, no
global state. ``gc_grid.DuplicateGrid`` renders what :func:`build` returns and
``gc_export`` writes it; neither owns the rules.

What this module does *not* do is arithmetic. ``mean``, ``c1``, ``c2`` and
``reldiff`` come out of ``AutoLib.combine_determinations`` via ``gc_fid.combine``,
which reads ``PeakRow.derived["mg_kg"]`` -- and that is kept current by
``gc_fid.NiasSample.recalculate`` on every edit. Recomputing them here would be
a second set of books; calling :func:`build` again after an edit is what makes
the view live.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "DEFAULT_MAX_RELDIFF",
    "DEFAULT_REPORTING_LIMIT",
    "DuplicateRow",
    "SETTLED_STATUSES",
    "SINGLE_DETERMINATION_STATUS",
    "VALID_DUPLICATE_STATUS",
    "build",
    "below_limit",
    "exceeds_reldiff",
    "needs_review",
]

#: The two duplicate statuses that settle a row. Both may carry a
#: ``", also in Blank"`` suffix, so every test on them is on the leading
#: characters, never on equality (spec v2.1 SS V.6).
VALID_DUPLICATE_STATUS = "Valid duplicate"

#: ``AutoLib.SINGLE_DETERMINATION_STATUS``. Repeated rather than imported
#: because the engine is loaded lazily and by path; ``gc_export`` keeps its own
#: copy for the same reason and a test asserts the three stay equal.
SINGLE_DETERMINATION_STATUS = "Einzelbestimmung"

SETTLED_STATUSES = (VALID_DUPLICATE_STATUS, SINGLE_DETERMINATION_STATUS)

#: Reporting limit in mg/kg when the settings do not carry one. Same default as
#: ``gc_fid.NiasSample.recalculate`` and ``AutoLib.make_duplicate_workbook``.
DEFAULT_REPORTING_LIMIT = 0.01

#: Duplicate difference limit in percent when no setting is supplied
#: (spec v3.1 SS VII.4, entschieden). Editable as ``duplicate_max_reldiff`` in
#: the Parameter sheet; ``gc_fid.DEFAULT_MAX_RELDIFF`` repeats the number so the
#: Parameter sheet does not depend on this module, and a test pins the two
#: together. **Flag only:** the mean is still reported, the status string still
#: comes from ``AutoLib.combine_determinations``.
DEFAULT_MAX_RELDIFF = 30.0


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class DuplicateRow:
    """One row of the ``Doppelbestimmung`` sheet.

    ``item`` is AutoLib's merged dict, untouched -- ``gc_export`` writes the
    workbook from the same shape, so anything derived from it must be read off
    here rather than copied into new fields.

    ``row_id1``/``row_id2`` are what the sheet's cell references are in the
    workspace: ``Area 1`` is ``sample1.row(row_id1).area``, which is the value
    ``=Bestimmung_1!E{r}`` picks up. They are ``None`` for a side the merge did
    not find, and then that side's two cells stay empty.
    """

    item: dict[str, Any]
    label1: str = ""
    label2: str = ""
    row_id1: Optional[int] = None
    row_id2: Optional[int] = None

    #: Distinct earlier reports this substance already appeared in (SS VII.5),
    #: or ``None`` when nobody looked it up. Appended last and defaulted so a
    #: caller constructing ``DuplicateRow(item, label1, label2, a, b)``
    #: positionally -- which ``build`` and the v3.0 tests do -- keeps working.
    #: Deliberately a plain field, not a property: it comes from the register
    #: database, not from ``item``, and this module owns no database handle.
    seen_before: Optional[int] = None

    # -- the twelve columns, in sheet order --------------------------------

    @property
    def rt(self) -> Optional[float]:
        return _num(self.item.get("rt"))

    @property
    def name(self) -> str:
        return self.item.get("name") or ""

    @property
    def cas(self) -> str:
        return self.item.get("cas") or ""

    @property
    def mean(self) -> Optional[float]:
        return _num(self.item.get("mean"))

    @property
    def area1(self) -> Optional[float]:
        return _num((self.item.get("source1") or {}).get("area"))

    @property
    def c1(self) -> Optional[float]:
        return _num(self.item.get("c1"))

    @property
    def area2(self) -> Optional[float]:
        return _num((self.item.get("source2") or {}).get("area"))

    @property
    def c2(self) -> Optional[float]:
        return _num(self.item.get("c2"))

    @property
    def status(self) -> str:
        return self.item.get("status") or ""

    @property
    def id_status(self) -> str:
        return self.item.get("id_status") or ""

    @property
    def review(self) -> str:
        return self.item.get("review") or ""

    @property
    def reldiff(self) -> Optional[float]:
        return _num(self.item.get("reldiff"))

    # -- addressing --------------------------------------------------------

    def side(self, n: int) -> tuple[str, Optional[int]]:
        """``(determination label, row_id)`` for side 1 or 2."""
        return (self.label1, self.row_id1) if n == 1 else (self.label2, self.row_id2)

    def value(self, key: str) -> Any:
        return getattr(self, key, None)


def _seen_lookup(counts: Optional[Mapping[tuple[str, str], int]]):
    """A ``(cas, name) -> Optional[int]`` reader over ``counts``, or ``None``.

    ``counts`` is keyed on ``gc_seen.keys(cas, name)``, so the same
    normalisation has to be applied here -- otherwise a sheet spelling and a
    register spelling of one name would never meet. ``gc_seen`` is imported
    lazily and its absence is tolerated: this module stays usable without the
    register, and the raw ``(cas, name)`` tuple is still honoured so a caller
    can pass a hand-built mapping in a test.
    """
    if not counts:
        return None
    try:
        import gc_seen
        normalise = gc_seen.keys
    except Exception:
        normalise = None

    def read(cas: Any, name: Any) -> Optional[int]:
        if normalise is not None:
            hit = counts.get(normalise(cas, name))
            if hit is not None:
                return int(hit)
        hit = counts.get((str(cas or ""), str(name or "")))
        return int(hit) if hit is not None else None

    return read


def build(session, tolerance: Optional[float] = None,
          counts: Optional[Mapping[tuple[str, str], int]] = None,
          ) -> list[DuplicateRow]:
    """The merged rows of the loaded session, resolved back onto the peak rows.

    One determination is a complete analysis (SS V.6) and gets the same row
    shape from ``AutoLib.single_determination_rows``; the sheet does not change
    its layout for it, so neither does this.

    ``counts`` is the ``gc_seen.counts`` mapping for the whole sheet, fetched
    once per rebuild by the caller (SS VII.5). Passing it fills
    :attr:`DuplicateRow.seen_before`; omitting it leaves every row at ``None``,
    which is what the grid paints as an empty cell. The database handle stays
    with the caller on purpose -- a rule module that opens files cannot be
    tested without one.
    """
    import gc_fid
    # GCWS-PATCH: helpers copied from gc_workspace (Tk) into this module.

    labels = list(getattr(session, "labels", ()) or ())
    if not labels:
        return []
    samples = [session.samples[label] for label in labels[:2]]

    if len(samples) == 1:
        engine = gc_fid.engine()
        to_peak = getattr(gc_fid, "_as_engine_peak")
        combined = engine.combine_determinations(
            [to_peak(r) for r in gc_fid.report_rows(samples[0])], None, 0.0)
    else:
        combined = gc_fid.combine(samples[0], samples[1], tolerance)

    index1 = _ws_row_index(samples[0])
    index2 = _ws_row_index(samples[1]) if len(samples) > 1 else {}
    label1 = samples[0].label
    label2 = samples[1].label if len(samples) > 1 else ""

    seen = _seen_lookup(counts)

    rows: list[DuplicateRow] = []
    for item in combined:
        a = _ws_lookup_row(index1, item.get("source1"))
        b = _ws_lookup_row(index2, item.get("source2")) if index2 else None
        row = DuplicateRow(
            item=item, label1=label1, label2=label2,
            row_id1=a.row_id if a is not None else None,
            row_id2=b.row_id if b is not None else None)
        if seen is not None:
            row.seen_before = seen(row.cas, row.name)
        rows.append(row)
    return rows


def below_limit(row: DuplicateRow,
                limit: float = DEFAULT_REPORTING_LIMIT) -> bool:
    """The workbook's grey rule, verbatim.

    ``AND(COUNT($D,$F,$H)>0, IF(ISNUMBER($D),$D,MAX($F,$H))<limit)``

    The mean decides, exactly as in the report. An artefact has no mean, so its
    single determination decides instead. A row with no number at all is not
    "below the limit" -- it is unmeasured, and greying it would claim something
    the data does not say.
    """
    mean, c1, c2 = row.mean, row.c1, row.c2
    if mean is None and c1 is None and c2 is None:
        return False
    if mean is not None:
        measure = mean
    else:
        singles = [c for c in (c1, c2) if c is not None]
        if not singles:
            return False
        measure = max(singles)
    return measure < limit


def exceeds_reldiff(row: DuplicateRow,
                    limit: float = DEFAULT_MAX_RELDIFF,
                    *,
                    reporting_limit: float = DEFAULT_REPORTING_LIMIT) -> bool:
    """True when both determinations measured the substance and disagree by
    more than ``limit`` percent. A row with one determination has no relative
    difference and is never flagged by this rule.

    Three guards, in the order they matter (spec v3.1 SS VII.4):

    * **One-sided.** ``reldiff`` is ``|c1-c2|/mean*100``; with only one result
      there is nothing to compare, and AutoLib may still leave a number in the
      field. Both concentrations must be present before the number means
      anything, so the presence test is on ``c1``/``c2``, not on ``reldiff``.
    * **Below the reporting limit wins.** The grey rule carries ``stopIfTrue``
      in the workbook and the same precedence in ``DuplicateGrid._paint``: a
      result nobody publishes must not send an analyst after a difference.
      Repeating the test here means the rule holds for every caller, including
      one that only asks this function.
    * **A non-finite difference is not a flag.** ``mean == 0`` produces ``inf``
      or ``nan`` upstream; neither is evidence of a bad duplicate.

    ``reporting_limit`` is keyword-only and additive to the contract signature
    of SS VII.4, so ``exceeds_reldiff(row, 30.0)`` behaves exactly as specified;
    a caller holding the session's real reporting limit can pass it.
    """
    c1, c2 = row.c1, row.c2
    if c1 is None or c2 is None:
        return False
    if below_limit(row, reporting_limit):
        return False
    diff = row.reldiff
    if diff is None or not math.isfinite(diff):
        return False
    return diff > limit


def needs_review(row: DuplicateRow,
                 limit: float = DEFAULT_MAX_RELDIFF,
                 *,
                 reporting_limit: float = DEFAULT_REPORTING_LIMIT) -> bool:
    """The workbook's red rule, verbatim, plus the SS VII.4 difference flag.

    ``OR(status is not settled, identification <> "Accepted", review <> "",
    relative difference > limit)``

    The status test is on the leading characters because both settled statuses
    may carry a ``", also in Blank"`` suffix (SS V.6).

    ``limit`` defaults to :data:`DEFAULT_MAX_RELDIFF`, so ``needs_review(row)``
    keeps the behaviour every v3.0 caller relies on at the 30 % default; the
    grid passes its own limit and ``gc_export`` the settings value.
    """
    if not row.status.startswith(SETTLED_STATUSES):
        return True
    if row.id_status != "Accepted":
        return True
    if row.review:
        return True
    return exceeds_reldiff(row, limit, reporting_limit=reporting_limit)


# GCWS-PATCH: copied from NIAS Working gc_workspace._row_index/_lookup_row so
# this module no longer imports the Tk workspace.
def _ws_row_index(sample) -> dict:
    index: dict = {}
    for row in sample.rows:
        index.setdefault(("id", row.row_id), row)
        fid = row.derived.get("fid_peak")
        if fid is not None:
            index.setdefault(("fid", str(fid)), row)
        index.setdefault(("rt", round(float(row.rt), 6)), row)
    return index


def _ws_lookup_row(index: dict, source):
    """The ``PeakRow`` an engine dict came from (row_id, FID peak, then RT)."""
    if not source:
        return None
    row_id = source.get("row_id")
    if row_id is not None and ("id", row_id) in index:
        return index[("id", row_id)]
    fid = source.get("fid_peak")
    if fid is not None and ("fid", str(fid)) in index:
        return index[("fid", str(fid))]
    rt = _num(source.get("rt"))
    if rt is None:
        return None
    return index.get(("rt", round(rt, 6)))

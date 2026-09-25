"""Peak identification over EI Atlas: every peak's spectrum searched again.

``Daten · Peaks identifizieren (EI Atlas)…``. The search uses EI Atlas's own
libraries and match functions (PBM, Qual 0-99, or NIST-style similarity)
through its local server (``gc_atlas.ensure_server`` -> ``POST /api/analyze``),
with the parameters of a saved search method (``gc_search_method``). Only the
server is started; the Atlas window stays closed.

The result replaces the identification that was loaded from the ``.D``
(``name``, ``cas``, ``si``, the hit list) -- it becomes the row's new baseline,
not a manual override. Rows whose name or CAS the analyst typed and internal
standards are protected unless explicitly ticked in the compound table. The
whole assignment is one undo step per determination (:class:`Reidentify`).

This module has no Tk; the windows are in ``gc_identify_ui``.
"""
from __future__ import annotations

import queue
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import gc_integrate
import gc_model as M
import gc_search_method as SM

#: ``row.derived["id_source"]`` of an Atlas identification.
SOURCE = "EI Atlas"
MAX_TOP_N = SM.MAX_TOP_N
#: Hit fields kept on the row and in the session (reference peaks are not:
#: they are looked up again when the compound table needs them).
HIT_FIELDS = ("name", "cas", "score", "forward", "reverse", "mf", "rmf", "formula", "source",
              "library", "library_id", "mw")

PROTECTED_MANUAL = "manuell benannt"
PROTECTED_ISTD = "ISTD"

#: Name an accepted hydrocarbon hit is written as (``SearchMethod.hydrocarbons``);
#: the report sums it through ``classify_name`` ("hydrocarbon" in the name).
HYDROCARBON_NAME = "Hydrocarbon"
#: Name stems of aromatic C/H compounds, which keep their own name.
AROMATIC_STEMS = ("benz", "phenyl", "naphth", "styr", "tolu", "xylen", "cumen", "cymen",
                  "mesityl", "anthrac", "phenanthr", "pyren", "azulen", "inden", "fluoren",
                  "acenaphth", "tetralin", "chrysen", "stilben")
FORMULA_PART = re.compile(r"([A-Z][a-z]?)(\d*)")


# -- settings -------------------------------------------------------------

@dataclass
class IdentifySettings:
    """A search method (``gc_search_method``) plus the batch's run options."""
    method: SM.SearchMethod
    all_samples: bool = True
    review: bool = True            # compound table (True) or apply directly
    rescan_below: Optional[int] = None   # only peaks whose loaded score is below this

    @property
    def libraries(self) -> list[str]:
        return self.method.enabled_libraries()

    @property
    def top_n(self) -> int:
        return self.method.top_n

    @property
    def min_score(self) -> int:
        return self.method.min_score


def available_libraries(status: dict) -> list[dict]:
    """The searchable sources of ``/api/status`` (at least one spectrum)."""
    return SM.available_libraries(status)


# -- the peaks --------------------------------------------------------------

@dataclass
class PeakJob:
    """One row to search, and what the search found."""
    label: str
    row_id: int
    peak_no: int
    rt: float
    before: tuple[str, str, Optional[float]]           # name, cas, si
    protected: str = ""                                 # "" | PROTECTED_*
    spectrum: list = field(default_factory=list)
    hits: list[dict] = field(default_factory=list)
    error: str = ""
    done: bool = False
    chosen: Optional[int] = None    # index into hits picked by the analyst
    apply: bool = True

    @property
    def top(self) -> Optional[dict]:
        if not self.hits:
            return None
        return self.hits[self.chosen if self.chosen is not None else 0]


def protected_reason(sample, row, istd_ids: set[int]) -> str:
    if row.row_id in istd_ids:
        return PROTECTED_ISTD
    manual = getattr(row, "manual", set()) or set()
    if "name" in manual or "cas" in manual:
        return PROTECTED_MANUAL
    return ""


def row_spectrum(sample, row, bg: Optional[int], background: str = "peak") -> list:
    """The spectrum searched for ``row``.

    ``"peak"`` is what the workspace shows (the row's background or the one
    the analyst picked); ``"none"`` is the apex scan without subtraction.
    """
    if background == "none":
        return sample.spectrum_at(row.effective_apex, None)
    return sample.spectrum(row.row_id) if bg is None else sample.spectrum(row.row_id, bg_override=bg)


def collect_jobs(samples, bg_for: Callable[[Any], Optional[int]],
                 background: str = "peak") -> list[PeakJob]:
    """Every live row of ``samples`` with the spectrum to search."""
    from gc_bulk_delete import DISABLED, protected_row_ids
    jobs = []
    for sample in samples:
        istd_ids = protected_row_ids(sample)
        bg = bg_for(sample)
        for row in sample.rows:
            if getattr(row, "integration_origin", None) == DISABLED:
                continue
            try:
                spectrum = row_spectrum(sample, row, bg, background)
            except Exception:
                spectrum = []
            job = PeakJob(sample.label, row.row_id, row.peak_no, float(row.rt),
                          (row.name, row.cas, row.si),
                          protected_reason(sample, row, istd_ids),
                          [(float(m), float(i)) for m, i in spectrum or [] if i > 0])
            job.apply = not job.protected
            if not job.spectrum:
                job.error, job.done = "kein MS-Spektrum", True
            jobs.append(job)
    return jobs


def split_by_score(jobs: list[PeakJob], limit: int) -> tuple[list[PeakJob], list[PeakJob]]:
    """``(to_search, kept)``: peaks whose loaded score (the PBM Qual of the
    RESULT.CSV) reaches ``limit`` keep their identification; peaks without a
    score are searched."""
    search, kept = [], []
    for job in jobs:
        si = job.before[2]
        (kept if si is not None and float(si) >= limit else search).append(job)
    return search, kept


def is_hydrocarbon(hit: Optional[dict]) -> bool:
    """A non-aromatic hydrocarbon: formula of C and H only, no aromatic name stem.

    Covers n-alkanes, branched and cyclic alkanes, alkenes and terpene-like
    polyenes (squalene). No double-bond-equivalent limit, so polycyclic
    saturated hydrocarbons (steranes) are not lost; aromatics are recognised
    by name. Without a formula nothing is recognised.
    """
    if not hit:
        return False
    formula = re.sub(r"\s+", "", str(hit.get("formula") or ""))
    parts = FORMULA_PART.findall(formula)
    if not parts or "".join(e + n for e, n in parts) != formula:
        return False
    if {e for e, _n in parts} != {"C", "H"}:
        return False
    name = str(hit.get("name") or "").casefold()
    return not any(stem in name for stem in AROMATIC_STEMS)


def top_is_hydrocarbon(job: PeakJob, settings: IdentifySettings) -> bool:
    """Whether the hydrocarbon rule renames ``job``'s result when applied."""
    top = job.top
    return (settings.method.hydrocarbons and top is not None
            and (job.chosen is not None or (top.get("score") or 0) >= settings.min_score)
            and is_hydrocarbon(top))


def mz_range(jobs: list[PeakJob]) -> tuple[int, int]:
    """One scan range for the whole batch.

    The same ``min_mz``/``max_mz`` for every query keeps EI Atlas's reference
    norm cache warm; the range of all measured ions is the acquisition range
    the method actually recorded.
    """
    masses = [m for job in jobs for m, _i in job.spectrum]
    if not masses:
        return 1, 1000
    return max(1, int(min(masses))), max(int(round(max(masses))), int(min(masses)) + 1)


def hit_record(hit: dict) -> dict:
    return {k: hit.get(k) for k in HIT_FIELDS if hit.get(k) not in (None, "")}


def query_text(job: PeakJob) -> str:
    import gc_atlas
    return gc_atlas.msp_text({"spectrum": job.spectrum, "rt": job.rt,
                              "name": f"{job.label} Peak {job.peak_no} RT {job.rt:.3f}"})


def search_one(base: str, job: PeakJob, settings: IdentifySettings,
               rng: tuple[int, int]) -> list[dict]:
    """The hit list of one peak; ``rng`` is the batch's acquisition range."""
    import gc_atlas
    method = settings.method
    result = gc_atlas.request(base, "/api/analyze", {
        "text": query_text(job),
        "settings": SM.to_api_settings(method, SM.mz_range(method, rng))})
    hits = result.get("hits") or []
    return [dict(hit_record(h), peaks=h.get("peaks") or [])
            for h in hits[:max(1, min(settings.top_n, MAX_TOP_N))]]


class BatchSearch:
    """Search the jobs one after another in a worker thread.

    Results arrive on :attr:`messages` as ``("hit", index)``, ``("status", text)``,
    ``("error", text)`` and finally ``("done", cancelled)``. The server
    serialises searches anyway, so one worker is as fast as several.
    """

    def __init__(self, jobs: list[PeakJob], settings: IdentifySettings, *,
                 server: Optional[Callable[[], str]] = None,
                 indices: Optional[list[int]] = None):
        self.jobs = jobs
        self.indices = indices            # only these jobs (a re-search), else all open ones
        self.settings = settings
        self.messages: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.cancelled = threading.Event()
        self._server = server
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "BatchSearch":
        self.thread.start()
        return self

    def cancel(self) -> None:
        self.cancelled.set()

    def _run(self) -> None:
        import gc_atlas
        try:
            self.messages.put(("status", "EI Atlas wird gestartet (ohne Fenster) …"))
            base = self._server() if self._server else gc_atlas.ensure_server()
            gc_atlas.wait_ready(base, cancelled=self.cancelled.is_set,
                                progress=lambda st: self.messages.put(("status", gc_atlas.load_text(st))))
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            self.messages.put(("done", True))
            return
        rng = mz_range(self.jobs)
        todo = ([n for n in self.indices if self.jobs[n].spectrum] if self.indices is not None
                else [n for n, job in enumerate(self.jobs) if not job.done])
        for count, index in enumerate(todo, 1):
            if self.cancelled.is_set():
                break
            job = self.jobs[index]
            self.messages.put(("status", f"Suche {count} / {len(todo)}: Peak "
                                         f"{job.peak_no} ({job.label}), RT {job.rt:.3f}"))
            job.error, job.chosen = "", None
            try:
                job.hits = search_one(base, job, self.settings, rng)
            except Exception as exc:
                job.hits, job.error = [], str(exc)
            job.done = True
            self.messages.put(("hit", index))
        self.messages.put(("done", self.cancelled.is_set()))


# -- applying -------------------------------------------------------------

def apply_identification(sample, row, hits: list[dict], *, chosen: Optional[int],
                         min_score: float, libraries: list[str], method: str = "",
                         hydrocarbons: bool = False) -> None:
    """Write one search result onto ``row`` as its new loaded identification.

    Mirrors ``gc_fid._apply_pbm_match``: AutoLib's ``display_identification``
    turns a top hit below ``min_score`` into "possible derivative of …" or
    "unknown", exactly as for the ``.D``'s own library report. A hit the
    analyst picked in the compound table is taken as the identity. With
    ``hydrocarbons`` an accepted non-aromatic hydrocarbon becomes
    :data:`HYDROCARBON_NAME` with CAS 0; the hit lists keep the library names.
    """
    import gc_fid
    ordered = list(hits)
    if chosen is not None and 0 < chosen < len(ordered):
        ordered.insert(0, ordered.pop(chosen))
    triples = [(str(h.get("name") or ""), M.clean_cas(h.get("cas") or "") or "",
                int(h["score"]) if h.get("score") is not None else None)
               for h in ordered]
    if chosen is not None and triples:
        name, status, cas = triples[0][0], M.ID_ACCEPTED, triples[0][1]
    else:
        mod = gc_fid.engine()
        pbm = mod.PBMPeak(row.peak_no or 0, float(row.rt),
                          float(getattr(row, "area_pct", None) or 0.0),
                          [mod.Hit(n, "", c, q) for n, c, q in triples])
        name, status, cas = mod.display_identification(pbm, int(min_score))
    hydrocarbon = hydrocarbons and status == M.ID_ACCEPTED and is_hydrocarbon(ordered[0] if ordered else None)
    if hydrocarbon:
        name, cas = HYDROCARBON_NAME, ""
    row.name = name
    row.cas = M.clean_cas(cas) if cas else "0"
    row.si = triples[0][2] if triples else None
    row.alt_hits = triples[1:]
    row.id_status = status
    row.derived.update({
        "pbm_hits": triples,
        "atlas_hits": [hit_record(h) for h in ordered],
        "id_source": SOURCE,
        "id_libraries": list(libraries),
        "id_method": method,
        "id_status": status,
        "match_status": "EI Atlas" if triples else "No MS match",
        "id_searched_at": datetime.now().isoformat(timespec="seconds"),
    })
    if hydrocarbon:
        row.derived["hydrocarbon_of"] = triples[0][0]
    else:
        row.derived.pop("hydrocarbon_of", None)
    # The search replaces what was loaded: it is the new baseline, so
    # "Peak zurücksetzen" does not bring the old library report back.
    original = getattr(row, "original", None)
    if isinstance(original, dict):
        for key in ("name", "cas", "si"):
            original[key] = getattr(row, key)
    invalidate = getattr(sample, "invalidate_spectrum", None)
    if callable(invalidate):
        invalidate(row.row_id)


class Reidentify(gc_integrate.IntegrationCommand):
    """All assignments of one determination as one undo step."""

    def __init__(self, jobs: list[PeakJob], settings: IdentifySettings) -> None:
        super().__init__()
        self.jobs = [j for j in jobs if j.apply and j.done and not j.error]
        self.settings = settings
        self.label = f"Peak-Identifikation EI Atlas ({len(self.jobs)} Peaks)"

    def _perform(self, sample) -> None:
        rows = [sample.row(j.row_id) for j in self.jobs]
        if not any(r is not None for r in rows):
            raise ValueError("Keine der Zeilen existiert noch.")
        self._capture(sample, [r for r in rows if r is not None])
        for job, row in zip(self.jobs, rows):
            if row is None:
                continue
            apply_identification(sample, row, job.hits, chosen=job.chosen,
                                 min_score=self.settings.min_score,
                                 libraries=self.settings.libraries,
                                 method=self.settings.method.name,
                                 hydrocarbons=self.settings.method.hydrocarbons)
        gc_integrate._refresh(sample)


def summary(jobs: list[PeakJob], min_score: float) -> dict[str, int]:
    """Counts for the result line."""
    out = {"identified": 0, "below": 0, "none": 0, "protected": 0, "errors": 0}
    for job in jobs:
        if job.protected and not job.apply:
            out["protected"] += 1
        elif job.error:
            out["errors"] += 1
        elif not job.hits:
            out["none"] += 1
        elif (job.top.get("score") or 0) >= min_score or job.chosen is not None:
            out["identified"] += 1
        else:
            out["below"] += 1
    return out

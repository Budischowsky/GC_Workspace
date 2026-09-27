"""Automatic library search of integrated peaks (built-in engine over the analyst's libraries)."""
from __future__ import annotations

import queue
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal as QtSignal

from gcws.core.ident import Identification
from gcws.ms.spectra import extract
from gcws.ms.assignment import override_for, fragment_id


@dataclass
class SearchItem:
    run_id: str
    key: str
    peak_index: int
    apex_rt: float
    job: object                      # gc_identify.PeakJob
    spectrum_mode: str
    top_ions: list
    peak_id: str = ""


def search_methods():
    import gc_search_method as SM
    return SM.MethodStore()


# -- Fast search -------------------------------------------------------------------------------
# Which search methods use Fast search (``gcws.libsearch.fast``) is kept beside the method file,
# so that file keeps NIAS's format.

def fast_search_path():
    from gcws import paths
    return paths.DATA / "library_search_fast.json"


def fast_search_methods() -> set:
    """Names of the search methods with Fast search switched on."""
    import json
    try:
        data = json.loads(fast_search_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names = data.get("methods") if isinstance(data, dict) else None
    return {n for n in names or [] if isinstance(n, str)}


def is_fast(method) -> bool:
    """True when ``method`` (a search method or its name) searches with Fast search."""
    name = method if isinstance(method, str) else getattr(method, "name", "")
    return bool(name) and name in fast_search_methods()


def set_fast(name: str, on: bool) -> bool:
    """Switch Fast search on or off for the method ``name``; False when it could not be saved."""
    import json
    names = fast_search_methods()
    if on:
        names.add(name)
    else:
        names.discard(name)
    path = fast_search_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"methods": sorted(names)}, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return False
    return True


def build_items(ws, run_ids: list[str], key: str, spectrum_mode: str,
                rescan_below: Optional[float] = None, skip_identified: bool = False,
                only: Optional[dict] = None) -> tuple[list[SearchItem], int]:
    """One job per peak that has MS data; returns (items, protected count).

    ``only`` maps run id -> peak indices to search (e.g. the peaks the table filter shows);
    runs missing from it are skipped. None searches every peak."""
    import gc_identify as GI
    items, protected = [], 0
    for rid in run_ids:
        st = ws.runs.get(rid)
        res = ws.result(rid, key) if st else None
        if st is None or res is None or st.run.ms is None:
            continue
        idents, _ = st.ident_set(key).bind(res.peaks)
        wanted = None if only is None else only.get(rid, set())
        for i, p in enumerate(res.peaks):
            if wanted is not None and i not in wanted:
                continue
            ident = idents.get(i)
            if ident is not None and (ident.manual or ident.istd):
                protected += 1
                continue
            if ident is not None and skip_identified and ident.name:
                continue
            if rescan_below is not None and ident is not None and ident.score is not None \
                    and ident.score >= rescan_below:
                continue
            from gcws.ms import deconv_cache as DC
            dsettings = DC.settings_of(ws)
            spec = extract(st.run, p, key, st.delay_value, spectrum_mode,
                           override=override_for(st, key, p),
                           component=lambda st=st, p=p: DC.for_peak(st, p, key, dsettings))
            points = spec.points(min_permille=1.0) if spec is not None else []
            if not points:
                continue
            job = GI.PeakJob(label=st.name, row_id=i, peak_no=p.number, rt=p.apex_rt,
                             before=(ident.name if ident else "", ident.cas if ident else "",
                                     ident.score if ident else None),
                             spectrum=points)
            items.append(SearchItem(rid, key, i, p.apex_rt, job, spec.mode, spec.top_ions(3), fragment_id(p)))
    return items, protected


#: largest distance (min) between a TIC apex + FID-MS delay and the FID apex that takes its name
TRANSFER_TOL = 0.03


def transfer_names(ws, run_id: str, changes: list, fid_key: str = "FID", tol: float = TRANSFER_TOL):
    """Names found on TIC peaks for the FID peak at the same time.

    ``changes`` are ``(TIC apex, Identification)``; each goes to the FID peak whose apex lies
    within ``tol`` of TIC apex + the run's FID-MS delay. FID peaks named by hand or bound as
    ISTD keep their identification; when two TIC peaks meet one FID peak, the better score
    wins. Returns ``(FID changes, counts)`` with counts of copied / no FID peak / protected /
    co-eluting names.
    """
    import copy
    counts = {"copied": 0, "unmatched": 0, "protected": 0, "coeluting": 0}
    st = ws.runs.get(run_id)
    res = ws.result(run_id, fid_key) if st is not None else None
    if st is None or res is None or not res.peaks:
        return [], counts
    idents, _ = st.ident_set(fid_key).bind(res.peaks)
    apexes = [(p.extra['deconv_component']['rt'] + st.delay_value
               if p.extra.get('deconv_component') else p.apex_rt) for p in res.peaks]
    source_components = {fragment_id(p): p.extra['deconv_component']
                         for signal, result in st.results.items() if not signal.startswith('FID')
                         for p in result.peaks if p.extra.get('deconv_component')}
    best: dict[int, Identification] = {}
    for t, ident in changes:
        if ident is None or not ident.name:
            continue
        component = source_components.get(ident.peak_id)
        if ident.peak_id and component is None:
            counts['unmatched'] += 1
            continue
        target = (component['rt'] if component else t) + st.delay_value
        # A mixed TIC identification cannot choose between resolved FID components.
        candidates = [i for i, p in enumerate(res.peaks) if component or not p.extra.get('deconv_component')]
        if not candidates:
            counts['unmatched'] += 1
            continue
        j = min(candidates, key=lambda k: abs(apexes[k] - target))
        if abs(apexes[j] - target) > tol:
            counts["unmatched"] += 1
            continue
        prev = idents.get(j)
        if prev is not None and (prev.manual or prev.istd):
            counts["protected"] += 1
            continue
        if j in best:
            counts["coeluting"] += 1
            if (ident.score or 0) <= (best[j].score or 0):
                continue
        best[j] = ident
    out = []
    for j, ident in sorted(best.items()):
        new = copy.deepcopy(ident)
        new.apex_rt = res.peaks[j].apex_rt
        new.peak_id = fragment_id(res.peaks[j])
        new.source = (ident.source + " via TIC").strip()
        prev = idents.get(j)
        new.istd = prev.istd if prev is not None else ""
        out.append((new.apex_rt, new))
    counts["copied"] = len(out)
    return out, counts


def identification_from_hits(item: SearchItem, hits: list[dict], chosen: Optional[int],
                             method, prev: Optional[Identification] = None) -> Identification:
    """AutoLib rules: accepted above the quality limit, otherwise class or unknown."""
    import gc_fid
    import gc_identify as GI
    import gc_model as M
    ordered = list(hits)
    if chosen is not None and 0 < chosen < len(ordered):
        ordered.insert(0, ordered.pop(chosen))
    triples = [(str(h.get("name") or ""), M.clean_cas(h.get("cas") or "") or "",
                int(h["score"]) if h.get("score") is not None else None) for h in ordered]
    if chosen is not None and triples:
        name, status, cas = triples[0][0], M.ID_ACCEPTED, triples[0][1]
    else:
        eng = gc_fid.engine()
        pbm = eng.PBMPeak(item.job.peak_no, float(item.apex_rt), 0.0,
                          [eng.Hit(n, "", c, q) for n, c, q in triples])
        name, status, cas = eng.display_identification(pbm, int(method.min_score))
    if name.startswith("unknown") and item.top_ions:
        name = "unknown (m/z " + ", ".join(str(m) for m in item.top_ions) + ")"
    top = ordered[0] if ordered else {}
    if method.hydrocarbons and status == M.ID_ACCEPTED and GI.is_hydrocarbon(top or None):
        name, cas = GI.HYDROCARBON_NAME, ""
    return Identification(
        apex_rt=item.apex_rt, name=name, cas=M.clean_cas(cas) if cas else "",
        score=triples[0][2] if triples else None, status=status,
        formula=str(top.get("formula") or ""), library=str(top.get("library") or ""),
        hits=[dict(GI.hit_record(h), peaks=h.get("peaks") or []) for h in ordered],
        source=GI.SOURCE, method=method.name,
        searched_at=datetime.now().isoformat(timespec="seconds"), spectrum_mode=item.spectrum_mode,
        istd=prev.istd if prev else "", peak_id=item.peak_id)


def adapt_library_names(method, names) -> None:
    """Keep a method's library choices when a library is now listed under another name.

    EI Atlas named libraries by their path in its folder (``Library\\NIST17.L``); libraries
    added here are named by their folder or file (``NIST17.L``). A method entry that no
    longer exists takes the library with the same last path part."""
    from pathlib import PureWindowsPath
    present = set(names)
    by_tail = {}
    for n in names:
        for key in (PureWindowsPath(n).name.lower(), PureWindowsPath(n).stem.lower()):
            by_tail.setdefault(key, n)
    taken = {e.name for e in method.libraries if e.name in present}
    for e in method.libraries:
        if e.name in present:
            continue
        old = PureWindowsPath(e.name)
        new = by_tail.get(old.name.lower()) or by_tail.get(old.stem.lower())
        if new and new not in taken:
            e.name = new
            taken.add(new)


def prepare_local(method, progress=lambda t: None) -> dict:
    """Load the analyst's libraries and align ``method`` with them (worker thread)."""
    import gc_search_method as SM
    from gcws.libsearch import service as LS
    status = LS.status(progress)
    names = [x["name"] for x in SM.available_libraries(status)]
    if not names:
        raise RuntimeError("No library loaded. Add the libraries on this PC under Identify > Libraries...")
    adapt_library_names(method, names)
    SM.reconcile(method, status)
    if not method.enabled_libraries():
        for e in method.libraries:
            e.enabled = True
    return status


def search_spectrum(points, name: str, method, rng=None, lite: bool = True) -> list[dict]:
    """Hits of one spectrum in the method's libraries (built-in engine)."""
    import gc_search_method as SM
    from gcws.libsearch import service as LS
    masses = [m for m, _ in points]
    acquired = rng or (max(1, int(min(masses))), int(max(masses)) + 1)
    result = LS.analyze(points, name, SM.to_api_settings(method, SM.mz_range(method, acquired), lite=lite))
    return result.get("hits") or []


def search_one(job, method, rng) -> list[dict]:
    """The kept hits of one batch job (``gc_identify.search_one`` without the server)."""
    import gc_identify as GI
    hits = search_spectrum(job.spectrum, f"{job.label} Peak {job.peak_no} RT {job.rt:.3f}", method, rng)
    return [dict(GI.hit_record(h), peaks=h.get("peaks") or [])
            for h in hits[:max(1, min(method.top_n, GI.MAX_TOP_N))]]


class LocalBatchSearch:
    """``gc_identify.BatchSearch`` on the built-in engine: same messages, no EI Atlas.

    ``("status", text)``, ``("hit", index)``, ``("error", text)``, finally ``("done", cancelled)``.
    """

    def __init__(self, jobs, method):
        import threading
        self.jobs, self.method = jobs, method
        self.messages: "queue.Queue" = queue.Queue()
        self.cancelled = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def cancel(self):
        self.cancelled.set()

    def _run(self):
        import gc_identify as GI
        try:
            self.messages.put(("status", "Loading the libraries ..."))
            prepare_local(self.method, lambda t: self.messages.put(("status", t)))
        except Exception as exc:  # noqa: BLE001
            self.messages.put(("error", str(exc)))
            self.messages.put(("done", True))
            return
        rng = GI.mz_range(self.jobs)
        todo = [n for n, job in enumerate(self.jobs) if not job.done]
        if len(todo) > 1 and is_fast(self.method):
            self._run_fast(todo, rng)
            self.messages.put(("done", self.cancelled.is_set()))
            return
        for count, index in enumerate(todo, 1):
            if self.cancelled.is_set():
                break
            job = self.jobs[index]
            self.messages.put(("status", f"Searching {count} / {len(todo)}: peak {job.peak_no} ({job.label}), "
                                         f"RT {job.rt:.3f}"))
            job.error, job.chosen = "", None
            try:
                job.hits = search_one(job, self.method, rng)
            except Exception as exc:  # noqa: BLE001
                job.hits, job.error = [], str(exc)
            job.done = True
            self.messages.put(("hit", index))
        self.messages.put(("done", self.cancelled.is_set()))

    def _run_fast(self, todo, rng):
        """Fast search: every peak at once, with the hits the search one by one gives."""
        import gc_identify as GI
        import gc_search_method as SM
        from gcws.libsearch import service as LS
        settings = SM.to_api_settings(self.method, SM.mz_range(self.method, rng), lite=True)
        keep = max(1, min(self.method.top_n, GI.MAX_TOP_N))
        jobs = [self.jobs[n] for n in todo]

        def done(i, result):
            job = jobs[i]
            job.error, job.chosen = "", None
            if isinstance(result, BaseException):
                job.hits, job.error = [], str(result)
            else:
                job.hits = [dict(GI.hit_record(h), peaks=h.get("peaks") or [])
                            for h in (result.get("hits") or [])[:keep]]
            job.done = True
            self.messages.put(("hit", todo[i]))

        self.messages.put(("status", f"Fast search: {len(jobs)} peaks at once"))
        try:
            LS.analyze_many([(f"{job.label} Peak {job.peak_no} RT {job.rt:.3f}", job.spectrum) for job in jobs],
                            settings, progress=lambda t: self.messages.put(("status", t)),
                            cancelled=self.cancelled.is_set, done=done)
        except Exception as exc:  # noqa: BLE001
            self.messages.put(("error", str(exc)))


class LibrarySearchWorker(QObject):
    """Runs the batch search in a thread and reports progress on the GUI thread."""
    progress = QtSignal(str)
    hit = QtSignal(int)
    finished = QtSignal(bool)
    failed = QtSignal(str)

    def __init__(self, items: list[SearchItem], method, parent=None):
        super().__init__(parent)
        self.items = items
        self.method = method
        self.batch = LocalBatchSearch([it.job for it in items], method)
        self.timer = QTimer(self)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._drain)
        self.done = 0

    def start(self):
        self.batch.start()
        self.timer.start()

    def cancel(self):
        self.batch.cancel()

    def _drain(self):
        while True:
            try:
                kind, value = self.batch.messages.get_nowait()
            except queue.Empty:
                return
            if kind == "status":
                self.progress.emit(str(value))
            elif kind == "hit":
                self.done += 1
                self.hit.emit(int(value))
            elif kind == "error":
                self.failed.emit(str(value))
            elif kind == "done":
                self.timer.stop()
                self.finished.emit(bool(value))
                return


def _english(text: str) -> str:
    return (text.replace("EI Atlas wird gestartet (ohne Fenster)", "Starting EI Atlas (no window)")
                .replace("Suche ", "Searching ").replace("Peak ", "peak "))

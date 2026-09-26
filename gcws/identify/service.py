"""Automatic library search of integrated peaks (EI Atlas backend)."""
from __future__ import annotations

import queue
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal as QtSignal

from gcws.core.ident import Identification
from gcws.ms.spectra import extract


@dataclass
class SearchItem:
    run_id: str
    key: str
    peak_index: int
    apex_rt: float
    job: object                      # gc_identify.PeakJob
    spectrum_mode: str
    top_ions: list


def search_methods():
    import gc_search_method as SM
    return SM.MethodStore()


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
                           override=st.spectrum_overrides.get(round(p.apex_rt, 4)),
                           component=lambda st=st, p=p: DC.for_peak(st, p, key, dsettings))
            points = spec.points(min_permille=1.0) if spec is not None else []
            if not points:
                continue
            job = GI.PeakJob(label=st.name, row_id=i, peak_no=p.number, rt=p.apex_rt,
                             before=(ident.name if ident else "", ident.cas if ident else "",
                                     ident.score if ident else None),
                             spectrum=points)
            items.append(SearchItem(rid, key, i, p.apex_rt, job, spectrum_mode, spec.top_ions(3)))
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
    apexes = [p.apex_rt for p in res.peaks]
    best: dict[int, Identification] = {}
    for t, ident in changes:
        if ident is None or not ident.name:
            continue
        target = t + st.delay_value
        j = min(range(len(apexes)), key=lambda k: abs(apexes[k] - target))
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
        istd=prev.istd if prev else "")


def prepare_server(method) -> str:
    """Start/find EI Atlas and align the method with its libraries (worker thread)."""
    import gc_atlas
    import gc_search_method as SM
    base = gc_atlas.ensure_server()
    gc_atlas.wait_ready(base)
    status = gc_atlas.request(base, "/api/status")
    SM.reconcile(method, status)
    if not method.enabled_libraries():
        for e in method.libraries:
            e.enabled = True
    return base


class LibrarySearchWorker(QObject):
    """Runs ``gc_identify.BatchSearch`` and reports progress on the GUI thread."""
    progress = QtSignal(str)
    hit = QtSignal(int)
    finished = QtSignal(bool)
    failed = QtSignal(str)

    def __init__(self, items: list[SearchItem], method, parent=None):
        super().__init__(parent)
        import gc_identify as GI
        self.items = items
        self.method = method
        self.settings = GI.IdentifySettings(method=method)
        self.batch = GI.BatchSearch([it.job for it in items], self.settings,
                                    server=lambda: prepare_server(self.method))
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
                self.progress.emit(_english(str(value)))
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

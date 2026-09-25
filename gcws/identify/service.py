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
                rescan_below: Optional[float] = None, skip_identified: bool = False) -> tuple[list[SearchItem], int]:
    """One job per peak that has MS data; returns (items, protected count)."""
    import gc_identify as GI
    items, protected = [], 0
    for rid in run_ids:
        st = ws.runs.get(rid)
        res = ws.result(rid, key) if st else None
        if st is None or res is None or st.run.ms is None:
            continue
        idents, _ = st.ident_set(key).bind(res.peaks)
        for i, p in enumerate(res.peaks):
            ident = idents.get(i)
            if ident is not None and (ident.manual or ident.istd):
                protected += 1
                continue
            if ident is not None and skip_identified and ident.name:
                continue
            if rescan_below is not None and ident is not None and ident.score is not None \
                    and ident.score >= rescan_below:
                continue
            spec = extract(st.run, p, key, st.delay_value, spectrum_mode,
                           override=st.spectrum_overrides.get(round(p.apex_rt, 4)))
            points = spec.points(min_permille=1.0) if spec is not None else []
            if not points:
                continue
            job = GI.PeakJob(label=st.name, row_id=i, peak_no=p.number, rt=p.apex_rt,
                             before=(ident.name if ident else "", ident.cas if ident else "",
                                     ident.score if ident else None),
                             spectrum=points)
            items.append(SearchItem(rid, key, i, p.apex_rt, job, spectrum_mode, spec.top_ions(3)))
    return items, protected


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
        self.batch = GI.BatchSearch([it.job for it in items], self.settings)
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

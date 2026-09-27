"""Class hints for the peak table, computed lazily in small batches.

The "Class hint" column asks :meth:`HintCache.get` for each painted row. A hint
not yet known is queued and computed on the GUI thread a few peaks at a time
(spectrum extraction + interpretation take a few ms each), so a large table
fills in progressively without blocking. Hints are dropped when a run's
integration changes.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal as QtSignal

BATCH = 6


class HintCache(QObject):
    updated = QtSignal(str)                    # run id whose hints changed

    def __init__(self, ws, parent=None):
        super().__init__(parent)
        self.ws = ws
        self._hints: dict[tuple, tuple[str, str]] = {}
        self._queue: list[tuple] = []
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._work)
        ws.resultChanged.connect(lambda rid, key: self.invalidate(rid))
        ws.runRemoved.connect(self.invalidate)
        ws.spectrumChanged.connect(self.invalidate)

    @staticmethod
    def _key(run_id, signal_key, peak):
        from gcws.ms.assignment import fragment_id
        return (run_id, signal_key, round(peak.apex_rt, 4), round(peak.start, 4), round(peak.end, 4),
                fragment_id(peak))

    def get(self, st, signal_key, peak) -> tuple[str, str] | None:
        """``(short text, tooltip)`` or None while it is being computed."""
        if st is None or st.run.ms is None:
            return ("", "")
        k = self._key(st.id, signal_key, peak)
        hit = self._hints.get(k)
        if hit is None and k not in self._queue:
            self._queue.append(k)
            self._timer.start()
        return hit

    def invalidate(self, run_id: str) -> None:
        self._hints = {k: v for k, v in self._hints.items() if k[0] != run_id}
        self._queue = [k for k in self._queue if k[0] != run_id]

    def _work(self):
        from gcws.ms.interpret import Context, interpret
        from gcws.ms.spectra import extract
        from gcws.ms.assignment import override_for
        done = set()
        for _ in range(BATCH):
            if not self._queue:
                break
            k = self._queue.pop(0)
            rid, skey, apex, _s, _e, identity = k
            st = self.ws.runs.get(rid)
            res = self.ws.result(rid, skey) if st is not None else None
            peak = next((p for p in res.peaks if self._key(rid, skey, p) == k), None) if res is not None else None
            if peak is None or st.run.ms is None:
                continue
            try:
                spec = extract(st.run, peak, skey, st.delay_value, "average_bg",
                               override=override_for(st, skey, peak))
                ms = st.run.ms
                r = interpret(spec.mz, spec.ab, Context(mass_range=ms.mass_range(), min_abundance=ms.min_abundance()))
            except Exception:  # noqa: BLE001 - a hint is optional
                self._hints[k] = ("", "")
                continue
            if r.classes:
                c = r.classes[0]
                text = f"{c.label} ({c.level})"
            elif r.compounds:
                text = f"like {r.compounds[0].name}"
            else:
                text = "-"
            self._hints[k] = (text, r.summary)
            done.add(rid)
        if not self._queue:
            self._timer.stop()
        for rid in done:
            self.updated.emit(rid)

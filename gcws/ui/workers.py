"""Background jobs on the Qt thread pool."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal as QtSignal


class WorkerSignals(QObject):
    finished = QtSignal(object)
    failed = QtSignal(str)
    progress = QtSignal(str)


class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = WorkerSignals()


    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the GUI
            self.signals.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=4)}")
            return
        self.signals.finished.emit(result)


#: running workers; the Python wrappers (and their signal objects) must stay
#: alive until the job has reported back, or the result is silently lost
_RUNNING: set = set()


def submit(fn, *args, on_done=None, on_error=None, **kwargs) -> Worker:
    w = Worker(fn, *args, **kwargs)
    w.setAutoDelete(False)
    _RUNNING.add(w)
    release = lambda *_: _RUNNING.discard(w)
    if on_done:
        w.signals.finished.connect(on_done)
    if on_error:
        w.signals.failed.connect(on_error)
    w.signals.finished.connect(release)
    w.signals.failed.connect(release)
    QThreadPool.globalInstance().start(w)
    return w


def load_and_integrate(path, role: str, workspace_methods):
    """Worker body: read raw data, estimate the FID-MS offset, integrate."""
    from gcws.core.model import FID, TIC
    from gcws.integration.engine import integrate
    from gcws.io.run_loader import load_run
    from gcws.signal.delay import estimate_delay
    run = load_run(path)
    if role:
        run.role = role
    results = {}
    for key in run.available_signals():
        if key in (FID, TIC):
            kind = FID if key == FID else TIC
            m = workspace_methods.get(workspace_methods.default_name(kind))
            results[key] = integrate(run.signal(key), m)
    delay = None
    if run.fid is not None and run.ms is not None:
        delay = estimate_delay(run.fid, run.signal(TIC))
    return run, results, delay

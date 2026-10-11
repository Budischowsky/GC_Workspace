"""Background jobs on the Qt thread pool."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QThreadPool, Signal as QtSignal, Slot


class _Relay(QObject):
    """One QObject for the whole app that carries every job's callbacks to the GUI thread.
    A QObject per job was lost now and then while its job ran (PySide: "Signal source has
    been deleted" at the final emit), and the report preview then spun for ever."""
    call = QtSignal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._call)

    @Slot(object)
    def _call(self, fn):
        fn()


_RELAY: _Relay | None = None


def _relay() -> _Relay:
    """Created on first use, from the GUI thread (``submit`` runs there), and kept for good."""
    global _RELAY
    if _RELAY is None:
        _RELAY = _Relay()
    return _RELAY


class Worker:
    """One job. A plain object whose ``run`` the pool calls: a QRunnable started with auto-delete off
    belonged to Qt for good, and every job kept its arguments (loaded runs with their MS data)."""

    def __init__(self, fn, *args, on_done=None, on_error=None, on_progress=None, **kwargs):
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.on_done, self.on_error, self.on_progress = on_done, on_error, on_progress
        self.relay = _relay()

    def _post(self, callback, value, last=False):
        def deliver():
            if last:
                _RUNNING.discard(self)
            if callback is not None:
                callback(value)
        self.relay.call.emit(deliver)

    def progress(self, text):
        self._post(self.on_progress, text)

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the GUI
            self._post(self.on_error, f"{exc}\n\n{traceback.format_exc(limit=4)}", last=True)
            return
        self._post(self.on_done, result, last=True)


#: running workers; kept until the job has reported back on the GUI thread
_RUNNING: set = set()


def submit(fn, *args, on_done=None, on_error=None, on_progress=None, with_progress=False, **kwargs) -> Worker:
    """Run ``fn(*args, **kwargs)`` on the thread pool.

    With ``with_progress`` the job gets a ``progress(text)`` keyword argument that
    is delivered to ``on_progress`` on the GUI thread.
    """
    w = Worker(fn, *args, on_done=on_done, on_error=on_error, on_progress=on_progress, **kwargs)
    if with_progress:
        w.kwargs["progress"] = w.progress
    _RUNNING.add(w)
    QThreadPool.globalInstance().start(w.run)
    return w


def load_and_integrate(path, role: str, workspace_methods, integrate: bool = True):
    """Worker body: read raw data, estimate the FID-MS offset, integrate (``integrate``; runs loaded
    fresh are only read until Method > Run Method, runs of a project are integrated again)."""
    from gcws.core.model import FID, TIC
    from gcws.integration.engine import integrate
    from gcws.io.run_loader import load_run
    from gcws.signal.delay import estimate_delay
    run = load_run(path)
    if role:
        run.role = role
    results = {}
    for key in run.available_signals() if integrate else ():
        if key in (FID, TIC):
            kind = FID if key == FID else TIC
            m = workspace_methods.get(workspace_methods.default_name(kind))
            results[key] = integrate(run.signal(key), m)
    delay = None
    if run.fid is not None and run.ms is not None:
        delay = estimate_delay(run.fid, run.signal(TIC))
    return run, results, delay

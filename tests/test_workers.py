"""Background jobs report back on the GUI thread through the one app-wide relay."""
import threading
import time

from PySide6.QtWidgets import QApplication

from gcws.ui import workers


def _wait(app, cond, timeout=10.0):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def test_result_error_and_progress_arrive_on_gui_thread():
    app = QApplication.instance() or QApplication([])
    gui = threading.get_ident()
    got = {}

    def job(progress):
        progress("half")
        return threading.get_ident()

    workers.submit(job, with_progress=True,
                   on_done=lambda r: got.update(done=(r, threading.get_ident())),
                   on_progress=lambda t: got.update(progress=(t, threading.get_ident())))
    workers.submit(lambda: 1 / 0, on_error=lambda e: got.update(error=(e, threading.get_ident())))
    assert _wait(app, lambda: {"done", "error", "progress"} <= got.keys())
    assert got["done"][0] != gui and got["done"][1] == gui          # ran in the pool, reported in the GUI
    assert got["progress"] == ("half", gui)
    assert "division by zero" in got["error"][0] and got["error"][1] == gui
    assert _wait(app, lambda: not workers._RUNNING)                   # finished jobs are let go


def test_many_jobs_all_report_back():
    app = QApplication.instance() or QApplication([])
    seen = []
    for i in range(50):
        workers.submit(lambda i=i: (time.sleep(0.005), i)[1], on_done=seen.append)
    assert _wait(app, lambda: len(seen) == 50)
    assert sorted(seen) == list(range(50))


def test_finished_jobs_let_go_of_their_arguments():
    """A job's arguments (a loaded run with its MS data) are freed once the job has reported back:
    jobs were kept by Qt for good, and every load, deconvolution and report stayed in memory."""
    import gc
    import weakref
    from PySide6.QtCore import QThreadPool
    app = QApplication.instance() or QApplication([])

    class Payload:
        pass

    refs, seen = [], []
    for _ in range(5):
        payload = Payload()
        refs.append(weakref.ref(payload))
        workers.submit(lambda p: None, payload, on_done=seen.append)
        del payload
    assert _wait(app, lambda: len(seen) == 5 and not workers._RUNNING)
    QThreadPool.globalInstance().waitForDone()
    gc.collect()
    assert [r() for r in refs] == [None] * 5

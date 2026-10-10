"""How smooth is the main window while panels are resized? (developer tool)

Starts the real main window on a copy of a data folder (its layout and settings), loads runs,
then drags the panel separators and the window size in small steps; every step is laid out and
painted synchronously, as a mouse drag does. Prints the time per step (median / p90 / max).

    .venv/Scripts/python tools/ui_benchmark.py --data <copy of data> --samples <batch folder>
        [--runs 8] [--no-process] [--freeze] [--profile] [--theme light|dark|neon]

Never point --data at the live data folder: the window writes its settings on close.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _stats(name, times):
    ms = sorted(t * 1000 for t in times)
    p90 = ms[int(0.9 * (len(ms) - 1))]
    print(f"{name:<28} n={len(ms):3d}  median {statistics.median(ms):7.1f} ms  p90 {p90:7.1f} ms  "
          f"max {ms[-1]:7.1f} ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--samples", required=True)
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--no-process", action="store_true")
    ap.add_argument("--freeze", action="store_true", help="freeze the plots while dragging (gcws.ui.freeze)")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--theme", default="")
    ap.add_argument("--steps", type=int, default=40)
    ap.add_argument("--close", default="", help="comma-separated panels to close first (attribution)")
    args = ap.parse_args()
    os.environ["GCWS_DATA"] = str(Path(args.data).resolve())

    import gcws  # noqa: F401
    from gcws import paths
    paths.initialize()
    from PySide6.QtCore import QCoreApplication, QSettings, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
    QCoreApplication.setOrganizationName("GCWorkspace")
    QCoreApplication.setApplicationName("GC Workspace")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(paths.DATA))
    app = QApplication.instance() or QApplication(sys.argv)
    from gcws.ui import theme
    theme.apply(app)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    from gcws.ui.main_window import MainWindow
    win = MainWindow()
    win.automation.start_with_app = lambda: None
    win.show()
    if args.theme:
        win.theme_actions[args.theme].trigger()
    screen = app.primaryScreen().availableGeometry()
    win.setGeometry(screen.left() + 20, screen.top() + 40, min(1800, screen.width() - 40),
                    min(1000, screen.height() - 80))
    app.processEvents()

    samples = Path(args.samples)
    runs = sorted(p for p in samples.glob("*.D") if p.is_dir())[:args.runs]
    t0 = time.perf_counter()
    win.load_runs([str(p) for p in runs])
    while win.loading or len(win.ws.runs) < len(runs):
        app.processEvents()
        time.sleep(0.01)
    if not args.no_process:
        win.ws.process_runs()
    for _ in range(20):
        app.processEvents()
    print(f"loaded {len(runs)} runs in {time.perf_counter() - t0:.1f} s; "
          f"visible docks: {[k for k, d in win.docks.items() if d.isVisible()]}")
    try:
        import psutil
        print(f"memory: {psutil.Process().memory_info().rss / 2**20:.0f} MB")
    except ImportError:
        pass

    for key in filter(None, args.close.split(",")):
        win.docks[key].close()
    for _ in range(5):
        app.processEvents()

    freeze = None
    if args.freeze:
        from gcws.ui import freeze

    def step_all(fn, n):
        times = []
        for i in range(n):
            t = time.perf_counter()
            fn(i)
            app.processEvents()
            win.repaint()
            times.append(time.perf_counter() - t)
        return times

    chrom = win.docks["chrom"]
    w0, h0 = chrom.width(), chrom.height()
    geo = win.geometry()

    def sep_h(i):
        win.resizeDocks([chrom], [w0 + (i % 10 - 5) * 25], Qt.Horizontal)

    def sep_v(i):
        win.resizeDocks([chrom], [h0 + (i % 10 - 5) * 15], Qt.Vertical)

    def window(i):
        win.resize(geo.width() + (i % 10 - 5) * 20, geo.height() + (i % 10 - 5) * 10)

    scenarios = (("separator (horizontal)", sep_h), ("separator (vertical)", sep_v), ("window resize", window))
    prof = cProfile.Profile() if args.profile else None
    for name, fn in scenarios:
        if freeze:
            freeze._native = True            # as if the mouse were held: the safety check stays out
            freeze.arm()
        if prof:
            prof.enable()
        times = step_all(fn, args.steps)
        if prof:
            prof.disable()
        t = time.perf_counter()
        if freeze:
            freeze._native = False
            freeze.disarm()
            app.processEvents()
            win.repaint()
        release = time.perf_counter() - t
        _stats(name, times)
        if freeze:
            print(f"{'':<28} release (one full layout + paint): {release * 1000:.1f} ms")
    if prof:
        out = io.StringIO()
        st = pstats.Stats(prof, stream=out).sort_stats("tottime")
        st.print_stats(30)
        st.sort_stats("cumulative").print_stats(40)
        print(out.getvalue())
    win.ws.dirty = False
    win.close()


if __name__ == "__main__":
    main()

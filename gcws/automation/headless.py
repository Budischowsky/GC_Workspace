"""The workspace without the main window: load runs, open a project, search the libraries.

Needs a ``QApplication`` (the workspace keeps undo stacks and settings) but no event loop:
everything here runs synchronously.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from gcws.core.model import FID, TIC


def new_workspace():
    from gcws.ui.workspace import Workspace
    return Workspace()


def load_one(ws, path):
    """Load and integrate one run (``ws.default_methods`` / the method store's defaults)."""
    from gcws.integration.engine import integrate
    from gcws.io.run_loader import load_run
    from gcws.signal.delay import estimate_delay
    run = load_run(path)
    results = {}
    for key in run.available_signals():
        if key in (FID, TIC):
            results[key] = integrate(run.signal(key), ws.default_method(FID if key == FID else TIC))
    delay = estimate_delay(run.fid, run.signal(TIC)) if run.fid is not None and run.ms is not None else None
    return run, results, delay


def add_runs(ws, paths, progress: Callable[[str], None] = lambda t: None) -> list[str]:
    """Load ``paths`` into ``ws`` (injection order), then suggest the blanks from the complete set.

    Returns the error texts of runs that could not be read."""
    from gcws.io import sequence
    errors = []
    for p in sequence.run_order([Path(x) for x in paths]):
        progress(f"loading {Path(p).name}")
        try:
            run, results, delay = load_one(ws, p)
        except Exception as exc:  # noqa: BLE001 - reported per run
            errors.append(f"{Path(p).name}: {exc}")
            continue
        ws.add_run(run, results, delay=delay)
    ws.resuggest_blanks()
    return errors


def open_project(ws, path) -> list[str]:
    """``MainWindow.open_project`` without dialogs; returns notes (missing raw data, changed integrations)."""
    from gcws.core import project as P
    path = Path(path)
    data = P.read(path)
    ws.audit.load(data.get("audit"))
    ws.replicate_groups = data.get("replicate_groups", [])
    ws.quant = data.get("quant", {})
    panels = data.get("panels") or {}
    if panels.get("keys"):
        ws.set_panels(panels["keys"], panels.get("blank") or [False, False], panels.get("table", 0))
    notes = []
    for entry in data.get("runs", []):
        rp = P.resolve_run_path(entry, path)
        if rp is None:
            notes.append(f"raw data not found: {entry.get('name', '?')}")
            continue
        try:
            run, results, delay = load_one(ws, rp)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{entry.get('name', '?')}: {exc}")
            continue
        run.id = entry["id"]
        st = ws.add_run(run, results, delay=delay)
        notes += P.apply_run_state(st, entry)
        for key in list(st.results):
            ws.integrate(st.id, key, emit=False)
    order = [e["id"] for e in data.get("runs", []) if e["id"] in ws.runs]
    ws.reorder(order + [i for i in ws.order if i not in order])
    ws.invalidate_blank(None)
    for st in ws.states():
        for key, dig in st.saved_digests.items():
            res = ws.result(st.id, key)
            if res is not None and res.digest != dig:
                notes.append(f"integration differs from the saved state: {st.name} ({key})")
    ws.project_path = path
    ws.dirty = False
    return notes


def identify(ws, run_ids: list[str], cfg, progress: Callable[[str], None] = lambda t: None,
             timeout: Optional[float] = None):
    """Library search of every peak of ``run_ids`` (``cfg``: :class:`gcws.core.proc_method.SearchConfig`);
    the names are applied as the interactive search does (TIC names copied to the FID peaks)."""
    from gcws.identify.service import apply_search_results, build_items, run_search_blocking
    key = cfg.target if cfg.target in (FID, TIC) else TIC
    items, protected = build_items(ws, run_ids, key, cfg.mode)
    if not items:
        return None
    done = run_search_blocking(items, cfg.method, fast=cfg.fast, progress=progress, timeout=timeout)
    return apply_search_results(ws, done, cfg.method, transfer=cfg.transfer, fid_key=FID)

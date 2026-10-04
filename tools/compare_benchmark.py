"""Time the Compare of the double determination on A/B pairs of a batch (read-only).

Usage:
  python tools/compare_benchmark.py [--samples FOLDER] [--pairs 07_:11_,09_:12_] [--load 06_,08_,...]
                                    [--reference] [--out report.md]

Replays what the Replicates / results panel does on *Compare*: the feature double determination
with its automatic changes, the background search of the consensus spectra, and the automatic
second compare when that search is done. Prints the time of each part and a digest of the end
state (peaks, areas, identifications, features, lights, proposals), so that two runs - e.g. one with
``--reference``, which uses today's reference routes (uncached trace fits, the legacy closer look,
the vendored library norms, no Fast search) - can be compared.

The methods and libraries come from ``GCWS_DATA``: point it at a *copy* of the ``data`` folder to
time with the real integration method and libraries (a junction to ``data/libcache`` saves
rebuilding the library index). The raw data are only read; nothing is saved.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_SAMPLES = ROOT.parent / "NIAS Working" / "samples" / "26016605_GIOSUN1635_4"


def reference_mode(setattr_) -> None:
    """Today's reference routes instead of the fast ones (``setattr_(obj, name, value)``)."""
    import gcws.libsearch  # noqa: F401  (vendor on sys.path)
    import engine
    from gcws.identify import service as IS
    from gcws.libsearch import service as LS
    from gcws.ms import component_fit as F
    from gcws.ms import deconv_probe as P
    setattr_(F, "fit_trace", F.fit_trace_uncached)
    setattr_(P, "probe", P.probe_reference)
    setattr_(LS.LocalEngine, "shard_norms", functools.lru_cache(maxsize=64)(engine.Engine.shard_norms.__wrapped__))
    setattr_(IS, "fast_search_methods", lambda: set())


def load(samples: Path, prefixes: list[str], fid_overrides: dict | None = None):
    """A workspace with the runs ``prefixes`` of ``samples``, FID integrated by the default method
    (with ``fid_overrides``) through the workspace, and the ChemStation LIB hits bound."""
    from gcws.core.model import FID
    from tools.duplicate_benchmark import _bind_lib_hits
    from gcws.io.run_loader import load_run
    from gcws.signal.delay import estimate_delay
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    method = ws.methods.get(ws.methods.default_name(FID))
    if fid_overrides:
        method = method.copy(**fid_overrides)
    for prefix in prefixes:
        d = next(Path(samples).glob(prefix + "*.D"))
        run = load_run(d)
        st = ws.add_run(run, delay=estimate_delay(run.fid, run.signal("TIC")))
        st.methods[FID] = method
        ws.integrate(st.id, FID, emit=False)
        _bind_lib_hits(ws, st, d)
    ws.recompute_quant()
    return ws


def group(ws, a: str, b: str) -> list[str]:
    """The run ids of the pair ``a``/``b`` as one replicate group, with the feature pairing."""
    ids = [next(s.id for s in ws.states() if s.name.startswith(p)) for p in (a, b)]
    ws.replicate_groups = [g for g in ws.replicate_groups if g.get("members") != ids]
    ws.replicate_groups.append({"id": f"bench-{a}{b}", "name": f"{a}{b}", "members": ids, "policy": "all"})
    ws.quant.setdefault("features", {})["pairing"] = "features"
    return ids


def _compare(ws, ids):
    """``DuplicatePanel._compare(sync=True)`` without the widgets."""
    from gcws.features import service as SV
    from gcws.quant import duplicate_view as DV
    stack = ws.project_undo
    before = (stack.count(), stack.index())
    table = SV.run(ws, ids, apply_auto=None, stack=stack, search=False)
    if (stack.count(), stack.index()) != before:
        ws.recompute_quant()
    DV.features_table(ws, ids, table)
    DV.compute(ws, ids, "all")
    return table


def replay(ws, ids) -> dict:
    """Compare, the consensus search, the second compare: their times and the final table."""
    from gcws.features import service as SV
    from gcws.features.consensus import search_consensus
    t0 = time.perf_counter()
    table = _compare(ws, ids)
    t1 = time.perf_counter()
    cfg = SV.settings(ws)
    needed = SV.consensus_needed(ws, table, cfg)
    if needed and cfg.consensus_search:
        note = search_consensus(None, SV.search_method(ws, table.members), [f for _k, f in needed])
        SV.store_consensus(ws, needed, note, group=SV.group_of(ws, ids))
    t2 = time.perf_counter()
    table = _compare(ws, ids)
    t3 = time.perf_counter()
    return {"compare": t1 - t0, "search": t2 - t1, "spectra": len(needed), "second": t3 - t2,
            "total": t3 - t0, "table": table}


def signature(ws, ids, table) -> list:
    """The end state: each run's FID peaks and identifications, then the features."""
    from gcws.core.model import FID
    out = []
    for rid in ids:
        res = ws.result(rid, FID)
        out.append([(round(p.start, 9), round(p.end, 9), round(p.apex_rt, 9), repr(p.area), p.origin)
                    for p in res.peaks])
        out.append([(round(i.apex_rt, 6), i.name, i.cas, i.source) for i in ws.runs[rid].ident_set(FID).items])
    out.append([(f.id, round(f.rt, 9), f.light, [(m.origin, m.note) for m in f.members],
                 (f.identity.name, f.identity.case, f.identity.status) if f.identity else None,
                 [p.text for p in f.proposals]) for f in table.features])
    return out


def digest(sig: list) -> str:
    return hashlib.sha1(json.dumps(sig, default=str).encode()).hexdigest()[:12]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    ap.add_argument("--pairs", default="07_:11_,09_:12_")
    ap.add_argument("--load", default="", help="run prefixes to load (default: every run of the folder)")
    ap.add_argument("--reference", action="store_true", help="today's reference routes")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    os.environ.setdefault("GCWS_DATA", str(ROOT / "data"))
    import gcws  # noqa: F401
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    if args.reference:
        reference_mode(setattr)
    samples = Path(args.samples)
    prefixes = [p for p in args.load.split(",") if p] or sorted(d.name.split("_")[0] + "_" for d in samples.glob("*.D"))
    t0 = time.perf_counter()
    ws = load(samples, prefixes)
    lines = [f"# Compare timing ({'reference' if args.reference else 'fast'}; GCWS_DATA={os.environ['GCWS_DATA']})", "",
             f"Loaded {len(prefixes)} runs in {time.perf_counter() - t0:.1f} s.", "",
             "| pair | Compare click | consensus search | second compare | total | end state |",
             "|---|---|---|---|---|---|"]
    for pair in args.pairs.split(","):
        a, b = pair.split(":")
        ids = group(ws, a, b)
        r = replay(ws, ids)
        lines.append(f"| {a}{b} | {r['compare']:.2f} s | {r['search']:.2f} s ({r['spectra']} spectra) | "
                     f"{r['second']:.2f} s | {r['total']:.2f} s | {digest(signature(ws, ids, r['table']))} |")
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

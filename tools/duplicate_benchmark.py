"""Classic (AutoLib) vs feature double determination on A/B pairs of a batch (read-only).

Usage:
  python tools/duplicate_benchmark.py [--samples FOLDER] [--pairs 07_:11_,09_:12_] [--lib-oracle]
                                      [--blanks 06_,08_] [--out report.md]

Loads the runs (the raw data are only read), integrates them with the default methods and
compares, per pair:

* classic: AutoLib's pairing -- valid pairs, identification conflicts, artefacts (one-sided);
* features: pairs, one-sided, drift, gap fills made, identity cases (A/B/C/D/U), traffic light,
  and the time taken.

``--lib-oracle`` binds the hits of the ChemStation LIB report of each run (as the tests do) so
the identification part has data without a library search.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEFAULT_SAMPLES = ROOT.parent / "NIAS Working" / "samples" / "26016605_GIOSUN1635"


def _bind_lib_hits(ws, st, d: Path) -> None:
    """ChemStation LIB hits as identifications (the test oracle of tests/test_report.py)."""
    from gcws.core.ident import Identification
    from gcws.core.model import FID
    from gcws.quant.nias_bridge import engine
    if not (d / "RESULTS.CSV").exists():
        return
    eng = engine()
    _tic, _fid, pbm = eng.parse_results(str(d / "RESULTS.CSV"))
    lib = eng.parse_library_report(str(d / "LIB"))
    res = st.results[FID]
    for p in pbm:
        hits = lib.get(p.peak) or p.hits
        if not hits:
            continue
        peak = min(res.peaks, key=lambda x: abs(x.apex_rt - (p.rt + st.delay_value)))
        if abs(peak.apex_rt - (p.rt + st.delay_value)) > 0.02:
            continue
        st.ident_set(FID).set(Identification(
            apex_rt=peak.apex_rt, name=hits[0].name, cas=hits[0].cas, score=hits[0].quality,
            hits=[{"name": h.name, "cas": h.cas, "score": h.quality} for h in hits], source="LIB (oracle)"))


def load(samples: Path, prefixes: list[str], oracle: bool):
    from gcws.core.model import FID
    from gcws.integration.engine import integrate
    from gcws.io.run_loader import load_run
    from gcws.signal.delay import estimate_delay
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    for prefix in prefixes:
        d = next(samples.glob(prefix + "*.D"))
        run = load_run(d)
        st = ws.add_run(run, {FID: integrate(run.fid, ws.methods.get(ws.methods.default_name(FID)))},
                        delay=estimate_delay(run.fid, run.signal("TIC")))
        if oracle:
            _bind_lib_hits(ws, st, d)
    ws.recompute_quant()
    return ws


def benchmark(samples: Path, a: str, b: str, blanks: list[str], oracle: bool) -> dict:
    from gcws.features import service as SV
    from gcws.quant import duplicate_view as DV
    ws = load(samples, blanks + [a, b], oracle)
    ids = [next(s.id for s in ws.states() if s.name.startswith(p)) for p in (a, b)]
    ws.replicate_groups = [{"id": "bench", "name": f"{a}{b}", "members": ids, "policy": "all"}]
    out = {"pair": f"{ws.runs[ids[0]].name} / {ws.runs[ids[1]].name}",
           "peaks": [len(ws.result(i, "FID").peaks) for i in ids]}
    ws.quant["features"] = {"pairing": "classic"}
    rows, _ = DV.compute(ws, ids, "all")
    st = Counter(r["status"].split(",")[0].split(":")[0] for r in rows)
    limit, rl = DV.limits(ws)
    verdicts = [DV.plain_verdict(r, limit, rl) for r in rows]
    levels = Counter(v.level for v in verdicts)
    out["classic"] = {"rows": len(rows), "valid": st.get("Valid duplicate", 0),
                      "conflicts": st.get("Identification conflict", 0), "artefacts": st.get("Artefact", 0),
                      "red": levels.get("bad", 0), "amber": levels.get("warn", 0),
                      "amber_identification": sum(1 for v in verdicts if v.text.startswith("Confirmed, check"))}
    ws.quant["features"] = {"pairing": "features"}
    t0 = time.time()
    table = SV.run(ws, ids, stack=ws.project_undo)
    ws.recompute_quant()
    dt = time.time() - t0
    rows, _ = DV.compute(ws, ids, "all")
    both = [f for f in table.features if len(f.found) == 2]
    out["features"] = {
        "features": len(table.features), "paired": len(both),
        "one_sided": len(table.features) - len(both),
        "gap_fills": sum(1 for f in table.features for m in f.members if m.origin == "gapfill"),
        "not_detectable": sum(1 for f in table.features for m in f.members if m.origin == "not_detectable"),
        "spectra_compared": sum(1 for f in both if f.sim is not None),
        "mismatch": sum(1 for f in both if f.mismatch), "split": sum(1 for f in table.features if f.split),
        "cases": dict(Counter(f.identity.case for f in table.features if f.identity)),
        "lights": dict(Counter(r["light"] for r in rows)),
        "boundary_proposals": sum(1 for f in table.features for p in f.proposals if p.kind == "boundary"),
        "notes": table.notes, "seconds": round(dt, 2)}
    return out


def markdown(results: list[dict]) -> str:
    lines = ["# Double determination: classic vs features", ""]
    for r in results:
        c, f = r["classic"], r["features"]
        lines += [f"## {r['pair']}", "", f"FID peaks: {r['peaks'][0]} / {r['peaks'][1]}", "",
                  "| | classic (AutoLib) | features |", "|---|---|---|",
                  f"| pairs | {c['valid'] + c['conflicts']} | {f['paired']} |",
                  f"| identification conflicts | {c['conflicts']} | case D (different spectra): {f['mismatch']} |",
                  f"| one-sided | {c['artefacts']} (artefacts, not reported) | {f['one_sided']} "
                  f"(after {f['gap_fills']} gap fills; {f['not_detectable']} checked not detectable) |",
                  f"| spectra compared | - | {f['spectra_compared']} of {f['paired']} |",
                  f"| identity cases | - | {f['cases']} |",
                  f"| traffic light | - | {f['lights']} |",
                  f"| rows to look at | {c['red']} red (conflicts, artefacts) + {c['amber']} amber (of them "
                  f"{c['amber_identification']} 'check identification' of unknowns) | {f['lights'].get('red', 0)} "
                  f"red + {f['lights'].get('yellow', 0)} yellow (quick look) |",
                  f"| boundary proposals | - | {f['boundary_proposals']} |",
                  f"| time | - | {f['seconds']} s |", ""]
        if f["notes"]:
            lines += ["Notes: " + "; ".join(f["notes"]), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    ap.add_argument("--pairs", default="07_:11_,09_:12_")
    ap.add_argument("--blanks", default="06_,08_")
    ap.add_argument("--lib-oracle", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    os.environ.setdefault("GCWS_DATA", str(ROOT / "tests" / "_data"))
    import gcws  # noqa: F401
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    samples = Path(args.samples)
    blanks = [b for b in args.blanks.split(",") if b]
    results = [benchmark(samples, *pair.split(":"), blanks, args.lib_oracle) for pair in args.pairs.split(",")]
    text = markdown(results)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

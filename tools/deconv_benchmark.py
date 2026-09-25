r"""Deconvolution benchmark against the AMDIS result of run 07 (ELU file).

    .venv\Scripts\python tools\deconv_benchmark.py [--samples FOLDER]

Prints recall (AMDIS components found with match factor >= 800 within
+-0.015 min) and the median match factor for the vendored NIAS engine
(gc_deconv) and the GC Workspace engine (gcws.ms.deconv).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gcws  # noqa: E402,F401

DEFAULT = ROOT.parent / "NIAS Working" / "samples" / "26016605_GIOSUN1635"


def engines(run):
    import gc_deconv

    def legacy(rt):
        comps = gc_deconv.deconvolute(run.ms_source, rt, gc_deconv.DeconvParams())
        return [(c.rt, {int(m): float(v) for m, v in c.spectrum}) for c in comps]
    out = {"legacy (gc_deconv)": legacy}
    try:
        from gcws.ms import deconv as D

        def new(rt):
            res = D.deconvolute_window(run.ms, rt, D.DeconvSettings())
            return [(c.rt, {int(m): float(v) for m, v in c.spectrum}) for c in res.components]
        out["GC Workspace (gcws.ms.deconv)"] = new
    except ImportError:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=str(DEFAULT))
    ap.add_argument("--details", action="store_true")
    a = ap.parse_args(argv)
    from gcws.io.run_loader import load_run
    from gcws.ms.amdis_elu import benchmark, read_elu
    folder = Path(a.samples)
    elu = next(folder.glob("07_*.ELU"))
    run = load_run(next(folder.glob("07_*.D")))
    comps = read_elu(elu)
    print(f"{elu.name}: {len(comps)} AMDIS components, {sum(1 for c in comps if len(c.spectrum) >= 5)} with >= 5 ions")
    for name, fn in engines(run).items():
        r = benchmark(comps, fn)
        s, al = r["substantial"], r["all"]
        print(f"{name:32s} substantial: recall {100 * s['recall']:5.1f} %  median MF {s['median_mf']:5.0f}  (n={s['n']})"
              f"   all: recall {100 * al['recall']:5.1f} %  median MF {al['median_mf']:5.0f}   {r['seconds']:.1f} s")
        if a.details:
            for row in r["rows"]:
                drt = "" if row["drt"] is None else f"{row['drt']:+.4f}"
                print(f"    scan {row['scan']:5d} rt {row['rt']:.4f} model {row['model']!s:>4} ions {row['ions']:3d} "
                      f"S/N {row['sn'] or 0:5.0f}  MF {row['mf']:5.0f} {drt}")


if __name__ == "__main__":
    main()

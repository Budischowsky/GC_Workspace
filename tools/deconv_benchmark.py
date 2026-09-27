r"""Deconvolution benchmark against the AMDIS result of run 07 (ELU file).

    .venv\Scripts\python tools\deconv_benchmark.py [--samples FOLDER]

Prints recall (AMDIS components found with match factor >= 800 within
+-0.015 min) and the median match factor for the vendored NIAS engine
(gc_deconv) and the GC Workspace engine (gcws.ms.deconv).

``--split`` also reports the peak splits of the reference peaks 11.865 and
13.41 min (MS time) on FID and TIC: fit R2, basis and relative areas.
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


def split_report(run, delay: float = 0.0066) -> None:
    from gcws.integration.engine import integrate
    from gcws.integration.method import default_for, nias_fid_method
    from gcws.ms import deconv as D
    from gcws.ms.peak_split import plan_split
    print(f"Peak splits (fit to the trace; FID-MS delay {delay:.4f} min):")
    for key in ("FID", "TIC"):
        sig = run.signal(key)
        method = nias_fid_method() if key == "FID" else default_for("TIC")
        res = integrate(sig, method, t_min=5.5 if key == "FID" else None)
        shift = delay if key == "FID" else 0.0
        for target in (11.865, 13.41):
            parent = min(res.peaks, key=lambda p: abs(p.apex_rt - shift - target))
            comps = D.deconvolute_window(run.ms, parent.apex_rt - shift, D.DeconvSettings()).components
            plan = plan_split(sig, parent, key, delay, comps)
            r2 = f"R2 {plan.fit.r2:.4f}" if plan.fit is not None else "no fit"
            shares = " / ".join(f"{100 * s:.1f} %" for s in plan.shares) if plan.ok else plan.problem
            print(f"  {key} {parent.apex_rt:7.3f} min  {r2}  basis {plan.basis:3s}  "
                  f"{len(plan.checked)} of {len(plan.candidates)} components  {shares}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default=str(DEFAULT))
    ap.add_argument("--details", action="store_true")
    ap.add_argument("--split", action="store_true", help="also report the reference peak splits")
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
    if a.split:
        split_report(run)


if __name__ == "__main__":
    main()

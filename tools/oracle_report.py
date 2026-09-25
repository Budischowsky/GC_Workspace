"""Compare the integrator with ChemStation's RESULTS.CSV (test oracle only).

Usage: python tools/oracle_report.py [run folder ...]
RESULTS.CSV is never used by the application; this tool quantifies how close
our own integration comes to the ChemStation integrator on the same raw data.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gcws  # noqa: E402,F401
from gcws.integration.engine import integrate  # noqa: E402
from gcws.integration.method import default_for  # noqa: E402
from gcws.io.run_loader import load_run  # noqa: E402

SAMPLES = ROOT.parent / "NIAS Working" / "samples" / "26016605_GIOSUN1635"


@dataclass
class CSPeak:
    rt: float
    start: float
    end: float
    ty: str
    height: float
    area: float


def parse_section(path: Path, marker: str) -> list[CSPeak]:
    lines = path.read_text(encoding="latin-1").splitlines()
    out, inside = [], False
    for ln in lines:
        if ln.startswith("["):
            inside = marker in ln
            continue
        if not inside or not re.match(r"^\d+=", ln):
            continue
        parts = [p.strip().strip('"') for p in ln.split("=", 1)[1].split(",")]
        if marker.startswith("INT TIC"):
            # Peak, RT, First, Max, Last, PK TY, Height, Area
            out.append(CSPeak(float(parts[2]), float("nan"), float("nan"), parts[6],
                              float(parts[7]), float(parts[8])))
        else:
            out.append(CSPeak(float(parts[2]), float(parts[3]), float(parts[4]), parts[5],
                              float(parts[6]), float(parts[7])))
    return out


def compare(ours, theirs, t_from=5.5, tol=0.01, min_pct=0.05):
    theirs = [p for p in theirs if p.rt >= t_from]
    total = sum(p.area for p in theirs) or 1.0
    big = [p for p in theirs if 100 * p.area / total >= min_pct]
    op = [p for p in ours if p.apex_rt >= t_from]
    matched, ratios, bb_ratios = [], [], []
    used = set()
    for c in big:
        cand = [(abs(p.apex_rt - c.rt), i) for i, p in enumerate(op) if i not in used and abs(p.apex_rt - c.rt) <= tol]
        if not cand:
            continue
        _, i = min(cand)
        used.add(i)
        matched.append((c, op[i]))
        ratios.append(op[i].area / c.area)
        if c.ty.startswith("BB"):
            bb_ratios.append(op[i].area / c.area)
    ours_big = [p for p in op if p.area >= min_pct / 100 * total]
    precision = len([1 for _, p in matched if p in ours_big]) / max(1, len(ours_big))
    return {
        "cs_peaks": len(big), "our_peaks": len(ours_big), "matched": len(matched),
        "recall": len(matched) / max(1, len(big)), "precision": precision,
        "median_ratio": float(np.median(ratios)) if ratios else float("nan"),
        "bb_median_ratio": float(np.median(bb_ratios)) if bb_ratios else float("nan"),
        "within3": float(np.mean([abs(r - 1) <= 0.03 for r in ratios])) if ratios else 0.0,
        "within10": float(np.mean([abs(r - 1) <= 0.10 for r in ratios])) if ratios else 0.0,
        "total_ratio": sum(p.area for p in op) / total,
        "pairs": matched,
    }


def main(argv):
    runs = [Path(a) for a in argv] or sorted(SAMPLES.glob("*.D"))
    for d in runs:
        csv = d / "RESULTS.CSV"
        if not csv.exists():
            continue
        run = load_run(d)
        fid = parse_section(csv, "FID1A")
        res = integrate(run.fid, default_for("FID"))
        c = compare(res.peaks, fid)
        print(f"{d.name[:40]:40s} FID  CS {c['cs_peaks']:3d} ours {c['our_peaks']:3d} "
              f"recall {c['recall']:.2f} prec {c['precision']:.2f} median {c['median_ratio']:.3f} "
              f"BB {c['bb_median_ratio']:.3f} ±3% {c['within3']:.2f} ±10% {c['within10']:.2f} "
              f"total {c['total_ratio']:.3f}")
        if "-v" in sys.argv:
            for cs, p in c["pairs"]:
                print(f"   {cs.rt:7.3f} {cs.ty:5s} {cs.area:12.0f}  ours {p.apex_rt:7.3f} {p.type_code:6s} {p.area:12.0f} {p.area / cs.area:6.3f}")


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if a != "-v"])

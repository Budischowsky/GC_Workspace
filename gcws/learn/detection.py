"""Peak detection as a fitting target: the FID trace of each evaluated run is cached once, re-integrated with
candidate integration settings (no MS, no library search) and scored against the analyst's peaks at or above the
reporting limit. Concentrations of program peaks are estimated from the run's own data: program area x ISTD scale
(analyst IS areas / program areas) x the analyst's mg/kg per ChemStation area unit."""
from __future__ import annotations

import copy
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from gcws.learn.match import HumanItem, human_items, match_run
from gcws.learn.runner import ProgramResult, run_key

#: candidate integration settings; the current NIAS value first (the tie-break's preference)
DETECTION_SPACE = {
    "slope_sensitivity": [8.0, 4.0, 2.0, 16.0],
    "area_reject": [500000.0, 250000.0, 100000.0, 50000.0, 25000.0],
    "height_reject": [5000.0, 2500.0, 1000.0],
    "min_sn": [3.0, 5.0],
    "integrator_on": [6.2, 5.5, 5.0],
}


@dataclass
class DetectionRun:
    key: str
    batch: str
    run_dir: str
    items: list[HumanItem]
    mgkg_per_area: Optional[float]
    signal: str                     # cached FID trace (.npz with rt, y)


def fid_cache(run_dir: str, out_root: Path) -> Path:
    """The run's FID trace as out_root/signals/<run_key>.npz (read once from the raw data)."""
    path = Path(out_root) / "signals" / f"{run_key(run_dir, 'FID')}.npz"
    if not path.is_file():
        from gcws.core.model import FID
        from gcws.io.run_loader import load_run
        sig = load_run(Path(run_dir)).signal(FID)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez(tmp, rt=np.asarray(sig.rt, float), y=np.asarray(sig.y, float))
        tmp.replace(path)
    return path


def load_detection_runs(root: Path, out_root: Path) -> tuple[list[DetectionRun], list[str]]:
    """Every evaluated run that can be scored (a mg/kg column, ISTD areas, a readable FID trace)."""
    from gcws.learn.corpus import scan
    from gcws.learn.workbook import parse_workbook
    runs, skipped = [], []
    for entry in scan(Path(root)):
        name = f"{Path(entry.batch_dir).name} / {Path(entry.run_dir).name} ({entry.analyst})"
        ev = parse_workbook(Path(entry.workbook))
        unit = next((u for u in ev.header.conc_units if "mg/kg" in u.casefold()), None)
        if unit is None:
            skipped.append(f"{name}: no mg/kg column")
            continue
        if any(p.startswith(("cannot open", "ISTD area missing", "parse error")) for p in ev.problems):
            skipped.append(f"{name}: " + "; ".join(ev.problems))
            continue
        ratios = [r.conc[unit] / r.area for r in ev.final
                  if r.rt is not None and r.area and r.conc.get(unit) and r.row_class != "sum"]
        try:
            signal = fid_cache(entry.run_dir, out_root)
        except Exception as exc:  # noqa: BLE001 - an unreadable trace skips the run, not the fit
            skipped.append(f"{name}: FID trace not readable ({exc})")
            continue
        runs.append(DetectionRun(key=run_key(entry.run_dir, entry.analyst), batch=Path(entry.batch_dir).name,
                                 run_dir=entry.run_dir, items=human_items(ev),
                                 mgkg_per_area=statistics.median(ratios) if ratios else None, signal=str(signal)))
    return runs, skipped


def _method(base_method: dict, params: dict):
    from gcws.integration.method import IntegrationMethod
    d = copy.deepcopy(base_method["sections"]["integration"]["FID"])
    d["deconv_split"] = "off"                    # FID only, the same for current and candidate settings
    for k, v in params.items():
        if k == "integrator_on":
            for ev in d.get("timed_events", []):
                if ev.get("kind") == "INTEGRATOR_ON":
                    ev["time"] = v
        else:
            d[k] = v
    return IntegrationMethod.from_dict(d)


def integrate_peaks(run: DetectionRun, base_method: dict, params: dict) -> list[dict]:
    from gcws.core.model import FID, Signal
    from gcws.integration.engine import integrate
    data = np.load(run.signal)
    res = integrate(Signal(FID, data["rt"], data["y"]), _method(base_method, params))
    return [{"rt": p.apex_rt, "start": p.start, "end": p.end, "area": p.area} for p in res.peaks]


def detection_score(run: DetectionRun, peaks: list[dict], limit: float = 0.01) -> Optional[float]:
    """F1 of finding the analyst's peaks at or above ``limit`` mg/kg; None when it cannot be scored."""
    if run.mgkg_per_area is None:
        return None
    prog = ProgramResult(run_dir=run.run_dir, method="", state="", peaks=peaks)
    pairs = match_run(None, prog, items=run.items)
    by_human = {p.human: p for p in pairs if p.human is not None}
    istd = [(it.area, peaks[by_human[h].program]["area"]) for h, it in enumerate(run.items)
            if it.decision == "istd" and it.area and by_human[h].kind == "exact"
            and peaks[by_human[h].program]["area"]]
    if not istd:
        return None
    scale = statistics.mean(a for a, _ in istd) / statistics.mean(b for _, b in istd) * run.mgkg_per_area
    targets = [h for h, it in enumerate(run.items)
               if it.source == "final" and it.decision != "istd" and (it.conc_mgkg or 0) >= limit]
    found = sum(1 for h in targets if by_human[h].kind == "exact")
    exact = {p.program for p in pairs if p.kind == "exact"}
    detections = [k for k, pk in enumerate(peaks) if pk["area"] * scale >= limit]
    right = sum(1 for k in detections if k in exact)
    if not targets and not detections:
        return None
    precision = right / len(detections) if detections else 0.0
    recall = found / len(targets) if targets else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0

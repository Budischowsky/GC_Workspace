"""Load one Agilent ``.D`` run from raw files only (no ChemStation results)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from gcws.core.model import FID, Run, Signal
from gcws.io import folders, masshunter
from gcws.io.metadata import read_metadata
from gcws.io.ms_matrix import MSMatrix
from gcws.io.sequence import classify_role


class RunLoadError(Exception):
    pass


def load_fid(d: Path, notes: list[str]) -> Signal | None:
    import gc_ch  # vendored
    for ch in folders.fid_files(d):
        try:
            tr = gc_ch.read_ch(ch)
            return Signal(FID, tr.rt, tr.y, source=ch.name, label=tr.signal or ch.stem, y_unit="pA")
        except Exception as exc:  # noqa: BLE001 - try the next source, keep the reason
            notes.append(f"{ch.name}: {exc}")
    acq = d / "AcqData"
    for cg in sorted(acq.glob("*.cg")) if acq.is_dir() else []:
        try:
            tr = masshunter.read_cg(cg)
            return Signal(FID, tr.rt, tr.y, source="AcqData/" + cg.name, label=tr.signal, y_unit="pA")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{cg.name}: {exc}")
    return None


def load_ms(d: Path, notes: list[str]):
    import extract_ms_spectra as ex  # vendored
    if (d / "data.ms").exists():
        try:
            src = ex.DataMS(d / "data.ms")
            return src, MSMatrix.from_source(src)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"data.ms: {exc}")
    acq = d / "AcqData"
    if (acq / "MSScan.bin").exists():
        try:
            src = masshunter.MSPeakSource(acq)
            return src, MSMatrix.from_source(src)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"MSScan.bin: {exc}")
    return None, None


def load_run(path) -> Run:
    d = Path(path)
    if d.is_file() and d.suffix.lower() == ".qgd":
        from gcws.io.shimadzu import QGDSource
        from gcws.io.metadata import RunMetadata
        try:
            src = QGDSource(d)
            run = Run(path=d, meta=RunMetadata(folder=d, instrument="Shimadzu GC-MS"),
                      ms_source=src, ms=src.matrix())
            run.role = classify_role(d.stem)
            return run
        except Exception as exc:
            raise RunLoadError(f"{d.name}: {exc}") from exc
    if not folders.is_run_dir(d):
        raise RunLoadError(f"{d} is not a GC run (.D with data.ms, *.ch or AcqData, or a Shimadzu .qgd file)")
    notes: list[str] = []
    fid = load_fid(d, notes)
    ms_source, ms = load_ms(d, notes)
    if fid is None and ms is None:
        raise RunLoadError(f"{d.name}: no readable FID or MS data. " + "; ".join(notes))
    meta = read_metadata(d)
    run = Run(path=d, meta=meta, fid=fid, ms_source=ms_source, ms=ms, load_notes=notes)
    run.role = classify_role(d.name)
    if ms is not None and ms.n_scans and not np.all(np.diff(ms.rt) > 0):
        notes.append("MS scan times are not strictly ascending")
    return run

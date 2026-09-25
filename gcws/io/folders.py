"""Recognising GC run folders and analysis folders on disk."""
from __future__ import annotations

from pathlib import Path


def fid_files(d: Path) -> list[Path]:
    chs = sorted(d.glob("*.ch")) + sorted(d.glob("*.CH"))
    seen, out = set(), []
    for p in chs:
        if p.name.lower() not in seen:
            seen.add(p.name.lower())
            out.append(p)
    return out


def is_run_dir(path) -> bool:
    d = Path(path)
    if not d.is_dir() or d.suffix.lower() != ".d":
        return False
    if (d / "data.ms").exists() or fid_files(d):
        return True
    acq = d / "AcqData"
    return (acq / "MSScan.bin").exists() or any(acq.glob("*.cg"))


def list_runs(folder) -> list[Path]:
    folder = Path(folder)
    try:
        return sorted((p for p in folder.iterdir() if is_run_dir(p)), key=lambda p: p.name.casefold())
    except OSError:
        return []


def is_analysis_folder(path) -> bool:
    d = Path(path)
    if not d.is_dir() or d.suffix.lower() == ".d":
        return False
    try:
        return any(is_run_dir(p) for p in d.iterdir() if p.suffix.lower() == ".d")
    except OSError:
        return False


def sources(d) -> dict[str, str]:
    """Which raw files a run folder offers (for tooltips and properties)."""
    d = Path(d)
    out = {}
    if (d / "data.ms").exists():
        out["MS"] = "data.ms"
    elif (d / "AcqData" / "MSScan.bin").exists():
        out["MS"] = "AcqData/MSScan.bin"
    chs = fid_files(d)
    if chs:
        out["FID"] = chs[0].name
    else:
        cgs = sorted((d / "AcqData").glob("*.cg")) if (d / "AcqData").is_dir() else []
        if cgs:
            out["FID"] = "AcqData/" + cgs[0].name
    return out

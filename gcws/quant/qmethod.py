"""Named quant methods (extraction quantification): ``<data>/quant_methods/<name>.json``.

A quant method is the extraction method (``quant["method"]``: sample type and amount, extract volume,
spiked volume, Conc. 1 / Conc. 2 units, reporting limit), its internal standards (``istd_defs``, stock
concentrations in mg/mL) with their options (``istd_options``) and the detector. A run's own sample
amount (``method_samples``) and its ISTD peak bindings are never part of it.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

from gcws import paths

FORMAT = "gcws-quant-method"
VERSION = 1


def folder() -> Path:
    return paths.DATA / "quant_methods"


def _file(name: str) -> Path:
    return folder() / (re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .") + ".json")


def collect(quant: dict, name: str) -> dict:
    """The quant method of the workspace's ``quant`` under ``name``."""
    import gc_fid
    from gcws.core.audit import current_user
    from gcws.quant import extraction as EX
    q = quant or {}
    method = EX.of(q)
    method["name"] = name
    defs = gc_fid.normalise_istd_defs(q["istd_defs"]) if q.get("istd_defs") else EX.nias_standards()
    return {"format": FORMAT, "version": VERSION, "name": name,
            "created": datetime.now().isoformat(timespec="seconds"), "by": current_user(),
            "method": method, "istd_defs": defs,
            "istd_options": gc_fid.normalise_istd_options(q.get("istd_options") or {}),
            "detector": q.get("detector", "FID")}


def applied(quant: dict, data: dict) -> dict:
    """``quant`` with the method ``data`` in place (extraction mode)."""
    q = copy.deepcopy(quant or {})
    q["mode"] = "extraction"
    q["method"] = dict(copy.deepcopy(data.get("method") or {}), name=data.get("name", ""))
    for k in ("istd_defs", "istd_options", "detector"):
        if data.get(k) is not None:
            q[k] = copy.deepcopy(data[k])
    return q


def save(data: dict) -> Path:
    path = _file(data["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("method"), dict):
        raise ValueError(f"{Path(path).name} is not a GC Workspace quant method.")
    data.setdefault("name", Path(path).stem)
    return data


def names() -> list[str]:
    out = []
    for f in sorted(folder().glob("*.json")) if folder().is_dir() else []:
        try:
            out.append(read(f)["name"])
        except (OSError, ValueError):
            continue
    return sorted(out, key=str.casefold)


def load(name: str) -> dict:
    for f in folder().glob("*.json") if folder().is_dir() else []:
        try:
            data = read(f)
        except (OSError, ValueError):
            continue
        if data["name"] == name:
            return data
    raise KeyError(name)


def delete(name: str) -> bool:
    for f in folder().glob("*.json") if folder().is_dir() else []:
        try:
            if read(f)["name"] == name:
                f.unlink()
                return True
        except (OSError, ValueError):
            continue
    return False

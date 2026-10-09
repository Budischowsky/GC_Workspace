"""Project files (``.gcws``): everything needed to reproduce the workspace.

Integration results are not stored: they are recomputed from the raw data,
the method and the manual events. The stored digest shows whether the
recomputed result is the one that was saved.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import gcws
from gcws.core.audit import current_user
from gcws.core.events import ManualEvent
from gcws.core.ident import IdentificationSet
from gcws.integration.method import IntegrationMethod

FORMAT = "gcws-project"
SCHEMA = 1
SUFFIX = ".gcws"


def _rel(path: Path, base: Path) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:            # other drive
        return str(path)


def _fingerprint(path: Path) -> dict:
    out = {}
    if path.is_file() and path.suffix.lower() == ".qgd":
        s = path.stat()
        return {path.name: [s.st_size, int(s.st_mtime)]}
    for name in ("data.ms", "FID1A.ch", "AcqData/MSScan.bin", "AcqData/FID1.cg"):
        f = path / name
        if f.exists():
            s = f.stat()
            out[name] = [s.st_size, int(s.st_mtime)]
    return out


def to_dict(ws, project_path: Path) -> dict:
    base = project_path.parent
    runs = []
    for st in ws.states():
        runs.append({
            "id": st.id,
            "path_rel": _rel(st.run.path, base),
            "path_abs": str(st.run.path),
            "fingerprint": _fingerprint(st.run.path),
            "name": st.name,
            "color": st.color,
            "visible": st.visible,
            "overlay": st.overlay,
            "shade": st.shade,
            "role": st.role,
            "blanks": st.blanks,
            "blanks_istd": st.blanks_istd,
            "blanks_manual": st.blanks_manual,
            "methods": {k: m.to_dict() for k, m in st.methods.items()},
            "manual": {k: [e.to_dict() for e in v] for k, v in st.manual.items() if v},
            "identifications": {k: s.to_list() for k, s in st.idents.items() if s.items},
            "delay": None if st.delay is None else {"value": st.delay.value, "quality": st.delay.quality,
                                                    "method": st.delay.method},
            "delay_override": st.delay_override,
            "spectrum_overrides": {str(k): v for k, v in st.spectrum_overrides.items()},
            "result_digest": {k: r.digest for k, r in st.results.items()},
            "processed": st.processed,
        })
    return {
        "format": FORMAT, "schema": SCHEMA, "app_version": gcws.__version__,
        "integrator_version": gcws.INTEGRATOR_VERSION,
        "saved": datetime.now().isoformat(timespec="seconds"), "user": current_user(),
        "active": ws.active_id, "signal_key": ws.signal_key,
        "panels": {"keys": list(getattr(ws, "panel_keys", [])), "blank": list(getattr(ws, "panel_blank", [])),
                   "table": getattr(ws, "table_panel", 0)},
        "runs": runs,
        "replicate_groups": ws.replicate_groups,
        "quant": ws.quant,
        "audit": ws.audit.to_list(),
        **({"automation": ws.automation} if getattr(ws, "automation", None) else {}),
    }


def save(ws, path, data: dict | None = None) -> Path:
    """Write the project of ``ws`` (or ``data``, a :func:`to_dict` of it taken before) to ``path``."""
    path = Path(path)
    if path.suffix.lower() != SUFFIX:
        path = path.with_suffix(SUFFIX)
    data = to_dict(ws, path) if data is None else data
    tmp = path.with_suffix(SUFFIX + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    if path.exists():
        bak = path.with_suffix(SUFFIX + ".bak")
        try:
            if bak.exists():
                bak.unlink()
            path.replace(bak)
        except OSError:
            pass
    tmp.replace(path)
    return path


def read(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise ValueError(f"{Path(path).name} is not a GC Workspace project")
    if int(data.get("schema", 0)) > SCHEMA:
        raise ValueError("The project was written by a newer GC Workspace version")
    return data


def resolve_run_path(entry: dict, project_path: Path) -> Path | None:
    rel, absolute = entry.get("path_rel") or "", entry.get("path_abs") or ""
    for cand in ([project_path.parent / rel] if rel else []) + ([Path(absolute)] if absolute else []):
        try:                     # (an empty path would be the project folder or the current folder)
            if (cand.is_dir() or (cand.suffix.lower() == ".qgd" and cand.is_file())):
                return cand.resolve()
        except OSError:
            continue
    return None


def apply_run_state(st, entry: dict) -> list[str]:
    """Restore the stored state of one run onto a freshly loaded RunState."""
    from gcws.signal.delay import DelayEstimate
    notes = []
    st.run.id = entry.get("id", st.run.id)
    st.color = entry.get("color", st.color)
    st.visible = entry.get("visible", True)
    st.overlay = bool(entry.get("overlay", False))
    st.shade = int(entry.get("shade", 0) or 0)
    st.run.role = entry.get("role", st.run.role)
    st.blanks = list(entry.get("blanks", []))
    st.blanks_istd = list(entry.get("blanks_istd", []))
    st.blanks_manual = bool(entry.get("blanks_manual", False))
    if entry.get("name") and st.run.meta is not None and entry["name"] != st.run.meta.display_name:
        st.run.meta.sample_name = entry["name"]
    for kind, md in (entry.get("methods") or {}).items():
        st.methods[kind] = IntegrationMethod.from_dict(md)
    st.manual = {k: [ManualEvent.from_dict(e) for e in v] for k, v in (entry.get("manual") or {}).items()}
    st.idents = {k: IdentificationSet.from_list(v) for k, v in (entry.get("identifications") or {}).items()}
    d = entry.get("delay")
    if d:
        st.delay = DelayEstimate(d["value"], d.get("quality", 1.0), d.get("method", "saved"))
    st.delay_override = entry.get("delay_override")
    from gcws.ms.assignment import restore_overrides
    st.spectrum_overrides = restore_overrides(entry.get("spectrum_overrides") or {})
    st.saved_digests = dict(entry.get("result_digest") or {})
    st.processed = bool(entry.get("processed", True))      # older projects: always integrated
    fp_saved = entry.get("fingerprint") or {}
    fp_now = _fingerprint(st.run.path)
    changed = [k for k in fp_saved if fp_now.get(k) != fp_saved[k]]
    if changed:
        notes.append(f"{st.name}: raw data changed since the project was saved ({', '.join(changed)})")
    return notes

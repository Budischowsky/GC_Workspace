"""Processing methods: every processing setting of the workspace saved under a name.

*Method > Save current settings as Method...* collects the settings below into
``<data>/processing_methods/<name>.json``; *Method > Load Method...* applies the chosen
sections of a saved method again (one undo step for the workspace settings; the
integration methods are applied to every loaded run and become the default for runs
loaded later). Run-specific things (ISTD peak bindings, manual integration events,
blank assignments) are not part of a method.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from gcws import paths

VERSION = 1

#: section -> label shown in the dialogs (the order of application)
SECTIONS = {
    "integration": "Integration methods (FID and MS)",
    "quant": "Quantification (mode, unit, ISTD table, NIAS parameters)",
    "blank": "Blank subtraction",
    "deconv": "Deconvolution",
    "ri": "Retention index",
    "migration": "Migration conditions",
    "search": "Library search (search method, peak type)",
    "own_search": "Own library search options",
    "report": "Report options",
    "table": "Peak table (columns, value filter)",
}
QUANT_KEYS = ("mode", "unit", "istd_conc_value", "settings", "istd_defs", "istd_options", "solvent_cut", "hs",
              "ms_solvent", "rrt_reference", "detector")
#: sections that live in ``ws.quant`` under one key
QUANT_SECTIONS = {"blank": "blank_sub", "deconv": "deconv", "ri": "ri", "migration": "migration"}


def folder() -> Path:
    return paths.DATA / "processing_methods"


def _file(name: str) -> Path:
    return folder() / (re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .") + ".json")


# -- collecting -------------------------------------------------------------------------------

def collect(win, name: str, comment: str = "") -> dict:
    """The current settings of the main window ``win`` as a method."""
    from PySide6.QtCore import QSettings
    from gcws.core.audit import current_user
    from gcws.core.keys import FID, TIC
    ws = win.ws
    s = QSettings()
    sections: dict = {}
    st = ws.active
    integ = {}
    for kind in (FID, TIC):
        m = ws.method_for(st, kind) if st is not None else ws.methods.get(ws.methods.default_name(kind))
        integ[kind] = m.to_dict()
    sections["integration"] = integ
    q = ws.quant or {}
    sections["quant"] = {k: copy.deepcopy(q[k]) for k in QUANT_KEYS if k in q}
    if "hs" in sections["quant"]:
        # A method contains definitions, never another sample's amounts or peak bindings.
        sections["quant"]["hs"].pop("samples", None)
        sections["quant"]["hs"].pop("istd_bindings", None)
    for sec, key in QUANT_SECTIONS.items():
        sections[sec] = copy.deepcopy(q.get(key))
    from gcws.identify.service import search_methods
    store = search_methods()
    gc_method = st.run.meta.method if st is not None and st.run.meta else ""
    sections["search"] = {"method": store.for_gc_method(gc_method).as_dict(),
                          "target": s.value("search/target", "TIC"),
                          "transfer": s.value("search/transfer", True, type=bool)}
    from gcws.ui.dialogs.own_search import load_options
    sections["own_search"] = load_options()
    sections["report"] = {"keep_middle": s.value("report/keep_middle", False, type=bool)}
    table = getattr(win, "table", None)
    sections["table"] = {"layout": table.layout_state() if table is not None else None,
                         "value_filter": json.loads(s.value("table/value_filter", "") or "{}")}
    return {"format": "gcws-processing-method", "version": VERSION, "name": name, "comment": comment,
            "created": datetime.now().isoformat(timespec="seconds"), "by": current_user(), "sections": sections}


# -- storing ------------------------------------------------------------------------------------

def save(method: dict, path: Optional[Path] = None) -> Path:
    path = Path(path) if path else _file(method["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(method, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read(path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sections"), dict):
        raise ValueError(f"{Path(path).name} is not a GC Workspace processing method.")
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
    for f in folder().glob("*.json"):
        try:
            data = read(f)
        except (OSError, ValueError):
            continue
        if data["name"] == name:
            return data
    raise KeyError(name)


def delete(name: str) -> None:
    for f in folder().glob("*.json"):
        try:
            if read(f)["name"] == name:
                f.unlink()
        except (OSError, ValueError):
            continue


def summary(method: dict) -> str:
    """A few lines describing a method (for the load dialog)."""
    sec = method.get("sections") or {}
    lines = [f"Saved {method.get('created', '')[:16].replace('T', ' ')} by {method.get('by') or '-'}"]
    if method.get("comment"):
        lines.append(method["comment"])
    integ = sec.get("integration") or {}
    if integ:
        lines.append("Integration: " + ", ".join(f"{k} '{v.get('name', '')}'" for k, v in integ.items()))
    q = sec.get("quant") or {}
    if q:
        lines.append(f"Quantification: {q.get('mode', '-')}, unit {q.get('unit', '-')}, "
                     f"{len(q.get('istd_defs') or [])} ISTDs defined")
    srch = (sec.get("search") or {}).get("method") or {}
    if srch:
        libs = [e.get("name") for e in srch.get("libraries") or [] if e.get("enabled")]
        lines.append(f"Library search: '{srch.get('name')}', {srch.get('algorithm')}, {len(libs)} libraries, "
                     f"{(sec.get('search') or {}).get('target', 'TIC')} peaks")
    own = sec.get("own_search") or {}
    if own.get("library"):
        lines.append(f"Own library: {own['library']}")
    mig = sec.get("migration") or {}
    if mig:
        lines.append(f"Migration: {mig.get('simulant', '')}, {mig.get('temperature', '')}, {mig.get('duration', '')}")
    return "\n".join(lines)


# -- applying -----------------------------------------------------------------------------------

def apply(win, method: dict, sections=None) -> list[str]:
    """Apply ``sections`` (default: all present) of ``method``; returns the applied ones."""
    from PySide6.QtCore import QSettings
    ws = win.ws
    sec = method.get("sections") or {}
    chosen = [k for k in SECTIONS if k in sec and (sections is None or k in sections)]
    name = method.get("name", "method")
    stack = ws.undo_group.activeStack() or ws.project_undo
    stack.beginMacro(f"load method '{name}'")
    try:
        q = copy.deepcopy(ws.quant or {})
        if "quant" in chosen:
            # Missing detector/RRT fields identify an older method. Do not keep
            # the current independent cut or reference when restoring it.
            for key in ("ms_solvent", "rrt_reference"):
                if key not in (sec["quant"] or {}):
                    q.pop(key, None)
            for k in QUANT_KEYS:
                if k in (sec["quant"] or {}):
                    value = copy.deepcopy(sec["quant"][k])
                    if k == "hs":
                        value["samples"] = copy.deepcopy(q.get("hs", {}).get("samples", {}))
                        value["istd_bindings"] = copy.deepcopy(q.get("hs", {}).get("istd_bindings", {}))
                    q[k] = value
        for s_name, key in QUANT_SECTIONS.items():
            if s_name in chosen:
                if sec[s_name] is None:
                    q.pop(key, None)
                else:
                    q[key] = copy.deepcopy(sec[s_name])
        if q != (ws.quant or {}):
            ws.push_quant(f"method '{name}': settings", q, "processing method")
        if "integration" in chosen:
            _apply_integration(ws, sec["integration"] or {}, name, stack)
    finally:
        stack.endMacro()
    s = QSettings()
    if "search" in chosen:
        _apply_search(sec["search"] or {})
    if "own_search" in chosen:
        from gcws.ui.dialogs.own_search import save_options
        save_options(sec["own_search"] or {})
    if "report" in chosen:
        on = bool((sec["report"] or {}).get("keep_middle", False))
        s.setValue("report/keep_middle", on)
        act = getattr(win, "a_keep_middle", None)
        if act is not None:
            act.setChecked(on)
    if "table" in chosen and getattr(win, "table", None) is not None:
        t = sec["table"] or {}
        if t.get("layout"):
            win.table.apply_layout(t["layout"])
            win.table.save_columns()
        vf = t.get("value_filter") or {}
        if vf.get("column"):
            win.table.set_value_filter(vf["column"], vf.get("op", ">"), vf.get("a") or None, vf.get("b") or None)
        else:
            win.table.clear_value_filter()
    s.setValue("method/current", name)
    ws.log("Processing method loaded", "", name, "", ", ".join(SECTIONS[k] for k in chosen))
    return chosen


def _apply_integration(ws, integ: dict, name: str, stack) -> None:
    from gcws.integration.method import IntegrationMethod
    from gcws.ui.undo import SetMethodCommand
    for kind, d in integ.items():
        m = IntegrationMethod.from_dict(d)
        ws.methods.save(m)                             # known by name, also for later sessions
        ws.methods.set_default(kind, m.name)           # runs loaded later start with it
        ids = [st.id for st in ws.states()]
        if ids:
            stack.push(SetMethodCommand(ws, ids, kind, m, f"method '{name}': {kind} integration '{m.name}'"))


def _apply_search(d: dict) -> None:
    from PySide6.QtCore import QSettings
    import gc_search_method as SM
    from gcws.identify.service import search_methods
    if d.get("method"):
        m = SM.SearchMethod.from_dict(d["method"])
        store = search_methods()
        store.put(m)
        store.set_default(m.name)
        store.save()
    s = QSettings()
    for k in ("target", "transfer"):
        if k in d:
            s.setValue(f"search/{k}", d[k])

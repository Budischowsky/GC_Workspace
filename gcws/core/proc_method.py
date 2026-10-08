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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from gcws import paths

VERSION = 1

#: section -> label shown in the dialogs (the order of application)
SECTIONS = {
    "integration": "Integration methods (FID and MS)",
    "quant": "Quantification (mode, unit, ISTD table, NIAS parameters, quant method)",
    "blank": "Blank subtraction",
    "deconv": "Deconvolution",
    "ri": "Retention index",
    "features": "Double determination (feature alignment)",
    "migration": "Migration conditions",
    "search": "Library search (search method, peak type)",
    "own_search": "Own library search options",
    "report": "Report options",
    "table": "Peak table (columns, value filter)",
}
QUANT_KEYS = ("mode", "unit", "istd_conc_value", "settings", "istd_defs", "istd_options", "solvent_cut", "hs",
              "ms_solvent", "rrt_reference", "detector", "istd_refs", "istd_detect", "method")
#: sections that live in ``ws.quant`` under one key
QUANT_SECTIONS = {"blank": "blank_sub", "deconv": "deconv", "ri": "ri", "migration": "migration",
                  "features": "features"}


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
    # the double determination with every parameter (also those left at their default), so a
    # method gives the same pairing, gap fills and harmonisation in any installation
    from gcws.features.model import Settings as FeatureSettings
    sections["features"] = FeatureSettings.from_dict(q.get("features")).to_dict()
    from gcws.identify.service import is_fast, search_methods
    store = search_methods()
    gc_method = st.run.meta.method if st is not None and st.run.meta else ""
    search = store.for_gc_method(gc_method)
    sections["search"] = {"method": search.as_dict(),
                          "fast": is_fast(search),
                          "target": s.value("search/target", "TIC"),
                          "transfer": s.value("search/transfer", True, type=bool),
                          "mode": s.value("search/mode", "average_bg") or "average_bg",
                          "skip": s.value("search/skip", False, type=bool),
                          "rescan": s.value("search/rescan", False, type=bool),
                          "rescan_limit": int(s.value("search/rescan_limit", 80))}
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
                     f"{(sec.get('search') or {}).get('target', 'TIC')} peaks"
                     + (", Fast search" if (sec.get('search') or {}).get('fast') else ""))
    own = sec.get("own_search") or {}
    if own.get("library"):
        lines.append(f"Own library: {own['library']}")
    feats = sec.get("features") or {}
    if feats:
        lines.append(f"Double determination: {feats.get('pairing', 'features')} pairing"
                     + (", gap fill" if feats.get("gap_fill", True) else "")
                     + (", consensus names" if feats.get("consensus_search", True) else "")
                     + (", harmonised boundaries" if feats.get("harmonise", True) else ""))
    mig = sec.get("migration") or {}
    if mig:
        lines.append(f"Migration: {mig.get('simulant', '')}, {mig.get('temperature', '')}, {mig.get('duration', '')}")
    return "\n".join(lines)


# -- applying -----------------------------------------------------------------------------------

#: sections that change the workspace itself (quantification settings and integration methods)
WORKSPACE_SECTIONS = ("integration", "quant", "blank", "deconv", "ri", "migration", "features")


def chosen_sections(method: dict, sections=None) -> list[str]:
    """The sections of ``method`` to apply, in the order of application."""
    sec = method.get("sections") or {}
    return [k for k in SECTIONS if k in sec and (sections is None or k in sections)]


def plan_quant(current: dict, method: dict, chosen) -> dict:
    """``ws.quant`` after applying the ``chosen`` sections of ``method`` (pure)."""
    sec = method.get("sections") or {}
    q = copy.deepcopy(current or {})
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
    return q


def apply_to_workspace(ws, method: dict, sections=None, *, persist: bool = True, log: bool = True) -> list[str]:
    """Apply the workspace sections of ``method`` to ``ws`` as one undo step.

    ``persist`` also keeps the integration methods for later sessions (method store and the
    default for runs loaded later). Without it nothing is written outside ``ws``: runs loaded
    later into this workspace start with the method's integration (``ws.default_methods``).
    Returns the applied sections."""
    sec = method.get("sections") or {}
    chosen = [k for k in chosen_sections(method, sections) if k in WORKSPACE_SECTIONS]
    name = method.get("name", "method")
    stack = ws.undo_group.activeStack() or ws.project_undo
    stack.beginMacro(f"load method '{name}'")
    try:
        q = plan_quant(ws.quant, method, chosen)
        if q != (ws.quant or {}):
            ws.push_quant(f"method '{name}': settings", q, "processing method")
        if "integration" in chosen:
            _apply_integration(ws, sec["integration"] or {}, name, stack, persist)
    finally:
        stack.endMacro()
    if log:
        ws.log("Processing method loaded", "", name, "", ", ".join(SECTIONS[k] for k in chosen))
    return chosen


def apply(win, method: dict, sections=None) -> list[str]:
    """Apply ``sections`` (default: all present) of ``method``; returns the applied ones."""
    from PySide6.QtCore import QSettings
    ws = win.ws
    sec = method.get("sections") or {}
    chosen = chosen_sections(method, sections)
    name = method.get("name", "method")
    apply_to_workspace(ws, method, sections, persist=True, log=False)
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


@dataclass
class SearchConfig:
    """How a method searches the libraries (for unattended processing; nothing is saved)."""
    method: object                       # gc_search_method.SearchMethod
    fast: bool
    target: str = "TIC"
    transfer: bool = True
    mode: str = "average_bg"


def search_config(method: dict, gc_method: str = "") -> SearchConfig:
    """The library search of ``method``; without a search section the search method the library
    search would choose for the acquisition method ``gc_method``."""
    import gc_search_method as SM
    from gcws.identify.service import is_fast, search_methods
    d = (method.get("sections") or {}).get("search") or {}
    if d.get("method"):
        m = SM.SearchMethod.from_dict(d["method"])
    else:
        m = search_methods().for_gc_method(gc_method)
    fast = bool(d["fast"]) if "fast" in d else is_fast(m)
    return SearchConfig(m, fast, str(d.get("target") or "TIC"), bool(d.get("transfer", True)),
                        str(d.get("mode") or "average_bg"))


def _apply_integration(ws, integ: dict, name: str, stack, persist: bool = True) -> None:
    from gcws.integration.method import IntegrationMethod
    from gcws.ui.undo import SetMethodCommand
    for kind, d in integ.items():
        m = IntegrationMethod.from_dict(d)
        if persist:
            ws.methods.save(m)                         # known by name, also for later sessions
            ws.methods.set_default(kind, m.name)       # runs loaded later start with it
        else:
            ws.default_methods[kind] = m.copy()        # runs loaded later into this workspace
        ids = [st.id for st in ws.states()]
        if ids:
            stack.push(SetMethodCommand(ws, ids, kind, m, f"method '{name}': {kind} integration '{m.name}'"))


def _apply_search(d: dict) -> None:
    from PySide6.QtCore import QSettings
    import gc_search_method as SM
    from gcws.identify.service import search_methods, set_fast
    if d.get("method"):
        m = SM.SearchMethod.from_dict(d["method"])
        store = search_methods()
        store.put(m)
        store.set_default(m.name)
        store.save()
        if "fast" in d:                    # methods saved before Fast search existed leave it as it is
            set_fast(m.name, bool(d["fast"]))
    s = QSettings()
    for k in ("target", "transfer", "mode", "skip", "rescan", "rescan_limit"):
        if k in d:
            s.setValue(f"search/{k}", d[k])

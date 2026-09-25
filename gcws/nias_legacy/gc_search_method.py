"""Library search methods: saved parameter sets for the EI Atlas search.

A method holds what Agilent MassHunter calls the "Library Search Parameters"
and Shimadzu GCMSsolution the "Search Conditions": which libraries are
searched in which order with which minimum score, the match algorithm, the
spectrum range, the hit constraints and how many hits a peak keeps. The batch
identification (``gc_identify``) and the single-peak hit window
(``gc_atlas_ui``) search with the same methods.

Methods live in ``data/library_search_methods.json``; one is the default and a
GC acquisition method (``.M`` name, see ``GCWorkspace.method_key``) can name
its own. No Tk in here.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import gc_model as M

DEFAULT_NAME = "NIAS Standard"
MAX_TOP_N = 50                      # the EI Atlas engine returns at most 50 hits
ALGORITHMS = {"pbm": "PBM (Agilent ChemStation, Qual 0–99)",
              "similarity": "Similarity (NIST-Stil, Match-Faktor 0–999)"}
MODES = {"combined": "Alle gewählten Bibliotheken gemeinsam durchsuchen",
         "sequential": "Nacheinander in Reihenfolge, Stopp bei Treffer ≥ Stopp-Score"}
BACKGROUNDS = {"peak": "Untergrund des Peaks abziehen (wie angezeigt)",
               "none": "Kein Untergrundabzug (Apex-Spektrum)"}


@dataclass
class LibraryEntry:
    name: str
    enabled: bool = True
    min_score: int = 0


@dataclass
class SearchMethod:
    name: str = DEFAULT_NAME
    libraries: list[LibraryEntry] = field(default_factory=list)
    # search
    algorithm: str = "pbm"
    mode: str = "combined"
    stop_score: int = 80
    top_n: int = 5
    min_score: int = int(M.DEFAULT_QUALITY_LIMIT)      # "identified" from here on
    # spectrum
    mz_auto: bool = True
    min_mz: int = 35
    max_mz: int = 600
    threshold: float = 0.0
    background: str = "peak"
    # constraints
    mw_min: Optional[float] = None
    mw_max: Optional[float] = None
    elements_required: str = ""
    elements_allowed: str = ""
    name_include: str = ""
    name_exclude: str = ""
    require_cas: bool = False
    dedupe: bool = True
    # classification of the accepted hit
    hydrocarbons: bool = False

    # -- (de)serialisation ---------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SearchMethod":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in (data or {}).items() if k in known and k != "libraries"}
        method = cls(**values)
        method.libraries = [LibraryEntry(str(e.get("name")), bool(e.get("enabled", True)),
                                         int(e.get("min_score") or 0))
                            for e in (data or {}).get("libraries") or []
                            if isinstance(e, dict) and e.get("name")]
        return method

    def copy(self, name: Optional[str] = None) -> "SearchMethod":
        clone = copy.deepcopy(self)
        if name is not None:
            clone.name = name
        return clone

    # -- libraries -------------------------------------------------------------

    def enabled_libraries(self) -> list[str]:
        return [e.name for e in self.libraries if e.enabled]


def move_enabled(method: SearchMethod, name: str, step: int) -> bool:
    """Move library ``name`` past its next enabled neighbour (``step`` -1 or +1).

    The search order of the sequential mode counts only enabled libraries, so
    switched-off entries in between are skipped. Returns whether it moved.
    """
    libs = method.libraries
    index = next((n for n, e in enumerate(libs) if e.name == name), None)
    if index is None or step not in (-1, 1):
        return False
    target = index + step
    while 0 <= target < len(libs) and not libs[target].enabled:
        target += step
    if not 0 <= target < len(libs):
        return False
    libs.insert(target, libs.pop(index))
    return True


def library_label(name: str) -> str:
    """Display name of a library: without the prefix of EI Atlas's default folder ``Library``."""
    text = str(name or "")
    for prefix in ("Library\\", "Library/"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def available_libraries(status: dict) -> list[dict]:
    """The searchable sources of ``/api/status`` (at least one spectrum)."""
    return [s for s in status.get("libraries") or []
            if isinstance(s, dict) and s.get("name") and (s.get("count") or 0) > 0]


def reconcile(method: SearchMethod, status: dict) -> list[str]:
    """Align ``method.libraries`` with what the running EI Atlas offers.

    The method's order and settings are kept. Libraries the Atlas no longer
    has are dropped (their names are returned, for a notice); new ones are
    appended switched off -- a method never starts searching a library nobody
    chose. A method without any library entry yet (the very first one) takes
    the Atlas window's selection, else every library.
    """
    names = [s["name"] for s in available_libraries(status)]
    if not method.libraries:
        chosen = [n for n in status.get("library_selection") or [] if n in names] or names
        method.libraries = [LibraryEntry(n, n in chosen) for n in names]
        return []
    present = set(names)
    missing = [e.name for e in method.libraries if e.name not in present]
    method.libraries = [e for e in method.libraries if e.name in present]
    known = {e.name for e in method.libraries}
    method.libraries += [LibraryEntry(n, False) for n in names if n not in known]
    return missing


ELEMENT = re.compile(r"[A-Z][a-z]?")


def split_elements(text: str) -> list[str]:
    return [s for s in re.split(r"[\s,;]+", text or "") if s]


def split_words(text: str) -> list[str]:
    return [s.strip() for s in (text or "").split(";") if s.strip()]


def problems(method: SearchMethod) -> list[str]:
    """German reasons why ``method`` cannot be searched; empty when it can."""
    out = []
    if not method.enabled_libraries():
        out.append("Keine Bibliothek ausgewählt.")
    if not 1 <= method.top_n <= MAX_TOP_N:
        out.append(f"Treffer pro Peak: 1–{MAX_TOP_N}.")
    if not 0 <= method.min_score <= 99 or not 0 <= method.stop_score <= 99:
        out.append("Scores liegen zwischen 0 und 99.")
    if any(not 0 <= e.min_score <= 99 for e in method.libraries):
        out.append("Min-Score je Bibliothek: 0–99.")
    if not method.mz_auto and not 1 <= method.min_mz < method.max_mz <= 10000:
        out.append("m/z-Bereich: 1 ≤ von < bis ≤ 10000.")
    if not 0 <= method.threshold <= 20:
        out.append("Mindestintensität: 0–20 %.")
    if method.mw_min is not None and method.mw_max is not None and method.mw_min > method.mw_max:
        out.append("MW von ist größer als MW bis.")
    for label, text in (("Pflicht-Elemente", method.elements_required),
                        ("Erlaubte Elemente", method.elements_allowed)):
        bad = [s for s in split_elements(text) if not ELEMENT.fullmatch(s)]
        if bad:
            out.append(f"{label}: ungültige Elementsymbole {', '.join(bad)}.")
    allowed = set(split_elements(method.elements_allowed))
    if allowed and not set(split_elements(method.elements_required)) <= allowed:
        out.append("Ein Pflicht-Element fehlt in den erlaubten Elementen.")
    if method.algorithm not in ALGORITHMS or method.mode not in MODES \
            or method.background not in BACKGROUNDS:
        out.append("Unbekannte Sucheinstellung.")
    return out


def mz_range(method: SearchMethod, acquired: tuple[int, int]) -> tuple[int, int]:
    """The search range: the batch's acquisition range, or the method's fixed one."""
    return acquired if method.mz_auto else (int(method.min_mz), int(method.max_mz))


def to_api_settings(method: SearchMethod, rng: tuple[int, int], *, lite: bool = True) -> dict:
    """``settings`` of ``POST /api/analyze`` for ``method``."""
    libraries = method.enabled_libraries()
    settings: dict[str, Any] = {
        "libraries": libraries, "min_mz": int(rng[0]), "max_mz": int(rng[1]),
        "threshold": float(method.threshold), "algorithm": method.algorithm,
        "mode": method.mode, "stop_score": int(method.stop_score),
        "library_min_scores": {e.name: int(e.min_score) for e in method.libraries
                               if e.enabled and e.min_score > 0},
        "max_hits": max(1, min(int(method.top_n), MAX_TOP_N)),
        "dedupe": bool(method.dedupe), "lite": lite,
    }
    for key in ("mw_min", "mw_max"):
        if getattr(method, key) is not None:
            settings[key] = float(getattr(method, key))
    if split_elements(method.elements_required):
        settings["elements_required"] = split_elements(method.elements_required)
    if split_elements(method.elements_allowed):
        settings["elements_allowed"] = split_elements(method.elements_allowed)
    if split_words(method.name_include):
        settings["name_include"] = split_words(method.name_include)
    if split_words(method.name_exclude):
        settings["name_exclude"] = split_words(method.name_exclude)
    if method.require_cas:
        settings["require_cas"] = True
    return settings


def summary(method: SearchMethod) -> str:
    """One line for titles and status bars."""
    libs = method.enabled_libraries()
    text = (f"{method.name}: {'PBM' if method.algorithm == 'pbm' else 'Similarity'}, "
            f"{len(libs)} Bibliothek(en)"
            + (", nacheinander" if method.mode == "sequential" else ""))
    constraints = [c for c, on in (("MW", method.mw_min is not None or method.mw_max is not None),
                                   ("Elemente", bool(method.elements_required or method.elements_allowed)),
                                   ("Name", bool(method.name_include or method.name_exclude)),
                                   ("CAS", method.require_cas),
                                   ("KW", method.hydrocarbons)) if on]
    return text + (f", Erweitert: {'/'.join(constraints)}" if constraints else "")


# -- persistence --------------------------------------------------------------

def store_path() -> Path:
    from nias_paths import DATA
    return DATA / "library_search_methods.json"


def legacy_path() -> Path:
    from nias_paths import DATA
    return DATA / "atlas_identify.json"


class MethodStore:
    """All saved methods, the default, per-GC-method defaults and run options."""

    def __init__(self, path: Optional[Path] = None, legacy: Optional[Path] = None):
        self.path = Path(path) if path else store_path()
        self.legacy = Path(legacy) if legacy else (legacy_path() if path is None else None)
        self.methods: dict[str, SearchMethod] = {}
        self.default = DEFAULT_NAME
        self.by_gc_method: dict[str, str] = {}
        self.run: dict[str, Any] = {"all_samples": True, "review": True,
                                    "rescan_below": False, "rescan_limit": 80}
        self.load()

    def load(self) -> None:
        data = self._read(self.path)
        if data is None:
            data = self._migrate()
        for item in data.get("methods") or []:
            if isinstance(item, dict) and item.get("name"):
                method = SearchMethod.from_dict(item)
                self.methods[method.name] = method
        if not self.methods:
            self.methods[DEFAULT_NAME] = SearchMethod()
        self.default = data.get("default") if data.get("default") in self.methods \
            else next(iter(self.methods))
        self.by_gc_method = {str(k): str(v) for k, v in (data.get("by_gc_method") or {}).items()
                             if v in self.methods}
        run = data.get("run")
        if isinstance(run, dict):
            self.run.update({k: bool(v) for k, v in run.items()
                             if k in ("all_samples", "review", "rescan_below")})
            try:
                self.run["rescan_limit"] = max(0, min(99, int(run["rescan_limit"])))
            except (KeyError, TypeError, ValueError):
                pass

    @staticmethod
    def _read(path: Optional[Path]) -> Optional[dict]:
        if path is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _migrate(self) -> dict:
        """The settings of the first identification dialog become "NIAS Standard"."""
        old = self._read(self.legacy) or {}
        method = SearchMethod()
        method.libraries = [LibraryEntry(str(n)) for n in old.get("libraries") or []
                            if isinstance(n, str)]
        for key in ("top_n", "min_score"):
            try:
                if old.get(key) is not None:
                    setattr(method, key, int(old[key]))
            except (TypeError, ValueError):
                pass
        run = {k: bool(old[k]) for k in ("all_samples", "review") if k in old}
        return {"methods": [method.as_dict()], "default": method.name, "run": run}

    def save(self) -> bool:
        data = {"default": self.default, "by_gc_method": self.by_gc_method, "run": self.run,
                "methods": [m.as_dict() for m in self.methods.values()]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError:
            return False

    # -- queries and edits -------------------------------------------------------

    def names(self) -> list[str]:
        return list(self.methods)

    def for_gc_method(self, gc_method: Optional[str]) -> SearchMethod:
        """A copy of the method to start with for ``gc_method``."""
        name = self.by_gc_method.get(gc_method or "") or self.default
        return self.methods.get(name, next(iter(self.methods.values()))).copy()

    def get(self, name: str) -> SearchMethod:
        return self.methods[name].copy()

    def put(self, method: SearchMethod) -> None:
        self.methods[method.name] = method.copy()

    def delete(self, name: str) -> None:
        if name not in self.methods or len(self.methods) == 1:
            raise ValueError("Die letzte Methode kann nicht gelöscht werden.")
        del self.methods[name]
        if self.default == name:
            self.default = next(iter(self.methods))
        self.by_gc_method = {k: v for k, v in self.by_gc_method.items() if v != name}

    def set_default(self, name: str, gc_method: Optional[str] = None) -> None:
        if name not in self.methods:
            raise KeyError(name)
        if gc_method:
            self.by_gc_method[gc_method] = name
        else:
            self.default = name

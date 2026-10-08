"""The analyst's libraries: which files and folders on this PC are searched.

Kept in ``DATA/libraries.json``. A library is one of

* ``agilent``  - an Agilent / ChemStation ``.L`` folder (HEADER.IND, FULL.D),
* ``nist``     - a NIST MS Search folder (mainlib, replib or a user library: nist.db,
                 nist.dbr or USER.DBU),
* ``shimadzu`` - a Wiley / Shimadzu ``.lib`` file,
* ``msp``      - an ``.msp`` text library.

``discover`` finds every library below a folder, so pointing at a whole library
folder (or a SpectrAtlas ``Library`` folder) adds all of them at once.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from gcws import paths

KINDS = {"agilent": "Agilent / ChemStation (.L)", "nist": "NIST MS Search", "shimadzu": "Wiley / Shimadzu (.lib)",
         "msp": "MSP text library"}
NIST_RECORD_FILES = ("nist.db", "nist.dbr", "user.dbu")
#: vendor programs unpacked beside libraries carry demo spectra: never searched
SKIP_FOLDERS = {"software", "__pycache__", ".cache", ".git"}


@dataclass
class LibrarySpec:
    name: str
    kind: str
    path: str
    enabled: bool = True

    @property
    def file(self) -> Path:
        return Path(self.path)

    def exists(self) -> bool:
        return self.file.exists()


def store_path() -> Path:
    return paths.DATA / "libraries.json"


def cache_root() -> Path:
    """Where the numeric search indexes are kept (built once per library, then reused)."""
    return paths.DATA / "libcache"


def load() -> list[LibrarySpec]:
    data = None
    for attempt in range(50):
        try:
            data = json.loads(store_path().read_text(encoding="utf-8"))
            break
        except PermissionError:                   # being replaced by ``save`` just now (Windows)
            time.sleep(0.01)
        except (OSError, ValueError):
            return []
    if not isinstance(data, dict):
        return []
    out = []
    for d in data.get("libraries") or []:
        if isinstance(d, dict) and d.get("path") and d.get("kind") in KINDS:
            out.append(LibrarySpec(str(d.get("name") or Path(d["path"]).name), d["kind"], str(d["path"]),
                                   bool(d.get("enabled", True))))
    return out


def save(libs: list[LibrarySpec]) -> None:
    """Written whole (temporary file, then replaced): another process (the automation watcher's
    jobs) reading the list at that moment sees the old or the new list, never an empty one."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps({"libraries": [asdict(x) for x in libs]}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                   # a reader has the file open just now (Windows)
            if attempt == 49:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.02)


def kind_of(path: Path) -> str | None:
    """The kind of library at ``path`` (a folder or a file), or None."""
    p = Path(path)
    if p.is_file():
        suffix = p.suffix.lower()
        if suffix == ".msp":
            return "msp"
        if suffix == ".lib":
            return "shimadzu"
        if p.name.lower() in NIST_RECORD_FILES:
            return "nist"
        if p.name.lower() == "header.ind":
            return "agilent"
        return None
    if not p.is_dir():
        return None
    try:
        names = {c.name.lower() for c in p.iterdir() if c.is_file()}
    except OSError:
        return None
    if "header.ind" in names:
        return "agilent"
    if names & set(NIST_RECORD_FILES):
        return "nist"
    return None


def _location(path: Path, kind: str) -> Path:
    """Agilent and NIST libraries are folders: a picked member file stands for its folder."""
    p = Path(path)
    return p.parent if p.is_file() and kind in ("agilent", "nist") else p


def discover(path, limit: int = 500) -> list[LibrarySpec]:
    """Every library at or below ``path`` (a library folder, a library file or a parent folder)."""
    root = Path(path)
    kind = kind_of(root)
    if kind is not None:
        loc = _location(root, kind)
        return [LibrarySpec(_default_name(loc, kind), kind, str(loc))]
    found: list[LibrarySpec] = []
    if not root.is_dir():
        return found
    stack = [root]
    while stack and len(found) < limit:
        folder = stack.pop()
        try:
            children = sorted(folder.iterdir())
        except OSError:
            continue
        k = kind_of(folder)
        if k is not None and folder != root:
            found.append(LibrarySpec(_default_name(folder, k), k, str(folder)))
            continue                              # a library folder holds no further libraries
        for c in children:
            if c.is_dir():
                if c.name.lower() not in SKIP_FOLDERS:
                    stack.append(c)
            elif c.suffix.lower() in (".msp", ".lib"):
                found.append(LibrarySpec(_default_name(c, kind_of(c)), kind_of(c), str(c)))
    return sorted(found, key=lambda x: x.name.lower())


def _default_name(path: Path, kind: str) -> str:
    return Path(path).stem if kind in ("msp", "shimadzu") else Path(path).name


def unique_name(name: str, taken) -> str:
    taken = set(taken)
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


def add(libs: list[LibrarySpec], new: list[LibrarySpec]) -> list[LibrarySpec]:
    """``libs`` plus ``new`` (a path already listed is not added twice; names stay unique)."""
    out = list(libs)
    known = {str(Path(x.path).resolve()).lower() for x in out}
    for spec in new:
        key = str(Path(spec.path).resolve()).lower()
        if key in known:
            continue
        known.add(key)
        out.append(LibrarySpec(unique_name(spec.name, [x.name for x in out]), spec.kind, spec.path, spec.enabled))
    return out


def atlas_libraries() -> list[LibrarySpec]:
    """The libraries a SpectrAtlas installation searches (one-off import; SpectrAtlas is not needed later).

    Named as SpectrAtlas names them (``Library\\NIST17.L``), so existing search methods keep
    their library choices."""
    try:
        import gc_atlas
        root = gc_atlas.atlas_root()
    except Exception:
        root = None
    if root is None:
        return []
    root = Path(root)
    out = []
    lib = root / "Library"
    for spec in discover(lib) if lib.is_dir() else []:
        try:
            spec.name = str(Path(spec.path).relative_to(root))
        except ValueError:
            pass
        out.append(spec)
    for msp in sorted((root / "libraries").rglob("*.msp")) if (root / "libraries").is_dir() else []:
        out.append(LibrarySpec(str(msp.relative_to(root)), "msp", str(msp)))
    try:
        prefs = json.loads((root / ".cache" / "preferences.json").read_text(encoding="utf-8"))
        for folder in prefs.get("library_dirs") or []:
            out += discover(folder)
    except (OSError, ValueError):
        pass
    return out

"""Folders of the application and access to the vendored NIAS modules."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "gcws"
RESOURCES = ROOT / "resources"
LEGACY = PACKAGE / "nias_legacy"
DATA = Path(os.environ["GCWS_DATA"]) if os.environ.get("GCWS_DATA") else ROOT / "data"


def bootstrap_legacy() -> None:
    """Put the vendored NIAS modules on ``sys.path`` under their flat names."""
    legacy = str(LEGACY)
    if legacy not in sys.path:
        sys.path.insert(0, legacy)
    os.environ.setdefault("GCWS_DATA", str(DATA))


#: NIAS installation next to this one; its search methods and settings are
#: taken over once so the standalone starts with the familiar configuration.
NIAS_WORKING = ROOT.parent / "NIAS Working"


def initialize() -> None:
    for folder in (DATA, DATA / "methods", DATA / "layouts", DATA / "logs",
                   DATA / "reports", DATA / "projects"):
        folder.mkdir(parents=True, exist_ok=True)
    import_nias_settings()


def import_nias_settings() -> list[str]:
    """Copy NIAS search methods and settings once (never overwrites)."""
    import json
    src = NIAS_WORKING / "data"
    done = []
    if not src.is_dir():
        return done
    # Search methods and libraries are GC Workspace's own (Identify > Libraries...); they are
    # no longer copied from NIAS / SpectrAtlas.
    target = DATA / "settings.json"
    if (src / "settings.json").exists() and not target.exists():
        try:
            data = json.loads((src / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        # the standalone keeps its own unknown register by default; the NIAS
        # register folder is remembered so Preferences can offer to share it
        value = data.get("unknown_register_dir")
        if value:
            nias_dir = Path(value) if Path(value).is_absolute() else (NIAS_WORKING / value).resolve()
            data["nias_unknown_register_dir"] = str(nias_dir)
        data["unknown_register_dir"] = str(DATA)
        data.pop("previous_unknown_register_dir", None)
        data["standard_cas_path"] = "CASINFO.xlsx"
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        done.append("settings.json")
    return done


def methods_dir() -> Path:
    return DATA / "methods"


def logs_dir() -> Path:
    return DATA / "logs"


def settings_ini() -> Path:
    return DATA / "gcws.ini"

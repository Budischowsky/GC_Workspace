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


def initialize() -> None:
    for folder in (DATA, DATA / "methods", DATA / "layouts", DATA / "logs",
                   DATA / "reports", DATA / "projects"):
        folder.mkdir(parents=True, exist_ok=True)


def methods_dir() -> Path:
    return DATA / "methods"


def logs_dir() -> Path:
    return DATA / "logs"


def settings_ini() -> Path:
    return DATA / "gcws.ini"

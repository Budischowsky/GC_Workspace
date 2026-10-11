"""Settings the domain reads: ``settings.json`` (Edit > Preferences...) and the own-library search
options (``QSettings("ownsearch/...")``, Identify > Own library search options...).

The dialogs that edit them live in ``gcws.ui``; reports, quantification and processing methods read
them from here without importing the UI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from gcws import paths


def settings_file() -> Path:
    return paths.DATA / "settings.json"


def load() -> dict:
    try:
        return json.loads(settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    settings_file().parent.mkdir(parents=True, exist_ok=True)
    settings_file().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def cas_path() -> Optional[Path]:
    """CASINFO.xlsx of the preferences, resolved like the NIAS main script does it: an absolute path
    as it is, a relative one in the resources, the program folder or the data folder; None if missing."""
    try:
        raw = Path(load().get("standard_cas_path", "CASINFO.xlsx"))
    except TypeError:
        raw = Path("CASINFO.xlsx")
    if raw.is_absolute():
        return raw if raw.exists() else None
    return next((b / raw for b in (paths.RESOURCES, paths.ROOT, paths.DATA) if (b / raw).exists()), None)


OWN_SEARCH_DEFAULTS = {"library": "", "algorithm": "similarity", "min_score": 0, "top_n": 10, "mz_auto": True,
                       "min_mz": 35, "max_mz": 600, "threshold": 0.0, "dedupe": True}


def load_own_search() -> dict:
    from PySide6.QtCore import QSettings
    s = QSettings()
    out = {}
    for k, v in OWN_SEARCH_DEFAULTS.items():
        raw = s.value(f"ownsearch/{k}", v)
        out[k] = (raw in (True, "true", "1", 1)) if isinstance(v, bool) else type(v)(raw)
    return out


def save_own_search(opts: dict) -> None:
    from PySide6.QtCore import QSettings
    s = QSettings()
    for k in OWN_SEARCH_DEFAULTS:
        if k in opts:
            s.setValue(f"ownsearch/{k}", opts[k])

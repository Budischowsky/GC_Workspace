"""Where the automation keeps its files (all under ``<data>/automation``).

The GUI, the watcher and the job processes share these files; JSON is written atomically
(temporary file + rename), so a reader never sees half a file.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from gcws import paths


def root() -> Path:
    return paths.DATA / "automation"


def workflows_dir() -> Path:
    return root() / "workflows"


def jobs_dir() -> Path:
    return root() / "jobs"


def journal_path() -> Path:
    return root() / "journal.sqlite"


def default_rules_path() -> Path:
    return root() / "default_rules.json"


def reject_reasons_path() -> Path:
    return root() / "reject_reasons.json"


def atomic_write_json(path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_json(path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def safe_name(text: str, limit: int = 80) -> str:
    """``text`` usable as a file name."""
    import re
    out = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(text)).strip(" .")
    return (out or "unnamed")[:limit]


def is_inside(path, folder) -> bool:
    """True when ``path`` is ``folder`` or lies below it (case-insensitive on Windows)."""
    try:
        p = os.path.normcase(os.path.abspath(str(path)))
        f = os.path.normcase(os.path.abspath(str(folder)))
    except (TypeError, ValueError):
        return False
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)

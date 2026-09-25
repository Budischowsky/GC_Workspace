"""The only door into the vendored ``NIAS Reporting v27.py`` (headless)."""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_MAIN = None


def main_script():
    """The NIAS main script as a module (loaded once, ~1 s)."""
    global _MAIN
    with _LOCK:
        if _MAIN is None:
            import gc_export
            _MAIN = gc_export.load_main_script()
        return _MAIN


def preload_async() -> None:
    threading.Thread(target=main_script, daemon=True).start()

"""The native EI Atlas window (research tabs), driven from Qt.

Protocol as ``gc_atlas.AtlasSession``: one private ``desktop_host.py
--research --tabs`` process; one JSON line per tab request on stdin; events
(``saved``, ``register``, ``error``, ``closed``) as JSON lines on stdout.
``--integration-dir`` points at the vendored modules so the Atlas imports the
same ``gc_atlas``/``gc_atlas_store`` (register database) as this app.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal as QtSignal

from gcws import paths


class AtlasBridge(QObject):
    saved = QtSignal(dict)
    registerRequested = QtSignal(object)
    error = QtSignal(str)

    _instance = None

    @classmethod
    def instance(cls) -> "AtlasBridge":
        if cls._instance is None:
            cls._instance = AtlasBridge()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.outgoing: "queue.Queue" = queue.Queue()
        self.events: "queue.Queue" = queue.Queue()
        self.process = None
        self.started = False
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._poll)

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def open_research(self, snapshot: dict, context: dict, db_path: Path, parent_hwnd: int = 0) -> None:
        import gc_atlas_store as store
        store.clean_spectrum(snapshot.get("spectrum") or [])
        payload = dict(snapshot=deepcopy(snapshot), context=deepcopy(context), db_path=str(Path(db_path).resolve()),
                       entry_id=None, spectrum_id=None, initial={}, parent_hwnd=int(parent_hwnd),
                       tab_id=uuid.uuid4().hex)
        json.dumps(payload, allow_nan=False)
        if self.started and not self._alive() and self.process is not None:
            self.started = False
            self.outgoing = queue.Queue()
        self.outgoing.put(payload)
        if not self.started:
            self.started = True
            threading.Thread(target=self._launch, daemon=True).start()
        self.timer.start()

    def _launch(self):
        import gc_atlas
        try:
            root = gc_atlas.atlas_root()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            candidates = [root / ".nias-venv/Scripts/python.exe", root / ".venv/Scripts/python.exe",
                          Path(sys.executable)]
            python = next((p for p in candidates if p.is_file() and subprocess.run(
                [str(p), "-c", "import webview, clr, numpy"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags, timeout=30).returncode == 0), None)
            if python is None:
                raise RuntimeError("The native EI Atlas environment is missing (UnknownEvaluation "
                                   "requirements-desktop.txt: pywebview, pythonnet).")
            with (root / "desktop-window.log").open("ab") as log:
                self.process = subprocess.Popen(
                    [str(python), str(root / "desktop_host.py"), "--research", "--tabs",
                     "--integration-dir", str(paths.LEGACY)], cwd=str(root),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                    creationflags=flags)
                out = self.outgoing

                def writer():
                    try:
                        while True:
                            payload = out.get()
                            if payload is None:
                                return
                            self.process.stdin.write(json.dumps(payload, ensure_ascii=True, allow_nan=False) + "\n")
                            self.process.stdin.flush()
                    except (OSError, ValueError) as exc:
                        self.events.put({"event": "error", "value": str(exc)})
                threading.Thread(target=writer, daemon=True).start()
                for line in self.process.stdout:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and "event" in ev:
                        self.events.put(ev)
                if self.process.wait():
                    raise RuntimeError(f"EI Atlas window ended with an error. Details: {root / 'desktop-window.log'}")
        except Exception as exc:  # noqa: BLE001 - reported on the GUI thread
            self.events.put({"event": "error", "value": str(exc)})
        finally:
            self.outgoing.put(None)
            self.started = False

    def _poll(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            kind = ev.get("event")
            if kind == "saved":
                self.saved.emit(dict(ev.get("value") or {}))
            elif kind == "register":
                self.registerRequested.emit(ev.get("value"))
            elif kind == "error":
                self.error.emit(str(ev.get("value")))
        if not self.started and self.events.empty():
            self.timer.stop()

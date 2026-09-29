"""The GUI's handle on the watcher process: is it running, start it, pause / resume / scan / quit."""
from __future__ import annotations

import json
import time
from typing import Optional

from gcws import paths
from gcws.automation import journal as J

#: the watcher writes a heartbeat every 10 s; older than this it counts as not responding
STALE_S = 45


class WatcherControl:
    def __init__(self, name: Optional[str] = None):
        from gcws.automation.watcher import server_name
        self.name = name or server_name()

    def send(self, cmd: str, timeout: int = 1500) -> Optional[dict]:
        """Send a command; the watcher's answer, or None when no watcher listens."""
        from PySide6.QtNetwork import QLocalSocket
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(timeout):
            return None
        try:
            sock.write((json.dumps({"cmd": cmd}) + "\n").encode("utf-8"))
            sock.flush()
            end = time.monotonic() + timeout / 1000
            buf = b""
            while b"\n" not in buf and time.monotonic() < end:
                if sock.waitForReadyRead(100):
                    buf += bytes(sock.readAll())
            if b"\n" not in buf:
                return None
            return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except ValueError:
            return None
        finally:
            sock.disconnectFromServer()

    def status(self, journal: Optional[J.Journal] = None) -> dict:
        """``{"state": running | paused | processing | stopped | not responding, "heartbeat": age s, ...}``"""
        reply = self.send("status", 700)
        row = {}
        try:
            row = (journal or J.Journal()).watcher_status()
        except Exception:  # noqa: BLE001
            pass
        age = time.time() - float(row.get("heartbeat") or 0) if row else None
        if reply is not None:
            return dict(reply, heartbeat=age, current=reply.get("current") or row.get("current_job", ""))
        if row and row.get("state") not in ("stopped", None) and age is not None and age < STALE_S:
            return {"state": "not responding", "heartbeat": age, "current": row.get("current_job", "")}
        return {"state": "stopped", "heartbeat": age, "current": ""}

    def start(self, paused: bool = False) -> bool:
        """Start the watcher as its own process (it keeps running after GC Workspace is closed)."""
        from PySide6.QtCore import QProcess
        from gcws.automation.watcher import python_exe
        args = ["-m", "gcws", "--watch"] + (["--paused"] if paused else [])
        ok = QProcess.startDetached(python_exe(), args, str(paths.ROOT))
        return bool(ok[0] if isinstance(ok, tuple) else ok)

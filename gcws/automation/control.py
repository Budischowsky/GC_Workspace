"""The GUI's handle on the watcher process: is it running, start it, pause / resume / scan / quit.

Whether a watcher runs is known without asking it: it holds a lock while its process lives
(``store.take_lock``) and writes a heartbeat into the journal. Polling the state therefore never
waits for a busy watcher (that froze GC Workspace), and nothing starts a second watcher beside one
that is busy or hangs.
"""
from __future__ import annotations

import json
import os
import socket
import time
from typing import Optional

from gcws import paths
from gcws.automation import journal as J
from gcws.automation import store

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

    @staticmethod
    def _row(journal: Optional[J.Journal]) -> dict:
        try:
            return (journal or J.Journal()).watcher_status()
        except Exception:  # noqa: BLE001 - the journal may be busy for a moment
            return {}

    def running(self, journal: Optional[J.Journal] = None, row: Optional[dict] = None) -> bool:
        """True while a watcher process of this data folder lives, answering or not."""
        if store.lock_held(self.name):
            return True
        # a watcher of a version without the lock: its heartbeat names its process
        row = self._row(journal) if row is None else row
        return bool(row) and row.get("state") not in ("stopped", None) and \
            row.get("host") in (None, "", socket.gethostname()) and store.alive(row.get("pid"), row.get("started"))

    def status(self, journal: Optional[J.Journal] = None) -> dict:
        """``{"state": starting | running | paused | processing | stopped | not responding, "heartbeat":
        age s, "current": job id, "copying": text, "pid": process}`` - from the lock and the heartbeat,
        without waiting for the watcher."""
        row = self._row(journal)
        age = time.time() - float(row.get("heartbeat") or 0) if row else None
        if not self.running(journal, row):
            return {"state": "stopped", "heartbeat": age, "current": ""}
        state = row.get("state")
        if state in (None, "stopped"):
            state = "starting"                         # the lock is taken, the first heartbeat not yet written
        elif age is None or age > STALE_S:
            state = "not responding"
        return {"state": state, "heartbeat": age, "current": row.get("current_job") or "",
                "copying": row.get("message") or "", "pid": row.get("pid")}

    def start(self, paused: bool = False) -> bool:
        """Start the watcher as its own process (it keeps running after GC Workspace is closed). Nothing
        is started while one runs (also when it is busy or hangs: :meth:`restart` replaces that one)."""
        if self.running():
            return True
        from PySide6.QtCore import QProcess
        from gcws.automation.watcher import python_exe
        args = ["-m", "gcws", "--watch"] + (["--paused"] if paused else [])
        ok = QProcess.startDetached(python_exe(), args, str(paths.ROOT))
        return bool(ok[0] if isinstance(ok, tuple) else ok)

    def restart(self, journal: Optional[J.Journal] = None, wait_s: float = 5.0) -> bool:
        """A watcher that does not answer is asked to quit, ended when it does not, and started again."""
        self.send("quit", 1500)
        if not self._wait_gone(journal, wait_s):
            pid = self._row(journal).get("pid")
            if store.alive(pid) and int(pid) != os.getpid():
                try:
                    import signal
                    os.kill(int(pid), signal.SIGTERM)  # Windows: TerminateProcess
                except (OSError, ValueError):
                    pass
            if not self._wait_gone(journal, wait_s):
                return False
        return self.start()

    def _wait_gone(self, journal, wait_s: float) -> bool:
        end = time.monotonic() + wait_s
        while self.running(journal):
            if time.monotonic() > end:
                return False
            time.sleep(0.1)
        return True

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


def listing_path(workflow_id: str) -> Path:
    """What the watcher saw in a workflow's watched folder at its last look (the Folders tab reads it,
    so that GC Workspace never has to read a slow network drive itself)."""
    return root() / "listing" / f"{safe_name(workflow_id)}.json"


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


def is_network(path) -> bool:
    """True when ``path`` lies on a network share (UNC path or a mapped network drive)."""
    p = str(path)
    if p.startswith("\\\\"):
        return True
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(p))[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4          # DRIVE_REMOTE
    except Exception:  # noqa: BLE001
        return False


def alive(pid, started: float | None = None) -> bool:
    """True while the process ``pid`` runs. ``started``: when it took a job; a process created after
    that only got the number of one that ended (Windows reuses process numbers)."""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)                          # POSIX: signal 0 only checks
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5             # access denied: it exists (another user's)
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:     # STILL_ACTIVE
            return False
        if started:
            times = [wintypes.FILETIME() for _ in range(4)]
            if k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 1e7 - 11644473600.0
                if created > float(started) + 2.0:
                    return False
        return True
    finally:
        k32.CloseHandle(handle)


_LOCKS: dict = {}                                   # name -> handle / open file, held until the process ends


def take_lock(name: str) -> bool:
    """Take the lock ``name`` for this process; False when another process holds it. Windows releases
    it when the process ends, also after a crash (a named mutex), so a stale lock never keeps a new
    process out."""
    if name in _LOCKS:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.CreateMutexW(None, False, "Local\\" + name)
        if not handle:
            return False
        if ctypes.get_last_error() == 183:              # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return False
        _LOCKS[name] = handle
        return True
    import fcntl
    root().mkdir(parents=True, exist_ok=True)
    f = open(root() / f"{name}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _LOCKS[name] = f
    return True


def release_lock(name: str) -> None:
    lock = _LOCKS.pop(name, None)
    if lock is None:
        return
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle(lock)
    else:
        lock.close()


_JOB = None                                         # Windows job object: its processes end with this one


def end_with_this_process(pid) -> bool:
    """The process ``pid`` (a job or copy process this one started) ends when this process ends, also
    when it crashes or is ended from outside (Restart). Windows lets a child process run on otherwise:
    the next watcher then processed its job a second time beside it, in the same folder. A venv's
    launcher takes the interpreter it runs with it. False where this is not possible."""
    global _JOB
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    if _JOB is None:
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # extended limits
            k32.CloseHandle(job)
            return False
        _JOB = job                                              # closed by Windows when this process ends
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    handle = k32.OpenProcess(0x0101, False, pid) if pid > 0 else None    # PROCESS_SET_QUOTA | TERMINATE
    if not handle:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(_JOB, handle))
    finally:
        k32.CloseHandle(handle)


def lock_held(name: str) -> bool:
    """True while some process (this one too) holds the lock ``name``."""
    if name in _LOCKS:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenMutexW.restype = wintypes.HANDLE
        k32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenMutexW(0x00100000, False, "Local\\" + name)     # SYNCHRONIZE
        if handle:
            k32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5             # access denied: it exists
    if not take_lock(name):
        return True
    release_lock(name)
    return False


def is_inside(path, folder) -> bool:
    """True when ``path`` is ``folder`` or lies below it (case-insensitive on Windows)."""
    try:
        p = os.path.normcase(os.path.abspath(str(path)))
        f = os.path.normcase(os.path.abspath(str(folder)))
    except (TypeError, ValueError):
        return False
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)

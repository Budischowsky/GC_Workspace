#!/usr/bin/env python3
"""Handoff of a single mass spectrum to *NIST MS Search*.

NIST MS Search has no API. Its documented way of accepting a spectrum from a
foreign program is a chain of three files plus one process launch, and every
GC/MS tool that talks to it (AMDIS above all) uses exactly this chain:

1. the spectrum itself, written as a NIST ``.msp`` file;
2. a **locator** file ("filespec" file, conventionally ``*.MSD``) whose *first*
   line is the full path of that ``.msp`` file and whose *second* line is the
   directive ``APPEND`` or ``OVERWRITE`` -- ``OVERWRITE`` replaces the contents
   of NIST's spectrum list, ``APPEND`` adds to it;
3. ``AUTOIMP.MSD``, a one-line file holding the full path of the locator file.
   NIST looks for it in the Windows directory and in its own ``MSSEARCH``
   directory and polls the locator file it names, so a *running* NIST picks the
   spectrum up on its own -- which is why an already running instance is
   brought forward rather than started a second time (MS Search is
   single-instance and a second process misbehaves).

The program is then started as ``nistms$.exe /par=2``: ``/par=2`` means "import
the pending spectrum and run a search on it".

Nothing in here may take the workspace down. Discovery, file writes and the
launch are wrapped individually; every failure that reaches the caller is a
:class:`NistError` carrying a German sentence fit for a message box.

This module is deliberately free of Tk: the pure parts (path discovery, MSP
text, locator bytes) are unit-testable without a display, and the GUI layer in
``gc_plots`` supplies the dialogs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "NistError", "NistInstallation",
    "msp_text", "locator_text", "autoimp_text",
    "discover_installations", "find_installation", "installation",
    "remember_mssearch_dir", "expected_locations_text",
    "temp_dir", "write_msp", "write_locator", "launch", "search_spectrum",
]

#: NIST MS Search is a legacy Win32 program: CRLF, no BOM, single-byte codepage.
NIST_ENCODING = "cp1252"
NIST_NEWLINE = "\r\n"

#: ``/par=2`` = import the pending spectrum *and* search it.
SEARCH_PARAMETER = "/par=2"

#: Preferred first -- ``nistms$.exe`` is the entry point that cooperates with an
#: already running MS Search; ``nistms.exe`` is the plain main program.
EXECUTABLE_NAMES = ("nistms$.exe", "nistms.exe")

#: Process names that mean "MS Search is already up".
RUNNING_PROCESS_NAMES = ("nistms.exe", "nistms$.exe")

#: The one-line file that points NIST at the locator file.
AUTOIMP_NAME = "AUTOIMP.MSD"

#: Locator file written when the installation has no AUTOIMP.MSD of its own.
DEFAULT_LOCATOR_NAME = "GCWS.MSD"

#: Directory name under the system temp directory for the workspace's scratch
#: files. NIST must be able to read the .msp back, so it cannot live in a
#: ``TemporaryDirectory`` that disappears when the call returns.
TEMP_DIR_NAME = "NIAS-GC-Workspace"

MSP_FILENAME = "nist_search.msp"

#: Settings live beside NIAS.py's own settings.json but in a separate file, so
#: that this module stays self-contained and a corrupt file damages nothing else.
SETTINGS_DIR_NAME = "NIAS-Screening-Processor"
SETTINGS_FILE_NAME = "gc_nist.json"


class NistError(Exception):
    """A handoff step failed. ``str(exc)`` is a finished German message."""


@dataclass(frozen=True)
class NistInstallation:
    """One discovered MS Search installation.

    ``autoimp``/``locator`` are the files that already exist; both may be None,
    in which case :func:`write_locator` creates them.
    """

    mssearch_dir: Path
    executable: Path
    autoimp: Optional[Path] = None
    locator: Optional[Path] = None
    version: str = ""

    @property
    def label(self) -> str:
        return f"{self.version or 'NIST MS Search'} ({self.mssearch_dir})"


# --------------------------------------------------------------------------
# Pure text generation -- one formatter, several callers
# --------------------------------------------------------------------------

def msp_text(spectrum: Sequence[tuple[float, int]], name: str = "",
             rt: Optional[float] = None, *,
             comment: str = "", cas: str = "") -> str:
    """NIST MSP text for one spectrum.

    The single formatter behind the m/z table's "Als MSP kopieren", the
    spectrum panel's copy/save items and the NIST handoff -- three callers, one
    definition of what the file looks like, so a fix reaches all of them.

    ``comment`` and ``cas`` are what the unknown register's MSP libraries need
    to say about an entry beyond its retention time. They are keyword-only and
    default to nothing, so every existing call produces the same bytes it did
    before they existed.
    """
    head = [f"Name: {name or 'unknown'}"]
    if cas:
        head.append(f"CAS#: {cas}")
    if comment or rt is not None:
        parts = []
        if rt is not None:
            parts.append(f"RT {rt:.4f} min")
        if comment:
            parts.append(comment)
        head.append("Comment: " + "; ".join(parts))
    head.append(f"Num Peaks: {len(spectrum)}")
    body = [f"{mz:.0f} {it};" for mz, it in spectrum]
    return NIST_NEWLINE.join(head + body) + NIST_NEWLINE


def locator_text(msp_path: Path | str, append: bool = False) -> str:
    """The two-line filespec file: spectrum path, then the import directive."""
    directive = "APPEND" if append else "OVERWRITE"
    return f"{msp_path}{NIST_NEWLINE}{directive}{NIST_NEWLINE}"


def autoimp_text(locator_path: Path | str) -> str:
    """AUTOIMP.MSD: one line, the full path of the locator file."""
    return f"{locator_path}{NIST_NEWLINE}"


# --------------------------------------------------------------------------
# Installation discovery
# --------------------------------------------------------------------------

_VERSION_DIGITS = re.compile(r"(\d+)")


def _version_key(directory_name: str) -> int:
    """Sort key for NIST08 / NIST11 / NIST14 / NIST17 / NIST20 / NIST23 / ...

    Four-digit years are folded onto two digits so that ``NIST2020`` does not
    outrank ``NIST23``. Names without digits sort last.
    """
    match = _VERSION_DIGITS.search(directory_name)
    if match is None:
        return -1
    value = int(match.group(1))
    return value % 100 if value >= 1900 else value


def default_search_roots() -> list[Path]:
    """The directories under which ``NIST*\\MSSEARCH`` is looked for."""
    system_drive = os.environ.get("SystemDrive", "C:") + os.sep
    roots = [
        Path(system_drive),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
    ]
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def default_windows_dir() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows"))


def _executable_in(mssearch_dir: Path) -> Optional[Path]:
    for name in EXECUTABLE_NAMES:
        candidate = mssearch_dir / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _read_first_line(path: Path) -> Optional[str]:
    """First non-empty, unquoted line of a NIST helper file."""
    try:
        raw = path.read_text(encoding=NIST_ENCODING, errors="replace")
    except (OSError, ValueError):
        return None
    for line in raw.splitlines():
        stripped = line.strip().strip('"')
        if stripped:
            return stripped
    return None


def _autoimp_locator(autoimp: Path) -> Optional[Path]:
    """The locator file AUTOIMP.MSD names, resolved relative to AUTOIMP.MSD."""
    line = _read_first_line(autoimp)
    if not line:
        return None
    try:
        locator = Path(line)
        if not locator.is_absolute():
            locator = autoimp.parent / locator
        return locator
    except (OSError, ValueError):
        return None


def _installation_at(mssearch_dir: Path,
                     windows_dir: Optional[Path]) -> Optional[NistInstallation]:
    executable = _executable_in(mssearch_dir)
    if executable is None:
        return None
    autoimp: Optional[Path] = None
    for candidate in _autoimp_candidates(mssearch_dir, windows_dir):
        try:
            if candidate.is_file():
                autoimp = candidate
                break
        except OSError:
            continue
    locator = _autoimp_locator(autoimp) if autoimp is not None else None
    return NistInstallation(
        mssearch_dir=mssearch_dir, executable=executable,
        autoimp=autoimp, locator=locator,
        version=mssearch_dir.parent.name or mssearch_dir.name,
    )


def _autoimp_candidates(mssearch_dir: Path,
                        windows_dir: Optional[Path]) -> list[Path]:
    """Where AUTOIMP.MSD may sit, in the order NIST itself looks.

    The Windows directory is the documented location; installations that were
    set up without administrator rights keep it next to the program instead.
    """
    candidates = []
    if windows_dir is not None:
        candidates.append(windows_dir / AUTOIMP_NAME)
    candidates.append(mssearch_dir / AUTOIMP_NAME)
    return candidates


def discover_installations(search_roots: Optional[Iterable[Path]] = None,
                           windows_dir: Optional[Path] = None,
                           ) -> list[NistInstallation]:
    """Every MS Search installation found, newest version first.

    ``search_roots`` and ``windows_dir`` are parameters and not constants so
    that the discovery can be exercised against a synthetic directory tree.
    """
    roots = list(search_roots) if search_roots is not None else default_search_roots()
    if windows_dir is None and search_roots is None:
        windows_dir = default_windows_dir()

    found: list[NistInstallation] = []
    seen: set[str] = set()

    def add(mssearch_dir: Path) -> None:
        key = os.path.normcase(os.path.normpath(str(mssearch_dir)))
        if key in seen:
            return
        install = _installation_at(mssearch_dir, windows_dir)
        if install is not None:
            seen.add(key)
            found.append(install)

    for root in roots:
        try:
            matches = sorted(root.glob("NIST*"),
                             key=lambda p: _version_key(p.name), reverse=True)
        except OSError:
            continue
        for versioned in matches:
            try:
                if not versioned.is_dir():
                    continue
            except OSError:
                continue
            add(versioned / "MSSEARCH")
        # Very old installations put MSSEARCH directly under the root.
        add(root / "MSSEARCH")

    # An AUTOIMP.MSD in the Windows directory names a locator file that lives
    # in the MSSEARCH directory of an installation the globs may have missed
    # (a non-standard drive or directory name).
    if windows_dir is not None:
        autoimp = windows_dir / AUTOIMP_NAME
        locator = _autoimp_locator(autoimp) if _is_file(autoimp) else None
        for hint in _locator_hints(locator):
            add(hint)

    found.sort(key=lambda i: _version_key(i.version), reverse=True)
    return found


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _locator_hints(locator: Optional[Path]) -> list[Path]:
    """Directories worth probing, given a locator path from AUTOIMP.MSD."""
    if locator is None:
        return []
    hints = [locator.parent]
    spectrum_line = _read_first_line(locator) if _is_file(locator) else None
    if spectrum_line:
        try:
            hints.append(Path(spectrum_line).parent)
        except (OSError, ValueError):
            pass
    return hints


def find_installation(search_roots: Optional[Iterable[Path]] = None,
                      windows_dir: Optional[Path] = None,
                      ) -> Optional[NistInstallation]:
    """The newest MS Search installation found, or None."""
    try:
        candidates = discover_installations(search_roots, windows_dir)
    except Exception:
        # Discovery walks the filesystem; nothing it hits may reach the panel.
        return None
    return candidates[0] if candidates else None


def expected_locations_text() -> str:
    """German explanation of where NIST is looked for, for the "not found" case."""
    return (
        "NIST MS Search wurde auf diesem Rechner nicht gefunden.\n\n"
        "Gesucht wird nach:\n"
        "    C:\\NIST*\\MSSEARCH\\nistms$.exe\n"
        "    C:\\Program Files (x86)\\NIST*\\MSSEARCH\\nistms$.exe\n"
        "    C:\\Program Files\\NIST*\\MSSEARCH\\nistms$.exe\n"
        "sowie nach AUTOIMP.MSD in C:\\Windows\\ oder im MSSEARCH-Ordner.\n\n"
        "Ist NIST anderswo installiert, kann der MSSEARCH-Ordner über\n"
        "\"NIST-Ordner wählen …\" gesetzt werden; er wird in\n"
        f"{settings_path()}\n"
        "gespeichert."
    )


# --------------------------------------------------------------------------
# Remembered path -- a setting the analyst can correct
# --------------------------------------------------------------------------

def settings_path() -> Path:
    from nias_paths import settings_path as main_settings
    return main_settings().with_name(SETTINGS_FILE_NAME)


def load_settings() -> dict[str, Any]:
    """Never raises: a missing or corrupt settings file means "no setting"."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(**updates: Any) -> bool:
    """Best effort. A read-only profile must not break the handoff."""
    try:
        path = settings_path()
        data = load_settings()
        data.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return True
    except Exception:
        return False


def remember_mssearch_dir(mssearch_dir: Path | str) -> bool:
    return save_settings(mssearch_dir=str(mssearch_dir),
                         discovered_at=datetime.now().isoformat(timespec="seconds"))


def installation(refresh: bool = False,
                 windows_dir: Optional[Path] = None) -> Optional[NistInstallation]:
    """The installation to use: the remembered one if it still exists.

    The remembered path wins because it is the analyst's correction; it is only
    dropped when the executable is gone, in which case discovery runs again and
    the result replaces it.
    """
    if not refresh:
        remembered = str(load_settings().get("mssearch_dir") or "")
        if remembered:
            try:
                install = _installation_at(
                    Path(remembered),
                    windows_dir if windows_dir is not None else default_windows_dir())
            except Exception:
                install = None
            if install is not None:
                return install

    found = find_installation(windows_dir=windows_dir)
    if found is not None:
        remember_mssearch_dir(found.mssearch_dir)
    return found


# --------------------------------------------------------------------------
# Writing the three files
# --------------------------------------------------------------------------

def temp_dir() -> Path:
    """The workspace's scratch directory; created on demand.

    Not a ``TemporaryDirectory``: NIST reads the .msp *after* this process has
    returned, so the file has to outlive the call.
    """
    path = Path(tempfile.gettempdir()) / TEMP_DIR_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NistError(
            f"Das temporäre Verzeichnis {path} konnte nicht angelegt werden: {exc}"
        ) from exc
    return path


def write_msp(text: str, directory: Optional[Path] = None,
              filename: str = MSP_FILENAME) -> Path:
    """Write the MSP file NIST will import.

    A stable filename keeps the temp directory from filling up; if that file is
    locked -- NIST holding it open, a virus scanner -- a timestamped name is
    used instead rather than failing the handoff.
    """
    target_dir = directory if directory is not None else temp_dir()
    path = target_dir / filename
    for candidate in (path, target_dir / _timestamped(filename)):
        try:
            candidate.write_text(text, encoding=NIST_ENCODING, errors="replace",
                                 newline="")
            return candidate
        except OSError:
            continue
    raise NistError(
        f"Das Spektrum konnte nicht nach {target_dir} geschrieben werden.\n"
        "Bitte prüfen, ob das temporäre Verzeichnis beschreibbar ist.")


def _timestamped(filename: str) -> str:
    stem, _, suffix = filename.rpartition(".")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{stem or filename}_{stamp}" + (f".{suffix}" if suffix else "")


def write_locator(install: NistInstallation, msp_path: Path,
                  append: bool = False) -> Path:
    """Write the filespec file and, if needed, the AUTOIMP.MSD that names it.

    An existing AUTOIMP.MSD is never overwritten -- it may belong to AMDIS or
    another tool, and sharing the locator file is exactly how the mechanism is
    meant to be used. Only when no AUTOIMP.MSD exists at all is one created,
    preferring the MSSEARCH directory over ``C:\\Windows`` because the latter
    usually needs administrator rights.
    """
    locator = install.locator
    if locator is None:
        locator = install.mssearch_dir / DEFAULT_LOCATOR_NAME

    try:
        locator.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        locator.write_text(locator_text(msp_path, append),
                           encoding=NIST_ENCODING, errors="replace", newline="")
    except OSError as exc:
        raise NistError(
            f"Die NIST-Importdatei {locator} konnte nicht geschrieben werden: {exc}\n"
            "Fehlen die Schreibrechte im MSSEARCH-Ordner, hilft ein Start des\n"
            "Workspace als Administrator oder eine NIST-Installation außerhalb\n"
            "von \"Program Files\"."
        ) from exc

    if install.autoimp is None or not _is_file(install.autoimp):
        _write_autoimp(install, locator)
    return locator


def _write_autoimp(install: NistInstallation, locator: Path) -> None:
    """Create AUTOIMP.MSD where NIST will find it; silent if nowhere is writable.

    Silent because a missing AUTOIMP.MSD is not necessarily fatal: NIST may
    already have been configured through a copy this process cannot see. The
    launch below reports the real failure if the spectrum does not arrive.
    """
    for candidate in (install.mssearch_dir / AUTOIMP_NAME,
                      default_windows_dir() / AUTOIMP_NAME):
        try:
            candidate.write_text(autoimp_text(locator), encoding=NIST_ENCODING,
                                 errors="replace", newline="")
            return
        except OSError:
            continue


# --------------------------------------------------------------------------
# Launching / fronting MS Search
# --------------------------------------------------------------------------

def running_pids(names: Sequence[str] = RUNNING_PROCESS_NAMES) -> list[int]:
    """PIDs of running MS Search processes, via the Win32 toolhelp snapshot.

    ctypes rather than a ``tasklist`` subprocess: no console window flashes up
    and no output parsing depends on the system language.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return []

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    wanted = {name.lower() for name in names}
    pids: list[int] = []
    snapshot = None
    try:
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in wanted:
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    except Exception:
        return pids
    finally:
        if snapshot:
            try:
                kernel32.CloseHandle(snapshot)
            except Exception:
                pass
    return pids


def bring_to_front(pids: Sequence[int]) -> bool:
    """Restore and focus the main window of one of ``pids``. False if it failed."""
    if not pids:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception:
        return False

    SW_RESTORE = 9
    wanted = set(pids)
    hits: list[int] = []

    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in wanted and user32.IsWindowVisible(hwnd):
                hits.append(hwnd)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(ENUMPROC(_callback), 0)
    except Exception:
        return False
    if not hits:
        # Running but without a visible top-level window (still starting up):
        # the spectrum is imported by the polling loop regardless.
        return False
    hwnd = hits[0]
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        return False
    return True


def launch(install: NistInstallation) -> str:
    """Start MS Search, or front the instance that is already running.

    Returns ``"gestartet"`` or ``"in den Vordergrund geholt"`` for the status
    line. MS Search is single-instance: starting a second process while one
    runs produces a program that neither imports nor searches, so the running
    one is fronted and left to pick the spectrum up from the locator file.
    """
    pids = running_pids()
    if pids:
        bring_to_front(pids)
        return "in den Vordergrund geholt"

    try:
        subprocess.Popen(
            [str(install.executable), SEARCH_PARAMETER],
            cwd=str(install.mssearch_dir),
            close_fds=True,
        )
    except OSError as exc:
        raise NistError(
            f"NIST MS Search ({install.executable}) konnte nicht gestartet "
            f"werden: {exc}"
        ) from exc
    return "gestartet"


# --------------------------------------------------------------------------
# The whole handoff
# --------------------------------------------------------------------------

def search_spectrum(spectrum: Sequence[tuple[float, int]], name: str = "",
                    rt: Optional[float] = None, append: bool = False,
                    install: Optional[NistInstallation] = None) -> str:
    """Hand one background-corrected spectrum to NIST MS Search.

    The spectrum is expected to be what ``Sample.spectrum()`` returns, i.e.
    background-corrected at the *effective* apex, so the current Apex-Nudge is
    already reflected in it.

    Returns a German status line. Raises :class:`NistError` with a German
    message on any failure; the caller shows it and carries on.
    """
    if not spectrum:
        raise NistError("Für diesen Peak liegt kein Spektrum vor.")

    if install is None:
        install = installation()
    if install is None:
        raise NistError(expected_locations_text())

    msp_path = write_msp(msp_text(spectrum, name, rt))
    write_locator(install, msp_path, append)
    state = launch(install)
    return (f"Spektrum an NIST MS Search übergeben ({len(spectrum)} Ionen) – "
            f"{install.version or 'NIST'} {state}.")

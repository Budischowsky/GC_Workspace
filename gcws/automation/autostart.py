"""Start the watcher with Windows (opt-in): a shortcut in the user's Startup folder.

Nothing is installed or registered; removing the shortcut (here, in the tray menu or by hand)
switches it off again.
"""
from __future__ import annotations

import os
from pathlib import Path

from gcws import paths

LINK_NAME = "GC Workspace Watcher.lnk"


def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / \
        "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def link_path() -> Path:
    return startup_dir() / LINK_NAME


def is_installed() -> bool:
    return link_path().exists()


def install() -> Path:
    """Create the shortcut: the project's pythonw runs ``gcws.watcher_start`` for this data folder."""
    import win32com.client
    from gcws.automation.watcher import python_exe
    link = link_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(link))
    sc.TargetPath = python_exe()
    script = paths.ROOT / "GC Workspace.pyw"
    args = f'"{script}" --watch'
    if paths.DATA.resolve() != (paths.ROOT / "data").resolve():
        args += f' --data "{paths.DATA}"'
    sc.Arguments = args
    sc.WorkingDirectory = str(paths.ROOT)
    sc.Description = "GC Workspace Watcher: processes new GC data in the background"
    sc.Save()
    return link


def remove() -> None:
    try:
        link_path().unlink()
    except OSError:
        pass

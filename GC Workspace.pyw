import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"


def _in_project_venv():
    try:
        return Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()
    except OSError:
        return False


def _error_box(text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "GC Workspace - startup failed", 0x10)
    except Exception:  # noqa: BLE001
        print(text, file=sys.stderr)


# Double-clicking the .pyw runs the system Python, which lacks the dependencies:
# hand over to the project's virtual environment instead.
if not _in_project_venv() and VENV_PYTHONW.exists() and not os.environ.get("GCWS_NO_VENV_SWITCH"):
    subprocess.Popen([str(VENV_PYTHONW), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=str(ROOT))
    sys.exit(0)

# --data <folder>: another data folder (e.g. the watcher started with Windows for a shared one)
if "--data" in sys.argv[:-1]:
    i = sys.argv.index("--data")
    os.environ["GCWS_DATA"] = sys.argv[i + 1]
    del sys.argv[i:i + 2]

sys.path.insert(0, str(ROOT))

try:
    from gcws.app import main
except ImportError as exc:
    _error_box(f"{exc}\n\nPython: {sys.executable}\n\n"
               'Please run "Setup GC Workspace.cmd" first.')
    sys.exit(1)

sys.exit(main())

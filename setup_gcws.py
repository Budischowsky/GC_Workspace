"""Create (or rebuild) the local runtime of GC Workspace on this PC."""
from pathlib import Path
import datetime
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


#: which optional features work on this PC (report preview: Word over COM, pypdfium2, Pillow)
OPTIONAL_CHECK = r"""
def ok(label, test):
    try:
        test()
        print(f"  [ok]      {label}")
    except Exception as exc:
        print(f"  [missing] {label}: {exc}")

def word():
    import winreg
    winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application").Close()

print("Optional features:")
ok("pywin32 (Word -> PDF for the report preview)", lambda: __import__("win32com.client"))
ok("Microsoft Word (report preview)", word)
ok("pypdfium2 (report preview pages)", lambda: __import__("pypdfium2"))
ok("Pillow (images)", lambda: __import__("PIL.Image"))
"""


def setup():
    if sys.version_info < (3, 12):
        raise RuntimeError("Python 3.12 or newer is required (3.14 recommended).")
    env = ROOT / ".venv"
    python = env / "Scripts" / "python.exe"
    marker = env / "gcws-location.json"
    identity = {"project": str(ROOT), "base_python": str(Path(sys.executable).resolve())}
    try:
        valid = json.loads(marker.read_text()) == identity and python.is_file()
        if valid:
            valid = subprocess.run([str(python), "-c", "import pip"], capture_output=True).returncode == 0
    except (OSError, ValueError):
        valid = False
    if env.exists() and not valid:
        backup = ROOT / (".venv-backup-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        env.rename(backup)
        print("Existing runtime kept as", backup.name)
    if not valid:
        subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    wheelhouse = ROOT / "wheelhouse"
    offline = ["--no-index", "--find-links", str(wheelhouse)] if wheelhouse.is_dir() else []
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip", *offline], check=False)
    subprocess.run([str(python), "-m", "pip", "install", *offline, "-r", str(ROOT / "requirements.txt")], check=True)
    subprocess.run([str(python), "-c", "import PySide6, pyqtgraph, numpy, openpyxl, docx; import gcws; "
                    "from gcws import paths; paths.initialize(); print('data folder:', paths.DATA)"],
                   check=True, cwd=str(ROOT))
    marker.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    subprocess.run([str(python), "-c", OPTIONAL_CHECK], check=False, cwd=str(ROOT))
    try:
        sys.path.insert(0, str(ROOT))
        import gcws  # noqa: F401
        import gc_atlas
        print("EI Atlas:", gc_atlas.atlas_root())
    except Exception as exc:  # noqa: BLE001
        print("EI Atlas not found (library search unavailable until configured):", exc)
    print("Done. Start with 'Start GC Workspace.cmd'.")


if __name__ == "__main__":
    try:
        setup()
    except Exception as exc:  # noqa: BLE001
        print("Setup failed:", exc)
        raise SystemExit(1)

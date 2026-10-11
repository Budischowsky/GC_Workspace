"""Create (or rebuild) the local runtime of GC Workspace on this PC."""
from pathlib import Path
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent

#: the Rust kernels (search, norms, integration, deconvolution, fit); without them GC Workspace uses Python
RUST_CRATE = ROOT / "rust" / "gcws_rust"
#: GNU target: links with the toolchain's own MinGW tools, so no Visual Studio is needed
RUST_TOOLCHAIN = "stable-x86_64-pc-windows-gnu"
RUSTUP_INIT = "https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-gnu/rustup-init.exe"
MATURIN = "maturin>=1.15,<2"
#: rustup is installed only on request; without it the shipped wheel or the Python code is used
INSTALL_RUST = "--install-rust" in sys.argv[1:]


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

#: every Rust glue module finds the kernels it needs in the installed extension
RUST_CHECK = r"""
import gcws
from gcws.integration import rust_integration
from gcws.libsearch import norms, rust_search
from gcws.ms import rust_deconv, rust_fit
missing = [m.__name__ for m in (rust_search, norms, rust_integration, rust_deconv, rust_fit) if m._rust is None]
if missing:
    raise SystemExit("the extension lacks the kernels of " + ", ".join(missing))
"""


def rust_digest() -> str:
    """Fingerprint of the crate's sources (line endings ignored), so a changed crate is rebuilt."""
    files = [RUST_CRATE / "Cargo.toml", RUST_CRATE / "Cargo.lock", RUST_CRATE / "pyproject.toml",
             *sorted((RUST_CRATE / "src").rglob("*.rs"))]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(RUST_CRATE).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def rust_works(python: Path) -> bool:
    result = subprocess.run([str(python), "-c", RUST_CHECK], cwd=str(ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        print("  " + (result.stderr.strip().splitlines() or ["Rust kernels not found"])[-1])
    return result.returncode == 0


def find_rustup():
    found = shutil.which("rustup")
    if found:
        return Path(found)
    rustup = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "bin" / "rustup.exe"
    return rustup if rustup.is_file() else None


def rust_toolchain() -> Path:
    """rustup.exe with ``RUST_TOOLCHAIN`` installed; installs rustup itself when this PC has none."""
    rustup = find_rustup()
    if rustup is None:
        print("Installing Rust (rustup, GNU toolchain; no Visual Studio needed) ...")
        folder = Path(tempfile.mkdtemp(prefix="gcws_rustup_"))
        try:
            init = folder / "rustup-init.exe"
            urllib.request.urlretrieve(RUSTUP_INIT, init)
            subprocess.run([str(init), "-y", "--no-modify-path", "--profile", "minimal",
                            "--default-host", "x86_64-pc-windows-gnu", "--default-toolchain", RUST_TOOLCHAIN],
                           check=True)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        rustup = find_rustup()
        if rustup is None:
            raise RuntimeError("rustup was installed but not found in CARGO_HOME\\bin")
    if subprocess.run([str(rustup), "run", RUST_TOOLCHAIN, "rustc", "-V"], capture_output=True).returncode != 0:
        print("Installing the Rust toolchain", RUST_TOOLCHAIN, "...")
        subprocess.run([str(rustup), "toolchain", "install", RUST_TOOLCHAIN, "--profile", "minimal"], check=True)
    return rustup


def build_rust(python: Path) -> None:
    """Build ``rust/gcws_rust`` with maturin and install the wheel into the runtime."""
    rustup = rust_toolchain()
    sysroot = subprocess.run([str(rustup), "run", RUST_TOOLCHAIN, "rustc", "--print", "sysroot"],
                             capture_output=True, text=True, check=True).stdout.strip()
    # gcc, ld and dlltool of the GNU toolchain link the extension
    linker = Path(sysroot) / "lib" / "rustlib" / "x86_64-pc-windows-gnu" / "bin" / "self-contained"
    env = dict(os.environ, RUSTUP_TOOLCHAIN=RUST_TOOLCHAIN,
               CARGO_TARGET_DIR=str(Path(tempfile.gettempdir()) / "gcws_rust_target"))
    env["PATH"] = os.pathsep.join([str(linker), str(rustup.parent), env.get("PATH", "")])
    subprocess.run([str(python), "-m", "pip", "install", MATURIN], check=True)
    print("Building the Rust kernels (the first build takes a few minutes) ...")
    wheels = Path(tempfile.mkdtemp(prefix="gcws_rust_wheels_"))
    try:
        subprocess.run([str(python), "-m", "maturin", "build", "--release", "-i", str(python),
                        "-m", str(RUST_CRATE / "Cargo.toml"), "-o", str(wheels)], check=True, env=env)
        wheel = next(wheels.glob("gcws_rust-*.whl"), None)
        if wheel is None:
            raise RuntimeError("maturin made no wheel")
        subprocess.run([str(python), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)],
                       check=True)
    finally:
        shutil.rmtree(wheels, ignore_errors=True)


def setup_rust(python: Path, env: Path, wheelhouse: Path) -> bool:
    """Install the Rust kernels: kept when up to date, else a wheel from ``wheelhouse/``, else built here."""
    if not RUST_CRATE.is_dir():
        print("Rust sources not found (rust/gcws_rust); GC Workspace uses its Python code.")
        return False
    marker = env / "gcws-rust.json"
    digest = rust_digest()
    try:
        current = json.loads(marker.read_text())["sources"] == digest
    except (OSError, ValueError, KeyError, TypeError):
        current = False
    if current and rust_works(python):
        return True
    shipped = sorted(wheelhouse.glob("gcws_rust-*.whl")) if wheelhouse.is_dir() else []
    if shipped:
        print("Installing the Rust kernels from wheelhouse ...")
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--find-links", str(wheelhouse),
                        "--force-reinstall", "--no-deps", "gcws_rust"], check=False)
        if rust_works(python):
            marker.write_text(json.dumps({"sources": digest, "from": "wheelhouse"}, indent=2), encoding="utf-8")
            return True
    if find_rustup() is None and not INSTALL_RUST:
        print("Rust is not installed on this PC: GC Workspace uses its Python code "
              "(run the setup with --install-rust to install it).")
        return False
    try:
        build_rust(python)
    except Exception as exc:  # noqa: BLE001 - without the kernels GC Workspace still works
        print("Rust kernels not built:", exc)
        return False
    if not rust_works(python):
        return False
    marker.write_text(json.dumps({"sources": digest, "from": "build"}, indent=2), encoding="utf-8")
    return True


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
    # offline only with a full wheelhouse; one holding just the Rust wheel still installs from the internet
    packages = [w for w in wheelhouse.glob("*.whl") if not w.name.startswith("gcws_rust-")]         if wheelhouse.is_dir() else []
    offline = ["--no-index", "--find-links", str(wheelhouse)] if packages else []
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip", *offline], check=False)
    subprocess.run([str(python), "-m", "pip", "install", *offline, "-r", str(ROOT / "requirements.txt")], check=True)
    subprocess.run([str(python), "-c", "import PySide6, pyqtgraph, numpy, openpyxl, docx; import gcws; "
                    "from gcws import paths; paths.initialize(); print('data folder:', paths.DATA)"],
                   check=True, cwd=str(ROOT))
    marker.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    try:
        rust = setup_rust(python, env, wheelhouse)
    except Exception as exc:  # noqa: BLE001 - the Rust kernels are optional
        print("Rust kernels not installed:", exc)
        rust = False
    subprocess.run([str(python), "-c", OPTIONAL_CHECK], check=False, cwd=str(ROOT))
    if rust:
        print("  [ok]      Rust kernels (faster search, integration, deconvolution)")
    else:
        print("  [missing] Rust kernels: GC Workspace uses its Python code (same results, slower); "
              "see the message above")
    try:
        sys.path.insert(0, str(ROOT))
        import gcws  # noqa: F401
        import gc_atlas
        print("SpectrAtlas:", gc_atlas.atlas_root())
    except Exception as exc:  # noqa: BLE001
        print("SpectrAtlas not found (library search unavailable until configured):", exc)
    print("Done. Start with 'Start GC Workspace.cmd'.")


if __name__ == "__main__":
    try:
        setup()
    except Exception as exc:  # noqa: BLE001
        print("Setup failed:", exc)
        raise SystemExit(1)

# Builds the Rust search kernels (rust/gcws_rust) and installs them into .venv.
#
# Needs rustup with the stable x86_64-pc-windows-gnu toolchain (no Visual Studio required) and
# maturin in .venv (pip install maturin). Without the extension GC Workspace keeps its Python search.
#   powershell -ExecutionPolicy Bypass -File tools\build_rust.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$toolchain = Join-Path $env:USERPROFILE ".rustup\toolchains\stable-x86_64-pc-windows-gnu\lib\rustlib\x86_64-pc-windows-gnu\bin\self-contained"
$env:Path = "$toolchain;$env:USERPROFILE\.cargo\bin;$env:Path"
$wheels = Join-Path $env:TEMP "gcws_rust_wheels"
Remove-Item -Recurse -Force $wheels -ErrorAction SilentlyContinue
& $python -m maturin build --release -i $python -m (Join-Path $root "rust\gcws_rust\Cargo.toml") -o $wheels
if ($LASTEXITCODE -ne 0) { throw "maturin build failed" }
$wheel = Get-ChildItem $wheels -Filter "gcws_rust-*.whl" | Select-Object -First 1
& $python -m pip install --force-reinstall --no-deps $wheel.FullName
& $python -c "import gcws_rust; from gcws.libsearch import rust_search; print('Rust search parts:', sorted(rust_search.parts()))"

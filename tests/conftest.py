import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GCWS_DATA", str(ROOT / "tests" / "_data"))

import gcws  # noqa: E402,F401  (bootstraps the vendored modules)

DEFAULT_SAMPLES = ROOT.parent / "NIAS Working" / "samples" / "26016605_GIOSUN1635"
SAMPLES = Path(os.environ.get("GCWS_SAMPLES", DEFAULT_SAMPLES))


def run_dir(prefix: str) -> Path:
    for p in sorted(SAMPLES.glob(f"{prefix}*.D")):
        if p.is_dir():
            return p
    pytest.skip(f"sample run {prefix} not available")


@pytest.fixture(scope="session")
def samples() -> Path:
    if not SAMPLES.is_dir():
        pytest.skip("sample batch not available (set GCWS_SAMPLES)")
    return SAMPLES


@pytest.fixture(scope="session")
def run07():
    from gcws.io.run_loader import load_run
    return load_run(run_dir("07_"))

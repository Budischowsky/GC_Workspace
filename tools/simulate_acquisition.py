r"""Try an automation workflow: copy a finished batch into a watched folder run by run, the way
the instrument writes it (sequence log first, each .D growing, checksum.xml last, "Sequence
completed" at the end). The source folder is only read.

    .venv\Scripts\python.exe tools\simulate_acquisition.py --source "..\NIAS Working\samples\26016605_GIOSUN1635"
        --target C:\temp\watch --delay 30 [--skip 13_]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gcws.automation.simulate import simulate_acquisition  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="finished batch folder (only read)")
    ap.add_argument("--target", required=True, help="the watched folder; the batch is copied into it")
    ap.add_argument("--delay", type=float, default=30.0, help="seconds between the pieces")
    ap.add_argument("--chunks", type=int, default=3, help="pieces per run")
    ap.add_argument("--skip", action="append", default=[], help="leave out runs starting with this (repeatable)")
    a = ap.parse_args()
    out = simulate_acquisition(a.source, a.target, delay_s=a.delay, chunks=a.chunks, skip=tuple(a.skip),
                               progress=lambda t: print(t, flush=True))
    print(f"done: {out}")


if __name__ == "__main__":
    main()

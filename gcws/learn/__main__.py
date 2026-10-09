"""python -m gcws.learn check <root> [--out DIR]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gcws.learn")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="parse every evaluation workbook under ROOT and write a corpus check")
    check.add_argument("root", type=Path)
    check.add_argument("--out", type=Path, default=None, help="output folder (default: <data>/learn)")
    args = parser.parse_args(argv)
    if args.command == "check":
        from gcws import paths
        from gcws.learn.check import run_check
        out = args.out or paths.DATA / "learn"
        s = run_check(args.root, out)
        print(f"{s['parsed']} of {s['workbooks']} workbooks parsed, {s['failed']} failed, "
              f"{len(s['problems'])} with problems -> {out / 'corpus_check.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""python -m gcws.learn check <root> [--out DIR]
python -m gcws.learn baseline <root> [--out DIR] [--method NIAS] [--limit N] [--force]
python -m gcws.learn fit {detection,background,report,naming} <root> [--out DIR] [--method NIAS]"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _ensure_app() -> None:
    """The workspace needs a QApplication (undo stacks, settings), offscreen as in automation/child.py."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication
    QCoreApplication.setOrganizationName("GCWorkspace")
    QCoreApplication.setApplicationName("GC Workspace Learn")
    QApplication.instance() or QApplication(["gcws-learn"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gcws.learn")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="parse every evaluation workbook under ROOT and write a corpus check")
    check.add_argument("root", type=Path)
    check.add_argument("--out", type=Path, default=None, help="output folder (default: <data>/learn)")
    base = sub.add_parser("baseline", help="process, match and score every evaluated run under ROOT")
    base.add_argument("root", type=Path)
    base.add_argument("--out", type=Path, default=None, help="output folder (default: <data>/learn)")
    base.add_argument("--method", default="NIAS", help="processing method name (default: NIAS)")
    base.add_argument("--limit", type=int, default=None, help="only the first N workbooks")
    base.add_argument("--force", action="store_true", help="process again instead of using the cache")
    fitp = sub.add_parser("fit", help="fit settings on the corpus and write a proposal (nothing is applied)")
    fitp.add_argument("target", choices=("detection", "background", "report", "naming"))
    fitp.add_argument("root", type=Path)
    fitp.add_argument("--out", type=Path, default=None, help="output folder (default: <data>/learn)")
    fitp.add_argument("--method", default="NIAS", help="processing method name (default: NIAS)")
    args = parser.parse_args(argv)
    from gcws import paths
    out = args.out or paths.DATA / "learn"
    if args.command == "check":
        from gcws.learn.check import run_check
        s = run_check(args.root, out)
        print(f"{s['parsed']} of {s['workbooks']} workbooks parsed, {s['failed']} failed, "
              f"{len(s['problems'])} with problems -> {out / 'corpus_check.md'}")
    elif args.command == "baseline":
        from gcws.learn import baseline as BL
        _ensure_app()
        result = BL.run_baseline(args.root, out, args.method, force=args.force, limit=args.limit)
        agg = result["aggregate"]
        print(f"{agg['scored']} runs scored, {agg['failed']} failed -> {out / 'baseline.md'}")
    elif args.command == "fit":
        from gcws.learn import propose
        _ensure_app()
        r = propose.run_fit(args.target, args.root, out, args.method)
        print(f"{r.target}: current {r.current} -> proposed {r.best}; CV {r.cv_current} -> {r.cv_best}; "
              f"accept recommended: {r.accept_recommended} -> {out / 'proposals' / (r.target + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Compare the vendored modules with their origins (read-only).

Usage: python tools/check_vendor.py [--write]
  --write   regenerate VENDORED.md from the current vendored files
Files carrying a ``GCWS-PATCH`` marker are expected to differ.

Two sets are vendored: the NIAS modules (from ``NIAS Working``) and EI Atlas's library
readers and search engine (from ``UnknownEvaluation``), which the built-in library
search uses unchanged.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETS = [
    ("NIAS modules", ROOT / "gcws" / "nias_legacy", ROOT.parent / "NIAS Working",
     "Copied from `NIAS Working` so GC Workspace runs on its own. Only files marked *patched* were\n"
     "changed; every change carries a `GCWS-PATCH` comment."),
    ("EI Atlas search engine", ROOT / "gcws" / "libsearch" / "vendor", ROOT.parent / "UnknownEvaluation",
     "EI Atlas's library readers (Agilent .L, NIST MS Search, Wiley/Shimadzu .lib, MSP), its search index\n"
     "and scoring (PBM, NIST-style similarity), copied unchanged from `UnknownEvaluation` (working tree of\n"
     "2026-09-26) into `gcws/libsearch/vendor`. `gcws/libsearch/service.py` subclasses `Engine` to load an\n"
     "explicit list of libraries (Identify > Libraries...), so EI Atlas itself is not needed for searching."),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def files(folder: Path):
    return sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def rows_of(folder: Path, origin: Path):
    rows = []
    for p in files(folder):
        rel = p.relative_to(folder)
        src = origin / rel
        patched = p.suffix == ".py" and "GCWS-PATCH" in p.read_text(encoding="utf-8", errors="replace")
        state = "missing in origin" if not src.exists() else ("identical" if sha(src) == sha(p) else "differs")
        rows.append((str(rel), sha(p), "patched" if patched else "verbatim", state))
    return rows


def main(argv):
    lines = ["# Vendored modules", "", "Check drift with `python tools/check_vendor.py`.", ""]
    for title, folder, origin, text in SETS:
        rows = rows_of(folder, origin)
        lines += [f"## {title}", "", text, "", "| File | SHA-256 (16) | Kind |", "|---|---|---|"]
        lines += [f"| {r} | `{h}` | {k} |" for r, h, k, _ in rows]
        lines.append("")
        print(f"-- {title} ({origin})")
        width = max(len(r[0]) for r in rows)
        for r, h, k, s in rows:
            flag = "" if (s == "identical" and k == "verbatim") or (k == "patched") else "  <-- check"
            print(f"{r:{width}}  {h}  {k:8}  {s}{flag}")
    if "--write" in argv:
        (ROOT / "VENDORED.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])

"""Compare the vendored NIAS modules with NIAS Working (read-only).

Usage: python tools/check_vendor.py [--write]
  --write   regenerate VENDORED.md from the current vendored files
Files carrying a ``GCWS-PATCH`` marker are expected to differ.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "gcws" / "nias_legacy"
NIAS = ROOT.parent / "NIAS Working"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def files():
    return sorted(p for p in LEGACY.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def main(argv):
    rows = []
    for p in files():
        rel = p.relative_to(LEGACY)
        src = NIAS / rel
        patched = p.suffix == ".py" and "GCWS-PATCH" in p.read_text(encoding="utf-8", errors="replace")
        state = "missing in NIAS" if not src.exists() else ("identical" if sha(src) == sha(p) else "differs")
        rows.append((str(rel), sha(p), "patched" if patched else "verbatim", state))
    if "--write" in argv:
        lines = ["# Vendored NIAS modules", "",
                 "Copied from `NIAS Working` so GC Workspace runs on its own. Only files marked *patched* were",
                 "changed; every change carries a `GCWS-PATCH` comment. Check drift with",
                 "`python tools/check_vendor.py`.", "",
                 "| File | SHA-256 (16) | Kind |", "|---|---|---|"]
        lines += [f"| {r} | `{h}` | {k} |" for r, h, k, _ in rows]
        (ROOT / "VENDORED.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    width = max(len(r[0]) for r in rows)
    for r, h, k, s in rows:
        flag = "" if (s == "identical" and k == "verbatim") or (k == "patched") else "  <-- check"
        print(f"{r:{width}}  {h}  {k:8}  {s}{flag}")


if __name__ == "__main__":
    main(sys.argv[1:])

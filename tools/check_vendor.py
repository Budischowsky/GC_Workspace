"""Compare the vendored modules with their origins (read-only).

Usage: python tools/check_vendor.py [--write]
  --write   regenerate VENDORED.md from the current vendored files
Files carrying a ``GCWS-PATCH`` marker are expected to differ.

Two sets are vendored: the NIAS modules (from ``NIAS Working``) and SpectrAtlas's library
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
    ("SpectrAtlas search engine", ROOT / "gcws" / "libsearch" / "vendor", ROOT.parent / "UnknownEvaluation",
     "SpectrAtlas's library readers (Agilent .L, NIST MS Search, Wiley/Shimadzu .lib, MSP), its search index\n"
     "and scoring (PBM, NIST-style similarity), copied unchanged from `UnknownEvaluation` (working tree of\n"
     "2026-09-26) into `gcws/libsearch/vendor`. `gcws/libsearch/service.py` subclasses `Engine` to load an\n"
     "explicit list of libraries (Identify > Libraries...), so SpectrAtlas itself is not needed for searching."),
]


#: Algorithms re-implemented in Python from other projects (not copied files, so not checked for drift).
PORTED = """## Ported algorithms (mzmine)

`gcws/features` re-implements in Python algorithms of mzmine (https://github.com/mzmine/mzmine,
commit ea6ee5e of 2026-09-28): the GC aligner's row score (`align_gc/GcRowAlignScorer`,
`align_join/RowVsRowScore`), the multi-list aligner (`align_common/BaseFeatureListAligner`), the
consensus quantifier ion (`align_gc/GCConsensusAlignerPostProcessor`), the gap filler
(`gapfill_peakfinder/Gap`), the spectral similarities (`util/scans/similarity`: `Weights`,
weighted and composite cosine) and the annotation RI score (`AnnotationSummary`). Each ported
function names its source. ADAP (dulab) and mzmine 2 (GPL) code is not used.

The MIT License (MIT)

Copyright (c) 2004-2025 The mzmine Development Team

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


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
    lines += ["", PORTED.rstrip("\n")]
    if "--write" in argv:
        (ROOT / "VENDORED.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])

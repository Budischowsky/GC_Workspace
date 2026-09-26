"""Built-in EI library search: GC Workspace searches its own libraries, no EI Atlas needed.

The readers and the scoring (PBM, NIST-style similarity) are EI Atlas's own code, copied
unchanged into ``vendor`` (see VENDORED.md); they import each other by flat names, so the
folder goes on ``sys.path`` like the vendored NIAS modules.
"""
from __future__ import annotations

import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"


def bootstrap() -> None:
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))


bootstrap()

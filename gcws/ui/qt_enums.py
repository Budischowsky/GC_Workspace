"""Fast short enum names: ``Qt.DisplayRole`` as quick as ``Qt.ItemDataRole.DisplayRole``.

PySide6 resolves a short name (``Qt.AlignRight``, ``QHeaderView.Stretch``) through a fallback
lookup on every access, about 4 us each; the code uses short names everywhere, also in what runs
for every table cell and plot item on every repaint. Each short name is set once on its class as
the very member the fallback returns, so later accesses are plain attribute lookups.
"""
from __future__ import annotations

import enum

_done = False


def install() -> int:
    """Once per process; returns how many names were set."""
    global _done
    if _done:
        return 0
    _done = True
    from PySide6 import QtCore, QtGui, QtWidgets
    n = 0
    for module in (QtCore, QtGui, QtWidgets):
        for attr in dir(module):                     # the module loads its classes lazily: not vars()
            cls = getattr(module, attr, None)
            if not isinstance(cls, type) or issubclass(cls, enum.Enum):
                continue
            own = vars(cls)
            for val in list(own.values()):
                if not (isinstance(val, type) and issubclass(val, enum.Enum)):
                    continue
                for name, member in val.__members__.items():
                    if name in own:
                        continue
                    try:
                        if getattr(cls, name) is not member:      # another enum's member of that name wins
                            continue
                        setattr(cls, name, member)
                        n += 1
                    except (AttributeError, TypeError):
                        continue
    return n

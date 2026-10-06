"""The report button of the result pages: one click previews the usual report, the arrow offers the others."""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QMenu, QToolButton

from gcws.ui import theme

#: report kind -> menu text
REPORTS = {"nias": "NIAS report...", "fingerprint": "Fingerprint report...",
           "total_extraction": "Total extraction report...", "hs_screening": "HS-Screening report..."}


def report_button(parent, preview: Callable[[], None], report: Callable[[str], None],
                  export: Callable[[], None], export_text: str = "Export worksheet...") -> QToolButton:
    """``Report preview`` with a menu of every report kind and the worksheet export.

    ``preview()`` shows the report that fits the quantification; ``report(kind)`` writes one."""
    b = QToolButton(parent)
    b.setText("Report preview")
    b.setToolTip("The report of these determinations with the rows and values chosen here; "
                 "the arrow: the other reports and the export")
    b.setPopupMode(QToolButton.MenuButtonPopup)
    theme.set_primary(b)
    b.clicked.connect(preview)
    menu = QMenu(b)
    for kind, text in REPORTS.items():
        menu.addAction(text, lambda k=kind: report(k))
    menu.addSeparator()
    menu.addAction(export_text, export)
    b.setMenu(menu)
    return b

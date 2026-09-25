"""Application entry point."""
from __future__ import annotations

import logging
import sys
import traceback

from gcws import paths


def _setup_logging():
    paths.initialize()
    logging.basicConfig(filename=str(paths.logs_dir() / "gcws.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    _setup_logging()
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    QCoreApplication.setOrganizationName("GCWorkspace")
    QCoreApplication.setApplicationName("GC Workspace")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(paths.DATA))
    app = QApplication.instance() or QApplication(argv)
    from gcws.ui import theme
    theme.apply(app)                  # before any icon or plot is created
    from gcws.ui.main_window import MainWindow

    def excepthook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        logging.error(text)
        try:
            QMessageBox.critical(None, "GC Workspace - error", f"{value}\n\n{text[-1500:]}")
        except Exception:  # noqa: BLE001
            print(text, file=sys.stderr)

    sys.excepthook = excepthook
    win = MainWindow()
    win.show()
    for arg in argv[1:]:
        if arg.lower().endswith(".gcws"):
            win.open_project(arg)
        else:
            win.load_runs([arg])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

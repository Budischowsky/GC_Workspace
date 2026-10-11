"""The workspace of the main window: :class:`gcws.core.workspace.Workspace` with Qt.

Holds no widgets. Adds what the window needs: Qt signals (docks observe them), Qt undo stacks
(the Edit menu), the quantification recomputed shortly after the last change, the solvent cut
remembered in QSettings, the theme's run colours, the class hints and the whole-run
deconvolution on the thread pool.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, QTimer, Signal as QtSignal
from PySide6.QtGui import QUndoGroup, QUndoStack

from gcws.core.workspace import Workspace as CoreWorkspace
from gcws.ui import theme


class Workspace(CoreWorkspace, QObject):
    # the core workspace's signals, as Qt signals (same names and arguments)
    runAdded = QtSignal(str)
    runRemoved = QtSignal(str)
    runChanged = QtSignal(str)
    activeRunChanged = QtSignal(str)
    signalKeyChanged = QtSignal(str)
    resultChanged = QtSignal(str, str)
    identsChanged = QtSignal(str, str)
    selectionChanged = QtSignal(str, int)
    methodChanged = QtSignal(str)
    replicatesChanged = QtSignal()
    quantChanged = QtSignal()
    deconvChanged = QtSignal(str)
    deconvJobChanged = QtSignal(str, bool, str)
    spectrumChanged = QtSignal(str)
    solventCutChanged = QtSignal()
    orderChanged = QtSignal()
    panelsChanged = QtSignal()
    peakFocusRequested = QtSignal(int)
    message = QtSignal(str)

    palette = theme.RUN_COLORS                        # changed in place when the theme changes

    def __init__(self, parent=None):
        # QObject first (the core workspace connects to the signals); the core class comes first
        # in the bases so QObject's initialiser does not run it a second time
        QObject.__init__(self, parent)
        self._quant_timer = QTimer(self)
        self._quant_timer.setSingleShot(True)
        self._quant_timer.setInterval(150)
        CoreWorkspace.__init__(self)
        self._quant_timer.timeout.connect(self.recompute_quant)
        from gcws.ui.hint_cache import HintCache
        self.hints = HintCache(self, self)

    def _new_undo_group(self):
        return QUndoGroup(self)

    def _new_undo_stack(self):
        return QUndoStack(self)

    def _remembered_solvent_cut(self) -> bool:
        return QSettings().value("integration/solvent_cut", False, type=bool)

    def _remember_solvent_cut(self, enabled: bool) -> None:
        QSettings().setValue("integration/solvent_cut", enabled)

    def shade_color(self, color: str, rank: int) -> str:
        return theme.shade(color, rank)

    def _submit(self, fn, *args, on_done, on_error, **kwargs) -> None:
        from gcws.ui import workers
        workers.submit(fn, *args, on_done=on_done, on_error=on_error, **kwargs)

    def schedule_quant(self) -> None:
        self._quant_timer.start()

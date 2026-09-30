"""A menu that stays open while its check boxes are switched (View: several panels at once).

A click on a checkable action toggles it and leaves the menu open; any other action works as
usual, and a click outside the menu (or Esc) closes it.
"""
from __future__ import annotations

from PySide6.QtWidgets import QMenu


class StayOpenMenu(QMenu):
    def _checkable_at(self, event):
        action = self.actionAt(event.position().toPoint())
        if action is not None and action.isCheckable() and action.isEnabled() and action.menu() is None:
            return action
        return None

    def mouseReleaseEvent(self, event):
        action = self._checkable_at(event)
        if action is None:
            super().mouseReleaseEvent(event)
            return
        action.trigger()
        self.setActiveAction(action)
        event.accept()

    def keyPressEvent(self, event):
        from PySide6.QtCore import Qt
        action = self.activeAction()
        if event.key() == Qt.Key_Space and action is not None and action.isCheckable() and action.isEnabled():
            action.trigger()
            event.accept()
            return
        super().keyPressEvent(event)

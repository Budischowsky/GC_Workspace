"""Help > User manual: the chapters of ``gcws/manual`` with a table of contents and a search."""
from __future__ import annotations

import re

from PySide6.QtCore import QByteArray, QRectF, QSettings, QSize, Qt, QUrl
from PySide6.QtGui import (QBrush, QDesktopServices, QFont, QImage, QKeySequence, QPainter, QShortcut,
                           QTextCharFormat, QTextCursor, QTextDocument, QTextFrameFormat, QTextLength, QTextTable)
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QSplitter,
                               QStackedWidget, QTextBrowser, QTextEdit, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from gcws import manual as M
from gcws.ui import theme

#: colours the pictures are drawn in (light theme); the viewer swaps them for the current theme's
PICTURE_TOKENS = ("TEXT", "MUTED", "FAINT", "BORDER", "BORDER_STRONG", "ACCENT", "ACCENT_SOFT", "ACCENT_SOFT2",
                  "ACCENT_TEXT", "SURFACE_ALT", "BG", "OK", "OK_SOFT", "WARN", "WARN_SOFT", "BAD", "BAD_SOFT",
                  "INFO", "INFO_SOFT", "NEUTRAL", "NEUTRAL_SOFT")


def themed_svg(text: str) -> str:
    """An SVG drawn in the light theme's colours, in the colours of the current theme."""
    if theme.MODE == "light":
        return text
    now = theme.THEMES[theme.MODE]
    swap = {theme.LIGHT[t].lower(): now[t] for t in PICTURE_TOKENS}
    return re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: swap.get(m.group(0).lower(), m.group(0)), text)


class ManualView(QTextBrowser):
    """One chapter. Links are handled by the window; pictures are recoloured for the theme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setObjectName("manualView")
        self.document().setDocumentMargin(18)

    def loadResource(self, kind, url):
        name = url.toString()
        if kind == QTextDocument.ImageResource and name.lower().endswith(".svg"):
            path = M.DIR / name
            if path.is_file():
                return self._render(themed_svg(path.read_text(encoding="utf-8")))
        return super().loadResource(kind, url)

    def _render(self, svg: str) -> QImage:
        from PySide6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        size = renderer.defaultSize()
        room = max(320, self.viewport().width() - 2 * int(self.document().documentMargin()) - 8)
        if size.width() > room:                                 # never wider than the page
            size = QSize(room, round(size.height() * room / size.width()))
        ratio = self.devicePixelRatioF()
        image = QImage(size * ratio, QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        renderer.render(painter, QRectF(0, 0, size.width(), size.height()))
        painter.end()
        return image

    def show_markdown(self, text: str) -> None:
        self.document().clear()                                 # also drops the cached pictures
        self.setMarkdown(text)
        self._style()

    def _style(self) -> None:
        doc = self.document()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        block = doc.begin()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level:
                fmt = QTextCharFormat()
                fmt.setForeground(QBrush(theme.qcolor(theme.ACCENT_TEXT if level == 1 else theme.TEXT)))
                fmt.setFontWeight(QFont.DemiBold)
                cursor.setPosition(block.position())
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                cursor.mergeCharFormat(fmt)
                bf = block.blockFormat()
                bf.setTopMargin(6 if level == 1 else 22 if level == 2 else 14)
                bf.setBottomMargin(8)
                cursor.setBlockFormat(bf)
            it = block.begin()
            while not it.atEnd():                               # keys and code: a readable fixed font
                frag = it.fragment()
                if frag.isValid() and frag.charFormat().fontFixedPitch():
                    fmt = QTextCharFormat()
                    fmt.setFontFamilies(["Consolas", "Cascadia Mono", "Courier New"])
                    fmt.setBackground(QBrush(theme.qcolor(theme.NEUTRAL_SOFT)))
                    cursor.setPosition(frag.position())
                    cursor.setPosition(frag.position() + frag.length(), QTextCursor.KeepAnchor)
                    cursor.mergeCharFormat(fmt)
                it += 1
            block = block.next()
        for frame in doc.rootFrame().childFrames():
            if isinstance(frame, QTextTable):
                self._style_table(frame)
        cursor.endEditBlock()

    @staticmethod
    def _style_table(table: QTextTable) -> None:
        fmt = table.format()
        fmt.setCellPadding(6)
        fmt.setCellSpacing(0)
        fmt.setBorder(1)
        fmt.setBorderBrush(QBrush(theme.qcolor(theme.BORDER)))
        fmt.setBorderStyle(QTextFrameFormat.BorderStyle_Solid)
        fmt.setBorderCollapse(True)
        fmt.setWidth(QTextLength(QTextLength.PercentageLength, 100))
        fmt.setTopMargin(6)
        fmt.setBottomMargin(12)
        table.setFormat(fmt)
        for col in range(table.columns()):                      # the header row
            cell = table.cellAt(0, col)
            cf = cell.format()
            cf.setBackground(QBrush(theme.qcolor(theme.SURFACE_ALT)))
            cell.setFormat(cf)

    def heading_block(self, name: str):
        block = self.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel() and M.anchor(block.text()) == name:
                return block
            block = block.next()
        return None

    def scroll_to(self, block) -> None:
        top = self.document().documentLayout().blockBoundingRect(block).top()
        self.verticalScrollBar().setValue(max(0, int(top) - 8))

    def highlight(self, words: list[str], start: int = 0) -> int:
        """Mark every occurrence of the words; the position of the first one at or after ``start``."""
        doc, marks, first = self.document(), [], -1
        fmt = QTextCharFormat()
        fmt.setBackground(QBrush(theme.qcolor(theme.WARN_SOFT)))
        fmt.setForeground(QBrush(theme.qcolor(theme.TEXT)))
        for word in words:
            cursor = doc.find(word, 0)
            while not cursor.isNull():
                sel = QTextEdit.ExtraSelection()
                sel.cursor, sel.format = cursor, fmt
                marks.append(sel)
                if cursor.selectionStart() >= start and (first < 0 or cursor.selectionStart() < first):
                    first = cursor.selectionStart()
                cursor = doc.find(word, cursor)
        self.setExtraSelections(marks)
        return first


class ManualWindow(QDialog):
    """The manual: chapters and their sections on the left (or the search results), the text on the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GC Workspace - User manual")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint | Qt.WindowMinimizeButtonHint)
        self.resize(1180, 820)
        self.current: tuple[str, str] = ("", "")                # chapter slug, section anchor
        self.history: list[tuple[str, str]] = []
        self.future: list[tuple[str, str]] = []

        self.back_btn = QToolButton()
        self.back_btn.setText("◀ Back")
        self.back_btn.setToolTip("The section shown before  [Alt+Left]")
        self.back_btn.clicked.connect(self.back)
        self.forward_btn = QToolButton()
        self.forward_btn.setText("Forward ▶")
        self.forward_btn.setToolTip("Forward again  [Alt+Right]")
        self.forward_btn.clicked.connect(self.forward)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search the manual...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._search)
        self.search_box.returnPressed.connect(self._first_result)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self._tree_clicked)
        self.tree.itemActivated.connect(self._tree_clicked)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.itemClicked.connect(self._result_clicked)
        self.results.itemActivated.connect(self._result_clicked)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.results)

        self.view = ManualView()
        self.view.anchorClicked.connect(self._link)

        nav = QHBoxLayout()
        nav.addWidget(self.back_btn)
        nav.addWidget(self.forward_btn)
        nav.addStretch(1)
        left = QWidget()
        col = QVBoxLayout(left)
        col.setContentsMargins(0, 0, 0, 0)
        col.addLayout(nav)
        col.addWidget(self.search_box)
        col.addWidget(self.stack, 1)
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(left)
        self.split.addWidget(self.view)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([310, 870])
        self.split.setChildrenCollapsible(False)
        lay = QVBoxLayout(self)
        lay.addWidget(self.split)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Alt+Left"), self, activated=self.back)
        QShortcut(QKeySequence("Alt+Right"), self, activated=self.forward)
        theme.notifier().changed.connect(self._theme_changed)
        geometry = QSettings().value("manual/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self._fill_tree()
        self._nav_buttons()
        if M.chapters():
            self.open(M.chapters()[0].slug, remember=False)

    # -- contents -------------------------------------------------------------

    def _fill_tree(self) -> None:
        self.tree.clear()
        parts: dict[str, QTreeWidgetItem] = {}
        for ch in M.chapters():
            part = parts.get(ch.part)
            if part is None:
                part = parts[ch.part] = QTreeWidgetItem(self.tree, [ch.part])
                font = part.font(0)
                font.setBold(True)
                part.setFont(0, font)
                part.setFlags(Qt.ItemIsEnabled)
                part.setExpanded(True)
            item = QTreeWidgetItem(part, [ch.title])
            item.setData(0, Qt.UserRole, (ch.slug, ""))
            for sec in ch.sections:
                if sec.level == 2:
                    sub = QTreeWidgetItem(item, [sec.title])
                    sub.setData(0, Qt.UserRole, (ch.slug, sec.anchor))

    def _tree_clicked(self, item, _column=0) -> None:
        target = item.data(0, Qt.UserRole)
        if target:
            self.open(*target)

    def _select_in_tree(self, slug: str) -> None:
        for i in range(self.tree.topLevelItemCount()):
            part = self.tree.topLevelItem(i)
            for j in range(part.childCount()):
                item = part.child(j)
                if item.data(0, Qt.UserRole)[0] == slug:
                    self.tree.blockSignals(True)
                    self.tree.setCurrentItem(item)
                    self.tree.blockSignals(False)
                    return

    # -- showing ---------------------------------------------------------------

    def open(self, slug: str, section: str = "", words: list[str] | None = None, remember: bool = True) -> bool:
        """Show a chapter, scrolled to one of its sections; search words are highlighted."""
        ch = M.chapter(slug)
        if ch is None:
            return False
        if remember and self.current[0] and self.current != (slug, section):
            self.history.append(self.current)
            self.future.clear()
        if self.current[0] != slug or not self.view.document().characterCount() > 1:
            self.view.show_markdown(ch.markdown)
        self.current = (slug, section)
        block = self.view.heading_block(section) if section else None
        if block is not None:
            self.view.scroll_to(block)
        else:
            self.view.verticalScrollBar().setValue(0)
        first = self.view.highlight(words or [], block.position() if block is not None else 0)
        if words and first >= 0:
            found = self.view.document().findBlock(first)
            rect = self.view.document().documentLayout().blockBoundingRect(found)
            bar, page = self.view.verticalScrollBar(), self.view.viewport().height()
            if rect.bottom() > bar.value() + page:                 # the hit is further down the section
                bar.setValue(int(rect.top()) - page // 3)
        self._select_in_tree(slug)
        self._nav_buttons()
        return True

    def _nav_buttons(self) -> None:
        self.back_btn.setEnabled(bool(self.history))
        self.forward_btn.setEnabled(bool(self.future))

    def back(self) -> None:
        if self.history:
            self.future.append(self.current)
            self.open(*self.history.pop(), remember=False)

    def forward(self) -> None:
        if self.future:
            self.history.append(self.current)
            self.open(*self.future.pop(), remember=False)

    def _link(self, url: QUrl) -> None:
        target = url.toString()
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return
        slug, section = M.split_target(target, self.current[0])
        self.open(slug, section)

    def _theme_changed(self, *_):
        slug, section = self.current
        self.current = ("", "")
        self.open(slug, section, self._words(), remember=False)

    # -- search ----------------------------------------------------------------

    def _focus_search(self) -> None:
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _words(self) -> list[str]:
        return self.search_box.text().split()

    def _search(self, text: str) -> None:
        self.results.clear()
        if not text.strip():
            self.stack.setCurrentWidget(self.tree)
            self.view.setExtraSelections([])
            return
        self.stack.setCurrentWidget(self.results)
        hits = M.search(text)
        for hit in hits:
            where = hit.chapter.title if hit.section.level == 1 else f"{hit.chapter.title}  ›  {hit.section.title}"
            item = QListWidgetItem(f"{where}\n{hit.snippet}")
            item.setData(Qt.UserRole, (hit.chapter.slug, "" if hit.section.level == 1 else hit.section.anchor))
            self.results.addItem(item)
        if not hits:
            item = QListWidgetItem("Nothing found. Try fewer or shorter words.")
            item.setFlags(Qt.NoItemFlags)
            self.results.addItem(item)

    def _result_clicked(self, item) -> None:
        target = item.data(Qt.UserRole)
        if target:
            self.open(target[0], target[1], self._words())

    def _first_result(self) -> None:
        if self.results.count() and self.results.item(0).data(Qt.UserRole):
            self.results.setCurrentRow(0)
            self._result_clicked(self.results.item(0))

    def closeEvent(self, event):
        QSettings().setValue("manual/geometry", self.saveGeometry())
        super().closeEvent(event)

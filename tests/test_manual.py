"""The user manual: its chapters are consistent, and the window finds and shows them."""
import pytest

from gcws import manual as M


def test_every_chapter_file_is_in_the_contents():
    listed = [slug for _, slugs in M.PARTS for slug in slugs]
    assert len(listed) == len(set(listed))
    assert sorted(listed) == sorted(p.stem for p in M.DIR.glob("*.md"))
    for ch in M.chapters():
        assert ch.sections and ch.sections[0].level == 1, ch.slug       # starts with its title


def test_placeholders_name_real_settings():
    for ch in M.chapters():
        for name, attr in M.placeholders(ch.source):
            assert M.value(name, attr) != "", f"{ch.slug}: {name}.{attr}"
        assert "{{" not in ch.markdown


def test_defaults_come_from_the_code():
    from gcws.features.model import Settings
    assert M.value("Features", "rt_tol") == f"{Settings().rt_tol:g}"
    assert M.value("IntegrationMethod", "peak_width") == "automatic"
    assert M.value("Features", "gap_fill") == "on"
    assert M.expand("x {{Features.rt_tol}} y") == f"x {Settings().rt_tol:g} y"
    with pytest.raises(KeyError):
        M.value("Features", "no_such_setting")


def test_links_and_pictures_resolve():
    for ch in M.chapters():
        for picture, target in M.links(ch.source):
            if target.startswith(("http://", "https://")):
                continue
            if picture:
                assert (M.DIR / target).is_file(), f"{ch.slug}: picture {target}"
                continue
            slug, section = M.split_target(target, ch.slug)
            other = M.chapter(slug)
            assert other is not None, f"{ch.slug}: link to {target}"
            assert not section or other.section(section) is not None, f"{ch.slug}: link to {target}"


def test_search_finds_sections():
    hits = M.search("default")
    assert hits and all("default" in (h.section.title + h.section.text).lower() for h in hits)
    assert M.search("zzzqqq") == [] and M.search("   ") == []
    assert M.anchor("Gap filling") == "gap-filling"


# -- the window ----------------------------------------------------------------------------------

pytest.importorskip("pytestqt")


@pytest.fixture
def window(qtbot, tmp_path):
    from PySide6.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    from gcws.ui import theme
    theme.ensure_applied()
    from gcws.ui.dialogs.manual import ManualWindow
    w = ManualWindow()
    qtbot.addWidget(w)
    w.show()
    yield w
    theme.set_theme("light")
    w.close()


def test_window_lists_and_shows_every_chapter(window):
    shown = [window.tree.topLevelItem(i).child(j).text(0) for i in range(window.tree.topLevelItemCount())
             for j in range(window.tree.topLevelItem(i).childCount())]
    assert shown == [c.title for c in M.chapters()]
    for ch in M.chapters():
        assert window.open(ch.slug)
        assert window.view.toPlainText().strip().startswith(ch.title)
        for sec in ch.sections:                                  # every heading can be jumped to
            assert window.view.heading_block(sec.anchor) is not None, f"{ch.slug}#{sec.anchor}"
    assert not window.open("no-such-chapter")


def test_window_search_back_and_theme(window, qtbot):
    from PySide6.QtCore import Qt
    first = M.chapters()[0]
    window.search_box.setText("default")
    assert window.stack.currentWidget() is window.results and window.results.count() >= 1
    window._first_result()
    assert window.view.extraSelections()                          # the word is marked in the text
    window.search_box.setText("zzzqqq")
    assert window.results.count() == 1 and window.results.item(0).data(Qt.UserRole) is None
    window.search_box.clear()
    assert window.stack.currentWidget() is window.tree and not window.view.extraSelections()

    sections = [s for s in first.sections if s.level == 2]
    window.open(first.slug, sections[0].anchor)
    window.open(first.slug, sections[-1].anchor)
    assert window.back_btn.isEnabled()
    window.back()
    assert window.current == (first.slug, sections[0].anchor) and window.forward_btn.isEnabled()
    window.forward()
    assert window.current == (first.slug, sections[-1].anchor)

    from gcws.ui import theme
    theme.set_theme("dark")                                       # re-rendered in the new colours
    assert window.current == (first.slug, sections[-1].anchor)
    assert window.view.toPlainText().strip().startswith(first.title)


def test_pictures_follow_the_theme():
    from gcws.ui import theme
    from gcws.ui.dialogs.manual import themed_svg
    svg = f'<rect fill="{theme.LIGHT["ACCENT"]}" stroke="#123456"/>'
    try:
        theme.set_theme("dark")
        assert themed_svg(svg) == f'<rect fill="{theme.DARK["ACCENT"]}" stroke="#123456"/>'
        theme.set_theme("light")
        assert themed_svg(svg) == svg
    finally:
        theme.set_theme("light")


def test_help_menu_opens_the_manual(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtGui import QKeySequence
    from PySide6.QtWidgets import QMessageBox
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    QSettings().clear()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    from gcws.ui.main_window import MainWindow
    win = MainWindow()
    qtbot.addWidget(win)
    try:
        assert win.a_manual.shortcut() == QKeySequence(QKeySequence.HelpContents)       # F1
        win.a_manual.trigger()
        assert win.manual.isVisible() and win.manual.current[0] == M.chapters()[0].slug
        first = win.manual
        win.a_manual.trigger()
        assert win.manual is first                                # one window, raised again
    finally:
        win.manual.close()
        win.ws.dirty = False
        win.close()

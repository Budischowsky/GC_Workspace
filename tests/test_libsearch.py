"""Built-in library search: the analyst's libraries, no EI Atlas."""
import pytest

from gcws import paths
from gcws.identify import library_edit as LE


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path)
    from gcws.libsearch import service
    service.reset()
    yield tmp_path
    service.reset()


RECORDS = [("Toluene", [(91, 999), (92, 600), (65, 120), (39, 80)]),
           ("o-Xylene", [(91, 999), (106, 700), (105, 300), (77, 120)]),
           ("Hexane", [(57, 999), (43, 700), (41, 500), (86, 150), (29, 300)])]


def _msp(path, records=RECORDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(LE.write_msp([LE.new_record(n, p) for n, p in records]), encoding="cp1252", newline="")
    return path


def test_discover_kinds(data):
    from gcws.libsearch import store
    lib = data / "libs"
    _msp(lib / "Own.msp")
    (lib / "NIST17.L").mkdir(parents=True)
    (lib / "NIST17.L" / "HEADER.IND").write_bytes(b"")
    (lib / "mainlib").mkdir()
    (lib / "mainlib" / "nist.db").write_bytes(b"")
    (lib / "Software" / "demo.L").mkdir(parents=True)             # vendor program folders are skipped
    (lib / "Software" / "demo.L" / "HEADER.IND").write_bytes(b"")
    found = {(s.name, s.kind) for s in store.discover(lib)}
    assert found == {("Own", "msp"), ("NIST17.L", "agilent"), ("mainlib", "nist")}
    # a file inside a library folder stands for the folder
    one = store.discover(lib / "NIST17.L" / "HEADER.IND")
    assert [(s.kind, s.path) for s in one] == [("agilent", str(lib / "NIST17.L"))]
    libs = store.add([], store.discover(lib))
    assert len(store.add(libs, store.discover(lib))) == 3        # nothing twice
    store.save(libs)
    assert [s.name for s in store.load()] == [s.name for s in libs]


def test_search_own_msp_library(data):
    from gcws.libsearch import service, store
    store.save(store.discover(_msp(data / "Own.msp")))
    st = service.status()
    assert st["libraries"][0]["name"] == "Own" and st["libraries"][0]["count"] == 3
    for algorithm in ("pbm", "similarity"):
        r = service.analyze([(91, 1000), (92, 580), (65, 110), (39, 90)], "q",
                            {"algorithm": algorithm, "lite": True, "libraries": ["Own"]})
        assert r["hits"][0]["name"] == "Toluene" and r["hits"][0]["score"] > 90, algorithm
        assert r["hits"][0]["library"] == "Own"
    # a changed file is read again
    _msp(data / "Own.msp", RECORDS + [("Styrene", [(104, 999), (103, 450), (78, 350)])])
    assert service.status()["count"] == 4


def test_old_method_names_are_kept(data):
    import gc_search_method as SM
    from gcws.identify.service import adapt_library_names
    m = SM.SearchMethod(libraries=[SM.LibraryEntry(r"Library\NIST17.L", True), SM.LibraryEntry(r"Library\Own.msp", False),
                                   SM.LibraryEntry(r"Library\gone.L", True)])
    names = ["NIST17.L", "Own", "Wiley7N"]
    adapt_library_names(m, names)
    SM.reconcile(m, {"libraries": [{"name": n, "count": 5} for n in names]})
    assert [(e.name, e.enabled) for e in m.libraries] == [("NIST17.L", True), ("Own", False), ("Wiley7N", False)]


def test_batch_search_needs_no_ei_atlas(data, monkeypatch):
    import gc_atlas
    import gc_identify as GI
    import gc_search_method as SM
    from gcws.identify.service import LocalBatchSearch
    from gcws.libsearch import store
    store.save(store.discover(_msp(data / "Own.msp")))

    def no_atlas(*a, **k):
        raise AssertionError("EI Atlas must not be started")
    monkeypatch.setattr(gc_atlas, "ensure_server", no_atlas)
    monkeypatch.setattr(gc_atlas, "request", no_atlas)
    jobs = [GI.PeakJob(label="s", row_id=i, peak_no=i, rt=5.0 + i, before=("", "", None), spectrum=sp)
            for i, sp in enumerate(([(91, 1000), (92, 600), (65, 120)], [(57, 999), (43, 690), (41, 480), (86, 140)]))]
    method = SM.SearchMethod(name="t", algorithm="similarity")
    b = LocalBatchSearch(jobs, method).start()
    b.thread.join(30)
    msgs = []
    while not b.messages.empty():
        msgs.append(b.messages.get())
    assert msgs[-1] == ("done", False) and not any(k == "error" for k, _ in msgs)
    assert [j.hits[0]["name"] for j in jobs] == ["Toluene", "Hexane"]
    assert method.enabled_libraries() == ["Own"]


def test_no_library_gives_a_clear_message(data):
    import gc_search_method as SM
    from gcws.identify.service import prepare_local
    with pytest.raises(RuntimeError, match="Libraries"):
        prepare_local(SM.SearchMethod())


def test_library_manager_dialog(qtbot, data):
    from PySide6.QtCore import Qt
    from gcws.libsearch import store
    from gcws.ui.dialogs.libraries import C_COUNT, C_STATUS, LibraryManagerDialog
    _msp(data / "libs" / "Own.msp")
    dlg = LibraryManagerDialog()
    qtbot.addWidget(dlg)
    assert dlg.table.rowCount() == 0 and "No library" in dlg.state.text()
    dlg._add(store.discover(data / "libs"), "libs")
    assert dlg.table.rowCount() == 1 and [s.name for s in store.load()] == ["Own"]
    dlg.check()
    qtbot.waitUntil(lambda: dlg.load_btn.isEnabled() and dlg.table.item(0, C_COUNT).text() == "3", timeout=20000)
    assert dlg.table.item(0, C_STATUS).text() == "Ready"
    dlg.table.item(0, 0).setCheckState(Qt.Unchecked)         # switched off: not searched, still listed
    assert store.load()[0].enabled is False
    dlg.table.selectRow(0)
    dlg.remove()
    assert store.load() == []


@pytest.fixture
def settings(tmp_path):
    from PySide6.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest-libsearch")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "ini"))
    QSettings().clear()
    yield
    QSettings().clear()


def test_own_library_search(qtbot, data, settings):
    from gcws.libsearch import store
    from gcws.ui.dialogs import own_search as OS
    from gcws.ui.dialogs.identify import AtlasHitsDialog
    libs = store.add([], store.discover(_msp(data / "Own.msp")))
    libs = store.add(libs, store.discover(_msp(data / "Other.msp", [("Toluene-d8", [(98, 999), (100, 600)])])))
    store.save(libs)
    assert OS.libraries() == ["Own", "Other"]
    dlg = OS.OwnSearchOptionsDialog()
    qtbot.addWidget(dlg)
    dlg.library.setCurrentText("Own")
    dlg.top_n.setValue(2)
    dlg.min_score.setValue(30)
    dlg._ok()
    opts = OS.load_options()
    assert opts["library"] == "Own" and opts["top_n"] == 2 and opts["min_score"] == 30 and opts["dedupe"] is True
    m = OS.method_from_options(opts)
    assert m.enabled_libraries() == ["Own"] and m.algorithm == "similarity"
    hits = AtlasHitsDialog([(91, 1000), (92, 590), (65, 115)], "q", m)
    qtbot.addWidget(hits)
    qtbot.waitUntil(lambda: hits.table.rowCount() > 0, timeout=20000)
    assert hits.hits_data[0]["name"] == "Toluene" and all(h["library"] == "Own" for h in hits.hits_data)
    assert len(hits.hits_data) <= 2


def test_own_library_first_use_asks_for_the_library(qtbot, data, settings, monkeypatch):
    """No own library chosen yet: the options dialog opens; OK searches, Cancel does nothing (no error)."""
    from PySide6.QtWidgets import QDialog, QWidget
    from gcws.libsearch import store
    from gcws.ui.dialogs import own_search as OS
    from gcws.ui.main_window import MainWindow
    store.save(store.add([], store.discover(_msp(data / "Own.msp"))))

    class Win(QWidget):
        def __init__(self):
            super().__init__()
            self.methods = []

        def atlas_hits(self, points, name, method=None):
            self.methods.append(method)

    win = Win()
    qtbot.addWidget(win)
    spectrum = [(91, 1000), (92, 590)]
    with monkeypatch.context() as mp:
        mp.setattr(OS.OwnSearchOptionsDialog, "exec", lambda self: QDialog.Rejected)
        MainWindow.own_library_search(win, spectrum, "q")
    assert win.methods == [] and OS.load_options()["library"] == ""
    with monkeypatch.context() as mp:
        mp.setattr(OS.OwnSearchOptionsDialog, "exec", lambda self: (self._ok(), QDialog.Accepted)[1])
        MainWindow.own_library_search(win, spectrum, "q")
    assert len(win.methods) == 1 and win.methods[0].enabled_libraries() == ["Own"]
    MainWindow.own_library_search(win, spectrum, "q")          # chosen now: no dialog any more
    assert len(win.methods) == 2

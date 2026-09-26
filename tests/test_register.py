"""Unknown register: find by text / sample / m/z, mark, share as MSP, look up."""
import pytest

pytest.importorskip("pytestqt")


def _item(sample, name, rt, spectrum, **kw):
    return dict(sample=sample, sample_name=name, report_type="test", rt=rt, spectrum=spectrum,
                source_file=sample, date="2026-09-26", **kw)


@pytest.fixture
def register(tmp_path, monkeypatch):
    import gc_export
    from gcws.ui.dialogs import register as REG
    db = tmp_path / "unknown_register.sqlite"
    gc_export.write_unknowns(db, [
        _item("26016605_A.D", "GIOSUN A", 18.3, [(149, 999), (167, 300), (279, 80), (57, 60), (41, 40)],
              assigned_name="Dibutyl phthalate", assigned_cas="84-74-2"),
        _item("26016605_B.D", "GIOSUN B", 18.31, [(149, 999), (167, 310), (279, 75), (57, 55), (41, 45)]),
        _item("27000001_A.D", "Other sample", 9.2, [(91, 999), (92, 600), (65, 120), (39, 80), (51, 60)]),
        _item("27000001_A.D", "Other sample", 14.8, [(57, 999), (71, 700), (85, 450), (43, 800), (99, 120)],
              assigned_cas="not a cas"),
    ])
    monkeypatch.setattr(REG, "db_path", lambda: db)
    return db


def test_search_modes(register):
    import gc_register as R
    from gcws.identify import register_search as RS
    con = R.connect(register)
    try:
        assert RS.parse_mz("149, 167 279.2") == [149, 167, 279]
        names = lambda rows: sorted(r["label"] for r in rows)
        assert len(RS.search(con, "text", "")) == 3                       # the two phthalates are one entry
        assert len(RS.search(con, "text", "dibutyl")) == 1
        assert len(RS.search(con, "sample", "GIOSUN")) == 1
        assert len(RS.search(con, "sample", "other")) == 2
        assert len(RS.search(con, "mz", "149 167")) == 1
        assert len(RS.search(con, "mz", "57")) == 2                        # 57 at >= 5 % in two entries
        assert len(RS.search(con, "mz", "57", min_rel=50)) == 1
        assert len(RS.search(con, "mz", "57 71", base_first=True)) == 1
        assert len(RS.search(con, "mz", "71", base_first=True)) == 0
        assert names(RS.search(con, "mz", "91 92 65")) == ["unknown (m/z 91/92/65/39)"]
        # an entry filed without a spectrum (older registers) is found by its significant ions
        assert RS.ranked_matches("267/43/268", [43, 267]) and not RS.ranked_matches("267/43", [43], base_first=True)
    finally:
        con.close()


def test_register_window_marks_and_exports(qtbot, register, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from gcws.identify import library_edit as LE
    from gcws.ui.dialogs.register import C_MARK, RegisterWindow
    w = RegisterWindow()
    qtbot.addWidget(w)
    assert w.table.rowCount() == 3 and not w.min_rel.isVisible()
    w.mode.setCurrentIndex(w.mode.findData("mz"))
    w.search.setText("57")
    w.reload()
    assert w.table.rowCount() == 2
    w.set_marks(w.shown_ids(), True)
    w.mode.setCurrentIndex(w.mode.findData("text"))
    w.search.setText("")
    w.reload()
    assert w.table.rowCount() == 3 and len(w.marked) == 2              # marks survive a new search
    assert sum(w.table.item(r, C_MARK).checkState() == Qt.Checked for r in range(3)) == 2
    out = tmp_path / "share.msp"
    assert w.export_msp(str(out)) == 2
    recs = LE.parse_msp(out.read_text(encoding="cp1252"))
    assert len(recs) == 2 and all(r.peaks for r in recs)
    dbp = next(r for r in recs if "Dibutyl" in r.name)
    assert dbp.cas == "84-74-2" and "UNK-" in dbp.name and "GIOSUN" in dbp.get("Comment")
    other = next(r for r in recs if r is not dbp)
    assert "CAS: not a cas" in other.get("Comment")                       # kept as text, not dropped
    # nothing marked: the selection is exported
    w.set_marks(list(w.marked), False)
    w.table.selectRow(0)
    assert w.export_msp(str(tmp_path / "one.msp")) == 1


def test_register_library_search_and_assign(qtbot, register, tmp_path, monkeypatch):
    from gcws import paths
    from gcws.libsearch import service, store
    from gcws.identify import library_edit as LE
    from gcws.ui.dialogs.register import RegisterWindow
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    service.reset()
    lib = tmp_path / "data" / "Own.msp"
    lib.parent.mkdir(parents=True)
    lib.write_text(LE.write_msp([LE.new_record("Toluene", [(91, 999), (92, 600), (65, 120), (39, 80)])]),
                   encoding="cp1252", newline="")
    store.save(store.discover(lib))
    w = RegisterWindow()
    qtbot.addWidget(w)
    w.search.setText("91/92")
    w.reload()
    assert w.table.rowCount() == 1
    w.table.setCurrentCell(0, 1)
    dlg = w.library_search()
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: dlg.table.rowCount() > 0, timeout=20000)
    assert dlg.hits_data[0]["name"] == "Toluene"
    dlg.table.setCurrentCell(0, 0)
    dlg._assign()
    row = w.R.entry_row(w.con, w.shown_ids()[0])
    assert row["assigned_name"] == "Toluene" and row["status"] == "in Arbeit" and "library hit" in row["note"]
    service.reset()

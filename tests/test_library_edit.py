"""Edit library: MSP entries and changes of a NIST user library through Lib2NIST.

The real libraries are never touched: the Lib2NIST tests work on a copy in tmp_path.
"""
import shutil
from pathlib import Path

import pytest

from gcws.identify import library_edit as LE

SAMPLE_MSP = (
    "Name: Benzyl-butyl-phthalate-d4\r\nMW: 210\r\nCASNO: 0\r\nID: 1\r\nNum peaks: 3\r\n"
    "  91  608; 149  999; 153  120;\r\n\r\n"
    "Name: Irgafos 168\r\nFormula: C42H63O3P\r\nMW: 646\r\nCASNO: 31570044\r\nID: 2\r\nRI:3200\r\n"
    "Comment: from a standard |RI:3200|\r\nNum peaks: 2\r\n 441  999; 646  120;\r\n\r\n")


def test_msp_round_trip_is_exact():
    recs = LE.parse_msp(SAMPLE_MSP)
    assert [r.name for r in recs] == ["Benzyl-butyl-phthalate-d4", "Irgafos 168"]
    assert recs[1].cas == "31570-04-4" and recs[0].cas == "" and recs[1].ri == 3200
    assert recs[1].peaks == [(441.0, 999.0), (646.0, 120.0)]
    again = LE.parse_msp(LE.write_msp(recs))
    assert [(r.fields, r.peaks) for r in again] == [(r.fields, r.peaks) for r in recs]


def test_new_record_fields_and_validation():
    r = LE.new_record("α-Tocopherol", [(430, 999), (165, 400.5)], cas="59-02-9", formula="C29 H50 O2", mw=430,
                      ri=3125.4, rt=28.9, synonyms=["Vitamin E"], comment="Standard 2026", source="Run 07")
    text = r.to_text()
    assert "Name: .alpha.-Tocopherol" in text and "Synon: Vitamin E" in text and "CAS#: 59-02-9" in text
    assert "Formula: C29H50O2" in text and "|RI:3125|" in text and "RT=28.900 min" in text
    assert "Num Peaks: 2" in text and "165 400.5;" in text
    with pytest.raises(ValueError):
        LE.new_record("", [(1, 2)])
    with pytest.raises(ValueError):
        LE.new_record("x", [(1, 2)], cas="12")
    assert LE.format_cas("117817") == "117-81-7" and LE.format_cas("0") == ""


def test_msp_library_add_edit_delete(tmp_path):
    info = LE.LibraryInfo("own", tmp_path / "libraries" / "gcws" / "own.msp", "msp", True, root=tmp_path)
    info.path.parent.mkdir(parents=True)
    ed = LE.LibraryEditor(info, None, tmp_path / "backups")
    ed.add(LE.new_record("First", [(57, 999), (71, 500)]))
    ed.add(LE.new_record("Second", [(149, 999)]))
    assert [r.name for r in ed.read()] == ["First", "Second"]
    rec = ed.read()[0]
    rec.set("Name", "First, corrected")
    ed.replace(0, rec)
    ed.delete(1)
    assert [r.name for r in ed.read()] == ["First, corrected"]
    assert any((tmp_path / "backups").iterdir())
    libs = LE.list_libraries(tmp_path)
    assert [(lb.name, lb.kind, lb.writable) for lb in libs] == [("own", "msp", True)]


def _atlas_copy(tmp_path):
    root = LE.atlas_root()
    exe = LE.find_lib2nist(root) if root else None
    src = root / "Library" / "CCAlu_GCMS" if root else None
    if exe is None or src is None or not src.is_dir():
        pytest.skip("EI Atlas with CCAlu_GCMS and NIST Lib2NIST not available")
    fake = tmp_path / "atlas"
    shutil.copytree(src, fake / "Library" / "CCAlu_GCMS")
    agilent = fake / "Library" / "CCALU_GCMS_1.L"
    agilent.mkdir()
    (agilent / "HEADER.IND").write_bytes(b"")
    return fake, LE.Lib2Nist(exe)


def test_nist_user_library_add_edit_delete(tmp_path):
    root, l2n = _atlas_copy(tmp_path)
    libs = LE.list_libraries(root)
    assert {(lb.name, lb.kind, lb.writable) for lb in libs} == {("CCAlu_GCMS", "nist", True),
                                                                ("CCALU_GCMS_1.L", "agilent", False)}
    info = LE.default_library(libs)
    assert info.name == "CCAlu_GCMS" and info.label == str(Path("Library") / "CCAlu_GCMS")
    ed = LE.LibraryEditor(info, l2n, tmp_path / "backups")
    before = ed.read()
    n = len(before)
    assert n > 100
    res = ed.add(LE.new_record("GCWS test entry", [(149, 999), (167, 300), (279, 120)], cas="117-81-7",
                               formula="C24H38O4", mw=390, ri=2530, rt=24.51, source="test"))
    assert res.count == n + 1 and res.backup is not None and res.backup.is_dir()
    after = ed.read()
    new = next(r for r in after if r.name == "GCWS test entry")
    assert new.cas == "117-81-7" and new.ri == 2530 and new.peaks == [(149, 999), (167, 300), (279, 120)]
    # everything else is unchanged
    old = {(r.name, tuple(r.peaks)) for r in before}
    assert old <= {(r.name, tuple(r.peaks)) for r in after}
    k = next(i for i, r in enumerate(after) if r.name == "GCWS test entry")
    ed.delete(k)                                  # (changing an entry uses the same checked save)
    final = ed.read()
    assert len(final) == n and {(r.name, tuple(r.peaks)) for r in final} == old
    with pytest.raises(LE.LibraryError):
        LE.LibraryEditor(libs[1] if not libs[1].writable else libs[0], l2n).save([])


def test_failed_check_leaves_the_library_untouched(tmp_path, monkeypatch):
    root, l2n = _atlas_copy(tmp_path)
    info = LE.default_library(LE.list_libraries(root))
    lib = info.path
    stamp = {p.name: p.stat().st_mtime_ns for p in lib.iterdir()}
    ed = LE.LibraryEditor(info, l2n, tmp_path / "backups")
    records = ed.read()
    real_export = l2n.export
    monkeypatch.setattr(l2n, "export", lambda library, msp: real_export(library, msp)[:-1])   # a "lost" entry
    with pytest.raises(LE.LibraryError, match="not changed"):
        ed.save(records + [LE.new_record("x", [(57, 999)])])
    assert {p.name: p.stat().st_mtime_ns for p in lib.iterdir()} == stamp

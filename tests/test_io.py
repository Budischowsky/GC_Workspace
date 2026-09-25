import numpy as np
import pytest

from gcws.io import folders, masshunter, sequence
from gcws.io.ms_matrix import MSMatrix, nominal
from gcws.io.run_loader import load_run


def _runs(samples):
    return folders.list_runs(samples)


def test_run_detection(samples):
    names = [p.name for p in _runs(samples)]
    assert any(n.startswith("07_") for n in names)
    assert len(names) >= 8
    assert folders.is_analysis_folder(samples)
    assert not folders.is_analysis_folder(_runs(samples)[0])


def test_cg_equals_ch_for_all_runs(samples):
    import gc_ch
    checked = 0
    for d in _runs(samples):
        cg = d / "AcqData" / "FID1.cg"
        ch = d / "FID1A.ch"
        if not (cg.exists() and ch.exists()):
            continue
        a = masshunter.read_cg(cg)
        b = gc_ch.read_ch(ch)
        assert a.y.size == b.y.size
        assert np.array_equal(a.y, b.y)
        assert np.allclose(a.rt, b.rt, atol=1e-9)
        checked += 1
    assert checked >= 1


@pytest.mark.parametrize("prefix", ["06_", "07_", "08_"])
def test_mspeak_equals_datams(samples, prefix):
    import extract_ms_spectra as ex
    from conftest import run_dir
    d = run_dir(prefix)
    a = masshunter.MSPeakSource(d / "AcqData")
    b = ex.DataMS(d / "data.ms")
    assert a.n_scans == b.n_scans
    assert np.allclose(a.rt, b.rt, atol=1e-6)
    assert np.allclose(a.tic, b.tic)
    ma, mb = MSMatrix.from_source(a), MSMatrix.from_source(b)
    assert np.array_equal(ma.ptr, mb.ptr)
    assert np.allclose(ma.mz, mb.mz, atol=0.01)
    assert np.allclose(ma.ab, mb.ab)
    for i in (0, a.n_scans // 2, a.n_scans - 1):
        sa, sb = a.spectrum(i), b.spectrum(i)
        assert len(sa) == len(sb)
        assert all(abs(x[0] - y[0]) < 0.01 and x[1] == y[1] for x, y in zip(sa, sb))


def test_matrix_tic_and_eic(run07):
    ms = run07.ms
    assert np.allclose(ms.tic(), ms.stored_tic, rtol=0.01)
    eic = ms.eic([149])
    i = int(np.argmax(eic))
    mz, ab = ms.scan(i)
    brute = ab[nominal(mz) == 149].sum()
    assert eic[i] == pytest.approx(brute)


def test_nominal_matches_legacy():
    import extract_ms_spectra as ex
    for v in (57.4, 57.5, 57.6, 647.5, 648.5, 206.49):
        assert nominal(np.array([v]))[0] == ex._nominal(v)


def test_load_run_metadata(run07):
    assert run07.name == "07_26016606_130m_min_GIOSUN1635_A"
    assert run07.fid is not None and run07.fid.n > 40000
    assert run07.ms is not None and run07.ms.n_scans > 3000
    assert run07.role == sequence.SAMPLE
    assert run07.meta.method.upper().startswith("NIAS-SCREENING")


def test_load_run_masshunter_only(samples, tmp_path):
    import shutil
    from conftest import run_dir
    src = run_dir("08_")
    dst = tmp_path / src.name
    shutil.copytree(src / "AcqData", dst / "AcqData")
    run = load_run(dst)
    ref = load_run(src)
    assert run.fid.source.startswith("AcqData/")
    assert np.array_equal(run.fid.y, ref.fid.y)
    assert np.allclose(run.signal("TIC").y, ref.signal("TIC").y)


def test_roles_and_suggestions(samples):
    runs = sequence.run_order(_runs(samples))
    ids = {p.name[:2]: p for p in runs}
    roles = {k: sequence.classify_role(p.name) for k, p in ids.items()}
    assert roles["06"] == sequence.BLANK_ISTD
    assert roles["08"] == sequence.BLANK
    for k in ("07", "09", "11", "12"):
        assert roles[k] == sequence.SAMPLE
    order = [p.name[:2] for p in runs]
    blanks, istd = sequence.suggest_blanks("07", ids, roles, order)
    assert blanks == ["08"] and istd == ["06"]
    groups = sequence.suggest_replicates({k: p.name for k, p in ids.items()})
    assert sorted(sorted(g) for g in groups) == [["07", "11"], ["09", "12"]]

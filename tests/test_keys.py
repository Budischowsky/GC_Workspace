"""Signal keys, derived keys and the vectorised MS matrix."""
import numpy as np
import pytest

from gcws.core import keys
from gcws.core.model import Run, Signal, parse_key


@pytest.mark.parametrize("key, base, derived", [
    ("FID", "FID", None),
    ("FID - Blank", "FID", keys.BLANK_SUFFIX),
    ("TIC - blank", "TIC", keys.BLANK_SUFFIX),
    ("EIC 149+57 - Blank", "EIC 149+57", keys.BLANK_SUFFIX),
])
def test_split_key(key, base, derived):
    assert keys.split_key(key) == (base, derived)
    assert keys.base_key(key) == base
    assert keys.is_derived(key) == (derived is not None)


def test_kind_of_derived_keys():
    assert parse_key("EIC 149+57 - Blank") == ("EIC", (149, 57))
    assert parse_key("FID - Blank") == ("FID", ())
    assert keys.is_fid("FID - Blank") and not keys.is_fid("TIC - Blank")
    assert keys.method_kind("FID - Blank") == "FID"
    assert keys.method_kind("EIC 149 - Blank") == "TIC"
    assert keys.derived_key("TIC") == "TIC - Blank"
    assert keys.derived_key("TIC - Blank") == "TIC - Blank"


def test_derived_signal_needs_provider(tmp_path):
    fid = Signal("FID", np.linspace(0, 1, 11), np.arange(11.0))
    run = Run(path=tmp_path, meta=None, fid=fid)
    assert run.signal("FID - Blank") is None            # never the raw FID by accident
    calls = []

    def provider(r, key):
        calls.append(key)
        return Signal(key, fid.rt, fid.y - 1)

    run.derive = provider
    sig = run.signal("FID - Blank")
    assert sig is not None and sig.key == "FID - Blank" and sig.kind == "FID"
    run.signal("FID - Blank")
    assert calls == ["FID - Blank"]                     # cached
    run.drop_derived()
    run.signal("FID - Blank")
    assert calls == ["FID - Blank", "FID - Blank"]
    assert run.signal("FID") is fid


def _matrix(seed=1, n_scans=40):
    from gcws.io.ms_matrix import MSMatrix
    rng = np.random.default_rng(seed)
    mzs, abs_ = [], []
    for _ in range(n_scans):
        k = int(rng.integers(0, 25))
        mz = np.sort(rng.uniform(35, 120, k).round(1))
        mzs.append(mz)
        abs_.append(rng.uniform(100, 5000, k))
    rt = np.linspace(5, 6, n_scans)
    return MSMatrix._build(rt, np.zeros(n_scans), mzs, abs_)


def _loop_spectrum(ms, scans, weights=None):
    weights = np.ones(len(scans)) if weights is None else np.asarray(weights, float)
    total = {}
    for s, w in zip(scans, weights):
        a, b = ms.ptr[s], ms.ptr[s + 1]
        for m, v in zip(ms.nom[a:b].tolist(), (ms.ab[a:b] * w).tolist()):
            total[m] = total.get(m, 0.0) + v
    return {m: v / weights.sum() for m, v in total.items()}


def test_vectorised_spectra_equal_loop():
    ms = _matrix()
    for scans, w in (([3], None), ([0, 5, 6, 7], None), ([10, 11, 12], [1, 2, 3]), (list(range(40)), None)):
        got = ms.nominal_spectrum(scans, w)
        ref = _loop_spectrum(ms, scans, w)
        assert got.keys() == ref.keys()
        for m in ref:
            assert got[m] == pytest.approx(ref[m])
    assert ms.nominal_spectrum([]) == {}


def test_dense_block_equals_spectra():
    ms = _matrix(seed=3)
    lo, hi = ms.mass_range()
    block = ms.dense_block(4, 9, lo, hi)
    assert block.shape == (6, hi - lo + 1)
    for r, s in enumerate(range(4, 10)):
        ref = _loop_spectrum(ms, [s])
        row = {lo + i: v for i, v in enumerate(block[r]) if v}
        assert row.keys() == ref.keys()
        for m in ref:
            assert row[m] == pytest.approx(ref[m])
    dense, lo2 = ms.dense()
    assert lo2 == lo and dense.shape == (ms.n_scans, hi - lo + 1)
    assert np.allclose(dense[4:10], block, rtol=1e-6)
    assert ms.min_abundance() > 0


def test_similarity():
    from gcws.ms.similarity import cosine, match_factor
    a = {41: 100.0, 43: 50.0, 57: 999.0}
    assert cosine(a, a) == pytest.approx(1.0)
    assert match_factor(a, dict(a)) == pytest.approx(999.0)
    assert cosine(a, {91: 999.0}) == 0.0
    assert 0 < cosine(a, {57: 999.0, 71: 300.0}) < 1
    assert cosine(a, None) == 0.0

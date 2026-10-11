"""Resumable library norms: bit for bit what the vendored engine computes, from fewer passes."""
import functools

import numpy as np
import pytest

from tests.test_fast_search import _clean, data, libraries  # noqa: F401  (fixtures)

RANGES = [(41, 250), (41, 300), (41, 120), (39, 300), (41, 300), (35, 400), (60, 61), (1, 10000)]


def _vendor():
    import gcws.libsearch  # noqa: F401  (vendor on sys.path)
    import engine
    return engine.Engine.shard_norms.__wrapped__


def test_folds_equal_vendor_norms_bitwise(libraries):
    from gcws.libsearch import service
    eng = service.get_engine()
    vendor = _vendor()
    assert len(eng.shards) == 2
    for i in range(len(eng.shards)):
        for lo, hi in RANGES:
            got = eng.shard_norms(i, lo, hi)
            assert got.dtype == np.float64
            assert np.array_equal(got, vendor(eng, i, lo, hi)), (i, lo, hi)
    assert eng._folds.snapshots                    # the engine's own norms come from the folds


def test_folds_resume_from_the_nearest_snapshot(libraries, monkeypatch):
    from gcws.libsearch import norms, service
    shard = service.get_engine().shards[0]
    f = norms.NormFolds()
    f.get(shard, 0, 41, 200)
    folded = []
    original = norms._fold

    def spy(out, shard_, first, maximum):
        folded.extend(range(first, maximum + 1))
        return original(out, shard_, first, maximum)
    monkeypatch.setattr(norms, "_fold", spy)
    f.get(shard, 0, 41, 260)
    assert folded == list(range(201, 261))


def test_folds_are_bounded(libraries):
    from gcws.libsearch import norms, service
    eng = service.get_engine()
    vendor = _vendor()
    f = norms.NormFolds(limit=3)
    for lo, hi in RANGES[:6]:
        assert np.array_equal(f.get(eng.shards[1], 1, lo, hi), vendor(eng, 1, lo, hi))
    assert len(f.snapshots) == 3


def test_engine_close_and_reset_with_folds(libraries):
    from gcws.libsearch import service
    eng = service.get_engine()
    eng.shard_norms(0, 41, 200)
    service.reset()
    name, points = libraries[0]
    assert service.analyze(points, name, dict(algorithm="pbm", max_hits=5))["hits"]


@pytest.mark.parametrize("settings", [
    dict(algorithm="pbm", mode="combined", max_hits=5, dedupe=True),
    dict(algorithm="pbm", mode="sequential", stop_score=60, max_hits=5, dedupe=True),
    dict(algorithm="similarity", mode="combined", max_hits=5, dedupe=True),
])
def test_analyze_results_unchanged_by_folds(libraries, monkeypatch, settings):
    from gcws.libsearch import service
    with_folds = [_clean(service.analyze(points, name, dict(settings))) for name, points in libraries]
    service.reset()
    vendor_lru = functools.lru_cache(64)(_vendor())
    monkeypatch.setattr(service.LocalEngine, "shard_norms", vendor_lru)
    vendor = [_clean(service.analyze(points, name, dict(settings))) for name, points in libraries]
    assert with_folds == vendor


def test_snapshots_on_disk_resume_in_a_new_process(libraries, tmp_path, monkeypatch):
    """Large libraries: a later process (a new NormFolds) resumes from the stored sums, bit for bit."""
    from gcws.libsearch import norms, service
    from test_libsearch_startup import _mapped
    eng = service.get_engine()
    vendor = _vendor()
    shard = _mapped(eng.shards[1], tmp_path / "index_fp")
    disk = norms.DiskNorms(tmp_path / "norms")
    first = norms.NormFolds(disk=disk)
    for lo, hi in [(41, 200), (41, 300), (39, 250)]:
        assert np.array_equal(first.get(shard, 1, lo, hi), vendor(eng, 1, lo, hi))
    assert sorted(p.name for p in (tmp_path / "norms" / "index_fp").iterdir()) == \
        ["39_250.npy", "41_200.npy", "41_300.npy"]
    folded = []
    original = norms._fold

    def spy(out, shard_, first, maximum):
        folded.extend(range(first, maximum + 1))
        return original(out, shard_, first, maximum)
    monkeypatch.setattr(norms, "_fold", spy)
    second = norms.NormFolds(disk=disk)
    work = {}
    for lo, hi in RANGES:
        folded.clear()
        assert np.array_equal(second.get(shard, 1, lo, hi), vendor(eng, 1, lo, hi)), (lo, hi)
        work.setdefault((lo, hi), list(folded))
    assert work[(41, 250)] == list(range(201, 251))         # resumed from the stored (41, 200)
    assert work[(41, 300)] == []                            # read back
    assert work[(39, 300)] == list(range(251, 301))         # from the stored (39, 250)
    assert work[(41, 120)] == list(range(41, 121))          # nothing below: from zero
    (tmp_path / "norms" / "index_fp" / "39_250.npy").write_bytes(b"broken")
    assert np.array_equal(norms.NormFolds(disk=disk).get(shard, 1, 39, 260), vendor(eng, 1, 39, 260))


def test_disk_snapshots_keep_to_the_budget(libraries, tmp_path):
    from gcws.libsearch import norms, service
    from test_libsearch_startup import _mapped
    eng = service.get_engine()
    shard = _mapped(eng.shards[0], tmp_path / "fp")
    size = 8 * int(shard["count"]) + 128                     # one snapshot file
    disk = norms.DiskNorms(tmp_path / "norms", budget=3 * size)
    folds = norms.NormFolds(disk=disk)
    for hi in range(100, 160, 10):
        folds.get(shard, 0, 41, hi)
    kept = sorted(p.name for p in (tmp_path / "norms" / "fp").iterdir())
    assert kept == ["41_130.npy", "41_140.npy", "41_150.npy"]      # the most recent three
    small = norms.NormFolds(disk=disk)
    assert np.array_equal(small.get(eng.shards[0], 0, 41, 170), _vendor()(eng, 0, 41, 170))
    assert len(list((tmp_path / "norms" / "fp").iterdir())) == 3   # in-memory shards: nothing stored


def _synthetic_shard(rng, count=60000, masses=600, ordered=True):
    """A mass-major index like the built ones: per mass a sorted random subset of rows."""
    pointers, rows, intensities = [0], [], []
    for m in range(masses):
        n = int(rng.integers(0, count // 4)) if m >= 30 and rng.random() > 0.1 else 0
        r = np.sort(rng.choice(count, size=n, replace=False)).astype(np.int32)
        if not ordered and n > 1:
            r[[0, -1]] = r[[-1, 0]]
        rows.append(r)
        intensities.append(rng.random(n, dtype=np.float32) * np.float32(999))
        pointers.append(pointers[-1] + n)
    pointers += [pointers[-1]] * (10002 - len(pointers))
    return dict(count=count, pointers=np.array(pointers, np.int64), rows=np.concatenate(rows),
                intensities=np.concatenate(intensities))


def _numpy_fold(out, shard, first, maximum):
    from gcws.libsearch import norms
    out = out.copy()
    for m in range(first, maximum + 1):
        norms._fold_mass(out, shard, m)
    return out


def test_rust_folds_equal_numpy_bitwise():
    """Many row chunks in parallel (60 000 rows), from zero and from a snapshot, empty masses and
    the last mass of the index: the numpy sums bit for bit."""
    from gcws.libsearch import norms
    if not norms.rust_folds():
        pytest.skip("Rust extension not built (tools/build_rust.ps1) or GCWS_RUST_NORMS=off")
    rng = np.random.default_rng(7)
    shard = _synthetic_shard(rng)
    zero = np.zeros(shard["count"])
    start = _numpy_fold(zero, shard, 35, 90)
    for first, maximum, base in [(35, 90, zero), (91, 400, start), (1, 10000, zero), (0, 29, zero),
                                 (500, 500, zero), (41, 599, start)]:
        got = norms._fold(base.copy(), shard, first, maximum)
        want = _numpy_fold(base, shard, first, maximum)
        assert got.view(np.int64).tolist() == want.view(np.int64).tolist(), (first, maximum)
    assert norms._rust_arrays(shard) is not None


def test_unordered_rows_fold_in_numpy(monkeypatch):
    from gcws.libsearch import norms
    if norms._rust is None:
        pytest.skip("Rust extension not built (tools/build_rust.ps1)")
    shard = _synthetic_shard(np.random.default_rng(3), count=5000, ordered=False)
    assert norms._rust_arrays(shard) is None
    called = []
    monkeypatch.setattr(norms._rust, "fold_norms", lambda *a: called.append(a))
    got = norms._fold(np.zeros(5000), shard, 35, 300)
    assert not called
    assert np.array_equal(got, _numpy_fold(np.zeros(5000), shard, 35, 300))


def test_rust_norms_off_keeps_numpy_and_the_disk_store(libraries, monkeypatch):
    from gcws.libsearch import norms, service
    monkeypatch.setenv("GCWS_RUST_NORMS", "off")
    assert not norms.rust_folds()
    service.reset()
    assert service.get_engine()._folds.disk is not None
    monkeypatch.delenv("GCWS_RUST_NORMS")
    service.reset()
    assert (service.get_engine()._folds.disk is None) == norms.rust_folds()

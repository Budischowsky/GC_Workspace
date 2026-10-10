"""Rust kernels of the fast search (gcws.libsearch.rust_search): the Python results, bit for bit."""
import math
import struct

import numpy as np
import pytest

from gcws import paths
from gcws.identify import library_edit as LE

rust = pytest.importorskip("gcws_rust", reason="Rust extension not built (tools/build_rust.ps1)")

COMMON = [39, 41, 43, 55, 57, 67, 69, 71, 77, 91]


def _spectrum(rng, heavy=False):
    peaks = {m: int(rng.integers(20, 999)) for m in rng.choice(COMMON, size=int(rng.integers(5, 10)), replace=False)}
    for m in rng.choice(np.arange(60, 400 if heavy else 250), size=int(rng.integers(3, 25)), replace=False):
        peaks[int(m)] = int(rng.integers(5, 999))
    peaks[max(peaks, key=peaks.get)] = 999
    return sorted(peaks.items())


@pytest.fixture
def libraries(tmp_path, monkeypatch):
    """Two MSP libraries; 350 copies of one spectrum force ties at the top-300 boundary."""
    monkeypatch.setattr(paths, "DATA", tmp_path)
    from gcws.libsearch import service, store
    service.reset()
    rng = np.random.default_rng(3)
    spectra = []
    for name, count, copies in (("A", 1300, 350), ("B", 1400, 0)):
        records = []
        for n in range(count):
            sp = _spectrum(rng, heavy=n % 3 == 0)
            spectra.append(sp)
            records.append(LE.new_record(f"{name} {n}", sp, cas=f"{1000 + n}-{n % 90 + 10}-{n % 10}"))
        records += [LE.new_record(f"{name} copy {c}", spectra[17], cas="1017-27-7") for c in range(copies)]
        (tmp_path / f"{name}.msp").write_text(LE.write_msp(records), encoding="cp1252", newline="")
    store.save(store.add(store.discover(tmp_path / "A.msp"), store.discover(tmp_path / "B.msp")))
    queries = []
    for n in range(40):
        a = dict(spectra[int(rng.integers(len(spectra)))]) if n % 10 else dict(spectra[17])
        if n % 3 == 0:
            for m, i in spectra[int(rng.integers(len(spectra)))]:
                a[m] = a.get(m, 0) + i * 0.4
        noisy = [(float(m), float(i) * float(rng.uniform(0.7, 1.3))) for m, i in a.items()]
        noisy += [(float(m), float(rng.uniform(1, 30))) for m in rng.choice(np.arange(35, 300), 6, replace=False)]
        queries.append((f"peak {n}", noisy))
    queries.append(("rare ions", [(397.0, 100.0), (399.0, 40.0)]))     # fewer than k references: None
    yield queries
    service.reset()


def _clean(result):
    if isinstance(result, BaseException):
        return ("error", str(result))
    return {k: v for k, v in result.items() if k != "seconds"}


def _jobs(search, queries, ranges=None):
    from gcws.libsearch import fast, service
    shared = search._shared()
    jobs = []
    for n, (name, points) in enumerate(queries):
        job = fast._Job(n, service._msp.Spectrum(name=name, peaks=points))
        if ranges:
            lo, hi = ranges[n % len(ranges)]
            job.settings = {**search.settings, "min_mz": lo, "max_mz": hi}
        search._prepare(job, shared)
        jobs.append(job)
    return shared, jobs


@pytest.mark.parametrize("mode", ["sparse", "tiled", "tiled2"])
@pytest.mark.parametrize("tile,chunk,segment", [(64, 7, 500), (192, 64, 16384), (4096, 3, 1000)])
@pytest.mark.parametrize("mixed", [False, True])
def test_prefilter_selects_the_python_candidates(libraries, monkeypatch, mode, tile, chunk, segment, mixed):
    """Rust stage 1 (no screening bound at all) picks the screen's candidates and tie rows."""
    from gcws.libsearch import fast, rust_search, service
    monkeypatch.setenv("GCWS_RUST_SEARCH", mode)
    rust_search.install()
    monkeypatch.setattr(rust_search, "TILE", tile)
    monkeypatch.setattr(rust_search, "CHUNK", chunk)
    monkeypatch.setattr(rust_search, "SEGMENT", segment)
    eng = service.get_engine()
    search = fast.FastSearch(eng, dict(libraries=["A", "B"], min_mz=35, max_mz=400, threshold=0, lite=True))
    shared, jobs = _jobs(search, libraries, [(35, 400), (41, 300), (60, 380), (35, 400)] if mixed else None)
    k = shared[2]
    for group in (shared[1][0], shared[1][0][:1]):
        want = rust_search._installed["_screen"](search, group, jobs, k, 0, 1)
        got = rust_search._screen(search, group, jobs, k, 0, 1)
        assert list(got) == list(want)
        for index in want:
            if want[index] is None:
                assert got[index] is None
                continue
            assert list(got[index][0].items()) == list(want[index][0].items()), index
            assert got[index][1] == want[index][1], index
        assert any(w is not None and w[1] for w in want.values())           # ties were exercised
        assert want[len(jobs) - 1] is None                                  # and the None case


@pytest.mark.parametrize("chosen", ["tiled2+score", "sparse", "tiled"])
def test_fast_search_with_rust_gives_the_standard_results(libraries, monkeypatch, chosen):
    from gcws.libsearch import rust_search, service
    monkeypatch.setenv("GCWS_RUST_SEARCH", chosen)
    rust_search.install()
    for extra in (dict(algorithm="pbm", mode="combined"), dict(algorithm="similarity", mode="combined"),
                  dict(algorithm="pbm", mode="sequential", stop_score=60)):
        settings = dict(libraries=["A", "B"], min_mz=35, max_mz=400, threshold=0, lite=True, max_hits=5, **extra)
        standard = []
        for name, points in libraries:
            try:
                standard.append(service.analyze(points, name, settings))
            except ValueError as exc:
                standard.append(exc)
        results = service.analyze_many(libraries, settings)
        assert [_clean(r) for r in results] == [_clean(r) for r in standard], extra


# -- the installed native libraries (Agilent, Shimadzu, NIST): skipped where there are none -------

@pytest.fixture(scope="module")
def native():
    from gcws.libsearch import service
    try:
        eng = service.get_engine()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no library engine: {exc}")
    if not eng.native_count:
        pytest.skip("no native libraries installed")
    return eng


def _sample_rows(eng, per_reader=60):
    rng = np.random.default_rng(1)
    rows = []
    for start, reader in eng.native:
        if reader.count:
            rows += (start + rng.choice(reader.count, size=min(per_reader, reader.count), replace=False)).tolist()
    return rows


def test_native_spectra_are_decoded_as_in_python(native):
    from gcws.libsearch import fast, rust_search
    stats = native.statistics()
    refs = rust_search._refs(native, stats)
    rows = _sample_rows(native)
    for lo, hi in ((35, 666), (1, 10000), (300, 320)):
        want = fast._decode_many(native, rows, lo, hi)
        assert refs.decode(np.asarray(rows, np.int64), lo, hi) is not None
        for row, (ref, positive) in zip(rows, want):
            got = refs.reference(row, lo, hi)
            assert got is not None, row
            assert list(got[0]) == list(ref), row
            assert [struct.pack("<d", v) for v in got[0].values()] == [struct.pack("<d", v) for v in ref.values()]
            assert got[1] == positive, row


def test_native_pbm_scores_are_the_python_scores(native):
    from gcws.libsearch import fast, rust_search
    from pbm import _percent, _significant
    stats = native.statistics()
    refs = rust_search._refs(native, stats)
    rows = _sample_rows(native)
    rng = np.random.default_rng(2)
    for n in range(10):
        query = {m: v * float(rng.uniform(0.5, 1.5)) for m, v in
                 fast.nominal_peaks(native.peaks(rows[int(rng.integers(len(rows)))])).items() if 35 <= m <= 666}
        unknown = _percent(query)
        peaks, weights = _significant(unknown, stats)
        job = fast._Job(n, None)
        job.pbm = (unknown, peaks, weights, sum(weights[m] for m in peaks))
        want = fast._pbm_many(job.pbm, _python_sides(native, rows, stats), stats)
        got = refs.pbm(np.asarray(rows, np.int64), 35, 666, *rust_search._unknown(job))
        for w, g in zip(want, got[:3]):
            assert [struct.pack("<d", v) for v in w.tolist()] == [struct.pack("<d", v) for v in g.tolist()]


def _python_sides(eng, rows, stats):
    from gcws.libsearch import fast
    return fast._reference_sides([r for r, _p in fast._decode_many(eng, rows, 35, 666)], stats)


def test_log2_table_is_math_log2():
    from gcws.libsearch import fast, rust_search
    job = fast._Job(0, None)
    job.pbm = ({41: 100.0, 43: 37.5, 57: 0.25}, [41, 43], {41: 3.0, 43: 2.0, 57: 1.0}, 5.0)
    dense, log2inv, *_ = rust_search._unknown(job)
    assert log2inv[43] == math.log2(1 / 0.375) and log2inv[41] == 0.0 and dense[57] == 0.25


# -- the normal search (Engine.analyze, peak by peak) ---------------------------------------------

def _standard_results(search_spectra, settings, chosen, monkeypatch):
    from gcws.libsearch import rust_search, service
    monkeypatch.setenv("GCWS_RUST_SEARCH", chosen)
    rust_search.install()
    eng = service.get_engine()
    out = []
    for name, points in search_spectra:
        spectrum = service._msp.Spectrum(name=name, peaks=[(float(m), float(i)) for m, i in points if i > 0])
        try:
            out.append(_clean(eng.analyze(spectrum, dict(settings), regional=False)))
        except ValueError as exc:
            out.append(("error", str(exc)))
    return out


@pytest.mark.parametrize("extra", [dict(algorithm="pbm", mode="combined"), dict(algorithm="similarity"),
                                   dict(algorithm="pbm", mode="sequential", stop_score=60),
                                   dict(algorithm="pbm", mode="combined", lite=False, max_hits=10)])
def test_standard_search_with_rust_prefilter_is_unchanged(libraries, monkeypatch, extra):
    """MSP references: the Rust prefilter feeds the Python scoring; the same results."""
    settings = {**dict(libraries=["A", "B"], min_mz=35, max_mz=400, threshold=0, lite=True, max_hits=5), **extra}
    want = _standard_results(libraries, settings, "off", monkeypatch)
    assert _standard_results(libraries, settings, "standard", monkeypatch) == want


def test_rust_prefilter_shard_is_the_python_one(libraries, monkeypatch):
    from gcws.libsearch import fast, rust_search, service
    monkeypatch.setenv("GCWS_RUST_SEARCH", "standard")
    rust_search.install()
    eng = service.get_engine()
    for name, points in libraries[:12]:
        query = {m: i for m, i in fast.nominal_peaks(points).items() if 35 <= m <= 400}
        qnorm = sum(i * (m / 100) ** 2 for m, i in query.items())
        for index in range(len(eng.shards)):
            want = rust_search._installed["engine_prefilter"](eng, index, query, qnorm, 35, 400)
            got = rust_search._engine_prefilter_shard(eng, index, query, qnorm, 35, 400)
            assert got["start"] == want["start"] and got["source"] == want["source"]
            for d in ("forward", "reverse"):
                assert got[d].tobytes() == want[d].tobytes(), (name, index, d)


@pytest.mark.parametrize("extra", [dict(algorithm="pbm", mode="sequential", stop_score=80),
                                   dict(algorithm="pbm", mode="combined", lite=False),
                                   dict(algorithm="similarity", mode="combined")])
def test_native_standard_search_gives_the_python_hits(native, monkeypatch, extra):
    """The installed libraries, searched peak by peak: Rust prefilter and scoring, Python's hits."""
    from gcws.libsearch import fast
    rows = _sample_rows(native, per_reader=1)
    rng = np.random.default_rng(4)
    spectra = []
    for row in rows[:6]:
        peaks = fast.nominal_peaks(native.peaks(row))
        spectra.append((f"row {row}", [(float(m), v * float(rng.uniform(0.7, 1.3))) for m, v in peaks.items()]))
    libraries = sorted({s["source"] for s in native.shards if s["count"] and s["start"] < native.native_count})
    settings = {**dict(libraries=libraries, min_mz=35, max_mz=666, threshold=0, lite=True, max_hits=5), **extra}
    want = _standard_results(spectra, settings, "off", monkeypatch)
    assert _standard_results(spectra, settings, "standard", monkeypatch) == want
"""Fast search: all peaks at once, exactly the hits of the search peak by peak."""
import numpy as np
import pytest

from gcws import paths
from gcws.identify import library_edit as LE

COMMON = [39, 41, 43, 55, 57, 67, 69, 71, 77, 91]


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path)
    from gcws.libsearch import service
    service.reset()
    yield tmp_path
    service.reset()


def _spectrum(rng, heavy=False):
    """A random EI-like spectrum: common low ions plus a few characteristic higher ones."""
    peaks = {m: int(rng.integers(20, 999)) for m in rng.choice(COMMON, size=int(rng.integers(5, 10)), replace=False)}
    for m in rng.choice(np.arange(60, 400 if heavy else 250), size=int(rng.integers(3, 25)), replace=False):
        peaks[int(m)] = int(rng.integers(5, 999))
    top = max(peaks, key=peaks.get)
    peaks[top] = 999
    return sorted(peaks.items())


def _library(path, rng, count, prefix, copies=()):
    records, spectra = [], []
    for n in range(count):
        sp = _spectrum(rng, heavy=n % 3 == 0)
        spectra.append(sp)
        records.append(LE.new_record(f"{prefix} {n}", sp, cas=f"{1000 + n}-{n % 90 + 10}-{n % 10}",
                                     formula="C7H8" if n % 4 else "C6H5Cl"))
    for n, times in copies:                  # identical spectra: exact ties at a top-list boundary
        records += [LE.new_record(f"{prefix} {n} copy {c}", spectra[n], cas=f"{1000 + n}-{n % 90 + 10}-{n % 10}")
                    for c in range(times)]
    path.write_text(LE.write_msp(records), encoding="cp1252", newline="")
    return spectra


class _Queries(list):
    copied = None


def _queries(rng, spectra, count):
    out = _Queries()
    for n in range(count):
        a = dict(spectra[int(rng.integers(len(spectra)))])
        if n % 3 == 0:                       # a mixture of two
            for m, i in spectra[int(rng.integers(len(spectra)))]:
                a[m] = a.get(m, 0) + i * 0.4
        noisy = [(float(m), float(i) * float(rng.uniform(0.7, 1.3))) for m, i in a.items()]
        noisy += [(float(m), float(rng.uniform(1, 30))) for m in rng.choice(np.arange(35, 300), 6, replace=False)]
        out.append((f"peak {n}", noisy))
    return out


def _clean(result):
    if isinstance(result, BaseException):
        return ("error", str(result))
    result = dict(result)
    result.pop("seconds")
    return result


@pytest.fixture
def libraries(data):
    """Two MSP libraries of ~1400 spectra each, with copies to force ties."""
    from gcws.libsearch import store
    rng = np.random.default_rng(7)
    a = _library(data / "A.msp", rng, 1300, "A", copies=[(5, 40), (17, 350)])
    b = _library(data / "B.msp", rng, 1400, "B")
    store.save(store.add(store.discover(data / "A.msp"), store.discover(data / "B.msp")))
    queries = _queries(rng, a + b, 30)
    queries.copied = a[17]
    return queries


SETTINGS = [
    dict(algorithm="pbm", mode="combined", max_hits=5, dedupe=True),
    dict(algorithm="pbm", mode="combined", max_hits=10, dedupe=False),
    dict(algorithm="similarity", mode="combined", max_hits=5, dedupe=True),
    dict(algorithm="pbm", mode="sequential", stop_score=60, max_hits=5, dedupe=True),
    dict(algorithm="pbm", mode="combined", max_hits=5, name_exclude=["copy"], library_min_scores={"B": 30}),
    dict(algorithm="pbm", mode="combined", max_hits=5, lite=False),
]


@pytest.mark.parametrize("extra", SETTINGS)
@pytest.mark.parametrize("block", [16384, 257])
def test_fast_search_gives_the_standard_results(libraries, monkeypatch, extra, block):
    from gcws.libsearch import fast, service
    monkeypatch.setattr(fast, "BLOCK", block)          # 257: many blocks, shuffled, partial last one
    settings = {**dict(libraries=["A", "B"], min_mz=35, max_mz=400, threshold=0, lite=True), **extra}
    standard = []
    for name, points in libraries:
        try:
            standard.append(service.analyze(points, name, settings))
        except ValueError as exc:
            standard.append(exc)
    reported = {}
    results = service.analyze_many(libraries, settings, done=lambda i, r: reported.setdefault(i, r))
    assert sorted(reported) == list(range(len(libraries)))
    assert [_clean(r) for r in results] == [_clean(r) for r in standard]
    eng = service.get_engine()
    search = fast.FastSearch(eng, settings)
    search.analyze([service._msp.Spectrum(name=n, peaks=p) for n, p in libraries])
    assert search.counts["screened"] + search.counts["standard"] == len(libraries)
    assert search.counts["screened"] > len(libraries) // 2         # the block screen did the work


def test_ties_at_the_boundary_are_detected(libraries):
    """350 copies of one spectrum: its peaks tie at the top-300 boundary; still the standard hits."""
    from gcws.libsearch import fast, service
    settings = dict(libraries=["A", "B"], min_mz=35, max_mz=400, max_hits=5, dedupe=False, lite=True)
    eng = service.get_engine()
    copy = dict(libraries.copied)
    queries = [(f"copy {n}", [(float(m), float(i) * (1 + n / 100)) for m, i in copy.items()]) for n in range(10)]
    search = fast.FastSearch(eng, settings)
    got = search.analyze([service._msp.Spectrum(name=n, peaks=p) for n, p in queries])
    assert search.counts["tie_boundaries"] > 0
    want = [eng.analyze(service._msp.Spectrum(name=n, peaks=p), dict(settings), regional=False) for n, p in queries]
    assert [_clean(r) for r in got] == [_clean(r) for r in want]


def test_errors_are_reported_per_spectrum(libraries):
    from gcws.libsearch import service
    settings = dict(libraries=["A", "B"], min_mz=35, max_mz=400, lite=True)
    spectra = libraries[:9] + [("empty", [(50.0, 0.0)]), ("below range", [(20.0, 100.0)])]
    results = service.analyze_many(spectra, settings)
    assert str(results[9]) == "The spectrum has no peaks."
    with pytest.raises(ValueError) as standard:
        service.analyze([(20.0, 100.0)], "below range", settings)
    assert isinstance(results[10], ValueError) and str(results[10]) == str(standard.value)
    bad = service.analyze_many(libraries[:9], dict(settings, libraries=["missing"]))
    assert all(isinstance(r, ValueError) and "unavailable" in str(r) for r in bad)


def test_cancel_stops_between_blocks(libraries, monkeypatch):
    from gcws.libsearch import fast, service
    monkeypatch.setattr(fast, "BLOCK", 257)
    finished = []
    results = service.analyze_many(libraries, dict(libraries=["A", "B"], min_mz=35, max_mz=400, lite=True),
                                   cancelled=lambda: len(finished) >= 5, done=lambda i, r: finished.append(i))
    assert 5 <= len(finished) < len(libraries)
    assert sum(r is not None for r in results) == len(finished)


def test_screened_cosines_are_bounded(libraries):
    """The float32 screen stays within the bound the exact recomputation relies on."""
    from gcws.libsearch import fast, service
    eng = service.get_engine()
    settings = dict(libraries=["A", "B"], min_mz=35, max_mz=400, lite=True)
    search = fast.FastSearch(eng, settings)
    shared = search._shared()
    jobs = []
    for n, (name, points) in enumerate(libraries[:12]):
        job = fast._Job(n, service._msp.Spectrum(name=name, peaks=points))
        search._prepare(job, shared)
        jobs.append(job)
    screen = fast._Screen(jobs, 300)
    group = shared[1][0]
    for q, job in enumerate(jobs):
        parts = eng._prefilter(group, job.query, 35, 400)
        exact_f = np.concatenate([p["forward"] for p in parts])
        exact_r = np.concatenate([p["reverse"] for p in parts])
        dense = np.zeros((screen.C, eng.count), np.float32)
        for n in group:
            shard = eng.shards[n]
            for c, m in enumerate(screen.masses):
                a, b = int(shard["pointers"][m]), int(shard["pointers"][m + 1])
                dense[c, shard["start"] + np.asarray(shard["rows"][a:b])] = np.sqrt(shard["intensities"][a:b])
        refnorm = np.concatenate([eng.shard_norms(n, 35, 400) for n in group])
        qf = np.zeros(screen.C, np.float32)
        qr = np.zeros(screen.C, np.float32)
        qf[screen.cols[q]] = screen.si[q][:, 0] * screen.w[q][:, 0]
        qr[screen.cols[q]] = screen.iw[q][:, 0]
        dots = qf @ dense
        rnorm = qr @ np.sign(dense)
        with np.errstate(divide="ignore", invalid="ignore"):
            forward = np.where(refnorm > 0, dots / np.sqrt(job.qnorm * refnorm), 0)
            reverse = np.where(rnorm * refnorm > 0, dots / np.sqrt(rnorm * refnorm), 0)
        assert np.abs(forward - exact_f).max() <= screen.delta / 2
        assert np.abs(reverse - exact_r).max() <= screen.delta / 2


def test_batch_search_uses_fast_search_when_switched_on(libraries, monkeypatch):
    import gc_identify as GI
    import gc_search_method as SM
    from gcws.identify.service import LocalBatchSearch, is_fast, set_fast
    from gcws.libsearch import service

    def jobs():
        return [GI.PeakJob(label="s", row_id=i, peak_no=i, rt=5.0 + i, before=("", "", None), spectrum=p)
                for i, (_n, p) in enumerate(libraries)]

    def run(method):
        batch = LocalBatchSearch(jobs(), method).start()
        batch.thread.join(120)
        messages = []
        while not batch.messages.empty():
            messages.append(batch.messages.get())
        assert messages[-1] == ("done", False) and not any(k == "error" for k, _ in messages)
        assert sorted(v for k, v in messages if k == "hit") == list(range(len(libraries)))
        return batch.jobs, messages

    method = SM.SearchMethod(name="Fast one", top_n=4, min_score=0)
    assert not is_fast(method)
    normal, _ = run(method)
    calls = []
    monkeypatch.setattr(service, "analyze_many", lambda *a, _f=service.analyze_many, **k: calls.append(1) or _f(*a, **k))
    assert set_fast("Fast one", True) and is_fast(method) and is_fast("Fast one")
    quick, messages = run(method)
    assert calls and any(k == "status" and "Fast search" in v for k, v in messages)
    assert [(j.hits, j.error) for j in quick] == [(j.hits, j.error) for j in normal]
    assert all(len(j.hits) <= 4 for j in quick)
    set_fast("Fast one", False)
    assert not is_fast(method)


def test_fast_search_switch_in_the_method_dialog(qtbot, data, monkeypatch):
    import gc_search_method as SM
    from gcws.identify.service import fast_search_methods, is_fast
    from gcws.ui.dialogs.search_method import SearchMethodDialog, order_summary
    monkeypatch.setattr(SM, "store_path", lambda: data / "library_search_methods.json")
    ms = SM.MethodStore()
    m = SM.SearchMethod(name="Daily")
    m.libraries = [SM.LibraryEntry("A"), SM.LibraryEntry("B")]
    ms.put(m)
    ms.save()
    dlg = SearchMethodDialog(None, "Daily")
    qtbot.addWidget(dlg)
    assert not dlg.fast.isChecked() and dlg.fast.text() == "Fast search"
    dlg.fast.setChecked(True)
    dlg._save()
    assert is_fast("Daily") and order_summary(SM.MethodStore().get("Daily")).endswith("Fast search")
    with monkeypatch.context() as mp:             # New... copies the switch
        mp.setattr("gcws.ui.dialogs.search_method.QInputDialog.getText", lambda *a, **k: ("Copy", True))
        dlg._new()
    assert is_fast("Copy") and dlg.fast.isChecked()
    dlg._delete()                                 # deleting "Copy" forgets its switch
    assert fast_search_methods() == {"Daily"}
    dlg.names.setCurrentText("Daily")
    assert dlg.fast.isChecked()
    dlg.fast.setChecked(False)
    dlg._save()
    assert not is_fast("Daily") and not order_summary(SM.MethodStore().get("Daily")).endswith("Fast search")


def test_processing_method_keeps_the_switch(data, monkeypatch):
    import gc_search_method as SM
    from gcws.core import proc_method as PM
    from gcws.identify.service import is_fast
    monkeypatch.setattr(SM, "store_path", lambda: data / "library_search_methods.json")
    m = SM.SearchMethod(name="From method")
    PM._apply_search({"method": m.as_dict(), "fast": True})
    assert is_fast("From method")
    PM._apply_search({"method": m.as_dict()})                     # older method files: unchanged
    assert is_fast("From method")
    PM._apply_search({"method": m.as_dict(), "fast": False})
    assert not is_fast("From method")


@pytest.mark.parametrize("extra", SETTINGS[:4])
@pytest.mark.parametrize("block", [16384, 257])
def test_spectra_of_different_ranges_share_one_screen(libraries, monkeypatch, extra, block):
    """Per-spectrum m/z ranges (the consensus spectra): one screen, still each range's standard hits."""
    from gcws.libsearch import fast, service
    monkeypatch.setattr(fast, "BLOCK", block)
    settings = {**dict(libraries=["A", "B"], min_mz=35, max_mz=400, threshold=0, lite=True), **extra}
    choices = [(35, 400), (39, 250), (41, 300), (41, 120), (60, 380), (None, None)]
    ranges = [choices[n % len(choices)] for n in range(len(libraries))]
    standard = []
    for (name, points), (lo, hi) in zip(libraries, ranges):
        try:
            standard.append(service.analyze(points, name, {**settings, "min_mz": lo, "max_mz": hi}))
        except ValueError as exc:
            standard.append(exc)
    screens = []
    real = fast.FastSearch._screen
    monkeypatch.setattr(fast.FastSearch, "_screen",
                        lambda self, group, jobs, *a: screens.append(len(jobs)) or real(self, group, jobs, *a))
    results = service.analyze_many(libraries, settings, ranges=ranges)
    assert [_clean(r) for r in results] == [_clean(r) for r in standard]
    assert screens and max(screens) > len(libraries) // 2           # the ranges were screened together


def test_pbm_of_many_candidates_is_the_scalar_pbm():
    """``_pbm_many`` scores all candidates of a peak at once, to the last bit as ``_pbm`` does one
    by one (also references whose base peak the unknown lacks, zero peaks, an empty side)."""
    import struct
    from gcws.libsearch import fast as FS
    from pbm import PeakStatistics, _percent, _significant
    rng = np.random.default_rng(11)
    refs = []
    for n in range(300):
        sp = dict(_spectrum(rng, heavy=n % 2 == 0))
        if n % 7 == 0:
            sp[int(rng.integers(30, 60))] = 0                   # a zero-intensity peak
        refs.append({m: 100.0 * i / max(sp.values()) for m, i in sorted(sp.items()) if 35 <= m <= 400})
    refs.append({})                                               # nothing in the range: no side
    stats = PeakStatistics.from_spectra(refs)
    sides = FS._reference_sides(refs, stats)
    assert sides[-1] is None
    for n in range(12):
        query = {m: float(i) * float(rng.uniform(0.5, 1.5)) for m, i in _spectrum(rng)}
        unknown = _percent(query)
        peaks, weights = _significant(unknown, stats)
        side = (unknown, peaks, weights, sum(weights[m] for m in peaks))
        got = FS._pbm_many(side, sides, stats)
        for i, (ref, rside) in enumerate(zip(refs, sides)):
            want = FS._pbm(side, ref, rside, stats)
            assert [struct.pack("<d", float(v[i])) for v in got] == [struct.pack("<d", v) for v in want], (n, i)
    none = FS._pbm_many(None, sides, stats)
    assert not any(v.any() for v in none)


def test_references_are_decoded_together_as_one_by_one(data):
    """``_decode_many`` bins the candidates' peaks to nominal masses all at once: the same spectra
    to the last bit as ``nominal_peaks`` per reference (fractional masses summed in peak order, a
    mass below 0.5 dropped, the base over the whole spectrum, the m/z range applied after it)."""
    import struct
    from gcws.libsearch import fast, service, store
    rng = np.random.default_rng(5)
    records = []
    for n in range(200):
        peaks = [(float(m) + float(rng.uniform(-0.45, 0.45)), float(rng.uniform(0.1, 999)))
                 for m in rng.choice(np.arange(20, 450), size=int(rng.integers(2, 40)), replace=False)]
        peaks += [(p[0] + 0.3, p[1] / 3) for p in peaks[:3]]             # two peaks on one nominal mass
        if n % 5 == 0:
            peaks.append((0.2, 50.0))                                      # nominal mass 0: dropped
        records.append(LE.new_record(f"R {n}", sorted(peaks), cas=""))
    (data / "R.msp").write_text(LE.write_msp(records), encoding="cp1252", newline="")
    store.save(store.add([], store.discover(data / "R.msp")))
    eng = service.get_engine()
    rows = list(range(eng.native_count + len(eng.custom)))[:200]
    for lo, hi in ((35, 400), (1, 10000), (300, 320)):
        got = fast._decode_many(eng, rows, lo, hi)
        for row, (ref, positive) in zip(rows, got):
            want = {m: i for m, i in fast.nominal_peaks(eng.peaks(row)).items() if lo <= m <= hi}
            assert list(ref) == list(want)
            assert [struct.pack("<d", v) for v in ref.values()] == [struct.pack("<d", v) for v in want.values()]
            assert positive == (None if all(i > 0 for i in want.values()) else
                                frozenset(m for m, i in want.items() if i > 0))

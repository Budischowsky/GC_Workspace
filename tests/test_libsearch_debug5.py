"""LibSearch fixes of the fifth debugging round: library caches that outlive a rebuilt index, the
GUI not waiting for a running search."""
import threading
import time

import numpy as np

from tests.test_fast_search import data, libraries  # noqa: F401  (fixtures)
from tests.test_libsearch_startup import _mapped


def _first_half(shard):
    """``shard`` with only its first half of the reference spectra (the same masses)."""
    keep = shard["count"] // 2
    rows, intensities, pointers = (np.asarray(shard[k]) for k in ("rows", "intensities", "pointers"))
    mass = np.repeat(np.arange(len(pointers) - 1), np.diff(pointers))
    good = rows < keep
    counts = np.bincount(mass[good], minlength=len(pointers) - 1)
    return dict(shard, count=keep, rows=rows[good], intensities=intensities[good],
                pointers=np.r_[0, np.cumsum(counts)])


def test_counts_of_a_rebuilt_index_are_computed_again(libraries, tmp_path):
    """open_index rebuilds an index in the same folder when its spectrum count changed: the
    counts kept beside the old index must not be read for the new one."""
    from gcws.libsearch import service, stats
    shard = service.get_engine().shards[0]
    mapped = _mapped(shard, tmp_path / "idx")
    stats.counts(mapped)
    del mapped
    smaller = _first_half(shard)
    for name in ("rows", "intensities", "pointers"):           # rebuilt in place, the old counts stay
        np.save(tmp_path / "idx" / f"{name}.npy", smaller[name])
    rebuilt = dict(smaller, **{name: np.load(tmp_path / "idx" / f"{name}.npy", mmap_mode="r")
                               for name in ("rows", "intensities", "pointers")})
    want = stats.compute(rebuilt)
    got = stats.counts(rebuilt)
    assert all(np.array_equal(a, b) for a, b in zip(got, want))


def test_reset_does_not_wait_for_a_running_search(libraries):
    """Libraries... saves and resets on the GUI thread; a search holding the engine must not freeze
    it, and the next search after it reads the libraries again."""
    from gcws.libsearch import service
    old = service.get_engine()
    holding, release = threading.Event(), threading.Event()

    def search():
        with service._lock:
            holding.set()
            release.wait(10)
    t = threading.Thread(target=search)
    t.start()
    try:
        assert holding.wait(5)
        started = time.perf_counter()
        service.reset()
        assert time.perf_counter() - started < 1.0
        assert service._engine is old and old.shards            # the running search keeps its engine
    finally:
        release.set()
        t.join()
    assert service.get_engine() is not old


def test_stale_norm_snapshot_on_disk_is_replaced(libraries, tmp_path, monkeypatch):
    """A stored snapshot of another spectrum count (an index rebuilt in place) or an unreadable
    one is computed again and stored over, not recomputed by every later process."""
    from gcws.libsearch import norms, service
    eng = service.get_engine()
    shard = _mapped(eng.shards[0], tmp_path / "fp")
    disk = norms.DiskNorms(tmp_path / "norms")
    folder = tmp_path / "norms" / "fp"
    folder.mkdir(parents=True)
    np.save(folder / "41_200.npy", np.zeros(int(shard["count"]) + 5))     # the old index's count
    (folder / "41_150.npy").write_bytes(b"broken")
    want = {hi: norms.NormFolds().get(eng.shards[0], 0, 41, hi) for hi in (150, 200)}
    for hi in (150, 200):
        assert np.array_equal(norms.NormFolds(disk=disk).get(shard, 0, 41, hi), want[hi])
    monkeypatch.setattr(norms, "_fold_mass", lambda *a: (_ for _ in ()).throw(AssertionError("folded")))
    for hi in (150, 200):                                       # stored now: read back by the next process
        assert np.array_equal(norms.NormFolds(disk=disk).get(shard, 0, 41, hi), want[hi])


def test_update_the_list_does_not_wait_for_a_running_search(qtbot, libraries, data, monkeypatch):
    """Search method > Update the list (and after Libraries...) reads the libraries in the
    background: a running search holding the engine does not freeze the dialog."""
    import gc_search_method as SM
    from gcws.libsearch import service
    from gcws.ui.dialogs.search_method import SearchMethodDialog
    monkeypatch.setattr(SM, "store_path", lambda: data / "library_search_methods.json")
    ms = SM.MethodStore()
    m = SM.SearchMethod(name="Only A")
    m.libraries = [SM.LibraryEntry("A")]
    ms.put(m)
    ms.save()
    dlg = SearchMethodDialog(None, "Only A")
    qtbot.addWidget(dlg)
    assert dlg.library_order() == ["A"]
    holding, release = threading.Event(), threading.Event()

    def search():
        with service._lock:
            holding.set()
            release.wait(10)
    t = threading.Thread(target=search)
    t.start()
    try:
        assert holding.wait(5)
        started = time.perf_counter()
        dlg._refresh_libs()
        assert time.perf_counter() - started < 1.0
    finally:
        release.set()
        t.join()
    qtbot.waitUntil(lambda: dlg.library_order() == ["A", "B"], timeout=10000)
    from PySide6.QtCore import Qt
    assert dlg.libs.item(1).checkState() == Qt.Unchecked        # a new library comes switched off

"""Old library indexes in DATA/libcache/.cache are removed when the libraries are loaded."""
import os
import time

import numpy as np
import pytest

from gcws import paths
from gcws.identify import library_edit as LE

RECORDS = [("Toluene", [(91, 999), (92, 600), (65, 120), (39, 80)]),
           ("Hexane", [(57, 999), (43, 700), (41, 500), (86, 150), (29, 300)])]
DAY = 86400


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path)
    from gcws.libsearch import service, store
    service.reset()
    lib = tmp_path / "Own.msp"
    lib.write_text(LE.write_msp([LE.new_record(n, p) for n, p in RECORDS]), encoding="cp1252", newline="")
    store.save(store.discover(lib))
    yield tmp_path
    service.reset()


def _index(cache, name, age_days, files=("intensities.npy", "manifest.json")):
    folder = cache / name
    folder.mkdir(parents=True)
    for f in files:
        if f.endswith(".npy"):
            np.save(folder / f, np.arange(1000, dtype=np.float32))
        else:
            (folder / f).write_text("{}")
    _age(folder, age_days)
    return folder


def _age(path, days):
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def _engine():
    from gcws.libsearch import service
    service.reset()
    return service.get_engine()


def test_old_indexes_are_removed_recent_ones_and_other_files_stay(data):
    from gcws.libsearch import cache_clean
    cache = data / "libcache" / ".cache"
    old = _index(cache, "a" * 64, 8)
    recent = _index(cache, "b" * 64, 2)
    old_msp = _index(cache, "msp-" + "c" * 32, 30, files=("mz.npy", "records.json"))
    leftover = _index(cache, "d" * 64 + ".removing-123", 0)
    for name in ("preferences.json", "nist-" + "e" * 64 + ".json"):
        (cache / name).write_text("{}")
        _age(cache / name, 30)
    engine = _engine()
    assert engine.count == len(RECORDS)
    own = cache_clean.index_folder(engine.root, [data / "Own.msp"])
    assert (own / "manifest.json").exists()                  # the fingerprint is the vendored one
    assert not old.exists() and not old_msp.exists() and not leftover.exists()
    assert recent.exists() and (cache / "preferences.json").exists() and (cache / f"nist-{'e' * 64}.json").exists()


def test_an_index_in_use_is_marked_and_kept(data):
    from gcws.libsearch import cache_clean
    engine = _engine()
    own = cache_clean.index_folder(engine.root, [data / "Own.msp"])
    _age(own, 30)                                             # not used for a month, but in use now
    _engine()
    assert (own / "manifest.json").exists()
    assert time.time() - own.stat().st_mtime < DAY


def test_an_index_another_process_has_open_stays_whole(data):
    cache = data / "libcache" / ".cache"
    held = _index(cache, "f" * 64, 30)
    mapped = np.load(held / "intensities.npy", mmap_mode="r")
    try:
        _engine()
        assert (held / "manifest.json").exists() and (held / "intensities.npy").exists()
    finally:
        del mapped
    _engine()
    assert not held.exists()

"""Library engine start: the PBM statistics kept per library and the Shimadzu reader's array pass
give exactly what the vendored code computes."""
import struct

import numpy as np
import pytest

from tests.test_fast_search import data, libraries  # noqa: F401  (fixtures)


def _vendor_statistics(eng):
    import gcws.libsearch  # noqa: F401  (vendor on sys.path)
    import engine
    return engine.Engine.statistics.__wrapped__(eng)


def test_statistics_equal_vendor(libraries):
    from gcws.libsearch import service
    eng = service.get_engine()
    got, ref = eng.statistics(), _vendor_statistics(eng)
    assert np.array_equal(got.uniqueness, ref.uniqueness) and np.array_equal(got.abundance, ref.abundance)
    assert eng.statistics() is got                     # cached per engine


def _mapped(shard, folder):
    """The shard as a large library has it: memory-mapped arrays in a cache folder."""
    folder.mkdir()
    out = dict(shard)
    for name in ("rows", "intensities", "pointers"):
        np.save(folder / f"{name}.npy", np.asarray(shard[name]))
        out[name] = np.load(folder / f"{name}.npy", mmap_mode="r")
    return out


def test_counts_are_kept_beside_the_index(libraries, tmp_path, monkeypatch):
    from gcws.libsearch import service, stats
    eng = service.get_engine()
    shard = _mapped(eng.shards[0], tmp_path / "idx")
    first = stats.counts(shard)
    assert (tmp_path / "idx" / stats.FILE).exists()
    monkeypatch.setattr(stats, "compute", lambda s: pytest.fail("recomputed"))
    again = stats.counts(shard)
    assert all(np.array_equal(a, b) for a, b in zip(first, again))
    monkeypatch.undo()
    (tmp_path / "idx" / stats.FILE).write_bytes(b"broken")      # unreadable: computed again
    assert all(np.array_equal(a, b) for a, b in zip(first, stats.counts(shard)))
    whole = stats.statistics([shard] + eng.shards[1:], eng.count)
    ref = _vendor_statistics(eng)
    assert np.array_equal(whole.uniqueness, ref.uniqueness) and np.array_equal(whole.abundance, ref.abundance)


def _shimadzu(folder, spectra):
    """A small Wiley/Shimadzu library in the layout the vendored reader validates; the spectra
    are stored in reverse compound order (search order differs from name order)."""
    n = len(spectra)
    info = bytearray(b"LH\x03\0" + bytes(24) + b"INF\0") + struct.pack("<II", 0, n * 16)
    for c, (name, peaks) in enumerate(spectra):
        info += struct.pack("<IIH6x", 0, 1000 + c * 17, 100 + c)

    def table(signature, records):
        head = bytearray(signature + bytes(16)) + struct.pack("<I", n * 4) + bytes(4)
        offsets, body = [], bytearray()
        for r in records:
            offsets.append(len(body))
            body += r
        return bytes(head + struct.pack(f"<{n}I", *offsets) + body)

    spc = []
    for c in reversed(range(n)):
        _name, peaks = spectra[c]
        base = max(peaks, key=lambda p: p[1])[0]
        rec = struct.pack("<HHIH", 100 + c, len(peaks), c + 1, base)
        rec += b"".join(bytes((i, m)) for m, i in peaks)
        spc.append(rec)
    nam = [struct.pack("<H", len(name)) + name.encode("cp1252") for name, _p in spectra]
    fom = [bytes((5,)) + b"C6H6O" for _ in spectra]
    path = folder / "TEST"
    path.with_suffix(".lib").write_bytes(bytes(info))
    path.with_suffix(".spc").write_bytes(table(b"SP\x02\0", spc))
    path.with_suffix(".nam").write_bytes(table(b"NM\x02\0", nam))
    path.with_suffix(".fom").write_bytes(table(b"FM\x02\0", fom))
    return path.with_suffix(".lib")


def test_shimadzu_reader_equals_vendor(tmp_path):
    import gcws.libsearch  # noqa: F401
    import shimadzu
    from gcws.libsearch import service
    spectra = [(f"compound {c}", [(39, 120), (57, 250), (91 + c, 30)]) for c in range(40)]
    lib = _shimadzu(tmp_path, spectra)
    ours, theirs = service._ShimadzuLibrary(lib), shimadzu.ShimadzuLibrary(lib)
    assert ours.ids.dtype == theirs.ids.dtype and np.array_equal(ours.ids, theirs.ids)
    assert ours.ids[0] == 40                       # stored in reverse
    for key in ("count", "info_start", "scan_offsets"):
        assert getattr(ours, key) == getattr(theirs, key)
    assert list(ours.records()) == list(theirs.records())
    assert [ours.metadata(r) for r in range(40)] == [theirs.metadata(r) for r in range(40)]
    # a broken ID table fails as the vendored reader fails
    spc = lib.with_suffix(".spc")
    raw = bytearray(spc.read_bytes())
    start = 28 + 40 * 4
    raw[start + 4:start + 8] = struct.pack("<I", 7)          # duplicate compound 7
    spc.write_bytes(bytes(raw))
    for cls in (service._ShimadzuLibrary, shimadzu.ShimadzuLibrary):
        with pytest.raises(ValueError, match="do not map uniquely"):
            cls(lib)

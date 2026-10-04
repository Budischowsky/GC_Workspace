"""The search engine over the analyst's libraries, shared by every search in the app.

``LocalEngine`` is SpectrAtlas's ``Engine`` (vendored, unchanged) loaded from an explicit
list of libraries instead of SpectrAtlas's folders. ``status()`` and ``analyze()`` answer in
the shape of SpectrAtlas's ``/api/status`` and ``/api/analyze``, so the search methods, the
batch search and the hit dialogs work on either.

The engine is built once (the first search builds or reads each library's index under
``DATA/libcache``) and rebuilt when the library list changes. Searches are serialised.
"""
from __future__ import annotations

import struct
import threading
from pathlib import Path
from typing import Callable, Optional

import gcws.libsearch  # noqa: F401  (vendor on sys.path)
from gcws.libsearch import store
from gcws.libsearch.norms import NormFolds

import engine as _atlas_engine        # vendored SpectrAtlas modules
import msp as _msp
import msp_cache
from agilent import AgilentLibrary
from nist import NistLibrary
from shimadzu import ShimadzuLibrary
from spectral_index import open_index

READERS = {"agilent": (AgilentLibrary, "Agilent / ChemStation EI"),
           "nist": (NistLibrary, "NIST MS Search EI"),
           "shimadzu": (ShimadzuLibrary, "Wiley / Shimadzu EI")}


class LocalEngine(_atlas_engine.Engine):
    """``Engine`` over ``specs`` (native libraries first, then MSP files, as SpectrAtlas orders them)."""

    def __init__(self, specs: list, cache: Path, progress: Callable[[str], None] = lambda t: None):
        self._folds = NormFolds()
        self.root = Path(cache)
        self.root.mkdir(parents=True, exist_ok=True)
        self.library_dirs = []
        self.native, self.custom, self.shards = [], [], []
        self.library_warnings, self.sources = [], []
        self.count = self.native_count = 0
        self._sqrt = {}
        specs = [s for s in specs if s.enabled]
        for spec in [s for s in specs if s.kind in READERS]:
            cls, kind = READERS[spec.kind]
            progress(f"Loading library {spec.name}")
            try:
                reader = cls(spec.path, cache=self.root / ".cache") if spec.kind == "nist" else cls(spec.path)
                arrays = open_index(self.root, reader.files, lambda r=reader: r.records(progress), reader.count,
                                    progress, reader)
                self.native.append((self.count, reader))
                self.shards.append(dict(start=self.count, source=spec.name, count=reader.count, **arrays))
                self.sources.append(dict(name=spec.name, count=reader.count, kind=kind, path=spec.path,
                                         status="Ready" if reader.count else "Empty library"))
                skipped = getattr(reader, "skipped", [])
                if skipped:
                    self.sources[-1]["skipped"] = len(skipped)
                self.count += reader.count
            except (ValueError, OSError, KeyError, IndexError, struct.error) as error:
                self.library_warnings.append(f"{spec.name}: {error}")
                self.sources.append(dict(name=spec.name, count=0, kind=kind, path=spec.path, status="Unavailable",
                                         error=str(error)))
        self.native_count = self.count
        self.agilent = next((r for _, r in self.native if isinstance(r, AgilentLibrary)), None)
        for spec in [s for s in specs if s.kind == "msp"]:
            progress(f"Loading library {spec.name}")
            try:
                records = msp_cache.load(self.root, Path(spec.path), progress)
                kept = [n for n, meta in enumerate(records.meta) if _atlas_engine.ei_compatible(meta)]
                arrays = open_index(self.root, [Path(spec.path)], lambda r=records, k=kept: (r.arrays(n) for n in k),
                                    len(kept), progress)
                self.shards.append(dict(start=self.count, source=spec.name, count=len(kept), **arrays))
                self.custom.extend((spec.name, records, n) for n in kept)
                self.sources.append(dict(name=spec.name, count=len(kept), stored=records.count, kind="MSP library",
                                         path=spec.path, status="Ready" if kept else "Empty library"))
                self.count += len(kept)
            except (ValueError, OSError) as error:
                self.library_warnings.append(f"{spec.name}: {error}")
                self.sources.append(dict(name=spec.name, count=0, kind="MSP library", path=spec.path,
                                         status="Unavailable", error=str(error)))
        progress(f"Libraries ready · {self.count:,} reference spectra")

    def shard_norms(self, index, minimum, maximum):
        """The vendored prefilter norms, bit for bit, resumed from the previous search range
        (:mod:`gcws.libsearch.norms`)."""
        return self._folds.get(self.shards[index], index, minimum, maximum)

    shard_norms.cache_clear = lambda: None      # the vendored close() clears its lru_cache

    def close(self):
        self._folds.clear()
        super().close()


_lock = threading.RLock()
_engine: Optional[LocalEngine] = None
_signature = None


def _key(specs):
    out = []
    for s in specs:
        p = Path(s.path)
        try:
            st = p.stat()
            stamp = (st.st_size, st.st_mtime_ns) if p.is_file() else \
                tuple(sorted((c.name, c.stat().st_size, c.stat().st_mtime_ns) for c in p.iterdir() if c.is_file()))
        except OSError:
            stamp = None
        out.append((s.name, s.kind, s.path, s.enabled, stamp))
    return tuple(out)


def get_engine(progress: Callable[[str], None] = lambda t: None, specs=None) -> LocalEngine:
    """The engine over the current library list (built or rebuilt when that list or a file changed)."""
    global _engine, _signature
    specs = store.load() if specs is None else specs
    with _lock:
        key = _key(specs)
        if _engine is None or key != _signature:
            if _engine is not None:
                _engine.close()
            _engine = LocalEngine(specs, store.cache_root(), progress)
            _signature = key
        return _engine


def library_signature() -> str:
    """The library list with every file's size and time (the engine's rebuild key), as text: hits
    stored under it are reused only while the libraries are unchanged."""
    import json
    return json.dumps(_key(store.load()), default=str)


def reset() -> None:
    """Forget the engine (after a library was edited; the next search reloads)."""
    global _engine, _signature
    with _lock:
        if _engine is not None:
            _engine.close()
        _engine, _signature = None, None


def status(progress: Callable[[str], None] = lambda t: None) -> dict:
    """Like SpectrAtlas's ``/api/status``: ``{"libraries": [{name, count, kind, status, ...}], ...}``."""
    eng = get_engine(progress)
    return {"libraries": [dict(s) for s in eng.sources], "count": eng.count, "warnings": list(eng.library_warnings),
            "library_selection": [s["name"] for s in eng.sources if s["count"] > 0]}


def analyze(points, name: str = "unknown", settings: Optional[dict] = None,
            progress: Callable[[str], None] = lambda t: None) -> dict:
    """Like SpectrAtlas's ``/api/analyze``: the hits of one spectrum ``[(m/z, abundance), ...]``."""
    peaks = [(float(m), float(i)) for m, i in points if float(i) > 0]
    if not peaks:
        raise ValueError("The spectrum has no peaks.")
    spectrum = _msp.Spectrum(name=name, peaks=peaks)
    with _lock:
        eng = get_engine(progress)
        if not eng.count:
            raise ValueError("No library loaded. Add your libraries under Identify > Libraries...")
        return eng.analyze(spectrum, dict(settings or {}), regional=False)


#: spectra per fast-search batch; the engine is locked for one batch at a time
FAST_BATCH = 400


def analyze_many(spectra: list, settings: Optional[dict] = None,
                 progress: Callable[[str], None] = lambda t: None,
                 cancelled: Callable[[], bool] = lambda: False,
                 done: Optional[Callable[[int, object], None]] = None) -> list:
    """Fast search: ``analyze`` of many spectra ``[(name, [(m/z, abundance), ...]), ...]`` at once.

    Each result is exactly what ``analyze`` returns for that spectrum (see ``gcws.libsearch.fast``),
    or the exception it raises; ``done(index, result)`` reports each one as it is finished."""
    from gcws.libsearch.fast import FastSearch
    results: list = [None] * len(spectra)

    def finished(index, result):
        results[index] = result
        if done is not None:
            done(index, result)

    parsed = []
    for n, (name, points) in enumerate(spectra):
        peaks = [(float(m), float(i)) for m, i in points if float(i) > 0]
        if peaks:
            parsed.append((n, _msp.Spectrum(name=name, peaks=peaks)))
        else:
            finished(n, ValueError("The spectrum has no peaks."))
    for start in range(0, len(parsed), FAST_BATCH):
        if cancelled():
            break
        batch = parsed[start:start + FAST_BATCH]
        with _lock:
            eng = get_engine(progress)
            if not eng.count:
                error = ValueError("No library loaded. Add your libraries under Identify > Libraries...")
                for n, _spectrum in parsed[start:]:
                    finished(n, error)
                break
            search = FastSearch(eng, dict(settings or {}), progress, cancelled)
            search.analyze([spectrum for _n, spectrum in batch],
                           lambda i, result, batch=batch: finished(batch[i][0], result))
    return results

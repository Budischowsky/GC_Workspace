"""Rust kernels for the library search (``rust/gcws_rust``), chosen by ``GCWS_RUST_SEARCH``.

Unset, the fastest combination (``tiled2+score+standard``) is used whenever the extension is built;
``off`` (or ``python``) keeps the Python search. Otherwise the value is a ``+``-separated list:

* ``score``  - stage 2 in Rust: reference spectra decoded from the library files (cached in Rust
  for the engine's lifetime), their PBM side and the PBM scores of a peak against all of its
  candidates; Python builds hit entries (dicts) only for the head of the ranked list.
* ``sparse`` - stage 1 in Rust: the standard prefilter itself (exact float32 terms, float64 sums
  in ascending m/z) for every peak of the batch against every reference, one peak per thread,
  with the standard candidate selection. Replaces the float32 BLAS screen and its bounds.
* ``tiled``  - stage 1 as ``sparse``, but chunks of peaks walk the index together reference tile
  by reference tile, so each posting is read once per chunk.
* ``tiled2`` - ``tiled`` with cache-sized tiles (``TILE`` references x ``CHUNK`` peaks fit the L2
  cache): a pair's dot product and reverse norm side by side, rows padded against cache-set
  aliasing, no bounds checks in the inner loop.
* ``standard`` - the normal search (``Engine.analyze``, peak by peak: Fast search off, single
  spectra, the fast search's own fallbacks): its prefilter per library and its scoring of the
  candidates in Rust.

Both stages reproduce the Python results bit for bit (the same operations in the same order);
whatever the Rust side cannot decode falls back to the Python code. Without the extension
nothing changes.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

try:
    import gcws_rust as _rust
except ImportError:  # the extension is not built
    _rust = None

#: stage 1 tuning (tiled): references per tile, peaks per chunk, references per task
TILE = int(os.environ.get("GCWS_RUST_TILE", "192"))
CHUNK = int(os.environ.get("GCWS_RUST_CHUNK", "64"))
SEGMENT = int(os.environ.get("GCWS_RUST_SEGMENT", "16384"))

_installed: dict = {}


#: the parts used when ``GCWS_RUST_SEARCH`` is not set
DEFAULT = "tiled2+score+standard"


def parts() -> set:
    if _rust is None:
        return set()
    value = os.environ.get("GCWS_RUST_SEARCH", DEFAULT).strip().lower()
    if value in ("off", "python", "0", "no"):
        return set()
    return {p for p in value.replace(",", "+").split("+") if p}


def install() -> set:
    """Switch the parts named by ``GCWS_RUST_SEARCH`` into ``FastSearch`` and the vendored ``Engine`` (idempotent).

    Called at the end of ``fast`` (which may still be importing this module)."""
    global _fast
    _fast = sys.modules["gcws.libsearch.fast"]
    chosen = parts()
    if not _installed:
        _installed["_screen"] = _fast.FastSearch._screen
        _installed["_score"] = _fast.FastSearch._score
        _installed["engine_prefilter"] = _fast._atlas_engine.Engine._prefilter_shard
        _installed["engine_score"] = _fast._atlas_engine.Engine._score
    _fast.FastSearch._screen = _installed["_screen"]
    _fast.FastSearch._score = _installed["_score"]
    _fast._atlas_engine.Engine._prefilter_shard = _installed["engine_prefilter"]
    _fast._atlas_engine.Engine._score = _installed["engine_score"]
    if chosen & {"sparse", "tiled", "tiled2"}:
        _fast.FastSearch._screen = _screen
    if "score" in chosen:
        _fast.FastSearch._score = _score
    if "standard" in chosen:
        _fast._atlas_engine.Engine._prefilter_shard = _engine_prefilter_shard
        _fast._atlas_engine.Engine._score = _engine_score
    return chosen


# -- stage 1 -------------------------------------------------------------------------------------

def _screen(self, group, jobs, k, number, groups) -> dict:
    """``FastSearch._screen`` by the exact prefilter in Rust: the same (candidates, maybe rows)."""
    eng = self.engine
    chosen = parts()
    mode = "tiled2" if "tiled2" in chosen else "tiled" if "tiled" in chosen else "sparse"
    self.progress(f"Fast search: prefiltering {len(jobs)} peaks against the libraries"
                  + (f" (library {number + 1} / {groups})" if groups > 1 else ""))
    ranges = sorted({(job.minimum, job.maximum) for job in jobs})
    range_of = {r: g for g, r in enumerate(ranges)}
    shards = []
    for n in group:
        shard = eng.shards[n]
        if not shard["count"]:
            continue
        norms = [np.ascontiguousarray(eng.shard_norms(n, a, b), np.float64) for a, b in ranges]
        shards.append((np.asarray(shard["pointers"]), np.asarray(shard["rows"]), np.asarray(shard["intensities"]),
                       norms, int(shard["start"]), int(n)))
    queries = []
    for job in jobs:
        masses = list(job.query)
        values = list(job.query.values())
        queries.append((np.array(masses, np.int64),
                        np.array([np.float32(math.sqrt(i)) for i in values], np.float32),
                        np.array([np.float32((m / 100) ** 2) for m in masses], np.float32),
                        np.array([i * (m / 100) ** 2 for m, i in job.query.items()], np.float64),
                        float(job.qnorm), range_of[(job.minimum, job.maximum)]))
    if not shards:
        return {job.index: None for job in jobs}
    selected = _rust.prefilter(shards, queries, k, mode, TILE, CHUNK, SEGMENT)
    sources = [s["source"] for s in eng.shards]
    out = {}
    for job, sel in zip(jobs, selected):
        if sel is None:
            out[job.index] = None
            continue
        rows, shard_ids, forward, reverse, maybe = sel
        candidates = {r: (sources[s], f, v) for r, s, f, v in
                      zip(rows.tolist(), shard_ids.tolist(), forward.tolist(), reverse.tolist())}
        out[job.index] = (candidates, set(maybe.tolist()))
        self.counts["tie_boundaries"] += bool(len(maybe))
    return out


# -- stage 2 -------------------------------------------------------------------------------------

def _refs(eng, stats):
    """The engine's Rust reference store (built once per engine and statistics)."""
    store = getattr(eng, "_rust_refs", None)
    if store is not None and (stats is None or store[1] is stats):
        return store[0]
    readers = []
    for start, reader in eng.native:
        if isinstance(reader, _fast._AgilentLibrary):
            readers.append((start, reader.count, "agilent", reader.data,
                            np.asarray(reader.scan_offsets, np.int64), None, 0, None, 0))
        elif isinstance(reader, _fast._ShimadzuBase):
            begin, offsets, ends = reader.tables["spc"]
            readers.append((start, reader.count, "shimadzu", reader.data, np.asarray(offsets, np.int64),
                            np.asarray(ends, np.int64), int(begin), reader.info, int(reader.info_start)))
        elif type(reader).__name__ == "NistLibrary":
            readers.append((start, reader.count, "nist", reader.data, np.asarray(reader.offsets, np.int64),
                            None, 0, None, 0))
    if stats is not None:
        refs = _rust.Refs(readers, np.ascontiguousarray(stats.uniqueness, np.float64),
                          np.ascontiguousarray(stats.abundance, np.float64))
    else:
        refs = _rust.Refs(readers)
    try:
        eng._rust_refs = (refs, stats)
    except AttributeError:
        pass
    return refs


def _unknown_arrays(pbm_side):
    """The unknown's PBM side ``(percent, significant peaks, weights, attainable)`` as the arrays
    ``Refs.pbm`` takes; ``math.log2(1 / dilution)`` per possible base m/z (C runtimes differ)."""
    unknown, qpeaks, qweights, qattainable = pbm_side
    dense = np.zeros(10002)
    log2inv = np.zeros(10002)
    for m, v in unknown.items():
        if 0 <= m <= 10001:
            dense[m] = v
            dilution = min(1.0, v / 100.0)
            if dilution > 0:
                log2inv[m] = math.log2(1 / dilution)
    return (dense, log2inv, np.asarray(qpeaks, np.int64), np.array([qweights[m] for m in qpeaks], np.float64),
            np.array([unknown[m] / _fast.FORWARD_WINDOW for m in qpeaks], np.float64), float(qattainable))


def _unknown(job):
    """The unknown's PBM side as arrays (once per peak)."""
    side = getattr(job, "_rust_unknown", None)
    if side is None:
        side = job._rust_unknown = _unknown_arrays(job.pbm)
    return side


def _score(self, job, candidates: dict, options, stats):
    """``FastSearch._score`` with decoding and PBM in Rust; the same head, value for value."""
    eng = self.engine
    query, minimum, maximum = job.query, job.minimum, job.maximum
    similarity = options.algorithm == 'similarity'
    if any(row >= eng.native_count for row in candidates):          # MSP references: Python
        return _installed["_score"](self, job, candidates, options, stats)
    refs = _refs(eng, None if similarity else stats)
    rows = np.fromiter(candidates, np.int64, len(candidates))
    if similarity:
        decoded = refs.decode(rows, minimum, maximum)
        if decoded is None:
            return _installed["_score"](self, job, candidates, options, stats)
    elif job.pbm is None:
        decoded = refs.decode(rows, minimum, maximum)
        if decoded is None:
            return _installed["_score"](self, job, candidates, options, stats)
        confidences = reverses = forwards = [0.0] * len(rows)
    else:
        result = refs.pbm(rows, minimum, maximum, *_unknown(job))
        if result is None:
            return _installed["_score"](self, job, candidates, options, stats)
        confidences, reverses, forwards = (v.tolist() for v in result[:3])
        decoded = result[3]
    self.counts["decoded"] += decoded
    self.counts["reused"] += len(rows) - decoded
    ranked = []
    for n, (row, (library, fcos, rcos)) in enumerate(candidates.items()):
        if similarity:
            mf, rmf, score = _fast.search_options.similarity_scores(fcos, rcos)
            if mf <= 0:
                continue
            ranked.append(((-mf, -rmf, row), row, library, fcos, rcos, (mf, rmf, score)))
        else:
            confidence = confidences[n]
            if confidence <= 0:
                continue
            ranked.append(((-confidence, -fcos, row), row, library, fcos, rcos,
                           (confidence, reverses[n], forwards[n])))
    ranked.sort(key=lambda item: item[0])

    def score_of(item):
        return item[5][2] if similarity else _fast.qual(item[5][0])

    sequential = options.mode == 'sequential'
    mins, constraints = options.library_min_scores, options.constraints
    seen, hits, head = set(), 0, len(ranked)
    stop_at, every, definite = None, False, False
    metadata = {}

    def meta(row):
        if row not in metadata:
            metadata[row] = eng.metadata(row)
        return metadata[row]

    for position, item in enumerate(ranked):
        row, library = item[1], item[2]
        score = score_of(item)
        if hits < options.max_hits and score >= mins.get(library, 0) and constraints.accepts(meta(row)):
            if options.dedupe:
                info = meta(row)
                key = info['cas'] or (info['name'].lower(), info['formula'])
                if key not in seen:
                    seen.add(key)
                    hits += 1
            else:
                hits += 1
        stopping = sequential and not definite and score >= max(options.stop_score, mins.get(library, 0))
        if stopping and constraints.accepts(meta(row)):
            every = True
            stop_at = position if stop_at is None else stop_at
            definite = row not in job.maybe
        decided = not sequential or definite or score < max(options.stop_score, mins.get(library, 0))
        if hits >= options.max_hits and decided:
            head = position + 1
            break
    if stop_at is not None:
        head = max(head, stop_at + 1)
    total = sum(query.values())
    masses = frozenset(query)
    scored = []
    for key, row, library, fcos, rcos, numbers in ranked[:head]:
        reference = refs.reference(row, minimum, maximum)
        if reference is None:                   # decoded a moment ago; cannot happen
            return _installed["_score"](self, job, candidates, options, stats)
        ref, positive = reference
        if similarity:
            mf, rmf, score = numbers
            values = dict(score=score, qual=score, mf=mf, rmf=rmf, confidence=round(mf / 9.99, 1),
                          forward=round(fcos ** 2 * 100, 1), reverse=round(rcos ** 2 * 100, 1))
        else:
            confidence, reverse_pbm, forward_pbm = numbers
            q = _fast.qual(confidence)
            values = dict(score=q, qual=q, confidence=round(confidence * 100, 1),
                          forward=round(forward_pbm * 100, 1), reverse=round(reverse_pbm * 100, 1))
        shared = [query[m] for m in sorted(masses & (ref.keys() if positive is None else positive))]
        values.update(coverage=round(sum(shared) / total * 100, 2), matched=len(shared))
        scored.append((key, row, ref, library, values))
    return scored, (every, definite if every else False)


# -- the normal search (``Engine.analyze``) ------------------------------------------------------

_query_terms: tuple = (None, None)


def _terms(query: dict) -> tuple:
    """A query's ions with the standard float32 / float64 terms (shared by its shards' threads)."""
    held, terms = _query_terms
    if held is query:
        return terms
    masses = list(query)
    terms = (np.array(masses, np.int64),
             np.array([np.float32(math.sqrt(i)) for i in query.values()], np.float32),
             np.array([np.float32((m / 100) ** 2) for m in masses], np.float32),
             np.array([i * (m / 100) ** 2 for m, i in query.items()], np.float64))
    globals()["_query_terms"] = (query, terms)
    return terms


def _engine_prefilter_shard(self, index, query, qnorm, minimum, maximum):
    """``Engine._prefilter_shard`` in Rust: the same forward and reverse arrays, bit for bit."""
    shard = self.shards[index]
    pointers, count = shard['pointers'], shard['count']
    if not count or not any(int(pointers[m + 1]) > int(pointers[m]) for m in query):
        return None
    refnorm = np.ascontiguousarray(self.shard_norms(index, minimum, maximum), np.float64)
    forward, reverse = _rust.prefilter_dense(np.asarray(pointers), np.asarray(shard['rows']),
                                             np.asarray(shard['intensities']), refnorm, *_terms(query), float(qnorm))
    return dict(start=shard['start'], source=shard['source'], forward=forward, reverse=reverse)


def _engine_score(self, parts, query, minimum, maximum, options, stats, presearch):
    """``Engine._score`` with the candidates decoded and PBM-scored in Rust: the same list."""
    candidates = {}
    for direction in ('forward', 'reverse'):
        if not parts:
            break
        similarity = np.concatenate([p[direction] for p in parts])
        offsets = np.cumsum([0] + [len(p[direction]) for p in parts])
        found = np.flatnonzero(similarity > 0)
        if len(found) > presearch:
            found = found[np.argpartition(-similarity[found], presearch)[:presearch]]
        for flat in found.tolist():
            n = int(np.searchsorted(offsets, flat, side='right')) - 1
            local = flat - int(offsets[n])
            candidates[parts[n]['start'] + local] = (parts[n], local)
    original = _installed["engine_score"]
    if not candidates or any(row >= self.native_count for row in candidates):      # MSP references
        return original(self, parts, query, minimum, maximum, options, stats, presearch)
    similarity = options.algorithm == 'similarity'
    refs = _refs(self, None if similarity else stats)
    rows = np.fromiter(candidates, np.int64, len(candidates))
    confidences = reverses = forwards = None
    unknown = None if similarity else _fast._percent(query)
    if unknown:
        peaks, weights = _fast._significant(unknown, stats)
        result = refs.pbm(rows, minimum, maximum,
                          *_unknown_arrays((unknown, peaks, weights, sum(weights[m] for m in peaks))))
        if result is None:
            return original(self, parts, query, minimum, maximum, options, stats, presearch)
        confidences, reverses, forwards = (v.tolist() for v in result[:3])
    elif refs.decode(rows, minimum, maximum) is None:          # (pbm_match of an empty unknown: 0)
        return original(self, parts, query, minimum, maximum, options, stats, presearch)
    total = sum(query.values())
    scored = []
    for n, (row, (part, local)) in enumerate(candidates.items()):
        fcos, rcos = float(part['forward'][local]), float(part['reverse'][local])
        if similarity:
            mf, rmf, score = _fast.search_options.similarity_scores(fcos, rcos)
            if mf <= 0:
                continue
            values = dict(score=score, qual=score, mf=mf, rmf=rmf, confidence=round(mf / 9.99, 1),
                          forward=round(fcos ** 2 * 100, 1), reverse=round(rcos ** 2 * 100, 1))
            key = (-mf, -rmf, row)
        else:
            confidence = confidences[n] if confidences is not None else 0.0
            if confidence <= 0:
                continue
            reverse_pbm, forward_pbm = reverses[n], forwards[n]
            values = dict(score=_fast.qual(confidence), qual=_fast.qual(confidence),
                          confidence=round(confidence * 100, 1),
                          forward=round(forward_pbm * 100, 1), reverse=round(reverse_pbm * 100, 1))
            key = (-confidence, -fcos, row)
        reference = refs.reference(row, minimum, maximum)
        if reference is None:                   # decoded a moment ago; cannot happen
            return original(self, parts, query, minimum, maximum, options, stats, presearch)
        ref = reference[0]
        shared = [i for m, i in query.items() if ref.get(m, 0) > 0]
        values.update(coverage=round(sum(shared) / total * 100, 2), matched=len(shared))
        scored.append((key, row, ref, part['source'], values))
    scored.sort(key=lambda item: item[0])
    return scored


# last: importing ``fast`` installs the kernels (its final lines call ``install``)
from gcws.libsearch import fast as _fast  # noqa: E402

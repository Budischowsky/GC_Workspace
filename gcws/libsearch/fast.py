"""Fast search: the library search of many peaks at once, with the standard search's hits.

The standard search (SpectrAtlas's ``Engine.analyze``, vendored) handles one spectrum at a time:

1. *Prefilter.* A forward and a reverse weighted cosine against every reference spectrum
   (inverted index; the whole library is read for every peak). The best ``PRESEARCH`` of each
   direction become the candidates.
2. *Scoring.* Every candidate is decoded from its library and scored exactly (PBM or the
   NIST-style similarity); the unknown's side of PBM is recomputed for every candidate.

Fast search computes the same two stages for a whole batch of peaks:

1. *Block screening.* The index is expanded block by block into dense matrices (a second thread
   prepares the next block) and multiplied with all peak spectra at once: float32 matrix products,
   BLAS on every core, so each library is read once per batch instead of once per peak. The
   rounding of those products is bounded (``_delta``). Per peak, a running floor (the k-th best
   screened cosine so far) drops every reference that cannot come within that bound of the top
   list; the few that remain are recomputed at the end with the standard engine's own arithmetic
   (the same float32 terms, the same float64 sums in the same order), so the candidate lists are
   the standard ones, bit for bit.
2. *Shared scoring.* The unknown's PBM side is computed once per peak. A reference's decoded
   spectrum and its PBM side (vectorised) are computed once and reused whenever it is a
   candidate again - neighbouring peaks and replicate runs share most of their candidates, and
   the cache outlives the batch. Every candidate gets its exact score; the full hit entries are
   built only for the head of the ranked list that the hit list can reach.
3. *Ties.* References with exactly equal cosines (the same spectrum in several libraries) can
   straddle a top-list boundary; the standard engine then keeps an arbitrary subset (numpy's
   partition). Fast search scores all of them. Should one of them reach a final hit list, or
   decide where a sequential search stops, that peak is searched again the standard way, so the
   answer is always the standard answer.

The hit lists themselves are assembled by the vendored ``Engine.analyze``: it runs on a view of
the engine whose two stages answer from the batch (``_View``).
"""
from __future__ import annotations

import math
import os
import queue
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from itertools import chain
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

import gcws.libsearch  # noqa: F401  (vendor on sys.path)
import engine as _atlas_engine        # vendored SpectrAtlas modules
import search_options
from agilent import AgilentLibrary as _AgilentLibrary, u16 as _u16
from msp import nominal_peaks
from shimadzu import ShimadzuLibrary as _ShimadzuBase
from pbm import (DEVIATION_PENALTY, FORWARD_WINDOW, MIN_ABUNDANCE, SIGNIFICANT_PEAKS, UNIQUENESS_WEIGHT, WINDOW,
                 _percent, _significant, qual)

#: fewer peaks than this are prefiltered one by one (the block pass has a fixed cost per batch)
MIN_BATCH = 8
#: reference spectra per dense block of the screening pass
BLOCK = 16384
#: peaks per matrix product (one large product beats narrower ones)
CHUNK = 512
#: decoded reference spectra kept for reuse (about 7 kB each)
REF_CACHE = 30000
#: threads for the element-wise passes of the screen (memory bound; numpy releases the GIL)
THREADS = max(1, min(4, (os.cpu_count() or 2) - 1))
#: float32 unit roundoff
UNIT = 2.0 ** -24
#: added to a screened reverse norm under a square root (keeps 0 / 0 at 0)
TINY = np.float32(1e-37)


def _delta(columns: int) -> float:
    """Bound on |screened - exact| for a forward or reverse cosine (both lie in [0, 1]).

    A float32 dot product of ``columns`` non-negative terms is off by at most
    ``columns * UNIT`` relative (any summation order, with or without FMA); the reverse cosine
    also carries the float32 reverse norm under a square root, the rest are a few roundings of
    the scale factors. Twice that, for safety."""
    return 2.0 * (1.5 * columns + 64) * UNIT * 1.01


@dataclass
class _Job:
    """One spectrum of the batch, prepared as ``Engine.analyze`` prepares it."""
    index: int
    spectrum: object
    query: dict = field(default_factory=dict)
    minimum: int = 0
    maximum: int = 0
    qnorm: float = 0.0
    pbm: Optional[tuple] = None          # the unknown's PBM side
    prepared: dict = field(default_factory=dict)   # group token -> scored candidates (their head)
    stops: dict = field(default_factory=dict)      # group token -> (stops, stops without tie rows)
    maybe: set = field(default_factory=set)        # rows the standard search might not score
    standard: bool = False               # search this spectrum the standard way
    active: bool = True
    settings: Optional[dict] = None      # the search settings of this spectrum (its own m/z range)


class _View:
    """``Engine`` as the vendored ``Engine.analyze`` sees it: stages 1 and 2 answer from the batch."""

    def __init__(self, engine, job: _Job, fallback):
        self._engine, self._job, self._fallback = engine, job, fallback

    def __getattr__(self, name):
        return getattr(self._engine, name)

    def _prefilter(self, shard_ids, query, minimum, maximum):
        return tuple(shard_ids)

    def _score(self, token, query, minimum, maximum, options, stats, presearch):
        scored = self._job.prepared.get(token)
        if scored is None:                  # not prepared (a stop decided otherwise): exact, now
            scored = self._fallback(list(token), self._job, options, stats, presearch)
        return scored


class FastSearch:
    """Searches spectra in batches; ``analyze(spectra)`` answers like ``Engine.analyze`` for each."""

    def __init__(self, engine, settings: dict, progress: Callable[[str], None] = lambda t: None,
                 cancelled: Callable[[], bool] = lambda: False):
        self.engine = engine
        self.settings = dict(settings or {})
        self.progress = progress
        self.cancelled = cancelled
        # decoded references outlive the batch: later searches over the same libraries reuse them
        cache = getattr(engine, "_fast_references", None)
        if cache is None:
            cache = OrderedDict()
            try:
                engine._fast_references = cache
            except AttributeError:
                pass
        self._refs: OrderedDict = cache
        self.counts = {"spectra": 0, "screened": 0, "standard": 0, "tie_boundaries": 0, "tie_fallbacks": 0,
                       "decoded": 0, "reused": 0}

    # -- public ---------------------------------------------------------------------------

    def analyze(self, spectra: list, done: Optional[Callable[[int, object], None]] = None,
                ranges: Optional[list] = None) -> list:
        """One result per spectrum (the dict of ``Engine.analyze``), or the exception it raised.

        ``done(index, result)`` is called as each spectrum is finished; after a cancel the
        unfinished ones stay None. ``ranges``: per spectrum ``(min_mz, max_mz)`` replacing the
        settings' range (None: the settings'); spectra of different ranges share the screen."""
        started = time.perf_counter()
        results: list = [None] * len(spectra)
        try:
            shared = self._shared()
        except Exception:  # noqa: BLE001 - invalid settings: every spectrum reports it the standard way
            shared = None
        jobs = []
        for n, spectrum in enumerate(spectra):
            job = _Job(n, spectrum)
            rng = ranges[n] if ranges is not None else None
            job.settings = self.settings if rng is None else \
                {**self.settings, "min_mz": rng[0], "max_mz": rng[1]}
            if shared is None:
                job.standard = True
            else:
                try:
                    self._prepare(job, shared)
                except Exception:  # noqa: BLE001 - the standard search raises the same error
                    job.standard = True
            jobs.append(job)
        finished = set()

        def complete(job):
            results[job.index] = self._finish(job)
            job.prepared, job.maybe = {}, set()        # the scored candidates are not needed any more
            finished.add(job.index)
            if done is not None:
                done(job.index, results[job.index])

        if shared is not None:
            options, groups, presearch, stats = shared
            for number, group in enumerate(groups):
                active = [j for j in jobs if j.active and not j.standard and j.index not in finished]
                if not active or self.cancelled():
                    break
                last = number == len(groups) - 1
                for job in self._stage(group, active, options, stats, presearch, number, len(groups)):
                    if options.mode == "sequential":
                        self._stop_check(job, group, options)
                    if last or not job.active or job.standard:
                        complete(job)
        for job in jobs:
            if self.cancelled():
                break
            if job.index not in finished:
                complete(job)
        self.counts["spectra"] += len(spectra)
        seconds = round((time.perf_counter() - started) / max(1, len(spectra)), 3)
        for r in results:
            if isinstance(r, dict):
                r["seconds"] = seconds          # this spectrum's share of the batch
        return results

    # -- preparation (as ``Engine.analyze`` does it) ----------------------------------------------

    def _shared(self):
        eng, settings = self.engine, self.settings
        available = {s['name'] for s in eng.sources if s['count'] > 0}
        libraries = settings.get('libraries')
        if libraries is None:
            libraries = sorted(available)
        if not isinstance(libraries, list) or any(not isinstance(s, str) for s in libraries):
            raise ValueError('Libraries must be a list of source names.')
        if not libraries or set(libraries) - available:
            raise ValueError('library selection')
        options = search_options.parse(settings, libraries)
        libraries = list(dict.fromkeys(libraries)) if options.mode == 'sequential' else sorted(set(libraries))
        stats = eng.statistics() if options.algorithm == 'pbm' else None
        filtering = options.constraints.active or any(options.library_min_scores.values())
        presearch = _atlas_engine.PRESEARCH * 5 if filtering else _atlas_engine.PRESEARCH
        if options.mode == 'sequential':
            groups = [[n for n, s in enumerate(eng.shards) if s['source'] == name] for name in libraries]
        else:
            groups = [[n for n, s in enumerate(eng.shards) if s['source'] in libraries]]
        return options, groups, presearch, stats

    def _prepare(self, job: _Job, shared) -> None:
        settings = self._settings(job)
        options, _groups, _presearch, stats = shared
        raw = nominal_peaks(job.spectrum.peaks)
        minimum = int(settings.get('min_mz') or min(raw))
        maximum = int(settings.get('max_mz') or max(raw))
        threshold = float(settings.get('threshold', 0))
        if not 1 <= minimum <= maximum <= 10000 or not math.isfinite(threshold) or not 0 <= threshold <= 20:
            raise ValueError('settings')
        if not _atlas_engine.ei_compatible(job.spectrum.metadata):
            raise ValueError('not EI')
        query = {m: i for m, i in raw.items() if minimum <= m <= maximum and i >= threshold}
        if not query:
            raise ValueError('no peaks')
        job.query, job.minimum, job.maximum = query, minimum, maximum
        job.qnorm = sum(i * (m / 100) ** 2 for m, i in query.items())
        if options.algorithm == 'pbm':
            unknown = _percent(query)
            if unknown:
                peaks, weights = _significant(unknown, stats)
                job.pbm = (unknown, peaks, weights, sum(weights[m] for m in peaks))

    # -- stage 1 + 2 for one group ------------------------------------------------------------

    def _stage(self, group, jobs, options, stats, presearch, number, groups):
        """Stage 1 for all ``jobs`` over ``group``, then stage 2 job by job (yielding each job)."""
        eng = self.engine
        postings = sum(np.diff(np.asarray(eng.shards[n]['pointers'])) for n in group)
        screened, alone = [], []
        for job in jobs:
            # The screen needs more than ``presearch`` references sharing an ion with the peak;
            # one ion whose postings alone exceed that guarantees it.
            longest = max((int(postings[m]) for m in job.query), default=0) if len(group) else 0
            (screened if longest > presearch else alone).append(job)
        if len(screened) < MIN_BATCH:
            alone, screened = alone + screened, []
        found = self._screen(group, screened, presearch, number, groups) if screened else {}
        total = len(jobs)
        for count, job in enumerate(jobs, 1):
            if self.cancelled():
                return
            if count % 25 == 0 or count == total:
                self.progress(f"Fast search: scoring {count} / {total}"
                              + (f" (library {number + 1} / {groups})" if groups > 1 else ""))
            selected = found.pop(job.index, None)
            if selected is None:            # prefiltered on its own, exactly as the standard search
                selected = (self._full(group, job, presearch), set())
            candidates, maybe = selected
            job.maybe |= maybe
            job.prepared[tuple(group)], job.stops[tuple(group)] = self._score(job, candidates, options, stats)
            yield job

    def _stop_check(self, job: _Job, group, options) -> None:
        """Sequential mode: the stop test of ``Engine.analyze``; a tie that decides it goes standard."""
        every, definite = job.stops[tuple(group)]
        if every != definite:
            job.standard = True
            job.active = False
            self.counts["tie_fallbacks"] += 1
        if every:
            job.active = False

    def _finish(self, job: _Job):
        eng = self.engine
        if not job.standard:
            try:
                result = _atlas_engine.Engine.analyze(_View(eng, job, self._fallback_score), job.spectrum,
                                                      dict(self._settings(job)), regional=False)
            except Exception as exc:  # noqa: BLE001
                return exc
            if not job.maybe or not any(hit['row'] in job.maybe for hit in result['hits']):
                self.counts["screened"] += 1
                return result
            self.counts["tie_fallbacks"] += 1
        # The standard search (its own prefilter per peak; scoring shares this batch's cache).
        self.counts["standard"] += 1
        job.prepared, job.maybe = {}, set()
        try:
            return _atlas_engine.Engine.analyze(_View(eng, job, self._fallback_score), job.spectrum,
                                                dict(self._settings(job)), regional=False) \
                if job.query else eng.analyze(job.spectrum, dict(self._settings(job)), regional=False)
        except Exception as exc:  # noqa: BLE001
            return exc

    def _settings(self, job: _Job) -> dict:
        return job.settings if job.settings is not None else self.settings

    def _fallback_score(self, group, job, options, stats, presearch):
        return self._score(job, self._full(group, job, presearch), options, stats)[0]

    # -- stage 1: exact candidates of one peak (the standard prefilter) ----------------------------

    def _full(self, group, job: _Job, presearch: int) -> dict:
        """``{row: (library, forward cosine, reverse cosine)}`` exactly as ``Engine._score`` selects."""
        eng = self.engine
        parts = eng._prefilter(group, job.query, job.minimum, job.maximum)
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
                part = parts[n]
                candidates[part['start'] + local] = (part['source'], float(part['forward'][local]),
                                                     float(part['reverse'][local]))
        return candidates

    # -- stage 1: block screening of many peaks ----------------------------------------------------

    def _screen(self, group, jobs, k, number, groups) -> dict:
        """``{job index: (candidates, maybe rows)}`` for ``jobs`` over the shards of ``group``."""
        eng = self.engine
        screen = _Screen(jobs, k)
        blocks = max(1, sum(-(-eng.shards[n]['count'] // BLOCK) for n in group))
        stop = threading.Event()
        # the next block is expanded while this one's element-wise passes run, not beside its
        # matrix products (they use every core)
        turn = threading.Semaphore(1)
        ready: queue.Queue = queue.Queue()
        free: queue.Queue = queue.Queue()
        for _ in range(2):          # double buffering: one block is expanded while the other is screened
            free.put((np.zeros((screen.C, BLOCK), np.float32), np.zeros((screen.C, BLOCK), np.float32)))
        producer = threading.Thread(target=self._expand, args=(group, screen.masses, screen.ranges, free,
                                                                 ready, stop, turn), daemon=True)
        producer.start()
        done = 0
        try:
            while True:
                item = ready.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                (dense, indicator), nr, isrs, refnorms, n, first = item
                if self.cancelled():
                    return {}
                done += 1
                if done % 10 == 1:
                    self.progress(f"Fast search: screening {len(jobs)} peaks against the libraries, "
                                  f"{100 * done // blocks} %"
                                  + (f" (library {number + 1} / {groups})" if groups > 1 else ""))
                screen.block(dense[:, :nr], indicator[:, :nr], isrs, refnorms, n, first, turn.release)
                free.put((dense, indicator))
        finally:
            stop.set()
            free.put(None)
            turn.release()
            producer.join()
            screen.close()
        out = {}
        for job, selected in zip(jobs, screen.finish(eng)):
            out[job.index] = selected
            self.counts["tie_boundaries"] += bool(selected and selected[1])
        return out

    def _expand(self, group, masses, ranges, free, ready, stop, turn) -> None:
        """Producer thread: each block of references as dense float32 sqrt(I) and 0/1 matrices over
        the batch's ions (rows) and the block's references (columns), with the references' norms
        (and their float32 inverse square roots) for each of the batch's m/z ``ranges``.

        The result does not depend on the order. Libraries are often sorted (by molecular weight),
        so a peak's best matches cluster; visiting the largest library first and its blocks in a
        fixed shuffled order lifts the floors early, and few pairs have to be kept."""
        eng = self.engine
        try:
            for n in sorted(group, key=lambda n: -eng.shards[n]['count']):
                shard = eng.shards[n]
                count = shard['count']
                if not count:
                    continue
                pointers, rows, intensities = shard['pointers'], shard['rows'], shard['intensities']
                refnorms, isrs = [], []
                for minimum, maximum in ranges:          # ascending: the norms resume
                    refnorm = eng.shard_norms(n, minimum, maximum)
                    isr = np.zeros(count, np.float32)
                    np.divide(1.0, np.sqrt(refnorm), out=isr, where=refnorm > 0, casting="unsafe")
                    refnorms.append(refnorm)
                    isrs.append(isr)
                starts = np.r_[np.arange(0, count, BLOCK), count]
                # the postings of the peaks' ions, in memory, split at the block starts
                spans = []
                for c, m in enumerate(masses):
                    a, b = int(pointers[m]), int(pointers[m + 1])
                    if b > a:
                        refs = np.array(rows[a:b])
                        spans.append((c, refs, np.sqrt(intensities[a:b]), np.searchsorted(refs, starts).tolist()))
                for block in np.random.default_rng(n).permutation(len(starts) - 1).tolist():
                    buffers = free.get()
                    if buffers is None or stop.is_set():
                        return
                    turn.acquire()
                    if stop.is_set():
                        return
                    dense, indicator = buffers
                    r0, r1 = int(starts[block]), int(starts[block + 1])
                    nr = r1 - r0
                    view = dense[:, :nr]
                    view.fill(0)
                    for c, refs, values, bounds in spans:
                        a, b = bounds[block], bounds[block + 1]
                        if b > a:
                            view[c, refs[a:b] - r0] = values[a:b]
                    np.greater(view, 0, out=indicator[:, :nr], casting="unsafe")      # 0.0 or 1.0
                    ready.put((buffers, nr, [isr[r0:r1] for isr in isrs], [rn[r0:r1] for rn in refnorms], n,
                               shard['start'] + r0))
            ready.put(None)
        except BaseException as exc:  # noqa: BLE001 - handed to the screening thread
            ready.put(exc)

    # -- stage 2: shared exact scoring -------------------------------------------------------------

    def _references(self, rows, minimum: int, maximum: int, stats) -> list:
        """(decoded spectrum in the range, its m/z with a positive value or None for all, PBM side)
        of each row: decoded once per batch, reused whenever the reference is a candidate again."""
        cache = self._refs
        while len(cache) > REF_CACHE:
            cache.popitem(last=False)
        keys = [(row, minimum, maximum) for row in rows]
        missing = [key for key in keys if key not in cache]
        decoded = dict(zip(missing, _decode_many(self.engine, [key[0] for key in missing], minimum, maximum)))
        for key in keys:
            if key in cache:
                cache.move_to_end(key)
                self.counts["reused"] += 1
            else:
                self.counts["decoded"] += 1
                ref, positive = decoded[key]
                cache[key] = (ref, positive, False)
        if stats is not None:
            todo = [key for key in keys if cache[key][2] is False]
            if todo:
                for key, side in zip(todo, _reference_sides([cache[key][0] for key in todo], stats)):
                    ref, positive, _ = cache[key]
                    cache[key] = (ref, positive, side)
        return [cache[key] for key in keys]

    def _score(self, job: _Job, candidates: dict, options, stats):
        """``Engine._score`` of the given candidates, value for value, as far as it is ever read.

        Every candidate gets its exact score and sort key. The full entries (reference spectrum,
        coverage) are built for the head of the sorted list only: ``Engine.analyze`` reads a
        group's entries in their order and stops at ``max_hits`` hits, and each acceptable entry
        is either a hit or a duplicate of one, so nothing after the ``max_hits``-th acceptable
        entry with a new compound can become a hit. In sequential mode the head also reaches the
        entry that decides the stop (scores fall along the list, so that is the first entry at or
        above the stop score that the constraints accept).

        Returns (head of the scored list, (stops, stops by rows no tie brought in))."""
        eng = self.engine
        query, minimum, maximum = job.query, job.minimum, job.maximum
        similarity = options.algorithm == 'similarity'
        references = self._references(list(candidates), minimum, maximum, None if similarity else stats)
        if not similarity:
            many = _pbm_many(job.pbm, [r[2] for r in references], stats)
            confidences, reverses, forwards = (v.tolist() for v in many)
        ranked = []
        for n, ((row, (library, fcos, rcos)), (ref, positive, side)) in enumerate(zip(candidates.items(),
                                                                                     references)):
            if similarity:
                mf, rmf, score = search_options.similarity_scores(fcos, rcos)
                if mf <= 0:
                    continue
                ranked.append(((-mf, -rmf, row), row, library, fcos, rcos, ref, positive, (mf, rmf, score)))
            else:
                confidence, reverse_pbm, forward_pbm = confidences[n], reverses[n], forwards[n]
                if confidence <= 0:
                    continue
                ranked.append(((-confidence, -fcos, row), row, library, fcos, rcos, ref, positive,
                               (confidence, reverse_pbm, forward_pbm)))
        ranked.sort(key=lambda item: item[0])

        def score_of(item):
            return item[7][2] if similarity else qual(item[7][0])

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
        for key, row, library, fcos, rcos, ref, positive, numbers in ranked[:head]:
            if similarity:
                mf, rmf, score = numbers
                values = dict(score=score, qual=score, mf=mf, rmf=rmf, confidence=round(mf / 9.99, 1),
                              forward=round(fcos ** 2 * 100, 1), reverse=round(rcos ** 2 * 100, 1))
            else:
                confidence, reverse_pbm, forward_pbm = numbers
                q = qual(confidence)
                values = dict(score=q, qual=q, confidence=round(confidence * 100, 1),
                              forward=round(forward_pbm * 100, 1), reverse=round(reverse_pbm * 100, 1))
            # query ions the reference also has (a positive value), in the query's ascending m/z order
            shared = [query[m] for m in sorted(masses & (ref.keys() if positive is None else positive))]
            values.update(coverage=round(sum(shared) / total * 100, 2), matched=len(shared))
            scored.append((key, row, ref, library, values))
        return scored, (every, definite if every else False)


class _Screen:
    """Screening state of one group of libraries for a batch of peaks.

    For every peak it keeps the (peak, reference) pairs that may be among the peak's ``k`` best in
    either direction, with the reference's float32 sqrt(I) at the peak's ions; the exact cosines
    are computed once, at the end, for the pairs still in the running.

    ``floor_f`` / ``floor_r`` are, per peak, the ``k``-th best screened cosine of some set of
    references. Those references have exact cosines of at least floor - delta, so the exact
    ``k``-th best T is too, and a reference whose exact cosine reaches T screens at no less than
    T - delta >= floor - 2 delta. Pairs below that bound can never be candidates.
    """

    def __init__(self, jobs, k: int):
        self.k = k
        self.nq = nq = len(jobs)
        # the batch's m/z ranges (ascending) and each peak's
        self.ranges = sorted({(job.minimum, job.maximum) for job in jobs})
        number = {r: g for g, r in enumerate(self.ranges)}
        self.range_of = np.array([number[(job.minimum, job.maximum)] for job in jobs], np.intp)
        self.masses = sorted({m for job in jobs for m in job.query})
        column = {m: c for c, m in enumerate(self.masses)}
        self.C = C = len(self.masses)
        # per peak, its ions in ascending m/z (the standard's order of summation)
        self.cols, self.si, self.w, self.iw = [], [], [], []
        qf = np.zeros((nq, C), np.float32)
        qr = np.zeros((nq, C), np.float32)
        for q, job in enumerate(jobs):
            cols = np.array([column[m] for m in job.query], np.intp)
            si = np.array([np.float32(math.sqrt(i)) for i in job.query.values()], np.float32)
            w = np.array([np.float32((m / 100) ** 2) for m in job.query], np.float32)
            iw = np.array([i * (m / 100) ** 2 for m, i in job.query.items()], np.float64)
            self.cols.append(cols)
            self.si.append(si[:, None])
            self.w.append(w[:, None])
            self.iw.append(iw[:, None])
            qf[q, cols] = si * w
            qr[q, cols] = iw
        self.qnorm = np.array([job.qnorm for job in jobs], np.float64)
        sq = np.sqrt(self.qnorm).astype(np.float32)
        self.delta = _delta(C)
        # peaks with a similar m/z extent share a matrix product over their columns only
        low = np.array([c[0] for c in self.cols])
        high = np.array([c[-1] + 1 for c in self.cols])
        order = np.argsort(high, kind="stable")
        self.chunks = []
        for s in range(0, nq, CHUNK):
            qs = order[s:s + CHUNK]
            c0, c1 = int(low[qs].min()), int(high[qs].max())
            g = self.range_of[qs]
            # one range: its scale broadcasts over the rows; several: each row takes its own
            g = int(g[0]) if (g == g[0]).all() else g
            self.chunks.append((qs, c0, c1, np.ascontiguousarray(qf[qs, c0:c1]),
                                np.ascontiguousarray(qr[qs, c0:c1]), sq[qs], g))
        rows = min(CHUNK, nq)
        self.buffers = [np.empty((rows, BLOCK), np.float32) for _ in range(4)] + \
            [np.empty((rows, BLOCK), bool) for _ in range(2)]
        # the element-wise passes over a block run on THREADS row slices at once
        self.pool = ThreadPoolExecutor(THREADS)
        self.rows = -(-rows // THREADS)
        self.floor_f = np.full(nq, -np.inf)
        self.floor_r = np.full(nq, -np.inf)
        # the k best screened cosines seen so far per peak; their minimum is a floor
        self.top_f = np.full((nq, k), -np.inf, np.float32)
        self.top_r = np.full((nq, k), -np.inf, np.float32)
        self.pools = [[] for _ in range(nq)]
        self.sizes = np.zeros(nq, np.int64)
        self.pruned = np.zeros(nq, np.int64)

    def block(self, dense, indicator, isrs, refnorms, shard: int, first_row: int,
              multiplied: Callable[[], None] = lambda: None) -> None:
        """Screen one block of references (dense rows = the batch's ions, columns = references);
        ``isrs`` / ``refnorms``: the block's inverse root norms and norms, one per range of
        ``ranges``. ``multiplied`` is called once the first matrix products are done."""
        nr = dense.shape[1]
        two = 2 * self.delta
        stacked = None
        for number, (qs, c0, c1, qf_c, qr_c, sq_c, g) in enumerate(self.chunks):
            nc = len(qs)
            full = nr == BLOCK
            dots, rnorm, product, bound = (b[:nc, :nr] for b in self.buffers[:4])
            passed, rev = (b[:nc, :nr] for b in self.buffers[4:])
            if full:
                np.matmul(qf_c, dense[c0:c1], out=dots)          # screened dot products
                np.matmul(qr_c, indicator[c0:c1], out=rnorm)     # screened reverse norms
            else:
                dots[...] = qf_c @ dense[c0:c1]
                rnorm[...] = qr_c @ indicator[c0:c1]
            if number == 0:
                multiplied()
            parts = [(a, min(a + self.rows, nc)) for a in range(0, nc, self.rows)]
            # = forward cosine * sqrt(qnorm); memory-bound passes run on row slices in parallel
            if isinstance(g, int):
                isr = isrs[g]
                list(self.pool.map(lambda ab: np.multiply(dots[ab[0]:ab[1]], isr, out=dots[ab[0]:ab[1]]), parts))
            else:
                if stacked is None:
                    stacked = np.stack(isrs)
                list(self.pool.map(lambda ab: np.multiply(dots[ab[0]:ab[1]], stacked[g[ab[0]:ab[1]]],
                                                          out=dots[ab[0]:ab[1]]), parts))
            cold = ~np.isfinite(self.floor_f[qs]) | ~np.isfinite(self.floor_r[qs])
            if nr > self.k and cold.any():
                self._warm(qs[cold], dots[cold], rnorm[cold], sq_c[cold])
            low_f = np.maximum(self.floor_f[qs] - two, 0).astype(np.float32)
            low_r = np.maximum(self.floor_r[qs] - two, 0).astype(np.float32)
            limit_f = (low_f * sq_c)[:, None]
            limit_r = (low_r * low_r)[:, None]

            def select(ab):
                # forward >= low_f, reverse >= low_r (reverse^2 = dots^2 / rnorm); strict, so a
                # reference without a shared ion (dots 0) never passes
                a, b = ab
                np.greater(dots[a:b], limit_f[a:b], out=passed[a:b])
                np.multiply(dots[a:b], dots[a:b], out=product[a:b])
                np.multiply(rnorm[a:b], limit_r[a:b], out=bound[a:b])
                np.greater(product[a:b], bound[a:b], out=rev[a:b])
                np.logical_or(passed[a:b], rev[a:b], out=passed[a:b])
                pq, pr = np.nonzero(passed[a:b])
                return pq + a, pr
            found = list(self.pool.map(select, parts))
            pq = np.concatenate([f[0] for f in found])
            pr = np.concatenate([f[1] for f in found])
            if not len(pq):
                continue
            d = dots[pq, pr]
            af = d / sq_c[pq]
            ar = d / np.sqrt(rnorm[pq, pr])
            _raise(qs, pq, af, self.top_f, self.floor_f)
            _raise(qs, pq, ar, self.top_r, self.floor_r)
            bounds = np.searchsorted(pq, np.arange(nc + 1)).tolist()
            for a in range(nc):
                s0, s1 = bounds[a], bounds[a + 1]
                if s1 == s0:
                    continue
                q = int(qs[a])
                r = pr[s0:s1]
                self.pools[q].append((first_row + r, af[s0:s1], ar[s0:s1], refnorms[self.range_of[q]][r],
                                      np.full(s1 - s0, shard, np.int32), dense[np.ix_(self.cols[q], r)]))
                self.sizes[q] += s1 - s0
                if self.sizes[q] > max(8 * self.k, 2 * self.pruned[q]):
                    self._prune(q)

    def _warm(self, qs, dots, rnorm, sq) -> None:
        """The k-th best screened cosine of one block: a first floor for peaks that have none."""
        nr, k = dots.shape[1], self.k
        for values, floor in ((dots / sq[:, None], self.floor_f),
                              (dots / np.sqrt(rnorm + TINY), self.floor_r)):
            kth = np.partition(values, nr - k, axis=1)[:, nr - k].astype(np.float64)
            ok = kth > 0
            floor[qs[ok]] = np.maximum(floor[qs[ok]], kth[ok])

    def _prune(self, q: int):
        """Merge peak ``q``'s pairs and drop those below its floors (never a candidate)."""
        parts = self.pools[q]
        if not parts:
            return None
        rows, af, ar, refn, shard = (np.concatenate([p[i] for p in parts]) for i in range(5))
        values = np.concatenate([p[5] for p in parts], axis=1)
        two = 2 * self.delta
        keep = (af >= self.floor_f[q] - two) | (ar >= self.floor_r[q] - two)
        if not keep.all():
            rows, af, ar, refn, shard, values = rows[keep], af[keep], ar[keep], refn[keep], shard[keep], \
                values[:, keep]
        self.pools[q] = [(rows, af, ar, refn, shard, values)]
        self.sizes[q] = self.pruned[q] = len(rows)
        return self.pools[q][0]

    def close(self) -> None:
        self.pool.shutdown(wait=True)

    def finish(self, eng) -> list:
        """Per peak: (candidates, maybe rows), or None when the peak must be searched the standard way."""
        self.close()
        sources = [s['source'] for s in eng.shards]
        out = []
        for q in range(self.nq):
            pool = self._prune(q)
            if pool is None:
                out.append(None)
                continue
            rows, _af, _ar, refnorm, shard, sqrt_i = pool
            # The standard terms: float32 sqrt(I) * float32 sqrt(i), times float32 weight, summed in
            # float64 over the peak's ions in ascending m/z (``Engine._prefilter_shard``).
            terms = sqrt_i * self.si[q]
            terms *= self.w[q]
            dots = np.cumsum(terms, axis=0, dtype=np.float64)[-1]
            reverse_norm = np.cumsum((sqrt_i > 0) * self.iw[q], axis=0)[-1]
            forward = np.divide(dots, np.sqrt(self.qnorm[q] * refnorm), out=np.zeros_like(dots),
                                where=refnorm > 0)
            reverse = np.divide(dots, np.sqrt(reverse_norm * refnorm), out=np.zeros_like(dots),
                                where=(reverse_norm * refnorm) > 0)
            out.append(_select(rows, forward, reverse, [sources[i] for i in shard.tolist()], self.k))
        return out


def _raise(qs, pq, values, top, floor) -> None:
    """Merge a block's screened values (``pq``: index into ``qs``, ascending) into the peaks'
    running k best; a full list's minimum raises the peak's floor."""
    counts = np.bincount(pq, minlength=len(qs))
    have = np.flatnonzero(counts)
    width = int(counts.max())
    rank = np.arange(len(pq)) - (np.cumsum(counts) - counts)[pq]
    new = np.full((len(qs), width), -np.inf, np.float32)
    new[pq, rank] = values
    peaks = qs[have]
    best = np.partition(np.concatenate([top[peaks], new[have]], axis=1), width, axis=1)[:, width:]
    top[peaks] = best
    floor[peaks] = np.maximum(floor[peaks], best.min(axis=1))


def _select(rows, forward, reverse, sources, k: int):
    """The standard selection from exact cosines: the k best of each direction, and the rows only
    a tie at the boundary brings in. None if the pool cannot hold the k best (never expected)."""
    chosen, definite = set(), set()
    for values in (forward, reverse):
        if int((values > 0).sum()) < k:
            return None
        kth = float(np.partition(values, len(values) - k)[len(values) - k])
        above = set(rows[values > kth].tolist())
        tied = set(rows[values == kth].tolist())
        chosen |= above | tied
        definite |= above | (tied if len(above) + len(tied) == k else set())
    position = {r: n for n, r in enumerate(rows.tolist())}
    candidates = {r: (sources[position[r]], float(forward[position[r]]), float(reverse[position[r]]))
                  for r in sorted(chosen)}
    return candidates, chosen - definite


def _peak_arrays(engine, row) -> tuple:
    """``engine.peaks(row)`` as two float64 arrays: the same values in the same order (every
    reader yields integers or float64 values). Agilent and Shimadzu records are read without the
    tuple list, the others through ``peaks``."""
    if row < engine.native_count:
        reader, local = engine.native_row(row)
        if isinstance(reader, _AgilentLibrary):
            offset = reader.scan_offsets[local]
            values = np.frombuffer(reader.data, dtype='>u2', count=_u16(reader.data, offset + 12) * 2,
                                   offset=offset + 18).reshape(-1, 2)
            raw = values[:, 1].astype(np.uint32)
            return values[:, 0] / 20, ((raw & 16383) * (8 ** (raw >> 14))).astype(np.float64)
        if isinstance(reader, _ShimadzuBase):
            masses, intensities = reader.decode(local)
            return np.array(masses, np.float64), np.array(intensities, np.float64)
    peaks = engine.peaks(row)
    return (np.fromiter((m for m, _i in peaks), np.float64, len(peaks)),
            np.fromiter((i for _m, i in peaks), np.float64, len(peaks)))


def _decode_many(engine, rows: list, minimum: int, maximum: int) -> list:
    """(spectrum in the range, its m/z with a positive value or None for all) of each row:

    ``{m: i for m, i in nominal_peaks(engine.peaks(row)).items() if minimum <= m <= maximum}``
    for all rows at once, value for value: ``int(math.floor(mz + 0.5))`` binning, the sum per
    nominal mass in peak order starting from 0.0 (``np.add.at`` adds in index order), the base
    over all nominal masses, ``100.0 * i / base``, ascending m/z. A row ``nominal_peaks`` refuses
    (nothing left, a zero base) is decoded the standard way, which raises its error."""
    out = [None] * len(rows)
    if not rows:
        return out
    arrays = [_peak_arrays(engine, row) for row in rows]
    lengths = np.fromiter((len(a[0]) for a in arrays), np.int64, len(arrays))
    mz = np.concatenate([a[0] for a in arrays]) if lengths.sum() else np.zeros(0)
    intensity = np.concatenate([a[1] for a in arrays]) if lengths.sum() else np.zeros(0)
    seg = np.repeat(np.arange(len(rows)), lengths)
    mass = np.floor(mz + 0.5)
    keep = mass > 0
    mass, intensity, seg = mass[keep].astype(np.int64), intensity[keep], seg[keep]
    keys, inverse = np.unique(seg * 1_000_000 + mass, return_inverse=True)
    sums = np.zeros(len(keys))
    np.add.at(sums, inverse, intensity)
    useg, umass = keys // 1_000_000, keys % 1_000_000
    counts = np.bincount(useg, minlength=len(rows))
    starts = np.cumsum(counts) - counts
    base = np.zeros(len(rows))
    have = counts > 0
    base[have] = np.maximum.reduceat(sums, starts[have])
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100.0 * sums / base[useg]
    inside = (umass >= minimum) & (umass <= maximum)
    fseg = useg[inside]
    fcounts = np.bincount(fseg, minlength=len(rows))
    fstarts = (np.cumsum(fcounts) - fcounts).tolist()
    nonpositive = np.bincount(fseg[~(value[inside] > 0)], minlength=len(rows)).tolist()
    m_list, v_list, fcounts = umass[inside].tolist(), value[inside].tolist(), fcounts.tolist()
    usable = (have & (base > 0)).tolist()
    for n, row in enumerate(rows):
        if not usable[n]:
            ref = {m: i for m, i in nominal_peaks(engine.peaks(row)).items() if minimum <= m <= maximum}
        else:
            a = fstarts[n]
            ref = dict(zip(m_list[a:a + fcounts[n]], v_list[a:a + fcounts[n]]))
            if not nonpositive[n]:
                out[n] = (ref, None)
                continue
        out[n] = (ref, None if all(i > 0 for i in ref.values()) else frozenset(m for m, i in ref.items() if i > 0))
    return out


def _reference_sides(refs: list, stats) -> list:
    """The reference side of ``pbm.pbm_match`` for many decoded spectra at once.

    ``pbm._percent`` and ``pbm._significant`` vectorised with the same float64 operations: per
    spectrum (the largest value, whose percent is 100, None for the significant peaks' tuple, base
    m/z, attainable bits, the significant peaks (m/z, weight, percent) as rows of an array, every
    m/z from 1 % with its percent as rows of an array), or None when nothing is left from 1 %.
    ``_pbm`` takes the significant peaks from the array."""
    n = len(refs)
    lengths = np.fromiter((len(r) for r in refs), np.int64, n)
    total = int(lengths.sum())
    out = [None] * n
    if not total:
        return out
    masses = np.fromiter(chain.from_iterable(refs), np.int64, total)
    values = np.fromiter(chain.from_iterable(r.values() for r in refs), np.float64, total)
    seg = np.repeat(np.arange(n), lengths)
    starts = np.cumsum(lengths) - lengths
    base = np.zeros(n)
    filled = lengths > 0
    base[filled] = np.maximum.reduceat(values, starts[filled])
    # _percent: 100 * i / base from MIN_ABUNDANCE on; nothing when the base is not positive
    with np.errstate(divide="ignore", invalid="ignore"):
        percent = 100.0 * values / base[seg]
    keep = (base[seg] > 0) & (percent >= MIN_ABUNDANCE)
    masses, percent, seg = masses[keep], percent[keep], seg[keep]
    if not len(seg):
        return out
    kept = np.bincount(seg, minlength=n)
    first = np.cumsum(kept) - kept
    # _significant: weight = UNIQUENESS_WEIGHT * u(m) + a(percent), best first, ties by m/z
    u = stats.uniqueness[np.minimum(masses, len(stats.uniqueness) - 1)]
    a = stats.abundance[np.clip(percent.astype(np.int64), 1, 100)]
    weight = UNIQUENESS_WEIGHT * u + a
    order = np.lexsort((masses, -weight, seg))
    rank = np.arange(len(order)) - first[seg[order]]
    chosen = rank < SIGNIFICANT_PEAKS
    top, top_rank = order[chosen], rank[chosen]
    top_seg = seg[top]
    # base peak: the first (lowest m/z) of the largest percent values
    largest = np.zeros(n)
    has = kept > 0
    largest[has] = np.maximum.reduceat(percent, first[has])
    at_max = np.flatnonzero(percent == largest[seg])
    segs, where = np.unique(seg[at_max], return_index=True)
    base_mz = np.zeros(n, np.int64)
    base_mz[segs] = masses[at_max[where]]
    # attainable bits as pbm sums them with Python's sum(): compensated, in the significant
    # peaks' order (a position a spectrum lacks adds nothing)
    w = np.zeros((n, SIGNIFICANT_PEAKS))
    on = np.zeros((n, SIGNIFICANT_PEAKS), bool)
    w[top_seg, top_rank] = weight[top]
    on[top_seg, top_rank] = True
    attainable, comp = np.zeros(n), np.zeros(n)
    for j in range(SIGNIFICANT_PEAKS):
        x, counts = w[:, j], on[:, j]
        t = attainable + x
        step = np.where(np.abs(attainable) >= np.abs(x), (attainable - t) + x, (x - t) + attainable)
        comp = np.where(counts, comp + step, comp)
        attainable = np.where(counts, t, attainable)
    attainable = np.where((comp != 0) & np.isfinite(comp), attainable + comp, attainable).tolist()
    # the significant peaks (m/z, weight, percent) and every m/z from 1 % with its percent
    top_rows = np.stack([masses[top].astype(np.float64), weight[top], percent[top]])
    all_rows = np.stack([masses.astype(np.float64), percent])
    top_first = np.searchsorted(top_seg, np.arange(n + 1)).tolist()
    first_list, kept_list, base_list, base_mz_list = first.tolist(), kept.tolist(), base.tolist(), base_mz.tolist()
    for s in np.flatnonzero(has).tolist():
        a, b, f = top_first[s], top_first[s + 1], first_list[s]
        out[s] = (base_list[s], None, base_mz_list[s], attainable[s], top_rows[:, a:b],
                  all_rows[:, f:f + kept_list[s]])
    return out


def _pbm(unknown_side, ref, reference_side, stats):
    """``pbm.pbm_match`` with both sides precomputed: the same operations in the same order.

    ``ref`` is the decoded reference in the search range; its percent value at m/z m,
    ``100.0 * ref[m] / largest`` when that reaches ``MIN_ABUNDANCE``, is ``pbm._percent``'s."""
    if unknown_side is None or reference_side is None:
        return 0.0, 0.0, 0.0
    unknown, qpeaks, qweights, qattainable = unknown_side
    largest, rpeaks, base, rattainable = reference_side[:4]
    if rpeaks is None:                  # (m/z, weight, percent) of the significant peaks, in order
        rows = reference_side[4]
        rpeaks = zip(rows[0].astype(np.int64).tolist(), rows[1].tolist(), rows[2].tolist())
    dilution = min(1.0, unknown.get(base, 0.0) / 100.0)
    if dilution <= 0 or rattainable <= 0:
        reverse = 0.0
    else:
        bits = 0.0
        for m, weight, value in rpeaks:
            expected, found = value * dilution, unknown.get(m, 0.0)
            if found >= expected / WINDOW:
                bits += weight
            elif found > 0:
                bits += max(0.0, weight - DEVIATION_PENALTY * (stats.a(expected) - stats.a(found)))
        bits -= math.log2(1 / dilution)
        reverse = max(0.0, bits / rattainable)
    if qattainable <= 0:
        forward = 0.0
    else:
        # reference.get(m, 0.0) >= unknown[m] / 2; unknown[m] >= 1, so a missing peak fails
        forward = sum(qweights[m] for m in qpeaks
                      if (value := ref.get(m)) is not None and (percent := 100.0 * value / largest) >= MIN_ABUNDANCE
                      and percent >= unknown[m] / FORWARD_WINDOW) / qattainable
    return (reverse + forward) / 2, reverse, forward


def _pbm_many(unknown_side, sides: list, stats):
    """:func:`_pbm` of one unknown against many references: (confidence, reverse, forward) arrays.

    Value for value the scalar function: every candidate goes through the same float64 operations
    in the same order. The reverse bits are accumulated peak position by peak position (a missing
    position adds 0.0, which changes nothing); the forward sum repeats the compensated summation
    of Python's ``sum()`` step by step; the dilution logarithm is ``math.log2``. A reference's
    percent value at an m/z is the one :func:`_reference_sides` computed, with the same rule
    (only values from ``MIN_ABUNDANCE`` exist), so the forward lookups need no decoded spectrum."""
    n = len(sides)
    confidence, reverse, forward = np.zeros(n), np.zeros(n), np.zeros(n)
    if unknown_side is None or not n:
        return confidence, reverse, forward
    unknown, qpeaks, qweights, qattainable = unknown_side
    use = [i for i, side in enumerate(sides) if side is not None]
    if not use:
        return confidence, reverse, forward
    sel = [sides[i] for i in use]
    k = len(sel)
    rattainable = np.fromiter((side[3] for side in sel), np.float64, k)
    dense = np.zeros(10002)
    for m, v in unknown.items():
        if 0 <= m <= 10001:
            dense[m] = v
    abundance = stats.abundance
    rows = np.arange(k)

    # reverse: the reference's significant peaks in the diluted unknown
    base_mz = np.fromiter((side[2] for side in sel), np.int64, k)
    dilution = np.minimum(1.0, dense[np.clip(base_mz, 0, 10001)] / 100.0)
    ok = (dilution > 0) & (rattainable > 0)
    top = [side[4] for side in sel]
    lengths = np.fromiter((t.shape[1] for t in top), np.int64, k)
    width = int(lengths.max())
    rev = np.zeros(k)
    if width and ok.any():
        flat_rows = np.repeat(rows, lengths)
        cols = np.arange(int(lengths.sum())) - np.repeat(np.cumsum(lengths) - lengths, lengths)
        mz, weight, value = np.concatenate(top, axis=1)
        expected = value * dilution[flat_rows]
        found = dense[mz.astype(np.int64)]
        partial = np.maximum(0.0, weight - DEVIATION_PENALTY * (
            abundance[np.clip(expected.astype(np.int64), 1, 100)] - abundance[np.clip(found.astype(np.int64), 1, 100)]))
        add = np.zeros((k, width))
        add[flat_rows, cols] = np.where(found >= expected / WINDOW, weight, np.where(found > 0, partial, 0.0))
        bits = np.add.accumulate(add, axis=1)[:, -1]          # left to right, as the scalar loop
        at = np.flatnonzero(ok)
        bits[at] -= np.array([math.log2(1 / d) for d in dilution[at].tolist()])
        with np.errstate(divide="ignore", invalid="ignore"):
            rev = np.where(ok, np.maximum(0.0, bits / np.where(ok, rattainable, 1.0)), 0.0)
    # forward: the unknown's significant ions explained by the reference (compensated sum)
    fwd = np.zeros(k)
    if qattainable > 0 and qpeaks:
        every = [side[5] for side in sel]
        sizes = np.fromiter((e.shape[1] for e in every), np.int64, k)
        masses, percents = np.concatenate(every, axis=1)
        column = np.full(10002, -1, np.int64)                      # m/z -> position in qpeaks
        column[np.asarray(qpeaks, dtype=np.int64)] = np.arange(len(qpeaks))
        where = column[masses.astype(np.int64)]
        inside = np.flatnonzero(where >= 0)
        percent = np.full((k, len(qpeaks)), np.nan)              # nan: the reference lacks the m/z
        percent[np.repeat(rows, sizes)[inside], where[inside]] = percents[inside]
        limits = np.array([unknown[m] / FORWARD_WINDOW for m in qpeaks])
        with np.errstate(invalid="ignore"):
            counts = (percent >= MIN_ABUNDANCE) & (percent >= limits[None, :])
        total, comp = np.zeros(k), np.zeros(k)
        for j, m in enumerate(qpeaks):
            x, c = qweights[m], counts[:, j]
            t = total + x
            step = np.where(total >= abs(x), (total - t) + x, (x - t) + total)   # total >= 0
            comp = np.where(c, comp + step, comp)
            total = np.where(c, t, total)
        total = np.where((comp != 0) & np.isfinite(comp), total + comp, total)
        fwd = total / qattainable
    reverse[use], forward[use] = rev, fwd
    confidence[use] = (rev + fwd) / 2
    return confidence, reverse, forward

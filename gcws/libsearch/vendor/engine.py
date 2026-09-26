"""Local, sparse, exhaustive nominal-mass EI library search."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from functools import lru_cache, partial
import math
import struct
import time
import numpy as np
from agilent import AgilentLibrary
from shimadzu import ShimadzuLibrary
from nist import NistLibrary, RECORD_FILES
from spectral_index import open_index
from msp import nominal_peaks
import msp_cache
import search_options
from chemistry import family_tags, interpret, candidate_evidence
from pbm import PeakStatistics, pbm_match, qual, MIN_ABUNDANCE

VERSION = '2.0.0'

# Vendor programs are often unpacked beside the libraries; their sample and
# demonstration spectra are deliberately not searched as references.
PROGRAM_FOLDER = 'software'


def ei_compatible(meta):
    ionization = ' '.join(meta.get(k, '') for k in ('ionization', 'ionization mode', 'ionmode', 'ion mode', 'instrumenttype', 'instrument type'))
    if any(token in ionization.lower() for token in ('electrospray', 'esi', 'apci', 'maldi')):
        return False
    mslevel = meta.get('mslevel', meta.get('ms level', ''))
    return mslevel not in ('2', '3', 'MS2', 'MS3')


# Candidates per direction (forward and reverse cosine) re-scored with the exact PBM.
PRESEARCH = 300
# Stage 1 is memory bound; more than a few threads only compete for bandwidth.
PREFILTER_THREADS = max(1, min(4, (os.cpu_count() or 2) - 1))

SIMILARITY_NOTE = ('Weighted dot-product similarity (NIST MS Search style): match factor MF and reverse match factor RMF '
                   '0-999 over sqrt(abundance) x m/z; the score is MF/10 (0-99). Labels and retention times are not search inputs.')


class Engine:
    def __init__(self, root, progress=lambda text: None, library_dirs=None, loading=None):
        self.root = Path(root)
        self.native, self.custom, self.shards = [], [], []
        self.library_warnings, self.sources = [], []
        self.count = self.native_count = 0
        self._sqrt = {}
        # ``Library`` in the project plus any folder the user pointed Atlas at
        # (``library_dirs``, e.g. a share or a NIST MS Search installation).
        self.library_dirs = [self.root / 'Library']
        for folder in library_dirs or ():
            folder = Path(folder)
            if not folder.is_dir():
                self.library_warnings.append(f'Library folder not found: {folder}')
            elif all(folder.resolve() != d.resolve() for d in self.library_dirs):
                self.library_dirs.append(folder)
        supplied = [p for p in self.root.rglob('*') if p.is_file() and not self.program_files(p)]
        for folder in self.library_dirs[1:]:
            if self.root not in folder.resolve().parents:
                supplied += [p for p in folder.rglob('*') if p.is_file() and not self.program_files(p)]
        folders = sorted({p.parent for p in supplied if p.name.lower() == 'header.ind'
                          and (p.parent.parent == self.root or self.in_library(p))})
        nist_folders = sorted({p.parent for p in supplied if p.name.lower() in RECORD_FILES
                               and self.in_library(p)})
        paths = [(p, AgilentLibrary, 'Agilent / ChemStation EI') for p in folders]
        paths += [(p, ShimadzuLibrary, 'Wiley / Shimadzu EI') for p in sorted(p for p in supplied if p.suffix.lower() == '.lib' and self.in_library(p))]
        paths += [(p, partial(NistLibrary, cache=self.root / '.cache'), 'NIST MS Search EI') for p in nist_folders]
        custom_files = set((self.root / 'libraries').rglob('*.msp'))
        for folder in self.library_dirs:
            custom_files |= set(folder.rglob('*.msp'))
        custom_files = [p for p in sorted(custom_files) if not self.program_files(p)]
        total = len(paths) + len(custom_files)
        step = 0

        def loaded(label):
            nonlocal step
            step += 1
            if loading:
                loading(step, total, label)

        for path, reader_type, kind in paths:
            label = self.label(path)
            if loading:
                loading(step, total, label)
            try:
                reader = reader_type(path)
                arrays = open_index(self.root, reader.files, lambda: reader.records(progress), reader.count, progress, reader)
                self.native.append((self.count, reader))
                self.shards.append(dict(start=self.count, source=label, count=reader.count, **arrays))
                self.sources.append(dict(name=label, count=reader.count, kind=kind, status='Ready' if reader.count else 'Empty library'))
                skipped = getattr(reader, 'skipped', [])
                if skipped:
                    self.sources[-1]['skipped'] = len(skipped)
                    self.library_warnings.append(f'{label}: skipped {len(skipped):,} records that failed decoding validation.')
                self.count += reader.count
            except (ValueError, OSError, KeyError, IndexError, struct.error) as error:
                self.library_warnings.append(f'{label}: {error}')
                self.sources.append(dict(name=label, count=0, kind=kind, status='Unavailable', error=str(error)))
            loaded(label)
        self.native_count = self.count
        self.agilent = next((r for _, r in self.native if isinstance(r, AgilentLibrary)), None)
        for path in custom_files:
            label = self.label(path)
            if loading:
                loading(step, total, label)
            try:
                records = msp_cache.load(self.root, path, progress)
                personal = 'personal' in Path(label).parts
                kept = [n for n, meta in enumerate(records.meta) if ei_compatible(meta)
                        and (meta.get('reference status', 'Unverified' if personal else 'Confirmed reference') == 'Confirmed reference')]
                if personal:
                    label = records.meta[0].get('library name', path.parent.name) if records.count else path.parent.name
                elif len(kept) != records.count:
                    self.library_warnings.append(f'{label}: skipped {records.count-len(kept)} explicitly non-EI/MS2 records.')
                arrays = open_index(self.root, [path], lambda: (records.arrays(n) for n in kept), len(kept), progress)
                self.shards.append(dict(start=self.count, source=label, count=len(kept), **arrays))
                self.custom.extend((label, records, n) for n in kept)
                existing = next((x for x in self.sources if personal and x['kind'] == 'Personal MSP library' and x['name'] == label), None)
                if existing:
                    existing['count'] += len(kept)
                    existing['stored'] += records.count
                else:
                    self.sources.append(dict(name=label, count=len(kept), stored=records.count, kind='Personal MSP library' if personal else 'MSP references', status='Ready'))
                self.count += len(kept)
            except (ValueError, OSError) as error:
                self.library_warnings.append(f'{label}: {error}')
                self.sources.append(dict(name=label, count=0, kind='MSP references', status='Unavailable', error=str(error)))
            loaded(label)
        progress(f'Ready · {self.count:,} reference spectra')

    def in_library(self, path):
        return any(folder in path.parents for folder in self.library_dirs)

    def label(self, path):
        """``Library\\mainlib`` in the project; ``<folder name>\\…`` outside it."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            for folder in self.library_dirs[1:]:
                if folder in path.parents or path == folder:
                    return str(Path(folder.name) / path.relative_to(folder))
            return str(path)

    def program_files(self, path):
        relative = Path(self.label(path))
        return PROGRAM_FOLDER in (part.lower() for part in relative.parts)

    def native_row(self, row):
        for start, reader in reversed(self.native):
            if start <= row < start + reader.count:
                return reader, row - start
        raise IndexError('Reference row out of range.')

    def close(self):
        self.shard_norms.cache_clear()
        self.statistics.cache_clear()
        for shard in self.shards:
            for name in ('rows', 'intensities', 'pointers'):
                array = shard[name]
                if isinstance(array, np.memmap):
                    array._mmap.close()
        self.shards.clear()
        self.native.clear()
        self.custom.clear()
        self.agilent = None

    def metadata(self, row):
        if row < self.native_count:
            reader, local = self.native_row(row)
            return reader.metadata(local)
        source, records, index = self.custom[row - self.native_count]
        meta = records.meta[index]
        return dict(name=records.names[index], formula=meta.get('formula', ''),
                    cas=meta.get('cas#', meta.get('casno', meta.get('cas', ''))),
                    mw=meta.get('mw', ''), comment=meta.get('comment', ''),
                    source=source, library_id=row + 1, reference_metadata=meta)

    def peaks(self, row):
        if row < self.native_count:
            reader, local = self.native_row(row)
            return reader.peaks(local)
        _source, records, index = self.custom[row - self.native_count]
        return records.peaks(index)

    @lru_cache(maxsize=64)
    def shard_norms(self, index, minimum, maximum):
        """Squared prefilter weights (sqrt(I) * m/100)^2 of one shard's references within the range."""
        shard = self.shards[index]
        pointers = shard['pointers']
        a, b = int(pointers[minimum]), int(pointers[maximum + 1])
        masses = np.repeat(np.arange(minimum, maximum + 1, dtype=np.float64),
                           np.diff(np.asarray(pointers[minimum:maximum + 2])))
        weights = np.asarray(shard['intensities'][a:b], dtype=np.float64) * (masses / 100) ** 2
        return np.bincount(shard['rows'][a:b], weights=weights, minlength=shard['count'])

    @lru_cache(maxsize=1)
    def statistics(self):
        """PBM uniqueness and abundance statistics of all loaded reference spectra."""
        occurrences, abundance = np.zeros(10001), np.zeros(101)
        for shard in self.shards:
            intensities, pointers = shard['intensities'], shard['pointers']
            for m in np.flatnonzero(np.diff(pointers)):
                values = np.asarray(intensities[pointers[m]:pointers[m + 1]], dtype=np.float64)
                values = values[values >= MIN_ABUNDANCE]
                occurrences[m] += len(values)
                abundance += np.bincount(np.minimum(values.astype(np.int64), 100), minlength=101)
        return PeakStatistics(self.count, occurrences, abundance)

    def _prefilter(self, shard_ids, query, minimum, maximum):
        """Stage 1: exhaustive forward and reverse weighted cosine over the inverted index.

        The postings of all query ions are gathered once per shard and summed
        per reference with one ``bincount`` each, instead of a scatter per
        query ion into arrays over every loaded reference. Each reference's
        sums still run over the query ions in ascending m/z order with the
        same float32 terms, so the similarities are unchanged. Shards run on a
        few threads (NumPy releases the GIL for most of the work); the result
        keeps the shard order.
        """
        qnorm = sum(i * (m / 100) ** 2 for m, i in query.items())
        work = partial(self._prefilter_shard, query=query, qnorm=qnorm, minimum=minimum, maximum=maximum)
        if len(shard_ids) > 1:
            with ThreadPoolExecutor(PREFILTER_THREADS) as pool:
                parts = list(pool.map(work, shard_ids))
        else:
            parts = [work(n) for n in shard_ids]
        return [part for part in parts if part is not None]

    def _prefilter_shard(self, index, query, qnorm, minimum, maximum):
        shard = self.shards[index]
        pointers, rows, intensities, count = shard['pointers'], shard['rows'], shard['intensities'], shard['count']
        spans = [(int(pointers[m]), int(pointers[m + 1]), m, i) for m, i in query.items()]
        spans = [span for span in spans if span[1] > span[0]]
        if not spans or not count:
            return None
        total = sum(b - a for a, b, _m, _i in spans)
        ids = np.empty(total, dtype=np.int32)
        terms = np.empty(total, dtype=np.float32)
        reverse_terms = np.empty(total, dtype=np.float64)
        position = 0
        for a, b, m, i in spans:
            end = position + b - a
            weight = (m / 100) ** 2
            ids[position:end] = rows[a:b]
            view = terms[position:end]
            np.sqrt(intensities[a:b], out=view)
            # (sqrt(i) * sqrt(I)) * weight in float32, as the former scatter computed it.
            np.multiply(view, np.float32(math.sqrt(i)), out=view)
            np.multiply(view, np.float32(weight), out=view)
            reverse_terms[position:end] = i * weight
            position = end
        dots = np.bincount(ids, weights=terms, minlength=count)
        reverse_norm = np.bincount(ids, weights=reverse_terms, minlength=count)
        refnorm = self.shard_norms(index, minimum, maximum)
        forward = np.divide(dots, np.sqrt(qnorm * refnorm), out=np.zeros_like(dots), where=refnorm > 0)
        reverse = np.divide(dots, np.sqrt(reverse_norm * refnorm), out=np.zeros_like(dots),
                            where=(reverse_norm * refnorm) > 0)
        return dict(start=shard['start'], source=shard['source'], forward=forward, reverse=reverse)

    def _score(self, parts, query, minimum, maximum, options, stats, presearch):
        """Stage 2: the best prefilter candidates of ``parts``, scored exactly and ranked.

        PBM (Agilent ChemStation Probability Based Matching) by default; the
        NIST-style similarity takes the prefilter's weighted cosines as match
        factors.
        """
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
        total = sum(query.values())
        scored = []
        for row, (part, local) in candidates.items():
            ref = {m: i for m, i in nominal_peaks(self.peaks(row)).items() if minimum <= m <= maximum}
            fcos, rcos = float(part['forward'][local]), float(part['reverse'][local])
            if options.algorithm == 'similarity':
                mf, rmf, score = search_options.similarity_scores(fcos, rcos)
                if mf <= 0:
                    continue
                values = dict(score=score, qual=score, mf=mf, rmf=rmf, confidence=round(mf / 9.99, 1),
                              forward=round(fcos ** 2 * 100, 1), reverse=round(rcos ** 2 * 100, 1))
                key = (-mf, -rmf, row)
            else:
                confidence, reverse_pbm, forward_pbm = pbm_match(query, ref, stats)
                if confidence <= 0:
                    continue
                values = dict(score=qual(confidence), qual=qual(confidence), confidence=round(confidence * 100, 1),
                              forward=round(forward_pbm * 100, 1), reverse=round(reverse_pbm * 100, 1))
                key = (-confidence, -fcos, row)
            # Query ions the reference also has (the index holds its positive nominal peaks).
            shared = [i for m, i in query.items() if ref.get(m, 0) > 0]
            values.update(coverage=round(sum(shared) / total * 100, 2), matched=len(shared))
            scored.append((key, row, ref, part['source'], values))
        scored.sort(key=lambda item: item[0])
        return scored

    def analyze(self, spectrum, settings=None, regional=True):
        started = time.perf_counter()
        settings = settings or {}
        available = {s['name'] for s in self.sources if s['count'] > 0}
        libraries = settings.get('libraries')
        if libraries is None:
            libraries = sorted(available)
        if not isinstance(libraries, list) or any(not isinstance(s, str) for s in libraries):
            raise ValueError('Libraries must be a list of source names.')
        if not libraries:
            raise ValueError('Select at least one library to search.')
        missing = set(libraries) - available
        if missing:
            raise ValueError('A selected library is unavailable. Refresh the library selection: '
                             + ', '.join(sorted(missing)))
        options = search_options.parse(settings, libraries)
        # In the sequential mode the given order is the search order.
        libraries = list(dict.fromkeys(libraries)) if options.mode == 'sequential' else sorted(set(libraries))
        search_count = sum(s['count'] for s in self.shards if s['source'] in libraries)
        raw = nominal_peaks(spectrum.peaks)
        minimum = int(settings.get('min_mz') or min(raw))
        maximum = int(settings.get('max_mz') or max(raw))
        threshold = float(settings.get('threshold', 0))
        # ``mass_weight`` of older clients is ignored: PBM weights peaks by their information.
        if not 1 <= minimum <= maximum <= 10000 or not math.isfinite(threshold) or not 0 <= threshold <= 20:
            raise ValueError('Invalid search settings: range 1–10000, threshold 0–20%.')
        if not ei_compatible(spectrum.metadata):
            raise ValueError('This record is labeled as non-EI or tandem MS. Use a 70 eV EI spectrum.')
        query = {m: i for m, i in raw.items() if minimum <= m <= maximum and i >= threshold}
        if not query:
            raise ValueError('No peaks remain after filtering. Reduce the threshold or adjust the scan range.')
        stats = self.statistics() if options.algorithm == 'pbm' else None
        # Constraints and per-library minimums discard scored candidates, so more are scored.
        filtering = options.constraints.active or any(options.library_min_scores.values())
        presearch = PRESEARCH * 5 if filtering else PRESEARCH
        if options.mode == 'sequential':
            groups = [[n for n, s in enumerate(self.shards) if s['source'] == name] for name in libraries]
        else:
            groups = [[n for n, s in enumerate(self.shards) if s['source'] in libraries]]
        scored, searched = [], []
        for group in groups:
            found = self._score(self._prefilter(group, query, minimum, maximum), query, minimum, maximum,
                                options, stats, presearch)
            scored.extend(found)
            searched.extend(s for s in dict.fromkeys(self.shards[n]['source'] for n in group) if s not in searched)
            # Agilent's "search libraries in order": stop at the first library with a good enough hit.
            if options.mode == 'sequential' and any(
                    values['score'] >= max(options.stop_score, options.library_min_scores.get(library, 0))
                    and options.constraints.accepts(self.metadata(row))
                    for _key, row, _ref, library, values in found):
                break
        scored.sort(key=lambda item: item[0])
        hits, seen = [], set()
        for _key, row, ref, library, values in scored:
            if values['score'] < options.library_min_scores.get(library, 0):
                continue
            meta = self.metadata(row)
            if not options.constraints.accepts(meta):
                continue
            if options.dedupe:
                key = meta['cas'] or (meta['name'].lower(), meta['formula'])
                if key in seen:
                    continue
                seen.add(key)
            hit = dict(**meta, row=row, library=library, **values, families=family_tags(meta), peaks=list(ref.items()))
            if not options.lite:
                hit['evidence'] = candidate_evidence(meta, query, ref, minimum, maximum)
            hits.append(hit)
            if len(hits) == options.max_hits:
                break
        for rank, hit in enumerate(hits, 1):
            hit['rank'] = rank
        analysis = None
        if not options.lite:
            analysis = interpret(query, hits, minimum, maximum)
            if regional and maximum > 300 and minimum < 200 and len([m for m in query if m <= 250]) >= 10 and (not hits or hits[0]['score'] < 60):
                region = self.analyze(spectrum, dict(min_mz=minimum, max_mz=250, threshold=threshold, libraries=libraries), regional=False)
                analysis['region_leads'] = [dict(name=h['name'], score=h['score'], families=h['families'], formula=h['formula'])
                                            for h in region['hits'][:3] if h['score'] >= 60]
        warnings = list(spectrum.warnings) + (analysis['warnings'] if analysis else [])
        if not settings.get('min_mz') or not settings.get('max_mz'):
            warnings.append('Search range is inferred from the reported peak endpoints. Enter the actual acquisition limits if known; unreported endpoint ions do not define the instrument scan range.')
        if any(abs(m - math.floor(m + .5)) > .05 for m, _ in spectrum.peaks):
            warnings.append('Decimal m/z values were rounded to nominal 1 Da bins; exact-mass information is not used.')
        if maximum - minimum < 50:
            warnings.append('The narrow search range limits identification specificity.')
        energy = ' '.join(spectrum.metadata.get(k, '') for k in ('ionization energy', 'electron energy', 'collisionenergy')).strip()
        if energy and '70' not in energy:
            warnings.append('The recorded energy may differ from 70 eV; relative ion abundances may differ from the EI references.')
        return dict(name=spectrum.name, metadata=spectrum.metadata, raw_peaks=list(raw.items()), peaks=list(query.items()),
                    retained=len(query), original=len(spectrum.peaks), base_peak=max(raw, key=raw.get),
                    hits=hits, interpretation=analysis, warnings=warnings,
                    settings=dict(min_mz=minimum, max_mz=maximum, threshold=threshold, libraries=libraries,
                                  searched=searched, **options.as_dict()),
                    library_count=search_count, version=VERSION, seconds=round(time.perf_counter()-started, 3),
                    conclusion=('Strong library similarity' if hits and hits[0]['score'] >= 80 else
                                'Tentative library lead' if hits and hits[0]['score'] >= 60 else 'Identity unresolved'),
                    score_note=SIMILARITY_NOTE if options.algorithm == 'similarity' else 'Agilent ChemStation PBM (Probability Based Matching; McLafferty/Stauffer): reverse and forward information match in bits, reported as Qual 0–99 and ranked by PBM confidence. The Qual conversion was calibrated against ChemStation results; single values can differ from ChemStation. Labels and retention times are not search inputs.')

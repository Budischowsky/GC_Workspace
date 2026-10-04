# Fast Compare of the double determination — design

Date: 2026-10-05 · Branch: `feature/fast-compare` (from `main` at a4f114c)

## Goal

The *Compare* of the Replicates / results panel (feature double determination: pairing, split
carry-over, gap filling, harmonisation, consensus identities) has to take drastically less time,
with **the same result** as today: the same peaks, areas, identifications, features, traffic
lights and proposals after the full sequence the panel runs (Compare, background consensus
search, automatic second compare). The automation (`automation/pipeline.py`, `SV.run(...,
apply_boundaries=True)`) uses the same code and must give the same result as well.

## Where the time goes (measured 2026-10-04)

The user's real settings were used: FID method *NIAS-SCREENING FID* (auto deconvolution split,
level 3, closer look) and the *NIAS Standard* search (sequential, PBM, 16 libraries). All 8 runs of
batch 26016605_GIOSUN1635_4 were loaded, and the panel's sequence was replayed headless.

| | 07/11 | 09/12 |
|---|---|---|
| Compare click (`SV.run`, UI thread) | 5.2 s | 6.6 s |
| consensus search (worker) | 4.1 s (8 spectra) | 6.5 s (15 spectra) |
| automatic second compare | 0.05 s | 2.7 s |
| **total** | **9.4 s** | **15.8 s** |

The feature algorithm itself (collect, align, gap fill, harmonise, decide) takes about 0.25 s per
pass. The time goes to two hidden costs:

1. **Re-integration after the automatic changes, about 75 % of a Compare.** Each applied gap
   fill or split re-integrates its run. The automatic deconvolution split then
   (`auto_deconv.plan_peaks`) fits *every* peak's trace again (`component_fit.fit_trace`, about
   110 fits, 3 s per run), although only a few peaks changed. The new, gap-filled peaks also get
   the closer look (`deconv_probe.probe`). It still runs on the legacy per-ion helpers and takes
   0.25 s per peak.
2. **The library search of the consensus spectra.** Each spectrum is a full sequential search
   (0.2–0.5 s). Two parts of it are slow:
   - the PBM scoring in Python repeats the query-side work for each of about 700 candidates;
   - the prefilter recomputes the library norms for every new m/z range (0.44 s for NIST05a.L).

## Design

The idea is to recompute only what changed and to search exactly, but in a batch. Every part
keeps today's arithmetic. Caches are content-addressed, so a stale entry can never be hit.

### Part 1 — split-fit cache (`gcws/ms/component_fit.py`)

- `fit_trace(t, y, shapes, shift0, scan_dt=None, mask=None)` keeps its signature and behaviour.
  It first looks in a module-level LRU cache (at most 4 096 entries, guarded by a
  `threading.Lock`).
- The key is a BLAKE2b digest of everything the fit reads:
  - `t` and `y` as contiguous float64;
  - the mask as bool (all true when None);
  - for every shape, `rt` (repr), `t` and `y`;
  - `shift0` and `scan_dt` (repr).
  The shape slopes `d` follow from `t` and `y`.
- A hit returns a copy of the stored `TraceFit` with fresh arrays, so a caller can never change
  the stored fit.
- All users benefit: the automatic split (`plan_peaks` and `suspect`), the split dialog and the
  split carry-over (`features/split_sync`).
- Prototype result: the end state of the replayed sequence was identical on both pairs. The
  Compare click went from 5.2 to 1.6 s (07/11) and from 6.6 to 3.5 s (09/12); the second compare
  of 09/12 went from 2.7 to 0.8 s.

### Part 2 — the closer look on the array engine (`gcws/ms/deconv_probe.py`)

- `probe(ms, t0, t1, apex, settings)` is rebuilt from the `gcws/ms/deconv_fast.py` building blocks:
  - `_ion_matrix`, `_ion_sigmas` (plus the legacy sigma floor), `_perceive_ions` and
    `_perceive_components`, which already includes the min-ion filter, the merging and the cap;
  - `_model_shape`;
  - `nnls_columns` for the residual fit of all ions and for the joint purification;
  - `_components` for the figures.
- The steps of today's probe are kept in order:
  1. perceive with the sensitive parameters (`params_of`);
  2. group, and clean the models;
  3. fit the found components to every ion; the residual is `max(x - fitted, 0)`;
  4. perceive in the residual with the same sigmas and the residual's own nonzero counts;
  5. group the residual peaks; keep the groups inside [t0, t1] that lie at least
     2 × MIN_SEPARATION_SCANS from every found group;
  6. sort by (apex_sub, −score, m/z) of the current model, clean the models, cap at
     MAX_COMPONENTS;
  7. purify, keep the components inside [t0, t1], apply the share and S/N limits, then
     `_sensitive_window`.
- The residual peaks are appended to the first peak table. Their smoothed traces are stacked
  and their row and peak indices offset, so models, merging and purification share one index
  space.
- `_clean_model` gets an index version: the narrowest ion with at least 20 % of the seed's S/N,
  ties broken by (width, −score, m/z); the rest keep their order.
- Today's implementation stays as `probe_reference(...)`. It is the test oracle and the
  fallback.
- Acceptance (as for the deconvolution work):
  - the same components: count, model ion, apex scan, number of ions, spectrum masses and
    intensities;
  - floating-point figures (rt, area, purity, s_n, profile) equal to rel 1e-10.
- Decision boundaries: the residual comes from a different NNLS routine (block principal pivoting
  instead of Lawson-Hanson; equal up to rounding), so a residual decision could in principle
  differ when it sits exactly on a boundary. The fast probe counts a residual decision as
  borderline in two cases:
  - a column's MIN_ION_SCANS gate depends on entries with |x − fitted| ≤ 1e-9 · the column's
    maximum (a rounding zero);
  - a column's maximum, or a residual peak's height or prominence, lies within 1e-9 (relative)
    of its noise threshold.
  A borderline peak is recomputed with `probe_reference`, as `features/pseudo.coeluting`
  already does for its correlations.
- Estimate: about 0.25 s → 0.03 s per peak. This also speeds up loading and integrating runs.

### Part 3 — exact fast library search for the consensus spectra (vendored files unchanged)

*Revised while planning:* `gcws/libsearch/fast.py` already does what the first draft of 3a planned
(a new scorer). Its *Fast search* (`FastSearch`, reached through `libsearch.service.analyze_many`)
computes the standard search's hits for many spectra at once and exactly:
- block screening with exact recomputation;
- the PBM query side once per peak;
- a decoded-reference cache that outlives the batch;
- a tie fallback.

It is switched on per search method (`data/library_search_fast.json`; on for "NIAS Standard"), and
the batch peak search uses it. The consensus search does not: `search_consensus` calls
`search_spectrum` one spectrum at a time.

**3a. The consensus search uses the Fast search.** `consensus.search_consensus`:
- computes each spectrum's search range exactly as `search_spectrum` does
  (`acquired = (max(1, int(min m/z)), int(max m/z) + 1)`, then `SM.mz_range(method, acquired)`);
- groups the spectra by range (a Fast search batch shares one settings dict), and searches the
  groups in ascending (min, max) order;
- calls `LS.analyze_many` per group when `is_fast(method)`; otherwise it calls `search_spectrum`
  per spectrum, in the same order.

Each spectrum's hits are independent of the grouping and the order.

Measured on 23 real consensus spectra in 11 ranges:

| | standard (today) | Fast search, grouped | Fast search, grouped, with 3b |
|---|---|---|---|
| time | 6.3 s | 4.3 s | 2.5 s |

The hits were identical in every field. The standard path with 3b took 4.6 s.

**3b. Resumable library norms.** `LocalEngine.shard_norms(index, minimum, maximum)` is overridden:

- The norms are accumulated mass by mass, `out[rows[p_m:p_m+1]] += I · (m/100)²` for
  m = minimum…maximum. Each row receives its terms in the same order and with the same float64
  operations as the engine's `bincount`, so the result is bit-identical. This was verified on all
  14 libraries; it is also about 2× faster per pass.
- Per (shard, minimum), the running sum and snapshots at the requested maxima are kept. A
  request continues from the nearest snapshot at or below its maximum. Snapshots are bounded at
  64 arrays in total (LRU), like the engine's own cache.
- Because 3a searches in ascending (min, max) order, each library needs about one pass per lower
  bound instead of one per spectrum. The measured batch had 2 lower bounds (41 and 39). The
  standard engine and the Fast search both call `shard_norms`, so both benefit.
- `Engine.close()` calls `shard_norms.cache_clear()`. The override provides that call, and
  `LocalEngine.close` drops the running sums.

### Part 4 — no wasted searches (`gcws/features/service.py`, `consensus.py`)

**4a.** `consensus_needed` also skips features with a found member carrying an analyst's
identification (`peak.manual`) or a named ISTD binding. `decide()` returns before it reads
consensus hits there, and nothing else reads `consensus_hits`.

**4b.** Consensus hits are saved in the replicate group, as
`group["features"]["consensus"] = {key: hits}`:

- The key is the SHA-1 of the JSON of:
  - the consensus key (`_consensus_key`: masses and abundances to 0.1);
  - the search method's settings (`SM.to_api_settings` without the m/z range, plus the method
    name);
  - the library signature (`libsearch.service._key(store.load())`: names, kinds, paths, enabled
    flags, file sizes and times).
- A changed library, library list or method therefore searches again.
- The hits pass through `json.dumps`/`json.loads` when they are stored, so the project file can
  hold them.
- Only the keys of features in the current table are kept, and failed searches are not stored.
  Writing marks the project dirty, like `remember_ids`.
- Lookup order: the session cache, then the group, then the search. The key is built without
  the library engine, so a Compare whose consensus spectra are all stored does not load the
  engine at all.

Estimate: the consensus search of a first Compare drops from 4–6.5 s to about 1–2 s, and to 0 s
once the hits are stored.

## Expected result (estimated)

| | today | after |
|---|---|---|
| 07/11 total | 9.4 s | ≈ 1.8 s |
| 09/12 total | 15.8 s | ≈ 3.2 s |
| either, hits already stored | — | ≈ 1 s |

The one-time library load per session (1–2 s) stays, but only when a search is needed.

## Testing

- **Part 1:**
  - a hit gives an equal fit and an independent copy;
  - the key changes with t, y, mask, shape rt, profile, shift0 and scan_dt;
  - concurrent calls are safe;
  - the existing deconvolution split tests stay green.
- **Part 2:**
  - fast vs `probe_reference` on synthetic windows (two components, a shoulder, a residual
    component, nothing);
  - on the real runs (with `GCWS_SAMPLES`), every peak the closer look examines at levels 1–5
    must give the same components;
  - the borderline fallback is exercised.
- **Part 3:**
  - `shard_norms` bit-equal to the vendored computation for many ranges, including resumed,
    descending and out-of-order requests and eviction;
  - closing the engine works;
  - on the synthetic MSP libraries of `tests/test_fast_search.py`, `search_consensus` gives
    exactly the per-spectrum `search_spectrum` hits, with Fast search on and off, for spectra
    with several ranges.
- **Part 4:**
  - the manual and ISTD skip;
  - the key changes with spectrum, method and library signature;
  - the hits survive a project save and reload;
  - no engine load when everything is stored;
  - failed searches are not stored.
- **End to end:** a test replays Compare → consensus search → second compare on 07/11 and 09/12
  (real data, skipped when absent). It compares the end-state signature (peaks, areas,
  identifications, feature members and notes, lights, identities, proposals) of the new code
  with the one from reference implementations. The reference implementations are the
  uncached fit, `probe_reference`, the vendored `_score` and `shard_norms`, and no persistence.
- **Timing:** a before/after table from the replayed sequence (the headless script used for the
  measurement becomes `tools/compare_benchmark.py`).
- Each commit keeps the full suite green. Real-data tests run with `GCWS_SAMPLES` pointing to
  `NIAS Working/samples/26016605_GIOSUN1635_4`.

## Delivery

One commit per part, in this order:
1. split-fit cache;
2. closer look;
3. resumable library norms;
4. consensus search through the Fast search;
5. no wasted searches;
6. benchmark tool, end-to-end test, README note.

Nothing is pushed or merged without the user's word.

## Not in scope

- Approximate shortcuts: searching only the members' candidates, or skipping the closer look for
  gap-filled peaks. They would be faster but can change identities and splits.
- Several processes (each would need the 1 GB library index).
- Moving `SV.run` off the UI thread.
- Changing the order of the apply rounds.

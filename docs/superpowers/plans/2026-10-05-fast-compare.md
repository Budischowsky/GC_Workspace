# Fast Compare Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the double-determination Compare (pairing, split carry-over, gap filling, harmonisation, consensus identities) several times faster while giving exactly today's result.

**Architecture:** The plan recomputes only what changed and searches exactly in batches:
- a content-addressed cache for trace fits;
- the closer look on the array engine, with the legacy code kept as oracle and fallback;
- resumable, bit-identical library norms;
- the consensus search through the existing exact Fast search;
- consensus hits saved per replicate group.

**Tech Stack:** Python 3.14, numpy 2.5 (no scipy), PySide6, pytest / pytest-qt.

**Spec:** `docs/superpowers/specs/2026-10-05-fast-compare-design.md`

## Global Constraints

- **Results must be today's results.**
  - Parts 1, 3, 4 and 5 must be bit-identical.
  - The closer look (Task 2) must give identical components (count, model ion, apex scan, number of ions, spectrum). Its floating-point figures must match to rel 1e-10, as `tests/test_deconv.py::assert_same` checks.
- **Vendored files stay unchanged:** `gcws/libsearch/vendor/*` and `gcws/nias_legacy/*`. `.venv/Scripts/python.exe tools/check_vendor.py` must report no drift.
- **Fixed numbers:**
  - fit cache: 4 096 entries;
  - norm snapshots: 64 arrays;
  - borderline tolerance: `EPS = 1e-9` (relative).
- **How to run things:**
  - Python is `.venv/Scripts/python.exe`.
  - The suite runs with `.venv/Scripts/python.exe -m pytest -q`.
  - Real-data tests need `GCWS_SAMPLES="C:/Users/dbudi/Desktop/KI Projects/NIAS Working/samples/26016605_GIOSUN1635_4"`. Without it they skip silently.
- **Keep each file's line endings.** README.md and several UI files are CRLF or mixed. Patch them with `open(..., newline="")`, and check that `git diff --stat` shows only the intended lines.
- **UI tests run on the user's desktop.** Never leave a modal dialog waiting; patch a dialog answer only around the one call (`monkeypatch.context()`).
- **Commits:**
  - Work on branch `feature/fast-compare`, one commit per task, full suite green at every commit, nothing pushed.
  - Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - In bash, use `git commit -F - <<'EOF'`.

## Review Focus

1. **A library is edited, or the library list changes, during a session** (Identify > Libraries). Consensus hits must be searched again, never served stale from the session or the project. Test: Task 5 `test_library_change_invalidates_session_and_stored_hits`.
2. **The UI thread and a worker call `fit_trace` at the same time** (background deconvolution). No cache entry may be corrupted. Test: Task 1 `test_fit_cache_is_thread_safe`.
3. **A caller changes the arrays of a returned `TraceFit`** (the split dialog's previews). Later fits of the same inputs must not change. Test: Task 1 `test_fit_cache_returns_an_equal_independent_copy`.
4. **A project saved on a PC with other libraries is opened.** The stored keys don't match, so the features are searched again, stale keys are pruned, and nothing crashes. Test: Task 5 `test_foreign_keys_are_searched_again_and_pruned`.
5. **The libraries are reset while norms are cached** (`service.reset()` → `Engine.close()`). There must be no exception, and the next search must be correct. Test: Task 3 `test_engine_close_and_reset_with_folds`.

---

### Task 1: Split-fit cache

**Files:**
- Modify: `gcws/ms/component_fit.py` (`fit_trace`, about line 214)
- Test: `tests/test_component_fit.py` (append)

**Interfaces:**
- Produces:
  - `fit_trace(t, y, shapes, shift0, scan_dt=None, mask=None) -> TraceFit`: same signature and results as today, now cached.
  - `fit_trace_uncached(...)`: the same signature; today's body, unchanged.
  - `fit_cache_clear() -> None`
  - `FIT_CACHE_SIZE = 4096`, and `_FIT_CACHE: OrderedDict[bytes, TraceFit]`, which tests read.

- [ ] **Step 1: Write the failing tests** in `tests/test_component_fit.py`. Use the module's `component()` and `trace()` helpers, plus `shapes = [F.Shape.of(component(10.0)), F.Shape.of(component(10.03, mz=71))]` and `t, y = trace([(10.0, 500.), (10.03, 300.)], 0.0066, 1.0)`. Call `F.fit_cache_clear()` first in every test.
  - `test_fit_cache_returns_an_equal_independent_copy`
    - `a = F.fit_trace(t, y, shapes, 0.0066)` and `b = F.fit_trace(t, y, shapes, 0.0066)`; then `a is not b`.
    - Every field of `a` equals `F.fit_trace_uncached(t, y, shapes, 0.0066)`: arrays with `np.array_equal`, scalars with `==`.
    - Then `a.curves[:] = 0; a.areas[:] = 0; a.amplitudes[:] = 0`. A third `fit_trace` call must still equal the uncached fit.
  - `test_fit_cache_hit_skips_the_search`
    - Wrap `F._residual` with a counter through monkeypatch.
    - The first call counts more than 100 calls; the second, identical call counts 0.
  - `test_fit_cache_key_covers_every_input`, parametrized over the changed input:
    - `t + 1e-9`;
    - `y * (1 + 1e-12)`;
    - a mask with one point False;
    - shape rt `+ 1e-9` (rebuild with `Shape.from_arrays`);
    - shape profile y `* 1.001`;
    - `shift0 + 1e-6`;
    - `scan_dt=0.0074`.

    For each, after a first call on the base inputs, the changed call counts residual calls > 0 and equals `fit_trace_uncached` on the changed inputs.
  - `test_fit_cache_is_bounded`
    - Monkeypatch `F.FIT_CACHE_SIZE = 3`.
    - Fit 5 different `shift0` values; then `len(F._FIT_CACHE) == 3`.
  - `test_fit_cache_is_thread_safe`
    - 8 threads (`concurrent.futures.ThreadPoolExecutor`) each fit 6 inputs, with `shift0` cycling over 3 values.
    - Every result equals the uncached fit for its input.

- [ ] **Step 2: Run them and see them fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_component_fit.py -q`. Expected: FAIL, with `AttributeError: ... has no attribute 'fit_cache_clear'`.

- [ ] **Step 3: Implement the cache in `gcws/ms/component_fit.py`.**
  - Rename today's `fit_trace` body to `fit_trace_uncached`.
  - Add a `threading.Lock`, the bounded `OrderedDict` LRU and `fit_cache_clear()`.
  - `fit_trace` builds the key, looks it up under the lock (`move_to_end` on a hit), computes outside the lock on a miss, and stores and evicts under the lock.
  - It returns `dataclasses.replace(fit, amplitudes=fit.amplitudes.copy(), curves=fit.curves.copy(), areas=fit.areas.copy())`, both on a hit and for the stored object.
  - The key is `hashlib.blake2b(digest_size=16)` over, in this order:
    - `np.ascontiguousarray(t, float).tobytes()` and the same for `y`;
    - the mask as `np.ascontiguousarray(bool)` (all True when None);
    - for each shape, `repr(float(s.rt))`, `s.t.tobytes()` and `s.y.tobytes()`;
    - `repr((float(shift0), None if scan_dt is None else float(scan_dt)))`.

    Put a separator byte between the fields. Update the module docstring with one sentence on the cache.

- [ ] **Step 4: Run the tests.** Run `.venv/Scripts/python.exe -m pytest tests/test_component_fit.py tests/test_deconv_split.py tests/test_auto_deconv.py -q`. Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit.**
  - Run `.venv/Scripts/python.exe -m pytest -q`; expected: passes.
  - Then commit: `git add gcws/ms/component_fit.py tests/test_component_fit.py && git commit -F - <<'EOF'` with the message "Fast compare 1: cache trace fits by content", one line of why, and the trailer.

---

### Task 2: The closer look on the array engine

**Files:**
- Create: `gcws/ms/deconv_probe_fast.py`
- Modify: `gcws/ms/deconv_probe.py`. Rename `probe` to `probe_reference`, keeping its body verbatim; a new `probe` dispatches.
- Test: `tests/test_deconv_probe.py` (new)

**Interfaces:**
- Consumes:
  - from `gcws.ms.deconv_fast`: `_ion_matrix(ms, lo, hi)`, `_ion_sigmas(x)`, `_perceive_ions(x, mzs, sigma, nonzero, params) -> _Peaks | None`, `_perceive_components(p, n, params) -> list[list[int]]`, `_model_shape(p, k, n)`, `nnls_columns(a, b)` and `_components(p, groups, a, contrib, tic, mzs, win_rt, lo)`;
  - `gc_deconv._sigma_floor`, `MIN_ION_SCANS`, `MIN_SEPARATION_SCANS` and `MAX_COMPONENTS`;
  - from `deconv_probe`: `params_of`, `_sensitive_window` and `MODEL_MIN_SN_SHARE`, `MIN_AREA_SHARE`, `MIN_SN`.
- Produces:
  - `deconv_probe.probe(ms, t0, t1, apex, settings=None) -> list[D.Component]`: unchanged signature. It calls `probe_fast`, and calls `probe_reference` when the result is not robust.
  - `deconv_probe.probe_reference(...)`: today's code.
  - `deconv_probe_fast.probe_fast(ms, t0, t1, apex, settings=None) -> tuple[list[D.Component], bool]`. The bool is True when every decision is robust.
  - `deconv_probe_fast.gate_borderline(x, diff, residual, sigma, noise_factor) -> bool`.
  - `deconv_probe_fast.EPS = 1e-9`.

- [ ] **Step 1: Write the failing tests** in `tests/test_deconv_probe.py`. Import `RT, build, assert_same` from `tests.test_deconv`, `_mixture` from `tests.test_deconv_fast`, and `MAIN, SHOULDER` from `tests.test_auto_deconv`.
  - `test_fast_probe_matches_reference_on_synthetic_windows[level 1..5]`
    - `settings = D.settings_for_level(D.DeconvSettings(), level)`.
    - Windows: `_mixture(seed)` for seeds 0–29, plus `build([(200.0, MAIN, 80000.0), (200.0 + d, SHOULDER, h)], background=False, seed=s)` for d in (3, 4, 6), h in (8000, 15000) and s in (1, 3).
    - For each, `comps, robust = probe_fast(ms, RT[194], RT[212], RT[200], settings)`. Then `assert_same(probe(ms, ...), probe_reference(ms, ...))`. When robust, also `assert_same(comps, probe_reference(...))`.
    - Finally, at least 90 % of the windows are robust.
  - `test_probe_falls_back_when_not_robust`
    - Monkeypatch `deconv_probe_fast.probe_fast` to return `([], False)`.
    - On the shoulder window, `probe(...)` must equal `probe_reference(...)`, which is non-empty.
  - `test_gate_borderline_detects_rounding_zeros`. Build a one-column `x` of 20 scans:
    - 2 entries with `diff = 50.0`, 3 with `diff = 1e-14`, and the rest with `diff = -5.0`; `residual = np.maximum(diff, 0)`; `sigma = [1.0]`.
    - With `noise_factor = 2.0`, `gate_borderline(...)` is True.
    - With the 3 tiny entries set to `-5.0`, it is False.
    - With 3 entries of `diff = 50.0`, it is False.
  - `test_fast_probe_matches_reference_on_real_runs[07_, 09_]`. This is a real-data test, using `run_dir` from conftest.
    - `run = load_run(run_dir(prefix))`.
    - `res = integrate(run.signal("TIC"), IntegrationMethod())`.
    - Take up to 40 evenly spaced peaks with `6.0 <= apex_rt <= 35.0`.
    - For levels 3 and 5, `assert_same(probe(run.ms, p.start, p.end, p.apex_rt, s), probe_reference(...))` for every peak.
    - The fallback count, counted with a monkeypatched spy on `probe_reference`, is at most 5 % of the calls.

- [ ] **Step 2: Run them and see them fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_deconv_probe.py -q`. Expected: FAIL, with `ImportError` on `probe_reference` and `deconv_probe_fast`.

- [ ] **Step 3: Implement `probe_fast` in `gcws/ms/deconv_probe_fast.py`.** It follows `probe_reference` step by step on array peaks. Every comparison that `probe_reference` makes on residual-derived or final values gets a robustness check; `robust` is the AND of all of them.

  ```text
  params = params_of(settings); window lo..hi, win_rt, n as probe_reference   (sel.size < 5 -> ([], True))
  x, mzs = _ion_matrix(ms, lo, hi); none -> ([], True)
  sigma, nonzero = _ion_sigmas(x); sigma = max(sigma, legacy._sigma_floor(sigma))
  p = _perceive_ions(x, mzs, sigma, nonzero, params)
  groups = [clean(p, g) for g in _perceive_components(p, n, params)] if p else []
  no groups -> (_sensitive_window(ms, t0, t1, apex, settings, []), robust)
  a = column_stack(_model_shape(p, g[0], n)); cols = flatnonzero(x.any(axis=0))
  fitted[:, cols] = a @ nnls_columns(a, x[:, cols]); diff = x - fitted; residual = max(diff, 0)
  robust &= not gate_borderline(x, diff, residual, sigma, params.noise_factor)
  q = _perceive_ions(residual, mzs, sigma, count_nonzero(residual, axis=0), params)
  if q is not None:                                   # probe_reference: "if extra:"
      robust &= same groups (as ordered lists of (mz, apex)) from perceive+group of the residual
                with params replace(noise_factor*(1±EPS), shape_r ∓ EPS, apex_tolerance ± EPS)
                as with params itself (dataclasses.replace on DeconvParams)
      P, off = concat(p, q)       # smooth stacked, q.row += p.smooth.shape[0], other arrays appended
      for g in _perceive_components(q, n, params): g = [k + off for k in g]
          rt = interp(P.apex_sub[g[0]], arange(n), win_rt)
          robust &= not near(rt, t0) and not near(rt, t1)               # |rt - t| <= EPS * max(1, |t|)
          if not t0 <= rt <= t1: continue
          gaps = [abs(P.apex_sub[g[0]] - P.apex_sub[h[0]]) for h in groups]
          robust &= all(abs(gap - 2 * MIN_SEPARATION_SCANS) > EPS for gap in gaps)
          if all(gap >= 2 * MIN_SEPARATION_SCANS for gap in gaps): groups.append(g)
      groups.sort(key=(P.apex_sub[g0], -P.score[g0], P.mz[g0]))
      groups = [clean(P, g) for g in groups[:MAX_COMPONENTS]]; p = P
  a = column_stack(_model_shape(p, g[0], n) for g in groups); contrib[:, cols] = nnls_columns(a, x[:, cols])
  comps = [D.Component(**vars(c)) for c in _components(p, groups, a, contrib, x.sum(axis=1), mzs, win_rt, lo)]
  inside = t0 <= c.rt <= t1 (robust &= not near); total; min_share/min_sn exactly as probe_reference
  robust &= no c with |c.area - min_share*total| <= EPS*total or |c.s_n - min_sn| <= EPS*min_sn
  return _sensitive_window(ms, t0, t1, apex, settings, selected), robust
  ```

  - **`clean(p, g)`** is the index version of `_clean_model`: `strong = [k for k in g if p.s_n[k] >= MODEL_MIN_SN_SHARE * p.s_n[g[0]]]`, model = `min(strong, key=(width_half, -score, mz))`, then `[model] + rest in order`. It sets `robust = False` (through a small mutable flag passed in) when an `s_n` lies within `EPS` (relative) of the strong limit, or when the two smallest strong `width_half` values are within `EPS` (relative).
  - **`gate_borderline`**: per column c, take `tol = EPS * max(abs(x[:, c]).max(), 1e-300)` and `sure = count(diff[:, c] > tol)`, `unsure = count(abs(diff[:, c]) <= tol)`. The column is borderline when `sure < MIN_ION_SCANS <= sure + unsure` and `residual[:, c].max() >= noise_factor * sigma[c] * (1 - EPS)`. A column is also borderline when `abs(residual[:, c].max() - noise_factor * sigma[c]) <= EPS * noise_factor * sigma[c]`.
  - **In `gcws/ms/deconv_probe.py`:** rename `probe` to `probe_reference` (body untouched). Add `probe(...)`: a lazy import of `probe_fast`; return its components when robust, else `probe_reference(...)`. Update the module docstring: two lines on the array path and the fallback.

- [ ] **Step 4: Run the tests.**
  - Run `.venv/Scripts/python.exe -m pytest tests/test_deconv_probe.py tests/test_auto_deconv.py tests/test_deconv_fast.py -q`, then the same with `GCWS_SAMPLES` set.
  - Expected: all pass, and the real-run test reports at most 5 % fallbacks.

- [ ] **Step 5: Run the full suite (with `GCWS_SAMPLES`), then commit** with the message "Fast compare 2: the closer look runs on the array engine".

---

### Task 3: Resumable library norms

**Files:**
- Create: `gcws/libsearch/norms.py`
- Modify: `gcws/libsearch/service.py` (`LocalEngine.__init__`, new `shard_norms` and `close`)
- Test: `tests/test_libsearch_norms.py` (new)

**Interfaces:**
- Produces:
  - `NormFolds(limit: int = 64)` with `.get(shard: dict, index: int, minimum: int, maximum: int) -> np.ndarray`, `.clear() -> None` and `.snapshots: OrderedDict[(index, minimum, maximum), np.ndarray]`;
  - `LocalEngine.shard_norms(index, minimum, maximum) -> np.ndarray`, which has a no-op `cache_clear` attribute because the vendored `Engine.close()` calls it;
  - `LocalEngine.close()`, which clears the folds, then runs `super().close()`.

- [ ] **Step 1: Write the failing tests** in `tests/test_libsearch_norms.py`. Reuse the fixtures with `from tests.test_fast_search import data, libraries, _clean  # noqa: F401`, take `eng = service.get_engine()`, and take the vendored computation as `vendor = engine.Engine.shard_norms.__wrapped__` (import the vendored `engine` module after `gcws.libsearch`).
  - `test_folds_equal_vendor_norms_bitwise`
    - For every shard index, request in this order: (41, 250), (41, 300), (41, 120), (39, 300), (41, 300), (35, 400), (60, 61), (1, 10000).
    - Each result must satisfy `np.array_equal(eng.shard_norms(i, lo, hi), vendor(eng, i, lo, hi))`, and its dtype must be float64.
  - `test_folds_resume_from_the_nearest_snapshot`
    - `f = NormFolds()`; call `f.get(shard, 0, 41, 200)`, then spy on how many masses are folded for `f.get(shard, 0, 41, 260)`.
    - Expect exactly the masses 201–260 to be folded, through a counter on a monkeypatched `norms._fold_mass` hook.
  - `test_folds_are_bounded`
    - `NormFolds(limit=3)` with 5 distinct requests leaves `len(snapshots) == 3`.
    - Every returned array is still bit-equal to the vendored one.
  - `test_engine_close_and_reset_with_folds`
    - `eng.shard_norms(0, 41, 200)`; `service.reset()` raises nothing.
    - A new `service.get_engine()` answers `analyze` again.
  - `test_analyze_results_unchanged_by_folds[combined pbm, sequential pbm, similarity]`
    - For the fixture queries, compare `_clean(service.analyze(points, name, settings))` with the folds against the same call after `monkeypatch.setattr(service.LocalEngine, "shard_norms", vendor_lru)`.
    - `vendor_lru = functools.lru_cache(64)(vendor)`, with a no-op `cache_clear` kept.
    - The results must be equal.

- [ ] **Step 2: Run them and see them fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_libsearch_norms.py -q`. Expected: FAIL, with `ModuleNotFoundError: gcws.libsearch.norms`.

- [ ] **Step 3: Implement `NormFolds` in `gcws/libsearch/norms.py`.**
  - Use one global lock for the LRU and one lock per shard index for computing.
  - A request continues from the snapshot of the same `(index, minimum)` with the largest `maximum` below the request; with none, it starts from zeros of `shard['count']`.
  - The continuation works on a copy. The result is stored and the oldest snapshots are evicted beyond `limit`.
  - The fold must be bit-identical to the vendored `bincount`. Each row receives its terms in mass order, using numpy's `x * x` square:

  ```python
  def _fold_mass(out, shard, m):
      a, b = int(shard['pointers'][m]), int(shard['pointers'][m + 1])
      if b > a:
          f = np.float64(m) / 100
          out[shard['rows'][a:b]] += np.asarray(shard['intensities'][a:b], dtype=np.float64) * (f * f)
  ```

  - **In `service.py`:**
    - `LocalEngine.__init__` sets `self._folds = NormFolds()` before loading.
    - `shard_norms` delegates to `self._folds.get(self.shards[index], index, minimum, maximum)`, then `shard_norms.cache_clear = lambda: None` is set after the def.
    - `close()` calls `self._folds.clear()` and then `super().close()`.
    - Add one docstring line on why the override is exact.

- [ ] **Step 4: Run the tests.** Run `.venv/Scripts/python.exe -m pytest tests/test_libsearch_norms.py tests/test_fast_search.py tests/test_libsearch.py -q`. Expected: all pass.

- [ ] **Step 5: Run the full suite and the vendor check, then commit.**
  - Run `.venv/Scripts/python.exe -m pytest -q` and `.venv/Scripts/python.exe tools/check_vendor.py`; expected: pass and no drift.
  - Commit with the message "Fast compare 3: resumable, bit-identical library norms".

---

### Task 4: Consensus search through the Fast search

**Files:**
- Modify: `gcws/features/consensus.py` (`search_consensus`, plus a new `search_range`)
- Test: `tests/test_features_consensus.py` (append)

**Interfaces:**
- Consumes:
  - `gcws.identify.service`: `prepare_local`, `search_spectrum` and `is_fast`;
  - `gcws.libsearch.service.analyze_many(spectra, settings) -> list[dict | Exception]`;
  - `gc_search_method`: `mz_range` and `to_api_settings`.
- Produces:
  - `search_range(points: list[tuple[int, float]], method) -> tuple[int, int]` = `SM.mz_range(method, (max(1, int(min m)), int(max m) + 1))`;
  - `search_consensus(table, method, needed, progress=lambda t: None) -> str`: same contract as today.

- [ ] **Step 1: Write the failing tests** in `tests/test_features_consensus.py`, using `from tests.test_fast_search import data, libraries  # noqa: F401`.
  - Build 12 features with `f.consensus` taken from the fixture queries; `id = F-001…`.
  - Trim 4 of them to masses ≥ 50 and 4 to masses ≤ 200, so there are at least 3 ranges.
  - `method = SM.SearchMethod(name="T", libraries=[SM.LibraryEntry("A", True), SM.LibraryEntry("B", True)], algorithm="pbm", mode="sequential", stop_score=60, top_n=5)`.
  - The expected hits for each f are `[dict(GI.hit_record(h), peaks=h.get("peaks") or []) for h in IS.search_spectrum(points, f.id, method)]` (the old per-spectrum route).
  - `test_search_consensus_equals_per_spectrum[fast_on, fast_off]`
    - Monkeypatch `gcws.identify.service.fast_search_methods` to return `{"T"}` or `set()`.
    - Spy on `gcws.libsearch.service.analyze_many`.
    - `search_consensus(None, method, feats) == ""`, and every `f.consensus_hits` equals its expected list.
    - The spy is called only when fast is on.
  - `test_search_consensus_runs_ranges_in_ascending_order`
    - With fast on, record each `analyze_many` call's `(settings["min_mz"], settings["max_mz"])`.
    - The list is sorted, and its length equals the number of distinct `search_range` values.
  - `test_failed_fast_result_is_a_reason`
    - Monkeypatch `analyze_many` to return a `ValueError("boom")` for the second spectrum of a group.
    - That feature gets the reason `"consensus search failed: boom"` and keeps empty hits; the others get their hits.

- [ ] **Step 2: Run them and see them fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_features_consensus.py -q`. Expected: the new tests FAIL, because `analyze_many` is never called and there is no `search_range`.

- [ ] **Step 3: Implement it in `gcws/features/consensus.py`.**
  - Keep the `prepare_local` try/except and its note text.
  - `points` are as today.
  - Group the features by `search_range`, and iterate over the groups sorted by range.
  - With `is_fast(method)`: `settings = SM.to_api_settings(method, rng, lite=True)`, then `LS.analyze_many([(f.id or "consensus", points) for f in group], settings)`. A result that is an Exception gives `f.reasons.append(f"consensus search failed: {exc}")`; otherwise use `result.get("hits") or []`.
  - Otherwise, call `search_spectrum(points, f.id or "consensus", method)` per feature, with today's except clause.
  - Hits are converted as today. Update the function docstring with one line on the Fast search and the range order.

- [ ] **Step 4: Run the tests.** Run `.venv/Scripts/python.exe -m pytest tests/test_features_consensus.py tests/test_fast_search.py -q`. Expected: all pass.

- [ ] **Step 5: Run the full suite, then commit** with the message "Fast compare 4: the consensus spectra go through the Fast search".

---

### Task 5: No wasted searches; consensus hits saved per replicate group

**Files:**
- Modify: `gcws/features/model.py` (`Feature`: new field `consensus_key: str = ""`)
- Modify: `gcws/features/service.py`: `consensus_needed`, `store_consensus`, `consensus` and `remember_ids`, plus the new `consensus_context` and `consensus_cache_key`
- Modify: `gcws/libsearch/service.py` (new `library_signature`)
- Modify: `gcws/ui/docks/duplicate.py` (`_start_consensus_search`, `_consensus_found`; keep its line endings)
- Test: `tests/test_features_consensus.py` (append)

**Interfaces:**
- Consumes: `search_method(ws, members)` and `group_of(ws, members)` from `service.py`.
- Produces:
  - `libsearch.service.library_signature() -> str` = `json.dumps(_key(store.load()), default=str)`.
  - `service.consensus_context(ws, members) -> str`: the JSON (`sort_keys=True`) of `{"method": m.name, "settings": SM.to_api_settings(m, (1, 1), lite=True)` without `min_mz`/`max_mz`, `"libraries": library_signature()}`, where `m = search_method(ws, members)`.
  - `service.consensus_cache_key(spectrum, context: str) -> str`: the sha1 hexdigest of `json.dumps([list(masses), list(abundances rounded to 0.1), context])`. The masses and abundances come from `_consensus_key(spectrum)`.
  - `consensus_needed(ws, table, cfg, group=None) -> list[tuple[str, Feature]]`. The keys are now these strings. `group` defaults to `group_of(ws, table.members)`.
  - `store_consensus(ws, needed, note="", group=None) -> None`.
  - The session caches `ws._feature_consensus` and `ws._feature_consensus_failed` are keyed by the same strings.

- [ ] **Step 1: Write the failing tests** in `tests/test_features_consensus.py`. Use the `member`/`feature`/`hits` helpers, `ws = SimpleNamespace(quant={}, runs={}, replicate_groups=[group])` with `group = {"id": "g", "members": ["a", "b"]}` and `table = FeatureTable(["a", "b"], ["A", "B"], "FID", feats, settings=Settings())`. Monkeypatch `gcws.libsearch.service.library_signature` to a fixed string.
  - `test_manual_and_istd_features_are_not_searched`
    - X 86 / Y 85 on one side and Y 85 / X 84.5 on the other gives 1 needed.
    - The same with `manual=True` on member A gives 0 needed.
    - The same with `istd="IS1"` on member A gives 0 needed.
  - `test_consensus_key_changes_with_spectrum_method_and_libraries`
    - Keys differ for a changed abundance (+1.0), a different method name (monkeypatch `service.search_method`), and a different `library_signature`.
    - The key is equal for an abundance change of +0.01 (it rounds to 0.1).
  - `test_stored_hits_are_used_without_a_search`
    - `group["features"] = {"consensus": {key: hits((*Y, 90))}}`.
    - Monkeypatch `gcws.libsearch.service.get_engine` to raise.
    - `consensus_needed(...) == []`, `f.consensus_hits == hits((*Y, 90))` and `f.consensus_key == key`.
  - `test_store_consensus_saves_json_hits_and_marks_dirty`
    - `ws.dirty = False`; after `store_consensus(ws, needed, group=group)`, `group["features"]["consensus"][key]` equals the JSON round trip of the hits, and `ws.dirty is True`.
    - With `note="x"`, nothing is stored.
    - A hit holding `object()` is not stored in the group, but is in `ws._feature_consensus`.
  - `test_foreign_keys_are_searched_again_and_pruned`
    - The group holds `{"deadbeef": [...]}`; the feature is needed.
    - After `remember_ids(ws, ["a", "b"], table)`, `"deadbeef"` is gone and `ws.dirty is True`.
    - A second `remember_ids` call leaves `dirty` unchanged after it is reset to False.
  - `test_library_change_invalidates_session_and_stored_hits`
    - Store hits under signature "s1".
    - Switch the monkeypatched `library_signature` to "s2"; the feature is needed again, from neither the session nor the group.
  - `test_hits_survive_a_project_round_trip`
    - `ws.replicate_groups = json.loads(json.dumps(ws.replicate_groups))`, as `gcws/core/project.py` saves it.
    - `consensus_needed` then finds the stored hits.
  - The existing `test_async_comparison_defers_identity_until_consensus` must still pass, unchanged.

- [ ] **Step 2: Run them and see them fail.** Run `.venv/Scripts/python.exe -m pytest tests/test_features_consensus.py -q`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement it.**
  - **`consensus_needed`:**
    - Compute the context once per call.
    - Skip features where any found member has `peak.manual`, or `peak.istd` together with a name. Do this after today's mismatch, case-A and quality-limit checks.
    - Set `f.consensus_key`.
    - Look the key up in the session cache, then in `group["features"]["consensus"]`; a group hit is copied into the session cache.
    - Otherwise the feature is needed, unless the key is in `failed`.
  - **`store_consensus`:** as today. In addition, when `not note and group is not None`, store `json.loads(json.dumps(hits, ensure_ascii=False))`. Skip on `TypeError`/`ValueError`. Set `ws.dirty = True` only when the stored value changed.
  - **`consensus()`:** passes `group_of(ws, members)` to both calls.
  - **`remember_ids()`:** also prunes `features["consensus"]` to `{f.consensus_key for f in table.features if f.consensus_key}`, with `dirty` only on a change.
  - **Dock:**
    - `_start_consensus_search` keeps `self._search_group = SV.group_of(self.ws, t.members)`.
    - `_consensus_found` calls `SV.store_consensus(self.ws, needed, note, group=getattr(self, "_search_group", None))`.
  - **Docstrings:** update the `consensus_needed` docstring (the manual/ISTD skip, the saved hits) and add one docstring line for `library_signature`.

- [ ] **Step 4: Run the tests.** Run `.venv/Scripts/python.exe -m pytest tests/test_features_consensus.py tests/test_duplicate_view.py tests/test_features_report.py tests/test_ui.py -q`. Expected: all pass, with no dialog left open.

- [ ] **Step 5: Run the full suite, then commit** with the message "Fast compare 5: no wasted consensus searches; hits saved with the replicate group".

---

### Task 6: Benchmark tool, end-to-end equality test, README

**Files:**
- Create: `tools/compare_benchmark.py`
- Create: `tests/test_fast_compare_real.py`
- Modify: `README.md`, item 9 "Double determination" (about line 341). The file has mixed CRLF/LF; keep it so.

**Interfaces:**
- Consumes: everything above, plus the reference routes `F.fit_trace_uncached`, `deconv_probe.probe_reference`, the vendored `engine.Engine.shard_norms`, and `fast_search_methods` returning `set()`.
- Produces, in `tools/compare_benchmark.py`:
  - `load(samples: Path, prefixes: list[str], fid_overrides: dict | None = None) -> Workspace`. It integrates FID with `ws.methods.get(default FID).copy(**fid_overrides)`, through `ws.add_run(...)`, so the auto split runs, and it binds the ChemStation LIB hits (the `_bind_lib_hits` of `tools/duplicate_benchmark.py`).
  - `replay(ws, ids) -> dict` with the timings `{"compare": s, "search": s, "spectra": n, "second": s, "total": s}`. It runs `SV.run(apply_auto=None, search=False)`, the consensus search of `SV.consensus_needed`, `SV.store_consensus(..., group=SV.group_of(ws, ids))`, then `SV.run` again, as `gcws/ui/docks/duplicate.py::_compare`/`_start_consensus_search` do.
  - `signature(ws, ids, table) -> list`: for each run in `ids` order, its FID peaks `(start, end, apex_rt rounded to 9 places, repr(area), origin)` and its identifications `(apex_rt to 6 places, name, cas, source)`; then the features `(id, rt to 9 places, light, [(origin, note)], identity (name, case, status), [proposal texts])`.
  - `reference_mode(setattr) -> None`: switches to the reference routes through the given setter (pytest's `monkeypatch.setattr`, or a plain setter in the CLI). The routes are: `F.fit_trace = F.fit_trace_uncached`; `deconv_probe.probe = probe_reference`; `LocalEngine.shard_norms =` the vendored computation under `lru_cache(64)`; and `gcws.identify.service.fast_search_methods = lambda: set()`. A fresh workspace has no saved hits, so persistence needs no switch.
  - `main(argv) -> int` with the options `--samples`, `--pairs` (default `07_:11_,09_:12_`), `--load` (default: all runs of the folder), `--reference` and `--out`. It prints a markdown table (pair, Compare click, consensus search with n spectra, second compare, total, and the signature sha1). Its docstring says to set `GCWS_DATA` to a copy of `data/` for the real methods and libraries.

- [ ] **Step 1: Write the failing test** `tests/test_fast_compare_real.py::test_compare_end_state_equals_reference[07_:11_, 09_:12_]`. It is a real-data test, using `samples` from conftest.
  - `fid_overrides = {"deconv_split": "auto", "deconv_level": 3, "deconv_probe": True}`.
  - Inside `monkeypatch.context()` with `CB.reference_mode(m.setattr)`, run `load(samples, ["06_", "08_", a, b], fid_overrides)`, `replay` and `signature`.
  - Then do the same with the new code, in a new Workspace, after `F.fit_cache_clear()`.
  - The two signatures must be equal.
  - The consensus search finds no libraries under `tests/_data`, so it gives its note; Tasks 4 and 5 test the library path.

- [ ] **Step 2: Run it and see it fail.** Run `GCWS_SAMPLES=... .venv/Scripts/python.exe -m pytest tests/test_fast_compare_real.py -q`. Expected: FAIL with `ModuleNotFoundError: tools.compare_benchmark`.

- [ ] **Step 3: Implement `tools/compare_benchmark.py`** to the interface above.

- [ ] **Step 4: Run the test.** Run the same command. Expected: PASS for both pairs.

- [ ] **Step 5: Measure, then add the README note.**
  - Run `GCWS_DATA=<scratch copy of data with junctioned libcache> .venv/Scripts/python.exe tools/compare_benchmark.py --samples ".../26016605_GIOSUN1635_4"` twice, once with `--reference`.
  - Expected: equal signature sha1 per pair, and the new totals well below the reference ones.
  - Then add to README item 9 one sentence: Compare re-fits only the peaks that changed; consensus spectra are searched with Fast search when the method has it on; their hits are saved with the replicate group, so a reopened project does not search them again.

- [ ] **Step 6: Run the full suite with `GCWS_SAMPLES` and the vendor check, then commit.**
  - Run `.venv/Scripts/python.exe -m pytest -q` and `tools/check_vendor.py`.
  - Commit with the message "Fast compare 6: benchmark tool, end-to-end equality test, README". The message body includes the before/after table.

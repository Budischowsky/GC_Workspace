# Learning report rules, phase 3 (fitting framework, peak detection, families 1–2): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fitting framework that proposes general, evidence-based settings with honest held-out numbers. It is used
first on FID peak detection (integration settings), which the baseline showed is the largest gap, and then on rule
families 1 (background) and 2 (keep/report).

**Why this scope (user decision 2026-10-09):** the baseline (`data/learn/baseline.md`) found:
- 83 analyst lines without any program peak, and a median of 45 program FID peaks per run against 165 in ChemStation.
- Analysts report exactly the peaks ≥ 10 ppb: 100 % of reported peaks are ≥ 0.01 mg/kg, and 91 % of kept-unreported
  peaks are below it.
- Family 2 alone would therefore only confirm 0.01. The detection settings are where a fit can gain.

**Architecture:**
- `folds.py` splits the corpus by batch: a locked test set plus k folds.
- `fit.py` runs a grid search with cross-validation, the tie-break and the acceptance check, and returns a
  `FitResult`.
- `detection.py` caches each run's FID trace once, re-integrates it with candidate settings
  (`integration.engine.integrate`, no MS, no library search), and scores detection against the analyst's
  above-limit peaks.
- `rules.py` holds families 1–2 as pure functions with reason strings, evaluated on the cached program evidence.
- `propose.py` + CLI `python -m gcws.learn fit <target> <root>` write a proposal (`.md` + `.json`) that the
  user accepts or rejects. Nothing is applied automatically.

**Tech stack:** Python 3.12+, numpy, pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-learn-from-evaluations-design.md` §5 (families 1–2), §8 (fitting). Earlier
plans: `2026-10-09-learn-from-evaluations.md` (phase 1), `2026-10-09-learn-phase2-baseline.md` (phase 2).

## Global constraints

- Phase 1–2 constraints hold: local data only under `paths.DATA/"learn"`, the training root is read-only, no dialogs,
  real-data tests skip without data.
- **Splits (spec §8):**
  - Grouped by batch folder name.
  - Locked test set: 20 % of batches, at least 1, chosen deterministically (sha1 of the batch name, lowest first).
    It is used only for the reported test score.
  - k = 5 folds over the remaining batches (k = number of batches when there are fewer).
- **Tie-break:** among parameter sets within 0.005 of the best mean CV score, take the one that comes first in the
  grid's preference order (each grid lists the current or stricter value first).
- **Acceptance:** the proposal is marked `accept_recommended` only if its CV score is above the current settings'
  CV score and no batch loses more than 0.02 against the current settings.
- **Reporting limit:** 0.01 mg/kg (`baseline.REPORTING_LIMIT_MGKG`). Analyst concentrations come from the
  worksheet's mg/kg column. Runs without a mg/kg column, or without ISTD areas, are left out of detection scoring
  and listed.
- **Program concentration estimate** = program area × ISTD scale (analyst IS-row areas / matched program areas) ×
  the analyst's mg/kg per ChemStation area unit (median of worksheet conc / area). Everything comes from the run's
  own data.
- Detection is fitted on FID-only integration with `deconv_split = "off"` for the current and the candidate
  settings alike, so the comparison is fair. This is stated in the proposal text.

## Review focus

1. A run whose FID trace cannot be read: listed in the fit's `skipped` and the fit continues.
2. A grid value that makes `integrate` raise (e.g. an invalid width): that parameter set scores `None` and is never
   chosen.
3. Fewer than 3 batches outside the test set: the fit still runs (k = number of batches) and says so.
4. Running the fit twice gives the same proposal (deterministic splits and grid order).
5. A proposal never modifies the stored processing method. Accepting is a separate, explicit step (phase 6).

---

### Task 1: Batch-grouped splits

**Files:** Create `gcws/learn/folds.py`. Test `tests/test_learn_folds.py`.

**Interfaces (produces):**
```python
@dataclass
class Split:
    test: list[str]                 # locked test batches
    folds: list[list[str]]          # each fold = held-out batches for one CV round
def make_split(batches: list[str], test_share: float = 0.2, k: int = 5) -> Split
```
- [ ] **Step 1: Failing tests.**
  - `test_split_is_deterministic_and_disjoint`: 13 batch names → 3 test batches (`ceil(0.2 × 13)`). The folds
    partition the other 10. The same input in shuffled order gives the same `Split`.
  - `test_few_batches`: 3 batches → 1 test, 2 folds of 1.
  - `test_one_batch`: 1 batch → no test batch, 1 fold (there is nothing to hold out, which the fit report says).
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_learn_folds.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Order batches by sha1 hex. The test set is the first `max(1, ceil(share × n))` when
  n ≥ 2. Folds are round-robin over the rest.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `Learn: batch-grouped locked test set and CV folds`.

### Task 2: Grid-search fit with cross-validation

**Files:** Create `gcws/learn/fit.py`. Test `tests/test_learn_fit.py`.

**Interfaces (produces):**
```python
@dataclass
class FitResult:
    target: str; current: dict; best: dict
    cv_current: float | None; cv_best: float | None
    test_current: float | None; test_best: float | None
    per_batch: dict[str, dict]      # batch -> {"current": score, "best": score}
    table: list[dict]               # [{"params": {...}, "cv": float|None}] in grid order
    accept_recommended: bool; notes: list[str]
def grid(space: dict[str, list]) -> list[dict]         # cartesian product, first values first (preference order)
def fit(target: str, space: dict[str, list], current: dict, batches: dict[str, list],
        evaluate: Callable[[dict, list], float | None], *, tie: float = 0.005, max_loss: float = 0.02) -> FitResult
#   batches: batch -> its run objects; evaluate(params, runs) -> mean score over those runs (None if not scorable)
```
- [ ] **Step 1: Failing tests** with a toy `evaluate` (score = 1 − |params["x"] − run.optimum|):
  - `test_fit_finds_the_optimum`: runs with optimum 3 in all batches, grid x ∈ [1, 2, 3, 4], current {x: 1} →
    `best == {"x": 3}`, `cv_best > cv_current`, `accept_recommended`.
  - `test_tie_prefers_first`: scores of x = 3 and x = 4 within 0.005 → the earlier one in grid order wins.
  - `test_batch_loss_blocks_acceptance`: one batch with optimum 1 and the rest 3 → best x = 3, but that batch
    loses more than 0.02 → `accept_recommended is False` and a note names the batch.
  - `test_failing_params_are_never_chosen`: `evaluate` returns `None` for x = 3 → x = 3 is not chosen and its
    table row has `cv` `None`.
  - `test_test_set_not_used_for_choice`: the test batches' optimum is 4 while the rest is 3 → `best == {"x": 3}`,
    and `test_best` is reported.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** For each fold, every candidate is scored on the held-out fold; the CV score of a
  candidate is the mean over folds. The choice uses CV scores only. Test scores and per-batch scores are computed
  for current and best afterwards.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: grid-search fit with batch-grouped cross-validation and acceptance check`.

### Task 3: Detection target (FID re-integration and detection score)

**Files:**
- Create: `gcws/learn/detection.py`
- Modify: `gcws/learn/match.py` (`HumanItem.conc_mgkg`)
- Test: `tests/test_learn_detection.py`, `tests/test_learn_real.py`

**Interfaces (produces):**
```python
# match.py: HumanItem gains conc_mgkg: Optional[float] = None (worksheet mg/kg column; None without one)
@dataclass
class DetectionRun:
    key: str; batch: str; run_dir: str
    items: list[HumanItem]          # the analyst's items (phase 2 human_items)
    mgkg_per_area: float | None     # analyst mg/kg per ChemStation area unit (median conc/area of final rows)
    signal: str                     # path of the cached FID trace (.npz: rt, intensity)
def fid_cache(run_dir: str, out_root: Path) -> Path                  # writes out_root/signals/<run_key>.npz once
def load_detection_runs(root: Path, out_root: Path) -> tuple[list[DetectionRun], list[str]]   # runs, skipped reasons
DETECTION_SPACE: dict[str, list]     # see below
def integrate_peaks(run: DetectionRun, base_method: dict, params: dict) -> list[dict]   # {"rt","start","end","area"}
def detection_score(run: DetectionRun, peaks: list[dict], limit: float = 0.01) -> float | None
```
- **Detection score** (F1 over above-limit peaks):
  - Targets = analyst items from the worksheet (source `final`, decision not `istd`) with `conc_mgkg ≥ limit`.
  - Program detections = peaks whose estimated conc (Global constraints) is ≥ limit.
  - A target is found when a detection is paired `exact` to it by `match_run`. A detection is right when it is
    paired `exact` to any analyst item.
  - `None` when there is no target and no detection.
- `DETECTION_SPACE` (the current value first):
  - `slope_sensitivity` [8, 4, 2, 16]
  - `area_reject` [500000, 250000, 100000]
  - `height_reject` [5000, 2500, 1000]
  - `min_sn` [3, 5]
  - `integrator_on` [6.2, 5.5, 5.0] (the time of the `INTEGRATOR_ON` timed event)
- [ ] **Step 1: Failing tests.**
  - `test_conc_mgkg_on_items`: fixture workbook → item 7.07 has `conc_mgkg == 1.458`. Removed items have `None`.
  - `test_detection_score_perfect` (synthetic run, peaks given directly): peaks exactly at the above-limit targets
    with areas giving conc ≥ limit → 1.0.
  - `test_detection_score_counts_misses_and_false`: one target missing, one detection at an RT with no analyst
    item → precision 2/3, recall 2/3 (score 2/3).
  - `test_integrate_peaks_applies_params` (real `integrate` on a synthetic Gaussian trace saved as `.npz`): a small
    peak below `area_reject=500000` is found with `area_reject=100000` but not with 500000. `integrator_on` 5.0 vs
    6.2 decides whether a peak at 5.5 min is found.
  - `tests/test_learn_real.py::test_detection_current_settings` (skips without data): GIO run, current settings →
    score between 0 and 1. The cached `.npz` exists and the training root is unchanged.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `fid_cache` uses `io.run_loader.load_run(run_dir)` → `run.signal(FID)` arrays, written with `np.savez`.
  - `integrate_peaks` builds an `IntegrationMethod` from `base_method["sections"]["integration"]["FID"]` with
    `params` applied (`integrator_on` replaces the `INTEGRATOR_ON` event's time) and `deconv_split="off"`.
  - The ISTD scale comes from items with decision `istd` matched `exact`, as in `score.py`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: detection target (cached FID traces, re-integration, above-limit detection score)`.

### Task 4: Rule families 1–2 on the cached program evidence

**Files:** Create `gcws/learn/rules.py`. Test `tests/test_learn_rules.py`.

**Interfaces (produces):**
```python
@dataclass
class Decision: keep: bool; report: bool; reason: str
def background(peak: dict, p: dict) -> tuple[bool, str]     # p: {"ratio_limit": float}; True = remove
def keep_report(conc_mgkg: float | None, p: dict) -> tuple[bool, str]   # p: {"limit": float}; True = report
BACKGROUND_SPACE = {"ratio_limit": [3.0, 2.0, 1.5, 5.0]}
REPORT_SPACE = {"limit": [0.01, 0.005, 0.02]}
def worksheet_agreement(run, params) -> float | None        # family 1 on phase-2 cached runs (score.py definition)
def report_agreement(run, params) -> float | None           # family 2: analyst reported vs conc >= limit, items with conc
```
Reason strings:
- `"background v1: in blank <name>, ratio 0.6 < 3"`
- `"report v1: 0.0123 mg/kg >= 0.01"`
- `"report v1: 0.0040 mg/kg < 0.01"`

- [ ] **Step 1: Failing tests:** exact reason strings. `background` keeps a peak that is not in a blank.
  `report_agreement` on items (reported 0.02 → report, kept 0.004 → not) = 1.0 at limit 0.01 and 0.5 at limit 0.03.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: rule families 1 (background) and 2 (keep/report) with reasons`.

### Task 5: Proposals and CLI; real fits

**Files:** Create `gcws/learn/propose.py`. Modify `gcws/learn/__main__.py`. Test `tests/test_learn_propose.py`.

**Interfaces (produces):**
```python
def write_proposal(result: FitResult, out_dir: Path, *, method_name: str, extra_notes: list[str] = ()) -> Path
#   out_dir/proposals/<target>.md + .json ; the md has "## Proposal", "## Scores", "## Per batch", "## All candidates"
def run_fit(target: str, root: Path, out_dir: Path, method_name: str = "NIAS", *, progress=print) -> FitResult
#   target in {"detection", "background", "report"}
# CLI: python -m gcws.learn fit {detection,background,report} <root> [--out DIR] [--method NIAS]
```
- [ ] **Step 1: Failing tests:**
  - `write_proposal` on a hand-made `FitResult` → both files, headings present, current → best parameter table,
    "accept recommended: yes/no", and the md states that nothing was applied.
  - The CLI parses its arguments (`run_fit` monkeypatched).
  - `run_fit("report", …)` on a synthetic corpus with cached `ProgramResult`s (phase 2 test helpers) → proposal
    files.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `background` and `report` use phase-2 cached runs (`baseline.json` + `runs/*/program.json`; `run_baseline`
    is called first when missing).
  - `detection` uses `load_detection_runs`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Run the three fits on the real data** in the background and read the proposals. Expected: each
  finishes with test and CV scores, or with stated reasons.
- [ ] **Step 6: Commit** `Learn: fit proposals (detection, background, report) and CLI`.

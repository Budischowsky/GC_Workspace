# Learning report rules, phase 2 (runner, matcher, scorer, baseline): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure how close the current GC Workspace gets to the human evaluations before anything is learned. Each
evaluated run is processed the way the automation processes it, the program's peaks are matched to the analyst's
rows, and a baseline report gives per-run and overall scores plus counts of disagreement types.

**Architecture:** `gcws/learn/runner.py` runs the production job (`automation.pipeline.run_job`) for one evaluated
run as a single determination with its batch blanks. A new optional `inspect` hook in `run_job` collects
per-peak evidence from the workspace through `quant.peak_values.VALUES`. The result is cached as JSON per run.
`match.py` pairs the analyst's FID rows with program FID peaks by RT on the same raw data. `score.py` computes the
spec's scores. `baseline.py` + CLI `python -m gcws.learn baseline` run the corpus and write `baseline.md` / `baseline.json`.

**Tech stack:** Python 3.12+, PySide6 (offscreen QApplication, as `automation/child.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-learn-from-evaluations-design.md` (§3 runner/matcher/score, §6, §7). Phase 1 plan:
`docs/superpowers/plans/2026-10-09-learn-from-evaluations.md`.

## Global constraints

- Same as phase 1: no client data in git (outputs under `paths.DATA / "learn"`), nothing written to the training
  root, real-data tests skip without `GCWS_LEARN_ROOT`, no dialogs.
- Baseline = the program as the user's automation runs it: processing method `NIAS` (`proc_method.load`),
  library search on, ISTD detection on (`min_confidence="high"`), report kind `"nias"`, `require_blank="auto"`.
  A batch without a blank is still processed (`override.allow_no_blank = True`) and flagged.
- **Single determination:** `group.members` = the evaluated run only (spec §1).
- **Matching is on the same raw data.** Analyst rows are ChemStation FID peaks of the evaluated run, and program
  peaks are GC Workspace FID peaks of the same run. So the matcher compares FID RTs directly (RI would only add
  noise here). This rules on spec §6 ("RI first"), which assumed cross-run matching.
- **Concentration comparison is ISTD-relative,** so units do not matter: scale = mean of the analyst's IS-row areas
  divided by the mean of the program areas of the peaks matched to them. The deviation per line is
  `|program_area × scale − analyst_area| / analyst_area`.
- Scores (spec §7): `client_f1` over reported lines, `name_agreement`, `conc_dev_median`, `worksheet_agreement`,
  `total = 0.7 × client_f1 + 0.3 × worksheet_agreement`.

## Review focus

1. A run whose ISTDs the analyst did not list (no IS rows) or that the program did not integrate: scale is `None`,
   conc deviation `None`, other scores still computed.
2. Two workbooks for one run (analyst pair): the run is processed once (cache by run dir) and scored per workbook.
3. A run where `run_job` fails or returns no report: the baseline records the state and reason and carries on, and
   the run counts as failed, not as score 0.
4. Rerunning the baseline uses the cache (no second library search) unless `--force`. A different method name is
   a different cache entry.
5. An analyst row inside a program peak that merges two ChemStation peaks: matched as `inside` to that peak, not
   `missing`.

---

### Task 1: `inspect` hook in `run_job` and the per-peak collector

**Files:**
- Modify: `gcws/automation/pipeline.py` (`run_job` signature + one call before the evidence step)
- Create: `gcws/learn/runner.py` (collector part)
- Test: `tests/test_learn_runner.py`

**Interfaces (produces):**
```python
# pipeline.py
def run_job(spec, *, progress=log.info, identify=None, inspect: Optional[Callable] = None) -> JobResult
#   after the reports: if inspect: evidence["inspect"] = _json_safe(inspect(ws, members)); an exception -> warnings
# runner.py
PEAK_KEYS = ("num", "rt", "ms_rt", "start", "end", "area", "height", "w50", "sym", "sn", "name", "cas", "score",
             "status", "library", "ri", "istd", "blank_area", "blank_ratio", "in_blank", "area_minus_blank",
             "class_hint", "origin")
def collect_peaks(ws, run_id: str) -> list[dict]     # FID peaks: PEAK_KEYS + "hits": [{"name","cas","score"}] (top 3)
def collect(ws, members: list[str]) -> dict           # {"peaks": collect_peaks(ws, members[0])}
```

- [ ] **Step 1: Failing tests** (`qapp` fixture + `samples` fixture from conftest, `lib_oracle`/`spec`/`copy_batch`
  imported from `test_pipeline`):
  - `test_run_job_inspect_hook`: `run_job(spec(..., group members=[A]), identify=lib_oracle, inspect=lambda ws, m: {"n": len(m)})`
    → `res.evidence["inspect"] == {"n": 1}`.
  - `test_run_job_inspect_error_is_a_warning`: an inspect hook that raises → `res.state` is not `"failed"` and one
    warning starts with `"Learning evidence failed:"`.
  - `test_collect_peaks`: same job with `inspect=collect` → every peak has all `PEAK_KEYS` + `"hits"`, areas > 0,
    at least one peak with a non-empty `name`, and the list is sorted by `rt`.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_learn_runner.py -q`. Expected: FAIL (unexpected
  keyword `inspect` / module missing).
- [ ] **Step 3: Implement.**
  - The hook call goes right after the project save and before `evidence_for`.
  - `collect_peaks` uses `peak_values.rows_for(ws, run_id, FID)` and `VALUES[k](row, ws, run_id, FID)`. Hits come
    from `row.ident.hits[:3]` when present.
  - Values are made JSON-safe (numbers as float, other values as str, `None` kept).
- [ ] **Step 4: Run** the tests. Expected: PASS. Also run `tests/test_pipeline.py -q` (unchanged behaviour). Expected: PASS.
- [ ] **Step 5: Commit** `Automation: run_job inspect hook; learn: per-peak evidence collector`.

### Task 2: Runner for one corpus entry, with cache

**Files:** Modify `gcws/learn/runner.py`. Test `tests/test_learn_runner.py`, `tests/test_learn_real.py`.

**Interfaces (produces):**
```python
@dataclass
class ProgramResult:
    run_dir: str; method: str; state: str; reason: str = ""
    peaks: list[dict] = field(default_factory=list)
    reported: list[dict] = field(default_factory=list)      # the NIAS report's reported rows {"name","cas","rt"}
    rows: list[dict] = field(default_factory=list)          # run_job evidence rows (slim combined rows)
    warnings: list[str] = field(default_factory=list); timings: dict = field(default_factory=dict)
def run_key(run_dir: str, method: str) -> str               # filesystem-safe: <run folder>-<method>-<sha1[:8] of path>
def build_spec(entry: CorpusEntry, method: dict, out_dir: Path) -> dict
def process(entry: CorpusEntry, out_root: Path, method_name: str = "NIAS", *, force: bool = False,
            identify=None) -> ProgramResult                  # cached at out_root/runs/<run_key>/program.json
def load_program(path: Path) -> ProgramResult
```

- [ ] **Step 1: Failing tests.**
  - `test_build_spec` (no Qt): for an entry with blanks `["03_EtOH_ISTD.D","10_EtOH.D"]`:
    - `group.members == [run folder name]`
    - `blanks == {"blank": ["10_EtOH.D"], "blank_istd": ["03_EtOH_ISTD.D"]}` (roles via `classify_role`)
    - `override.allow_no_blank is True`
    - `reports == [{"node": "learn", "kind": "nias", "formats": ["xlsx"]}]`
    - `batch_folder == entry.batch_dir`, `method["name"] == "M"`
  - `test_run_key`: stable; differs by method; only `[A-Za-z0-9_.-]`.
  - `test_process_caches` (qapp, sample copy, `identify=lib_oracle`):
    - The first call writes `program.json`.
    - A second call returns an equal `ProgramResult` without running the job (`run_job` monkeypatched to raise).
    - `force=True` runs it again.
  - `tests/test_learn_real.py::test_process_gio_run` (skip without data; real library search):
    - `state in {"accepted_auto", "control"}`, peaks > 50, at least 1 reported row
    - the run folder is unchanged (snapshot before/after)
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The method comes from `proc_method.load(method_name)`.
  - The job's `out_dir` is `out_root/runs/<run_key>/job`.
  - `reported` = the `"learn"` node's rows in `evidence["reported"]`. `peaks` = `evidence["inspect"]["peaks"]`.
  - A job exception gives `state="failed"` and `reason=str(exc)` (Review focus 3).
  - Writing the cache is atomic: write a temp file, then replace.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: runner (production job per evaluated run, cached)`.

### Task 3: Matcher and decision labels

**Files:** Create `gcws/learn/match.py`. Test `tests/test_learn_match.py` (synthetic `HumanEvaluation` + `ProgramResult`, no Qt).

**Interfaces (produces):**
```python
DECISIONS = ("istd", "reported_named", "reported_group", "reported_unknown", "reported_coelution",
             "reported_derivative", "kept_unreported", "background", "removed_blank", "removed_other")
@dataclass
class Pair:
    human: int | None        # index into HumanItems
    program: int | None      # index into ProgramResult.peaks
    kind: str                # "exact" | "inside" | "missing" (human only) | "extra" (program only)
    decision: str            # human decision ("" for extra)
    rt_diff: float | None
@dataclass
class HumanItem: rt: float; area: float | None; label: str; cas: str; row_class: str; decision: str; source: str  # "final"|"removed"
def human_items(ev: HumanEvaluation, report_rt_tol: float = 0.005) -> list[HumanItem]
def match_run(ev: HumanEvaluation, prog: ProgramResult, rt_tol: float = 0.02) -> list[Pair]
```
Decision rules (parsing only):
- Final rows with an RT (no `sum` rows). `istd` → `"istd"`; `background` → `"background"`.
- A final row whose RT is in the client report (within `report_rt_tol`) → `"reported_" + row_class`, where
  `named_no_cas` counts as `named`.
- Any other final row → `"kept_unreported"`.
- A removed FID peak (`workbook.removed_peaks`) → `"removed_blank"` when the matched program peak has a non-empty
  `in_blank`, otherwise `"removed_other"`. This decision is set in `match_run`, because it needs the program peak.

- [ ] **Step 1: Failing tests.**
  - `test_human_items_decisions`: from the phase 1 fixture workbook (`learn_fixtures.make_workbook` +
    `parse_workbook`):
    - 7.07 → `reported_named`, 17.878 → `reported_coelution`, 21.048 → `reported_group`
    - 6.917 → `kept_unreported`, 14.33 → `istd`
    - removed 5.132 and 6.409 present with `source="removed"`
    - no `sum` item
  - `test_match_exact_and_extra`: program peaks at 7.071 and 9.5 → the 7.07 human item is paired `exact` with
    `rt_diff≈0.001`, 9.5 is `extra`, and a human item with no program peak is `missing`.
  - `test_match_inside` (Review focus 5): a program peak with `start=21.0, end=21.5` and apex 21.2 and no peak near
    21.449 → the 21.449 item is `inside` that peak, and 21.048 is also `inside`. One program peak can take several
    `inside` pairs.
  - `test_removed_blank_vs_other`: removed 5.132 matched to a program peak with `in_blank="EtOH_3"` →
    `removed_blank`. Removed 6.409 matched to a peak with `in_blank=""` → `removed_other`.
  - `test_one_to_one_exact`: two human items at 7.00 and 7.01 and one program peak at 7.005 → exactly one `exact`
    pair (the nearer), and the other item is `inside` if within that peak's start/end, else `missing`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** a greedy one-to-one by ascending `|Δrt| ≤ rt_tol`. Then each unpaired human item →
  `inside` of the program peak whose `[start, end]` contains its RT, otherwise `missing`. Unpaired program
  peaks → `extra`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: matcher (analyst FID rows <-> program FID peaks) with decision labels`.

### Task 4: Scorer

**Files:** Create `gcws/learn/score.py`. Test `tests/test_learn_score.py` (synthetic, no Qt).

**Interfaces (produces):**
```python
@dataclass
class RunScore:
    client_f1: float | None; client_precision: float | None; client_recall: float | None
    name_agreement: float | None      # both named (CAS) and paired: share with equal CAS
    conc_dev_median: float | None; istd_scale: float | None
    worksheet_agreement: float | None # over paired items with decision in (kept_*, reported_*, background, removed_*)
    total: float | None
    disagreements: dict[str, int]     # type -> count (see below)
def program_reported_rts(prog: ProgramResult) -> list[float]
def score_run(ev: HumanEvaluation, prog: ProgramResult, pairs: list[Pair], items: list[HumanItem], *,
              weights=(0.7, 0.3), rt_tol: float = 0.02) -> RunScore
```
Definitions:
- **Client lines.** Human lines = items with decision `reported_*`. Program lines = `prog.reported` RTs.
  - A human line counts as a true positive when a program reported RT is within `rt_tol` of its paired program
    peak's RT (or of its own RT when unpaired). For `reported_named` it also needs the program row's CAS to equal
    the human CAS.
  - Precision = TP / program lines, recall = TP / human lines. F1 is `None` when both counts are 0.
- **Worksheet agreement.** Over paired items: the human *keeps* the peak (`kept_unreported`, `reported_*`) versus
  the program keeps it (the peak's `blank_ratio` is `None` or above 1, i.e. not explained by the blank). The
  human *removes* it (`background`, `removed_blank`, `removed_other`) versus the program removes it (the peak's
  `in_blank` is non-empty and `blank_ratio ≤ 1`). Agreement = share of equal keep/remove.
- **Disagreement types** (counts):
  - `missing_peak`: a human `reported_*` item without a pair
  - `not_reported`: a human `reported_*` item paired but not reported by the program
  - `extra_reported`: a program reported line with no human `reported_*` within `rt_tol`
  - `name_differs`: both named, CAS differs
  - `kept_but_program_removes` / `removed_but_program_keeps`: worksheet keep/remove mismatches

- [ ] **Step 1: Failing tests** with hand-built items/pairs/program:
  - `test_client_f1`: 3 human reported lines (named 97-88-1, group, coelution) and program reported at those 3 RTs,
    but the named one with CAS 50-00-0 → `client_precision == 2/3`, `client_recall == 2/3`, `name_agreement == 0`,
    `disagreements["name_differs"] == 1`.
  - `test_conc_dev_uses_istd_scale`: IS item area 100 paired to a program peak with area 50 → `istd_scale == 2`. A
    reported item with area 30 paired to program area 18 → `conc_dev_median ≈ 0.2`.
  - `test_no_istd` (Review focus 1): `istd_scale is None` and `conc_dev_median is None`, client F1 still computed.
  - `test_worksheet_agreement`: kept item with program `blank_ratio=None` (agree), removed_blank item with program
    `in_blank="b", blank_ratio=0.5` (agree), removed_other with program not in blank (disagree) → `2/3`, and
    `disagreements["removed_but_program_keeps"] == 1`.
  - `test_total_weights`: `total == 0.7 * f1 + 0.3 * ws`. `None` when either part is `None`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** Program CAS per reported RT comes from `prog.reported` rows (`normalise_cas`).
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: run scores (client F1, names, ISTD-relative concentration, worksheet agreement)`.

### Task 5: Baseline over the corpus and CLI

**Files:** Create `gcws/learn/baseline.py`. Modify `gcws/learn/__main__.py`. Test `tests/test_learn_baseline.py`.

**Interfaces (produces):**
```python
def run_baseline(root: Path, out_dir: Path, method_name: str = "NIAS", *, force: bool = False, limit: int | None = None,
                 process_fn=None, progress=print) -> dict
# writes out_dir/baseline.json ({"method", "runs": [{id, workbook, run_dir, analyst, state, reason, score}], "aggregate"})
# and out_dir/baseline.md (summary table, per-batch table, disagreement totals, failed runs)
# CLI: python -m gcws.learn baseline <root> [--out DIR] [--method NIAS] [--limit N] [--force]
#      (creates the offscreen QApplication as automation/child.py does)
```
Aggregate: mean and median of each score over scored runs, number of runs scored or failed, summed disagreement
types, and scores per batch.

- [ ] **Step 1: Failing tests** with `process_fn` injected (no Qt). It returns `ProgramResult`s built from the
  fixture workbook's own rows, so the "perfect program" scores 1.0:
  - `test_baseline_perfect_program`: client F1 = 1, worksheet agreement = 1, `baseline.md` has `## Summary`,
    `## Per batch`, `## Disagreements`.
  - `test_baseline_failed_run` (Review focus 3): `process_fn` returns `state="failed"` for one entry → that run is
    in `failed` with its reason and is not in the score means.
  - `test_baseline_processes_run_once` (Review focus 2): two workbooks for one run → `process_fn` is called once,
    two scored rows.
  - `test_cli_baseline_parses_args`: `main(["baseline", root, "--limit", "1", "--method", "X", "--out", out])`
    with `run_baseline` monkeypatched records `limit=1`, `method_name="X"`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Run the baseline on the real data** in the background:
  `.venv/Scripts/python.exe -m gcws.learn baseline "C:\Users\dbudi\Desktop\KI Projects\Testsample"`.
  Read `data/learn/baseline.md`. Expected: every run is scored or failed with a stated reason. Matcher or scorer
  gaps it exposes are fixed with a synthetic test before committing.
- [ ] **Step 6: Commit** `Learn: baseline over the corpus (CLI, Markdown + JSON report)`.

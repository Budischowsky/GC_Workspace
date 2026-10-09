# Learning report rules, phase 4 (identification, grouping, fallback labels): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Learn how the analysts turn above-limit peaks into client-report lines, and fit those rules against the
client F1 with batch-grouped cross-validation:
- family 3: when a peak is named
- family 4: which peaks form a family that is reported as one "Sum of …" line
- family 5: unknowns and the rest

**Why this shape (data, 2026-10-09):**
- **Grouping:** analyst "hydrocarbon" lines sit on program peaks named "Hydrocarbon" or with the class hint
  "Alkane (n- or branched; POSH/MOSH)". Analyst "styrene oligomer" lines sit on the program's styrene/α-methylstyrene
  oligomer hits and "2-Methoxy-2'-methyl-stilbene", which the program sums separately today. The analysts' sum
  wording is in their report footnotes ("Sum of hydrocarbons (alkanes, cyclic alkanes, …)").
- **Naming:** reported named peaks have program match scores of 79–99 (median 92). In 10 of 30 name differences
  the analyst's substance is the program's 2nd or 3rd hit.
- **Draft reports:** some client reports are drafts (the Zipper BDa report: 156 of 160 lines without a name). They
  must not teach that unnamed peaks are reported.

**Architecture:**
- `simulate.py` builds a client report from the cached program evidence and rule parameters, applying in order:
  ISTD exclusion → family 1 → family 2 → family 4 → family 3 → family 5. It is scored with the phase-2
  `score_run`, so every family is fitted against the client F1 directly, without re-running the library search.
- `families.py` learns the family table from the training batches only: analyst group labels → program evidence
  (names, class hints) and the analysts' sum wording.
- `fit.py` gains `fit_learned`: cross-validation in which a model (the family table) is learned on the training
  folds and scored on the held-out fold.
- The proposal contains the parameters and the learned family table, for the user to review. Nothing is applied
  (phase 6 applies rules in the real report pipeline).

**Spec:** `docs/superpowers/specs/2026-10-09-learn-from-evaluations-design.md` §5 (families 3–5), §8. Earlier plans:
phase 1–3 in `docs/superpowers/plans/2026-10-09-learn-*.md`.

## Global constraints

- Phase 1–3 constraints hold: local data only, read-only training root, no dialogs, proposals only.
- **No name lists in code.** Family members are learned (program names and class hints that co-occur with an analyst
  group label), stored in the proposal JSON, and shown in the proposal Markdown. Code may only contain parsing
  vocabulary (e.g. `"sum of"`).
- **Learning without leakage:** the family table used to score a fold is learned without that fold and without the
  locked test batches.
- **Draft client report:** more than 30 % of report lines without a name. Such workbooks are left out of phase 4
  fitting and listed. The corpus check flags them as `client report looks like a draft`.
- **Simulated report lines** have the shape of `ProgramResult.report_lines` (`{"rt","name","cas","kind"}`).
- **Program concentration (mg/kg)** of a peak = area × the run's median `mean_mgkg / area` over
  `prog.reported` rows matched to peaks (±0.02 min). A run without such a ratio is not simulated.

## Review focus

1. A family learned from one batch only: not kept (`min_batches` = 2), so a single product cannot create a family.
2. A peak matching two learned families: it goes to the family with more supporting peaks, ties in label order.
   That is deterministic.
3. Simulation of a run whose reported rows have no `mean_mgkg`: skipped and listed, never scored 0.
4. The current-equivalent parameters reproduce the real program report roughly. Task 2 reports both F1s for each
   run, so a broken simulation shows up.
5. An unknown line's name keeps the program's ion list (`unknown (m/z 105, 432, 106)`). It is not rewritten into
   the analyst's format, because the label text is not scored.

---

### Task 1: Draft client reports

**Files:** Modify `gcws/learn/workbook.py`, `gcws/learn/check.py`. Test `tests/test_learn_workbook.py`, `tests/test_learn_check.py`.

**Interfaces (produces):** `def report_is_draft(ev: HumanEvaluation, share: float = 0.3) -> bool`

- [ ] **Step 1: Failing tests.**
  - `test_report_is_draft`: 4 report rows, 2 without a label → draft. 1 of 5 → not draft. 0 rows → not draft.
  - The corpus check adds problem `client report looks like a draft (N of M lines without a name)`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: flag draft client reports`.

### Task 2: Simulated client report (ISTD, families 1–3, 5)

**Files:** Create `gcws/learn/simulate.py`. Test `tests/test_learn_simulate.py`.

**Interfaces (produces):**
```python
def peak_conc(prog: ProgramResult) -> list[float | None]                 # mg/kg per peak (Global constraints)
NAMING_SPACE = {"min_score": [80.0, 70.0, 75.0, 85.0, 90.0], "unknowns": ["report", "drop"]}
def simulate_report(prog: ProgramResult, params: dict, families: list[dict] = ()) -> list[dict] | None
#   params: min_score, unknowns, plus ratio_limit (default 3.0) and limit (default 0.01)
```
Order of rules per peak:
1. `istd` field non-empty → out.
2. `rules.background` → out.
3. Concentration below the limit, or `None` → out.
4. A learned family matches → collected into that family's sum line (Task 3; no-op without families).
5. `score ≥ min_score`, a name that does not start with "unknown", and a CAS present → line with name and CAS.
6. Otherwise, with `unknowns == "report"`: a line with the program's name (unknown text, or the hit name without
   CAS). With `"drop"`: out.

- [ ] **Step 1: Failing tests** with hand-built `ProgramResult`s:
  - ISTD out, blank peak out, below-limit out.
  - Named at score 85 with `min_score` 80, unknown line at score 60.
  - `unknowns="drop"` removes it.
  - `peak_conc` from two reported rows (ratio median), and `None` without `mean_mgkg`.
  - `simulate_report` returns `None` when no ratio exists.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Real check** (no commit gate): per run, F1 of the real program report vs. the simulated report with
  `{min_score: 0, unknowns: "report"}`. Print both means and record them in the ledger (Review focus 4).
- [ ] **Step 6: Commit** `Learn: simulated client report from cached evidence (ISTD, families 1-3, 5)`.

### Task 3: Learning the family table

**Files:** Create `gcws/learn/families.py`. Modify `simulate.py` (family step). Test `tests/test_learn_families.py`.

**Interfaces (produces):**
```python
def learn_families(runs: list[CachedRun], footnotes: dict[str, list[str]] | None = None, *, min_peaks: int = 5,
                   min_share: float = 0.6, min_batches: int = 2) -> list[dict]
# family: {"label": "hydrocarbon", "sum_text": "Sum of hydrocarbons (…)", "names": [...], "hints": [...],
#          "support": int, "batches": int}
def family_of(peak: dict, families: list[dict]) -> dict | None
```
- **Learning:**
  1. The analyst group label (casefold, plural s removed) of each `exact`-paired `reported_group` item gives the
     program peak's `name` and `class_hint` as evidence for that label.
  2. An evidence value joins a family when at least `min_share` of all exact-paired program peaks carrying it
     (in any analyst decision with a label) belong to that family's label, and it has at least `min_peaks` such
     peaks.
  3. A family needs `min_batches` batches.
  4. `sum_text` is the most frequent analyst footnote or line starting "Sum of" that contains the label words
     (`score.same_family`), else `"Sum of <label>"`.
- `CachedRun` gains `footnotes: list[str]` (from the workbook), set in `propose.load_cached_runs`.
- [ ] **Step 1: Failing tests:**
  - Two batches with "Hydrocarbon"-named peaks labelled hydrocarbon → family with that name, and sum_text from the
    footnote.
  - One batch only → no family.
  - An evidence value shared 50/50 with named peaks (below `min_share`) → not a member.
  - `family_of` follows Review focus 2.
  - `simulate_report` with a family gives one sum line (`rt None`, `kind "sum"`, name = sum_text) for all members
    above the limit.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: family table learned from analyst group labels (names, class hints, sum wording)`.

### Task 4: Fit with a learned model

**Files:** Modify `gcws/learn/fit.py`. Test `tests/test_learn_fit.py`.

**Interfaces (produces):**
```python
def fit_learned(target: str, space: dict, current: dict, batches: dict[str, list],
                learn: Callable[[dict, list], object], evaluate: Callable[[dict, object, list], float | None],
                **kw) -> FitResult
# FitResult gains model: object = None (the model learned on all cross-validation batches, for the proposal)
```
- For each fold, `model = learn(params, runs of the other CV batches)` and the score is
  `evaluate(params, model, fold runs)`. Test and per-batch scores use the model learned on all CV batches. The
  tie-break and acceptance are as in `fit`.
- [ ] **Step 1: Failing tests:**
  - A toy "model" = the mean optimum of the training runs; the held-out score is computed with it. A batch whose
    optimum differs shows that its own data was not used when it was held out (the score differs from in-sample).
  - `model` is set.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement** by sharing the CV and acceptance code with `fit`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: cross-validation with a model learned on the training folds`.

### Task 5: Naming fit, proposal, CLI; real run

**Files:** Modify `gcws/learn/propose.py`, `__main__.py` (target `naming`). Test `tests/test_learn_propose.py`.

- The `naming` target:
  - space = `NAMING_SPACE` × `{"family_min_share": [0.6, 0.5, 0.75]}`
  - `current = {"min_score": 0.0, "unknowns": "report", "family_min_share": 0.6}`, where `min_score` 0 stands for
    "as today"
  - `learn` = `learn_families(train runs, min_share=params["family_min_share"])`
  - `evaluate` = mean client F1 of `score_run(items, prog with report_lines=simulate_report(...), pairs)` over runs
  - Drafts and runs that cannot be simulated are left out and listed.
- The proposal also writes the family table (`## Learned families`: label, sum text, names, hints, support,
  batches) and `families` into the JSON.
- [ ] **Step 1: Failing tests:**
  - A synthetic corpus (fixture workbooks, perfect program from `test_learn_baseline._perfect` with program names
    and hints per item) → a proposal with a `## Learned families` section.
  - CLI `fit naming` parses.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Real run** `python -m gcws.learn fit naming <Testsample>` and read the proposal.
- [ ] **Step 6: Commit** `Learn: naming fit (families 3-5) with learned family table; proposal and CLI`.

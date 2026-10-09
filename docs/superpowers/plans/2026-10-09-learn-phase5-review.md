# Learning report rules, phase 5 (review list, verdicts, analyst consistency): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal (spec §9):**
- Every disagreement between program and analyst goes on a review list with its evidence.
- The user gives a verdict on each (analyst right / program right / both acceptable). The verdicts are stored and
  change the scores of the next baseline and fit.
- An analyst-consistency report compares the runs evaluated by two analysts.

**Architecture:**
1. `score_run` records each disagreement it counts in `RunScore.details`:
   `{"type", "rt", "human": index|None, "program": index|None, "line": index|None}`.
   It takes an optional `verdicts: dict[(type, rt rounded to 2), verdict]`. With "program" or "both", the
   disagreement is not counted:
   - the human line leaves the recall denominator (missing / not reported)
   - the program line leaves the precision denominator (extra)
   - a name difference becomes a true positive
2. `review.py`:
   - builds `ReviewItem`s from the cached runs: id, batch, run, analyst, type, RT, the analyst's side (decision,
     label, CAS, mg/kg) and the program's side (name, CAS, score, top hits, class hint, blank match)
   - stores verdicts in `<out>/review/verdicts.json`
   - gives each run's verdict dict to the scorer
3. `baseline.run_baseline` and the naming/background/report fits pass the verdicts. CLI:
   - `python -m gcws.learn review <root>` writes `review/items.json` + `review/review.md`
   - `python -m gcws.learn consistency <root>` writes `consistency.md`
4. UI: **Report² > View > Learning review...** opens a non-modal window (`ui/dialogs/learn_review.py`):
   - a filterable table of the items (type, batch, run, RT, analyst, program)
   - the evidence of the selected item
   - the buttons **Analyst right**, **Program right**, **Both acceptable**, plus a note field
   Each click saves at once. No modal prompts.

**Spec:** `docs/superpowers/specs/2026-10-09-learn-from-evaluations-design.md` §9.

## Global constraints

- Phase 1–4 and 6 constraints hold: local data only, no dialogs left waiting in tests, proposals are never applied
  automatically.
- **Item id** = sha1 of `batch|run|analyst|type|round(rt, 2)`, first 12 hex characters. It is stable across
  rebuilds, so verdicts survive a new baseline.
- **Verdict values:** `"analyst"` (the analyst is right: the program should change, so the disagreement keeps
  counting), `"program"`, `"both"`.
- **Consistency report:** for each run with two analyst workbooks, the analysts' items are paired by RT (±0.01).
  It reports:
  - agreement of keep/remove
  - agreement of the label class (named / group / unknown / unnamed / background)
  - CAS agreement where both named the peak
  - peaks only one analyst listed

  It also gives the totals over all pairs.

## Review focus

1. A verdict for an item that no longer occurs (a different program result) is kept in the file and does nothing.
2. Saving a verdict while another process reads the file: write to a temp file, then replace.
3. The window opened with no `items.json`: it shows a hint to run `review` first and no table.
4. Scoring with verdicts never raises for unknown types.
5. Analyst pairs where one workbook has no worksheet rows: listed as "not comparable".

---

### Task 1: Disagreement details and verdict-aware scoring

**Files:** Modify `gcws/learn/score.py`. Test `tests/test_learn_score.py`.
- [ ] **Step 1: Failing tests.**
  - `details` lists one entry per counted disagreement, with type and RT.
  - With `verdicts={("not_reported", 6.0): "program"}` that line no longer lowers recall.
  - With `{("extra_reported", 8.0): "both"}` precision is 1.
  - With `{("name_differs", 7.07): "program"}` it counts as a true positive.
  - With an `"analyst"` verdict the scores are unchanged.
- [ ] **Steps 2–4:** RED, implement, GREEN (all `tests/test_learn_score.py`).
- [ ] **Step 5: Commit** `Learn: disagreement details and verdict-aware scores`.

### Task 2: Review items, verdict store, CLI `review`

**Files:** Create `gcws/learn/review.py`. Modify `__main__.py`, `baseline.py`, `propose.py` (pass verdicts). Test `tests/test_learn_review.py`.

**Interfaces:**
```python
@dataclass
class ReviewItem: id: str; batch: str; run: str; analyst: str; type: str; rt: float
                  analyst_side: dict; program_side: dict; workbook: str
def item_id(batch, run, analyst, type, rt) -> str
def build_items(root: Path, out_dir: Path, method_name="NIAS", process_fn=None) -> list[ReviewItem]
def write_review(items, out_dir) -> Path                 # review/items.json + review/review.md (counts per type, per batch)
def load_verdicts(out_dir) -> dict[str, dict]            # id -> {"verdict","note","by","at"}
def save_verdict(out_dir, item_id, verdict, note="") -> None
def run_verdicts(out_dir, batch, run, analyst) -> dict[tuple, str]   # (type, rt2) -> verdict for score_run
```
- [ ] **Step 1: Failing tests:**
  - On the synthetic corpus, the items have both sides filled and stable ids.
  - `save_verdict` → `load_verdicts`; an invalid verdict raises `ValueError`; the save is atomic.
  - `run_baseline` with a saved "program" verdict raises that run's client F1.
  - CLI `review` writes the files.
- [ ] **Steps 2–4:** RED, implement, GREEN.
- [ ] **Step 5: Commit** `Learn: review list with evidence and stored verdicts (CLI review)`.

### Task 3: Analyst consistency report

**Files:** Create `gcws/learn/consistency.py`. Modify `__main__.py`. Test `tests/test_learn_consistency.py`.
- [ ] **Step 1: Failing tests:**
  - Two synthetic workbooks for one run, differing in one label class and one removed peak → the exact
    agreement numbers.
  - A pair where one workbook has no rows → "not comparable".
  - CLI `consistency` writes `consistency.md`.
- [ ] **Steps 2–4:** RED, implement, GREEN.
- [ ] **Step 5: Commit** `Learn: analyst consistency report for runs evaluated twice`.

### Task 4: Learning review window in Report²

**Files:** Create `gcws/ui/dialogs/learn_review.py`. Modify `gcws/ui/docks/report2.py` (View menu entry),
`gcws/manual/wf-learn.md` (window and buttons). Test `tests/test_learn_review_ui.py`.
- [ ] **Step 1: Failing tests** (`qtbot`, temporary `paths.DATA` with `learn/review/items.json`):
  - The window lists the items.
  - Selecting one shows its evidence.
  - **Program right** saves the verdict (`load_verdicts`) and the row shows it.
  - The type filter narrows the table.
  - Without `items.json` a hint label is shown.
  - The Report² View menu has **Learning review...**.
  - The manual coverage test passes (labels documented).
- [ ] **Steps 2–4:** RED, implement, GREEN (+ `tests/test_manual_coverage.py`, `tests/test_report2_ui.py`).
- [ ] **Step 5: Commit** `Report²: Learning review window (verdicts on program/analyst disagreements)`.

### Task 5: Real run

- [ ] Run `review` and `consistency` on the training data. Read both reports.
- [ ] Commit nothing new unless a gap shows (then test-first).

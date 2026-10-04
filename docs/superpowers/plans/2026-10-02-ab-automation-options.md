# A/B Automation and Workflow Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wait for a complete A/B pair without a sequence log, run the selected double determination actions automatically, and expose their controls with Report² settings in one per-workflow Options view.

**Architecture:** Keep pair readiness in the pure batch planner and pass the source node's pair requirement from the watcher. Store processing switches in method node parameters, resolve them against the saved method in a small pure module, then pass the resolved values to the child pipeline. The Options dialog edits the same workflow nodes that the chart editor uses.

**Tech Stack:** Python 3.12+, PySide6, pytest, JSON workflow files, SQLite journal.

**Spec:** `docs/superpowers/specs/2026-10-02-ab-automation-options-design.md`

## Global Constraints

- Every sample in the selected watched workflow requires A and B; existing workflows keep their behavior until **Require A and B before processing** is enabled.
- A and B filenames share a sample name apart from the injection prefix and final `_A` or `_B` suffix; no reliable sequence log is available.
- Start processing after both runs and required blanks are ready, without waiting for the whole batch folder to be quiet.
- Raw run folders remain read-only; a failed enabled comparison or harmonisation step prevents automatic acceptance and delivery.
- Preserve the existing workflow chart, method thresholds, journal revisions, and **Remove from queue** / **Process again** actions.
- Base the work on the current branch, including commit `45c1070`; do not rewrite the approved spec.

## Review Focus

- Different injection prefixes but the same sample stem must join one job: Task 1 planner and watcher tests.
- B arriving first, or A remaining alone after quiet time, must stay waiting for the missing partner: Task 1 tests.
- One unstable run or missing required blank must keep the pair unqueued: Task 1 tests.
- A workflow with several method/report branches must edit only the selected branch: Task 4 UI test.
- Disabled review rules must not auto-accept a failed enabled comparison or harmonisation step: Task 3 pipeline test.

---

### Task 1: Pair readiness without a sequence log

**Files:** Modify `gcws/automation/planner.py`, `gcws/automation/watcher.py`; test `tests/test_planner.py`, `tests/test_watcher.py`.

**Interfaces:** `plan_batch(present, seq, *, quiet, require="either", baseline=frozenset(), require_pair=False) -> BatchPlan`. The watcher passes `bool(wf.source.p("require_pair"))`. A waiting `GroupPlan.members` contains observed/planned real run stems; its key comes from the common sample stem and stays fixed when B appears.

- [ ] **Step 1: Write failing planner tests** for `require_pair=True`: A alone after quiet waits for B; B alone waits for A; distinct injection prefixes pair by normalized sample name; both ready with a ready blank become `READY` while `quiet=False`; an unstable member or missing blank remains waiting. Keep the existing single determination tests with `require_pair=False`.
- [ ] **Step 2: Run** `.\.venv\Scripts\python.exe -m pytest tests/test_planner.py -q`; expect the new cases to fail.
- [ ] **Step 3: Implement pair slots** in `plan_batch`. Group samples by the existing normalized sample key, accept only one A and one B per key, retain planned names when a log exists, and prevent quiet time from waiving a missing slot. Use the current blank selection and readiness checks after both slots exist.
- [ ] **Step 4: Write and run a watcher test** in `tests/test_watcher.py`: with no log, acquire all A runs before B, scan past quiet time, assert no child starts and the journal says “waiting for B”; add B and ready blanks, assert one child spec contains exactly the matching A/B members. Also assert a removed missing-partner job no longer blocks batch completion.
- [ ] **Step 5: Pass** `.\.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_watcher.py -q`, then commit only these files.

### Task 2: Persist and resolve workflow processing switches

**Files:** Create `gcws/automation/options.py`; modify `gcws/automation/workflow.py`, `gcws/ui/automation/node_dialogs.py`; test `tests/test_automation_model.py`, `tests/test_automation_ui.py`.

**Interfaces:** Source node parameter `require_pair: bool` defaults to `False`. Method node parameter `feature_options: dict[str, bool]` defaults to `{}` and may contain `compare`, `split_sync`, `gap_fill`, `consensus_search`, `consensus_names`, `harmonise`. Define `effective_feature_options(method_features: dict, overrides: dict) -> dict[str, bool]` in `options.py`; missing overrides inherit the saved method's feature settings. `compare` inherits whether the method uses feature pairing. `split_sync`, `gap_fill`, and `consensus_names` inherit their feature setting combined with `apply_auto`.

- [ ] **Step 1: Write failing model tests** for old JSON/default compatibility, round trips of `require_pair` and `feature_options`, inheritance versus explicit `False`, and validation of unknown option keys or `compare=True` with classic pairing when a method loader is available.
- [ ] **Step 2: Run** `.\.venv\Scripts\python.exe -m pytest tests/test_automation_model.py -q`; expect the new cases to fail.
- [ ] **Step 3: Implement defaults, resolver, and validation.** Preserve absent keys when reading old workflows; reject unknown keys and incompatible combinations with actionable `Issue` text.
- [ ] **Step 4: Add the pair and feature switches to existing source/method `NodeDialog` controls** so the chart edits the same node parameters. Add a round-trip test in `tests/test_automation_ui.py`.
- [ ] **Step 5: Pass** `.\.venv\Scripts\python.exe -m pytest tests/test_automation_model.py tests/test_automation_ui.py -q`, then commit only this task's files.

### Task 3: Execute selected actions and fail closed in Report²

**Files:** Modify `gcws/features/service.py`, `gcws/automation/watcher.py`, `gcws/automation/pipeline.py`; test `tests/test_features_consensus.py`, `tests/test_pipeline.py`, `tests/test_watcher.py`.

**Interfaces:** Extend `gcws.features.service.run(..., auto_kinds: frozenset[str] | None = None)` to filter automatically applied proposal kinds while preserving its current behavior for `None`. Watcher job specs carry `feature_options` from `effective_feature_options`. Extend `run_features(ws, members, options: dict | None = None) -> dict`; `None` preserves current behavior. Result evidence includes each switch's enabled/skipped/completed/failed state and existing applied counts.

- [ ] **Step 1: Write failing feature tests** showing `auto_kinds={"gapfill"}` leaves identity/split proposals unapplied while gap fills can apply, and `None` preserves the current Compare behavior.
- [ ] **Step 2: Run** `.\.venv\Scripts\python.exe -m pytest tests/test_features_consensus.py -q`; expect the new filter test to fail.
- [ ] **Step 3: Implement proposal-kind filtering**, effective feature settings, and watcher-to-child option transfer. Pass `apply_auto=True` when any selected proposal kind is enabled, even if the saved method disables its general automatic-application switch; never mutate the saved method. Recompute quantification after automatic edits; obtain comparison rows/verdicts from the same saved feature table used by Replicates / results.
- [ ] **Step 4: Write failing pipeline tests** for enabled versus skipped action evidence and an injected comparison/harmonisation exception with Report² rules disabled: the result must be `CONTROL`, include a failure finding, and retain a draft if report generation succeeds. Add a watcher spec assertion for option transfer.
- [ ] **Step 5: Implement the compulsory failure gate** after ordinary rule evaluation. A deliberately disabled comparison is recorded as skipped; only an enabled step's failure forces control.
- [ ] **Step 6: Pass** `.\.venv\Scripts\python.exe -m pytest tests/test_features_consensus.py tests/test_pipeline.py tests/test_watcher.py -q`, then commit only this task's files.

### Task 4: One Options view for a selected workflow

**Files:** Create `gcws/ui/automation/options_dialog.py`; modify `gcws/ui/docks/automation.py`, `gcws/ui/automation/node_dialogs.py`; test `tests/test_automation_ui.py`.

**Interfaces:** `WorkflowOptionsDialog(wf: W.Workflow, method_names: list[str], parent=None)` edits a copy and returns its `Workflow` through `values()`. The Automation dock opens it for `selected_id()`, saves the accepted workflow, and refreshes the table. The view selects a method branch when several exist and edits the linked Report², report, and folder nodes only.

- [ ] **Step 1: Write failing Qt tests** for the Options button, Input/Processing/Report²/Reports & delivery groups, save/reopen persistence, rule Off/Note/Control round trip, and two method branches where editing one leaves the other unchanged.
- [ ] **Step 2: Run** `.\.venv\Scripts\python.exe -m pytest tests/test_automation_ui.py -q`; expect the new cases to fail.
- [ ] **Step 3: Build the dialog** using the existing node parameters and `gcws.automation.rules` serialization. Show per-rule enabled state and level directly; open `RulesDialog` for numeric/pattern details. Present a branch selector for multi-method workflows and a report selector for multiple reports. Disable or explain switches dependent on feature comparison.
- [ ] **Step 4: Connect the dock button, workflow validation, and save.** Reopening the chart and Options must show the same values; Cancel must not mutate the saved workflow.
- [ ] **Step 5: Pass** `.\.venv\Scripts\python.exe -m pytest tests/test_automation_ui.py tests/test_automation_model.py -q`, then commit only this task's files.

### Task 5: End-to-end proof and user documentation

**Files:** Modify `tests/test_watcher.py`, `tests/test_pipeline.py`, `gcws/manual/wf-automation.md`.

**Interfaces:** Uses the pair planner and job-spec options from Tasks 1–3; no new production API.

- [ ] **Step 1: Add an end-to-end regression** with no sequence log and A then B: no report before B, one paired job after B and ready blanks, project with both group members, comparison evidence, and no automatic delivery on a forced comparison failure.
- [ ] **Step 2: Run** `.\.venv\Scripts\python.exe -m pytest tests/test_watcher.py tests/test_pipeline.py -q`; expect the new regression to pass with Tasks 1–3 complete, and treat any failure as an integration gap.
- [ ] **Step 3: Fix only integration gaps exposed by that regression**, then document the strict A/B option, queue handling, Options view, and Report² failure behavior in `gcws/manual/wf-automation.md`.
- [ ] **Step 4: Pass** `.\.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_watcher.py tests/test_pipeline.py tests/test_automation_model.py tests/test_automation_ui.py tests/test_features_consensus.py -q`. Check `git diff --check`, inspect the full diff, and commit only this task's files.

# Learning report rules, phase 6 (learned rules in the real report pipeline): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Approved learned rules live in a processing method and change the real NIAS report, with a reason on
every affected row. The first rule applied is the learned family table (family 4), measured by rerunning the
baseline with "NIAS (learned)".

**Why families first:**
- The phase 4 simulation showed families lift the client F1 the most (simulated 0.48 → 0.56, in-sample).
- The real pipeline has a concrete gap: the workspace identification already calls alkane peaks "Hydrocarbon"
  (class hint "Alkane …"), but the NIAS report names them by their top hit ("Octacosane", "Tetracosane", …).
  `classify_name` therefore never forms "Sum of hydrocarbons" and the report shows "Sum of Octacosane" instead.
- The analysts put the group label ("Hydrocarbon", "Styrene Oligomer") on such rows in their worksheets. Renaming
  matched rows to the family's label reproduces that, and the existing category sums
  (`NIAS Reporting.classify_name`, `SUMMARY_LABELS`) then build the analysts' sum line.

**Architecture:**
1. A new processing-method section `learned_rules` (stored in `ws.quant["learned_rules"]`, like
   `report_template`) holds the approved rule set:
   `{"version": 1, "approved": "<date>", "source": "<proposal>", "families": [...]}`.
2. `gcws/learn/apply.py::apply_families(sample, evidence, families)` renames the matching `PeakRow`s, clears their
   CAS, and records the reason in `row.derived["learned"]`.
3. `quant/service.compute` calls it after `build_sample` when the workspace has `learned_rules`. The evidence
   (identification name, class hint) comes from the workspace.
4. CLI `python -m gcws.learn apply-families <proposal.json> --method <name>` writes the table into that method.
   Only the named method changes.

**Spec:** `docs/superpowers/specs/2026-10-09-learn-from-evaluations-design.md` §3 (rule set in the method, reasons), §12 phase 6.

## Global constraints

- Without `learned_rules` in `ws.quant`, nothing changes: same rows, same names, same report. This is guarded by
  the existing pipeline and report tests passing unchanged.
- **The user's `NIAS` method is not touched.** The CLI writes only the method named with `--method` and refuses
  to run without it.
- **Reason text** (in `row.derived["learned"]`):
  `"family v1 (<label>): <evidence kind> '<value>' -> '<row_name>'; was '<old name>' (<old CAS>)"`
- A row whose name the analyst entered by hand (`ident.manual`) is never renamed.
- ISTD rows (`sample` standards / bound ISTD peaks) are never renamed.

## Review focus

1. A project saved with `learned_rules` and reopened keeps the rules (it lives in `ws.quant`).
2. A method saved from the window (`collect`) after a learned method was applied keeps the section. A method
   without it removes the section from the workspace, as `report_template` does.
3. Several family matches for one row: `family_of` (most support).
4. A family label `classify_name` does not know (a future family): its rows share one name and fall into the
   existing "sum of a substance found more than once" line. That is acceptable and documented.
5. Class hints need the MS data. A run without MS is matched by name only.

---

### Task 1: The `learned_rules` method section

**Files:** Modify `gcws/core/proc_method.py`. Test `tests/test_learn_apply.py`.

- [ ] **Step 1: Failing tests.**
  - `plan_quant({}, {"sections": {"learned_rules": {"version": 1, "families": [F]}}}, ["learned_rules"])["learned_rules"]["families"] == [F]`.
  - A method with `"learned_rules": None` removes it.
  - `"learned_rules" in PM.SECTIONS` and in `PM.WORKSPACE_SECTIONS`.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_learn_apply.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `SECTIONS["learned_rules"] = "Learned report rules (families)"`
  - `QUANT_SECTIONS["learned_rules"] = "learned_rules"`
  - add `"learned_rules"` to `WORKSPACE_SECTIONS`
- [ ] **Step 4: Run** the test, plus the existing method tests (`tests/test_ui.py -k method`, `tests/test_pipeline.py`).
  Expected: PASS.
- [ ] **Step 5: Commit** `Method: learned report rules section`.

### Task 2: Row name per family

**Files:** Modify `gcws/learn/families.py`. Test `tests/test_learn_families.py`.

- `learn_families` adds `"row_name"`: the analysts' most frequent original group label text for that family, e.g.
  "Styrene Oligomer" or "Hydrocarbon".
- [ ] **Step 1: Failing test:** labels "Hydrocarbon" ×4 and "hydrocarbon" ×2 → `row_name == "Hydrocarbon"`.
- [ ] **Step 2–4:** implement, run. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: family row name from the analysts' own label`.

### Task 3: Applying families to the NIAS sample

**Files:** Create `gcws/learn/apply.py`. Modify `gcws/quant/service.py` (`compute`). Test `tests/test_learn_apply.py`.

**Interfaces (produces):**
```python
def apply_families(sample, evidence: dict, families: list[dict]) -> int
#   evidence: row_id -> {"name": ident name, "hint": class hint, "manual": bool, "istd": bool}; returns renamed rows
def row_evidence(ws, st, det, key, sample) -> dict          # the workspace side (identification name, class hint)
```
- [ ] **Step 1: Failing tests.**
  - `apply_families` on hand-made `gc_model.PeakRow`s:
    - A row with evidence name "Hydrocarbon" → renamed to "Hydrocarbon", CAS "0", reason text as in Global
      constraints.
    - A row matched by hint only gets "hint" in its reason.
    - Manual and ISTD rows are left as they are.
    - Return value = number renamed.
  - `NIAS Reporting.classify_name("Hydrocarbon") == "hydrocarbon"` and
    `classify_name("Styrene Oligomer") == "styrene"`, so renamed rows reach the category sums.
  - Integration (`qapp`, `samples`, `lib_oracle` as in `test_pipeline`): a job whose method has
    `learned_rules.families` naming one of the sample's real identification names → the generated NIAS report
    (`runner.parse_program_report`) has no line with that row's old name at its RT, and `row.derived["learned"]`
    is set.
  - The same job without `learned_rules` → the report is unchanged (line names equal to a run without the section).
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** In `compute`, after `build_sample`:
  `fams = (quant.get("learned_rules") or {}).get("families")`. If set, call
  `apply_families(sample, row_evidence(...), fams)`.
  - `row_evidence` maps `row.derived["fid_peak"]` → `det.idents[n]` (name, manual) and
    `peak_values.class_hint(ws, st.id, key, det.peaks[n])`.
  - ISTD rows are the peaks bound in `quant["istd_bindings"][st.id]` or the sample's standards' RTs.
- [ ] **Step 4: Run** these tests, plus `tests/test_pipeline.py`, `tests/test_report.py`, `tests/test_quant.py`.
  Expected: PASS.
- [ ] **Step 5: Commit** `Report: learned families rename matching NIAS rows (group label, reason kept)`.

### Task 4: CLI `apply-families`, real rerun

**Files:** Modify `gcws/learn/__main__.py`, `gcws/learn/propose.py` (`apply_families_to_method`). Test `tests/test_learn_propose.py`.

**Interfaces (produces):**
```python
def apply_families_to_method(proposal_json: Path, method_name: str) -> Path
#   writes {"version": 1, "approved": today, "source": str(proposal_json), "families": [...]} into that method
# CLI: python -m gcws.learn apply-families <proposal.json> --method NAME   (--method is required)
```
- [ ] **Step 1: Failing tests:**
  - A temp `paths.DATA` with a stored method "M" → its `sections.learned_rules.families` equals the proposal's.
  - Other sections are unchanged.
  - The CLI without `--method` exits with an error.
  - A proposal without families raises `ValueError`.
- [ ] **Step 2–4:** implement, run. Expected: PASS.
- [ ] **Step 5: Real run:**
  - `python -m gcws.learn apply-families data/learn/proposals/naming.json --method "NIAS (learned)"`
  - `python -m gcws.learn baseline <Testsample> --method "NIAS (learned)" --out data/learn/learned_rules --force`
  - Compare with `data/learn/baseline.json` and `data/learn/learned/baseline.json`.
- [ ] **Step 6: Commit** `Learn: apply learned families to a named method (CLI)`.

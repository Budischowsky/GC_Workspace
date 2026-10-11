# 05: Switching the reported determination in Replicates

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** For an SD pair, the Replicates panel marks which run is reported and which is the
control, and lets the analyst switch the reported determination between A and B. The run switched to is
the one reported. The choice is stored with the replicate group. A switch after an accept makes the group
stale ("Accept again"). In "Edit in GC Workspace" a switch is a report change: the report goes back to
control and gets a new revision. Re-reporting the edited project uses the switched run. The audit trail
records who switched and which run is reported.

**Blocked by:** 03

**Status:** resolved

- [x] Group operations (S3): set the reported member; the default is A/`_1`; a switch after an accept is stale
- [x] Automation job (S1): re-reporting a project switched to B gives B's values
- [x] Edit session: changing the reported member counts as a report change
- [x] Audit trail entry for the switch
- [x] UI test (no modal dialogs): the panel shows reported / control and the switch changes the reported run
- [x] The manual (F1) documents the switch; the coverage test passes

## Comments

- 2026-10-11: group key `reported` (run id), set by `DD.set_reported` (stale when it changes; dropped by
  `pair` / `set_members` when the members change); `determination.of` uses it for a pair under Single
  determination only, else A. Replicates panel: chip *Reported: … · control: …* and **Switch reported
  determination** (undoable, audit *Reported determination switched* with before / after run). Edit session:
  `report_inputs` already counts every group key except the sign-off, so a switch is a report change. Pipeline
  `rereport` now keeps the project's own group (same members) instead of a fresh one, so the switch - and the
  analyst's DD edits, which were dropped before - reach the report again. Tests: `test_switching_the_reported_
  determination_of_a_single_determination_pair`, `..._is_a_report_change`, re-report to B in
  `test_single_determination_only_job` (S1), `test_switching_the_reported_determination_in_replicates` (UI).

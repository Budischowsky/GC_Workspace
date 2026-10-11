# 04: Watcher timing under single determination only

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** With Single determination only on, the watcher still waits for B, so the comparison is
ready when the analyst opens the sample. When the batch is finished (sequence log ended or folder quiet)
and B never came, A is ready and reported as a single determination with no "incomplete" state or flag.
When only B came, B is the reported determination and Report² shows the info line "A missing, B reported".
With the flag off, the planner behaves exactly as today.

**Blocked by:** 03

**Status:** resolved

- [x] Planner (S2), flag on: a pair with only A present waits while the batch is not finished
- [x] Planner, flag on: the batch is finished without B, so A is ready as an SD, not an incomplete double determination
- [x] Automation job (S1), flag on, only B present: B is reported, with the info line "A missing, B reported"
- [x] Planner, flag off: unchanged (existing planner tests pass)

## Comments

- 2026-10-11: The planner needed no change and takes no flag: it already waits for a B planned in the sequence
  log (or until the folder is quiet) and, once the batch is finished, plans the lone run as its own group. The
  flag only decides what the job calls that group: `determination.of` with `single_only` makes a lone suffixed
  run SINGLE (no incomplete state); a lone B (suffix not A/1/01/REP1) gets the note "A missing, B reported",
  which the `double_determination` rule shows as its info line. Tests: `test_planner_single_determination_only`
  (S2, flag on and off), `test_single_determination_only_b_alone` (S1). Manual: wf-double.md.

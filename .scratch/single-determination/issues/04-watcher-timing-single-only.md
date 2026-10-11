# 04: Watcher timing under single determination only

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** With Single determination only on, the watcher still waits for B, so the comparison is
ready when the analyst opens the sample. When the batch is finished (sequence log ended or folder quiet)
and B never came, A is ready and reported as a single determination with no "incomplete" state or flag.
When only B came, B is the reported determination and Report² shows the info line "A missing, B reported".
With the flag off, the planner behaves exactly as today.

**Blocked by:** 03

**Status:** ready-for-agent

- [ ] Planner (S2), flag on: a pair with only A present waits while the batch is not finished
- [ ] Planner, flag on: the batch is finished without B, so A is ready as an SD, not an incomplete double determination
- [ ] Automation job (S1), flag on, only B present: B is reported, with the info line "A missing, B reported"
- [ ] Planner, flag off: unchanged (existing planner tests pass)

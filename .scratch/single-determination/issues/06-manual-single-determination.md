# 06: Making a single determination by hand in Replicates

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** In the Replicates panel, the analyst can pick "— single determination —" as partner B
for any sample run, including taking a run out of its pair (A becomes an SD, B stays unused). The Accept
button reads **Accept single determination**, is enabled without a comparison, and records the sign-off
(who, when) with undo, as for pairs. A hand-made SD goes to Report² the same way a hand-made pair does
(Rename/Overwrite when the name exists). Accepting a workflow-processed SD in Replicates also accepts its
Report² entry.

**Blocked by:** 02

**Status:** resolved

- [x] Group operations (S3): make a run single (also out of a pair); accept an SD with sign-off; undo
- [x] Accept single determination is enabled for a lone run; its report is labelled single determination
- [x] A hand-made SD is listed in Report²; a workflow SD's Report² entry is accepted from Replicates
- [x] Audit trail entries for making single and accepting
- [x] UI test (no modal dialogs): "— single determination —" plus Accept single determination
- [x] The manual (F1) documents the manual SD; the coverage test passes

## Comments

- 2026-10-11: group operation `DD.make_single` (the group gets `single: true`, reused when the run was in a
  pair, the partner stays unused; pairing again drops it); `determination.of` treats a `single` group as an
  SD even with a suffix. Replicates panel: "— single determination —" as B calls `make_single` (undoable,
  audit *Single determination made by hand*); **Accept single determination** for any one-run selection
  (an incomplete DD accepted this way becomes an SD), one undo step, audit *Single determination accepted*.
  Report²: `_list_in_report2` takes one-run groups, its evidence carries the determination (SD info line);
  `EditSession.find_for` finds a one-run workflow report, so a workflow SD is accepted there too. Manual:
  wf-double "Making a single determination by hand", ref-panels rows.

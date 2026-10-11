# 06: Making a single determination by hand in Replicates

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** In the Replicates panel, the analyst can pick "— single determination —" as partner B
for any sample run, including taking a run out of its pair (A becomes an SD, B stays unused). The Accept
button reads **Accept single determination**, is enabled without a comparison, and records the sign-off
(who, when) with undo, as for pairs. A hand-made SD goes to Report² the same way a hand-made pair does
(Rename/Overwrite when the name exists). Accepting a workflow-processed SD in Replicates also accepts its
Report² entry.

**Blocked by:** 02

**Status:** ready-for-agent

- [ ] Group operations (S3): make a run single (also out of a pair); accept an SD with sign-off; undo
- [ ] Accept single determination is enabled for a lone run; its report is labelled single determination
- [ ] A hand-made SD is listed in Report²; a workflow SD's Report² entry is accepted from Replicates
- [ ] Audit trail entries for making single and accepting
- [ ] UI test (no modal dialogs): "— single determination —" plus Accept single determination
- [ ] The manual (F1) documents the manual SD; the coverage test passes

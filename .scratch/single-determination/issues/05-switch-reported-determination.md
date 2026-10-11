# 05: Switching the reported determination in Replicates

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** For an SD pair, the Replicates panel marks which run is reported and which is the
control, and lets the analyst switch the reported determination between A and B. The run switched to is
the one reported. The choice is stored with the replicate group. A switch after an accept makes the group
stale ("Accept again"). In "Edit in GC Workspace" a switch is a report change: the report goes back to
control and gets a new revision. Re-reporting the edited project uses the switched run. The audit trail
records who switched and which run is reported.

**Blocked by:** 03

**Status:** ready-for-agent

- [ ] Group operations (S3): set the reported member; the default is A/`_1`; a switch after an accept is stale
- [ ] Automation job (S1): re-reporting a project switched to B gives B's values
- [ ] Edit session: changing the reported member counts as a report change
- [ ] Audit trail entry for the switch
- [ ] UI test (no modal dialogs): the panel shows reported / control and the switch changes the reported run
- [ ] The manual (F1) documents the switch; the coverage test passes

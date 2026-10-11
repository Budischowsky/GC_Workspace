# Single determination: design

Status: ready-for-agent · 2026-10-11 · terms as in `CONTEXT.md` (Double determination section)

## Problem Statement

Some samples are analysed only once, and some are injected twice where the second run (B) is only a
control. GC Workspace treats every sample as a double determination. A sample with one run is flagged
"Double determination incomplete" and needs control, as if something went wrong. In the Replicates panel
a single run shows only "choose a partner B to compare", and **Accept** stays disabled, so the analyst
can't sign off a single determination or send it to Report². When B is only a control, there is no way
to report A alone while still comparing it with B.

## Solution

- **Single determination only** checkbox in the Quantification panel, saved with the method's quant
  settings. When it is ticked:
  - both runs of a pair are processed and shown side by side in the Replicates panel;
  - only the **reported determination** goes into the report: A (`_A` / `_1`) by default, and the
    analyst can switch to B there;
  - the other run is the **control determination**, compared but not reported.
- A run without a replicate suffix is always a single determination.
- With the box off, a suffixed run whose partner never came is an **incomplete double determination**.
  It is processed alone and needs control, as today.
- In the Replicates panel the analyst can make any sample run a single determination by hand and
  **Accept single determination**.
- A single determination's report looks like a double determination's (same layout and header) with
  one value column, labelled *single determination*. Automation follows the method: it processes,
  reports and lists single determinations in Report² like pairs.

## User Stories

1. As an analyst, I want a **Single determination only** checkbox in the Quantification panel, so that a method can say its samples are reported from one run.
2. As an analyst, I want that checkbox saved with the processing method, so that automation and every project using the method follow it.
3. As an analyst, I want a run without a replicate suffix to always be a single determination, so that samples injected once are never treated as failed pairs.
4. As an analyst, I want `_1` / `_2` (and `_A` / `_B`) to count as replicate suffixes, so that both naming styles form pairs.
5. As an analyst, with Single determination only ticked, I want both A and B processed (integration, identification, ISTDs, blanks, quantification), so that I can compare them.
6. As an analyst, with Single determination only ticked, I want A and B shown side by side in the Replicates panel with the usual comparison, so that I can judge the control.
7. As an analyst, I want A (`_A` / `_1`) to be the reported determination by default, so that I don't have to choose for every sample.
8. As an analyst, I want to switch the reported determination between A and B in the Replicates panel, so that I can report B when A's injection was bad.
9. As an analyst, I want the panel to show clearly which run is reported and which is the control, so that I never confuse them.
10. As an analyst, I want only the reported determination's values in the report, so that the report is a clean single determination.
11. As an analyst, I want the control determination left out of the report and kept in the project and audit trail, so that the report reads as one determination while the evidence stays traceable.
12. As an analyst, I want a single determination's report to keep the double determination layout and header, with one value column, so that reports look the same for every sample.
13. As an analyst, I want the report labelled *single determination* (also the `{determination}` template field), so that the customer knows how the value was obtained.
14. As an analyst, I want the batch report to list single and double determinations together, so that a mixed batch gives one report.
15. As an analyst, I want red A/B differences in an SD pair to be information in Report², so that a control determination doesn't block the report.
16. As an analyst, I want to be able to set that rule to "control needed", so that a lab that wants strict control can have it.
17. As an analyst, I want Report² to show an info line for a single determination, so that I see it at a glance.
18. As an analyst, I want "Double determination incomplete" never to fire for a single determination, so that planned single determinations aren't flagged as errors.
19. As an analyst, with the box off, I want a suffixed run whose partner never came to stay an incomplete double determination needing control, so that a missing B is still caught.
20. As an analyst, with the box on, I want the watcher to wait for B before processing, so that the comparison is ready when I open the sample.
21. As an analyst, with the box on, I want A reported anyway when B never comes (sequence ended or folder quiet), without an "incomplete" flag, so that a missing control doesn't hold up the report.
22. As an analyst, with the box on, I want B reported, with an info line "A missing, B reported", when only B came, so that the sample still gets a result.
23. As an analyst, I want to pick "— single determination —" as partner B in the Replicates panel for any sample run, so that I can do a one-off single determination without changing the method.
24. As an analyst, I want to split a run out of a pair and make it a single determination, so that I can report A alone when B is broken.
25. As an analyst, I want an **Accept single determination** button with the same sign-off (who, when) and undo as for pairs, so that single determinations are signed off the same way.
26. As an analyst, I want a hand-made single determination sent to Report² like a hand-made pair (Rename/Overwrite when the name exists), so that every result is in one place.
27. As an analyst, I want accepting a workflow-processed single determination in Replicates to accept its Report² entry too, so that I don't accept twice.
28. As an analyst, I want an accepted single determination to become stale ("Accept again") when I switch the reported determination or toggle the box, so that a changed result is never delivered with an old sign-off.
29. As an analyst, I want a workflow report sent back to control when such a change is saved from "Edit in GC Workspace", so that Report² reflects the change.
30. As an analyst, I want re-reporting an edited project to use the reported determination saved in it, so that my switch to B survives automation.
31. As a reviewer, I want the audit trail to record which run was reported and who switched it, so that the result can be traced.
32. As an analyst, I want the user manual (F1) to explain single determination, reported and control determination, so that new colleagues can use it.

## Implementation Decisions

- **One place answers "which determination is this".** A new small quant module, in the style of the
  quant mode module, takes the quant settings and a replicate group and answers:
  - the kind: single determination, double (N-fold) determination or incomplete double determination;
  - the reported member(s);
  - the control member, if any.

  Pipeline, report assembly, template data, rules evidence and the Replicates panel ask it instead of
  checking member counts themselves.
- **Method setting.** The quant settings get a "single determination only" flag (missing = off). It
  joins the quant keys that the processing method saves and loads. The Quantification panel shows it
  as a checkbox.
- **Replicate group schema.** A group can carry a **reported** member (run id; missing = the A/`_1`
  member, else the first by injection). Setting it and making a run single (taking it out of its pair)
  are new plain-data group operations next to pair / set members / accept. They follow the existing
  stale rule: a change to what was accepted clears the sign-off to "Accept again". Toggling the method
  flag counts as such a change.
- **Grouping stays name-based.** Suffix detection is unchanged (`_A`/`_B`, `_1`/`_2`, `repN`). A lone
  sample run without a suffix already becomes a one-member group, and that group is now a single
  determination in its own right.
- **Reports.** With an SD, report assembly (classic and template reports, single and batch) uses only
  the reported member's values. It uses the double determination layout and header with one value
  column, and labels the determination *single determination*. The control member is not in the report.
- **Rules / Report².** The job evidence records the determination kind and the reported/control members.
  - "Double determination incomplete" fires only for an incomplete double determination.
  - An SD adds an info finding ("single determination", or "A missing, B reported").
  - In an SD pair, the "substances to decide" rule reports at info level by default and can be set to
    control.
- **Planner.** With the flag on, a pair still waits for both members. When the batch is finished (log
  ended or folder quiet), a pair with only one member present is ready as an SD, not as an incomplete
  double determination. Without the flag, today's behaviour is unchanged.
- **Replicates panel.**
  - "— single determination —" as partner B is allowed for any sample run.
  - The Accept button reads **Accept single determination** for an SD and is enabled without a
    comparison.
  - For an SD pair the panel marks the reported and control runs and offers the A/B switch.
  - Accepting emits the existing accept signal, so Report² accepts a workflow entry, and a hand-made
    entry goes through the existing manual Report² path.
- **Edit session.** The reported member and the flag are report inputs, so changing them is a report
  change: the report goes back to control and a new revision is made.
- **Audit trail.** Switching the reported determination, making a run single and accepting an SD are
  logged with user and runs.
- No workflow setting is added. Automation follows the method only.

## Testing Decisions

- Good tests check behaviour from the outside: what the report contains, which findings Report² gets,
  which samples the planner says are ready, what the group says after an operation. They don't check
  which helper was called or how state is stored internally.
- **S1 – automation job** (headless job runner with the library search replaced, real sample data as in
  the pipeline tests):
  - SD-only pair: the report is labelled single determination, has A's values only, and B is processed
    but absent;
  - no "incomplete" finding, an SD info finding instead;
  - a run without a suffix is an SD;
  - only B present: B is reported, with an info finding;
  - a re-report of a project switched to B gives B's values.
  - Prior art: the pipeline tests.
- **S2 – planner** (pure):
  - flag on: a pair waits for B;
  - B missing after the batch is finished: ready as an SD with no incomplete state;
  - flag off: unchanged.
  - Prior art: the planner tests.
- **S3 – replicate group operations** (plain data):
  - set reported, make single, accept an SD;
  - stale after switching or toggling the flag;
  - the default reported member is A/`_1`.
  - Prior art: the double determination / duplicate view tests.
- The determination module is tested through S1–S3, not as its own seam.
- One thin Replicates panel UI test: switch A/B, Accept single determination enabled for a lone run.
  It must never open a modal dialog. Prior art: the Report² UI tests.
- The manual coverage test must still pass, with the new UI texts documented.

## Out of Scope

- Removing auto-accept (a separate change; the user plans it).
- A workflow-level determinations setting (dropped in favour of the method flag).
- Changing how replicate suffixes are recognised.
- N-fold (three or more) determinations with a single reported member.

## Further Notes

- The German *Einzelbestimmung* only appears in legacy import mapping, which already turns it into
  "single determination"; the UI and glossary are English only.
- Today's behaviour this replaces: a lone run gets `pairing: "single"` in the job and the "Double
  determination incomplete" rule flags it for control.
- Deliver in phases on this branch, one commit per phase, tests green, no push.

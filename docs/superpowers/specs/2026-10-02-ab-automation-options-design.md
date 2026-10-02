# A/B automation and per-workflow Options

## Intent and success criteria

GC batches may acquire every A determination before any B determination. There is no reliable sequence log listing future runs. Every sample in these watched batches requires an A and a B run; their filenames share a sample name and differ only in the injection prefix and final `_A` or `_B` suffix. A sample must not be processed or reported as a single determination because the folder became quiet. Once both runs and their required blanks are finished, the watcher should perform the configured double determination, harmonise peaks, compare results, and pass one result to Report² without waiting for the entire batch. Required blanks are already present when the pair is complete.

The analyst wants one per-workflow Options view in the Automation panel for the available unattended processing and Report² controls. Different workflows may use different choices. Existing chart routing and detailed method thresholds remain available.

Success means an A-first/B-later run yields one paired report, a missing partner stays visibly pending, enabled comparison actions and their evidence run automatically, and Report² does not automatically deliver a report when a required comparison action fails.

## Workflow and readiness model

The watcher derives a logical pair key from the normalized sample name, within one batch folder. It tracks separate A and B slots under that key. It creates a waiting job on the first observed member and records which partner is missing. It does not invent a future filename or assign a run from another batch. A and B can arrive in either order; the stable job key and display name do not change when the second run arrives.

Each observed run must pass the existing scanner's acquisition and stability checks. The planner can queue the job only when both A and B are ready and the blanks required by the workflow are present and ready. The absence of a sequence log or a quiet folder cannot waive the A/B requirement. Quiet time can still help establish that a run has finished when no stronger acquisition marker exists. Pair processing starts as soon as these conditions hold; it does not wait for batch completion.

A partner that never arrives leaves the job in **Waiting**, with a specific reason such as “waiting for B.” The existing **Remove from queue** action is the analyst's way to close that case and allow a batch report to finish. Changes to an already processed run or selected blank use the journal's revision mechanism and cause a new processing pass. A removed job stays removed until **Process again** is chosen.

## Processing sequence and evidence

One job loads both determinations and the selected blanks into one workspace. After applying the saved processing method, it runs the enabled library search and ISTD detection. With feature comparison enabled, it builds the feature table, applies the enabled automatic proposals (split carryover, gap fills, consensus identity), applies boundary harmonisation when enabled, recomputes quantification after changes, and computes the comparison rows and verdicts used by **Replicates / results** and the reports. Proposal application must be selectable by kind so that turning off one action does not disable the others. The saved project contains the A/B group and the resulting edits.

The pipeline records which steps were enabled, completed, skipped, or failed, plus counts of pairs, fills, names, split carryovers, harmonised boundaries, and red/yellow results. Report² displays this evidence with the report. An intentionally disabled optional step is recorded as skipped; an enabled step that fails is recorded as failed. A comparison or harmonisation failure still permits a draft report where possible, but forces **Control needed** and holds automatic delivery, regardless of an optional review rule's setting. No draft can be automatically accepted as a complete double determination after such a failure.

## Per-workflow Options view

Selecting a workflow in the Automation panel exposes **Options...**. The view has four groups:

- **Input:** watched folder, scan interval, run stability, required blanks, whether to process existing runs, and a **Require A and B before processing** checkbox. It explains the waiting behavior when that checkbox is on. This checkbox is on for the workflow described here; existing workflows retain their saved behavior until it is enabled.
- **Processing:** saved method, library search, ISTD detection, feature comparison, split carryover, gap filling, consensus naming/search, and boundary harmonisation. The saved method supplies initial values and detailed numeric thresholds. The workflow stores explicit overrides for these switches; an override does not change the method used elsewhere. Dependent switches are disabled or explained when feature comparison is off.
- **Report²:** automatic acceptance, tray notification, and each existing rule's Off / Note only / Control needed state, with access to the rule's numeric or pattern details. The compulsory failure gate above remains in force when a required comparison action fails.
- **Reports & delivery:** report kind, output formats, batch timing, and target folders. Complex routing and arrow filters remain editable in the chart.

The Options view edits the existing workflow nodes and their parameters, not a separate preferences store. The chart's node dialogs show the same values. When a chart contains several method, Report², or report nodes, the Options view identifies the branch being edited so a switch cannot silently affect another path. Old workflow JSON files load with their current defaults; saving writes only the new options needed for that workflow.

## Validation and tests

Workflow validation explains missing methods, incompatible switches, and absent output destinations before an active workflow runs. The queue and Report² surface failures with actionable reasons. The source run folders remain read-only.

Acceptance cases cover: A arriving before B, B before A, all A runs before all B runs, no sequence log, a quiet folder with only A, an unfinished run, a missing required blank, a permanently missing partner, and a changed run after a completed report. Processing tests check that enabled feature actions run once for the pair, comparison evidence and saved project agree, and an enabled action's failure requires analyst control. UI and persistence tests check that Options and chart settings stay in sync, workflow overrides survive restart, and existing workflows retain their previous behavior except where the explicit A/B requirement is enabled for this workflow.

## Scope

This design changes unattended workflows and their configuration. It does not change the interactive double determination workflow, detailed numeric tuning in saved methods, or the existing file-routing chart. The A/B requirement is configured for the relevant workflow rather than imposed on all existing workflows.

# GC Workspace: domain terms

Short definitions of the words the code and the manual use. Code that answers a question about one of
these terms lives in the module named after it.

## Automation and Report²

- **Journal**: the SQLite file the watcher and GC Workspace share (`gcws/automation/journal.py`). It
  stores batches, jobs, deliveries and events, and the allowed state transitions of a job. It does not
  decide what a state means for the views.
- **Report² entry**: one job row as Report² shows it, either a sample's report or a batch report. What an
  entry's state means and what may be done with it live in `gcws/automation/review.py`:
  - its **bucket** (the filter chip it counts for: control, accepted, waiting, not processed, failed,
    rejected or removed) and its **level** (colour);
  - **allowed** actions (accept, reject, set back to control, process again);
  - review and delete, each giving an **undo token**.
- **Undo time** (`UNDO_GRACE`): the seconds after an accept during which the analyst can still undo it.
  The watcher delivers an accepted report only after it.
- **Delivery**: copying a report's files to the workflow's target folders (`gcws/automation/export.py`).
  `export_state` is `done`, `partial` (delivered while control was needed) or `error`.
- **Done**: accepted and fully delivered (`export_state == "done"`, nothing pending). An entry accepted by
  hand, which no workflow delivers, is done once accepted. Partly delivered is not done.
- **Closed batch / Archive**: every live report of the batch is done, no more samples are to come, and it
  was not reopened since. Report² lists closed batches under Archive, the rest under To do.
- **Deleted** (in Report²): hidden, nothing on disk is touched. A deleted entry still in the queue is
  **removed** from it (state REMOVED); undoing the delete puts it back in the state it had.
- **Edit session** ("Edit in GC Workspace"): a Report² report bound to the project open in GC Workspace
  (job id, the revision it was opened at, its project file) with its **baseline**, the project values
  that can change a report (`gcws/automation/edit_session.py`). Saving a **report change** sends the
  report back to control; display changes are saved without that. A report that got a new revision
  while it was open is **stale**: its project is not written over. Two determinations are **the same
  members** when their run file names match, whatever the case and order.

## Double determination

- **Double determination (DD)**: two runs of one sample compared row by row, kept as a replicate group
  in the project (`ws.replicate_groups`). Its changes live in `gcws/quant/double_determination.py`;
  the rows and verdicts in `gcws/quant/duplicate_view.py`.
- **Edits**: the analyst's changes of a row (values, Report box, comment, deleted, outlier dismissed),
  kept per quantification mode and unit in the group.
- **Accepted / signoff**: who accepted the pair, when, and how many red rows were left to the default
  rule. Any later change of what was accepted (an edit, a reset, other members, another validity rule,
  a substance's name or CAS) makes it **stale**: "Accept again". Renaming the group does not.

## Quantification

- **Quant mode**: how a method turns peak areas into concentrations, stored as `quant["mode"]`
  (missing = NIAS mg/kg): NIAS mg/kg (migration, SML check), NIAS total extraction (µg/L), internal
  standard concentration, area percent, HS screening (MS only) and extraction (quant method). What a
  mode answers for itself (its own unit, the further and report units, unit conversion, detector,
  limits, where DD edits are kept, its own report) lives in `gcws/quant/modes.py`: callers ask
  `modes.of(quant)` instead of comparing the mode string.
- **Own unit / Conc.**: the unit the mode computes in; **further units** are the same result in other
  units; **Conc. 1 / Conc. 2** are the two units a report shows.

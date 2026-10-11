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

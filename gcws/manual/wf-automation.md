# Unattended processing (automation)

GC Workspace can process new data by itself: it watches the folder the instrument writes into, processes
every finished sample with a processing method, judges the report, and puts the files where they belong. You
only look at the reports that need a decision.

## The idea: a workflow

A **workflow** is a chain of steps drawn as a chart:

**Watched folder → Method → Report² → Report → Target folder**

| Step | What it does |
|---|---|
| **Watched folder** | The folder the instrument writes into. Each batch is a folder below it. |
| **Method** | Processes each sample with a saved processing method: integration, library search, internal standards, blanks, quantification, double determination. |
| **Report²** | Judges the result with rules: *accepted* or *control needed*. |
| **Report** | Writes the report files. |
| **Target folder** | Where the files are delivered. There can be several. |

The **arrows** between the steps can carry filters, so that for example only accepted reports are delivered,
Excel files go to one folder and Word files to another.

## Set up a workflow

You need a processing method first: set everything up for one batch by hand and save it with
**Method > Save current settings as Method...** (see [Methods and projects](wf-methods.md)).

1. **Automation > New workflow** and choose a template:

   | Template | What it sets up |
   |---|---|
   | **NIAS: Excel to folder A, Word + PDF to folder B** | Accepted reports only; the Excel files to one folder, Word and PDF to another. |
   | **NIAS: accepted reports out, reports to check to a review folder** | Accepted reports to one folder; drafts of the ones needing control to a review folder. |
   | **One report into one folder** | The shortest chain, without Report². |
   | **Empty chart** | Nothing: build it yourself. |

2. In the chart, **double-click a step** to set it up, and **double-click an arrow** to set its filter. Drag
   a step from the list on the left onto the chart to add one. Connect two steps by dragging from the dot on
   the right of a step onto the next step. `Delete` removes the selection. **Fit** shows the whole chart.
3. The **Checks** list on the left names what is still missing. Only a complete workflow can be switched on.
4. Tick **Active (the watcher runs it)** and **Save**.

### The settings of each step

**Watched folder**

| Setting | Default | Meaning |
|---|---|---|
| **Folder to watch** | | The folder above the batch folders. |
| **Batch folders** | directly below | How deep below the watched folder the batch folders are. |
| **Folder names** | * | Only batch folders whose name matches, for example `2601*`. Several patterns with `;`. |
| **Check every** | 5 min | How often the folder is looked at. |
| **Quiet time** | 30 min | Without a sequence log: a batch is processed once nothing changed for this long. It also ends a sequence that stopped early. |
| **A run must be at least** | 2 min old | A run younger than this is still being written. |
| **Unchanged for** | 2 checks | A run must look the same for this many checks. |
| **Skip folders older than** | 14 days | Batch folders not changed for longer are not looked at (0 = all). |
| **Also process the runs already in the folder** | off | By default only runs that arrive after watching started are processed. |

**Method**

| Setting | Default | Meaning |
|---|---|---|
| **Processing method** | | The saved method to use. |
| **Library search of all peaks** | on | |
| **Automatic ISTD detection** | on | Finds the internal standards as **Detect ISTDs...** does. |
| **Bind detected ISTDs** | High confidence only | Which detections are bound without asking. |
| **Blank needed** | As the blank subtraction uses them | Which blank a sample must have in its batch folder to be processed at all. |
| **Stop a sample after** | 30 min | A sample that takes longer is stopped and listed as failed. |

**Report²**

| Setting | Default | Meaning |
|---|---|---|
| **Accept reports without findings automatically** | on | Off: every report waits for you. |
| **Tray message when a report needs control** | on | |
| **Control needed when** - **Rules...** | the default rules | Own rules for this workflow. |

**Report**

| Setting | Default | Meaning |
|---|---|---|
| **Report** | NIAS Report | Which report. |
| **Files** | Excel and Word | Per sample: Excel report, Word report, PDF report (needs Microsoft Word), double determination workbook. Per batch: one Word / PDF report of all samples, a Report² summary workbook. |
| **Batch report** | When every sample of the batch is accepted | When the report of the whole batch is written. The other choice: **When every sample is processed**. |
| **Keep the intermediate workbook** | off | |

**Target folder**

| Setting | Default | Meaning |
|---|---|---|
| **Target folder** | | Where the files go. |
| **Subfolder** | {batch} | A subfolder below it. Placeholders: `{batch}` `{sample}` `{kind}` `{status}` `{date}` `{workflow}`. Empty = directly into the target folder. |
| **File exists** | Keep both (add _2, _3 ...) | Or **Replace**, or **Keep the existing file**. |
| **Allow a folder inside the watched raw-data folder** | off | Normally refused, so that reports never land among the raw data. |

**An arrow** (double-click it): **Report status** (accepted automatically, accepted by the analyst, control
needed), **Files** (which file types pass), **Sample names** and **Batch folders** (name patterns). Only
what matches every chosen condition passes. Nothing chosen = everything passes.

## The watcher

The **watcher** is a small separate program that does the work in the background. It has an icon in the
Windows tray and keeps running when GC Workspace is closed.

| Control (Automation panel or menu) | What it does |
|---|---|
| **Start** / **Automation > Start the watcher** | Starts it. |
| **Pause** / **Automation > Pause / resume the watcher** | Takes no new work until you resume. |
| **Check now** | Looks at the watched folders at once instead of waiting. |
| **Stop** / **Automation > Stop the watcher** | Ends it. |
| **Start with Windows** | Starts the watcher when you log on. |

It processes one sample at a time, each in its own process, so that a crash or a hanging Office program cannot
stop the watcher. The raw data are only read.

## When a sample is processed

A **sample** here is all determinations of one sample number - usually A and B. It is processed as soon as

- all its determinations are finished, **and**
- the blanks it needs, **from the same batch folder**, are finished.

The instrument's sequence log tells which runs are still to come, so the watcher knows to wait for run B or
for the blank after it. Without a log, the folder must be quiet for the **Quiet time**.

A sample **without the required blank** in its batch folder is not processed at all. Report² lists it under
**Not processed**; right-click it > **Process again > Process without a blank...** overrides that, and the report then carries a finding.

## The queue

The **Queue** in the **Automation** panel lists the samples that are waiting, being processed, failed or not
processed, with the reason.

A sample that can never be completed - its B run was never measured - would hold up the batch report. Select
it and press **Remove from queue**. The watcher then skips it and the batch report no longer waits for it.
**Show removed** lists such samples, and **Process again** brings one back.

## Report²: what needs your control

**Automation > Report²** opens the panel: one list of the reports, grouped by batch folder. A batch row shows a
bar with how many of its reports are accepted (green), need control (yellow), wait (blue) or failed / were
rejected (red), how many are delivered, and when something last happened.

**To do** lists the open batches: something still waits, needs control, failed or is not yet delivered.
**Archive** lists the batches whose reports are all accepted and delivered - a batch moves there by itself. The
archive shows one row per batch; the period box (**Today**, **7 days**, **30 days**, **12 months**) and
**Search sample or batch** find older ones (a sample name finds its batch). Double-click a batch for its batch
report, right-click it for **Reopen**, which brings it back to **To do** until its reports are decided again.

The chips above the list - **All**, **Control needed**, **Accepted**, **Waiting**, **Not processed**,
**Failed**, **Rejected**, **Removed** - count the samples and filter the list: click one to see only those,
click it again for all. The chip at the right shows whether the watcher is running; **Start watcher** starts it.

Select a report: the findings that sent it to control appear as labels above the buttons (hover over them for
all of them). Expand a sample in the list to see how many substances its double determination marked **Red - to
decide** and **Yellow - to check** (hover for their names); the details are in **Replicates / results**. **Delivered** shows ✓,
*pending*, *partly* or *failed*. Several reports can be selected (Ctrl / Shift); the buttons act on all of them.

| Button | What it does |
|---|---|
| **Accept** | Accepts the report with your name and time - one click, no comment needed (the arrow beside it: **Accept with comment...**). If you changed the sample, Report² makes the report again from the saved project first and checks for new findings; the updated report is delivered after acceptance. The next report needing control is selected. |
| **Reject** | Rejects it: choose a reason, or **Other...** for a comment of your own (optional). **View > Reject reasons...** changes the list. A rejected report can still be accepted. |
| **Open report** | Opens the Word, Excel or PDF file. |
| **Open in GC Workspace** | Opens the sample exactly as it was processed - runs, integration, names, internal standards, blanks - to check or correct it. |
| **Preview** | Shows the selected report beside the list (a batch row: its batch report). A report without a PDF is converted from its Word report by Microsoft Word the first time (a few seconds); the preview is kept in the job folder and is not delivered. A report with Excel only is opened with **Open report**. |
| **Undo** | For a few seconds after an accept, a reject or a delete, under the list. An accepted report is delivered only after that time. |
| **Rules...** | The rules, see below. |
| **View** | **Show deleted reports** lists what was deleted (greyed); **Reject reasons...** edits the reasons. |

**Right-click a sample** (or several):

| Item | What it does |
|---|---|
| **Open report** | One of its files. |
| **Open in GC Workspace**, **Open in Replicates / results** | The sample as processed; an A/B pair in its double determination. |
| **Accept**, **Accept with comment...**, **Reject** | As the buttons. |
| **Process again > Report again from the (edited) project** | After you corrected the sample and saved the project: makes the report again from it. |
| **Process again > Process again from the raw data** | Starts over from the raw data. |
| **Process again > Process without a blank...** | Processes a sample that has no blank in its batch. |
| **Remove from the queue...** | As **Remove from queue** in the Automation panel. |
| **Deliver to the target folders now** | Delivers the files again. |
| **Show history...** | What happened to the report - processed, accepted, rejected, delivered, by whom and when - and its files with where they were delivered. |
| **Open the job folder** | The folder with everything the job wrote. |
| **Copy sample name** | The names of the selected samples. |
| **Delete...** | Hides the report in Report². Nothing on disk is deleted - reports, project and delivered files stay - and a sample still in the queue is removed from it. **Restore** (with **Show deleted reports**) brings it back. |

**Right-click a batch**: **Open batch report**, **Open batch folder**, **Accept all "control needed"...**,
**Reject batch** (with a reason), **Process batch again...**, **Reopen** (in the archive), **Delete batch...**
(hides the batch with all its reports; the watcher no longer looks at the folder) and **Restore batch**.

Keys in the list: `A` accept, `R` reject, `Enter` open the report, `Ctrl+O` open in GC Workspace, `H` history,
`Del` delete, `Ctrl+Z` undo.

The usual way through a report that needs control: open its double determination, correct what the finding names,
then **Accept**. Switching to another pair with **Load from Report²…** beside **Compare** saves changes to the
current project. If the watcher is stopped, a requested report update waits in **Control needed** until it starts.
An edited report with new findings stays in **Control needed** for another review.

## The rules

**Rules...** sets what sends a report to **Control needed**. Each rule can mean **Control needed** or
**Note only** (the finding is listed, the report is still accepted), or be switched off.

| Rule | On by default | A finding when |
|---|---|---|
| **Substances to check manually** | yes | an identification is uncertain or unknown, the determinations disagree on a name, their difference is above the limit, or a substance was found in one determination only - for substances at or above the reporting limit. |
| **SML exceeded** | yes | a reported substance is above its specific migration limit. |
| **Internal standards (QC)** | yes | a standard was not found, its area differs between the determinations by more than 50 %, there is no ISTD factor, or it was quantified with another peak than the automatic detection found. |
| **Processing problems** | yes | the report could not be made completely. |
| **Processed without a blank** | yes | you allowed a sample to be processed without a blank. |
| **Double determination: substances to decide** | yes | the double determination marked a substance red. Yellow ones can be listed too. |
| **Double determination incomplete** | yes | the sample has one determination only, or the two could not be paired. |
| **No SML above the reporting limit** | no | a substance at or above the reporting limit has no SML. |
| **Substance above a concentration** | no | any substance, or those matching a name or CAS pattern, is above a concentration you set. |
| **Many unidentified substances** | no | there are more unidentified substances than allowed. |
| **Automatic deconvolution split** | yes (note only) | a peak split automatically had its areas divided by the MS component proportions (at or above the reporting limit), or an internal standard peak was split. How many peaks were split is listed too. |

With **Also make these the default rules for new Report² steps** the rules become the starting point for new
workflows. A workflow's Report² step can have its own.

In the automation the double determination also **harmonises the integration boundaries** by itself, which the
panel only proposes. What was done - pairs, gap fills, names, boundaries, red and yellow rows - is listed with
every report.

## Manage workflows

The **Workflows** table of the **Automation** panel shows each workflow with its watched folder, how often it
is checked, how many samples are waiting and how many need control. **Edit chart...** opens the chart,
**Duplicate** copies a workflow, **Delete** removes it, and **Import...** / **Export...** move a workflow to
another PC as a file.

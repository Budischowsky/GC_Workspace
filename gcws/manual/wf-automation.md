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
**Not processed**; **More > Process without a blank...** overrides that, and the report then carries a finding.

## The queue

The **Queue** in the **Automation** panel lists the samples that are waiting, being processed, failed or not
processed, with the reason.

A sample that can never be completed - its B run was never measured - would hold up the batch report. Select
it and press **Remove from queue**. The watcher then skips it and the batch report no longer waits for it.
**Show removed** lists such samples, and **Process again** brings one back.

## Report²: what needs your control

**Automation > Report²** opens the panel. It has two areas, **Control needed** and **Accepted**, grouped by
batch folder, and tabs for **Waiting / processing** and **Not processed / failed / rejected**.

Select a report to see

- **Findings** - why it needs control: rule, determination, substance, retention time, what was found;
- **Files** - the report files and where they were delivered;
- **History** - what happened to it, who accepted it and when.

| Button | What it does |
|---|---|
| **Open report** | Opens the Word, Excel or PDF file. |
| **Open in GC Workspace** | Opens the sample exactly as it was processed - runs, integration, names, internal standards, blanks - to check or correct it. |
| **Accept...** | Accepts the report with your name, the time and a comment (required when there are findings). It is then delivered along the arrows. |
| **Reject...** | Rejects it with a comment. |
| **More > Report again from the (edited) project** | After you corrected the sample and saved the project: makes the report again from it. |
| **More > Process again from the raw data** | Starts over from the raw data. |
| **More > Process without a blank...** | Processes a sample that has no blank in its batch. |
| **More > Remove from the queue...** | As **Remove from queue** in the Automation panel. |
| **More > Open the job folder** | The folder with everything the job wrote. |
| **More > Deliver to the target folders now** | Delivers the files again. |
| **Rules...** | The rules, see below. |

The usual way through a report that needs control: **Open in GC Workspace**, correct what the finding names,
save the project, **More > Report again from the (edited) project**, then **Accept...**.

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

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
| **Local copy** | Optional, between the watched folder and the method: copies every finished run to a folder on this PC, and the method works on the copy. For a watched folder on a slow network drive, see [Working from a slow network drive](wf-automation.md#working-from-a-slow-network-drive). |
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
| **Batch folders** | directly below | How deep below the watched folder the batch folders are. Runs dropped straight into the watched folder (or into a folder above the batch folders) count as a batch of their own. |
| **Folder names** | * | Only batch folders whose name matches, for example `2601*`. Several patterns with `;`. |
| **Check every** | 5 min | How often the folder is looked at. |
| **Quiet time** | 30 min | Without a sequence log: a batch is processed once nothing changed for this long. It also ends a sequence that stopped early. Data copied or moved in, whose files were written longer ago, does not wait: it is processed once its runs are finished. |
| **A run must be at least** | 2 min old | A run younger than this is still being written. |
| **Unchanged for** | 2 checks | A run must look the same for this many checks. |
| **Skip folders older than** | 14 days | Batch folders not changed for longer are not looked at again (0 = all). A folder that arrives later is always looked at, however old its files are. |
| **Also process the runs already in the folder** | off | By default only runs that arrive after watching started are processed. This holds again when you choose another folder to watch. |

**Local copy**

| Setting | Default | Meaning |
|---|---|---|
| **Local folder** | | The folder on this PC the runs are copied to, for example `C:\GC Data`. It must not lie inside the watched folder. |
| **Copies go to** | | Shows where a batch folder ends up: the layout below the watched folder is kept (`X:\GC\2610_A` → `C:\GC Data\2610_A`). |

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
what matches every chosen condition passes. Nothing chosen = everything passes. On the arrow into the
**Local copy**, **Batch folders** chooses which batch folders are copied.

### Working from a slow network drive

If the instrument writes to a network drive (for example `X:`) and reading it is slow - in the home office
over VPN, say - add a **Local copy** step. Drag **Local copy** from the list onto the chart: it is put between
the watched folder and the method by itself, and the filters of the arrows stay on the way to the method.
Double-click it and choose the **Local folder** on this PC.

- Only the watched folder is watched. As soon as a run is finished there, it is copied to the local folder,
  together with the files beside the runs (the sequence log). One copy runs at a time, beside the processing.
- A sample is processed once its runs and blanks are copied; the queue says *being copied* meanwhile. The
  method reads the copy, and the project saved with the report points to it, so **Edit in GC Workspace** is
  fast too.
- The copies are **kept**: nothing is deleted, and files you add to a copy stay. You can open the copies
  yourself as you would any batch. If a copy needed for **Process again** has been deleted, it is copied again
  from the watched folder.
- The watched folder is only read. A run that changes there after it was copied is copied again.
- A copy that fails (the disk is full, say) is noted once in the log and tried again at the next check.
- A workflow with a **Local copy** but no method only copies.

If the watched folder cannot be reached - the network drive is not there before the VPN connects - the
watcher notes it once in the log and looks again at every check. It goes on by itself as soon as the drive is
back; nothing needs to be started again. The chart editor, too, only notes a watched folder it cannot reach,
so a workflow can be edited and switched on without the VPN.

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
| **Start with GC Workspace** | On by default: starts the watcher when GC Workspace starts and a workflow is active, so that new data is never left waiting because nobody started it. |

It processes one sample at a time, each in its own process, so that a crash or a hanging Office program cannot
stop the watcher. The raw data are only read. Beside the state, the Automation panel shows what the watcher is
processing and copying. If the watcher or the PC stops while a sample is being processed, the sample goes back
to the queue and is processed again as soon as the watcher runs.

## When a sample is processed

A **sample** here is all determinations of one sample number - usually A and B. It is processed as soon as

- all its determinations are finished, **and**
- the blanks it needs, **from the same batch folder**, are finished.

The instrument's sequence log tells which runs are still to come, so the watcher knows to wait for run B or
for the blank after it. Without a log, the folder must be quiet for the **Quiet time** - unless the data were
copied or moved in and their files were written longer ago than that.

**Data put in again is processed again.** A batch folder that was taken out of the watched folder and put
back, or deleted and copied in again, is processed again: each of its samples gets a new revision in Report²,
and reports you had deleted there are shown again. The same holds for a single finished run deleted and copied
in again (a run folder of an Agilent `.D`). Otherwise a report deleted in Report² stays hidden, even when its
data change.

A sample **without the required blank** in its batch folder is not processed at all. Report² lists it under
**Not processed**; right-click it > **Process again > Process without a blank...** overrides that, and the report then carries a finding.

## The queue

The **Queue** in the **Automation** panel lists the samples that are waiting, being processed, failed or not
processed, with the reason.

A sample that can never be completed - its B run was never measured - would hold up the batch report. Select
it and press **Remove from queue**. The watcher then skips it and the batch report no longer waits for it;
the sample leaves the list. Adding it to the queue again brings it back. A *Requested* row (samples added by
hand) is removed the same way. When a batch folder is deleted, its samples leave the queue by themselves.

**Add samples...** puts samples into the queue by hand, in the window **Add samples to the queue**: choose the
**Workflow** (one that is active) and the **Batch folder**; its samples are listed as the watcher groups them
(hover one for its blanks) - tick those to process (**Select all**, **Select none**) and press
**Add to queue**. The batch folder may also lie outside the watched folder. The watcher processes them at its
next look, with the blanks of the same folder:

- without waiting for the quiet time,
- also samples that were there before watching started, lie in a folder too old or with a name the workflow
  skips,
- and samples that were processed already - as a new revision; a report deleted in Report² is shown again.

Until the watcher has taken them up they are listed as *Requested*. If the watcher is not running, it is
started. **Add to queue** in the **Folders** tab does the same for the selected batch folders (all their
samples) and runs, and so does **Send to Automation...** in the right-click menu of the **Folder** panel: select
runs or batch folders (a folder without runs sends the batch folders in it), and choose the workflow. A sample without the blank it needs is still not processed; Report² then offers
**Process without a blank...**.

## What is in the watched folders

The **Folders** tab of the **Automation** panel shows, for every workflow, its watched folder with the batch
folders and runs below it, beside their local copy (with a **Local copy** step), and what became of each
sample. It shows what the watcher saw at its last look - the top row says when that was - so GC Workspace
never has to read a slow network drive itself; the local copy folder is listed directly.

| Column | What it shows |
|---|---|
| **Name** | The workflow, a batch folder, a run, or another file in the batch folder (greyed, for example the sequence log). |
| **Watched folder** | A batch folder: *watched* (with or without a sequence log), *not looked at* because it is older than **Skip folders older than** or its name does not match **Folder names**. Batch folders deleted from the disk or in Report² are not listed - also those outside the watched folder that were added to the queue by hand or seen before the watched folder changed. A run: *finished*, *being written*, *there before watching: not processed*, *put in again*. |
| **Local copy** | How many finished runs are copied; a run: *copied*, *waiting to be copied*, *not copied* with the reason, *an older copy*. Folders and runs that are *only in the local copy* are listed too. |
| **Sample** | The sample a run belongs to, the samples a blank serves, or the number of samples of a batch. |
| **State**, **Why** | The sample's state in the queue or in Report² and the reason - for example how long a batch without a sequence log still waits until it is quiet. |

Select a row: **Open folder** opens it in the Explorer (double-click does the same), **Open local copy** its
copy, **Show in Report²** the sample's report (a batch: its batch report). The same is on the right-click menu.

## Report²: what needs your control

**Automation > Report²** opens the panel: one list of the reports, grouped by batch folder. A batch row shows a
bar with how many of its reports are accepted (green), need control (yellow), wait (blue) or failed / were
rejected (red), how many are delivered, and when something last happened.

**To do** lists the open batches: something still waits, needs control, failed or is not yet delivered.
**Archive** lists the batches whose reports are all accepted and delivered - a batch moves there by itself. The
archive shows one row per batch; the period box (**Today**, **7 days**, **30 days**, **12 months**) and
**Search sample or batch** find older ones (a sample name finds its batch). Double-click a batch for its batch
report, right-click it for **Reopen**, which brings it back to **To do** until its reports are decided again.

**Register** is the report register: every sample of every batch in one list, whatever its state, with its
**Batch**, **Workflow**, **Status**, **Done**, **Reviewed by**, **Decided** (when), **Delivered** and
**Processed**. A sample is *Done* when it is accepted and delivered (an entry **Accepted by hand** when it is
accepted); everything else is *Not done* - waiting, in work, needing control, failed, not processed, rejected,
or accepted but not delivered yet. The box beside the period shows **Done and not done**, only **Done** or only
**Not done**; click a column header to sort by it. Selecting a sample works as in the list (the buttons, the
right-click menu and the keys act on it). **Export register...** saves the register as shown - its filters and
order - as an Excel workbook, with the reason and comment of each sample.

The chips above the list - **All**, **Control needed**, **Accepted**, **Waiting**, **Not processed**,
**Failed**, **Rejected**, **Removed** - count the samples and filter the list: click one to see only those,
click it again for all. The chip at the right shows whether the watcher is running; **Start watcher** starts it.

Select a report to act on it and see its preview. Expand a sample in the list to see how many substances its double determination marked **Red - to
decide** and **Yellow - to check** (hover for their names); the details are in **Replicates / results**. **Delivered** shows ✓,
*pending*, *partly* or *failed* (hover for the files and where they went). Several reports can be selected (Ctrl / Shift); the buttons act on all of them.

| Button | What it does |
|---|---|
| **Accept** | Accepts the report with your name and time - one click, no comment needed (the arrow beside it: **Accept with comment...**). If you changed the sample, GC Workspace first makes the report again from the saved project at once - the watcher is not needed - and the updated report comes back as **Control needed** (new findings of it are listed): check it, then accept it. The next report needing control is selected. |
| **Reject** | Rejects it: choose a reason, or **Other...** for a comment of your own (optional). **View > Reject reasons...** changes the list. A rejected report can still be accepted. |
| **Open report** | Opens the Word, Excel or PDF file. |
| **Save as** | **Save as Word...** or **Save as Excel...**: a copy of the report where you choose - also when the workflow delivers it nowhere. A report the workflow wrote without Word gets its Word report made from the Excel report. With a batch row (or its batch report) selected: the batch report, as below. |
| **Edit in GC Workspace** | Opens the sample exactly as it was processed - runs, integration, names, internal standards, blanks - to check or correct it, whatever its state: also an accepted or a rejected report, a single determination or a group of three. See [Editing a report](wf-automation.md#editing-a-report). |
| **Preview** | Shows the selected report beside the list (a batch row: its batch report). A report without a PDF is converted from its Word report by Microsoft Word the first time (a few seconds); the preview is kept in the job folder and is not delivered. A report with Excel only is opened with **Open report**. |
| **Undo** | For a few seconds after an accept, a reject or a delete, under the list. An accepted report is delivered only after that time. |
| **Rules...** | The rules, see below. |
| **View** | **Show deleted reports** lists what was deleted (greyed); **Reject reasons...** edits the reasons. |

**Right-click a sample** (or several):

| Item | What it does |
|---|---|
| **Open report** | One of its files. |
| **Save as Word...**, **Save as Excel...** | As the **Save as** button. |
| **Edit in GC Workspace**, **Open in Replicates / results** | The sample as processed, to change it; an A/B pair in its double determination. |
| **Accept**, **Accept with comment...**, **Reject** | As the buttons. |
| **Set status > Accepted**, **Set status > Control needed** | **Accepted** accepts the report (as **Accept**). **Control needed** sets an accepted (or rejected) report back to control: it is checked again and accepted again. Files delivered already stay in the target folders; accepting it again delivers what is missing. |
| **Process again > Report again from the (edited) project** | After you corrected the sample and saved the project: makes the report again from it. |
| **Process again > Process again from the raw data** | Starts over from the raw data. |
| **Process again > Process without a blank...** | Processes a sample that has no blank in its batch. |
| (an entry **Accepted by hand**) | A double determination accepted in Replicates that no workflow processed: **Process again** and **Deliver to the target folders now** are not offered - open it in Replicates, change it and accept it again. |
| **Remove from the queue...** | As **Remove from queue** in the Automation panel. |
| **Deliver to the target folders now** | Delivers all its files again, also those delivered before (the earlier copy of this revision is replaced). A delivered file that was deleted from a target folder is delivered again by itself the next time the report is delivered. |
| **Show history...** | What happened to the report - processed, accepted, rejected, delivered, by whom and when - and its files with where they were delivered. |
| **Open the job folder** | The folder with everything the job wrote. |
| **Copy sample name** | The names of the selected samples. |
| **Delete...** | Hides the report in Report². Nothing on disk is deleted - reports, project and delivered files stay - and a sample still in the queue is removed from it. **Restore** (with **Show deleted reports**) brings it back. |

**Right-click a batch**: **Open batch report**, **Save batch report as Word...**, **Save batch report as
Excel...**, **Open batch folder**, **Open the local copy** (with a **Local copy** step),
**Accept all "control needed"...**,
**Reject batch** (with a reason), **Process batch again...**, **Reopen** (in the archive), **Delete batch...**
(hides the batch with all its reports; the watcher no longer looks at the folder) and **Restore batch**.

**Save batch report as Word...** saves every sample's report of the batch one after the other in one Word
document - a copy of the batch report when it is up to date, otherwise it is made at once from the samples'
current reports (in the background; the bar under the list says when it is saved). **Save batch report as
Excel...** saves two workbooks into the folder you choose: the Report² summary (one row per sample with its
status, reviewer and findings) and *(batch name)_Sample_Reports.xlsx*, with every sample's Excel report as a sheet
of its own.

Keys in the list: `A` accept, `R` reject, `Enter` open the report, `Ctrl+O` edit in GC Workspace, `H` history,
`Del` delete, `Ctrl+Z` undo.

The usual way through a report that needs control: open its double determination, correct what the finding names,
then **Accept**. Switching to another pair with **Load from Report²…** beside **Compare** saves changes to the
current project. The updated report is made in GC Workspace, also when the watcher is stopped (the list says
*Updating report* meanwhile); if GC Workspace is closed before it is done, the watcher makes it. The updated
report is never accepted by itself: it comes back as **Control needed** - look at it, then **Accept** it. A report
that cannot be made stays in **Control needed** with the reason.

## Editing a report

Any report can be changed afterwards, also one already accepted and delivered: select it in Report² and press
**Edit in GC Workspace** (`Ctrl+O`). Its project opens - an A/B pair in its double determination, otherwise
with its first determination shown - and the status bar says **Editing the Report² report** with its name and
state. Change what is needed: integration, names, internal standards, blanks, the double determination.

- **Update report** saves your changes and makes the report again at once, in GC Workspace (the watcher is not
  needed), as a new revision. Updating never accepts it: the updated report comes back in Report² as **Control
  needed** - new findings of it are listed - and once you accept it there it is delivered to the target folders
  again, by the folder's **File exists** rule. You can go on editing and update again.
- **Stop editing** leaves the report as it is. Changes you have not saved yet: **Yes** saves them to the
  report's project (the report then waits in **Control needed**; **Accept** makes it again, to be checked),
  **No** keeps them open here only - **Save project** then asks where to save.

Saving the project (`Ctrl+S`) while editing also saves into the report's project, and the report goes back to
**Control needed** until it is updated or accepted. An entry **Accepted by hand** is listed again from its
double determination. A report accepted by mistake goes back with right-click > **Set status > Control needed**.

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

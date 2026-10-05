# From raw data to a report

This is the whole path in eight steps, for a sample measured as a double determination with a blank. Each step
links to the chapter that explains it.

| Step | What you do | Where |
|---|---|---|
| 1 | Load the runs of the batch: the two determinations, the blank, the blank with internal standards. Then **Method > Run Method** processes them. | [Load and view data](wf-load.md) |
| 2 | Check the integration and correct single peaks if needed. | [Integrate and correct peaks](wf-integrate.md) |
| 3 | Check the roles and the blanks that were suggested. | [Blanks, internal standards and quantification](wf-quantify.md) |
| 4 | Run the library search. | [Identify the peaks](wf-identify.md) |
| 5 | Check the internal standards and the quantification settings. | [Blanks, internal standards and quantification](wf-quantify.md) |
| 6 | Compare the two determinations and decide the red rows. | [Double determination](wf-double.md) |
| 7 | Preview and write the report. | [Reports](wf-report.md) |
| 8 | Save the project. | [Methods and projects](wf-methods.md) |

## What happens by itself

Much of this needs no click:

- Loading a run only reads it. **Method > Run Method** (`Ctrl+R`) **integrates** every loaded run with the
  integration method of its signal, subtracts the blanks, searches the libraries, detects the internal
  standards and quantifies.
- The **role** of a run (sample, blank, blank + ISTD, alkane ladder) is read from its name and the sequence,
  and the **blanks** of a sample are suggested from the injection order in its batch folder.
- The **FID-MS delay** - the small time difference between the two detectors - is estimated for each run, so
  that a substance sits at the same place in both chromatograms.
- The **internal standards** are found by name and target retention time once the peaks have names.

You check these and correct them where needed; the status bar shows role, blank and delay of the active
chromatogram at all times, and turns a chip yellow when a sample has no blank or the delay is uncertain.

## Doing the same for every sample

Once the settings are right for one batch:

- **Method > Save current settings as Method...** stores all of them under a name. **Method > Load Method...**
  applies them to the next batch. See [Methods and projects](wf-methods.md).
- The **automation** does steps 1 to 7 by itself for every sample the instrument finishes, and shows you only
  the reports that need a decision. See [Unattended processing](wf-automation.md).

# The window at a glance

GC Workspace is one window made of **panels**. Each panel does one job, and you arrange them as you like.
This chapter names the parts, so that the rest of the manual can refer to them.

## The parts of the window

| Part | Where | What it is for |
|---|---|---|
| **Menu bar** | top | Every function of the program, grouped by task: **File**, **Edit**, **Method**, **View**, **Chromatogramm**, **Mass Spectrum**, **Integration**, **Identify**, **Quantify**, **Report**, **Automation**, **Layout**, **Help**. See [Menus](ref-menus.md). |
| **Main toolbar** | below the menu bar | The functions used most: load, open, save, undo, redo, **Integrate**, **Library search...**, **Subtract baseline**. |
| **Integration tools** | beside the main toolbar | The mouse tools for the chromatograms: select, pan, draw a baseline, split, delete, add ... See [Toolbars and tools](ref-tools.md). |
| **Panels** | the rest of the window | See the table below. |
| **Status bar** | bottom | Messages, the role, blank and FID-MS delay of the active chromatogram, the current tool, the current processing method, and the progress of long tasks with **Cancel**. |

## The panels

| Panel | What it shows |
|---|---|
| **Folders** | The folders of your PC with the runs in them, and below it the **Loaded samples** list. |
| **Chromatogram 1** | A chromatogram of the loaded runs. By default the FID signal - the one used for quantification. |
| **Chromatogram 2** | A second chromatogram on the same time axis. By default the TIC of the MS - the one used for identification. |
| **Peaks / substances** | The peak table of one of the two chromatograms: retention time, area, name, CAS, score, concentration ... |
| **Mass spectrum** | The spectrum of the selected peak or of a point you right-clicked in a chromatogram, with its interpretation and library hits. |
| **Integration method** | The integration parameters, timed events and the list of your manual changes. |
| **Properties** | Facts about the active chromatogram: role, blanks, FID-MS delay, noise. |
| **Audit trail** | Every change made in this project, with user and time. |
| **Quantification** | The quantification mode, internal standards and NIAS parameters. |
| **Replicates / results** | The double determination and groups of three or more determinations, with the report buttons. |
| **Automation** | The watcher and the workflows for unattended processing. |
| **Report²** | The reports the automation made: accepted, or needing your control. |

Each panel is described control by control in the **Reference** part.

## Arranging the panels

- **Show or hide** a panel in the **View** menu. The menu stays open while you tick several panels; click
  anywhere else to close it.
- **Move** a panel by dragging its title. Near an edge of the window a band shows where it will dock. Drop it
  on the middle of another panel to put both behind tabs.
- **Detach** a panel with the detach button in its title, for example to put it on a second screen.
- **Maximize** a panel by double-clicking its title; double-click again to get the layout back.
- **Collapse the Folders panel** with **◀** to gain width; the narrow **Folders** strip brings it back.
- The **Layout** menu has ready-made arrangements (**Chromatogram top**, **Table left (classic)**,
  **Integration**, **Review**, **Dual monitor - table detached**), lets you save your own, and can
  **Lock panels** so that nothing moves by accident.
- **Light Mode**, **Dark Mode** and **Dark Mode - Neon** in the **Layout** menu change the look. Pictures you
  export and reports always stay on white paper.

The arrangement and the look are remembered for the next start.

## The active chromatogram

Several runs can be loaded at once. One of them is the **active chromatogram**: the one selected in the
**Loaded samples** list. The peak table, the spectrum, **Integrate** and most menu items work on it. The
other loaded runs are drawn thin behind it (**Overlay**).

## Undo and the audit trail

Almost everything you change can be taken back with `Ctrl+Z` and repeated with `Ctrl+Y`: manual integration,
names, internal standards, settings, a loaded method. Each run has its own undo history, so undo takes back
the last change *of the active chromatogram*.

Every change is also written to the **Audit trail** panel with user, time and what was changed. If
**Ask for a reason for every manual integration (GLP)** is switched on in **Edit > Preferences...**, the
program asks for a reason each time.

## Projects

A **project** (`.gcws` file) stores your work: which runs are loaded, their roles and blanks, the methods,
your manual changes, the names, the quantification settings, the replicate groups and the audit trail. The
raw data files are never changed. An autosave is written every two minutes; **File > Recover autosave...**
brings it back after a crash.

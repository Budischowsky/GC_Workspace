# Menus

Every item of the menu bar, in the order of the menus. Items ending in "..." open a window.

## File

| Item | Key | What it does |
|---|---|---|
| **Open folder...** | `Ctrl+Shift+O` | Shows a folder in the tree of the **Folders** panel. |
| **Load chromatograms...** | `Ctrl+L` | Loads a run, or all runs of a batch folder. |
| **Load Shimadzu QGD files...** | | Loads Shimadzu full-scan `.qgd` files. |
| **Open project...** | `Ctrl+O` | Opens a saved project (`.gcws`). |
| **Save project** | `Ctrl+S` | Saves the project. |
| **Save project as...** | `Ctrl+Shift+S` | Saves the project under a new name. |
| **Recent projects** | | The projects opened last. |
| **Recover autosave...** | | Opens the automatic save written every two minutes. |
| **Export peak table...** | | Writes the peak table, as shown, to an Excel or CSV file. |
| **Export chromatogram...** | | Saves a chromatogram as a picture. See [Dialogs](ref-dialogs.md#export-chromatogram). |
| **Close all chromatograms** | | Closes all loaded runs. |
| **Exit** | `Ctrl+Q` | Closes the program. |

## Edit

| Item | Key | What it does |
|---|---|---|
| **Undo** | `Ctrl+Z` | Takes back the last change of the active chromatogram. The tooltip names the change. |
| **Redo** | `Ctrl+Y` | Makes it again. |
| **Preferences...** | | Folders of helper programs and two working habits. See [Methods and projects](wf-methods.md#preferences). |

## Method

| Item | What it does |
|---|---|
| **Save current settings as Method...** | Stores all processing settings under a name. |
| **Load Method...** | Applies a stored method, wholly or in parts; also imports, exports and deletes methods. |
| **Report template...** | Designs the Template Report the method makes: columns, header, rows, extras, with a live preview (see [Report templates](wf-report-templates.md)). |
| **Run Method** (`Ctrl+R`) | Processes the loaded runs with the loaded method: integration, blank subtraction, library search, internal standards, quantification. Loading runs does none of this. |

See [Methods and projects](wf-methods.md#processing-methods).

## View

One item per panel: **Folders**, **Chromatogram 1**, **Chromatogram 2**, **Peaks / substances**,
**Mass spectrum**, **Integration method**, **Properties**, **Audit trail**, **Quantification**,
**Replicates / results**, **Automation**, **Report²**. A tick shows the panel, no tick hides it. The menu
stays open while you tick; click elsewhere to close it.

| Item | Key | What it does |
|---|---|---|
| **Go to panel** ▸ one item per panel | `Ctrl+1` ... `Ctrl+9` | Shows the panel, brings it to the front and puts the keys into it; a key never hides a panel. |
| **Go to panel** ▸ **Switch panels** | `Ctrl+Tab` | A list of the panels, the one used last first; see [Keys](ref-tools.md#keys). |
| **Extracted ion chromatogram...** | `Ctrl+I` | Asks for m/z values and shows their chromatogram in the MS chromatogram panel. |

## Chromatogramm

| Item | What it does |
|---|---|
| **FID solvent cut** | Leaves out the FID signal before the solvent end. The field **Solvent end RT** below it sets the time in FID minutes. |
| **TIC/MS solvent cut** | The same for the MS traces, with its own time in MS minutes. |
| **Chromatogram 1** > **subtract blank** | Shows and integrates the signal of Chromatogram 1 minus the assigned blank. |
| **Chromatogram 2** > **subtract blank** | The same for Chromatogram 2. |

## Mass Spectrum

All items work on the spectrum now shown in the **Mass spectrum** panel. The same menu opens with a
right-click in the spectrum.

| Item | What it does |
|---|---|
| **Library hits** | Searches the spectrum with the default search method and lists the hits. |
| **Own library** | Searches it in the one library chosen for this. |
| **Own library selection and options** | Chooses that library; **Options...** sets algorithm, minimum score, number of hits, m/z range and threshold. |
| **Investigate** | Opens the spectrum in SpectrAtlas, if installed. |
| **NIST** | Sends the spectrum to NIST MS Search, if installed. |
| **Copy MSP** | Copies the spectrum in MSP format to the clipboard. |
| **Save MSP...** | Saves it as an `.msp` file. |
| **Register unknown** | Stores it in the unknown register. |
| **Add to library...** | Stores it in one of your libraries. |
| **Whole scan range on the m/z axis** | Shows every spectrum on the full scan range of the run, instead of fitting the axis to the ions of the spectrum. |

## Integration

| Item | Key | What it does |
|---|---|---|
| **Integrate** | `F5` | Integrates the active chromatogram again. |
| **Integrate all** | `Shift+F5` | Integrates all loaded chromatograms again. |
| **Select / zoom**, **Pan**, **Draw baseline**, **Split (drop line)**, **Delete peak**, **Add peak**, **Move start/end**, **Merge peaks**, **Tangent skim**, **Negative peak**, **Reset range** | `Z` `H` `B` `S` `D` `A` `M` `G` `K` `N` `R` | The mouse tools. See [Toolbars and tools](ref-tools.md#integration-tools). |
| **Integration method panel** | | Shows the **Integration method** panel. |
| **Solvent cut...** | | Switches the solvent cut and sets the solvent end time. |

## Identify

| Item | Key | What it does |
|---|---|---|
| **Library search...** | `Ctrl+F` | Searches all integrated peaks in your libraries. |
| **Search methods...** | | Libraries, order, algorithm and limits of a search. |
| **Libraries...** | | The list of libraries on this PC. |
| **Own library search options...** | | The options of **Own library**. |
| **Library hit list (selected peak)** | `Ctrl+E` | Searches the selected peak and shows all hits. |
| **Search selected peak in NIST** | `Ctrl+N` | Sends the selected peak's spectrum to NIST MS Search. |
| **Investigate selected peak in SpectrAtlas...** | `Ctrl+Shift+E` | Opens it in SpectrAtlas. |
| **Register selected peak as unknown...** | | Stores the selected peak in the unknown register. |
| **Unknown register...** | | Opens the register of unknowns. |
| **Edit library...** | | Adds, edits and deletes entries of your own libraries. |
| **Add current spectrum to library...** | | The same window, filled with the spectrum now shown. |
| **Retention index (alkane ladder)...** | | Sets up the retention index from an n-alkane run. |
| **Deconvolution...** | `Ctrl+K` | Splits the selected peak into its components, or lists the components of a range. |
| **Deconvolution of the whole run...** | | The same window, opened on the whole run. |

See [Identify the peaks](wf-identify.md).

## Quantify

| Item | What it does |
|---|---|
| **Role of active chromatogram** | **Sample**, **Blank**, **Blank + ISTD**, **Standard** or **Alkane ladder**. |
| **Assign blanks...** | Which blank runs belong to the active sample. |
| **Blank subtraction settings...** | How a blank is subtracted and how blank peaks are recognised. |
| **Set selected peak as ISTD** | Binds the selected peak to one of the internal standards (IS1, IS2 ...). A tick marks the standard it is bound to. |
| **Detect internal standards...** | Finds the standards by name, spectrum and retention time. |
| **Quantification panel** | Shows the **Quantification** panel. |
| **Double determination...** | Opens the **Double determination** tab. |
| **Replicate groups (N-fold)** | Opens the worksheet of the first group of three or more in **Replicates / results**. |

See [Blanks, internal standards and quantification](wf-quantify.md).

## Report

| Item | What it does |
|---|---|
| **NIAS Report...** | Writes the NIAS report (Excel and Word). |
| **NIAS Report - preview** | Shows it page by page without saving. |
| **Fingerprint Report...** / **Fingerprint Report - preview** | The Fingerprint report. |
| **Total Extraction Report...** / **Total Extraction Report - preview** | The Total Extraction report. |
| **HS-Screening Report...** / **HS-Screening Report - preview** | The HS-Screening report. |
| **Quantification Report...** / **Quantification Report - preview** | The report of the quant method (mode **Extraction (quant method)**). |
| **Template Report...** / **Template Report - preview** | The report the method's report template designs, in every quantification mode (see [Report templates](wf-report-templates.md)). |
| **Edit report template...** | Opens the window **Report template** (the same as **Method > Report template...**). |
| **Batch report of this folder...** | Reports every sample of the active chromatogram's batch folder. |
| **Keep intermediate workbook** | Also keeps the workbook with the intermediate calculations. |

See [Reports](wf-report.md).

## Automation

| Item | What it does |
|---|---|
| **Automation panel** | Shows the **Automation** panel. |
| **Report²** | Shows the **Report²** panel. |
| **New workflow** | Starts a workflow from a template: **NIAS: Excel to folder A, Word + PDF to folder B**, **NIAS: accepted reports out, reports to check to a review folder**, **One report into one folder**, **Empty chart**. |
| **Start the watcher** | Starts the background processing. |
| **Pause / resume the watcher** | Pauses or resumes it. |
| **Stop the watcher** | Ends it. |

See [Unattended processing](wf-automation.md).

## Layout

| Item | Key | What it does |
|---|---|---|
| **Chromatogram top** | | Both chromatograms wide on top, peak table and spectrum below. |
| **Table left (classic)** | | Peak table on the left, chromatograms and spectrum stacked on the right. |
| **Integration** | | The chromatograms with the integration method panel. |
| **Review** | | Chromatograms, table, spectrum and replicates; the folder tree hidden. |
| **Dual monitor - table detached** | | Chromatograms here; peak table and spectrum on the second screen. |
| **Reset layout** | | Arranges the panels again as in the ticked layout, after you moved them. |
| **Save layout...** | | Stores the present arrangement under a name. An existing name is replaced only after you confirm. |
| **Update saved layout** | | Stores the present arrangement under the ticked saved layout, without asking for a name. |
| **Saved layouts** | | Applies one of your stored arrangements. |
| **Delete saved layout** | | Removes one, after you confirm. |
| **Suggest docking position when dragging panels** | | Shows a band where a dragged panel will dock. Remembered. |
| **Lock panels** | | Fixes the panels so they cannot be moved by accident. Remembered. |
| **Light Mode**, **Dark Mode**, **Dark Mode - Neon** | | The look of the program. |
| **Next theme** | `Ctrl+Shift+D` | Steps to the next look. |

The layout you chose last, a preset or a saved one, is ticked. A panel that did not exist yet when a layout
was saved is put into its usual group of tabs, and a detached panel saved on a screen that is no longer
connected comes back onto this screen.

In every layout, Quantification, Replicates / results, Automation and Report² are tabs beside the peak table,
where they have room; Integration method, Properties and Audit trail are tabs beside Folders.

## Help

| Item | Key | What it does |
|---|---|---|
| **User manual** | `F1` | This manual. |
| **Keyboard shortcuts** | | All keys and mouse gestures on one page. |
| **About GC Workspace** | | The version of the program. |

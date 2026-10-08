# Dialogs

Every window the program opens, in the order of the menus that open them.

## Export chromatogram

**File > Export chromatogram...**, or **Export...** in a chromatogram panel.

| Control | What it does |
|---|---|
| **Chromatogram** | **Chromatogram 1**, **Chromatogram 2** or **Both, stacked**. |
| **Format** | PNG, JPEG, TIFF, BMP, SVG or PDF. |
| **Size** | Width and height in pixels, per chromatogram. |
| **Resolution** | 1× to 3×: more pixels for the same picture, for print. |
| **Title line** | Writes the sample and signal above the chromatogram. |
| **Copy to clipboard** | Puts the picture on the clipboard. |
| **Save...** | Saves it as a file. |

The preview shows what will be exported: the chromatogram as it is on screen (zoom, labels, overlay), always
on white with the light colours.

## Preferences

**Edit > Preferences...** See [Methods and projects](wf-methods.md#preferences).

## Save current settings as Method

**Method > Save current settings as Method...**: **Name** and **Comment** of the method. The window lists what
is saved and what is not (things that belong to single runs).

## Load Method

**Method > Load Method...**: the list of stored methods with a summary of the selected one. Under **Apply**,
tick the parts to take over. **Load** applies them, **Import...** reads a method file from a colleague
(`.json`), **Export...** writes one, **Delete** removes the selected method.

## Solvent cut

**Integration > Solvent cut...**: the switch **FID solvent cut** or **TIC/MS solvent cut** for the signal of the
active panel, and **Solvent end** in minutes of that detector. Earlier data are left out of plots, integration
and whole-run deconvolution.

## Automatic library search

**Identify > Library search...** (`Ctrl+F`). See [Identify the peaks](wf-identify.md#search-all-peaks).

## Library search - review

Shown after the search when **Review hits before applying** is ticked.

| Control | What it does |
|---|---|
| Left table | One row per searched peak: **Apply**, **Chromatogram**, **RT**, **Before** (the present name), **Top hit**, **Score**. Untick **Apply** to leave a peak unchanged. |
| Right table | The hit list of the selected peak: **Name**, **CAS**, **Score**, **Formula**, **Library**. |
| **Use selected hit for this peak** | Takes the marked hit instead of the first. |
| **Apply** | Writes the names to the peaks (one undo step). |

## Library search methods

**Identify > Search methods...**

| Control | What it does |
|---|---|
| Method box, **New...**, **Delete** | The stored search methods. |
| **Set as default** | Makes the method the one used when none is chosen, for example by **Library hits**. |
| **Algorithm** | **pbm** or **similarity**. |
| **Speed: Fast search** | All peaks at once; same results, faster. |
| **Library order** | **Combined** or **Sequential**. |
| **Hits per peak** | Length of the hit list. |
| **Quality limit (identified from)** | The score from which the first hit names the peak. |
| **Stop score (sequential)** | The score that ends a sequential search. |
| **m/z range**, **From the acquisition** | The ions compared. |
| **Intensity threshold** | Ions below it are ignored. |
| **Name must contain**, **Name must not contain** | Only hits whose name has, or has not, these words. |
| **Report accepted non-aromatic hydrocarbons as 'Hydrocarbon'** | Such hits are reported under the common name instead of one particular isomer. |
| **Remove duplicate compounds from the hit list** | Each compound once. |
| **Only hits with a CAS number** | Hits without CAS are left out. |
| **Libraries** | Tick the libraries to search. |
| **Search order**: **▲ Up**, **▼ Down**, **To top**, **To bottom** | The order of the libraries (it matters for **Sequential**). Dragging works too. |
| **Update the list** | Takes over libraries added or removed under **Libraries...**. New ones are switched off. |
| **Libraries...** | Opens the library list. |

How these act on a search is explained in [How the library search works](library-search.md).

## Libraries

**Identify > Libraries...**

| Control | What it does |
|---|---|
| Table | **Use**, **Name**, **Type**, **Spectra**, **Status**, **Location** of each library. |
| **Add library file...** | An `.msp` file, a Wiley / Shimadzu `.lib` file, or a file inside an Agilent `.L` or NIST folder. |
| **Add folder...** | An Agilent `.L` folder, a NIST library folder, or a folder holding several libraries. |
| **Take over from SpectrAtlas** | Adds the libraries of a SpectrAtlas installation on this PC. |
| **Remove** | Stops searching the selected libraries. The files stay. |
| **Load / check** | Reads the libraries now. The first time builds the search index. |

## Own library search - options

**Identify > Own library search options...**, or **Own library selection and options > Options...**

| Control | What it does |
|---|---|
| **Library** | The one library that **Own library** searches. |
| **Algorithm** | As in a search method. |
| **Minimum score** | Hits below it are not listed. |
| **Hits** | Length of the hit list. |
| **m/z range**, **From the spectrum** | The ions compared. |
| **Intensity threshold** | Ions below this share of the base peak are left out. |
| **Each compound only once in the hit list** | |

## Library search - hit list

**Identify > Library hit list (selected peak)** (`Ctrl+E`), **Library hits** and **Own library**.

The table lists **Name**, **CAS**, **Score**, **Fwd**, **Rev** and **Library** of every hit.
**Assign hit to peak** makes the selected hit the name of the peak.
**Add spectrum to library...** stores the measured spectrum in one of your libraries.

## Register unknown

**Identify > Register selected peak as unknown...**: shows **Sample**, **RT** and the **Significant ions**;
you can enter a **Substance name (optional)**, a **CAS (optional)**, a **Note** and a **Status**.
**Store the chromatogram around the peak** keeps a small piece of the chromatogram with the entry.

## Unknown register

**Identify > Unknown register...**

| Control | What it does |
|---|---|
| **Find** | Text (ID, name, CAS, note), a sample name, or m/z values such as `149 167 279`. |
| Percentage | For an m/z search: every ion given must reach this share of the base peak. |
| **first = base peak** | The first m/z given must be the base peak. |
| **only with spectrum** | Only entries that have a spectrum. |
| **Open folder** | Opens the register's folder. |
| ✓ column | Marks entries to export or search together. |
| **Mark all shown**, **Mark selected**, **Clear marks** | |
| **Export to MSP...**, **Copy MSP** | The marked entries (or the selected ones) as MSP, to share. |
| Lower table | Where the selected unknown was seen: **Sample**, **RT**, **Report**, **Date**, **mg/kg**. |
| **Library search**, **Own library**, **NIST search** | Search the selected unknown again. |
| **Add to library...** | Stores it in one of your libraries. |
| **Edit...**, **Delete entry** | Change name, note and status, or remove the entry. |

## Edit library

**Identify > Edit library...** and **Add current spectrum to library...**

| Control | What it does |
|---|---|
| **Library** box, **New library...** | The library to change, or a new one. |
| **New entry** tab | **Name \***, **Synonym**, **CAS**, **Formula**, **MW**, **Retention index**, **RT [min]**, **Column / method**, **Source**, **Comment**. |
| **Keep ions from ... of the base peak** | Leaves out ions below this share (noise). |
| **Take current spectrum** | Takes the spectrum now shown in the **Mass spectrum** panel. The window stays open while you select another peak. |
| **Import MSP...** | A spectrum from an `.msp` file, with its name, CAS and formula. |
| **Paste** | An MSP record or "m/z abundance" pairs from the clipboard. |
| **Type ions...** | Opens **Ions of the entry**: enter or correct the ions as lines of "m/z abundance". |
| **Add to library** | Stores the entry. |
| **New entry instead** | When editing an entry: stores it as a new one instead. |
| **Entries** tab | The entries of the library (**Name**, **CAS**, **Formula**, **MW**, **RI**) with a filter; **Edit...**, **Delete**, **Reload**. |

NIST user libraries are changed through NIST's Lib2NIST program; MSP libraries are written directly. Before
every change the library is copied to the backup folder. If NIST MS Search has the library open, close it
first.

## Retention index - alkane ladder

**Identify > Retention index (alkane ladder)...**

| Control | What it does |
|---|---|
| **Ladder chromatogram** | The run of the n-alkanes. |
| **Read n-alkanes from identifications** | Fills the table from the named peaks of that run. |
| Table, **Add row**, **Remove** | Carbon number and **RT [min]** of each alkane. |
| **Report RI column** | Adds the retention index to the reports. |
| **Replace RT by RI in the reports** | Shows the index instead of the time. |

## Deconvolution

**Identify > Deconvolution...** (`Ctrl+K`). See
[Integrate and correct peaks](wf-integrate.md#split-a-peak-that-contains-two-substances).

| Control | What it does |
|---|---|
| **Selected peak** / **Visible range** / **Whole run** | What is deconvoluted. |
| **Settings** | Shows the settings: **Presets** for **Resolution**, **Sensitivity** and **Shape**, and the advanced values. See [Settings and defaults](ref-settings.md#deconvolution). |
| **Save as default** | Uses these settings also for the spectrum mode **Deconvoluted component** and the library search. |
| **Deconvolute** | Finds the components again with the present settings. |
| Plot | The peak with the fitted curve of each component and the cut points. |
| Component table | One row per component: **#**, **RT MS**, **Model m/z** (its most characteristic ion), **Purity**, **S/N**, **Ions**, **Share %** (the part of the peak area the split gives it), **Area**, **Class hint**. Tick the ones to use. |
| **Show components outside the peak** | Also lists neighbours. |
| **Spectrum** / **Interpretation** | The selected component. |
| **Library hits...** | Searches the selected component. |
| **Use for peak spectrum** | The selected peak uses this component's spectrum for search and register. |
| **Add as peaks** | Integrates the ticked components as new peaks. |
| **Split peak** | Replaces the peak by one peak per ticked component. |

## Blanks

**Quantify > Assign blanks...**: a table of the loaded runs with the columns **Blank** and **Blank + ISTD**.
Tick the runs that are the blanks of this sample. The suggestion comes from the injection order. With several
blanks of one kind, the larger matching area is subtracted.

## Blank subtraction

**Quantify > Blank subtraction settings...** See [Settings and defaults](ref-settings.md#blank-subtraction).
**Subtract** applies the settings and subtracts now; it is available when **Automatic blank subtraction** is
off.

## Detect internal standards

**Quantify > Detect internal standards...**, or **Detect ISTDs...** in the **Quantification** panel.

| Control | What it does |
|---|---|
| **Chromatograms** | **Active chromatogram** or **All loaded samples**. |
| List | For each standard the peak found, its retention time against the target, the evidence and the confidence. Tick the ones to bind. |
| **Run shift** | The common shift of the run that was found. |
| **Also move the target RTs to where the standards were found** | Updates the standards table. |
| **Bind checked** | Binds the ticked standards (one undo step). |

## Migration conditions

**Migration conditions...** in the **Quantification** panel: analyst, simulant (choose one or type another),
temperature, duration, cell area, coverage factor, volume and surface-to-volume ratio. The line below shows
how cell and coverage will be named in the report.

## Double determination - settings

**Settings…** in the **Double determination** tab. See
[How the double determination works](double-determination.md#settings).

## Determinations of a group

**Members...** (right-click a group in the list **Determinations** of **Replicates / results**): tick the determinations of the sample and drag them into order.
The first is determination A (1), the second B (2), and so on.

## Report preview

**Report > ... - preview**: the Word report page by page. **Zoom** sets the size, **Save report...** saves the
files, **Open in Word** opens the document. Warnings of the report are listed at the top.

## Choose columns

**Columns** in the peak table: **Available columns** on the left, **Shown columns (table order)** on the right.
**Show →** and **← Hide** move a column, **↑ Up**, **↓ Down**, **⤒ First**, **⤓ Last** order the shown ones,
**Reset to defaults** restores the standard set. Double-click and drag and drop work too.

## Report template

**Method > Report template...** or **Report > Edit report template...**: designs the Template Report (see
[Report templates](wf-report-templates.md)).

- Top: the saved templates (**Template**), **New from** (**NIAS layout**, **HS-Screening layout**,
  **Quantification layout**, **Empty template**, **The method's template**), **Save**, **Save as...**,
  **Delete**; the chips **changed** and **In method**.
- **Columns**: **Available** (filter, grouped, grey = not in this quantification), **Add →**; **Report columns
  (report order)** with **Header**, **Decimals** and **Double determination** (Mean, Each determination,
  Each + Mean, Merged (A / B)); **▲ Up**, **▼ Down**, **Remove**.
- **Header**: **Title (red band)**, **Subtitle**, the fields (**Line**, **Label**, **Value**), **Add field**,
  **Add line**, **Remove field**, **Insert placeholder**.
- **Rows**: **Only rows ticked Report in the double determination**, **Leave out the internal standards**,
  **Leave out rows without name and CAS**, **Leave out library sum rows ('Sum of ...')**, **Unidentified
  substances**, **Minimum score**, **Reporting limit** (**Off**, **The method's reporting limit**, **This
  value**, on a concentration), **Sort by**, **NIAS category sums (styrene oligomers, hydrocarbons, siloxanes,
  cyclic polyester oligomers)**, **Sums of substances found more than once**, **Text when nothing is
  reported**.
- **Extras**: **Page**, **Word document (.docx) next to the workbook**, **Bold above the SML**, **Footnote
  markers from CASINFO.xlsx**, **Notes**, **Further sheets of the workbook** (**Determinations**,
  **Calculation**, **Audit**), **Audit Trail page in the Word document**, **Hide columns without any value**,
  **Count the reported substances in the unknown register**, **Create the report when the method runs (Run
  Method, automation)**, **Report buttons preview this report**, **File name ending**.
- **Preview**: **Active run** or **Active replicate group**, the warnings, the report as it will look;
  **Word preview...** shows the Word pages.
- **Use in method** (one undo step), **Save to method** (also writes the processing method file), **Close**.

## Workflow chart

**Edit chart...** in the **Automation** panel, or **Automation > New workflow**. See
[Unattended processing](wf-automation.md#set-up-a-workflow).

| Control | What it does |
|---|---|
| **Steps** | The step types. Drag one onto the chart, or double-click it. |
| **Checks** | What is still missing or wrong. |
| **Name** | The name of the workflow. |
| **Active (the watcher runs it)** | Switches the workflow on. |
| **Save**, **Delete**, **Fit** | Save the workflow, delete the selected step or arrow, show the whole chart. |

Double-click a step for its settings, an arrow for its filter.

## Report² - rules

**Rules...** in the **Report²** panel or in a Report² step. Each rule is a box that can be ticked. **Finding
means** chooses **Control needed** or **Note only**. Some rules have values of their own (a limit, a name
pattern). See [Unattended processing](wf-automation.md#the-rules).

## Accept or reject a report

**Accept with comment...** (the arrow beside **Accept**) and **Reject > Other...** in the **Report²** panel. The
**Comment** is optional; **Accept** and the reject reasons need no dialog at all. Your name and the time are
recorded with it.

## Report² - reject reasons

**View > Reject reasons...** in the **Report²** panel: the reasons offered under **Reject**, one per line.
**Defaults** puts back the reasons GC Workspace comes with.

## Report² - history

**Show history...** (right-click a report, or `H`): what happened to the report, by whom and when, and its files
with where they were delivered (double-click a file to open it). The window does not block the panel.

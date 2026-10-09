# Panels

Every control of every panel. The buttons in a panel's title (maximize, detach, close) are the same everywhere
and are described in [Toolbars, tools, keys and mouse](ref-tools.md#panel-title-buttons).

## Folders

The upper part is the folder tree, the lower part the **Loaded samples** list.

| Control | What it does |
|---|---|
| Folder box | The root folder of the tree; the list remembers the folders used before. |
| **↑** | Goes to the parent folder. |
| **Choose the root folder...** | Picks the folder where the tree starts. |
| **Load** | Loads the runs marked in the tree. A double-click on a run does the same. |
| **◀** | Collapses the panel to a narrow strip; **Folders ›** on the strip opens it again. |

The collapsed strip still shows the loaded samples, one square each, in the order of the list. The square
carries the injection number the data file starts with (`07_..._A.D` shows **07**); a run without one shows two
letters of its name. The colour is the run's colour. A filled square is a sample. An outlined square is a
blank, with a dot for a blank with ISTD. A mark in the top right corner means a standard, and a bar along the
bottom an alkane ladder. A faded square is not shown in the overlay, and a ring marks the active
chromatogram. Click a square to make it the active chromatogram; right-click it for the same menu as in
**Loaded samples**. Pointing at a square shows the full name and the role.

Right-click in the tree: **Load as** (with a role), **Load all runs in folder**, **Set as tree root**,
**Send to Automation...** (the selected samples into the automation queue; you choose the workflow), **Open in Explorer**.

**Loaded samples**: click a run to make it the active chromatogram; drag to reorder. The right-click menu is
described in [Load and view data](wf-load.md#the-loaded-samples-list).

## Chromatogram 1 and Chromatogram 2

Two chromatogram panels with the same controls. They share one time axis.

| Control | What it does |
|---|---|
| Signal box | The signal shown: FID, TIC, BPC or an extracted ion. |
| **subtract blank** | Shows and integrates this signal minus the assigned blank. |
| **Solvent cut** | Leaves out everything before the solvent end time. |
| **Lock view** | A peak picked in the substance or replicate list is selected without zooming to it. Applies to both panels. |
| **Overlay** | Shows the other loaded chromatograms behind the active one. |
| **Normalize** | Scales every trace to its own maximum. |
| **Stack** | Offsets the traces vertically. |
| Labels box | **Labels: RT**, **Labels: #**, **Labels: name** or **Labels: off**. |
| **Export...** | Saves this chromatogram as a picture. |
| **More** | More plot controls. |

In the plot: the baseline and the start and end marks of each integrated peak, drop lines where peaks were
split, peak labels, a cursor with the time and intensity under the mouse, and the regions used for the
spectrum of the selected peak. A peak split by deconvolution is drawn as its fitted curve.
With no chromatogram loaded, the plot says what to do: double-click a run in Folders.

## Peaks / substances

The peak table of one chromatogram.

| Control | What it does |
|---|---|
| **Chromatogram 1 · ...** / **Chromatogram 2 · ...** | Which chromatogram's peaks the table lists. The button names the signal. |
| **Filter...** | Shows only rows that contain the typed text. |
| **Delete peak(s)** | Deletes the marked peaks (one undo step). `Delete` does the same. |
| **Integrate** | Integrates the active chromatogram again. |
| **Integrate all** | Integrates all loaded chromatograms again. |
| **Library search** | Opens the automatic library search. |
| **Export** | Writes the table as shown to an Excel or CSV file. |
| **Columns** | Chooses and orders the columns. |
| **Hide blank peaks** | Hides peaks that are at blank level. |
| **Show only peaks with** | A value filter: choose the column, a comparison (<, ≤, =, ≥, >, **between**, **outside**) and one or two values. **Clear** shows all peaks again. |

The line below the table says how many peaks are shown and how many are identified.
An empty table says why: no chromatogram loaded, the run is not integrated yet (Method ▸ Run Method or
Integrate), or no peak matches the filter.

Click a row to select the peak; both chromatograms zoom to it. **Name** and **CAS** can be typed into
directly. Click a column header to sort; right-click it for **Choose columns...**.

Right-click a row:

| Item | What it does |
|---|---|
| **Copy** | Copies the marked rows. |
| **Clear identification** | Removes the name of the peak. |
| **Use library hit** | Chooses another hit of the peak's stored hit list. |
| **Search selected peak in NIST** | Sends the spectrum to NIST MS Search. |
| **Library hit list (selected peak)** | Searches this peak and shows all hits. |
| **Register selected peak as unknown...** | Stores it in the unknown register. |
| **Set selected peak as ISTD** | Binds it to an internal standard. |
| **Split by deconvolution...** | Splits the peak into its components. |
| **Keep unsplit (no automatic deconvolution split)** | The automatic deconvolution split leaves this peak as one peak. |
| **Allow automatic deconvolution split** | Removes that mark. |
| **Merge deconvoluted peaks** | Undoes the deconvolution split of the selected peak: its fragments are one peak again. |
| **Delete peak(s)** | Deletes the marked peaks. |

### Columns

Columns marked * are shown unless you change it.

| Column | Meaning |
|---|---|
| **#** * | Peak number. |
| **RT [min]** * | Retention time of the apex. |
| **RT MS [min]** | The time in the MS for an FID peak (delay-corrected), or of its assigned component. |
| **Type** * | How the peak starts and ends: B baseline, V valley, P penetration, H hold. Added letters: S solvent, T tangent skim, X exponential skim, F / R front or rear shoulder, N negative, M manual, + area sum. |
| **Start**, **End** | The integration boundaries. |
| **Area** *, **Area %** *, **Height** * | The integration result. |
| **W½ [s]** | Width at half height. |
| **Symmetry** | USP tailing factor. |
| **S/N** | Signal-to-noise ratio. |
| **Name** *, **CAS** * | The identification; editable. |
| **Score** * | Library score of the chosen hit. |
| **ID status** * | Accepted, Manual review, Unknown ... |
| **Library** | The library the hit is from. |
| **RI** | Retention index, if an alkane ladder is set up. |
| **RRT** | Retention time relative to the chosen reference ISTD. |
| **ISTD** * | Which internal standard the peak is. |
| **Blank area** | NIAS quantification: the blank area subtracted. |
| **Corr. area** * | NIAS quantification: the area after its blank correction. |
| **In blank** * | Whether the peak is also in the assigned blank. |
| **Blank ratio** | Sample area ÷ blank area of the matching blank peak. |
| **Area − blank** | Area minus the matching blank peak's area. |
| **mg/dm²** | |
| **µg/HS**, **µg/dm²**, **µg/g**, **mg/m²** | The HS-Screening quantities. |
| **mg/g**, **mg/kg**, **µg/kg** | Extraction (quant method) of a solid: the substance per sample mass. |
| **Conc.** * | Concentration in the unit of the quantification mode. |
| **SML** | The specific migration limit of the substance. |
| **Status** * | Status of the quantification. |
| **Integration** | How the peak came about: automatic, manual, gap fill ... |
| **Class hint** | The substance class suggested by the interpretation of the spectrum. |

## Mass spectrum

| Control | What it does |
|---|---|
| Mode box | How the spectrum of a selected peak is formed. See [Identify the peaks](wf-identify.md#how-the-spectrum-of-a-peak-is-formed). |
| **subtract blank** | Subtracts the blank's spectrum at the same time. |
| **More** | More plot controls. |

The plot shows the spectrum as sticks with m/z labels on the strongest ions. The caption names the source:
peak, scan or range, and what was subtracted. When a library hit is chosen, its reference spectrum is drawn
downwards below the measured one.
Without a spectrum, the plot says how to get one: click a peak, or right-click a chromatogram.

The tabs below the spectrum:

| Tab | What it shows |
|---|---|
| **Interpretation** | Clues about an unknown: molecular ion candidate, isotope patterns, ion series, neutral losses, substance class, similar NIAS substances, formula suggestions. |
| **Library hits** | The hit list of the last search of this spectrum: Name, CAS, Score, Fwd, Rev, Library. Click a hit to compare it. |
| **m/z table** | The spectrum as numbers: m/z, Abundance, Rel. %. |
| **Scans** | The scans around the peak with two regions to drag: blue = averaged, red = subtracted as background. **Use these scans** fixes them for the selected peak; **Automatic** returns to the automatic choice. |

The menu of the panel is the **Mass Spectrum** menu, see [Menus](ref-menus.md#mass-spectrum).

## Integration method

| Control | What it does |
|---|---|
| **Method:** box | The stored integration methods for the signal of the table's chromatogram. |
| **Save as...** | Stores the present parameters and timed events under a name. |
| **Apply to active** | Integrates the active chromatogram with these settings. |
| **Apply to all** | Uses these settings for all loaded chromatograms. |
| **Auto parameters** | Sets peak width, slope sensitivity, threshold and smoothing back to automatic. |

**Parameters** tab: see [Settings and defaults](ref-settings.md#integration-method). A value with **Auto**
ticked is chosen by the program from the signal. The group **Automatic deconvolution split** below the
parameters splits co-eluted peaks by their MS components and shows how many were split
(see [Settings and defaults](ref-settings.md#automatic-deconvolution-split)).

**Timed events** tab: a table of **Time [min]**, **Event** and **Value**. **Add event** adds a row,
**Remove** deletes the selected one. See [Settings and defaults](ref-settings.md#timed-events).

**Manual events** tab: your manual integration changes and the gap fills of the double determination, with
**On**, **Event**, **User**, **When** and **Comment**. Untick **On** to switch one off.
**Delete selected** removes the marked events, **Remove all manual changes** all of them.

## Properties

Facts about the active chromatogram.

| Group | Contents |
|---|---|
| **Chromatogram** | **Sample**, **Folder**, **Acquired**, **Acq. method**, **Raw data** (which files were read) and **Notes** from loading. |
| **Role and blanks** | **Role** box, the assigned **Blanks**, and **Assign blanks...**. |
| **FID → MS retention time offset** | **Estimated**: the delay the program found and how reliable it is. Tick **Manual value**, enter a number and press **Apply** to set it yourself. |
| **Integration** | **Noise** of the signal, the **Parameters used** by the integrator, and the **Result digest**, a checksum of the integration result. |

The digest changes whenever the peaks change. A project stores it, so a result that differs after an update
of the program is noticed when the project is opened.

## Audit trail

A list of every change in this project: time, user, action, run, details, the value before and after, and
the reason if one was given. It is saved with the project and cannot be edited.

## Quantification

Described task by task in [Blanks, internal standards and quantification](wf-quantify.md).

| Control | What it does |
|---|---|
| **Mode** | The quantification mode. |
| **Detector** | **FID** or **TIC (MS)**: the peaks the quantities are computed from. |
| **ISTD concentration**, **Unit** | For the mode **Internal standard concentration** only. |
| **Result unit** | For **HS-Screening** only. |
| **NIAS parameters** | A table of **Parameter**, **Value** and **Unit**: the numbers of the NIAS calculation and of the reports (solvent end, reporting limit, quality limit, duplicate difference limit ...). Edit a value in place. |
| **Migration conditions...** | Opens the migration conditions. |
| **Internal standards** | The table of standards: **Code**, **Name**, **Conc.**, **Target RT**, **Quantify**. |
| **Factor from the mean of the ISTD areas** | Uses the mean of the quantifying standards. |
| **RRT reference ISTD** | The standard for the relative retention time. |
| **Add**, **Remove** | Add or remove a standard. |
| **ISTDs in the active chromatogram** | For each standard: **Code**, **Name**, **RT**, **Area**, **Deviation %**, **Status**. |
| **Bind selected peak** | Binds the peak selected in the peak table to the selected standard. |
| **Unbind** | Releases it. |
| **Automatic** | Finds the standards by name and target RT again. |
| **Detect ISTDs...** | Finds them by name, spectrum and retention time and shows the evidence. |
| **Learn spectrum** | Keeps the bound peak's spectrum as the standard's reference. |

In the mode **HS-Screening** the panel shows instead: **Calibration** (**Internal standards in each sample**,
**External: calibration runs** or **External: entered areas**), with external calibration runs the list
**Calibration runs** (tick the calibration vials; they get the role Standard) and **Go to run**, **Result unit**,
**Use mean of activated ISTD areas**, **Subtract matching Blank / Blank+ISTD (larger area)**,
**Sample area (dm²)**, **Sample mass (g)**, the table of HS standards (with entered areas also **TIC area**), the
standards of the active run (with calibration runs, on a sample: the area in each calibration run and the
**Mean area**), and **Bind selected TIC peak**, **Unbind**, **Automatic**, **Detect...**, **Learn spectrum**.
See [External calibration](wf-quantify.md#external-calibration). **Report Conc. 1** and **Report Conc. 2** choose
the units of the HS report (by default µg/dm² and mg/m²).

In the mode **Extraction (quant method)** the group **Quant method** replaces the NIAS parameters: the saved
methods with **Save**, **Save as...** and **Delete**, **Sample**, **Sample amount**, **Extract volume**,
**Spiked standard**, **Conc. 1**, **Conc. 2**, **Reporting limit**, **Active run amount** and **NIAS standards**.
See [Quant method](wf-quantify.md#quant-method).

## Replicates / results

On the left the list **Determinations**: every replicate group of the project with **N** (its number of
determinations) and **Open** (red rows still open at its last comparison: a number, ✔ when nothing is left,
– when not compared yet). Pairs the run names suggest but that are no group yet are shown in italics;
selecting one compares it and makes it a group. **+** offers **Suggest** and **New...**; a right-click on a
group offers **Suggest**, **New...**, **Rename...**, **Members...**, **Validity rule** and **Delete**.
`Ctrl+F3` goes to the next group that is not compared yet or still has open red rows. Selecting a pair or a
single determination opens the double-determination page, a group of three or more its worksheet.
Described in [Double determination](wf-double.md).

The double-determination page:

| Control | What it does |
|---|---|
| **A**, **B** boxes | The two determinations. |
| **⇄** | Swaps A and B. |
| **Compare** | Pairs the two determinations and fills the list. |
| **More** | **Settings…** (pairing, gap filling, consensus name), **3+ determinations…** (the worksheet of the first group of three or more) and **Reset all** (undoes every change made in this double determination). |
| **Load from Report²…** | Switches to another processed A/B sample visible in Report², grouped by batch. Changes to the current pair are saved first. The empty menu says **No processed A/B pairs under the current Report² filters**. |
| **All**, **To check**, **Red**, **Yellow**, **Green**, **Grey** | Chips that count the substances of each colour. A click shows only those rows, a second click all rows again. **To check** is red and yellow plus the rows you changed. |
| **Deleted** | Shown once you deleted rows: a click shows them, to restore them (right-click **Restore row**). Deleted rows are not counted and not reported. |
| **Difference limit** | The largest difference between A and B in percent. It is the report parameter *Duplicate difference limit*: the chip **report parameter** beside it says so, and shows the default when the value differs from it. Changing it changes every report of the project (the status bar says so). |
| **?** | The keys of the list (Enter, Backspace, Space on the **Report** box, Delete, F2, Ctrl+C / Ctrl+V, Ctrl+D). |
| **Report preview** | The report of this pair: the report of the quantification mode, or the Template Report when the method's template says so (**Report buttons preview this report**). Its arrow offers **NIAS report...**, **Fingerprint report...**, **Total extraction report...**, **HS-Screening report...**, **Quantification report...**, **Template report...** and **Export worksheet...**. |
| **Harmonise boundaries** | Takes over every proposed integration boundary. Shown when there are proposals. |
| **Accept double determination** | Possible once A and B are compared; red rows still open keep their default (the tooltip counts them). Saves who accepted it and when with the project, writes it to the audit trail and marks the pair ✔ in the list; when the pair is a Report² report (a workflow processed it) it is accepted there too, however the project was opened; otherwise it is listed in Report² with its report (**Rename…** / **Overwrite** when the name is taken). The status bar says which. A later change reopens it (**Accept again**). |

A right-click on a row offers **Delete row**, **Reset row** and the other row actions (see [Double determination](wf-double.md#decide)).

The line under the chips sums up the comparison in one line (the whole text is its tooltip); beside it,
how many substances go into the report and how many you changed. **Columns...** beside it (or **Choose
columns...** on a right-click on a column header) chooses and orders the columns - also A, B and the mean in
mg/dm², µg/dm², µg/L, mg/L and mg/mL (with a quant method also mg/g, mg/kg, µg/g and µg/kg, each determination
converted with its own sample amount); a right-click on a column header shows or hides one column. **Diff. %**
is a bar against the limit.

Below the list: the two chromatograms of the selected substance, A upwards and B downwards, with its
integrated peak shaded in each, and the two spectra, mirrored the same way (feature pairing only). Clicking a
dot selects its substance. **Plots** (beside the summary line) hides or shows them.

The worksheet of a group of three or more: the averaged results (the line above it names the determinations
and the validity rule), the chips **All**, **Red**, **Yellow**, **Green** and **Grey** that count and filter
the rows, an icon column in the same order as on the double-determination page (red first; a click on a
column header sorts by it), and below it the chromatograms of all determinations on top of each other in
their colours. Clicking a row zooms there and opens the peak. A right-click on the header shows or hides
columns. **Report preview** has the other reports and **Export worksheet...** under its arrow. The
worksheet is for reading: names and values are changed in the determinations themselves. **← Double determination** above it
goes back to the pair shown last.

## Automation

Described in [Unattended processing](wf-automation.md).

The watcher box is on top; the rest is in four tabs.

| Group | Controls |
|---|---|
| **Watcher (background processing)** | The state of the watcher; **Start**, **Pause**, **Check now**, **Stop**; **Start with Windows**, **Start with GC Workspace**. |
| **Workflows** tab | The table of workflows (**Active**, **Name**, **Watched folder**, **Every**, **Waiting**, **To check**); **New**, **Edit chart...**, **Duplicate**, **Delete**, **Import...**, **Export...**, **Report²**. |
| **Folders** tab | Every watched folder with its batch folders and runs (**Name**, **Watched folder**, **Local copy**, **Sample**, **State**, **Why**); **Add to queue**, **Open folder**, **Open local copy**, **Show in Report²**. See [What is in the watched folders](wf-automation.md#what-is-in-the-watched-folders). |
| **Queue** tab | The samples waiting, in work, failed or not processed (**Sample**, **Batch**, **Workflow**, **State**, **Why**), with their number on the tab, and the samples you added that the watcher has not taken up yet (*Requested*); **Add samples...**, **Remove from queue**, **Process again** (removed samples and deleted batch folders are not listed). |
| **Activity** tab | What the watcher did, with the time. |

## Report²

Described in [Unattended processing](wf-automation.md#report²-what-needs-your-control).

| Control | What it does |
|---|---|
| **To do**, **Archive**, **Register** | Open batches, or those whose reports are all accepted and delivered (with the number of batches); **Register**: every sample in one sortable list, done or not done (with the number of samples). |
| Done box, **Export register...** | Register only: **Done and not done**, **Done**, **Not done**; the register as an Excel workbook. |
| Workflow box | **All workflows** or one of them; **Accepted by hand** shows the double determinations accepted in Replicates that no workflow processed. |
| Period box | **All**, **Today**, **7 days**, **30 days**, **12 months**. |
| **Search sample or batch** | Filters the list. |
| Chips | **All**, **Control needed**, **Accepted**, **Waiting**, **Not processed**, **Failed**, **Rejected**, **Removed**: counts; a click filters the list. |
| Watcher chip, **Start watcher** | Whether the watcher runs; the button is shown when it does not. |
| **Rules...** | The rules that decide *control needed*. |
| **View** | **Show deleted reports**, **Reject reasons...**. |
| The list | Batches with their samples (**Sample**, **Status**, **Findings**, **Delivered**, **Processed**); a sample expands to its number of red and yellow substances. Right-click a sample or a batch for more. |
| **Accept** | Your decision, one click; the arrow: **Accept with comment...**. |
| **Reject** | With a reason, or **Other...** for a comment. |
| **Open report** | Opens one of its files. |
| **Save as** | **Save as Word...**, **Save as Excel...**: a copy of the report (a batch: the batch report) where you choose. |
| **Edit in GC Workspace** | Opens the sample as it was processed, to change it - also an accepted report; **Update report** in the status bar makes it again. |
| **Preview** | Shows the selected report beside the list (its PDF, or its Word report converted once by Microsoft Word). |
| **Undo** | Takes back the last accept, reject or delete for a few seconds. |

# GC Workspace

A standalone application for GC-FID / GC-MS data: raw data browsing, its own integrator,
automatic library search in the analyst's own libraries with NIST handoff, NIAS quantification (mg/kg) and the
NIAS / Fingerprint / Total extraction reports. It needs no ChemStation or MassHunter software and no
`RESULTS.CSV`: everything is computed from the raw files.

## Install and start

1. Run **Setup GC Workspace.cmd** once. It creates `.venv` and installs `requirements.txt`, using `wheelhouse/`
   for an offline install if that folder exists.
2. Start with **Start GC Workspace.cmd**. You can also pass a `.D` folder or a `.gcws` project on the command line.

The setup ends with a list of optional features. The report preview needs Microsoft Word, pywin32, pypdfium2 and
Pillow; the last three come with `requirements.txt`.

**Libraries**: GC Workspace searches its own list of libraries; add the ones on your PC in *Identify >
Libraries...* (see below). EI Atlas (`..\UnknownEvaluation`) is not needed for searching; it is only used by
*Investigate*, and *Libraries... > Take over from EI Atlas* adds its libraries once. Search methods and
libraries are no longer copied from `..\NIAS Working`. The unknown register is kept separate unless you choose
*Share the NIAS register* in the preferences.

## Raw data

| Detector | Files read |
|---|---|
| FID | `*.ch` (ChemStation v179), otherwise `AcqData/FID*.cg` + `.cd` (MassHunter) |
| MS | `data.ms`, otherwise `AcqData/MSScan.bin` + `MSPeak.bin` (MassHunter centroid) |
| Metadata | `AcqData/sample_info.xml` (sample name = tab name), `Contents.xml`, `Sequence Log .TSV` (injection order) |

Both formats are verified to be bit-identical on the reference batch (`tests/test_io.py`).

## Workflow

1. **Folders** panel: browse to the analysis folder. Double-click a `.D` run, or right-click it and choose
   *Load*, *Load as Blank...*, or *Load all runs in folder*. Every run gets a coloured tab; the active tab is the
   one you work on, and the other runs are overlaid thin.
2. **Chromatogram 1 and 2**: two chromatogram panels, one above the other.
   - Each panel has its own signal (FID, TIC, BPC or an EIC via *EIC ...*), its own **− Blank** switch, and
     Overlay, Normalize, Stack, labels and *Export...*. The default is FID in Chromatogram 1 (quantification)
     and TIC in Chromatogram 2 (identification).
   - Both panels share one time axis, set by the detector of Chromatogram 1. The other detector is shifted by
     each run's FID–MS delay, so a compound sits at the same x in both. The cursor readout shows the panel's own
     time.
   - Zoom and pan are synced (as in Agilent Enhanced Data Analysis):
     - left-drag draws a box and zooms into it (time and intensity);
     - **left-drag on the RT axis or the intensity axis moves the chromatogram left / right**, whatever tool is
       selected;
     - double-click shows the whole run in both panels;
     - the wheel zooms the time around the cursor, and on an axis it scales that axis;
     - picking a peak in the peak table zooms both panels to it.
   - Integration runs automatically on load, with the method of each signal. The *Integration method* panel
     holds the parameters (auto or fixed), the timed events and the list of manual events. F5 re-integrates the
     signals of both panels.
3. **Peaks / substances** table:
   - The two buttons at the top choose which chromatogram the table lists (e.g. *Chromatogram 1 · FID − Blank*).
   - *Show only peaks with* filters by concentration, corrected area, area, area %, height, mg/dm², score or RT.
     The operators are <, ≤, =, ≥, >, *between* and *outside*. Copy and export follow the filter.
   - If a run cannot be blank-subtracted (the blank itself, or no Blank assigned), the table shows its plain
     trace, and a note says why.
4. **Manual tools** in the toolbar work in both chromatograms, each on the signal of the panel you use them in:
   - select/zoom (Z), pan (H)
   - draw baseline (B), split with a drop line (S), delete (D), add peak (A)
   - move start/end (M), merge (G), tangent/exponential skim (K), negative peak (N), reset range (R)

   Manual changes are stored as events and re-applied after every re-integration. They can be undone
   (Ctrl+Z / Ctrl+Y) and are written to the audit trail. A click in the other panel selects the table's peak
   at that time.
   - **Spectra by time**: right-click any chromatogram for the mass spectrum at that time, right-drag for the
     mean spectrum over a range, Shift+right-drag to set a background range that is subtracted. In the
     spectrum panel ← / → step one scan and Esc returns to the selected peak. Clicking an ion in a spectrum
     shows its EIC in the MS chromatogram panel.
   - **Export chromatogram** (*File* menu or the panel's *Export...*):
     - one chromatogram or both stacked, as PNG, JPEG, TIFF, BMP, SVG or PDF;
     - size per chromatogram, resolution 1×–3×, an optional title line, a preview, and copy to the clipboard.
5. **Library search** (Ctrl+F) searches the integrated peaks in your libraries and fills Name, CAS and Score.
   You can review the hits first.
   - **Libraries** (*Identify > Libraries...*): *Add library file...* (an `.msp` or Wiley/Shimadzu `.lib`) or
     *Add folder...* (an Agilent `.L` folder, a NIST library folder such as mainlib, replib or a user library, or
     a folder holding several libraries: all of them are added). Libraries can be switched off or removed; the
     files are never changed. *Load / check* reads them now: the first time builds a search index in
     `data/libcache` (NIST17.L: about 35 s), later starts read it in a fraction of a second. The search
     (PBM or NIST-style similarity) is EI Atlas's own engine, built into GC Workspace; about 0.6 s per peak
     against NIST17.
   - It searches the **TIC peaks** by default. Every name found is also given to the FID peak at the same
     delay-corrected time (within 0.03 min), so the quantification and the NIAS report get it. FID names set by
     hand or bound as ISTD are kept. This works for the active chromatogram and for *All loaded chromatograms*.
   - **Only peaks shown in the peak table**: with a value filter, text filter or *Hide blank peaks* set, only
     the peaks that pass it are searched, in every searched chromatogram. The search time grows with the number
     of peaks, so filtering (e.g. Conc. > 0.01) shortens it accordingly. A filter on quantities (FID) selects
     the TIC peaks at the same time for a TIC search.
   - You can also search the FID peaks directly; each one's spectrum then comes from the MS at the FID peak's time.
   - For a single peak the spectrum panel has *Library hits* (default search method), **Own library**
     (the button searches one chosen library; its arrow picks the library and opens the options: algorithm,
     minimum score, hits, m/z range, threshold), *Investigate* (EI Atlas window, optional), *NIST*, MSP
     copy/save and *Register unknown*. The own-library options are also in *Identify > Own library search
     options...*.
   - **Interpretation** tab of the spectrum panel: clues about the unknown from interpretation rules, not an
     identification. It shows:
     - the molecular-ion candidate (isotope peaks, illogical losses and the nitrogen rule are checked);
     - Cl / Br / S / Si from the isotope pattern and the carbon number from M+1;
     - ion series and neutral losses;
     - the most likely substance class with its evidence (plasticizers, antioxidants and their degradation
       products, photoinitiators, slip agents, siloxanes, oligomers, ...);
     - NIAS substances with a matching ion pattern;
     - formula suggestions;
     - checks against the retention index and the library hit.

     Laboratory rules and substances can be added in `data/interpret_rules.json` (same format as
     `gcws/ms/knowledge.py`). The optional peak-table column *Class hint* shows the class for every peak.
   - **Deconvolution** (Identify menu): around the selected peak, over the visible range, or for the whole run.
     - Noise model, scan-skew correction, background fit and residual search are built in; each component
       gets a quality score.
     - Components can split a peak, be added as peaks, or have their spectrum pinned to the peak.
     - *Save as default* makes the settings apply to the "deconvoluted" spectrum mode and the library search.
     - After a whole-run deconvolution, components without an integrated peak are marked in the chromatogram.
6. **Edit library** (*Identify > Edit library...*, or *Add to library...* in the spectrum panel, the hit list
   and the unknown register) stores a spectrum in one of your libraries, as in ChemStation. The window stays
   open while you work: a new entry gets its spectrum with **Take current spectrum** (select a peak or
   right-click a chromatogram first), **Import MSP...** (pick a record from the file), **Paste** (an MSP record
   or m/z–abundance pairs) or **Type ions...**.
   - The entry is filled in from what is known: the identification's name, CAS and formula, the MW from the
     formula, RT (MS and FID), RI from the alkane ladder, GC method, source run and spectrum mode.
   - Ions below a chosen ‰ of the base peak are left out.
   - Target libraries:
     - **NIST MS Search user libraries** (default: **CCAlu_GCMS**) are changed through NIST's Lib2NIST. The
       library is exported, changed, rebuilt in a temporary folder and checked by exporting it again. Only then
       is the original copied to `data/library_backups` and replaced. If a step fails or the library is in use
       (close NIST MS Search), nothing is changed.
     - MSP libraries are written directly.
     - Agilent `.L`, NIST main/replicate and Wiley libraries are read-only.
     - *New library...* creates an own NIST user library, or an MSP library when Lib2NIST is missing, in
       `data/libraries`. It is added to *Libraries...* and switched on in every search method.
   - The *Entries* tab lists a library's entries; they can be edited and deleted.
   - The next search reads the changed library.
   - Lib2NIST is found automatically in a NIST MS Search installation; otherwise set its path in
     *Edit > Preferences*.
7. **Roles and blanks**: right-click a tab and set its role (Sample / Blank / Blank + ISTD / Standard /
   Alkane ladder). Blanks are suggested from the injection order; blanks you assign yourself are never
   re-suggested.
   - **Blank subtraction**: the **− Blank** switch of a chromatogram panel shows "FID − Blank" / "TIC − Blank"
     there. You decide per panel which trace is blank-subtracted.
     - This is the sample minus its aligned blank, integrated like any trace.
     - FID default: only the blank's peaks, so the baseline is kept. MS default: the whole trace, including
       the bleed.
     - Settings are in *Quantify > Blank subtraction settings*. By default only a Blank is subtracted; a
       Blank + ISTD (or both) can be chosen.
     - **Internal standards are never subtracted**: the peaks of the ISTD table (at the bound RT, or the
       largest peak within 0.1 min of the target RT; on MS traces at the FID time minus the delay) keep their
       full signal, and the peak-level blank check never marks them as blank.
     - On a blank-subtracted FID the quantification columns come from the FID peak with the nearest apex.
       Re-integrating the FID or the blank rebuilds the subtracted trace.
     - Spectra can subtract the blank spectrum at the same time.
     - The peak table column *In blank* marks peaks found in the blank (blank level / partly / also in
       blank); *Hide blank peaks* hides the blank-level ones.
     - NIAS mg/kg keeps its own blank correction.
8. **Quantification** panel: choose the mode. Each mode explains its formula in the panel.
   - **NIAS screening (mg/kg)**: the ISTD factor chain and the migration conditions, computed with the unchanged
     NIAS/AutoLib arithmetic. The unit is fixed (mg/kg).
   - **Internal standard concentration**: c(substance) = corrected area ÷ ISTD area × c(ISTD).
     - Enter the ISTD concentration in the analysed solution and its unit; the results come out in that unit.
     - The ISTD area is the mean of the quantifying ISTDs found in the run (or the reference ISTD); the ISTD
       table needs no concentrations for this.
     - Only this mode shows the Unit and ISTD concentration fields.
   - **Total extraction (µg/L)**.
   - **Area %**.

   Edits of the Internal standards table apply at once and can be undone.

   **Migration conditions** (*Quantify > Migration conditions...*): analyst, simulant (EtOH 95 %, 50 %, 20 %,
   Tenax, or *Other...* to type one), temperature, duration, cell area, coverage factor, volume and
   surface/volume. The report texts "Migrationszelle" and "Belegung" follow from the cell area (0.51 dm² =
   Zelle groß, 0.34 dm² = Zelle klein, 0.44 dm² = Glaszelle) and the coverage factor (1 = einfach,
   2 = doppelt). The numbers start from the NIAS parameter table, and the report always uses the current
   parameters.
9. **Double determination**: right-click a tab and choose *Double determination with ▸* (the partner is
   suggested), or use *Quantify > Double determination*.
   - Summary cards count confirmed substances, differences above the limit, artefacts found in only one
     determination, and differing identifications.
   - Every substance gets a plain-language verdict. The mirror plot shows A above and B below in FID signal
     units (baseline removed, solvent front left out); it fits the intensity to the visible time window, so a
     picked substance is shown full height. Wheel / drag: time; double-click: the whole run.
   - The limit is the report parameter "Duplicate difference limit". Clicking a row opens the peak.
   - **The analyst decides what goes out:**
     - the *Report* box of each substance (default: no artefacts, nothing below the reporting limit);
     - Substance and CAS: they become the identification of the peak in both determinations;
     - Area A/B, A/B and Mean [mg/kg], and a comment.

     The table edits like Excel: click or arrow keys to move, Shift+arrows or drag to mark cells;
     **Enter** puts the marked substances into the report, **Delete** takes them out; type or F2 to edit a
     cell; **Ctrl+C / Ctrl+V** copy and paste (one value goes into every marked cell); **Ctrl+D** or dragging
     the small square at the corner of the marking copies a name (or any value) down. Changed cells are
     marked with the old value, every change - also a whole fill - is one undo step and is written to the
     audit trail, and *Reset row* / *Reset all* take the changes back.
   - The NIAS report of the pair (*NIAS report – preview* or *NIAS report...*) contains exactly the rows with the
     Report box on, with the values set here.
   - Triplicates and more are in the *Groups (N-fold)* tab. The report buttons are here and in the *Report*
     menu, each with a preview.

10. **Unknown register** (*Identify > Unknown register...*): find unknowns by text (ID, name, CAS, note), by
    **sample name**, or by **m/z values** (e.g. `149 167 279`: every ion at least x % of the base peak, optionally
    the first one the base peak). Select an unknown to see its spectrum and sightings, then *Library search*,
    *Own library* or *NIST search* it; a library hit can be assigned as its name and CAS. **Mark** unknowns
    (✓ column, *Mark all shown*, *Mark selected*) and *Export to MSP...* or *Copy MSP* them to share with
    colleagues (ID, label, RT, samples, status and note are in the MSP comment). *Add to library...* stores an
    unknown in one of your libraries.

## Processing methods

*Method > Save current settings as Method...* stores all processing settings under a name
(`data/processing_methods`): integration methods (FID and MS), quantification (mode, unit, ISTD table, NIAS
parameters), blank subtraction, deconvolution, retention index, migration conditions, library search method
and peak type, own-library options, report options and the peak table's columns and value filter.
*Method > Load Method...* lists the saved methods with a summary; tick which parts to apply. Loading is one
undo step; the integration methods also become the default for runs loaded later. Methods can be exported
and imported as `.json` files to share them. Things that belong to single runs (ISTD peak bindings, manual
integration, blank assignments) are not part of a method. The status bar shows the current method.

## Layout

Every panel can be docked, tabbed or detached, for example onto a second screen. Its title bar has buttons to
maximize, detach / dock back and close it. **Double-click a panel's title** to let it fill the window (a detached
panel fills its screen); double-click again to restore the layout. Choose and order the columns of
the peak table in *Columns...* (Available ↔ Shown). The layout is stored by column, so new versions do not
reshuffle it.
- When a detached panel is dragged near an edge of the window, a band suggests where it will dock; release to
  dock it there.
- *Layout* menu: presets (Chromatogram top, Table left (classic), Integration, Review, Dual monitor), saved
  named layouts, and a switch to lock the panels.
- **Dark mode**: *View > Dark mode* (Ctrl+Shift+D) switches between the light and the dark look at once and is
  remembered. Plots, icons and run colours follow; exported pictures and reports stay on white paper.

## Projects

*File > Save project* writes a `.gcws` file. It holds the runs (relative paths), roles, blanks, methods,
manual events, identifications, quantification settings, replicate groups and the audit trail. Integrations
are recomputed on opening, and a changed result is reported. An autosave runs every 2 minutes
(*File > Recover autosave...*).

## Development

```
.venv\Scripts\python -m pytest            # all tests (the sample batch is read from ..\NIAS Working\samples)
.venv\Scripts\python tools\oracle_report.py   # integrator vs. former ChemStation integration
.venv\Scripts\python tools\check_vendor.py    # drift of the vendored NIAS modules and EI Atlas engine
.venv\Scripts\python tools\deconv_benchmark.py   # deconvolution vs. the AMDIS result of run 07
```

Install `requirements-dev.txt` for the tests (pytest, pytest-qt, hypothesis).

The NIAS modules the reports and quantification rely on are vendored in `gcws/nias_legacy`, EI Atlas's library
readers and search engine in `gcws/libsearch/vendor` (see `VENDORED.md`). Patches are marked `GCWS-PATCH`.

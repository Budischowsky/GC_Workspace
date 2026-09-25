# GC Workspace

A standalone application for GC-FID / GC-MS data: raw data browsing, its own integrator,
automatic library search (EI Atlas) with NIST handoff, NIAS quantification (mg/kg) and the
NIAS / Fingerprint / Total extraction reports. It needs no ChemStation or MassHunter software and no
`RESULTS.CSV`: everything is computed from the raw files.

## Install and start

1. Run **Setup GC Workspace.cmd** once. It creates `.venv` and installs `requirements.txt`, using `wheelhouse/`
   for an offline install if that folder exists.
2. Start with **Start GC Workspace.cmd**. You can also pass a `.D` folder or a `.gcws` project on the command line.

EI Atlas (library search) is the sibling project `..\UnknownEvaluation`, as for NIAS. Change its location in
*Edit > Preferences* if needed. On the first start the NIAS library-search methods and settings are copied
from `..\NIAS Working\data`. The unknown register is kept separate unless you choose
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
2. **Integration** runs automatically on load, with the method for the signal: FID / TIC / BPC / EIC, chosen in
   the toolbar. The *Integration method* panel holds the parameters (auto or fixed), the timed events and the
   list of manual events.
3. **Manual tools** in the toolbar work in the chromatogram and in the peak zoom:
   - select/zoom (Z), pan (H)
   - draw baseline (B), split with a drop line (S), delete (D), add peak (A)
   - move start/end (M), merge (G), tangent/exponential skim (K), negative peak (N), reset range (R)

   Manual changes are stored as events and re-applied after every re-integration. They can be undone
   (Ctrl+Z / Ctrl+Y) and are written to the audit trail.
   - **FID + MS**: with both detectors, the chromatogram and the peak zoom show the FID on top and an MS trace
     (TIC, BPC or any EIC) below, each run shifted by its own FID–MS delay, with linked zoom and a shared cursor.
     A click in the MS pane selects the FID peak at that time.
   - **Spectra by time**: right-click any chromatogram for the mass spectrum at that time, right-drag for the
     mean spectrum over a range, Shift+right-drag to set a background range that is subtracted. In the
     spectrum panel ← / → step one scan and Esc returns to the selected peak. Clicking an ion in a spectrum
     shows its EIC.
4. **Library search** (Ctrl+F): searches every integrated peak in EI Atlas and fills Name, CAS and Score. You can
   review the hits first. For a single peak there is the *EI Atlas* hit list, *Investigate* (the native
   EI Atlas window), *NIST*, MSP copy/save and *Register unknown*.
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
5. **Roles and blanks**: right-click a tab and set its role (Sample / Blank / Blank + ISTD / Standard /
   Alkane ladder). Blanks are suggested from the injection order; blanks you assign yourself are never
   re-suggested.
   - **Blank subtraction**: the toolbar button *− Blank* switches to "FID − Blank" / "TIC − Blank".
     - This is the sample minus its aligned blank, integrated like any trace.
     - FID default: only the blank's peaks, so the baseline is kept. MS default: the whole trace, including
       the bleed.
     - Settings are in *Quantify > Blank subtraction settings*.
     - Spectra can subtract the blank spectrum at the same time.
     - The peak table column *In blank* marks peaks found in the blank (blank level / partly / also in
       blank); *Hide blank peaks* hides the blank-level ones.
     - NIAS mg/kg keeps its own blank correction.
6. **Quantification** panel: choose the mode.
   - NIAS mg/kg: the ISTD factor chain and the migration conditions, computed with the unchanged NIAS/AutoLib
     arithmetic.
   - ISTD concentration, with a unit you choose.
   - Total extraction (µg/L).
   - Area %.
7. **Double determination**: right-click a tab and choose *Double determination with ▸* (the partner is
   suggested), or use *Quantify > Double determination*.
   - Summary cards count confirmed substances, differences above the limit, artefacts found in only one
     determination, and differing identifications.
   - Every substance gets a plain-language verdict, and a mirror plot shows A above and B below.
   - The limit is the report parameter "Duplicate difference limit". Clicking a row opens the peak.
   - Triplicates and more are in the *Groups (N-fold)* tab. The report buttons are here and in the *Report*
     menu, each with a preview.

## Layout

Every panel can be docked, tabbed or detached, for example onto a second screen. Choose and order the columns of
the peak table in *Columns...* (Available ↔ Shown). The layout is stored by column, so new versions do not
reshuffle it.
- When a detached panel is dragged near an edge of the window, a band suggests where it will dock; release to
  dock it there.
- *Layout* menu: presets (Chromatogram top, Table left (classic), Integration, Review, Dual monitor), saved
  named layouts, and a switch to lock the panels.

## Projects

*File > Save project* writes a `.gcws` file. It holds the runs (relative paths), roles, blanks, methods,
manual events, identifications, quantification settings, replicate groups and the audit trail. Integrations
are recomputed on opening, and a changed result is reported. An autosave runs every 2 minutes
(*File > Recover autosave...*).

## Development

```
.venv\Scripts\python -m pytest            # all tests (the sample batch is read from ..\NIAS Working\samples)
.venv\Scripts\python tools\oracle_report.py   # integrator vs. former ChemStation integration
.venv\Scripts\python tools\check_vendor.py    # drift of the vendored NIAS modules
.venv\Scripts\python tools\deconv_benchmark.py   # deconvolution vs. the AMDIS result of run 07
```

Install `requirements-dev.txt` for the tests (pytest, pytest-qt, hypothesis).

The NIAS modules the reports and quantification rely on are vendored in `gcws/nias_legacy` (see
`VENDORED.md`). Patches are marked `GCWS-PATCH`.

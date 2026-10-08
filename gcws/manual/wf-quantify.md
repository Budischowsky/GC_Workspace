# Blanks, internal standards and quantification

## Roles

Every loaded run has a **role**. It decides how the run is used.

| Role | Meaning |
|---|---|
| **Sample** | A run that is quantified and reported. |
| **Blank** | The solvent or simulant alone. What is in it is not from the sample. |
| **Blank + ISTD** | The blank with the internal standards added. |
| **Standard** | A standard solution. |
| **Alkane ladder** | The n-alkane run for the retention index. |

The role is suggested from the name of the run. Change it with a right-click on the run in **Loaded samples**
(**Role**), with **Quantify > Role of active chromatogram**, or in the **Properties** panel. The status bar
shows the role of the active chromatogram.

## Blanks

The blanks of a sample are **suggested from the injection order** in its own batch folder: the solvent blank
injected after the sample and the Blank + ISTD injected before it. A blank from another folder is never
suggested.

**Quantify > Assign blanks...** (also in the right-click menu of a loaded sample and in **Properties**) shows
the suggestion; tick other runs to change it. Blanks you assign yourself are kept.

The status bar shows the blank of the active sample, in yellow **Blank: none** when there is none.

### Blank subtraction in the chromatogram

**subtract blank** in a chromatogram panel (or **Chromatogramm > Chromatogram 1 / 2 > subtract blank**) shows
and integrates the sample *minus its blank*. The signal is then called "FID − Blank" or "TIC − Blank". You
decide this per panel.

**Quantify > Blank subtraction settings...** sets how:

- For the FID only the blank's *peaks* are subtracted, so the sample keeps its own baseline. For MS traces the
  whole blank trace is subtracted, which also removes the rise of the column bleed.
- The blank is first shifted in time to fit the sample.
- With **Automatic blank subtraction** the subtracted traces are rebuilt after every change. Switch it off to
  rebuild them only with **Subtract**.
- Peaks of the internal standards are never subtracted.

All fields are listed in [Settings and defaults](ref-settings.md#blank-subtraction).

### Blank check in the peak table

Independently of the subtraction, every peak is compared with the blank. The peak table column **In blank**
says whether a peak is also in the blank, and **Hide blank peaks** hides the peaks that are at blank level
(sample area below three times the blank area unless changed).

The NIAS mg/kg calculation has its own blank correction, which these settings do not change.

## Internal standards

Open **Quantify > Quantification panel**. The **Internal standards** table lists the standards of the method:
code (IS1, IS2 ...), name, concentration, target retention time, and whether the standard is used to quantify.
**Add** and **Remove** change the table; every edit applies at once and can be undone.

**ISTDs in the active chromatogram** shows, for the active run, which peak was bound to each standard, its
area and how far it is from the target time.

| Function | What it does |
|---|---|
| **Automatic** | Finds the standards again by name and target retention time. |
| **Bind selected peak** | Binds the peak selected in the peak table to the standard selected in the list. |
| **Unbind** | Releases the binding. |
| **Quantify > Set selected peak as ISTD** | The same from the menu or the peak table's right-click menu: choose IS1, IS2 ... directly. |
| **Detect ISTDs...** | A more thorough search, see below. |
| **Learn spectrum** | Keeps the spectrum of the bound peak as the reference spectrum of that standard. |

### Detect internal standards

**Detect ISTDs...** (or **Quantify > Detect internal standards...**) looks for every standard in three ways:
by its **library names** (in all hits of a peak, not only the first), by its **reference spectrum** if one was
learned, and by its **retention time**. For the retention time it allows a common shift of the whole run - a
shortened column moves all standards alike.

The window lists the peak found for each standard with the evidence and a confidence (high, medium, low).
High confidence is ticked already. **Bind checked** binds them as one undo step. With
**Also move the target RTs to where the standards were found** the table is updated too.

Use **Learn spectrum** once on a run where the standards are bound correctly. Later runs then find them by
spectrum even without a library search.

## Quantification modes

**Mode** in the **Quantification** panel. The panel shows the formula of the chosen mode.

| Mode | Result | Notes |
|---|---|---|
| **NIAS screening (mg/kg via ISTD, migration)** | mg/kg | The NIAS calculation: ISTD factor and migration conditions. The **NIAS parameters** table and **Migration conditions...** belong to it. |
| **Internal standard concentration** | the unit you choose | Concentration = corrected area ÷ ISTD area × ISTD concentration. Enter **ISTD concentration** and **Unit**. |
| **NIAS total extraction (µg/L)** | µg/L | |
| **Area percent** | % | Each peak as a share of all peaks. |
| **HS-Screening (MS only)** | µg/HS, µg/dm², µg/g or mg/m² | Headspace screening on the TIC, with its own standards. See below. |
| **Extraction (quant method)** | Conc. 1 of the quant method | A solid (g) or a foil (dm²) extracted into a volume (mL) with the internal standards. See [Quant method](#quant-method). |

**Detector** chooses whether the quantities come from the **FID** peaks or from the **TIC (MS)** peaks.
Internal standards, blanks and names are then taken from the same detector.

**Factor from the mean of the ISTD areas** uses the mean of all quantifying standards instead of one.
**RRT reference ISTD** chooses the standard against which the relative retention time (peak table column
**RRT**) is calculated.

### Migration conditions

**Migration conditions...** in the panel: analyst, simulant, temperature, duration, cell area, coverage factor,
volume and surface-to-volume ratio. Cell area, coverage and surface / volume are inputs of the calculation and
change the NIAS parameters too. The report texts follow from them. A NIAS report cannot be made without them;
the program opens this window if they are missing.

### Quant method

The mode **Extraction (quant method)** quantifies an extraction: a solid (g) or a foil (dm²) is extracted into
a volume of solvent (mL) to which the internal standards are added. The panel shows the group **Quant method**
instead of the NIAS parameters; the **Internal standards** table stays (by default the NIAS standards, with
**Conc.** = the stock concentration of each standard in mg/mL).

| Field | Meaning |
|---|---|
| **Sample** | **Solid (g)** or **Foil (dm²)**. |
| **Sample amount** | The sample mass (g) or area (dm²) of the method. |
| **Extract volume** | The volume of the extract (mL). |
| **Spiked standard** | The volume of the standard solution added (µL). |
| **Conc. 1**, **Conc. 2** | The two units of the result. Conc. 1 is the **Conc.** of the peak table and the double determination; the Quantification report gives both. |
| **Reporting limit** | Substances below it (in the Conc. 1 unit) are left out of the report; **Off** reports all. |
| **Active run amount** | The active run's own sample amount (e.g. its weighed mass); **Method amount** uses the method's. |
| **NIAS standards** | Fills the **Internal standards** table with the NIAS standards and their stock concentrations again. |

Units: a solid gives mg/mL, µg/L (of the extract), mg/g, mg/kg, µg/g and µg/kg; a foil gives mg/mL, µg/L,
mg/dm² and µg/dm².

Calculation:

- standard amount (mg) = stock concentration (mg/mL) × spiked volume (µL) ÷ 1000
- factor = mean standard amount ÷ mean standard area of the quantifying standards found in the run (with
  **Factor from the mean of the ISTD areas** off: the reference standard alone)
- substance (mg) = corrected area × factor
- mg/kg = substance (mg) × 1000 ÷ sample mass (g); µg/L = substance (mg) × 1 000 000 ÷ extract volume (mL);
  mg/dm² = substance (mg) ÷ sample area (dm²); and so on

The panel shows the active run's factor and what is missing. Every substance is taken to respond like the
standards (no response factors). The cells of the peak table show their calculation as a tooltip.

Methods are kept by name: the list at the top of the group chooses a saved method, **Save** keeps the changes
under its name, **Save as...** under a new one and **Delete** removes the saved method (the settings stay). The
note under the list says when the method differs from its saved version. A quant method holds the fields above,
the internal standards and the detector, never a run's own amount. It is also part of a processing method
(**Method > Save current settings as Method...**).

The report of this mode is the **Quantification Report** (see [Reports](wf-report.md)).

### HS-Screening

1. Choose the mode **HS-Screening (MS only)**. Chromatogram 1 switches to the TIC.
2. Enter up to seven standards with name and target retention time, or bind TIC peaks with
   **Bind selected TIC peak**. The amount is 1 µg per vial unless changed.
3. Tick the standards to use. With **Use mean of activated ISTD areas** their mean is the reference; without
   it exactly one standard must be active.
4. Choose the **Result unit**. For µg/dm² and mg/m² enter **Sample area (dm²)**, for µg/g **Sample mass (g)**.
   **Report Conc. 1** and **Report Conc. 2** choose the two units of the HS report (by default µg/dm² and
   mg/m²; 1 µg/dm² = 0.1 mg/m²); every determination of the report then needs its sample area.
5. **Subtract matching Blank / Blank+ISTD (larger area)** subtracts the blank's peak area.

Calculation: corrected TIC area × mean ISTD amount ÷ mean ISTD TIC area. These are screening estimates; the
response of every substance is taken to be that of the standards.

#### External calibration

When the samples contain no ISTD, the same seven standards are measured in separate calibration vials (the ISTD
mix) - a 1-point calibration. **Calibration** offers two ways.

**External: calibration runs** - the standards are measured in one or more runs loaded beside the samples:

1. Set **Calibration** to **External: calibration runs**.
2. Under **Calibration runs**, tick the run(s) with the calibration vial. The list shows every loaded run with its
   role; runs that look like an ISTD mix (role Blank + ISTD) come first. A ticked run gets the role **Standard**
   (undoable, saved with the project); unticked, it gets back the role its name suggests.
3. **Go to run** (or a double-click in the list) shows the calibration run. Check there that the seven standards
   are found - by target RT or name - and bind them if needed (**Bind selected TIC peak**, **Detect...**, the
   right-click menu of a peak). Binding is only possible in a calibration run; on a sample the status line points
   to **Go to run**.

On a sample the table below shows each standard's TIC area in every calibration run, the **Mean area**, whether
it is active and its status - before any run is ticked too, with the status *No calibration run*. Each
standard's area is averaged over the calibration runs; the factor is Σ µg ÷ Σ mean areas of the activated
standards (with one active standard: its µg ÷ its mean area). An activated standard missing in any calibration
run stops the calculation, and the message names the run.

**External: entered areas** - the calibration was measured before (another sequence, a control chart):

1. Set **Calibration** to **External: entered areas**. The table of standards gets the column **TIC area**; when
   calibration runs were ticked, their mean areas are filled in for a start.
2. Type the TIC area of each activated standard (and its µg/HS). An active standard without an area stops the
   calculation and the message names it.

The factor is Σ µg ÷ Σ entered areas. The areas are part of the HS settings, so they are kept in a processing
method and also apply to unattended processing.

In both ways every TIC peak of a sample is quantified as corrected area × factor; blank correction and the units
work as above. **Method > Run Method** and **Detect...** (all loaded samples) look for the standards only in the
calibration runs (with entered areas: nowhere), binding a standard by right-click works only there, and sample
peaks near a standard's RT are blank-corrected like any other peak. The HS report names the calibration
(*external standard (calibration runs)* or *(entered areas)*), and its sheet HS standards lists the area of each
standard in every calibration run.

To keep this setup, save it with **Method > Save current settings as Method...** (e.g. "HS external").

## Where the results are

The peak table shows **Conc.** in the unit of the mode, **Corr. area** (after the blank correction), **ISTD**
(which standard a peak is), **SML** and **Status**. More columns - **mg/dm²**, **µg/dm²**, **µg/L**, **mg/L**,
**mg/mL**, **mg/g**, **mg/kg**, **µg/kg**, **µg/HS**, **µg/g**, **mg/m²**, **RRT**, **Blank area** - can be
switched on with **Columns**.

The NIAS modes give every substance in these further units as well:

| Column | NIAS screening (mg/kg) | NIAS total extraction (µg/L) |
|---|---|---|
| **mg/dm²** | corrected area × mean ISTD factor | - |
| **µg/dm²** | mg/dm² × 1000 | - |
| **µg/L** | mg/dm² × cell area × coverage × 1 000 000 ÷ extract volume (mL): the substance in one litre of extract | the result itself |
| **mg/L** | µg/L ÷ 1000 | µg/L ÷ 1000 |
| **mg/mL** | µg/L ÷ 1 000 000 | µg/L ÷ 1 000 000 |

The extract volume is the **Extract volume** of the NIAS parameters. Hover a value to see its calculation in
numbers; **Conc.** shows its own (mg/kg = mg/dm² × surface/volume). An empty cell's tooltip says what is
missing. In HS-Screening, **µg/dm²** is the HS amount divided by the sample area.

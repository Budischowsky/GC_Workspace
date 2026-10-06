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
| **HS-Screening (MS only)** | µg/HS, µg/dm² or µg/g | Headspace screening on the TIC, with its own standards. See below. |

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

### HS-Screening

1. Choose the mode **HS-Screening (MS only)**. Chromatogram 1 switches to the TIC.
2. Enter up to seven standards with name and target retention time, or bind TIC peaks with
   **Bind selected TIC peak**. The amount is 1 µg per vial unless changed.
3. Tick the standards to use. With **Use mean of activated ISTD areas** their mean is the reference; without
   it exactly one standard must be active.
4. Choose the **Result unit**. For µg/dm² enter **Sample area (dm²)**, for µg/g **Sample mass (g)**.
5. **Subtract matching Blank / Blank+ISTD (larger area)** subtracts the blank's peak area.

Calculation: corrected TIC area × mean ISTD amount ÷ mean ISTD TIC area. These are screening estimates; the
response of every substance is taken to be that of the standards.

#### External calibration

When the samples contain no ISTD, the same seven standards are measured in separate calibration vials (the ISTD
mix) - a 1-point calibration.

1. Set **Calibration** to **External (Standard runs)**.
2. Give every calibration vial the role **Standard** in the sample rail.
3. Open each Standard run and check that the seven standards are found; bind them there if needed. Binding is
   only possible in Standard runs.

Each standard's TIC area is averaged over all Standard runs; the factor is Σ µg ÷ Σ mean areas of the activated
standards (with one active standard: its µg ÷ its mean area). Every TIC peak of a sample is quantified as
corrected area × factor; blank correction and the units work as above. An activated standard missing in any
Standard run stops the calculation, and the message names the run. The HS report lists the averaged standards and
the calibration runs.

To keep this setup, save it with **Method > Save current settings as Method...** (e.g. "HS external").

## Where the results are

The peak table shows **Conc.** in the unit of the mode, **Corr. area** (after the blank correction), **ISTD**
(which standard a peak is), **SML** and **Status**. More columns - **mg/dm²**, **µg/HS**, **µg/dm²**, **µg/g**,
**RRT**, **Blank area** - can be switched on with **Columns**.

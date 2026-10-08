# Report templates

A **report template** decides what the **Template Report** shows: its columns, its header, which rows are
reported and the extras (sums, footnotes, further sheets, the Word document). You design it once in the window
**Report template**, keep it with the processing method, and the report is made for every sample the method
processes - by **Run Method**, by the automation and from the report buttons. It works in every quantification
mode: NIAS mg/kg, internal standard concentration, total extraction, area percent, HS-Screening and
Extraction (quant method).

The fixed reports (NIAS, Fingerprint, Total extraction, HS-Screening, Quantification) stay as they are; a
template is a further report next to them.

## Open the window

**Method > Report template...** or **Report > Edit report template...**. The window stays open while you work:
click another run or replicate group and the preview follows.

The window opens with the method's template. A method without one starts from the layout that fits the
quantification mode (NIAS, HS-Screening or Quantification).

## Start from a layout

**New from** offers the built-in layouts:

| Layout | Columns |
|---|---|
| **NIAS layout** | RT, Name, CAS-No., % match, Conc. mg/dm², Conc. mg/kg, SML (mg/kg), Ref.; reporting limit 0.01 mg/kg, the NIAS sums and footnotes, landscape - the look of the NIAS Report. |
| **HS-Screening layout** | RT, Name, CAS-No., Qual, Conc. 1, Conc. 2 in the HS report units, portrait. |
| **Quantification layout** | The same six columns in the units of the quant method. |
| **Empty template** | Nothing yet: add the columns you want. |

**The method's template** opens the template the method carries now.

## Columns

On the left, **Available** lists every value of the **Peaks / substances** panel and of the double
determination and replicate worksheet, in five groups:

- **Substance**: RT, Name, CAS, Score, ID status, Library, RI, RRT, Class hint.
- **Peak**: Peak #, RT MS, Type, Start, End, Area, Area %, Height, W½, Symmetry, S/N, Integration, ISTD, Raw
  area, Blank area, Corr. area, In blank, Blank ratio, Area − blank.
- **Concentration**: Conc. in the mode's unit, Conc. 1 and Conc. 2 of the mode, and every unit: mg/kg,
  mg/dm², µg/dm², µg/L, mg/L, mg/mL, mg/g, µg/g, µg/kg, µg/HS, mg/m².
- **Double determination**: Diff. %, SD, RSD %, Found in, Verdict, Notes, Comment, Outlier, Changed by
  analyst, Feature, Similarity.
- **Regulatory**: SML, Ref., Footnote, SML check (≤ SML, > SML or no SML) and the NIAS SML status.

A grey column is one the current quantification cannot fill; its tooltip says why (for example "HS-Screening
gives no mg/kg"). You can still add it: a template is meant for several modes, and in a report the column is
left out with a warning.

**Add →** (or a double-click) puts the selected columns at the end of **Report columns (report order)** on the
right. There you set for every column:

- **Header**: the column title in the report. `{unit}` is the column's unit; an empty header takes the
  default, which follows the unit.
- **Decimals**: `auto` is the column's usual number of decimals.
- **Double determination**: how a double or N-fold determination is shown:
  **Mean** (the result, with the analyst's edits and without a dismissed outlier), **Each determination**
  (one column per determination: A, B, ...), **Each + Mean** (both), or **Merged (A / B)** (both values in
  one cell, a dismissed one in brackets). A single determination always shows one column. For **Name** and
  **CAS**, each determination is its own library hit.

**▲ Up**, **▼ Down** and drag and drop order the columns; **Remove** takes them out. Double-determination
columns appear only when there are two or more determinations.

## Header

**Title (red band)** and **Subtitle** are the first two lines. Below them come the header fields: a **Label**
and its **Value** each, on the **Line** given (fields with the same line number share a line). **Add field**,
**Add line** and **Remove field** change them.

Values and titles can hold placeholders in curly brackets; **Insert placeholder** lists them:

| Placeholder | Becomes |
|---|---|
| `{sample}` | the sample (first determination) |
| `{samples}` | every determination's name |
| `{group}` | the replicate group |
| `{determination}` | single determination, double determination, N-fold determination |
| `{n}` | the number of determinations |
| `{analyst}` | the analyst of the migration conditions, else the Windows user |
| `{operator}` | empty (to fill in by hand) |
| `{migrate}` | simulant, temperature and duration |
| `{sv_ratio}` | the surface/volume ratio |
| `{date}` | today |
| `{method}` | the processing method |
| `{quant_method}` | how the result was quantified (mode, quant method, HS calibration) |
| `{mode}`, `{unit}`, `{unit1}`, `{unit2}` | the quantification mode, its unit, Conc. 1 and Conc. 2 |
| `{detector}` | FID or TIC |
| `{blanks}` | the blank runs |
| `{template}` | the template's name |

## Rows

- **Only rows ticked Report in the double determination**: the rows you report in the double determination,
  with your values (a single determination reports every substance).
- **Leave out the internal standards**, **Leave out rows without name and CAS**, **Leave out library sum rows
  ('Sum of ...')**.
- **Unidentified substances**: report them or leave them out.
- **Minimum score**: rows with a lower library score are left out (**Off**: none).
- **Reporting limit**: **Off**, **The method's reporting limit** (NIAS parameters or the quant method) or
  **This value**, compared with the concentration chosen after **on** (default: the first concentration
  column).
- **Sort by**: retention time, name, or the concentration (highest first).
- **NIAS category sums (styrene oligomers, hydrocarbons, siloxanes, cyclic polyester oligomers)** and **Sums of
  substances found more than once**: the sum rows of the NIAS Report, bold below the substances, with the note
  on how they were calculated.
- **Text when nothing is reported**: the line the report shows instead of an empty table.

## Extras

- **Page**: portrait or landscape.
- **Word document (.docx) next to the workbook**.
- **Bold above the SML**: the concentration printed bold when the substance has no SML or exceeds it, as in the
  NIAS Report.
- **Footnote markers from CASINFO.xlsx**: the footnotes of CASINFO.xlsx as (a), (b), ... on Ref. (or on the
  name) and their text below the table.
- **Notes**: lines printed below the table.
- **Further sheets of the workbook**: **Determinations** (each determination's result in Conc. 1 and Conc. 2),
  **Calculation** (factor and calculation of every determination, its standards) and **Audit**.
- **Audit Trail page in the Word document**.
- **Hide columns without any value**.
- **Count the reported substances in the unknown register**.
- **Create the report when the method runs (Run Method, automation)**.
- **Report buttons preview this report**: the **Report preview** buttons of the panels show the template
  report instead of the fixed one.
- **File name ending**: the end of the file name after the sample number; empty: `_<template name>_Report`.

## Preview

The lower half shows the report with real data: **Active run** (the active chromatogram on its own) or
**Active replicate group** (its double or N-fold determination). Columns the quantification cannot fill and
other problems are listed above it. **Word preview...** makes the report and shows its Word pages (this needs
Microsoft Word).

## Keep it

- **Save** and **Save as...** keep the template by name (in the data folder, `report_templates`), **Delete**
  removes the saved file. The chips show **changed** (not saved since the last change) and **In method**.
- **Use in method**: the method reports with this template from now on (one step, **Edit > Undo** reverts it).
  Save the processing method to keep it in the method file.
- **Save to method**: the same, and the template is written into the processing method loaded last right away.

A processing method carries its template in the section **Report template**
(see [Processing methods](wf-methods.md)).

## When the report is made

- **Run Method** writes the Template Report of every processed sample - per replicate group whose
  determinations were all processed, else per sample - next to the data, as the menu reports do. No window
  asks anything; the status bar names the files and any sample that could not be reported.
- The automation's **Report** node with the kind **Template Report** writes it for every job
  (see [Unattended processing](wf-automation.md)).
- **Report > Template Report...** and **Report > Template Report - preview** make it by hand.

The concentrations are those of the double determination: each determination is converted with its own sample
amount, the analyst's edits and a dismissed outlier count, and the NIAS sums follow the NIAS Report. A
CAS-only substance keeps an empty name (the Template Report does not look names up on the internet).
Determinations of an N-fold group (three or more) are reported without edits, as in the fixed reports.

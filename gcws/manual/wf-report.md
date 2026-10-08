# Reports

## The reports

| Report | For | Quantity |
|---|---|---|
| **NIAS Report** | Migration testing: substances in mg/kg against their limits. | mg/kg |
| **Fingerprint Report** | A qualitative picture of the sample. | Area % |
| **Total Extraction Report** | Total extraction. | µg/L |
| **HS-Screening Report** | Headspace screening (mode **HS-Screening**). | Conc. 1 and Conc. 2, by default µg/dm² and mg/m² |
| **Quantification Report** | An extraction quantified with a quant method (mode **Extraction (quant method)**). | Conc. 1 and Conc. 2 of the quant method |
| **Template Report** | Your own report: the columns, header and rows of the method's report template, in every quantification mode (see [Report templates](wf-report-templates.md)). | Any value of the Peaks panel and of the double determination |

Each report is written as an Excel workbook and a Word document.

The Quantification and the HS-Screening reports have the same table: **RT**, **Name**, **CAS**, **Qual** (the
library match; of a double determination the lower one), **Conc. 1** and **Conc. 2** - the same result in two
units. A single determination gives its own values. A double determination gives the mean of A and B in each
unit, with your changes from the double determination (an edited value, a dismissed outlier); A and B are each
converted with their own sample amount. The sheet **Determinations** lists A and B in both units; the other
sheets show the calculation (sample amounts, standards, factor, every peak). The preview button of the
**Peaks** panel reports the active run alone; the **Replicates / results** panel and the Report menu report the
group. The report that fits the quantification mode is the one the **Report preview** buttons show - or the
Template Report, when the method's report template asks for it.

## Make a report

1. Make sure the sample has its blank, its internal standards and its names
   (see [Blanks, internal standards and quantification](wf-quantify.md)).
2. For a double determination, **Compare** first and decide the red rows
   (see [Double determination](wf-double.md)).
3. **Report > NIAS Report - preview** (or the preview of another report) shows the Word report page by page
   without saving anything. **Save report...** in the preview saves it, **Open in Word** opens it.
4. **Report > NIAS Report...** asks where to save and writes the files.

The report is made for the replicate group of the active chromatogram - the pair of the double determination,
or the group selected in the list **Determinations**. If there is none, the program says so and opens the
**Replicates / results** panel. The same report buttons are in that panel.

If something needed is missing, the program names it instead of writing a wrong report. Without migration
conditions the window to enter them opens. A sample without a blank from its batch is reported with a warning.

The preview needs Microsoft Word on the PC. Without it, use **Open in Word** after saving.

## What goes into the report

- The substances of the peak table of the quantification detector, with the names, CAS numbers and quantities
  as they are in the project.
- For a double determination: exactly the rows with the **Report** box ticked, with the values shown there.
- The report parameters of the **NIAS parameters** table in the **Quantification** panel: reporting limit,
  quality limit, duplicate difference limit and others.
- Retention index instead of retention time if chosen under
  **Identify > Retention index (alkane ladder)...**.

**Report > Keep intermediate workbook** also keeps the workbook with the intermediate calculations, to follow a
value step by step.

## All samples of a batch at once

**Report > Batch report of this folder...**

1. Load the batch and select one of its chromatograms.
2. Choose the report and the folder for the results.
3. Every sample of that batch folder is reported. Each report is judged by the Report² rules
   (see [Unattended processing](wf-automation.md)), and all samples go into one Word document with a summary
   workbook: sample, status, findings, report file.

## Other exports

| Export | Where |
|---|---|
| The peak table as an Excel or CSV file | **File > Export peak table...**, or **Export** in the peak table. It follows the table's filters and columns. |
| A chromatogram as a picture | **File > Export chromatogram...** |
| A spectrum as MSP | **Mass Spectrum > Copy MSP** / **Save MSP...** |
| The double determination list | **Export...** in the **Double determination** tab |
| The results of a replicate group | **Report preview ▾ > Export worksheet...** on the worksheet of the group |

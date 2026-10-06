# Double determination

A sample measured twice is reported as one result: the mean of determination A and B, for every substance
found in both. This chapter is the practical guide. What the program does inside is explained in
[How the double determination works](double-determination.md).

## Start

- Right-click one of the two runs in **Loaded samples** and choose **Double determination with**, then the
  partner. The run with the same sample number is offered first.
- Or **Quantify > Double determination...** and choose **A** and **B** at the top of the page. **⇄** swaps them.
- Or select the pair in the list **Determinations** on the left of the panel.

Press **Compare**.

## Read the result

The chips at the top count the rows of each colour - **All**, **To check**, **Red**, **Yellow**, **Green**,
**Grey** (and **Deleted** once you deleted a row) - and the line below them sums it up, for example: *3 of 84 substances need your decision (red;
F3 = next); 9 were made consistent automatically (yellow), 72 are confirmed. Mean difference 6.2 %.* When the
panel is narrow the line is shortened; its tooltip shows all of it.

| Colour | Meaning | You |
|---|---|---|
| **Green** | Confirmed: in both determinations, same substance, difference within the limit. | do nothing. |
| **Yellow** | Made consistent automatically - a gap fill, a name chosen from both hit lists, two candidate names, boundaries proposed. | take a quick look. |
| **Red** | Needs a decision - only in one determination, spectra differ, unclear pairing, difference too large. | decide. |
| **Grey** | Not reported anyway - below the reporting limit or at blank level. | do nothing. |

The column with the verdict says why a row has its colour; its tooltip gives the evidence.

- The list starts with the open red rows, then the decided ones, yellow, green and grey, each by retention
  time. Click a column header to sort by that column instead; the list keeps it until the program is
  closed.
- **Diff. %** is drawn as a bar: the thin line is the limit, a full bar 1.5 times the limit. The colour is
  the row's.
- A ⚠ before the substance means the two determinations found different library hits; right-click the row
  for the candidate names.
- Right-click a column header to show or hide columns. **Area A**, **Area B**, **Notes** and the two hit
  columns are hidden at first; the icon, **Report** and **Substance** always stay. The choice is
  remembered.

- Click a chip to see only its rows; click it again for all rows. **Red** shows just the rows to decide;
  `F3` jumps to the next red row that is still open.
- A red row you have answered (its **Report** box clicked, a comment or a value) is **decided**: it gets the
  mark ◉ and `F3` goes past it. It stays decided when you tick the box back. The line above the list counts
  down, for example *Red: 7 → 2 open · 5 decided*, and says **All decisions made** when none is left. Undo
  (or **Reset row**) makes the row open again.
- A decided row stays where it is in the list, so the next click hits the row you expect. The list is sorted
  again at the next **Compare**, chip or header click.
- **To check** shows the red and yellow rows and the rows you changed.
- Click a row: both chromatograms below show that substance, A upwards and B downwards, and beside them the
  two spectra, mirrored the same way (feature pairing only). The integrated peak of the substance is shaded
  in both determinations, so different boundaries show before you harmonise them.
- Click a dot in the chromatograms to select its substance in the list (the list shows all rows again if
  the substance was filtered out).
- **Plots** beside the summary line hides the chromatograms and spectra, so the list gets the whole height.

## Decide

Right-click a row:

| Item | When it is offered | What it does |
|---|---|---|
| **Report** / **Not reported** | always | Puts the substance (or all marked rows, when it is one of them) into the report, or takes it out. |
| **Delete row** | always | Deletes the substance (or all marked rows): it leaves the list, the counts and the report. A red row counts as decided. |
| **Restore row** | under the chip **Deleted** | Brings the deleted substance (or all marked rows) back. |
| **Reset row** | the row has your changes (not offered otherwise) | Takes back your changes of this substance. |
| **Comment…** | always | A comment for the report. |
| **Show in A** / **Show in B** | the substance is in that determination | Opens that determination with the peak selected. |
| **Copy row** | always | Copies the visible cells of the row as text. |
| **Name: ...** | two candidate names, or differing spectra | Gives both determinations that name. |
| **Remove the gap fill** | the row has a gap fill | Takes the gap-filled peak out again. |
| **Harmonise the boundaries** | boundaries are proposed | Moves the peak boundaries of one determination to match the other. |

**Harmonise boundaries** below the list takes over all proposed boundaries at once.

You can also edit the list directly, as in a spreadsheet:

- the **Report** box of a substance decides whether it goes into the report. One click anywhere in the cell
  switches it; the list does not scroll. By default, substances found in one determination only, values
  below the reporting limit and (feature pairing) substances at blank level are not reported;
- **Substance** and **CAS** become the name of the peak in both determinations;
- the areas, the values of A and B, the mean and a comment can be changed.

Move with the arrow keys, mark several cells with `Shift` or by dragging, type or press `F2` to edit.
`Enter` puts the marked substances into the report and `Backspace` takes them out; `Space` on a **Report** box
switches all marked rows (into the report, or out of it when all are in already). `Delete` deletes the marked
rows (see below). `Ctrl+C` / `Ctrl+V` copy and
paste, `Ctrl+D` copies a value down. A changed cell is written in italics with a small triangle in its corner (as a comment in a
spreadsheet), so it is never mistaken for a yellow row; its tooltip shows the old value. Every edit is one undo
step and is written to the audit trail.

**Reset row** in the right-click menu takes back your changes of a substance; **More > Reset all** every change
of this double determination.

**Delete row** (or `Delete`) takes substances you do not want out of the list altogether - a peak of the
solvent or the column, for example. They no longer count on the chips or in the line above the list, `F3` skips
them, and the report leaves them out. The chip **Deleted** shows them (struck through); **Restore row** there,
or `Delete` again, brings them back, and `Ctrl+Z` undoes the deletion. Like every change it is saved in the
project and written to the audit trail.

## Accept

**Accept double determination** below the list can be pressed once A and B are compared - also while red
rows are still open: those keep their default (found in one determination only: not reported; the others:
reported), and the button's tooltip says how many there are. It saves your name, the time and the number of red
rows left open with the project, writes them to the audit trail, and the pair gets a ✔ in the list
**Determinations**. `Ctrl+Z` takes the acceptance back. The status bar says what happened:

- When the pair is a sample a workflow processed (it is listed in Report²), its report in Report² is
  accepted too - also when you opened the project by hand rather than from Report². Changes you made are
  saved first, and the report is then made again; without changes it is accepted as it is, with the usual
  few seconds to undo it in Report².
- Otherwise (a project you made yourself) the pair is **listed in Report²**, accepted by you: GC Workspace
  makes its report (Excel, Word and the double-determination workbook, as **Report preview** does) and
  keeps it with a copy of the project in a Report² job folder. It stands under the batch of its sequence
  folder - beside the samples a workflow processed there, if any - and Report² shows and selects it. Your
  own project is not changed (save it to keep the acceptance). The report needs what **Report preview**
  needs (e.g. the migration conditions); if something is missing, the status bar says what, and the pair is
  listed when you accept it again.
- When Report² lists that name in the batch already, GC Workspace asks: **Rename…** gives the pair (and its
  entry) a new name - a free one such as *Name (2)* is proposed; **Overwrite** replaces the entry you
  accepted before with this one (its next revision), or hides the report a workflow made of that name
  (**View > Show deleted reports** in Report² brings it back); **Cancel** lists nothing.
- An entry accepted by hand is never processed or delivered by a workflow: open it in Replicates from
  Report², change it and accept it again - its entry is replaced without asking.

A change after that - a value, a **Report** box, a comment, a name - reopens the pair: the button then reads
**Accept again** and its tooltip says who accepted it before.

## The limit

**Difference limit** is the largest relative difference between A and B (as a percentage of their mean)
that counts as agreement. It is the report parameter *Duplicate difference limit* - changing it here changes
every report of the project, and the status bar says so. The chip beside it reads **report parameter**; when
the value is not the default it turns yellow and shows the default.

## Report

**Report preview** shows the report of this pair (NIAS, or HS-Screening when the quantification uses the
TIC) with exactly the rows that have the **Report** box ticked and the values set here. The arrow beside it
offers the other reports and **Export worksheet...**, which writes the list as a worksheet. The reports are
also in the **Report** menu.

## Settings

**More > Settings…** opens the pairing, gap filling and naming parameters. They are explained, with their defaults,
in [How the double determination works](double-determination.md#settings). **Pairing: Classic** switches back
to the pairing by name and retention time of earlier versions.

## Three or more determinations

Select a group of three or more in the list **Determinations** on the left: its worksheet shows the means.
The colours are those of the pair page - **Red**: the determinations name the substance differently;
**Yellow**: found often enough, but the identification is to be reviewed; **Green**: found as the validity
rule asks and identified; **Grey**: found in too few determinations, not reported. The chips filter the
rows, and the chromatograms of all determinations are drawn on top of each other below the worksheet.
**← Double determination** above the worksheet, or a click on a pair in the list, goes back. Comparing
two runs of a group of three as a pair leaves the group whole.
**More > 3+ determinations…** and **Quantify > Replicate groups (N-fold)** go there too.

| In the list | What it does |
|---|---|
| **+ > Suggest** | Groups runs with the same sample number that differ only by _A, _B, _C. |
| **+ > New...** | Makes a group by hand. |
| **Members...** (right-click) | Ticks the determinations of the group and puts them in order: the first is A, the second B ... |
| **Validity rule** (right-click) | **Found in all determinations**, **Found in the majority** or **Found in at least one**. |
| **Rename...**, **Delete** (right-click) | Renames or removes the group (not the runs). |
| **Report preview** | The report of the group; its arrow offers the other reports and **Export worksheet...** (the averaged results of the group). |

## Many samples

The list **Determinations** holds every pair of the project. **Open** counts the red rows still open at
the last comparison, ✔ means nothing is left to decide and – that the pair was not compared yet. `Ctrl+F3`
goes to the next pair that still needs you. Pairs the run names suggest appear in italics until you select
them.

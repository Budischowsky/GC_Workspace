# Learning from evaluations

GC Workspace can learn report rules from evaluations analysts made by hand: the `NIAS-Screening-*.xlsm` workbooks
in the `Auswertung` folder of each run. It compares its own result with the analyst's, proposes settings and
rules that bring the two closer, and measures every proposal on batches it was not fitted on. Nothing is
changed without your approval, and every rule that changes a report row says so in the row.

The learning tools are run from a command prompt in the GC Workspace folder, with
`.venv\Scripts\python.exe -m gcws.learn ...`. Everything they write goes to the `learn` folder of the data
folder; the training data and the raw data are only read.

## The training data

A training folder holds batch folders as they come from the instrument: the runs (`*.D`), the blank runs of the
batch, and in the evaluated run's `Auswertung` folder the analyst's workbook. Each workbook evaluates one run
(a single determination).

**check** reads every workbook and writes `corpus_check.md`: how many workbooks could be read, which template
they use, what the analysts decided per peak (named, group, unknown, co-elution, blank remark ...), and the
problems: a batch without a blank run, missing internal standard areas, a client report that looks like a
draft.

## How close is the program?

**baseline** processes every evaluated run the way the automation does it (the processing method, library
search, ISTD detection, NIAS report), pairs the program's FID peaks with the analyst's rows and writes
`baseline.md` with these scores per run, per batch and overall:

- **client F1**: how far the client report lines agree (named lines need the same CAS; a "Sum of ..." line counts
  for the analyst's lines of that family);
- **name agreement**, **concentration deviation** (relative to the internal standards, so units do not matter)
  and **worksheet agreement** (which peaks are kept and which are removed as blank);
- the disagreements by type: a peak the program did not find, a line it did not report, a line the analyst did
  not report, a different name.

The program results are cached, so a second baseline only scores again. A workbook whose client report is empty
counts as "nothing above the reporting limit" only when no kept peak reaches 10 ppb; otherwise the report was
simply not made in that workbook and its client scores are left out.

## Fitting and proposals

**fit** tries settings or rules on the training data and writes a proposal to `proposals`:

- **detection**: the FID integration settings (minimum area and height, slope sensitivity, S/N, integrator on);
- **background**: the blank ratio below which a peak counts as blank;
- **report**: the reporting limit;
- **naming**: the families (peaks the analysts report as one "Sum of ..." line), when a library hit is trusted
  as a name, and whether unidentified peaks are reported.

Whole batches are held out: a few batches are locked away as a test, the rest is cross-validated. A proposal is
only recommended when it beats the current settings and no batch loses noticeably. The proposal lists the
current and proposed values, the scores, every batch and every candidate. Families are learned from the
analysts' own group labels, never from a list in the program; the proposal shows the program names and
spectrum class hints each family is recognised by, and the analysts' wording of its sum line.

## Approving learned rules

**apply-families** writes the family table of a naming proposal into one processing method, named with
`--method`. It becomes the method's **Learned report rules** part. Use a copy of your method to try it, rerun the
baseline with that method and compare.

With learned rules in the quantification, a NIAS row whose peak belongs to a learned family is given the family's
name as the analysts write it ("Hydrocarbon", "Styrene oligomer"), so the report adds it to the family's sum
line. Names you typed yourself and internal standards are never changed. The peak table's **Learned rule**
column shows the rule, the evidence and the former name of every row it changed.

## Reviewing the disagreements

**review** lists every disagreement between the program and the analysts with its evidence: the analyst's
decision, label, CAS and mg/kg, and the program's name, CAS, match score, the next library hits, the class hint
and the blank match. **Report² > View > Learning review...** opens the list in a window that stays open next to
the program.

- **All types** narrows the list to one kind of disagreement; **Only without a verdict** hides the ones you have
  decided.
- Select a row to see its evidence, type a **Note** if you like, and decide:
  - **Analyst right**: the program should change; the disagreement keeps counting;
  - **Program right** or **Both acceptable**: the disagreement no longer counts in the next baseline and fit.

A verdict is saved at once (`learn/review/verdicts.json`) and stays with the disagreement when the list is made
again.

## How far do the analysts agree?

**consistency** compares the runs two analysts evaluated, peak by peak: whether they kept or removed the same
peaks, gave the same kind of label (named, group, unknown, co-elution, blank remark) and the same CAS. It shows
how far the evaluations themselves agree, the best any learned rule can reach.

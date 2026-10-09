# Learning report rules from human NIAS evaluations — design

Date: 2026-10-09 · Status: draft for review

## 1. Goal

GC Workspace should produce NIAS screening reports that are as good as the ones analysts make by hand in the
`NIAS-Screening-*.xlsm` workbooks. It gets there by learning **general, auditable rules** from a corpus of human
evaluations, not by hard-coding anything for one sample. Every decision in a generated report states the rule and
the evidence behind it.

### What the user said (decisions)

- Corpus: more than 50 human-evaluated workbooks across several NIAS methods. They will be copied to this PC in the
  same layout as the test sample: batch folder → `*.D` → `Auswertung\*.xlsm`.
- Success means both reports match, weighted: the client report (`externerBericht`) is the main score and the full
  worksheet (`Auswertung`) is the secondary score.
- Treating human evaluations as truth: fit to what analysts do consistently. Disagreements go on a review list with
  their evidence, and the user's verdicts become extra labels.
- Each workbook evaluates a **single determination** (run A *or* run B), never both together.
- **Blank subtraction was done by hand**: analysts deleted blank signals from the list. Blank runs (simulant blank,
  ISTD blank) are in the same batch folder and have no workbook.
- The rule families in §5 match how analysts work.

### Assumptions (not stated by the user)

- Client data stays on this PC. The parsed corpus lives outside the git repo. Only code, rule definitions and
  aggregate scores are committed.
- Workbook templates vary somewhat over the years. The parser must handle this and report what it can't read.
- The alkane RI table in each workbook (`Auswertung`, columns n-Alkane / RT / RI) is a reliable retention index
  reference for that run.

### Non-goals

- No black-box model decides report content. ML is only used as a diagnostic.
- No changes to the integration or deconvolution engines as part of this work. Gaps found there are reported as
  separate follow-ups.
- No use of the A/B replicate to make decisions, because analysts did not have it.

## 2. Evidence from the test sample

`Testsample\25011662_GIO_Diary\05_25011675_4891130401_HSL_OPV100m_A.D\Auswertung\…A.xlsm`:

- Sheets: `Rohdaten` (ChemStation TIC integration, about 440 peaks), `kopfdaten`, `Chromatogram`, `Auswertung (2)`
  (the list before clean-up), `Auswertung` (final worksheet, about 130 rows), `externerBericht` (client report),
  `POSH`, `StyOl91`, `StyOl209`, `Berechnungen`, `_old`.
- Header: simulant EtOH 95 %, 40 °C, 10 d, volume 10, S/V ratio 6, injection volume 30, ISTD batch with C17 / BBP
  / DnNP / DBP-d4 areas, GC method PA 26.009, alkane RT → RI table.
- Final row types:
  - named substance with CAS, library (NIST05/NIST17/CCALU_GCMS_1) and % match (90–98)
  - unnamed peak with area and concentration only
  - `IS1..IS4`
  - group label "Styrene Oligomer", summed with a `**` "estimated" note
  - "mehrere Verbindungen" (co-elution)
  - "unknown m/z 145/167/270/91/269/146/165"
  - "possible derivative of an antioxidant (Irganox 1076) m/z …"
- One peak (28.862) has its area split 50/50 between two assignments.
- Rows present in `Auswertung (2)` but missing from `Auswertung` (e.g. 5.132, 6.409) are peaks the analyst removed.
- Client report: named substances, groups and unknowns with mg/dm², mg/kg, SML, references (`[1],[2]`) and a Cramer
  class note (`CC I(a)`).

## 3. Architecture

A new package `gcws/learn/`. Each module has one job.

| Module | Job | Input → output |
|---|---|---|
| `corpus.py` | Crawl the training root without changing anything; find workbooks, their `.D`, the batch's blank runs | root dir → `CorpusIndex` (per run: workbook path, run path, blank runs, template version, problems) |
| `workbook.py` | Parse one workbook, whatever its template version | `.xlsm` → `HumanEvaluation` (header, raw peaks, pre-clean list, final list, client report rows) |
| `runner.py` | Process the run with the normal GC Workspace method in headless mode, as a single determination with the batch blanks | run + method → `ProgramResult` (peaks with evidence, see §4) |
| `matcher.py` | Pair human rows with program peaks | `HumanEvaluation` + `ProgramResult` (+ blank results) → `LabelledPeaks` |
| `dataset.py` | Turn matched runs into one table (one row per peak: evidence + human decision) and store it locally | `LabelledPeaks[]` → dataset file (Parquet/JSON) in the corpus folder |
| `rules/` | Rule families 1–6 (§5). Each is a pure function: evidence + parameters → decision + reason | — |
| `ruleset.py` | Versioned rule set: family parameters, learned lists (families, known background), limits | JSON, stored in the method; one shared default |
| `fit.py` | Fit the parameters of each family with batch-grouped cross-validation | dataset + rule set → proposed rule set + score report |
| `score.py` | Compare a generated report with the human one | → per-run and aggregate scores |
| `review.py` | Collect disagreements, group them by type, store the user's verdicts | → review items; verdicts → label overrides |

Data flow:

```
workbooks + .D + blanks ─► corpus ─► workbook parse ─┐
                                   └► runner ────────┴► matcher ─► dataset ─► fit ─► proposed rule set
                                                                                  │
                         score (held-out) ◄───────────────────────────────────────┘
                         review list ─► user verdicts ─► label overrides ─► next fit
accepted rule set ─► method ─► normal report pipeline (reasons visible in Report² and the report)
```

Applying a rule set happens in the existing report pipeline. Rules add a decision and a reason to each report row.
Report-level checks (`gcws/automation/rules.py`) stay as they are and can use the new reasons.

### Corpus storage

- The training root is configurable (default `C:\GCWS_Training`). Nothing is written next to the raw data.
- The parsed corpus, dataset, fit reports and review verdicts live under the app data dir (`learn/`), outside git.

## 4. Evidence per peak

Computed by the runner from what GC Workspace already has:

- RT, RI (from the workbook's alkane table, falling back to the method's), area, height, S/N, peak width, asymmetry
- area / mean ISTD area, concentration (mg/dm², mg/kg) via the method's quant settings
- library hits: top-k with match, reverse match, library name, library RI and ΔRI, score gap between hit 1 and hit 2
- deconvolution: number of components, purity of the main component, top ions
- blank evidence for each batch blank: present (ΔRI ≤ tol and spectral match ≥ s), blank area, sample/blank ratio
- across the corpus: how often a spectrum at this RI appears (in blanks and in samples)

The A/B replicate is **not** an evidence feature. It may appear in the review list as context only.

## 5. Rule families

They run in order. Each is fitted with the earlier ones fixed. Every affected row gets a reason string naming the rule,
the version and the evidence values.

1. **Background**: remove as blank or background. Uses: blank presence (ΔRI, spectral match), sample/blank area
   ratio, number of blanks containing it, recurrence across the corpus. Parameters: ratio k, match s, blanks n.
   Example reason: `background v3: in blank EtOH_3 (match 92, ΔRI 2), ratio 1.4 < 3`.
2. **Keep / report**: reported vs. listed only. Uses: concentration or area/ISTD, S/N, peak shape, solvent window.
   Parameters: reporting limit, S/N minimum, RT window.
3. **Identification**: named vs. not named. Uses: match, reverse match, ΔRI, score gap, library ranking,
   deconvolution purity. Parameters: minimum match, ΔRI limit, minimum gap, library ranking.
4. **Grouping**: family labels (e.g. "Styrene Oligomer"). Uses: characteristic ion pattern similarity, RI window,
   library family members. Family definitions (ion patterns, RI ranges, display name, footnote) are **learned data
   in the rule set**, not code.
5. **Fallback label**: "mehrere Verbindungen" (≥ 2 components, low purity), "unknown m/z a/b/c…" (clean spectrum,
   no reliable hit; number of ions listed is a parameter), "possible derivative of X" (shared high-mass ions with a
   known additive). Shared peaks: deconvolution split where possible. The equal-split convention is used only if
   the corpus shows analysts use it consistently.
6. **Quant and text**: ISTD, units, SML, references, Cramer class, footnotes, taken from the existing quant method
   and substance catalogue. Not fitted. Where the analyst's value differs from the catalogue, a review item is
   raised (catalogue gap or analyst typo).

**No hard-coding.** Rules work on evidence and never on substance names or retention times. Any name lists
(families, known background) are learned across the whole corpus, stored in the rule set, shown to the user and
approved by the user.

**Per method.** Each NIAS method gets its own fitted rule set if it has enough workbooks (threshold set in
config, default 15 runs). Otherwise it uses the shared default.

## 6. Matching

- Pair human rows to program peaks by RI (tolerance fitted on named substances, starting at ±5), then by RT
  (±0.05 min), and confirm with a spectral check where the workbook names a substance.
- 1:n and n:1 pairs (co-elution, splits, a shared peak) are allowed and recorded as such.
- Removed peaks: present in `Rohdaten`/`Auswertung (2)` but missing from `Auswertung`. Classified as
  *removed-as-blank* (found in a batch blank, §4) or *removed-other*.
- Name equivalence: normalised CAS (no leading zeros), then synonyms and InChIKey from the library, then a
  case-insensitive name.
- Human decision classes: `removed_blank`, `removed_other`, `kept_unreported`, `reported_named`,
  `reported_group`, `reported_unknown`, `reported_coelution`, `istd`.

## 7. Scoring

Per run:

- **Client score (main)**: F1 over reported lines. A line matches on RI ± tol and the same class. Named lines also
  need the same CAS. Group lines need the same family.
- **Concentration deviation**: median relative deviation over matched lines. Reported separately, because the
  integration differs from ChemStation.
- **Worksheet score (secondary)**: agreement on keep / remove-blank / remove-other over all matched peaks.
- **Total** = 0.7 × client + 0.3 × worksheet. The weights can be configured.

Aggregate: mean and spread per method, simulant and family. The **baseline** (current program, no learned rules)
is measured before any fitting.

## 8. Fitting and validation

- Group by batch (Syn-Summary number). Runs from one batch are never split between training and test.
- About 20 % of batches form a locked test set, stratified by method, simulant and year. It is used only to report
  results.
- The rest is fitted with 5-fold cross-validation, each fold leaving out whole batches.
- Families are fitted one after another (1 → 6), each with a bounded grid search inside the parameter limits from
  the rule definition.
- Tie-break: among settings within 0.5 % of the best, prefer the simpler or stricter one.
- Acceptance: the held-out score improves overall and no method gets worse by more than 2 % (configurable).
- Diagnostic: a shallow decision tree per family shows evidence features the current rule does not use. It is
  never applied.
- Output per round: scores per method and family, parameter changes against the current rule set, the largest
  disagreement types, and a proposed rule set. The user accepts or rejects it. **Nothing is applied automatically.**

## 9. Review loop

- After each fit, disagreements are grouped by type (e.g. "program named, analyst unknown", "analyst removed,
  not in any blank"), each with its evidence: spectrum vs. library, match, ΔRI, blank trace, the replicate as
  context.
- The user gives a verdict on each case: analyst right / program right / both acceptable. Verdicts are stored
  and change the labels for the next fit.
- Analyst-consistency report: similar evidence with opposite human decisions in different workbooks.

## 10. Error handling

- Unreadable workbook (unknown template, missing sheets, `#DIV/0!` headers): skipped and listed with the reason.
- Missing `.D` or no batch blank: the run stays in the dataset. Without blanks it is left out of family 1.
- A runner failure on a run is recorded and the corpus continues.
- All parse and run problems appear in a corpus-check report before any fitting.

## 11. Testing

- Parser: unit tests on the test-sample workbook (header values, row classes, removed rows, client rows).
- Matcher: synthetic cases (co-elution, shared peak, missing peak, blank peak, RI drift).
- Each rule family: hand-made evidence → expected decision and reason.
- Scorer: known pairs → known F1.
- End-to-end: the test-sample batch through parse → run → match → score. This needs `GCWS_SAMPLES`/the training
  root and is skipped when it is missing.
- No test opens a dialog or window.

## 12. Delivery phases

One commit per phase, tests green.

1. Workbook parser + corpus crawler + corpus-check report
2. Runner, matcher, scorer → **baseline score** on the corpus
3. Families 1–2 (background, keep/report) + fitting framework
4. Families 3–5 (identification, grouping, fallback labels)
5. Review list in Report² (verdicts stored as labels)
6. Rule sets stored per method, applied in the normal report pipeline, reasons shown in the report

## 13. Open points

- The exact weighting and tolerances are set after the baseline (phase 2).
- Whether the equal-split convention for shared peaks becomes a rule depends on the corpus statistics.
- Template versions: the list is known only after the corpus crawl.

# Learning report rules from human evaluations — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 1 of the spec. Parse every human NIAS workbook into a structured, labelled record and produce a
corpus-check report. Phases 2–6 are listed at the end and get their own detailed plan when they start, because
their design depends on what phase 1 and the phase 2 baseline show.

**Architecture:** A new package `gcws/learn/` with pure-Python, headless modules:
- `model.py`: dataclasses
- `workbook.py`: one workbook → `HumanEvaluation`
- `corpus.py`: training root → `CorpusEntry` list with batch blanks
- `check.py` + `__main__.py`: write the JSON corpus and a Markdown corpus-check report under `paths.DATA/learn`

**Tech stack:** Python 3.12+, openpyxl (already in the venv), pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-learn-from-evaluations-design.md`

## Global constraints

- Client data stays local. Nothing parsed from workbooks is written inside the repo. The default output is
  `paths.DATA / "learn"`. Tests use synthetic workbooks built in `tmp_path`. Real-data tests read
  `GCWS_LEARN_ROOT` (default `<repo parent>/Testsample`) and skip when it is missing.
- Read-only on the training root: never write next to workbooks or `.D` folders.
- No UI, no dialogs, no Qt import in `gcws/learn` (headless, tests on the user's desktop).
- No decision rules in phase 1. The parser only records what the analyst did. Label vocabulary such as "unknown"
  or "mehrere Verbindungen" belongs to parsing, not to rules.
- Facts found in the test data (they shape the parser):
  - **Quantification is on FID.** `Rohdaten` has two INI-style sections, `[INT TIC: …data.ms]` and
    `[INT …FID1A.ch]`. `Auswertung` rows match the FID section by RT and area. Peak type containing `M` means the
    analyst integrated it by hand.
  - **Area corrections are formulas** in the `Auswertung` area column (e.g. `F29 = L29-F30`). Column L can hold
    the original area.
  - **Two template variants:** v2 has an n-Alkane RT/RI block (`M4 = "n-Alkane"`), v1 has none. The table header
    row is found by `A = "RT / min"` and `B = "Name"`.
  - **Concentration columns** are named `Conc. <unit>` (mg/dm², mg/kg, µg/L, µg/Zipper, …). SML and Reference
    columns are optional.
  - **`Auswertung (2)`** (the list before clean-up) only exists in some workbooks.
  - **Some runs have two workbooks** from different analysts (`_BDa_` / `_BlM_` in the file name). One workbook
    evaluates a blank run.
  - **`AcqData/**/rptdef.xls`** are ChemStation method files, not evaluations.

## Review focus

1. A workbook with `#DIV/0!` or empty header cells (the `StyOl*` sheets show this). The parser records `None` and a
   problem, and never raises.
2. An open Excel lock file `~$NIAS-….xlsm` in `Auswertung`: skipped by the crawler.
3. A workbook that fails to open (corrupt, password, unknown template): `check` lists it under problems and goes on
   with the rest.
4. Two workbooks for one run: both are kept as separate evaluations and listed as an analyst pair.
5. A batch without a blank run: `CorpusEntry.problems` says so, and the entry is still in the corpus.

Each has a test in the task that owns it (Tasks 3, 4, 5).

---

### Task 1: Data model and `Rohdaten` parsing

**Files:**
- Create: `gcws/learn/__init__.py` (docstring only), `gcws/learn/model.py`, `gcws/learn/workbook.py`
- Test: `tests/test_learn_workbook.py`, `tests/learn_fixtures.py` (synthetic workbook builder)

**Interfaces (produces):**
```python
# model.py
@dataclass
class RawPeak:   signal: str; number: int; rt: float; area: float; height: float | None
                 start: float | None; end: float | None; peak_type: str; manual: bool
@dataclass
class AlkanePoint: name: str; rt: float | None; ri: float
@dataclass
class IstdEntry:  name: str; conc: float | None; area: float | None
@dataclass
class Header:     sample_name: str = ""; syn_id: str = ""; syn_summary: str = ""; evaluator: str = ""
                  operator: str = ""; data_file: str = ""; simulant: str = ""; temperature: float | None = None
                  duration: str = ""; volume: float | None = None; sv_ratio: float | None = None
                  gc_method: str = ""; inj_volume: float | None = None; istd: list[IstdEntry]
                  istd_mean_area: float | None = None; alkanes: list[AlkanePoint]; conc_units: list[str]
@dataclass
class EvalRow:    sheet_row: int; rt: float | None; label: str; cas: str; library: str; match: float | None
                  area: float | None; area_formula: str; area_original: float | None
                  conc: dict[str, float | None]; sml: str; reference: str; note: str; row_class: str
@dataclass
class ReportRow:  rt: float | None; label: str; cas: str; library: str; match: float | None
                  conc: dict[str, float | None]; sml: str; reference: str; row_class: str
@dataclass
class HumanEvaluation: path: str; template: str; header: Header; raw: list[RawPeak]
                  pre_clean: list[EvalRow] | None; final: list[EvalRow]; report: list[ReportRow]
                  footnotes: list[str]; problems: list[str]
def normalise_cas(text) -> str     # "000097-88-1" -> "97-88-1"; "" for empty or "-"
# workbook.py
def parse_rohdaten(rows: list[tuple]) -> list[RawPeak]
```

- [ ] **Step 1: Write the fixture builder and failing tests.**
  `tests/learn_fixtures.py::make_workbook(path, *, template="v2", pre_clean=True, final_rows=None, report_rows=None)`
  writes an `.xlsx` with sheets `Rohdaten`, `Auswertung`, optionally `Auswertung (2)`, and `externerBericht`. The
  cell layout copies the real one: the header labels at the coordinates listed in Task 2, the table header at row 27,
  `Rohdaten` as `key=` / value rows. Tests:
  - `test_parse_rohdaten_sections`: TIC + FID sections. FID row `['40=', 40, 7.07, 6.938, 7.313, '  M ', 3238076, 119966715, 0.23, 0.062]`
    → `RawPeak(signal="FID", number=40, rt=7.07, start=6.938, end=7.313, peak_type="M", height=3238076, area=119966715, manual=True)`.
    TIC row `['2=', 2, 8.203, 244, 256, 272, 'VV  ', 2697716, 40534814, 13.57, 2.301]` → signal "TIC", start/end
    `None` (scan numbers are not times), `manual=False`.
  - `test_normalise_cas`: `"000097-88-1"→"97-88-1"`, `"-"→""`, `None→""`.
- [ ] **Step 2: Run** `.venv/Scripts/python.exe -m pytest tests/test_learn_workbook.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `model.py` and `parse_rohdaten`. Section names come from `[INT TIC…]` → "TIC" and
  `[INT …FID…]` → "FID", otherwise the text inside the brackets. Column meaning is taken from the section's
  `Header=` row, so the TIC (`First/Max/Last` scans) and FID (`Start/End` minutes) layouts both work. `manual`
  is true when `"M"` is among the peak type letters.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: data model and Rohdaten (TIC/FID) parsing`.

### Task 2: `Auswertung` header and table parsing

**Files:** Modify `gcws/learn/workbook.py`. Test `tests/test_learn_workbook.py`.

**Interfaces (produces):**
```python
def parse_header(cells: dict[str, object]) -> Header          # cells: coordinate -> value (data_only)
def parse_eval_table(values: dict[str, object], formulas: dict[str, object]) -> list[EvalRow]
def classify_rows(rows: list[EvalRow]) -> None                # sets row_class in place
ROW_CLASSES = ("istd", "named", "named_no_cas", "group", "unknown", "coelution", "derivative", "unnamed", "sum")
```

- [ ] **Step 1: Failing tests.**
  - `test_header_v2`: the synthetic v2 workbook gives `simulant="EtOH 95%"`, `temperature=40`, `duration="10 d"`,
    `volume=10`, `sv_ratio=6`, `gc_method="PA 26.009"`, `inj_volume=30`, `syn_summary="25011662"`, `evaluator="BlM"`,
    ISTD names `["C17","BBP","DnNP","DBP-d4"]` with areas `[9871653, 7095674, 7904784, 742832]`,
    `istd_mean_area≈8290703.67`, 21 alkanes with `C10 → (7.331, 1000)`, `conc_units == ["mg/dm²","mg/kg"]`.
  - `test_header_v1_no_alkanes`: `alkanes == []`, template `"v1"`.
  - `test_header_div0`: `#DIV/0!` in the ISTD area cell gives `area=None` and no exception (Review focus 1).
  - `test_eval_table`:
    - Row `7.07 | Butyl methacrylate | 000097-88-1 | NIST05 | 90 | =L29-F30 | … | L29=119966715` →
      `cas="97-88-1"`, `area_formula="=L29-F30"`, `area_original=119966715`, `row_class="named"`.
    - Rows `IS1`…`IS4` → `"istd"`.
    - Label `"Styrene Oligomer"` on ≥2 rows with no CAS → `"group"`. A single no-CAS name →
      `"named_no_cas"`.
    - `"unknown m/z 145/167"` → `"unknown"`. `"mehrere Verbindungen"` → `"coelution"`.
      `"possible derivative of …"` → `"derivative"`. No label → `"unnamed"`.
    - Text in column A starting with `"Sum"` → `"sum"`. Parsing stops at `"Ende"`.
    - `conc == {"mg/dm²": …, "mg/kg": …}`. A µg/L workbook with three `Conc.` columns and no SML gives three
      keys and `sml=""`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Header values are found **by label** (`"Simulans:"`, `"Temperatur:"`, `"Dauer:"`, `"Volumen:"`, `"O/V-Ratio"`,
    `"GC-Methode:"`, `"Inj-Vol:"`, `"Syn-Summary"`, `"Syn-Proben-ID"`, `"Auswerter:"`, `"GC-Operator:"`,
    `"Datenfile"`, `"Probenname"`): the value is the next non-empty cell to the right in the same row.
  - ISTD rows: column D name, E conc, F area, plus the H/I/J block for a fourth ISTD, in rows 22–24.
  - Alkanes: column M `C<n>` rows below the `n-Alkane` header, with N = RT and O = RI. Repeated RTs past the last
    measured alkane (formula fill-down) are kept, with `rt` as given.
  - Table columns are found by header text in the `RT / min` row: `Name`, `CAS`, `DB`, `%match`, `Fläche`,
    `Conc. *`, `SML`, `Reference`. Column L of a row holds `area_original` when it is numeric and the area cell is a
    formula.
  - Classification regexes (case-insensitive): `^IS\d+$`, `^unknown\b`, `mehrere Verbindungen|several compounds|co-?elut`,
    `possible derivative|mögliches? Derivat`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: Auswertung header and table parsing with row classes`.

### Task 3: Full workbook parse, client report, removed peaks

**Files:** Modify `gcws/learn/workbook.py`. Test `tests/test_learn_workbook.py`, `tests/test_learn_real.py`.

**Interfaces (produces):**
```python
def parse_report(values: dict[str, object]) -> tuple[list[ReportRow], list[str]]   # rows, footnotes
def parse_workbook(path: Path) -> HumanEvaluation      # never raises on content; IO errors -> problems
def removed_peaks(ev: HumanEvaluation, rt_tol: float = 0.01) -> list[RawPeak]     # FID peaks with no final row
```

- [ ] **Step 1: Failing tests.**
  - `test_parse_report`:
    - The header row (`RT | Name | CAS-No. | %\nmatch | Migration concentration | SML | Ref`) plus the unit row
      (`min | mg/dm² | mg/kg | mg/kg`) give `conc` keys `["mg/dm²","mg/kg"]`.
    - `I = "CC I(a)"` → `sml="CC I(a)"`.
    - Lowercase headers (`name`, `migration conc.`, `ref.`) are accepted.
    - Text rows after the table become `footnotes`.
  - `test_parse_workbook_synthetic`: template, a pre-clean list present or `None`, final, report, no problems.
  - `test_parse_workbook_bad_file`: a text file renamed `.xlsm` gives `problems == ["cannot open: …"]` and empty
    lists (Review focus 3).
  - `test_removed_peaks`: the FID peaks at 5.132 and 6.409 with no final row are returned. 7.07 is not.
  - `tests/test_learn_real.py::test_gio_workbook` (skips without data), on the `25011662_GIO_Diary` workbook:
    - `template == "v2"`, the pre-clean list is not `None`
    - BMA at 7.07: area 118978514, original 119966715
    - 4 `istd` rows, ≥ 30 `group` rows labelled `Styrene Oligomer`, 1 `sum` row
    - the FID raw peak at 7.07 has `manual=True`
    - the report contains `"97-88-1"` and no unnamed rows
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** Load with openpyxl twice (`data_only=True` for values, `data_only=False` for
  formulas), `read_only=True`, `keep_vba=False`. Build `{coordinate: value}` dicts per sheet with
  `ws.iter_rows()`. Close both workbooks.
- [ ] **Step 4: Run** `.venv/Scripts/python.exe -m pytest tests/test_learn_workbook.py tests/test_learn_real.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `Learn: full workbook parse, client report rows, removed FID peaks`.

### Task 4: Corpus crawler

**Files:** Create `gcws/learn/corpus.py`. Test `tests/test_learn_corpus.py`.

**Interfaces (produces):**
```python
@dataclass
class CorpusEntry: workbook: str; run_dir: str; batch_dir: str; analyst: str; run_role: str
                   blanks: list[str]; problems: list[str]
def find_workbooks(root: Path) -> list[Path]    # **/*.D/Auswertung/*.xls[xm]; skips "~$*" and AcqData
def scan(root: Path) -> list[CorpusEntry]
def analyst_from_name(name: str) -> str         # "NIAS-Screening-SYN25011889_BlM_ 05_….xlsm" -> "BlM"
```

- [ ] **Step 1: Failing tests** (folders made in `tmp_path`):
  - `test_find_workbooks`:
    - Finds `B/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsm`.
    - Skips `~$NIAS….xlsm` (Review focus 2) and `B/05_X_A.D/AcqData/M.M/rptdef.xls`.
  - `test_scan_blanks`: a batch with `05_X_A.D`, `10_EtOH.D`, `12_Blank_EtOAc.D`, `03_EtOH_ISTD.D` →
    `blanks == ["03_EtOH_ISTD.D","10_EtOH.D","12_Blank_EtOAc.D"]` (sorted names, roles from
    `gcws.io.sequence.classify_role` in `{BLANK, BLANK_ISTD}`), `run_role == "sample"`.
  - `test_scan_no_blank`: `problems == ["no blank run in batch"]` (Review focus 5).
  - `test_two_analysts`: two workbooks in one `Auswertung` give two entries with analysts `BDa` and `BlM`
    (Review focus 4).
  - `test_blank_evaluated`: a workbook in `05_Blank_EtOH.D` gives `run_role == "blank"` and problem
    `"evaluated run is a blank"`.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** `run_dir` is the `.D` that contains `Auswertung`. `batch_dir` is its parent. Blanks
  are the sibling `*.D` folders. `run_role` uses `classify_role(run_dir.name)`.
- [ ] **Step 4: Run** the tests. Expected: PASS. `tests/test_learn_real.py::test_scan_testsample` (skips without
  data): ≥ 31 entries under `Testsample/2025`, no `rptdef` paths, the coffee-capsule batch blank list is not empty.
- [ ] **Step 5: Commit** `Learn: corpus crawler with batch blanks and analyst pairs`.

### Task 5: Corpus check (JSON corpus + report) and CLI

**Files:** Create `gcws/learn/check.py`, `gcws/learn/__main__.py`. Test `tests/test_learn_check.py`.

**Interfaces (produces):**
```python
def entry_id(entry: CorpusEntry, root: Path) -> str     # stable, filesystem-safe id from the relative workbook path
def run_check(root: Path, out_dir: Path) -> dict        # writes out_dir/corpus/<id>.json + out_dir/corpus_check.md; returns summary
# CLI: python -m gcws.learn check <root> [--out DIR]    (default DIR = paths.DATA / "learn")
```
The summary dict keys: `workbooks`, `parsed`, `failed`, `templates` (count per template), `row_classes`
(count per class over final rows), `reported_rows`, `manual_integrations`, `area_formulas`, `removed_peaks`,
`analyst_pairs` (list of run dirs with ≥ 2 workbooks), `no_blank` (list), `problems` (`{id: [..]}`).

- [ ] **Step 1: Failing tests.**
  - `test_run_check_synthetic`: two synthetic workbooks plus one corrupt one → `parsed == 2`, `failed == 1`.
    `corpus/*.json` round-trips to `HumanEvaluation` fields plus the `CorpusEntry`. `corpus_check.md` contains the
    headings `## Summary`, `## Problems`, `## Analyst pairs`.
  - `test_cli`: `main(["check", str(root), "--out", str(out)])` returns 0 and writes the files.
  - `test_nothing_written_to_root`: the set of files under the root is unchanged after a run.
- [ ] **Step 2: Run** the tests. Expected: FAIL.
- [ ] **Step 3: Implement.** Per workbook: `parse_workbook` + `removed_peaks`. A failure on one workbook goes into
  `problems` and the run continues. JSON via `dataclasses.asdict`, UTF-8, `ensure_ascii=False`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Run the check on the real test data.**
  `.venv/Scripts/python.exe -m gcws.learn check "C:\Users\dbudi\Desktop\KI Projects\Testsample"`, then read
  `data/learn/corpus_check.md`. Expected: every workbook parsed or a clear problem stated for it. Fix parser gaps
  it exposes (each with a synthetic test) before committing.
- [ ] **Step 6: Commit** `Learn: corpus check (JSON corpus, Markdown report, CLI)`.

---

## Later phases (own plan each, written when the phase starts)

2. **Runner, matcher, scorer and baseline.** Headless GC Workspace processing of each evaluated run as a single
   determination with its batch blanks. **Quantification on FID**, as the analysts did. Program peaks are matched
   to `final`/`removed` rows (RI from the workbook alkanes, then RT, then spectra). Baseline client and worksheet
   scores per run.
3. **Families 1–2** (background, keep/report) and the batch-grouped cross-validation fit framework.
4. **Families 3–5** (identification, grouping, fallback labels). This includes learning the area corrections
   analysts made as formulas (tail/front components, cf. hidden-components work).
5. **Review list in Report²**, with verdicts stored as labels. The analyst-pair workbooks feed the analyst-consistency
   report.
6. **Rule sets per method**, applied in the report pipeline, with reasons shown.

Note for phase 2+: the test data holds about 32 workbooks, below the spec's 50+. Phase 1–2 work as planned. Fits
in phase 3+ must report their uncertainty, and the per-method split will fall back to the shared default until
more workbooks are added.

# 03: Single determination only checkbox: A reported, B as control

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** A **Single determination only** checkbox in the Quantification panel, saved with the
method's quant settings (missing = off). When it is ticked, both runs of a pair are processed (integration,
identification, ISTDs, blanks, quantification) and compared. Only the **reported determination** goes into
the report: A (`_A`/`_1`, otherwise the first by injection). The **control determination** stays in the
project and the audit trail but not in the report. The report is labelled single determination. In an SD
pair, red A/B differences ("Double determination: substances to decide") are info by default, and the rule
can be set to control. Toggling the checkbox changes what an accepted group means, so it becomes stale
("Accept again").

**Blocked by:** 02

**Status:** resolved

- [x] The checkbox is in the Quantification panel, saved and loaded with the processing method
- [x] Automation job (S1), flag on, pair A/B: B is processed, the report has A's values only, labelled single determination
- [x] Report² findings for that job: SD info line; "substances to decide" at info level; can be set to control
- [x] Flag off: pairs behave exactly as before
- [x] Group operations (S3): toggling the flag makes an accepted group stale
- [x] The manual (F1) documents the checkbox and reported / control determination; the coverage test passes

## Comments

- 2026-10-11: `quant["single_only"]` (in `QUANT_KEYS`, missing = off); `determination.of` makes a pair with
  the flag a single determination: reported A (`_A`/`_1`, else first by injection), control B; a lone
  suffixed run with the flag is SD too (the B-only note "A missing, B reported" is left for 04). Reports
  needed no change (`assemble.prepare` already uses `reported`). Rule `feature_review` gets param
  `single_control` (Report² rules dialog: "Control needed also in a single determination"); the SD info line
  names the control. Toggling goes through `Workspace.set_single_only` (one undo macro: quant + groups);
  `double_determination.method_changed` stales accepted groups whose kind/reported/control changed. N-fold
  groups (3+) are unaffected. Tests: `test_single_determination_only_job` (S1), `..._rules`,
  `test_single_determination_only_reports_a_and_keeps_b_as_control`, `test_toggling_..._stales_an_accepted_pair`
  (S3), `test_single_determination_only_checkbox` (panel, undo, method round trip).

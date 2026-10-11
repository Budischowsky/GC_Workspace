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

**Status:** ready-for-agent

- [ ] The checkbox is in the Quantification panel, saved and loaded with the processing method
- [ ] Automation job (S1), flag on, pair A/B: B is processed, the report has A's values only, labelled single determination
- [ ] Report² findings for that job: SD info line; "substances to decide" at info level; can be set to control
- [ ] Flag off: pairs behave exactly as before
- [ ] Group operations (S3): toggling the flag makes an accepted group stale
- [ ] The manual (F1) documents the checkbox and reported / control determination; the coverage test passes

# 02: A run without a suffix is a single determination

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** A sample run without a replicate suffix (`_A`/`_B`, `_1`/`_2`, `repN`) is a single
determination. This holds both in automation and in GC Workspace. Its report keeps the double
determination layout and header, with one value column, labelled *single determination* (the
`{determination}` template field too). The batch report lists single and double determinations together.
Report² shows an info line "single determination", and "Double determination incomplete" does not fire.
A lone suffixed run whose partner never came stays an incomplete double determination that needs control.

**Blocked by:** 01

**Status:** resolved

- [x] Automation job (seam S1): a lone run without a suffix gives a report labelled single determination in the double determination layout with one value column
- [x] Its Report² findings: an info line, no "Double determination incomplete"
- [x] A lone `_A` (partner missing) still gets "Double determination incomplete" at control level
- [x] A batch report with one single and one double determination lists both
- [x] Template report: `{determination}` reads "single determination"
- [x] The manual (F1) explains single vs incomplete double determination; the manual coverage test passes

## Comments

- 2026-10-11: rule "Double determination incomplete" fires only for an incomplete double determination; a
  single determination gets an info finding "single determination". The classic NIAS report written from one
  run was labelled "double determination" (only an Einzelbestimmung workbook got "single"): GCWS-PATCH in
  `NIAS Reporting v27.py` labels a Doppelbestimmung sheet from one determination "single determination".
  Manual: wf-double.md "Single determination", rule table in wf-automation.md. Test:
  `test_a_run_without_a_suffix_is_a_single_determination` (S1: NIAS + template label, info line, lone `_A`
  control, batch report with SD + DD). VENDORED.md hashes were already stale before; not regenerated.

# 02: A run without a suffix is a single determination

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** A sample run without a replicate suffix (`_A`/`_B`, `_1`/`_2`, `repN`) is a single
determination. This holds both in automation and in GC Workspace. Its report keeps the double
determination layout and header, with one value column, labelled *single determination* (the
`{determination}` template field too). The batch report lists single and double determinations together.
Report² shows an info line "single determination", and "Double determination incomplete" does not fire.
A lone suffixed run whose partner never came stays an incomplete double determination that needs control.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Automation job (seam S1): a lone run without a suffix gives a report labelled single determination in the double determination layout with one value column
- [ ] Its Report² findings: an info line, no "Double determination incomplete"
- [ ] A lone `_A` (partner missing) still gets "Double determination incomplete" at control level
- [ ] A batch report with one single and one double determination lists both
- [ ] Template report: `{determination}` reads "single determination"
- [ ] The manual (F1) explains single vs incomplete double determination; the manual coverage test passes

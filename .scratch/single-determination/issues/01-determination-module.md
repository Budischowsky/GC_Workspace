# 01: Prefactor: one place answers "which determination is this"

Spec: `docs/superpowers/specs/2026-10-11-single-determination-design.md` · terms: `CONTEXT.md`

**What to build:** A small quant module, in the style of the quant mode module. Given the quant settings and
a replicate group, it answers three things: the kind (single determination, double / N-fold determination,
incomplete double determination), the reported member(s) and the control member (if any). The automation
job, the rules evidence, report assembly (classic, template, batch) and the Replicates panel ask it instead
of counting members themselves. Nothing changes for the analyst: today a lone run is still treated as it is
now (pairing "single", flagged by "Double determination incomplete").

**Blocked by:** None (can start immediately)

**Status:** resolved

- [x] The module answers kind, reported and control members for: a pair, a lone run without a suffix, a lone suffixed run, an N-fold group
- [x] Pipeline, rules evidence, report assembly and the Replicates panel use it; no member-count checks remain at those places
- [x] No behaviour change: the full test suite stays green, and reports and Report² findings are unchanged

## Comments

- 2026-10-11: `gcws/quant/determination.py` (`of`, `of_workspace`, `suffix`; kinds single / double /
  incomplete; reported, control, note, label). Used by the automation job (pairing and the features step),
  its evidence (`determination`, `reported`, `control` in `double_determination`), the rules (old evidence:
  pairing "single" = incomplete), `report.assemble` (reported members, feature table), the template header
  `{determination}` and the DD page (compare banner, Accept). Full suite: 1245 passed; 6 failures are
  pre-existing (3 fail on clean HEAD, 3 order-dependent, pass alone).

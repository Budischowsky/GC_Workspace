"""Double determination as features (mzmine-style replicate processing).

The determinations of one sample (A/B, or more) are processed as one data set
instead of as independent result lists:

1. a retention-time map between the runs (``timemap``);
2. peaks of the determinations paired by retention time *and* EI spectrum
   (``align``, the mzmine GC aligner's score with the NIST GC composite cosine
   of ``similarity``) into features with stable ids (F-001, ...);
3. a peak found in one determination only is searched for again in the other
   (``gapfill``, mzmine's gap filler checked by the co-eluting ions);
4. one identification per feature from both determinations and their
   consensus spectrum (``consensus``);
5. a traffic light per feature (``triage``) so only the exceptions are
   reviewed; ``combine`` turns the table into AutoLib's merged rows for the
   reports.

Everything here is headless (no Qt); ``service`` connects it to the workspace.

Several algorithms are ported from mzmine (https://github.com/mzmine/mzmine,
commit ea6ee5e of 2026-09-28), MIT License, Copyright (c) 2004-2025 The mzmine
Development Team; see VENDORED.md for the notice. Each ported function names
its mzmine source.
"""

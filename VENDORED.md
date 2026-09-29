# Vendored modules

Check drift with `python tools/check_vendor.py`.

## NIAS modules

Copied from `NIAS Working` so GC Workspace runs on its own. Only files marked *patched* were
changed; every change carries a `GCWS-PATCH` comment.

| File | SHA-256 (16) | Kind |
|---|---|---|
| AutoLib\Geänderten Python-Code herunterladen.py | `d81e2019034309bc` | verbatim |
| ei_atlas_config.json | `b7014ec12007593b` | verbatim |
| extract_ms_spectra.py | `5996e48afe790d4d` | verbatim |
| gc_atlas.py | `498f4ea38f9f0386` | verbatim |
| gc_atlas_store.py | `885a4724cc308115` | verbatim |
| gc_ch.py | `5466f55d98cf669c` | verbatim |
| gc_deconv.py | `c77273bb021f38cb` | verbatim |
| gc_duplicate.py | `868f3f1cffdaa675` | patched |
| gc_export.py | `f0e6108764e21a7e` | patched |
| gc_fid.py | `4b8cf2373da5b4d6` | verbatim |
| gc_identify.py | `31fae57be1559a02` | verbatim |
| gc_integrate.py | `d999ca059735338f` | verbatim |
| gc_load.py | `84b0a6b2df58396c` | verbatim |
| gc_model.py | `a2c76853c494094e` | verbatim |
| gc_nist.py | `b16a6084de99b076` | verbatim |
| gc_qc.py | `dcb4558a9ade0b98` | verbatim |
| gc_register.py | `0b700587e1dd232c` | verbatim |
| gc_register_ui.py | `860b6fbe2be9b486` | verbatim |
| gc_report_layout.py | `8e80cf2db8719d5e` | verbatim |
| gc_search_method.py | `d41e55687477fade` | verbatim |
| gc_seen.py | `08c7ce0d7f6b3406` | verbatim |
| NIAS Reporting v27.py | `66c5c58c1355fb9d` | patched |
| nias_paths.py | `b0d12c4f12a9a4d0` | patched |

## SpectrAtlas search engine

SpectrAtlas's library readers (Agilent .L, NIST MS Search, Wiley/Shimadzu .lib, MSP), its search index
and scoring (PBM, NIST-style similarity), copied unchanged from `UnknownEvaluation` (working tree of
2026-09-26) into `gcws/libsearch/vendor`. `gcws/libsearch/service.py` subclasses `Engine` to load an
explicit list of libraries (Identify > Libraries...), so SpectrAtlas itself is not needed for searching.

| File | SHA-256 (16) | Kind |
|---|---|---|
| agilent.py | `6cda17b80c6545f1` | verbatim |
| chemistry.py | `562af4c5c09026a9` | verbatim |
| engine.py | `b1a8858438a86037` | verbatim |
| msp.py | `2dcfb295a329e8fc` | verbatim |
| msp_cache.py | `d32d40669291bec9` | verbatim |
| nist.py | `61b880ae81d4690c` | verbatim |
| pbm.py | `13ece6caa9cb8b49` | verbatim |
| search_options.py | `024aca9994dd92a3` | verbatim |
| shimadzu.py | `7a0c0d27152b9527` | verbatim |
| spectral_index.py | `0ab02093485b9edb` | verbatim |

## Ported algorithms (mzmine)

`gcws/features` re-implements in Python algorithms of mzmine (https://github.com/mzmine/mzmine,
commit ea6ee5e of 2026-09-28): the GC aligner's row score (`align_gc/GcRowAlignScorer`,
`align_join/RowVsRowScore`), the multi-list aligner (`align_common/BaseFeatureListAligner`), the
consensus quantifier ion (`align_gc/GCConsensusAlignerPostProcessor`), the gap filler
(`gapfill_peakfinder/Gap`), the spectral similarities (`util/scans/similarity`: `Weights`,
weighted and composite cosine) and the annotation RI score (`AnnotationSummary`). Each ported
function names its source. ADAP (dulab) and mzmine 2 (GPL) code is not used.

The MIT License (MIT)

Copyright (c) 2004-2025 The mzmine Development Team

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

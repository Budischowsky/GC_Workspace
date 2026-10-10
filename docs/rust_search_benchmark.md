# Library search in Rust: variants and measured speed (2026-10-10)

Branch `test/rust-libsearch`. Crate `rust/gcws_rust` (PyO3 0.27, rayon), glue
`gcws/libsearch/rust_search.py`, hook at the end of `gcws/libsearch/fast.py`, build with
`tools/build_rust.ps1`. `GCWS_RUST_SEARCH` picks the parts (default `tiled2+score` when the
extension is installed, `off` = Python).

## Accuracy

Every variant gives the Python fast search's results bit for bit: the same hits, scores, qualities,
coverage and order for every spectrum (results compared as `repr` of the full result dicts).

| Input | Spectra | Variants checked |
|---|---|---|
| Job j228fcff3 (26016606 GIOSUN 130 m/min), sequential PBM | 131 + 10 consensus | all |
| Same input, combined mode | 141 | tiled2+score |
| Same input, similarity (NIST-style) algorithm | 141 | tiled2+score |
| Job j02d23ae0 (26016607 GIOSUN 170 m/min) | 122 + 9 | tiled2+score |
| Job j50ad79d2 (25011675 HSL OPV, other batch) | 295 + 14 | score, sparse, tiled, tiled2 |

No screening bound is involved: stage 1 computes the standard engine's float32 terms and float64
sums (in ascending m/z) for every reference, so the candidate lists equal the standard ones by
construction. Stage 2 repeats `_decode_many`, `_reference_sides` and `_pbm_many` operation by
operation; `log2` is taken from Python's `math.log2` (a table per peak) because C runtimes differ.

## Variants

| Variant | Stage | Idea |
|---|---|---|
| `score` | 2 | Agilent/Shimadzu/NIST records decoded in Rust, decoded spectra + PBM sides cached in Rust, PBM of all candidates in one call; dicts only for the head of the ranked list |
| `sparse` | 1 | exact inverted-index prefilter, one peak per thread, accumulators over a whole library |
| `tiled` | 1 | peaks in chunks walk the index together, reference tile by tile (4096 x 16) |
| `tiled2` | 1 | tiles sized for the L2 cache (192 references x 64 peaks), dot product and reverse norm in one cell, rows padded against cache-set aliasing, no bounds checks inside |

## Speed (search only, recorded job inputs; AMD Ryzen 5 4500U, 6 cores; best of runs)

Job j228fcff3 (141 spectra, 13 libraries, 1.82 M references):

| Variant | Total | Stage 1 (screen) | Stage 2 (score) | vs Python |
|---|---|---|---|---|
| Python (main) | 25.7 s | 14.8 s | 9.9 s | 1.0x |
| score | 14.6 s | 12.5 s | 1.4 s | 1.8x |
| sparse | 22.0 s | 10.0 s | 11.1 s | 1.2x |
| tiled (4096 x 16) | 19.3 s | 10.1 s | 8.4 s | 1.3x |
| tiled2 | 11.2 s | 2.7 s | 7.8 s | 2.3x |
| sparse+score | 11.4 s | 9.3 s | 1.3 s | 2.3x |
| **tiled2+score** | **4.2 s** | **2.2 s** | **1.3 s** | **6.2x** |

Other inputs (one run each): j02d23ae0 28.8 s -> 7.2 s (4.0x); j50ad79d2 67.2 s -> 11.6 s (5.8x;
score alone 26.1 s, sparse+score 39.0 s, tiled+score 12.0 s); similarity algorithm 13.7 s -> 5.0 s;
combined mode 22.9 s -> 6.6 s.

Whole automation job j228fcff3 through the branch code (`child.process`, the job's own timings):
search 27.3 s -> 8.7 s, total 47.0 s -> 32.7 s. The NIAS report workbook is identical cell for cell
(except the audit timestamp and paths); `result.json` differs only in its timings.

Tests on the branch tree with the extension: tests/test_rust_search.py (24, including decoding and
PBM against the installed Agilent, Shimadzu and NIST libraries), and through the Rust kernels
tests/test_fast_search.py, test_libsearch*.py, test_fast_compare_real.py (58): all pass.

## Normal search (`standard`, Engine.analyze peak by peak)

The vendored engine's `_prefilter_shard` (Rust `prefilter_dense`: the same float32 terms, float64
sums in the query's ion order, the same divisions) and `_score` (candidates chosen by the same
numpy code, decoded and PBM-scored in Rust) are replaced; results are identical:

| Recorded peaks | Python | Rust | |
|---|---|---|---|
| 20, sequential PBM, 13 libraries | 21.5 s | 3.8 s | 5.7x |
| 10, combined PBM | 5.3 s | 1.8 s | 3.0x |

Active app (2026-10-10, extension installed in .venv): whole job j228fcff3 19 s (search 4.3 s),
before 32 s.

Stage 1 micro benchmark (NIST17, 306 k references, 131 peaks, 1.26 G postings): sparse 3.3 s;
tiled2 with tiles of 128 / 256 references and power-of-two row stride 1.4 / 3.7 s (cache-set
aliasing); with padded rows 0.58-0.67 s for every tile size.

Timings were taken while another session ran a UI benchmark on the same machine; single runs
varied by up to 2x, the table shows the best of three.

## Notes

* Built with the stable `x86_64-pc-windows-gnu` toolchain (no Visual Studio). PyO3 0.29 needs
  `dlltool` with an assembler on that toolchain, so the crate uses PyO3 0.27.
* Rows from MSP libraries (custom references) are scored by the Python code; stage 1 handles them.
* A record the Rust decoder rejects sends that peak's scoring back to Python, which raises the
  readers' own errors.

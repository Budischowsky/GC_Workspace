# GC Workspace

A standalone application for GC-FID / GC-MS data: raw data browsing, its own integrator,
automatic library search (EI Atlas) with NIST handoff, NIAS quantification (mg/kg) and the
NIAS / Fingerprint / Total extraction reports. It needs no ChemStation or MassHunter software and no
`RESULTS.CSV`: everything is computed from the raw files.

## Install and start

1. Run **Setup GC Workspace.cmd** once. It creates `.venv` and installs `requirements.txt`, using `wheelhouse/`
   for an offline install if that folder exists.
2. Start with **Start GC Workspace.cmd**. You can also pass a `.D` folder or a `.gcws` project on the command line.

EI Atlas (library search) is the sibling project `..\UnknownEvaluation`, as for NIAS. Change its location in
*Edit > Preferences* if needed. On the first start the NIAS library-search methods and settings are copied
from `..\NIAS Working\data`. The unknown register is kept separate unless you choose
*Share the NIAS register* in the preferences.

## Raw data

| Detector | Files read |
|---|---|
| FID | `*.ch` (ChemStation v179), otherwise `AcqData/FID*.cg` + `.cd` (MassHunter) |
| MS | `data.ms`, otherwise `AcqData/MSScan.bin` + `MSPeak.bin` (MassHunter centroid) |
| Metadata | `AcqData/sample_info.xml` (sample name = tab name), `Contents.xml`, `Sequence Log .TSV` (injection order) |

Both formats are verified to be bit-identical on the reference batch (`tests/test_io.py`).

## Workflow

1. **Folders** panel: browse to the analysis folder. Double-click a `.D` run, or right-click it and choose
   *Load*, *Load as Blank...*, or *Load all runs in folder*. Every run gets a coloured tab; the active tab is the
   one you work on, and the other runs are overlaid thin.
2. **Integration** runs automatically on load, with the method for the signal: FID / TIC / BPC / EIC, chosen in
   the toolbar. The *Integration method* panel holds the parameters (auto or fixed), the timed events and the
   list of manual events.
3. **Manual tools** in the toolbar work in the chromatogram and in the peak zoom:
   - select/zoom (Z), pan (H)
   - draw baseline (B), split with a drop line (S), delete (D), add peak (A)
   - move start/end (M), merge (G), tangent/exponential skim (K), negative peak (N), reset range (R)

   Manual changes are stored as events and re-applied after every re-integration. They can be undone
   (Ctrl+Z / Ctrl+Y) and are written to the audit trail.
4. **Library search** (Ctrl+F): searches every integrated peak in EI Atlas and fills Name, CAS and Score. You can
   review the hits first. For a single peak there is the *EI Atlas* hit list, *Investigate* (the native
   EI Atlas window), *NIST*, MSP copy/save and *Register unknown*.
5. **Roles and blanks**: right-click a tab and set its role (Sample / Blank / Blank + ISTD / Standard /
   Alkane ladder). Blanks are suggested from the injection order.
6. **Quantification** panel: choose the mode.
   - NIAS mg/kg: the ISTD factor chain and the migration conditions, computed with the unchanged NIAS/AutoLib
     arithmetic.
   - ISTD concentration, with a unit you choose.
   - Total extraction (µg/L).
   - Area %.
7. **Replicates / results** panel: *Suggest* groups `_A`, `_B`, `_C` runs of the same sample. A group can hold two,
   three or more determinations; the worksheet shows c1..cN, the mean and the rel. difference (or SD/RSD). The
   report buttons are here and in the *Report* menu, each with a preview.

## Layout

Every panel can be docked, tabbed or detached, for example onto a second screen.
- When a detached panel is dragged near an edge of the window, a band suggests where it will dock; release to
  dock it there.
- *Layout* menu: presets (Chromatogram top, Table left (classic), Integration, Review, Dual monitor), saved
  named layouts, and a switch to lock the panels.

## Projects

*File > Save project* writes a `.gcws` file. It holds the runs (relative paths), roles, blanks, methods,
manual events, identifications, quantification settings, replicate groups and the audit trail. Integrations
are recomputed on opening, and a changed result is reported. An autosave runs every 2 minutes
(*File > Recover autosave...*).

## Development

```
.venv\Scripts\python -m pytest            # all tests (the sample batch is read from ..\NIAS Working\samples)
.venv\Scripts\python tools\oracle_report.py   # integrator vs. former ChemStation integration
.venv\Scripts\python tools\check_vendor.py    # drift of the vendored NIAS modules
```

The NIAS modules the reports and quantification rely on are vendored in `gcws/nias_legacy` (see
`VENDORED.md`). Patches are marked `GCWS-PATCH`.

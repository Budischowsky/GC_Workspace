#!/usr/bin/env python3
"""Load Agilent ``.D`` folders into the GC workspace model.

Fills the gaps in ``extract_ms_spectra``:

* ``parse_int_tic_full`` keeps all ten columns of the ``[INT TIC]`` section.
  The existing ``parse_int_tic_peaks`` reads past and discards ``Last``,
  ``PK TY``, ``Height``, ``Area``, ``Pct Max`` and ``Pct Total`` -- exactly the
  values the grid needs.
* ``parse_lib_all_hits`` keeps the second and third library hits, which
  ``parse_lib_peaks`` drops by design.
* ``merge_peaks`` binds the two peak lists by *mutual* nearest neighbour.

Why a merge is needed at all
----------------------------
The two lists come from different integrations: the Area Percent Report is
produced by ``autoint1FID.e`` and the library search by ``autoint1MS.e``. In the
reference sample that is 37 integrated peaks against 49 library hits; the 12
extras are real shoulder peaks the FID-tuned integrator merged into their
neighbours. Every row is kept -- an identification without an integration shows
blank Area/Height, and an integration without an identification is an unknown.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import extract_ms_spectra as ex
from gc_model import (PeakRow, Sample, allocate_ids, clean_name, display_cas)

#: How close an identification and an integrated peak must sit to be the same
#: peak. Matches ``extract_ms_spectra.RT_OVERRIDE_TOL``.
MERGE_TOLERANCE_MIN = 0.01

#: Half-width of the retention-time window drawn around a selected peak.
PLOT_CONTEXT_MIN = 0.6


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

def parse_int_tic_full(results_csv: Path) -> list[dict[str, Any]]:
    """Every column of the ``[INT TIC: ...data.ms]`` section.

    Columns: Peak, R.T., First, Max, Last, PK TY, Height, Area, Pct Max,
    Pct Total. Scan numbers are 1-based, as ChemStation reports them.

    The parser now lives in ``extract_ms_spectra`` -- this is the same function
    under the name the workspace already imports.
    """
    return ex.parse_int_tic_full(results_csv)


def parse_lib_all_hits(lib_path: Path) -> dict[int, list[dict[str, Any]]]:
    """All library hits per peak, keyed by peak number.

    ``extract_ms_spectra.parse_lib_peaks`` keeps only the first hit because that
    is the one PBM reports. The workspace shows hits two and three in a detail
    panel so a weak identification can be judged against its alternatives.
    """
    hits: dict[int, list[dict[str, Any]]] = {}
    lines = ex._read_text(lib_path).splitlines()
    i = 0
    current: Optional[int] = None
    while i < len(lines):
        head = ex._LIB_HEAD.match(lines[i])
        if head:
            current = int(head.group(1))
            hits.setdefault(current, [])
            i += 1
            continue
        if current is None:
            i += 1
            continue
        hit = ex._lib_hit(lines[i])
        if not hit:
            i += 1
            continue

        name = lines[i][ex._LIB_NAME_COL:ex._LIB_NAME_END]
        i += 1
        # Long names wrap, broken mid-word, so continuations concatenate
        # without a separator.
        while (i < len(lines) and lines[i].strip()
               and not ex._LIB_HEAD.match(lines[i]) and not ex._lib_hit(lines[i])):
            name += lines[i][ex._LIB_NAME_COL:ex._LIB_NAME_END]
            i += 1

        hits[current].append({
            "name": re.sub(r"\s{2,}", " ", name).strip(),
            "ref": hit.group(1),
            "cas": hit.group(2),
            "qual": int(hit.group(3)),
        })
    return hits


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def merge_peaks(int_tic: list[dict[str, Any]], pbm: list[ex.Peak],
                tolerance: float = MERGE_TOLERANCE_MIN) -> list[tuple]:
    """Bind identifications to integrated peaks by mutual nearest neighbour.

    Returns ``[(int_tic_row | None, pbm_peak | None), ...]`` sorted by retention
    time. Exactly one side may be None; never both.

    ``locate_bounds`` picks the nearest integrated peak *per* identification
    with no exclusivity, so two identifications can bind to the same peak.
    Sorting all candidate pairs by separation and consuming each side once
    removes that failure mode.
    """
    candidates = []
    for pi, p in enumerate(pbm):
        for ti, t in enumerate(int_tic):
            delta = abs(p.rt - t["rt"])
            if delta <= tolerance:
                candidates.append((delta, pi, ti))
    candidates.sort()

    pbm_taken: dict[int, int] = {}
    tic_taken: dict[int, int] = {}
    for _, pi, ti in candidates:
        if pi in pbm_taken or ti in tic_taken:
            continue
        pbm_taken[pi] = ti
        tic_taken[ti] = pi

    pairs: list[tuple] = []
    for ti, t in enumerate(int_tic):
        pi = tic_taken.get(ti)
        pairs.append((t, pbm[pi] if pi is not None else None))
    for pi, p in enumerate(pbm):
        if pi not in pbm_taken:
            pairs.append((None, p))

    pairs.sort(key=lambda pair: pair[0]["rt"] if pair[0] is not None else pair[1].rt)
    return pairs


# --------------------------------------------------------------------------
# Row construction
# --------------------------------------------------------------------------

def build_rows(d_dir: Path, ms: ex.DataMS, ids) -> list[PeakRow]:
    """Merged peak rows for one ``.D`` folder."""
    results_csv = d_dir / "RESULTS.CSV"
    lib_path = d_dir / "LIB"

    int_tic = parse_int_tic_full(results_csv) if results_csv.is_file() else []
    pbm: list[ex.Peak] = []
    if results_csv.is_file():
        pbm = ex.parse_pbm_peaks(results_csv)
    if not pbm and lib_path.is_file():
        pbm = ex.parse_lib_peaks(lib_path)
    alt_hits = parse_lib_all_hits(lib_path) if lib_path.is_file() else {}

    # locate_bounds still wants the three-tuple form.
    bounds_input = [(t["rt"], t["first"], t["max"]) for t in int_tic]

    rows: list[PeakRow] = []
    for tic_row, hit in merge_peaks(int_tic, pbm):
        rt = tic_row["rt"] if tic_row is not None else hit.rt
        apex, bg, rule = ex.locate_bounds(ms, rt, bounds_input)

        if tic_row is not None and hit is not None:
            source = "INT_TIC+PBM"
        elif tic_row is not None:
            source = "INT_TIC"
        else:
            source = "PBM_ONLY"

        row = PeakRow(
            row_id=next(ids),
            # Renumbered by retention time below: the two source lists number
            # independently, so their numbers collide.
            peak_no=len(rows) + 1,
            source=source,
            rt=hit.rt if hit is not None else rt,
            base_mz=None,
            pk_ty=(tic_row or {}).get("pk_ty", ""),
            name=clean_name(hit.name) if hit is not None else "",
            cas=(hit.cas if hit is not None else "0"),
            si=float(hit.qual) if hit is not None else None,
            ref=(hit.ref if hit is not None else ""),
            pbm_area_pct=(hit.area_pct if hit is not None else None),
            apex_scan=apex, bg_scan=bg, bounds_rule=rule,
        )

        if tic_row is not None:
            row.tic_peak_no = tic_row.get("peak")
            row.area = tic_row.get("area")
            row.height = tic_row.get("height")
            row.first_scan = tic_row.get("first")
            row.max_scan = tic_row.get("max")
            row.last_scan = tic_row.get("last")
            # Start/End in minutes are not in the report; derive them from the
            # scan numbers ChemStation does give us.
            row.start_tm = _scan_rt(ms, row.first_scan)
            row.end_tm = _scan_rt(ms, row.last_scan)

        if hit is not None:
            extra = alt_hits.get(hit.num, [])
            row.alt_hits = [
                (h["name"], display_cas(h["cas"]), h["qual"]) for h in extra[1:]
            ]

        if hit is not None:
            row.pbm_peak_no = hit.num

        row.base_mz = _base_peak(ms, row)
        row.snapshot()
        rows.append(row)

    # merge_peaks already returns retention-time order; number the rows the way
    # the analyst reads them.
    for n, row in enumerate(rows, 1):
        row.peak_no = n

    return rows


def _scan_rt(ms: ex.DataMS, scan: Optional[int]) -> Optional[float]:
    """Retention time of a 1-based ChemStation scan number."""
    if scan is None:
        return None
    idx = min(max(scan - 1, 0), ms.n_scans - 1)
    return ms.rt[idx]


def _base_peak(ms: ex.DataMS, row: PeakRow) -> Optional[int]:
    """Base-peak m/z of the background-corrected spectrum."""
    try:
        spec = ex.build_spectrum(ms, row.apex_scan, row.bg_scan, 1.0, False, 1.0)
    except Exception:
        return None
    if not spec:
        return None
    return int(round(max(spec, key=lambda p: p[1])[0]))


# --------------------------------------------------------------------------
# Sample and batch loading
# --------------------------------------------------------------------------

_LABEL_RE = re.compile(r"(?:^|[_\- ])([ABC])$", re.I)


def sample_label(d_dir: Path) -> str:
    """A / B / C from the folder name, or '' when it carries no suffix."""
    stem = d_dir.name
    if stem.lower().endswith(".d"):
        stem = stem[:-2]
    m = _LABEL_RE.search(stem)
    return m.group(1).upper() if m else ""


def load_sample(d_dir: Path, label: str = "", ids=None) -> Sample:
    """Read one ``.D`` folder into a :class:`Sample`.

    Roughly 25 ms for a 3900-scan run: the TIC arrays come free with the header
    walk, and spectra are decoded lazily per row.
    """
    d_dir = Path(d_dir)
    data_ms = d_dir / "data.ms"
    if not data_ms.is_file():
        raise FileNotFoundError(f"{d_dir.name}: data.ms fehlt")

    ms = ex.DataMS(data_ms)
    ids = ids if ids is not None else allocate_ids()
    rows = build_rows(d_dir, ms, ids)

    meta = _read_metadata(d_dir)
    meta.setdefault("sample", d_dir.name[:-2] if d_dir.name.lower().endswith(".d")
                    else d_dir.name)
    meta["scans"] = ms.n_scans
    meta["rt_start"] = ms.rt[0] if ms.rt else None
    meta["rt_end"] = ms.rt[-1] if ms.rt else None

    return Sample(label or sample_label(d_dir) or d_dir.name, d_dir, rows, ms, meta)


def _read_metadata(d_dir: Path) -> dict[str, Any]:
    """Run metadata, reusing the viewer's BOM-sniffing reader.

    ``acqmeth.txt`` is UTF-16LE while the other reports are cp1252, so
    ``extract_ms_spectra._read_text`` would mangle it.
    """
    try:
        import plot_ms_spectra as plot
        return dict(plot.read_run_metadata(d_dir))
    except Exception:
        return {}


def load_cas_lookup(cas_path: Path) -> dict[str, dict[str, Any]]:
    """``{CAS: {"sml", "reference", "footnote"}}`` from CASINFO.xlsx.

    Mirrors the main script's loader: first sheet, header in row 1, headers
    normalised, and the reference/footnote pair swapped when the footnote cell
    is empty but the reference starts with a bracketed marker. Rows sharing a
    CAS are merged field by field, first usable value winning.
    """
    import warnings

    from openpyxl import load_workbook

    def norm(value: Any) -> str:
        text = str(value or "").casefold()
        for a, b in (("²", "2"), ("³", "3"), ("µ", "u")):
            text = text.replace(a, b)
        return re.sub(r"[^a-z0-9]", "", text)

    # CASINFO.xlsx carries conditional-formatting extensions openpyxl cannot
    # read; the warning is noise, the data is fine.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = load_workbook(Path(cas_path), data_only=True, read_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, ()) or ()
        index = {norm(v): i for i, v in enumerate(header) if v is not None}

        def col(*names: str, default: int) -> int:
            for n in names:
                if n in index:
                    return index[n]
            return default

        c_cas = col("cas", "casno", "casnr", default=0)
        c_sml = col("sml", "smlmgkg", default=1)
        c_ref = col("reference", "ref", default=2)
        c_foot = col("footnote", default=3)

        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not row or c_cas >= len(row):
                continue
            cas = display_cas(row[c_cas])
            if not cas:
                continue
            pick = lambda i: (row[i] if i < len(row) else None)
            sml, reference, footnote = pick(c_sml), pick(c_ref), pick(c_foot)
            if not footnote and reference and re.match(r"^\s*\([A-Za-z]\)\s+", str(reference)):
                reference, footnote = footnote, reference
            entry = out.setdefault(cas, {})
            for key, value in (("sml", sml), ("reference", reference),
                               ("footnote", footnote)):
                if entry.get(key) in (None, "", "-") and value not in (None, "", "-"):
                    entry[key] = value
        return out
    finally:
        wb.close()


def find_d_dirs(root: Path) -> list[Path]:
    """Every ``.D`` folder under ``root``; ``root`` itself if it is one."""
    return ex.find_d_dirs(Path(root))


def load_batch(root: Path, limit_labels: bool = True) -> list[Sample]:
    """Load the injections of one batch folder, ordered A, B, C.

    Folders without an A/B/C suffix are loaded in name order and labelled by
    position, so a single-injection folder still opens.
    """
    dirs = find_d_dirs(Path(root))
    if not dirs:
        raise FileNotFoundError(f"{root}: keine .D-Ordner gefunden")

    ids = allocate_ids()
    labelled: list[tuple[str, Path]] = [(sample_label(d), d) for d in dirs]

    if limit_labels and all(lbl for lbl, _ in labelled):
        labelled.sort(key=lambda pair: pair[0])
    else:
        labelled.sort(key=lambda pair: pair[1].name)
        labelled = [(lbl or "ABC"[i] if i < 3 else str(i + 1), d)
                    for i, (lbl, d) in enumerate(labelled)]

    return [load_sample(d, lbl, ids) for lbl, d in labelled]

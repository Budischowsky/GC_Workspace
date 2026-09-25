#!/usr/bin/env python3
"""
extract_ms_spectra.py -- pull mass spectra out of Agilent GC/MS ".D" folders.

For every peak in the PBM library-search result list, extract the background-
subtracted mass spectrum ("apex minus start of peak", the same spectrum Agilent
ChemStation / Enhanced Data Analysis feeds to its library search) straight from
the binary "data.ms" file, and write it as a NIST-format .msp file.

Standard library only -- no third-party packages required.

Usage
-----
    python extract_ms_spectra.py <folder-with-.D-dirs> [options]
    python extract_ms_spectra.py <single-sample.D>     [options]

Options
-------
    -o, --outdir DIR     write results here (default: next to each .D folder)
    --mz-precision P     "nominal" (default, integer m/z) or a step such as 0.05
    --absolute           keep raw abundances instead of normalising base to 999
    --min-rel PERMILLE   drop ions below this permille of the base peak (default 1)
    --verify             run parser self-checks and print a spectrum preview
    -q, --quiet          only print the final summary line and problems
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import struct
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# data.ms binary reader
# --------------------------------------------------------------------------
#
# Layout (verified against AcqData/MSTS.xml, tic_front.csv and results.txt):
#
#   header
#     >H  @0x118          number of scans
#     >H  @0x10A          offset of first scan record, in 2-byte words
#                         (byte offset = value * 2 - 2)
#   per scan record
#     >H  @+0             record length, in 2-byte words
#     >I  @+2             retention time, milliseconds
#     >H  @+6             n_points * 2 + 6      (cross-check)
#     >H  @+12            n_points
#     >H,>H @+18 ...      n_points pairs: (m/z * 20, packed abundance)
#     >I  @+reclen-4      total ion count for the scan
#   trailing directory, immediately after the last scan record
#     n_scans * 12 bytes: (>I scan-offset in words, >I rt_ms, >I tic)
#
# Abundances use a 14-bit mantissa with a 2-bit base-8 exponent:
#     value = (a & 0x3FFF) * 8 ** (a >> 14)

_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")
_PAIR = struct.Struct(">HH")

SCAN_TAIL_BYTES = 10  # bytes after the m/z pairs, ending with the >I stored TIC


class DataMSError(Exception):
    """Raised when data.ms cannot be parsed."""


class DataMS:
    """Lazy reader for an Agilent ChemStation ``data.ms`` file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.raw = self.path.read_bytes()
        d = self.raw

        if len(d) < 0x120:
            raise DataMSError(f"{self.path.name}: too small to be a data.ms file")

        self.n_scans = _U16.unpack_from(d, 0x118)[0]
        first = _U16.unpack_from(d, 0x10A)[0] * 2 - 2
        if self.n_scans == 0:
            raise DataMSError(f"{self.path.name}: header reports 0 scans")
        if not 0 < first < len(d):
            raise DataMSError(f"{self.path.name}: bad first-scan offset {first}")

        # Walk the record chain once; every later lookup is O(1).
        offsets = []
        off = first
        for i in range(self.n_scans):
            if off + 18 > len(d):
                raise DataMSError(
                    f"{self.path.name}: scan {i + 1} runs past end of file"
                )
            offsets.append(off)
            reclen = _U16.unpack_from(d, off)[0] * 2
            if reclen < 18 + SCAN_TAIL_BYTES:
                raise DataMSError(
                    f"{self.path.name}: scan {i + 1} has implausible length {reclen}"
                )
            off += reclen
        self.offsets = offsets
        self.dir_offset = off

        self.rt = [0.0] * self.n_scans   # minutes
        self.tic = [0] * self.n_scans

        # Prefer the trailing directory: it yields RT and TIC for every scan
        # without touching a single m/z pair.
        self.has_directory = (len(d) - off) >= self.n_scans * 12
        if self.has_directory:
            for i in range(self.n_scans):
                q = off + i * 12
                self.rt[i] = _U32.unpack_from(d, q + 4)[0] / 60000.0
                self.tic[i] = _U32.unpack_from(d, q + 8)[0]
        else:
            for i, o in enumerate(offsets):
                reclen = _U16.unpack_from(d, o)[0] * 2
                self.rt[i] = _U32.unpack_from(d, o + 2)[0] / 60000.0
                self.tic[i] = _U32.unpack_from(d, o + reclen - 4)[0]

    # -- scan access -------------------------------------------------------

    def n_points(self, i: int) -> int:
        o = self.offsets[i]
        n = _U16.unpack_from(self.raw, o + 12)[0]
        alt = (_U16.unpack_from(self.raw, o + 6)[0] - 6) // 2
        if n != alt:
            raise DataMSError(
                f"{self.path.name}: scan {i + 1} ion-count mismatch ({n} vs {alt})"
            )
        reclen = _U16.unpack_from(self.raw, o)[0] * 2
        if 18 + n * 4 + SCAN_TAIL_BYTES > reclen:
            raise DataMSError(
                f"{self.path.name}: scan {i + 1} declares {n} ions but the record "
                f"holds only {reclen} bytes"
            )
        return n

    def spectrum(self, i: int) -> list[tuple[float, int]]:
        """Return [(m/z, abundance), ...] for scan index ``i`` (0-based)."""
        d = self.raw
        o = self.offsets[i]
        out = []
        for k in range(self.n_points(i)):
            mz_raw, ab = _PAIR.unpack_from(d, o + 18 + k * 4)
            out.append((mz_raw / 20.0, (ab & 0x3FFF) * 8 ** (ab >> 14)))
        out.sort()
        return out

    def scan_at_rt(self, rt: float) -> int:
        """Index of the scan whose retention time is closest to ``rt``."""
        return min(range(self.n_scans), key=lambda i: abs(self.rt[i] - rt))

    # -- self-checks -------------------------------------------------------

    def self_check(self, expected_scans: int | None = None) -> list[str]:
        """Return a list of human-readable check results."""
        notes = []

        if expected_scans is None:
            notes.append(f"scan count {self.n_scans} (MSTS.xml not available)")
        elif expected_scans == self.n_scans:
            notes.append(f"scan count {self.n_scans} matches MSTS.xml  [OK]")
        else:
            notes.append(
                f"scan count {self.n_scans} DIFFERS from MSTS.xml "
                f"({expected_scans})  [WARN]"
            )

        if self.has_directory:
            bad = sum(
                1
                for i in range(self.n_scans)
                if _U32.unpack_from(self.raw, self.dir_offset + i * 12)[0] * 2 - 2
                != self.offsets[i]
            )
            notes.append(
                f"trailing directory: {self.n_scans} entries, "
                f"{bad} offset mismatches  [{'OK' if bad == 0 else 'WARN'}]"
            )
        else:
            notes.append("trailing directory absent -- RT/TIC read from records")

        # sum(intensities) must reproduce the stored TIC to within the rounding
        # error of the packed 14-bit mantissa.
        step = max(1, self.n_scans // 200)
        worst = 0.0
        worst_scan = 0
        for i in range(0, self.n_scans, step):
            stored = self.tic[i]
            if stored <= 0:
                continue
            dev = abs(sum(a for _, a in self.spectrum(i)) - stored) / stored
            if dev > worst:
                worst, worst_scan = dev, i + 1
        notes.append(
            f"TIC reconstruction: worst deviation {worst * 100:.3f}% "
            f"(scan {worst_scan})  [{'OK' if worst < 0.01 else 'WARN'}]"
        )
        return notes


# --------------------------------------------------------------------------
# Peak lists
# --------------------------------------------------------------------------

PLACEHOLDER_CAS = {"000000-00-0", "0-00-0", ""}


class Peak:
    __slots__ = ("num", "rt", "area_pct", "name", "ref", "cas", "qual",
                 "apex", "bg", "rule")

    def __init__(self, num, rt, area_pct, name, ref, cas, qual):
        self.num = num
        self.rt = rt
        self.area_pct = area_pct
        self.name = name
        self.ref = ref
        self.cas = cas
        self.qual = qual
        self.apex = self.bg = None
        self.rule = ""


def decode_bytes(raw: bytes) -> str:
    """Decode an Agilent text file's bytes, sniffing the encoding.

    A ``.D`` folder mixes encodings: ``RESULTS.CSV``, ``LIB`` and ``results.txt``
    are Windows ANSI, while ``acqmeth.txt`` is UTF-16LE (with a BOM) and the
    ``AcqData`` XML files may be either. Reading everything as cp1252 turns
    ``acqmeth.txt`` into NUL-riddled mojibake, so the byte-order mark decides,
    with a NUL-density fallback for BOM-less UTF-16 and cp1252 as the last
    resort (it never raises, so a text file always comes back as text).
    """
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return raw.decode("utf-32", errors="replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", errors="replace")
    # BOM-less UTF-16: ASCII text in either byte order is half NUL bytes, and
    # which half tells the two orders apart. cp1252 text has no NULs at all.
    head = raw[:4096]
    if head:
        even = head[0::2].count(0)
        odd = head[1::2].count(0)
        if odd > len(head) // 4 and odd > even * 4:
            return raw.decode("utf-16-le", errors="replace")
        if even > len(head) // 4 and even > odd * 4:
            return raw.decode("utf-16-be", errors="replace")
    return raw.decode("cp1252", errors="replace")


def _read_text(path: Path) -> str:
    """Read an Agilent text file with the encoding sniffed from its bytes."""
    return decode_bytes(path.read_bytes())


def _sections(text: str):
    """Yield (section_name, [data_rows]) from an Agilent RESULTS.CSV."""
    name, rows = None, []
    for line in text.splitlines():
        if line.startswith("["):
            if name is not None:
                yield name, rows
            name, rows = line.strip().strip("[]").strip(), []
        elif name is not None:
            m = re.match(r"^\d+=,(.*)$", line)
            if m:
                # Compound names contain commas -- this must go through csv.
                rows.append(next(csv.reader(io.StringIO(m.group(1)))))
    if name is not None:
        yield name, rows


def _num(v, cast=float, default=None):
    try:
        return cast(str(v).strip())
    except (TypeError, ValueError):
        return default


def parse_pbm_peaks(results_csv: Path) -> list[Peak]:
    """Peaks from [PBM ...]: PK, RT, Area Pct, Library/ID, Ref, CAS, Qual."""
    peaks: list[Peak] = []
    for name, rows in _sections(_read_text(results_csv)):
        if not name.upper().startswith("PBM"):
            continue
        for r in rows:
            if len(r) < 7:
                continue
            rt = _num(r[1])
            if rt is None:
                continue
            peaks.append(
                Peak(
                    num=_num(r[0], int, len(peaks) + 1),
                    rt=rt,
                    area_pct=_num(r[2], float, 0.0),
                    name=r[3].strip(),
                    ref=r[4].strip(),
                    cas=r[5].strip(),
                    qual=_num(r[6], int, 0),
                )
            )
        break
    return peaks


#: Field order of the ``[INT TIC: ...data.ms]`` section, as ChemStation writes
#: it. The names match ``gc_load.parse_int_tic_full`` so the two stay readable
#: as one contract.
INT_TIC_FIELDS = ("peak", "rt", "first", "max", "last",
                  "pk_ty", "height", "area", "pct_max", "pct_total")


def parse_int_tic_full(results_csv: Path) -> list[dict]:
    """Every column of the ``[INT TIC: ...data.ms]`` section.

    Columns: Peak, R.T., First, Max, Last, PK TY, Height, Area, Pct Max,
    Pct Total -- keyed by :data:`INT_TIC_FIELDS`. Scan numbers are 1-based, as
    ChemStation reports them. A row is kept only when R.T., First and Max parse;
    the trailing columns are optional and come back as ``None`` (``""`` for the
    textual ``PK TY``) when the file is short.

    Same parse as ``gc_load.parse_int_tic_full``; keep the two in step.
    """
    out: list[dict] = []
    for name, rows in _sections(_read_text(results_csv)):
        up = name.upper()
        if not (up.startswith("INT TIC") and "DATA.MS" in up):
            continue
        for r in rows:
            if len(r) < 5:
                continue
            rt, first, mx = _num(r[1]), _num(r[2], int), _num(r[3], int)
            if None in (rt, first, mx):
                continue
            out.append({
                "peak": _num(r[0], int, len(out) + 1),
                "rt": rt,
                "first": first,
                "max": mx,
                "last": _num(r[4], int),
                "pk_ty": r[5].strip() if len(r) > 5 else "",
                "height": _num(r[6]) if len(r) > 6 else None,
                "area": _num(r[7]) if len(r) > 7 else None,
                "pct_max": _num(r[8]) if len(r) > 8 else None,
                "pct_total": _num(r[9]) if len(r) > 9 else None,
            })
        break
    return out


def parse_int_tic_peaks(results_csv: Path) -> list[tuple[float, int, int]]:
    """(rt, first_scan, max_scan) from [INT TIC: ...data.ms], scans 1-based.

    The narrow view ``locate_bounds`` needs. Use :func:`parse_int_tic_full` for
    the remaining columns (Last, PK TY, Height, Area, Pct Max, Pct Total).
    """
    return [(p["rt"], p["first"], p["max"])
            for p in parse_int_tic_full(results_csv)]


# The LIB report is fixed-width. A peak header names the library, the hit lines
# that follow carry name / Ref# / CAS# / Qual, and long names wrap onto extra
# lines -- broken mid-word, so continuations concatenate with no separator:
#
#   col 0        17                                 51
#   |            |                                  |
#     7  13.379 48.28 X:/GC-Datenbanken/NIST05a.L
#                     1,3-Benzenedicarboxylic acid, diet  72447 000636-53-3 96
#                     hyl ester
#
# CAS numbers may be NIST pseudo-CAS with a 7-digit prefix (1000406-33-8).

_LIB_NAME_COL = 17
_LIB_NAME_END = 51
_LIB_HEAD = re.compile(r"^\s*(\d+)\s+(\d+\.\d+)\s+([\d.]+)\s+\S*\.L\s*$", re.I)
# Anchored to the column boundary, not to whitespace: when a name fills the
# whole 34-character field only a single space separates it from Ref#.
_LIB_TAIL = re.compile(r"^\s*(\d+)\s+(\d+-\d+-\d+)\s+(\d{1,3})\s*$")


def _lib_hit(line: str):
    """Return the Ref/CAS/Qual match if ``line`` is a library-hit line."""
    if line[:_LIB_NAME_COL].strip():
        return None
    return _LIB_TAIL.match(line[_LIB_NAME_END:])


def parse_lib_peaks(lib_path: Path) -> list[Peak]:
    """Fallback: parse the LIB text report when RESULTS.CSV has no PBM section.

    Only each peak's first hit is taken -- that is the one PBM reports.
    """
    peaks: list[Peak] = []
    lines = _read_text(lib_path).splitlines()
    i = 0
    while i < len(lines):
        head = _LIB_HEAD.match(lines[i])
        i += 1
        if not head:
            continue
        num, rt, area = int(head.group(1)), float(head.group(2)), float(head.group(3))

        # The first hit line after the header is the top match.
        hit = None
        while i < len(lines) and lines[i].strip() and not _LIB_HEAD.match(lines[i]):
            hit = _lib_hit(lines[i])
            if hit:
                break
            i += 1
        if not hit:
            continue
        name = lines[i][_LIB_NAME_COL:_LIB_NAME_END]
        i += 1

        # Wrapped remainder of the name, if any.
        while (i < len(lines) and lines[i].strip()
               and not _LIB_HEAD.match(lines[i]) and not _lib_hit(lines[i])):
            name += lines[i][_LIB_NAME_COL:_LIB_NAME_END]
            i += 1

        peaks.append(
            Peak(num, rt, area, re.sub(r"\s{2,}", " ", name).strip(),
                 hit.group(1), hit.group(2), int(hit.group(3)))
        )
    return peaks


def read_expected_scans(d_dir: Path) -> int | None:
    msts = d_dir / "AcqData" / "MSTS.xml"
    if not msts.is_file():
        return None
    m = re.search(r"<NumOfScans>\s*(\d+)\s*</NumOfScans>", _read_text(msts))
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# Peak boundaries and spectrum construction
# --------------------------------------------------------------------------

RT_OVERRIDE_TOL = 0.01   # min; how close an INT TIC peak must be to reuse its scans
APEX_WINDOW = 0.03       # min; how far to look for the true TIC maximum
BG_MAX_SCANS = 60        # cap on the walk back towards the peak start
BG_PATIENCE = 3          # consecutive non-improving scans before giving up


def locate_bounds(ms: DataMS, rt: float,
                  int_tic: list[tuple[float, int, int]]) -> tuple[int, int, str]:
    """Return (apex_index, background_index, rule) as 0-based scan indices.

    Preferred: reuse ChemStation's own First/Max scans when an integrated TIC
    peak sits at the same retention time. Otherwise re-derive both from the TIC.
    """
    best = None
    for prt, first, mx in int_tic:
        delta = abs(prt - rt)
        if delta <= RT_OVERRIDE_TOL and (best is None or delta < best[0]):
            best = (delta, first, mx)
    if best is not None:
        _, first, mx = best
        apex = min(max(mx - 1, 0), ms.n_scans - 1)
        bg = min(max(first - 1, 0), ms.n_scans - 1)
        if bg != apex:
            return apex, bg, "INT_TIC"

    # Snap to the local TIC maximum near the reported retention time.
    centre = ms.scan_at_rt(rt)
    lo = hi = centre
    while lo > 0 and ms.rt[centre] - ms.rt[lo - 1] <= APEX_WINDOW:
        lo -= 1
    while hi < ms.n_scans - 1 and ms.rt[hi + 1] - ms.rt[centre] <= APEX_WINDOW:
        hi += 1
    apex = max(range(lo, hi + 1), key=lambda i: ms.tic[i])

    # Walk left to the local minimum in front of the peak.
    bg, lowest, misses, j = apex, ms.tic[apex], 0, apex
    for _ in range(BG_MAX_SCANS):
        if j == 0:
            break
        j -= 1
        if ms.tic[j] < lowest:
            bg, lowest, misses = j, ms.tic[j], 0
        else:
            misses += 1
            if misses >= BG_PATIENCE:
                break
    return apex, bg, "DERIVED"


def _nominal(x: float) -> int:
    """Round half *down*.

    The quadrupole reports ions slightly high, and high-mass ions land exactly
    on .50 (e.g. m/z 647.50 for the M-15 of Irgafos 168 oxide, true mass 647).
    Python's round() is banker's rounding, so it would fold both 647.50 and
    648.50 onto 648 -- merging an ion with its own 13C isotope and moving the
    base peak. Rounding halves down reproduces the true nominal masses.
    """
    return math.ceil(x - 0.5)


def _binned(pairs, unit: float) -> dict[int, int]:
    out: dict[int, int] = {}
    for mz, ab in pairs:
        k = _nominal(mz / unit)
        out[k] = out.get(k, 0) + ab
    return out


def build_spectrum(ms: DataMS, apex: int, bg: int, unit: float,
                   normalise: bool, min_permille: float):
    """Background-subtracted spectrum as [(m/z, intensity), ...], m/z ascending."""
    top = _binned(ms.spectrum(apex), unit)
    base = _binned(ms.spectrum(bg), unit) if bg != apex else {}

    diff = {k: v - base.get(k, 0) for k, v in top.items()}
    diff = {k: v for k, v in diff.items() if v > 0}
    if not diff:
        return []

    cutoff = max(diff.values()) * min_permille / 1000.0
    diff = {k: v for k, v in diff.items() if v >= cutoff}
    if not diff:
        return []

    peak_max = max(diff.values())
    out = []
    for k in sorted(diff):
        inten = round(diff[k] / peak_max * 999) if normalise else diff[k]
        if inten > 0:
            out.append((k * unit, int(inten)))
    return out


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _fmt_mz(mz: float, unit: float) -> str:
    return str(_nominal(mz)) if unit >= 1.0 else f"{mz:.2f}"


def msp_entry(sample: str, p: Peak, spectrum, unit: float) -> str:
    lines = [f"Name: {p.num:02d} {p.name} (RT {p.rt:.3f})"]
    if p.cas and p.cas not in PLACEHOLDER_CAS:
        # NIST expects the CAS without the Agilent zero padding.
        lines.append(f"CAS#: {p.cas.lstrip('0') or p.cas}")
    lines.append(
        "Comments: "
        f"Sample={sample}; RT={p.rt:.4f}; Area%={p.area_pct:.4f}; "
        f"Qual={p.qual}; Ref={p.ref}; ApexScan={p.apex + 1}; "
        f"BgScan={p.bg + 1}; Bounds={p.rule}"
    )
    lines.append(f"Num Peaks: {len(spectrum)}")
    for i in range(0, len(spectrum), 5):
        chunk = spectrum[i:i + 5]
        lines.append(" " + " ".join(f"{_fmt_mz(m, unit)} {v};" for m, v in chunk))
    lines.append("")
    return "\n".join(lines)


INDEX_HEADER = [
    "Peak", "RT_min", "ApexScan", "BgScan", "BoundsRule", "ApexTIC", "BgTIC",
    "Compound", "CAS", "Qual", "Area_pct", "NumIons", "BasePeak_mz",
]


def write_index_csv(path: Path, ms: DataMS, peaks, spectra, unit: float) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(INDEX_HEADER)
        for p in peaks:
            sp = spectra[p.num]
            base = _fmt_mz(max(sp, key=lambda x: x[1])[0], unit) if sp else ""
            w.writerow([
                p.num, f"{p.rt:.4f}", p.apex + 1, p.bg + 1, p.rule,
                ms.tic[p.apex], ms.tic[p.bg], p.name, p.cas, p.qual,
                f"{p.area_pct:.4f}", len(sp), base,
            ])


# --------------------------------------------------------------------------
# Per-sample driver
# --------------------------------------------------------------------------

class SampleResult:
    def __init__(self, name, status, detail="", msp=None, index=None, n_peaks=0):
        self.name, self.status, self.detail = name, status, detail
        self.msp, self.index, self.n_peaks = msp, index, n_peaks


def process_sample(d_dir: Path, outdir: Path | None, unit: float,
                   normalise: bool, min_permille: float,
                   verify: bool, log) -> SampleResult:
    sample = d_dir.name[:-2] if d_dir.name.lower().endswith(".d") else d_dir.name
    data_ms = d_dir / "data.ms"
    if not data_ms.is_file():
        return SampleResult(sample, "skipped", "no data.ms (MS-free sample?)")

    try:
        ms = DataMS(data_ms)
    except (DataMSError, OSError) as exc:
        return SampleResult(sample, "failed", str(exc))

    if verify:
        log(f"  self-check:")
        for note in ms.self_check(read_expected_scans(d_dir)):
            log(f"    - {note}")

    results_csv = d_dir / "RESULTS.CSV"
    lib = d_dir / "LIB"
    peaks: list[Peak] = []
    int_tic: list[tuple[float, int, int]] = []
    try:
        if results_csv.is_file():
            peaks = parse_pbm_peaks(results_csv)
            int_tic = parse_int_tic_peaks(results_csv)
        if not peaks and lib.is_file():
            why = "no RESULTS.CSV" if not results_csv.is_file() else "no PBM section"
            log(f"  {why} -- falling back to the LIB report")
            peaks = parse_lib_peaks(lib)
    except OSError as exc:
        return SampleResult(sample, "failed", f"peak list unreadable: {exc}")

    if not peaks:
        return SampleResult(sample, "skipped", "no PBM peak list found")

    spectra: dict[int, list] = {}
    for p in peaks:
        p.apex, p.bg, p.rule = locate_bounds(ms, p.rt, int_tic)
        try:
            spectra[p.num] = build_spectrum(
                ms, p.apex, p.bg, unit, normalise, min_permille
            )
        except DataMSError as exc:
            log(f"  peak {p.num} skipped ({exc})")
            spectra[p.num] = []

    usable = [p for p in peaks if spectra[p.num]]
    if not usable:
        return SampleResult(sample, "skipped",
                            "no peak yielded ions after background subtraction")

    target = outdir if outdir else d_dir.parent
    target.mkdir(parents=True, exist_ok=True)
    msp_path = target / f"{sample}.msp"
    idx_path = target / f"{sample}_peak_index.csv"

    with msp_path.open("w", encoding="cp1252", errors="replace", newline="\n") as fh:
        for p in usable:
            fh.write(msp_entry(sample, p, spectra[p.num], unit))
    write_index_csv(idx_path, ms, peaks, spectra, unit)

    if verify:
        _preview(usable, spectra, unit, log)

    n_derived = sum(1 for p in usable if p.rule == "DERIVED")
    return SampleResult(
        sample, "ok",
        f"{len(usable)}/{len(peaks)} peaks ({n_derived} with re-derived bounds), "
        f"{ms.n_scans} scans",
        msp_path, idx_path, len(usable),
    )


def _preview(usable, spectra, unit, log) -> None:
    """Print the largest peak's spectrum -- the chemical sanity check."""
    p = max(usable, key=lambda q: q.area_pct)
    top = sorted(spectra[p.num], key=lambda x: -x[1])[:10]
    log(f"  largest peak: #{p.num} at RT {p.rt:.3f} -- {p.name} (Qual {p.qual})")
    log(f"    apex scan {p.apex + 1}, background scan {p.bg + 1} [{p.rule}]")
    log("    top ions: " + ", ".join(f"{_fmt_mz(m, unit)}:{v}" for m, v in top))


# --------------------------------------------------------------------------
# Batch driver
# --------------------------------------------------------------------------

def find_d_dirs(root: Path) -> list[Path]:
    if root.is_dir() and root.name.lower().endswith(".d"):
        return [root]
    found = {
        p.resolve()
        for p in root.rglob("*")
        if p.is_dir() and p.name.lower().endswith(".d")
    }
    return sorted(found)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract background-subtracted mass spectra from Agilent "
                    "GC/MS .D folders into NIST .msp files."
    )
    ap.add_argument("path", type=Path,
                    help="a .D folder, or a parent folder containing .D folders")
    ap.add_argument("-o", "--outdir", type=Path, default=None,
                    help="output directory (default: alongside each .D folder)")
    ap.add_argument("--mz-precision", default="nominal",
                    help="'nominal' for integer m/z (default), or a step such as 0.05")
    ap.add_argument("--absolute", action="store_true",
                    help="keep raw abundances instead of normalising base peak to 999")
    ap.add_argument("--min-rel", type=float, default=1.0, metavar="PERMILLE",
                    help="drop ions below this permille of the base peak (default 1)")
    ap.add_argument("--verify", action="store_true",
                    help="run parser self-checks and print a spectrum preview")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    def log(msg=""):
        if not args.quiet:
            print(msg)

    if args.mz_precision.strip().lower() in ("nominal", "unit", "integer", "1"):
        unit = 1.0
    else:
        try:
            unit = float(args.mz_precision)
        except ValueError:
            ap.error("--mz-precision must be 'nominal' or a number such as 0.05")
        if unit <= 0:
            ap.error("--mz-precision must be positive")

    root = args.path.expanduser()
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2

    targets = find_d_dirs(root)
    if not targets:
        print(f"error: no .D folders found under {root}", file=sys.stderr)
        return 2

    log(f"Found {len(targets)} .D folder(s) under {root}\n")
    results = []
    for d in targets:
        log(f"* {d.name}")
        try:
            res = process_sample(d, args.outdir, unit, not args.absolute,
                                 args.min_rel, args.verify, log)
        except Exception as exc:  # one bad sample must not kill the batch
            res = SampleResult(d.name, "failed", f"{type(exc).__name__}: {exc}")
        results.append(res)
        if res.status == "ok":
            log(f"  -> {res.detail}")
            log(f"  -> {res.msp.name}")
            log(f"  -> {res.index.name}")
        else:
            log(f"  -> {res.status}: {res.detail}")
        log()

    ok = [r for r in results if r.status == "ok"]
    skipped = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]
    print(f"Done. {len(ok)} written, {len(skipped)} skipped, {len(failed)} failed; "
          f"{sum(r.n_peaks for r in ok)} spectra total.")
    for r in skipped + failed:
        print(f"  {r.status}: {r.name} -- {r.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

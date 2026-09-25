"""Readers for Agilent MassHunter ``AcqData`` files (no Agilent software needed).

Layouts, verified bit for bit against ``data.ms``/``FID1A.ch`` written for the
same injection:

``FID*.cg``  84-byte header; little-endian float64 at 0x44 = start time and
             sampling interval (minutes); float64 samples from 0x54.
``FID*.cd``  descriptor; carries the point count as little-endian u32.
``MSScan.bin`` int32 at 0x58 = offset of the first scan record; fixed-size
             records follow, the size derived from ``NumOfScans`` in MSTS.xml.
             Per record: ScanID i32 @0, ScanTime f8 @12 (min), TIC f8 @28,
             BasePeakMZ f8 @36, BasePeakValue f8 @44, SpectrumOffset i64 @136,
             ByteCount i32 @144, PointCount i32 @148.
``MSPeak.bin`` at SpectrumOffset: PointCount float64 m/z, then PointCount
             float64 abundances (centroids).

Anything that does not satisfy these checks is refused, never guessed: a wrong
layout gives a plausible but wrong chromatogram.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CG_HEADER = 0x54
CG_TIMES = 0x44
SCAN_FIRST_OFFSET = 0x58
MIN_RECORD = 152


class UnsupportedFormat(Exception):
    """The file does not match a layout this reader knows."""


@dataclass
class DetectorTrace:
    rt: np.ndarray          # minutes
    y: np.ndarray
    hz: float
    path: Path
    signal: str


def read_cg(path) -> DetectorTrace:
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) < CG_HEADER + 16 or (len(raw) - CG_HEADER) % 8:
        raise UnsupportedFormat(f"{path.name}: unexpected size {len(raw)} bytes")
    t0, dt = struct.unpack_from("<2d", raw, CG_TIMES)
    if not (np.isfinite(t0) and np.isfinite(dt) and dt > 0 and dt < 1.0):
        raise UnsupportedFormat(f"{path.name}: implausible time header ({t0}, {dt})")
    y = np.frombuffer(raw, dtype="<f8", offset=CG_HEADER).copy()
    if not np.isfinite(y).all():
        raise UnsupportedFormat(f"{path.name}: data block contains NaN/Inf")
    cd = path.with_suffix(".cd")
    signal = path.stem
    if cd.exists():
        desc = cd.read_bytes()
        counts = {v for (v,) in struct.iter_unpack("<I", desc[: len(desc) // 4 * 4])}
        if y.size not in counts and not _count_anywhere(desc, y.size):
            raise UnsupportedFormat(
                f"{path.name}: point count {y.size} not confirmed by {cd.name}")
        m = re.search(rb"Signal #\d+", desc)
        if m:
            signal = f"{path.stem} ({m.group().decode()})"
    rt = t0 + np.arange(y.size, dtype=np.float64) * dt
    return DetectorTrace(rt=rt, y=y, hz=1.0 / (dt * 60.0), path=path, signal=signal)


def _count_anywhere(buf: bytes, n: int) -> bool:
    needle = struct.pack("<I", n)
    return needle in buf


def _num_scans(acq: Path) -> int | None:
    msts = acq / "MSTS.xml"
    if not msts.exists():
        return None
    text = msts.read_text(encoding="utf-8-sig", errors="replace")
    found = re.findall(r"<NumOfScans>(\d+)</NumOfScans>", text)
    return sum(int(v) for v in found) if found else None


class MSPeakSource:
    """Centroid MS data from ``MSScan.bin`` + ``MSPeak.bin``.

    Implements the interface of ``extract_ms_spectra.DataMS`` that the rest of
    the code uses (``n_scans``, ``rt``, ``tic``, ``spectrum(i)``,
    ``scan_at_rt``), so deconvolution, purity and spectrum building work on it
    unchanged.
    """

    def __init__(self, acq_dir):
        acq = Path(acq_dir)
        self.path = acq / "MSScan.bin"
        scan = self.path.read_bytes()
        self._peak_path = acq / "MSPeak.bin"
        if not self._peak_path.exists():
            raise UnsupportedFormat("MSPeak.bin missing (profile-only data is not supported)")
        self._peak = self._peak_path.read_bytes()
        if len(scan) < SCAN_FIRST_OFFSET + 4:
            raise UnsupportedFormat("MSScan.bin too small")
        first = struct.unpack_from("<i", scan, SCAN_FIRST_OFFSET)[0]
        n = _num_scans(acq)
        if n is None or n <= 0:
            raise UnsupportedFormat("MSTS.xml missing or without NumOfScans")
        body = len(scan) - first
        if first <= 0 or body <= 0 or body % n:
            raise UnsupportedFormat(
                f"MSScan.bin: {body} record bytes do not divide into {n} scans")
        size = body // n
        if size < MIN_RECORD:
            raise UnsupportedFormat(f"MSScan.bin: record size {size} too small")
        rec = np.dtype({
            "names": ["id", "time", "tic", "bp_mz", "bp", "off", "bytes", "points"],
            "formats": ["<i4", "<f8", "<f8", "<f8", "<f8", "<i8", "<i4", "<i4"],
            "offsets": [0, 12, 28, 36, 44, 136, 144, 148],
            "itemsize": size,
        })
        table = np.frombuffer(scan, dtype=rec, count=n, offset=first)
        if not np.all(table["bytes"] == table["points"] * 16):
            raise UnsupportedFormat("MSScan.bin: ByteCount != 16 x PointCount")
        ends = table["off"] + table["bytes"]
        if table["off"].min() < 0 or ends.max() > len(self._peak):
            raise UnsupportedFormat("MSScan.bin: spectrum offsets outside MSPeak.bin")
        if np.any(np.diff(table["time"]) <= 0):
            raise UnsupportedFormat("MSScan.bin: scan times are not ascending")
        self._table = table
        self.n_scans = int(n)
        self.rt = table["time"].astype(float).tolist()
        self.tic = table["tic"].astype(float).tolist()

    def arrays(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        row = self._table[i]
        k = int(row["points"])
        off = int(row["off"])
        mz = np.frombuffer(self._peak, "<f8", k, off)
        ab = np.frombuffer(self._peak, "<f8", k, off + 8 * k)
        return mz, ab

    def spectrum(self, i: int) -> list[tuple[float, float]]:
        mz, ab = self.arrays(i)
        order = np.argsort(mz, kind="stable")
        return [(float(mz[j]), float(ab[j])) for j in order]

    def scan_at_rt(self, rt: float) -> int:
        times = self._table["time"]
        j = int(np.searchsorted(times, rt))
        if j <= 0:
            return 0
        if j >= self.n_scans:
            return self.n_scans - 1
        return j if abs(times[j] - rt) < abs(times[j - 1] - rt) else j - 1

    def self_check(self, expected_scans: int | None = None) -> list[str]:
        notes = [f"{self.n_scans} scans (MassHunter MSScan.bin)"]
        bad = 0
        for i in range(0, self.n_scans, max(1, self.n_scans // 50)):
            _, ab = self.arrays(i)
            stored = self.tic[i]
            if stored > 0 and abs(float(ab.sum()) - stored) / stored > 0.01:
                bad += 1
        notes.append("TIC self-check OK" if not bad else f"TIC mismatch in {bad} sampled scans")
        return notes

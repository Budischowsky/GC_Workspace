"""Read full-scan GCMSsolution QGD data without changing the acquisition file.

QGD stores scan offsets, millisecond retention times and spectra in OLE streams.
Layout reference: https://github.com/scisciuro/shimadzu-qgd2csv
Each spectrum is checked against its index boundary, RT and stored TIC. Unknown
layouts fail explicitly instead of silently dropping scans or inventing data.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from gcws.io.ms_matrix import MSMatrix


def decode_scan(block: bytes, rt_ms: int) -> tuple[np.ndarray, np.ndarray]:
    if len(block) < 32:
        raise ValueError("Truncated QGD scan header")
    if struct.unpack_from("<I", block, 4)[0] != rt_ms:
        raise ValueError("QGD scan time disagrees with the retention-time index")
    width, count = struct.unpack_from("<HH", block, 20)
    payload = len(block) - 32
    if count == 0:
        if payload:
            raise ValueError("QGD empty scan has unexpected data")
        return np.zeros(0), np.zeros(0)
    # A documented saturated-signal variant declares one byte but stores four.
    if width == 1 and payload == count * 6:
        width = 4
    if width not in (1, 2, 3, 4) or payload != count * (2 + width):
        raise ValueError("Unsupported or truncated QGD full-scan layout")
    values = np.frombuffer(block, dtype=np.uint8, offset=32).reshape(count, 2 + width)
    mz = (values[:, 0].astype(np.uint32) + (values[:, 1].astype(np.uint32) << 8)) / 20.0
    ab = np.zeros(count, dtype=np.uint64)
    for j in range(width):
        ab |= values[:, 2 + j].astype(np.uint64) << (8 * j)
    if width == 4:
        ab &= 0x7fffffff
    if np.any(mz <= 0) or np.any(np.diff(mz) <= 0):
        raise ValueError("Unsupported QGD spectrum: masses must be positive and ascending")
    return mz, ab.astype(float)


class QGDSource:
    def __init__(self, path: Path):
        try:
            from olefile import OleFileIO
        except ImportError as exc:
            raise ValueError("QGD import requires olefile; run Setup GC Workspace") from exc
        with OleFileIO(str(path)) as container:
            def read(name):
                key = ["GCMS Raw Data", name]
                if not container.exists(key):
                    raise ValueError(f"Unsupported QGD: missing {name}")
                return container.openstream(key).read()

            times, indices, totals, raw = (read(n) for n in
                ("Retention Time", "Spectrum Index", "TIC Data", "MS Raw Data"))
        if not times or len(times) % 4 or len(indices) != len(times) or len(totals) != len(times) * 2:
            raise ValueError("QGD scan, time and TIC counts disagree")
        rt_ms = np.frombuffer(times, dtype="<u4").astype(np.int64)
        offsets = np.frombuffer(indices, dtype="<u4").astype(np.int64)
        self.rt = rt_ms / 60000.0
        self.tic = np.frombuffer(totals, dtype="<u8").astype(float)
        self.n_scans = len(rt_ms)
        ends = np.r_[offsets[1:], len(raw)]
        if (offsets[0] != 0 or np.any(ends - offsets < 32) or
                np.any(ends > len(raw)) or np.any(np.diff(rt_ms) <= 0)):
            raise ValueError("Invalid QGD spectrum boundaries or scan times")
        self._spectra = []
        for i, (start, end) in enumerate(zip(offsets, ends)):
            try:
                mz, ab = decode_scan(raw[int(start):int(end)], int(rt_ms[i]))
                if abs(float(ab.sum()) - self.tic[i]) > 0.5:
                    raise ValueError("decoded spectrum sum disagrees with the stored TIC")
                self._spectra.append((mz, ab))
            except ValueError as exc:
                raise ValueError(f"QGD scan {i + 1}: {exc}") from exc

    def arrays(self, i):
        return self._spectra[i]

    def spectrum(self, i):
        return list(zip(*self.arrays(i)))

    def matrix(self):
        return MSMatrix.from_source(self)

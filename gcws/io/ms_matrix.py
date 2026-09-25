"""All MS scans of one run decoded once into compact numpy arrays.

The layout is CSR-like: scan ``i`` owns ``mz[ptr[i]:ptr[i+1]]`` and
``ab[ptr[i]:ptr[i+1]]``. A run of ~4 000 scans holds a few hundred thousand
points (a few MB), so TIC/EIC/BPC and averaged spectra become vectorised
operations instead of per-scan Python loops.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

_U16 = struct.Struct(">H")


def nominal(mz: np.ndarray) -> np.ndarray:
    """Nominal mass with exact halves rounded down (the NIAS/PBM convention)."""
    return np.ceil(np.asarray(mz, dtype=float) - 0.5).astype(np.int64)


@dataclass
class MSMatrix:
    rt: np.ndarray        # minutes, one per scan
    stored_tic: np.ndarray
    ptr: np.ndarray       # int64, n_scans + 1
    mz: np.ndarray        # float64
    ab: np.ndarray        # float64
    nom: np.ndarray       # nominal m/z per point (int64)

    @property
    def n_scans(self) -> int:
        return int(self.rt.size)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_source(cls, src) -> "MSMatrix":
        if hasattr(src, "offsets") and hasattr(src, "raw"):
            return cls._from_datams(src)
        if hasattr(src, "arrays"):
            parts = [src.arrays(i) for i in range(src.n_scans)]
            return cls._build(src.rt, src.tic, [p[0] for p in parts], [p[1] for p in parts])
        parts = [src.spectrum(i) for i in range(src.n_scans)]
        mzs = [np.array([p[0] for p in s], float) for s in parts]
        abs_ = [np.array([p[1] for p in s], float) for s in parts]
        return cls._build(src.rt, src.tic, mzs, abs_)

    @classmethod
    def _from_datams(cls, dm) -> "MSMatrix":
        raw = dm.raw
        mzs, abs_ = [], []
        for i, o in enumerate(dm.offsets):
            n = dm.n_points(i)
            pairs = np.frombuffer(raw, dtype=">u2", count=2 * n, offset=o + 18)
            mz = pairs[0::2].astype(np.float64) / 20.0
            enc = pairs[1::2].astype(np.int64)
            ab = (enc & 0x3FFF).astype(np.float64) * np.power(8.0, enc >> 14)
            order = np.argsort(mz, kind="stable")
            mzs.append(mz[order])
            abs_.append(ab[order])
        return cls._build(dm.rt, dm.tic, mzs, abs_)

    @classmethod
    def _build(cls, rt, tic, mzs, abs_) -> "MSMatrix":
        counts = np.fromiter((len(m) for m in mzs), dtype=np.int64, count=len(mzs))
        ptr = np.zeros(len(mzs) + 1, dtype=np.int64)
        np.cumsum(counts, out=ptr[1:])
        mz = np.concatenate(mzs) if mzs else np.zeros(0)
        ab = np.concatenate(abs_) if abs_ else np.zeros(0)
        return cls(rt=np.asarray(rt, dtype=float), stored_tic=np.asarray(tic, dtype=float),
                   ptr=ptr, mz=mz.astype(float), ab=ab.astype(float), nom=nominal(mz))

    # -- chromatograms -----------------------------------------------------

    def _scan_index(self) -> np.ndarray:
        idx = getattr(self, "_scan_of_point", None)
        if idx is None:
            idx = np.repeat(np.arange(self.n_scans), np.diff(self.ptr))
            self._scan_of_point = idx
        return idx

    def tic(self) -> np.ndarray:
        return np.bincount(self._scan_index(), weights=self.ab, minlength=self.n_scans)

    def eic(self, masses, tol: float = 0.5, nominal_match: bool = True) -> np.ndarray:
        """Summed abundance of the given m/z values per scan."""
        masses = np.atleast_1d(np.asarray(masses, dtype=float))
        if nominal_match:
            mask = np.isin(self.nom, nominal(masses))
        else:
            mask = np.zeros(self.mz.size, dtype=bool)
            for m in masses:
                mask |= np.abs(self.mz - m) <= tol
        return np.bincount(self._scan_index()[mask], weights=self.ab[mask],
                           minlength=self.n_scans)

    def bpc(self) -> np.ndarray:
        out = np.zeros(self.n_scans)
        nonempty = np.diff(self.ptr) > 0
        if self.ab.size:
            maxima = np.maximum.reduceat(self.ab, self.ptr[:-1][nonempty])
            out[nonempty] = maxima
        return out

    # -- spectra -----------------------------------------------------------

    def scan(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        a, b = self.ptr[i], self.ptr[i + 1]
        return self.mz[a:b], self.ab[a:b]

    def scan_at_rt(self, rt: float) -> int:
        j = int(np.searchsorted(self.rt, rt))
        if j <= 0:
            return 0
        if j >= self.n_scans:
            return self.n_scans - 1
        return j if abs(self.rt[j] - rt) < abs(self.rt[j - 1] - rt) else j - 1

    def scans_between(self, t0: float, t1: float) -> np.ndarray:
        lo = int(np.searchsorted(self.rt, min(t0, t1), side="left"))
        hi = int(np.searchsorted(self.rt, max(t0, t1), side="right"))
        return np.arange(lo, hi)

    def _points_of(self, scans: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(point indices, row of each point) for a list of scans, vectorised."""
        starts = self.ptr[scans]
        counts = self.ptr[scans + 1] - starts
        total = int(counts.sum())
        if total == 0:
            return np.zeros(0, np.int64), np.zeros(0, np.int64)
        rows = np.repeat(np.arange(scans.size), counts)
        offsets = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
        return np.repeat(starts, counts) + offsets, rows

    def nominal_spectrum_arrays(self, scans, weights=None) -> tuple[np.ndarray, np.ndarray]:
        """Mean nominal-mass spectrum over ``scans`` as sorted ``(mz, abundance)`` arrays."""
        scans = np.atleast_1d(np.asarray(scans, dtype=np.int64))
        if scans.size == 0:
            return np.zeros(0, np.int64), np.zeros(0)
        w = np.ones(scans.size) if weights is None else np.asarray(weights, dtype=float)
        idx, rows = self._points_of(scans)
        if idx.size == 0:
            return np.zeros(0, np.int64), np.zeros(0)
        nom = self.nom[idx]
        uniq, inv = np.unique(nom, return_inverse=True)
        sums = np.bincount(inv, weights=self.ab[idx] * w[rows])
        return uniq.astype(np.int64), sums / (float(w.sum()) or 1.0)

    def nominal_spectrum(self, scans, weights=None) -> dict[int, float]:
        """Mean nominal-mass spectrum over ``scans`` (optionally weighted)."""
        mz, ab = self.nominal_spectrum_arrays(scans, weights)
        return dict(zip(mz.tolist(), ab.tolist()))

    def nominal_block(self, scans, lo: int, hi: int, dtype=float) -> np.ndarray:
        """Dense (len(scans), hi-lo+1) matrix of nominal-mass abundances."""
        scans = np.atleast_1d(np.asarray(scans, dtype=np.int64))
        width = hi - lo + 1
        out = np.zeros(scans.size * width, dtype=dtype)
        idx, rows = self._points_of(scans)
        if idx.size:
            nom = self.nom[idx]
            keep = (nom >= lo) & (nom <= hi)
            np.add.at(out, rows[keep] * width + (nom[keep] - lo), self.ab[idx][keep])
        return out.reshape(scans.size, width)

    def dense_block(self, s0: int, s1: int, lo: int, hi: int, dtype=float) -> np.ndarray:
        """Dense matrix of the contiguous scans ``s0..s1`` (inclusive) and masses ``lo..hi``."""
        return self.nominal_block(np.arange(max(0, s0), min(self.n_scans - 1, s1) + 1), lo, hi, dtype)

    def dense(self) -> tuple[np.ndarray, int]:
        """The whole run as a dense float32 (scans x masses) matrix and its lowest mass.

        Built once and cached (a 4 000-scan run over m/z 35-700 is ~10 MB).
        """
        cached = getattr(self, "_dense", None)
        if cached is None:
            lo, hi = self.mass_range()
            cached = (self.dense_block(0, self.n_scans - 1, lo, hi, np.float32), lo)
            self._dense = cached
        return cached

    def min_abundance(self) -> float:
        """Smallest recorded abundance: the acquisition threshold of centroided data."""
        v = getattr(self, "_min_ab", None)
        if v is None:
            pos = self.ab[self.ab > 0]
            v = float(np.percentile(pos, 0.5)) if pos.size else 0.0
            self._min_ab = v
        return v

    def mass_range(self) -> tuple[int, int]:
        if self.nom.size == 0:
            return (0, 0)
        return int(self.nom.min()), int(self.nom.max())

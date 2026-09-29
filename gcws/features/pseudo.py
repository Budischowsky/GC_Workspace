"""The pseudo spectrum of a peak: only the ions that elute with it.

mzmine compares GC-EI features by their *pseudo spectra* from its spectral deconvolution
(``featdet_spectraldeconvolutiongc``, "RT grouping and shape correlation"): the ions whose
chromatographic profile correlates with the main ion (Pearson r >= 0.8 over the common
retention-time range) form one spectrum. mzmine, MIT, Copyright (c) 2004-2025 The mzmine
Development Team.

A background-subtracted peak spectrum still carries what is left of the column bleed and of
the noise, mostly at high m/z -- exactly the masses the NIST GC weights (m/z^3) emphasise, so
two injections of the same substance can look different. Keeping only the co-eluting ions
removes that: a flat bleed ion or a noise spike does not follow the peak's shape.

The intensities stay those of the peak spectrum (:func:`gcws.ms.spectra.extract`), so an
analyst's pinned spectrum or a deconvolution component keeps its values; only ions are left
out. With fewer than ``min_ions`` co-eluting ions the MS signal is too weak to compare and
the result is None.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    x = np.asarray(x, float) - np.mean(x)
    y = np.asarray(y, float) - np.mean(y)
    den = float(np.sqrt(np.dot(x, x) * np.dot(y, y)))
    return float(np.dot(x, y) / den) if den > 0 else 0.0


def coeluting(ms, t0: float, t1: float, spectrum, min_r: float = 0.8, pad: int = 2,
              min_scans: int = 5, min_ions: int = 3) -> Optional[tuple]:
    """``(mz, ab)`` of ``spectrum`` restricted to the ions whose profile over the peak (MS time
    ``t0``..``t1``, ``pad`` scans either side) correlates with the main ion by at least
    ``min_r``; None when too few ions (or scans) remain."""
    if spectrum is None or ms is None:
        return None
    mz, ab = np.asarray(spectrum[0], np.int64), np.asarray(spectrum[1], float)
    if mz.size == 0 or ab.max() <= 0:
        return None
    scans = ms.scans_between(t0, t1)
    if scans.size == 0:
        return None
    s0, s1 = max(0, int(scans[0]) - pad), min(ms.n_scans - 1, int(scans[-1]) + pad)
    if s1 - s0 + 1 < min_scans:
        return None
    lo, hi = int(mz.min()), int(mz.max())
    block = ms.dense_block(s0, s1, lo, hi)
    main = int(mz[int(np.argmax(ab))])
    ref = block[:, main - lo]
    if not np.any(ref > 0):
        return None
    keep = np.zeros(mz.size, bool)
    for k, m in enumerate(mz):
        if m == main:
            keep[k] = True
            continue
        prof = block[:, int(m) - lo]
        if np.count_nonzero(prof) >= 3 and pearson(prof, ref) >= min_r:
            keep[k] = True
    if int(keep.sum()) < min_ions:
        return None
    return mz[keep], ab[keep]

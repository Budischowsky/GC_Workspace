"""Display masks only: never modify spectra used for analysis or export."""
import numpy as np


def visible_ions(mz, abundance, hide_noise=True):
    mz, ab = np.asarray(mz), np.asarray(abundance, float)
    keep = np.isfinite(ab) & (ab > 0)
    if not hide_noise or len(mz) < 32:
        return keep
    width = float(np.max(mz) - np.min(mz) + 1)
    if width <= 0 or len(np.unique(mz)) / width < 0.8:
        return keep
    values = ab[np.isfinite(ab)]
    if values.size:
        floor = np.median(values)
        threshold = floor + 3 * 1.4826 * np.median(np.abs(values - floor))
        keep &= ab > threshold
    return keep

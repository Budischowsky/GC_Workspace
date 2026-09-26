"""Per-source numeric caches: adding a personal spectrum reuses vendor indexes."""
import hashlib
import json
from pathlib import Path
import numpy as np

CACHE_VERSION = 4


def _cache_name(path, root):
    """Relative inside the project (unchanged cache keys), absolute outside it."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(Path(path).resolve())


def open_index(root, source, records, count, progress, reader=None):
    mmap_mode = 'r' if count >= 10000 else None
    fingerprint = hashlib.sha256(json.dumps([(_cache_name(p, root), p.stat().st_size, p.stat().st_mtime_ns)
                                            for p in source] + [CACHE_VERSION]).encode()).hexdigest()
    folder = Path(root) / '.cache' / fingerprint
    names = ('rows', 'intensities', 'pointers')
    try:
        if json.loads((folder / 'manifest.json').read_text())['count'] == count:
            arrays = {name: np.load(folder / (name + '.npy'), mmap_mode=mmap_mode, allow_pickle=False) for name in names}
            if reader is not None and hasattr(reader, 'legacy'):
                reader.scan_offsets = np.load(folder / 'scan_offsets.npy', allow_pickle=False).tolist()
            return arrays
    except (OSError, ValueError, KeyError):
        pass
    progress('Building index: ' + source[0].parent.name)
    masses, intensities, rows = [], [], []
    for row, (mz, abundance) in enumerate(records()):
        nominal = np.floor(np.asarray(mz) + .5).astype(np.int32)
        unique, inverse = np.unique(nominal, return_inverse=True)
        merged = np.bincount(inverse, weights=np.asarray(abundance, dtype=np.float64))
        good = (unique > 0) & (merged > 0)
        unique, merged = unique[good], merged[good]
        if not len(unique) or unique[-1] > 10000:
            raise ValueError(f'Unusable nominal peaks in record {row + 1}.')
        masses.append(unique.astype(np.uint16))
        intensities.append((merged / merged.max() * 100).astype(np.float32))
        rows.append(np.full(len(unique), row, dtype=np.int32))
    if len(rows) != count:
        raise ValueError('Index spectrum count mismatch.')
    if masses:
        mz = np.concatenate(masses)
        masses.clear()
        order = np.argsort(mz, kind='stable')
        counts = np.bincount(mz, minlength=10001)
        del mz
        row_array = np.concatenate(rows)[order]
        rows.clear()
        intensity_array = np.concatenate(intensities)[order]
        intensities.clear()
    else:
        row_array, intensity_array = np.array([], dtype=np.int32), np.array([], dtype=np.float32)
        counts = np.zeros(10001, dtype=np.int64)
    arrays = dict(rows=row_array, intensities=intensity_array, pointers=np.r_[0, np.cumsum(counts)])
    folder.mkdir(parents=True, exist_ok=True)
    for name, values in arrays.items():
        np.save(folder / (name + '.npy'), values, allow_pickle=False)
    if reader is not None and hasattr(reader, 'legacy'):
        np.save(folder / 'scan_offsets.npy', np.asarray(reader.scan_offsets), allow_pickle=False)
    (folder / 'manifest.json').write_text(json.dumps({'count': count}))
    return {name: np.load(folder / (name + '.npy'), mmap_mode=mmap_mode, allow_pickle=False) for name in names}

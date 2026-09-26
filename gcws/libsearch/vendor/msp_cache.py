"""Parsed MSP reference files, cached as arrays next to the spectral index.

``parse_msp`` is strict and therefore slow: the 139 MB predicted-spectra MSP
took about 100 s, and it was parsed again on every server start. The records
are parsed once and stored under the file's fingerprint (path, size, mtime), so
later starts read three arrays and one JSON list; the parser stays the only
authority on what a record contains. Small files (personal library entries)
are parsed directly -- a cache folder per entry would cost more than it saves.
"""
import hashlib
import json
from pathlib import Path
import numpy as np

from msp import parse_msp

CACHE_VERSION = 1
MIN_CACHED_BYTES = 1 << 20


class MspRecords:
    """Name, metadata and peaks of every record of one MSP file."""

    def __init__(self, names, metadata, mz, intensity, offsets):
        self.names, self.meta = names, metadata
        self.mz, self.intensity, self.offsets = mz, intensity, offsets
        self.count = len(names)

    @classmethod
    def from_spectra(cls, spectra):
        offsets = np.zeros(len(spectra) + 1, dtype=np.int64)
        offsets[1:] = np.cumsum([len(s.peaks) for s in spectra])
        mz = np.fromiter((m for s in spectra for m, _ in s.peaks), dtype=np.float64, count=int(offsets[-1]))
        intensity = np.fromiter((i for s in spectra for _, i in s.peaks), dtype=np.float64, count=int(offsets[-1]))
        return cls([s.name for s in spectra], [s.metadata for s in spectra], mz, intensity, offsets)

    def peaks(self, index):
        a, b = self.offsets[index], self.offsets[index + 1]
        return list(zip(self.mz[a:b].tolist(), self.intensity[a:b].tolist()))

    def arrays(self, index):
        a, b = self.offsets[index], self.offsets[index + 1]
        return self.mz[a:b], self.intensity[a:b]


def _folder(root, path):
    stat = path.stat()
    try:
        name = str(path.relative_to(root))
    except ValueError:
        name = str(path.resolve())
    key = json.dumps([name, stat.st_size, stat.st_mtime_ns, 'msp-records', CACHE_VERSION])
    return Path(root) / '.cache' / ('msp-' + hashlib.sha256(key.encode()).hexdigest()[:32])


def load(root, path, progress=lambda text: None):
    """The records of ``path``, from the cache when the file is unchanged."""
    path = Path(path)
    if path.stat().st_size < MIN_CACHED_BYTES:
        return MspRecords.from_spectra(parse_msp(path.read_bytes()))
    folder = _folder(root, path)
    try:
        records = json.loads((folder / 'records.json').read_text(encoding='utf-8'))
        arrays = [np.load(folder / f'{n}.npy', allow_pickle=False) for n in ('mz', 'intensity', 'offsets')]
        if len(arrays[2]) == len(records['names']) + 1:
            return MspRecords(records['names'], records['metadata'], *arrays)
    except (OSError, ValueError, KeyError):
        pass
    progress(f'Reading {path.name} (first time only)')
    parsed = MspRecords.from_spectra(parse_msp(path.read_bytes()))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('mz', 'intensity', 'offsets'):
            np.save(folder / f'{name}.npy', getattr(parsed, name), allow_pickle=False)
        # Written last: its presence marks a complete cache entry.
        (folder / 'records.json').write_text(json.dumps(
            {'names': parsed.names, 'metadata': parsed.meta}, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass  # a read-only folder only costs the parse next time
    return parsed

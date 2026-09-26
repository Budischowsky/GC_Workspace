"""Reader for the supplied Wiley Shimadzu LH3/SP2/NM2/FM2 library layout.

Spectra are stored in search order, NOT name order. The embedded one-based
compound ID maps each spectrum to the name/formula/INF records. A zero pair
advances the mass page by 255. All boundaries, IDs, MW and base peaks are
validated before a source is indexed. Original files remain read-only.
"""
from pathlib import Path
import struct
import numpy as np


def u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


class ShimadzuLibrary:
    def __init__(self, path):
        self.folder = Path(path).with_suffix('')
        self.files = [self.folder.with_suffix('.' + ext) for ext in ('lib', 'spc', 'nam', 'fom')]
        self.info, self.data, self.names, self.formulas = [p.read_bytes() for p in self.files]
        if self.info[:4] != b'LH\x03\0' or self.info[28:32] != b'INF\0':
            raise ValueError('Unsupported Shimadzu library header.')
        self.count = u32(self.info, 36) // 16
        self.info_start = 40 + u32(self.info, 32)
        if u32(self.info, 36) % 16 or self.info_start + self.count * 16 > len(self.info):
            raise ValueError('Invalid Shimadzu compound table.')
        if not self.count:
            self.scan_offsets = []
            return
        self.tables = {}
        for key, data, signature in [('spc', self.data, b'SP\x02\0'), ('nam', self.names, b'NM\x02\0'), ('fom', self.formulas, b'FM\x02\0')]:
            if data[:4] != signature or u32(data, 20) != self.count * 4:
                raise ValueError('Shimadzu record counts or versions disagree.')
            start = 28 + self.count * 4
            if start > len(data):
                raise ValueError('Truncated Shimadzu index.')
            offsets = np.frombuffer(data, dtype='<u4', count=self.count, offset=28).astype(np.int64)
            ends = np.r_[offsets[1:], len(data) - start]
            if self.count and (offsets[0] != 0 or np.any(ends <= offsets) or ends[-1] > len(data) - start):
                raise ValueError('Invalid Shimadzu record boundaries.')
            self.tables[key] = (start, offsets, ends)
        self.scan_offsets = []
        if self.count:
            start, offsets, _ = self.tables['spc']
            self.ids = np.array([u32(self.data, start + int(o) + 4) for o in offsets], dtype=np.int32)
            if not np.array_equal(np.sort(self.ids), np.arange(1, self.count + 1)):
                raise ValueError('Shimadzu spectrum IDs do not map uniquely to compound records.')

    def record(self, key, row):
        start, offsets, ends = self.tables[key]
        data = {'spc': self.data, 'nam': self.names, 'fom': self.formulas}[key]
        return data[start + int(offsets[row]):start + int(ends[row])]

    def metadata(self, row):
        compound = int(self.ids[row]) - 1
        name, formula = self.record('nam', compound), self.record('fom', compound)
        if len(name) < 2 or struct.unpack_from('<H', name)[0] != len(name) - 2 or not formula or formula[0] != len(formula) - 1:
            raise ValueError(f'Invalid Shimadzu text record {compound + 1}.')
        names = name[2:].decode('cp1252', errors='replace').split(' $$ ')
        o = self.info_start + compound * 16
        cas = str(u32(self.info, o + 4))
        mw = struct.unpack_from('<H', self.info, o + 8)[0]
        return dict(name=names[0], formula=formula[1:].decode('ascii'),
                    comment='Synonyms: ' + ' | '.join(names[1:]) if len(names) > 1 else '',
                    cas=f'{cas[:-3]}-{cas[-3:-1]}-{cas[-1]}' if len(cas) >= 4 else '',
                    mw=mw, source=self.folder.name, library_id=compound + 1)

    def decode(self, row):
        data = self.record('spc', row)
        mw, count, compound, base = struct.unpack_from('<HHIH', data)
        if len(data) != 10 + count * 2 or not count:
            raise ValueError(f'Invalid Shimadzu spectrum size {row + 1}.')
        page, masses, intensities = 0, [], []
        for intensity, mass in zip(data[10::2], data[11::2]):
            if intensity == mass == 0:
                page += 255
                continue
            masses.append(page + mass)
            intensities.append(intensity)
        if (not masses or any(m <= 0 or m > 10000 for m in masses)
                or any(a >= b for a, b in zip(masses, masses[1:]))
                or max(intensities) != 250 or (base, 250) not in zip(masses, intensities)):
            raise ValueError(f'Invalid Shimadzu masses or base peak {row + 1}.')
        if mw != struct.unpack_from('<H', self.info, self.info_start + (compound - 1) * 16 + 8)[0]:
            raise ValueError(f'Shimadzu molecular weight mapping mismatch {row + 1}.')
        return masses, intensities

    def records(self, progress=lambda text: None):
        for row in range(self.count):
            self.metadata(row)
            yield self.decode(row)
            if row % 10000 == 0:
                progress(f'Validating {self.folder.name}: {row + 1:,} / {self.count:,}')

    def peaks(self, row):
        return list(zip(*self.decode(row)))

"""Validated reader for the supplied legacy and newer HP/Agilent layouts.

FULL.D uses ChemStation centroid records (big-endian, m/z * 20,
14-bit intensity mantissa and two-bit base-8 exponent). Header offsets
were inferred from this supplied library and checked across every record.
Other vendor/library versions are deliberately rejected if invariants fail.
"""
from pathlib import Path
import struct
import numpy as np


def u16(b, o):
    return struct.unpack_from('>H', b, o)[0]


def u32(b, o):
    return struct.unpack_from('>I', b, o)[0]


class AgilentLibrary:
    def __init__(self, folder):
        self.folder = Path(folder)
        paths = {p.name.lower(): p for p in self.folder.iterdir() if p.is_file()}
        self.files = [paths[n] for n in ('header', 'header.ind', 'full.d')]
        self.header, index, self.data = [p.read_bytes() for p in self.files]
        if index[:3] != b'\x0222' or self.header[:3] != b'\x0221' or self.data[:2] != b'\x012':
            raise ValueError('Unsupported Agilent library version; export it as MSP using LIB2NIST.')
        self.count = u32(index, 4)
        if not 0 < self.count <= 1000000 or len(index) < 8 + 4 * self.count:
            raise ValueError('Invalid library index size.')
        if u32(self.data, 278) != self.count:
            raise ValueError('Library header and FULL.D spectrum counts disagree.')
        self.offsets = np.frombuffer(index, dtype='>u4', offset=8, count=self.count).astype(np.int64) - 1
        self.scan_offsets = []
        # Both layouts retain indexed metadata order; newer records omit the
        # redundant metadata ID and use unscaled scan IDs without a trailer.
        first = (u32(self.data, 264) - 1) * 2
        self.legacy = u32(self.data, first + 2) == 1000

    def metadata(self, row):
        o = int(self.offsets[row])
        size = u16(self.header, o)
        end = o + size
        if end > len(self.header) or size < 60 or (self.legacy and u32(self.header, o + 22) != row + 1):
            raise ValueError(f'Invalid metadata record {row + 1}.')
        if row + 1 < self.count and end != self.offsets[row + 1]:
            raise ValueError(f'Metadata boundary mismatch at {row + 1}.')
        # Length-prefixed, NUL-terminated strings following the 54-byte fixed part.
        fields, pos = [], o + 54
        for _ in range(3):
            length = u16(self.header, pos)
            pos += 2
            if not length or pos + length > end:
                raise ValueError(f'Invalid string length in record {row + 1}.')
            fields.append(self.header[pos:pos + length].split(b'\0', 1)[0].decode('cp1252', errors='replace'))
            pos += length
        cas = str(u32(self.header, o + 2))
        return dict(name=fields[0], formula=fields[1], comment=fields[2],
                    mw=u32(self.header, o + 6) / 1000,
                    cas=f'{cas[:-3]}-{cas[-3:-1]}-{cas[-1]}' if len(cas) >= 4 else '',
                    source=self.folder.name, library_id=row + 1)

    def records(self, progress=lambda x: None):
        self.scan_offsets = []
        offset = (u32(self.data, 264) - 1) * 2
        for row in range(self.count):
            size, count = u16(self.data, offset) * 2, u16(self.data, offset + 12)
            if size != (28 if self.legacy else 18) + count * 4 or offset + size > len(self.data) or u32(self.data, offset + 2) != (row + 1) * (1000 if self.legacy else 1):
                raise ValueError(f'Invalid spectral record {row + 1}.')
            values = np.frombuffer(self.data, dtype='>u2', count=count * 2, offset=offset + 18).reshape(-1, 2)
            masses = values[:, 0].astype(np.float64) / 20
            raw = values[:, 1].astype(np.uint32)
            intensities = (raw & 16383) * (8 ** (raw >> 14))
            if count == 0 or np.any(masses <= 0) or np.max(intensities) == 0:
                raise ValueError(f'Invalid peaks in record {row + 1}.')
            # Verify the independently stored base-peak mass and intensity.
            bp_raw = u16(self.data, offset + 16)
            bp = (bp_raw & 16383) * 8 ** (bp_raw >> 14)
            bp_m = u16(self.data, offset + 14) / 20
            if max(intensities) != bp or not np.any((masses == bp_m) & (intensities == bp)):
                raise ValueError(f'Base peak mismatch in record {row + 1}.')
            self.metadata(row)  # Validate the mapping, including every boundary and ID.
            self.scan_offsets.append(offset)
            yield masses, intensities
            offset += size
            if row % 10000 == 0:
                progress(f'Validating library: {row + 1:,} / {self.count:,} spectra')

    def peaks(self, row):
        offset = self.scan_offsets[row]
        values = np.frombuffer(self.data, dtype='>u2', count=u16(self.data, offset + 12) * 2, offset=offset + 18).reshape(-1, 2)
        raw = values[:, 1].astype(np.uint32)
        intensities = (raw & 16383) * (8 ** (raw >> 14))
        return [(float(m / 20), float(i)) for m, i in zip(values[:, 0], intensities)]

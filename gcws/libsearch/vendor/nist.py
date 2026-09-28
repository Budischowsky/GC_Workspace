"""Reader for the supplied NIST MS Search library layout (mainlib, replib, *.DBU).

One record file holds every spectrum: ``nist.db`` (main), ``nist.dbr``
(replicate) or ``user.dbu`` (user/Wiley sets). Records are chained by their
own length behind a 512-byte file header; each starts with the marker 0xFAFA
and carries CAS, NIST number, molecular weight, name, formula and the packed
spectrum.

Peaks are stored as an ascending m/z code stream followed by an intensity
stream. A mass code 1-203 is a nominal m/z step, 204 takes a second byte and
steps 204 + that byte, 205-255 is a run of (257 - code) consecutive m/z
values, and a leading 0xFF adds 255 to the first m/z. A closing run may reach
past the stored peak count and is cut back to it. Intensities are base-999
normalized: 0-250 directly, otherwise two bytes b, n giving 5n + (255 - b).
Names in the NIST-built libraries are compressed
with the fixed 128-entry chemical-morpheme table of NIST MS Search, which is
reproduced in TOKENS so the names can be read without that program.

Record and library counts, marker chaining, ascending masses, the m/z range
and the base-999 normalization are checked; records that fail are skipped and
reported rather than guessed at. Tandem-MS and retention-index sets share the
container but not the spectrum format and are rejected. Original files remain
read-only.
"""
from pathlib import Path
import hashlib
import json
import struct
import numpy as np

CACHE_VERSION = 2
RECORD_FILES = ('nist.db', 'nist.dbr', 'user.dbu')
TANDEM_FILES = ('precmz.inu', 'precmzb.inu', 'mzbin.dbu')
MARKER = b'\xfa\xfa'
MAX_STEP = 204
MAX_MZ = 4000

# Name compression table of NIST MS Search (nistms32.dll), byte 0x80 + index.
TOKENS = ('phenyl', 'dimethyl', 'trimethyl', 'methyl', 'ethyl', 'benzene', 'pyridine', 'propyl',
          'isobutyl', 'cyclopropane', 'cyclobutane', 'cyclopent', 'cyclohex', 'acetic', 'quino',
          'butyl', 'triazole', 'imid', 'bicyclo', 'aldehyde', 'hydride', 'cyan', 'dihydro',
          'vinyl', 'form', 'bor', 'cyclohept', 'pyrrol', 'cis', 'morpho', 'alanan', 'lactone',
          'indinone', 'thiophene', 'glycol', 'nor', 'diol', 'croto', 'spiro', 'methoxy', 'keto',
          'alcohol', 'piper', 'malon', 'prop', 'but', 'pent', 'hex', 'hept', 'oct', 'dec',
          'diene', 'ene', 'ane', 'one', 'acetate', 'ate', 'hydroxy', ' acid', 'ester', 'ether',
          'eth', 'amin', 'nitro', 'nitrile', 'phosph', 'azo', 'sulf', 'ine', 'oxo', 'oxide',
          'amide', 'furan', 'yne', 'ol', 'oic', 'naph', 'phen', 'chlor', 'fluor', 'iod', 'brom',
          'cyclo', 'ammon', 'trans', 'bi', 'thio', 'carb', ', ', 'hydr', 'mono', 'tetra', 'tri',
          'epoxy', '(e)-', '(z)-', '.alpha.', '.beta.', 'ide', '.pi.', 'yl', '.sigma.', '.mu.',
          '.gamma.', 'iso', 'oxa', '.omega.', '.delta.', 'mer', 'acet', '.epsilon.', 'pyr',
          'oxy', '.+/-.', 'sil', 'ic', 'ino', 'ano', '1,', '2,', 'en', 'gly', 'tetr', 'tert',
          '.eta.', 'sec', 'gua', 'fur')
TEXT = tuple(chr(c) for c in range(128)) + TOKENS


def expand(raw):
    return ''.join(TEXT[c] for c in raw)


def format_cas(number):
    # Registry numbers start at 50-00-0; shorter values are placeholders.
    text = str(number)
    return f'{text[:-3]}-{text[-3:-1]}-{text[-1]}' if len(text) >= 5 else ''


class NistLibrary:
    def __init__(self, folder, cache=None):
        self.folder = Path(folder)
        self.cache = Path(cache) if cache else None
        names = {p.name.lower(): p for p in self.folder.iterdir() if p.is_file()}
        tandem = sorted(set(TANDEM_FILES) & set(names))
        if tandem:
            raise ValueError('Tandem-MS library; SpectrAtlas evaluates 70 eV EI spectra only.')
        readme = names.get('readme')
        if readme and 'Retention Index Library' in readme.read_text(errors='replace'):
            raise ValueError('Retention-index metadata only; no EI spectra.')
        record_file = next((names[n] for n in RECORD_FILES if n in names), None)
        if record_file is None:
            raise ValueError('No NIST MS Search record file (nist.db, nist.dbr or user.dbu).')
        self.compressed_names = record_file.name.lower() != 'user.dbu'
        self.files = [record_file, Path(__file__)]
        self.data = record_file.read_bytes()
        if len(self.data) < 512 or MARKER not in self.data[:512 + 512]:
            raise ValueError('Unsupported NIST library header.')
        # The header count includes the trailing free-slot entry.
        stated = struct.unpack_from('<I', self.data, 20)[0] - 1
        if not 0 < stated <= 5000000:
            raise ValueError('Invalid NIST library record count.')
        self.offsets, self.skipped = self.validate(stated)
        self.count = len(self.offsets)
        if not self.count:
            raise ValueError(f'No spectrum record could be read ({stated:,} stored).')

    def validate(self, stated):
        """Return the readable records' offsets, reusing the cached scan if unchanged."""
        stat = self.files[0].stat()
        name = hashlib.sha256(json.dumps([str(self.files[0].resolve()), stat.st_size, stat.st_mtime_ns,
                                          stated, CACHE_VERSION]).encode()).hexdigest()
        store = self.cache / f'nist-{name}.json' if self.cache else None
        try:
            if store is None:
                raise ValueError('Cache disabled')
            saved = json.loads(store.read_text())
            return saved['offsets'], saved['skipped']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        offsets, skipped = [], []
        offset = self.data.index(MARKER)
        for row in range(stated):
            if self.data[offset:offset + 2] != MARKER:
                raise ValueError(f'Record chain breaks at record {row + 1}.')
            size = struct.unpack_from('<I', self.data, offset + 2)[0]
            if size < 32 or offset + size > len(self.data):
                raise ValueError(f'Invalid record size at record {row + 1}.')
            try:
                self.decode(offset)
            except (ValueError, IndexError, struct.error) as error:
                skipped.append(f'record {row + 1}: {error}')
            else:
                offsets.append(offset)
            offset += size
        if store is not None and offsets:
            try:
                store.parent.mkdir(parents=True, exist_ok=True)
                store.write_text(json.dumps(dict(offsets=offsets, skipped=skipped)))
            except OSError:
                pass
        return offsets, skipped

    def fields(self, offset):
        """Return the record's text boundaries and fixed fields."""
        end = offset + struct.unpack_from('<I', self.data, offset + 2)[0]
        name_end = self.data.index(b'\0', offset + 28, end)
        formula_end = self.data.index(b'\0', name_end + 1, end)
        return dict(end=end, name=(offset + 28, name_end), formula=(name_end + 1, formula_end),
                    peaks=formula_end + 3, cas=struct.unpack_from('<I', self.data, offset + 12)[0],
                    library_id=struct.unpack_from('<I', self.data, offset + 16)[0],
                    mw=struct.unpack_from('<H', self.data, offset + 24)[0],
                    count=struct.unpack_from('<H', self.data, formula_end + 1)[0])

    def decode(self, offset):
        field = self.fields(offset)
        count, end, i = field['count'], field['end'], field['peaks']
        if not 0 < count <= MAX_MZ:
            raise ValueError('Invalid peak count.')
        mz = 0
        while i < end and self.data[i] == 0xFF:
            mz += 255
            i += 1
        if i >= end:
            raise ValueError('Truncated first mass.')
        masses = [mz + self.data[i]]
        i += 1
        while len(masses) < count and i < end:
            code = self.data[i]
            i += 1
            if code == MAX_STEP:
                if i >= end:
                    raise ValueError('Truncated extended mass step.')
                masses.append(masses[-1] + MAX_STEP + self.data[i])
                i += 1
            elif code < MAX_STEP:
                masses.append(masses[-1] + code)
            else:
                # A closing run code may cover more than the remaining peaks.
                masses += [masses[-1] + step for step in range(1, 258 - code)]
        del masses[count:]
        if len(masses) != count or any(b <= a for a, b in zip(masses, masses[1:])):
            raise ValueError('Truncated or nonascending mass stream.')
        intensities = []
        while len(intensities) < count and i < end:
            code = self.data[i]
            if code >= 0xFB:
                if i + 1 >= end:
                    raise ValueError('Truncated intensity escape.')
                intensities.append(5 * self.data[i + 1] + (255 - code))
                i += 2
            else:
                intensities.append(code)
                i += 1
        if len(masses) != count or len(intensities) != count:
            raise ValueError('Truncated peak record.')
        if not 0 < masses[0] or masses[-1] > MAX_MZ:
            raise ValueError('Peak masses outside 1-%d.' % MAX_MZ)
        if max(intensities) != 999:
            raise ValueError('Spectrum is not normalized to the NIST base peak.')
        return masses, intensities

    def metadata(self, row):
        offset = self.offsets[row]
        field = self.fields(offset)
        return dict(name=(expand(self.data[slice(*field['name'])]) if self.compressed_names
                          else self.data[slice(*field['name'])].decode('cp1252', errors='replace')),
                    formula=self.data[slice(*field['formula'])].decode('cp1252', errors='replace'),
                    cas=format_cas(field['cas']), mw=field['mw'], comment='',
                    source=self.folder.name, library_id=field['library_id'] or int.from_bytes(self.data[offset + 10:offset + 12], 'little'))

    def records(self, progress=lambda text: None):
        for row, offset in enumerate(self.offsets):
            masses, intensities = self.decode(offset)
            yield np.asarray(masses, dtype=np.float64), np.asarray(intensities, dtype=np.float64)
            if row % 10000 == 0:
                progress(f'Validating {self.folder.name}: {row + 1:,} / {self.count:,}')

    def peaks(self, row):
        masses, intensities = self.decode(self.offsets[row])
        return [(float(m), float(i)) for m, i in zip(masses, intensities)]

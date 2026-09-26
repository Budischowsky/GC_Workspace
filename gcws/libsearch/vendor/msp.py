"""Strict, dependency-free MSP reader. Input labels never influence identification."""
from dataclasses import dataclass, field
import math
import re


@dataclass
class Spectrum:
    name: str
    peaks: list
    metadata: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
PAIR = re.compile(rf'({NUMBER})\s*(?:,|:)\s*({NUMBER})|({NUMBER})\s+({NUMBER})')


def decode(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1252')


def parse_msp(text):
    if isinstance(text, bytes):
        text = decode(text)
    spectra, meta, peaks, notes = [], {}, [], []
    expected, reading, count = None, False, 0

    def finish():
        if not meta and not peaks:
            return
        name = meta.get('name', f'Spectrum {len(spectra) + 1}')
        if expected is None:
            raise ValueError(f'{name}: missing Num Peaks field.')
        if count != expected:
            raise ValueError(f'{name}: declared {expected} peaks, found {count}. Check for a truncated file.')
        positive = [(m, i) for m, i in peaks if i > 0]
        if not positive:
            raise ValueError(f'{name}: no positive intensities.')
        if len(positive) != len(peaks):
            notes.append('Zero-intensity peaks were omitted.')
        merged = {}
        for m, i in positive:
            merged[m] = merged.get(m, 0) + i
        if len(merged) < len(positive):
            notes.append('Duplicate m/z values were summed.')
        spectra.append(Spectrum(name, sorted(merged.items()), dict(meta), list(notes)))

    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip('\ufeff')
        if not line or line.startswith(('#', '//')):
            continue
        header = re.match(r'^([A-Za-z][A-Za-z0-9 #_()/-]*):\s*(.*)$', line)
        if header:
            key, value = header.groups()
            key = re.sub(r'\s+', ' ', key.lower().strip())
            if key == 'name' and (meta or peaks):
                finish()
                meta, peaks, notes, expected, reading, count = {}, [], [], None, False, 0
            if key.replace(' ', '') in ('numpeaks', 'numberofpeaks'):
                if expected is not None:
                    raise ValueError(f'Line {line_no}: repeated Num Peaks field.')
                try:
                    expected = int(value)
                except ValueError:
                    raise ValueError(f'Line {line_no}: invalid Num Peaks value.') from None
                if not 1 <= expected <= 50000:
                    raise ValueError('Num Peaks must be between 1 and 50000.')
                reading = True
            else:
                meta[key] = value
            continue
        if not reading:
            raise ValueError(f'Line {line_no}: expected an MSP metadata field.')
        # Quoted ion annotations may contain digits; remove them before parsing.
        clean = re.sub(r'"[^"\n]*"', '', line).split('#', 1)[0]
        found = list(PAIR.finditer(clean))
        remaining = PAIR.sub('', clean).strip(' ;,\t')
        if not found or remaining:
            raise ValueError(f'Line {line_no}: malformed peak data: {line[:80]}')
        for match in found:
            a, b, c, d = match.groups()
            mz, intensity = float(a or c), float(b or d)
            if not math.isfinite(mz) or not math.isfinite(intensity) or not 0 < mz <= 10000 or not 0 <= intensity <= 1e30:
                raise ValueError(f'Line {line_no}: m/z must be positive and ≤10000; intensity must be finite and nonnegative.')
            peaks.append((mz, intensity))
            count += 1
    finish()
    if not spectra:
        raise ValueError('No spectra found. Load a text MSP file with Name and Num Peaks fields.')
    return spectra


def nominal_peaks(peaks):
    merged = {}
    for mz, intensity in peaks:
        mass = int(math.floor(mz + 0.5))
        if mass > 0:
            merged[mass] = merged.get(mass, 0.0) + intensity
    if not merged:
        raise ValueError('No peaks remain after nominal-mass binning.')
    base = max(merged.values())
    return {m: 100.0 * i / base for m, i in sorted(merged.items())}


def write_msp(spectra):
    """Serialize parsed spectra without normalizing/rounding their measured peaks."""
    records = []
    for spectrum in spectra:
        fields = dict(spectrum.metadata, name=spectrum.name)
        lines = ['Name: ' + spectrum.name]
        for key, value in fields.items():
            if key == 'name' or key.replace(' ', '') in ('numpeaks', 'numberofpeaks'):
                continue
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9 #_()/-]*', key) or any(c in str(value) for c in '\r\n\0'):
                raise ValueError('MSP metadata must contain valid field names and single-line values.')
            lines.append(key + ': ' + str(value))
        if any(c in spectrum.name for c in '\r\n\0'):
            raise ValueError('Spectrum names must be a single line.')
        lines.append(f'Num Peaks: {len(spectrum.peaks)}')
        lines.extend(f'{mz:.17g} {intensity:.17g}' for mz, intensity in spectrum.peaks)
        records.append('\n'.join(lines))
    text = '\n\n'.join(records) + '\n'
    parse_msp(text)
    return text

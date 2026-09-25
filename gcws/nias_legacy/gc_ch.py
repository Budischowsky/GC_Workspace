#!/usr/bin/env python3
"""Agilent ChemStation ``.ch`` reader for the FID trace (spec v3.0 §VI.2).

Nothing else in the codebase reads ``FID1A.ch``: the FID peaks come from the
``[INT …\\FID1A.ch]`` block of ``RESULTS.CSV``. Without the trace there is
nothing to draw and nothing to drag on, so this module is the foundation of the
whole chromatogram workspace.

Layout, measured on ``08_26017015_ECTapStrip_A.D/FID1A.ch`` (348 392 bytes) and
re-verified before this module was written:

===========================  =====================================
Magic, bytes 0..4            ``b"\\x03179"`` -- ChemStation version 179
Data offset                  ``0x1800``
Sample encoding              little-endian float64, no compression
Points                       42 781
``>f4 @0x11A``               0.344 -- run start [ms]
``>f4 @0x11E``               2 139 000.25 -- run end [ms] = 35.6500 min
``>f4 @0x122``               248 896 560.0 -- equals ``y.max()``
Derived interval             50.0000 ms -> 20.000 Hz
Signal string ``@0x1075``    ``"Front Signal"`` (Pascal, UTF-16LE)
===========================  =====================================

Two deliberate refusals to guess:

* any magic other than version 179 raises :class:`UnsupportedChVersion`. A
  guessed layout produces a plausible-looking *wrong* chromatogram, which is a
  far worse outcome than a clear refusal.
* the sampling rate is **derived** from the two header times, never hardcoded.
  20 Hz is what this instrument happens to write, not a property of the format.

The ``>f8 @0x127C`` value (1/7680) is *not* a y-scale for this version -- the
decoded values already are ChemStation's height unit -- and is ignored.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import NamedTuple, Optional

import numpy as np

# --------------------------------------------------------------------------
# Format constants
# --------------------------------------------------------------------------

CH_MAGIC = b"\x03179"        # Pascal string: length 3 + b"179"
CH_DATA_OFFSET = 0x1800

CH_T_START = 0x11A           # >f4, ms
CH_T_END = 0x11E             # >f4, ms
CH_Y_MAX = 0x122             # >f4, counts

#: Pascal UTF-16LE string naming the detector signal, e.g. "Front Signal".
CH_SIGNAL = 0x1075

#: Tolerance of the decode self-check. The header value is a float32, so it
#: carries ~7 significant digits; any wrong offset, dtype or byte order misses
#: by orders of magnitude, never by 1e-6.
Y_MAX_RTOL = 1e-6

#: Bytes per sample, little-endian float64.
_ITEM = 8


class UnsupportedChVersion(Exception):
    """The file is a ``.ch`` of a version whose layout is not known here."""


class ChDecodeError(Exception):
    """The file claims version 179 but does not decode consistently."""


class Trace(NamedTuple):
    """One detector signal, fully decoded.

    ``rt`` and ``y`` are parallel float64 arrays; ``rt`` is ascending minutes,
    ``y`` is counts in ChemStation's height unit (no scaling is applied -- see
    the module docstring). Held once per sample as ``sample.fid_trace``:
    42 781 x 8 B = 342 KB, irrelevant next to the spectra.
    """

    rt: np.ndarray
    y: np.ndarray
    hz: float
    path: Path
    signal: str

    @property
    def dt(self) -> float:
        """Sampling interval in **seconds** -- the unit areas are reported in."""
        return 1.0 / self.hz

    @property
    def n(self) -> int:
        return int(self.y.size)

    @property
    def span(self) -> tuple[float, float]:
        """(first, last) retention time in minutes."""
        return float(self.rt[0]), float(self.rt[-1])


# --------------------------------------------------------------------------
# Header helpers
# --------------------------------------------------------------------------

def _pascal_utf16(raw: bytes, offset: int) -> str:
    """Read a length-prefixed UTF-16LE string, empty on anything implausible.

    Best effort by design: the signal name is cosmetic, and a header variant
    that moves it must not stop a trace from being read.
    """
    if offset < 0 or offset >= len(raw):
        return ""
    length = raw[offset]
    end = offset + 1 + 2 * length
    if not (0 < length <= 128) or end > len(raw):
        return ""
    try:
        text = raw[offset + 1:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return ""
    return text.strip() if text.isprintable() else ""


def _version(raw: bytes) -> str:
    """The version string as the file spells it, for the refusal message."""
    if not raw:
        return "<leer>"
    length = raw[0]
    if 0 < length <= 8 and len(raw) > length:
        try:
            text = raw[1:1 + length].decode("ascii")
        except UnicodeDecodeError:
            pass
        else:
            if text.isprintable():
                return text
    return raw[:8].hex()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def read_ch(path) -> Trace:
    """Decode one ``.ch`` file into a :class:`Trace`.

    Raises :class:`UnsupportedChVersion` for a version other than 179 and
    :class:`ChDecodeError` when the decoded array contradicts the header.
    """
    path = Path(path)
    raw = path.read_bytes()

    if len(raw) < CH_DATA_OFFSET + 2 * _ITEM:
        raise ChDecodeError(
            f"{path.name}: Datei ist zu kurz für eine .ch-Datei "
            f"({len(raw)} Bytes, mindestens {CH_DATA_OFFSET + 2 * _ITEM} nötig).")

    if raw[:len(CH_MAGIC)] != CH_MAGIC:
        raise UnsupportedChVersion(
            f"{path}: nicht unterstützte ChemStation-.ch-Version "
            f"„{_version(raw)}“ (erwartet 179). Das Dateilayout wird nicht "
            f"geraten — ein falsches Layout ergibt ein plausibel aussehendes, "
            f"aber falsches Chromatogramm.")

    n = (len(raw) - CH_DATA_OFFSET) // _ITEM
    if n < 2:
        raise ChDecodeError(f"{path.name}: keine Messpunkte im Datenblock.")

    # .copy() so the array owns its buffer and is writable; 342 KB, ~0.1 ms.
    y = np.frombuffer(raw, dtype="<f8", count=n, offset=CH_DATA_OFFSET).copy()
    if not np.isfinite(y).all():
        raise ChDecodeError(
            f"{path.name}: der Datenblock enthält NaN/Inf — Offset, Datentyp "
            f"oder Bytereihenfolge passen nicht.")

    t_start, t_end, y_max_header = struct.unpack_from(">3f", raw, CH_T_START)
    dt = (t_end - t_start) / (n - 1)                     # ms between samples
    if not (dt > 0.0) or not np.isfinite(dt):
        raise ChDecodeError(
            f"{path.name}: unbrauchbare Laufzeiten im Header "
            f"(Start {t_start} ms, Ende {t_end} ms).")

    # Decode self-check: a free end-to-end checksum of offset, dtype and byte
    # order. Costs one max() and catches every layout mistake that matters.
    peak = float(y.max())
    reference = float(y_max_header)
    if abs(peak - reference) / max(abs(reference), 1.0) > Y_MAX_RTOL:
        raise ChDecodeError(
            f"{path.name}: Dekodierung fehlgeschlagen — Maximum der Kurve "
            f"{peak:.1f}, Header-Wert (0x122) {reference:.1f}. Offset, "
            f"Datentyp oder Bytereihenfolge passen nicht zu Version 179.")

    rt = (t_start + np.arange(n, dtype=np.float64) * dt) / 60000.0
    return Trace(rt=rt, y=y, hz=1000.0 / dt, path=path,
                 signal=_pascal_utf16(raw, CH_SIGNAL))


def find_fid(d_dir) -> Optional[Path]:
    """The FID signal file of a ``.D`` directory, or ``None``.

    ``FID1A.ch`` by name first; otherwise the first ``FID*.ch``, otherwise the
    first ``*.ch`` at all. §VI.19.2: if a ``.D`` ever holds a second FID signal
    the first one is taken.
    """
    d_dir = Path(d_dir)
    if not d_dir.is_dir():
        return None
    for name in ("FID1A.ch", "FID1A.CH", "fid1a.ch"):
        candidate = d_dir / name
        if candidate.is_file():
            return candidate
    files = sorted((p for p in d_dir.iterdir()
                    if p.is_file() and p.suffix.lower() == ".ch"),
                   key=lambda p: p.name.lower())
    preferred = [p for p in files if p.name.upper().startswith("FID")]
    for group in (preferred, files):
        if group:
            return group[0]
    return None


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------

def slice_index(trace: Trace, lo: float, hi: float) -> tuple[int, int]:
    """``[i, j)`` of the samples with ``lo <= rt <= hi``.

    Split out because both :func:`window` and the integrator need exactly this
    convention, and a half-sample disagreement between them would move areas.
    """
    i = int(np.searchsorted(trace.rt, lo, side="left"))
    j = int(np.searchsorted(trace.rt, hi, side="right"))
    return i, max(i, j)


def window(trace: Trace, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
    """The samples between ``lo`` and ``hi`` as **views**, never copies.

    A basic slice of a numpy array shares its buffer, so panning across a
    35-minute run costs nothing. Callers must not write into the result.
    """
    i, j = slice_index(trace, lo, hi)
    return trace.rt[i:j], trace.y[i:j]


def index_of(trace: Trace, rt: float) -> int:
    """Index of the sample nearest to ``rt``, clamped to the trace."""
    times = trace.rt
    n = times.size
    i = int(np.searchsorted(times, rt, side="left"))
    if i <= 0:
        return 0
    if i >= n:
        return n - 1
    return i - 1 if (rt - times[i - 1]) <= (times[i] - rt) else i


def decimate(rt: np.ndarray, y: np.ndarray,
             n_px: int) -> tuple[np.ndarray, np.ndarray]:
    """Reduce to at most ``2 * n_px`` points, **min and max per pixel column**.

    Plain striding drops the sample that happens to be a peak apex, so a
    zoomed-out chromatogram loses exactly the spikes the analyst is looking
    for. Taking both extremes of each column and emitting them in time order
    keeps every excursion visible while the drawn point count stays bounded.
    """
    if n_px < 1:
        raise ValueError("n_px muss mindestens 1 sein.")
    n = int(y.size)
    if n <= 2 * n_px:
        return rt, y

    block = -(-n // n_px)                 # ceil, so at most n_px columns
    full = n // block
    offsets = np.arange(full, dtype=np.intp) * block
    head = y[:full * block].reshape(full, block)
    lo = head.argmin(axis=1).astype(np.intp) + offsets
    hi = head.argmax(axis=1).astype(np.intp) + offsets

    idx = np.empty(2 * full, dtype=np.intp)
    idx[0::2] = np.minimum(lo, hi)        # time order within the column
    idx[1::2] = np.maximum(lo, hi)

    rest = n - full * block
    if rest:
        base = full * block
        tail = y[base:]
        a = base + int(tail.argmin())
        b = base + int(tail.argmax())
        idx = np.concatenate([idx, np.array(sorted((a, b)), dtype=np.intp)])

    return rt[idx], y[idx]

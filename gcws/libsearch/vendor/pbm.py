"""Probability Based Matching (PBM), the library search of Agilent ChemStation.

Published basis:
- McLafferty, Hertel & Villwock, Org. Mass Spectrom. 1974, 9, 690-702 (PBM).
  doi:10.1002/oms.1210090710
- Pesyna, Venkataraghavan, Dayringer & McLafferty, Anal. Chem. 1976, 48, 1362-1368.
  doi:10.1021/ac50003a026
- Stauffer, McLafferty, Ellis & Peterson, Anal. Chem. 1985, 57, 1056-1060
  (forward searching for mixtures). doi:10.1021/ac00283a021

Every reference peak carries information in bits: the uniqueness U of its m/z
(how rarely any reference spectrum has a peak there) plus the abundance term A
(how rarely a peak is that intense). Both probabilities are counted in the
searched reference libraries themselves, as PBM prescribes. The reverse search
checks the reference's most informative peaks in the unknown after correcting
for dilution (the reference may be one component of a mixture); peaks below the
tolerance window lose abundance bits, missing peaks lose all their bits. The
forward search checks that the unknown's most informative ions are explained by
the reference. The confidence is the mean of both, as a fraction of the bits
attainable, and is converted to Agilent's 0-99 Qual scale by ``qual``.

The parameters and the Qual conversion were calibrated against 1,053 ChemStation
PBM hits (Qual values from Agilent LIB reports) of the NIAS GC/MS samples.
Agilent's internal tables are not published, so individual Qual values can
differ from ChemStation's.
"""
import math
import numpy as np

SIGNIFICANT_PEAKS = 20      # Most informative peaks checked per spectrum.
WINDOW = 1.5                # A peak matches when found at >= expected / WINDOW.
FORWARD_WINDOW = 2.0        # An unknown ion is explained by a reference peak >= its half.
UNIQUENESS_WEIGHT = 0.5     # Bits of U relative to bits of A.
DEVIATION_PENALTY = 2.0     # Bits lost per missing abundance bit below the window.
MIN_ABUNDANCE = 1.0         # Peaks below 1% of the base peak carry no PBM information.

# Score (0-1) -> ChemStation Qual, monotone fit to the Agilent reference results.
QUAL_TABLE = ((0.0, 1), (0.30, 28), (0.40, 34), (0.50, 40), (0.55, 46), (0.60, 53),
              (0.65, 62), (0.70, 70), (0.75, 73), (0.80, 78), (0.85, 81), (0.90, 91),
              (0.95, 92), (1.0, 99))


class PeakStatistics:
    """Uniqueness (per m/z) and abundance (per percent) information in bits."""

    def __init__(self, spectra_count, occurrences, abundance_counts):
        # ``occurrences[m]``: spectra with a peak >= 1% at m/z m.
        # ``abundance_counts[a]``: peaks >= 1% whose abundance floors to a percent (0-100).
        count = max(int(spectra_count), 1)
        frequency = np.maximum(np.asarray(occurrences, dtype=np.float64) / count, 1 / count)
        self.uniqueness = -np.log2(np.minimum(frequency, 1.0))
        counts = np.asarray(abundance_counts, dtype=np.float64)
        tail = np.cumsum(counts[::-1])[::-1]
        total = tail[1] if len(tail) > 1 and tail[1] > 0 else 1.0
        self.abundance = -np.log2(np.clip(tail / total, 1 / max(total, 1), 1.0))
        self.abundance[:2] = 0.0

    @classmethod
    def from_spectra(cls, spectra):
        """Build the statistics from {m/z: percent} dictionaries."""
        occurrences, abundance, count = np.zeros(10001), np.zeros(101), 0
        for spectrum in spectra:
            count += 1
            for m, i in spectrum.items():
                if i >= MIN_ABUNDANCE and m <= 10000:
                    occurrences[m] += 1
                    abundance[min(100, int(i))] += 1
        return cls(count, occurrences, abundance)

    def u(self, m):
        return float(self.uniqueness[min(int(m), len(self.uniqueness) - 1)])

    def a(self, percent):
        return float(self.abundance[min(100, max(1, int(percent)))])

    def weight(self, m, percent):
        return UNIQUENESS_WEIGHT * self.u(m) + self.a(percent)


def _percent(spectrum):
    base = max(spectrum.values(), default=0)
    if base <= 0:
        return {}
    return {m: 100.0 * i / base for m, i in spectrum.items() if 100.0 * i / base >= MIN_ABUNDANCE}


def _significant(spectrum, stats):
    weights = {m: stats.weight(m, i) for m, i in spectrum.items()}
    return sorted(spectrum, key=lambda m: (-weights[m], m))[:SIGNIFICANT_PEAKS], weights


def reverse_confidence(unknown, reference, stats):
    """Reverse PBM: the reference's informative peaks, found in the (diluted) unknown."""
    peaks, weights = _significant(reference, stats)
    base = max(reference, key=reference.get)
    dilution = min(1.0, unknown.get(base, 0.0) / 100.0)
    attainable = sum(weights[m] for m in peaks)
    if dilution <= 0 or attainable <= 0:
        return 0.0
    bits = 0.0
    for m in peaks:
        expected, found = reference[m] * dilution, unknown.get(m, 0.0)
        if found >= expected / WINDOW:
            bits += weights[m]
        elif found > 0:
            bits += max(0.0, weights[m] - DEVIATION_PENALTY * (stats.a(expected) - stats.a(found)))
    bits -= math.log2(1 / dilution)
    return max(0.0, bits / attainable)


def forward_confidence(unknown, reference, stats):
    """Forward PBM: the unknown's informative ions, explained by the reference."""
    peaks, weights = _significant(unknown, stats)
    attainable = sum(weights[m] for m in peaks)
    if attainable <= 0:
        return 0.0
    return sum(weights[m] for m in peaks if reference.get(m, 0.0) >= unknown[m] / FORWARD_WINDOW) / attainable


def pbm_match(unknown, reference, stats):
    """(confidence, reverse, forward), each 0-1, for two {nominal m/z: abundance} spectra."""
    unknown, reference = _percent(unknown), _percent(reference)
    if not unknown or not reference:
        return 0.0, 0.0, 0.0
    reverse = reverse_confidence(unknown, reference, stats)
    forward = forward_confidence(unknown, reference, stats)
    return (reverse + forward) / 2, reverse, forward


def qual(confidence):
    """Agilent-style Qual (0-99) for a PBM confidence (0-1)."""
    confidence = min(1.0, max(0.0, confidence))
    for (x0, q0), (x1, q1) in zip(QUAL_TABLE, QUAL_TABLE[1:]):
        if confidence <= x1:
            return int(round(q0 + (q1 - q0) * (confidence - x0) / (x1 - x0)))
    return QUAL_TABLE[-1][1]

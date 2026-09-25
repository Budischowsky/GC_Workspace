"""Isotope patterns at unit mass resolution.

``pattern({"C": 20, "Cl": 2})`` gives the relative abundances of M, M+1, ...
(M normalised to 1) by convolving the isotope distributions of the elements.
The interpreter compares observed clusters with such patterns to recognise
chlorine, bromine, sulphur and silicon and to estimate the carbon number.

Centroided data are thresholded (data.ms keeps only ions above ~150 counts),
so an isotope peak that would fall below the threshold is *not observable*;
its absence is no evidence. All fits therefore take the absolute detection
limit relative to the cluster's monoisotopic peak.
"""
from __future__ import annotations

import re
from functools import lru_cache

import numpy as np

#: (nominal mass of the lightest isotope, abundances of +0, +1, +2, ... u)
ISOTOPES: dict[str, tuple[int, tuple[float, ...]]] = {
    "C": (12, (0.9893, 0.0107)),
    "H": (1, (0.999885, 0.000115)),
    "N": (14, (0.99636, 0.00364)),
    "O": (16, (0.99757, 0.00038, 0.00205)),
    "S": (32, (0.9499, 0.0075, 0.0425, 0.0, 0.0001)),
    "Si": (28, (0.92223, 0.04685, 0.03092)),
    "Cl": (35, (0.7576, 0.0, 0.2424)),
    "Br": (79, (0.5069, 0.0, 0.4931)),
    "P": (31, (1.0,)),
    "F": (19, (1.0,)),
    "I": (127, (1.0,)),
}
MASS = {el: v[0] for el, v in ISOTOPES.items()}
VALENCE = {"C": 4, "H": 1, "N": 3, "O": 2, "S": 2, "Si": 4, "Cl": 1, "Br": 1, "P": 3, "F": 1, "I": 1}


def _conv(a: np.ndarray, b: np.ndarray, n: int) -> np.ndarray:
    return np.convolve(a, b)[:n]


@lru_cache(maxsize=4096)
def _element_pattern(el: str, count: int, n: int) -> tuple[float, ...]:
    base = np.array(ISOTOPES[el][1], float)
    out = np.array([1.0])
    power = base
    k = count
    while k:                                  # exponentiation by squaring
        if k & 1:
            out = _conv(out, power, n)
        k >>= 1
        if k:
            power = _conv(power, power, n)
    res = np.zeros(n)
    res[:min(n, out.size)] = out[:n]
    return tuple(res)


def pattern(counts: dict[str, int], n: int = 8) -> np.ndarray:
    """Relative abundances of M, M+1, ... M+n-1 (M = 1)."""
    out = np.zeros(n)
    out[0] = 1.0
    for el, c in counts.items():
        if c:
            out = _conv(out, np.array(_element_pattern(el, int(c), n)), n)
    if out[0] > 0:
        out = out / out[0]
    return out


def nominal_mass(counts: dict[str, int]) -> int:
    return int(sum(MASS[el] * c for el, c in counts.items()))


def parse_formula(text: str) -> dict[str, int]:
    """``"C24H38O4"`` -> {"C": 24, "H": 38, "O": 4}; unknown symbols are kept as-is."""
    out: dict[str, int] = {}
    for el, num in re.findall(r"([A-Z][a-z]?)(\d*)", text or ""):
        out[el] = out.get(el, 0) + (int(num) if num else 1)
    return out


def format_formula(counts: dict[str, int]) -> str:
    order = ["C", "H"] + sorted(k for k in counts if k not in ("C", "H"))
    return "".join(f"{el}{counts[el] if counts[el] > 1 else ''}" for el in order if counts.get(el))


def rdbe(counts: dict[str, int]) -> float:
    """Ring + double-bond equivalents (C/Si tetravalent, N/P trivalent, halogens monovalent)."""
    c = counts.get("C", 0) + counts.get("Si", 0)
    h = counts.get("H", 0) + counts.get("F", 0) + counts.get("Cl", 0) + counts.get("Br", 0) + counts.get("I", 0)
    n = counts.get("N", 0) + counts.get("P", 0)
    return c - h / 2.0 + n / 2.0 + 1.0


# -- cluster analysis ------------------------------------------------------------

#: heteroatom combinations the cluster fit distinguishes (C is fitted separately)
HETERO_COMBOS: list[dict[str, int]] = [
    {}, {"S": 1}, {"S": 2}, {"Si": 1}, {"Si": 2}, {"Si": 3}, {"Si": 4}, {"Si": 5}, {"Si": 6}, {"Si": 7},
    {"Cl": 1}, {"Cl": 2}, {"Cl": 3}, {"Cl": 4}, {"Cl": 5}, {"Cl": 6},
    {"Br": 1}, {"Br": 2}, {"Br": 3}, {"Br": 4},
    {"Br": 1, "Cl": 1}, {"Br": 1, "Cl": 2}, {"Br": 2, "Cl": 1},
]


def combo_label(combo: dict[str, int]) -> str:
    if not combo:
        return "no Cl/Br/S/Si"
    return " ".join(f"{el}{n if n > 1 else ''}" for el, n in sorted(combo.items()))


def observed_cluster(spec: dict[int, float], m: int, n: int = 8) -> np.ndarray:
    """Abundances at m, m+1, ... (absolute)."""
    return np.array([spec.get(m + k, 0.0) for k in range(n)], float)


def m1_ratio(combo: dict[str, int], n_c: float) -> float:
    """Expected (M+1)/M for ``n_c`` carbons (with ~2n_c H) plus the heteroatoms."""
    r = 0.01082 * n_c + 0.000115 * 2 * n_c
    r += 0.0079 * combo.get("S", 0) + 0.0508 * combo.get("Si", 0) + 0.0037 * combo.get("N", 0)
    return r


def carbon_estimate(i_m: float, i_m1: float, threshold: float, combo: dict[str, int] | None = None,
                    m: int | None = None) -> tuple[int, int, int] | None:
    """(n, lo, hi) carbons from the M+1/M ratio; None when M+1 is not reliably observable."""
    combo = combo or {}
    if i_m <= 0:
        return None
    # the M+1 of a small peak can vanish below the threshold: need M+1 >= ~3x threshold
    if i_m1 < 3 * threshold:
        return None
    r = i_m1 / i_m - m1_ratio(combo, 0)
    per_c = 0.01082 + 0.00023
    if r <= 0:
        return None
    n = r / per_c
    # relative uncertainty: ~12 % from spectrum skew/averaging plus the counting noise of M+1
    rel = 0.12 + min(0.5, np.sqrt(threshold / i_m1))
    lo, hi = int(max(1, np.floor(n * (1 - rel)))), int(np.ceil(n * (1 + rel)))
    if m is not None:
        hi = min(hi, m // 12)
        n = min(n, m // 12)
        lo = min(lo, hi)
    return int(round(n)), lo, hi


def _combo_plausible(combo: dict[str, int], nc: float, m: int | None) -> bool:
    """Chemical sanity of a heteroatom set with ~nc carbons in an ion of mass m.

    Rejects what an isotope fit alone would accept: a ~C31 skeleton plus Cl
    at m/z 415 (too few H left), Si4 at m/z 147 (a siloxane ion needs ~70 u
    per Si), or 15 C plus S2 at m/z 663 (more mass than H and O can supply).
    """
    if m is None:
        return True
    n_si = combo.get("Si", 0)
    if n_si and m < 65 * n_si + 8:
        return False
    rest = m - 12.0 * nc - nominal_mass(combo)
    if rest < 0:
        return False
    halo = combo.get("Cl", 0) + combo.get("Br", 0)
    # hydrogen-poor beyond a large PAH: RDBE > 0.8 C + 2
    if nc >= 4 and nc - (rest + halo) / 2.0 + 1 > 0.8 * nc + 2:
        return False
    max_rest = 2 * nc + 3 + 2 * n_si + 16 * max(4, nc // 2 + n_si)
    return rest <= max_rest


def fit_cluster(obs: np.ndarray, threshold: float, n_c: float | None = None,
                combos: list[dict[str, int]] | None = None, m: int | None = None) -> list[tuple[dict, float]]:
    """Rank heteroatom combinations by how well their pattern explains ``obs`` (M first).

    Returns ``[(combo, chi2)]`` sorted by chi2 (smaller is better). Offsets whose
    predicted *and* observed intensity are below the detection limit are ignored;
    the tolerance of each offset scales with its intensity (spectrum skew and
    averaging make ratios uncertain by ~15-20 %).
    """
    combos = combos if combos is not None else HETERO_COMBOS
    i_m = float(obs[0])
    if i_m <= 0:
        return []
    o = obs / i_m
    lim = threshold / i_m
    # M+1 fixes the carbon number *for each combination* (Si and S add to M+1 too)
    r1 = o[1] if obs.size > 1 and obs[1] >= 3 * threshold else None
    out = []
    for combo in combos:
        if m is not None and nominal_mass(combo) > m:
            continue
        c_max = (m - nominal_mass(combo)) / 12.0 if m else 60.0
        if r1 is not None:
            nc = (r1 - m1_ratio(combo, 0)) / (0.01082 + 0.00023)
        elif n_c is not None:
            nc = n_c
        else:
            nc = (m - nominal_mass(combo)) / 14.0 if m else 10.0
        nc = min(max(0.0, nc), max(0.0, c_max))
        if combo and not _combo_plausible(combo, nc, m):
            continue
        c_counts = dict(combo)
        c_counts["C"] = int(round(max(0.0, nc)))
        c_counts["H"] = 2 * c_counts["C"]
        p = pattern(c_counts, obs.size)
        chi = 0.0
        for k in range(1, obs.size):
            pk, ok = p[k], o[k]
            if pk < lim and ok < lim:
                continue                     # neither expected nor seen above the threshold
            tol = 0.18 * max(pk, ok) + lim + 0.01
            chi += ((ok - pk) / tol) ** 2
        out.append((combo, chi))
    out.sort(key=lambda x: (x[1], sum(x[0].values())))
    return out

"""Elemental-formula suggestions for a nominal molecular ion.

At unit resolution a nominal mass fits many formulas; this module only narrows
them with what the spectrum does tell: the carbon number from M+1, halogens /
S / Si from the isotope cluster, the nitrogen rule, and the chemical rules of
Kind & Fiehn (2007): valence/RDBE, element ratios (H/C, N/C, O/C ...).
The result is a short ranked list of *suggestions*.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from gcws.ms.isotopes import format_formula, pattern, rdbe


@dataclass
class FormulaCandidate:
    formula: str
    counts: dict
    rdbe: float
    fit: float                 # 0..1, isotope-pattern agreement (1 = perfect or not testable)


def candidates(m: int, obs_cluster: np.ndarray | None = None, threshold: float = 0.0,
               c_range: tuple[int, int] | None = None, fixed: dict | None = None,
               max_n: int = 4, max_o: int = 10, allow_s: int = 0, limit: int = 5,
               hints: dict | None = None) -> list[FormulaCandidate]:
    """Formulas C/H/N/O(+fixed Cl/Br/S/Si) with nominal mass ``m``.

    ``fixed``: atoms known from the isotope pattern (e.g. {"Cl": 2}); ``allow_s``
    extra S atoms to try; ``obs_cluster``: observed abundances at M, M+1, ...;
    ``hints``: ranges expected by the interpreted substance class, e.g.
    ``{"O": [4, 4], "rdbe": [6, 10]}`` -- matching formulas rank higher.
    """
    fixed = {k: v for k, v in (fixed or {}).items() if v}
    base_mass = sum({"Cl": 35, "Br": 79, "S": 32, "Si": 28, "P": 31, "F": 19}.get(el, 0) * n for el, n in fixed.items())
    rest = m - base_mass
    if rest < 12:
        return []
    c_lo, c_hi = c_range if c_range else (1, rest // 12)
    c_lo, c_hi = max(1, c_lo), min(c_hi, rest // 12)
    out: list[tuple[float, FormulaCandidate]] = []
    for s_extra in range(0, allow_s + 1):
        for n_c in range(c_lo, c_hi + 1):
            for n_n in range(0, max_n + 1):
                for n_o in range(0, max_o + 1):
                    h = rest - 12 * n_c - 14 * n_n - 16 * n_o - 32 * s_extra
                    if h < 0:
                        break
                    counts = {"C": n_c, "H": h, "N": n_n, "O": n_o}
                    for el, v in fixed.items():
                        counts[el] = counts.get(el, 0) + v
                    if s_extra:
                        counts["S"] = counts.get("S", 0) + s_extra
                    if not _plausible(counts):
                        continue
                    fit = _isotope_fit(counts, obs_cluster, threshold)
                    # prefer fewer heteroatoms at equal fit (Occam) unless the class says otherwise
                    hetero = n_n + n_o + s_extra
                    score = fit - 0.015 * hetero + _hint_bonus(counts, hints)
                    out.append((score, FormulaCandidate(format_formula(counts), counts, rdbe(counts), fit)))
    out.sort(key=lambda x: -x[0])
    return [c for _s, c in out[:limit]]


def _plausible(c: dict) -> bool:
    n_c, h = c.get("C", 0), c.get("H", 0)
    if n_c <= 0:
        return False
    d = rdbe(c)
    if d < 0 or abs(d - round(d)) > 1e-9:       # a molecular ion (odd electron) has an integer RDBE
        return False
    halo = c.get("Cl", 0) + c.get("Br", 0) + c.get("F", 0)
    if h + halo == 0 and n_c > 4:
        return False
    hc = h / n_c
    if not (0.2 <= hc <= 3.1):
        return False
    if c.get("N", 0) / n_c > 1.3 or c.get("O", 0) / n_c > 1.2 or c.get("S", 0) / n_c > 0.8:
        return False
    if d > 0.9 * n_c + 2:                       # more unsaturation than the skeleton allows
        return False
    return True


def _isotope_fit(counts: dict, obs: np.ndarray | None, threshold: float) -> float:
    if obs is None or obs.size < 2 or obs[0] <= 0:
        return 1.0
    p = pattern(counts, min(obs.size, 5))
    o = obs[: p.size] / obs[0]
    lim = threshold / obs[0]
    err, n = 0.0, 0
    for k in range(1, p.size):
        if p[k] < lim and o[k] < lim:
            continue
        tol = 0.2 * max(p[k], o[k]) + lim + 0.01
        err += ((o[k] - p[k]) / tol) ** 2
        n += 1
    if n == 0:
        return 1.0
    return float(np.exp(-0.5 * err / n))


def _hint_bonus(counts: dict, hints: dict | None) -> float:
    if not hints:
        return 0.0
    bonus = 0.0
    for key, (lo, hi) in hints.items():
        v = rdbe(counts) if key == "rdbe" else counts.get(key, 0)
        bonus += 0.06 if lo <= v <= hi else -0.04
    return bonus

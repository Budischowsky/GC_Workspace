"""Peak-level blank check: which sample peaks are also in the blank, and how strongly.

Pairs the peaks of a sample with the peaks of its blank one-to-one, like
AutoLib's blank subtraction (overlapping peaks first, then the smallest
retention-time difference, after aligning the blank in time). With MS data a
pair must also have similar spectra. For every sample peak the result says
how its area compares with the blank's:

* ``blank``  -- sample area < ``ratio_limit`` x blank area: blank level;
* ``partly`` -- up to 10 x the blank: partly from the blank;
* ``also``   -- far above the blank, but the substance is in the blank too.

This only *flags*; the NIAS quantification keeps its own blank correction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

STATUS_TEXT = {"blank": "blank level", "partly": "partly blank", "also": "also in blank"}
STATUS_LEVEL = {"blank": "bad", "partly": "warn", "also": "neutral"}


@dataclass
class BlankMatch:
    index: int                      # sample peak index
    blank_run: str
    blank_index: int
    drt: float                      # sample apex - (blank apex + shift)
    area: float
    blank_area: float
    ratio: float                    # sample area / (scale * blank area)
    cosine: Optional[float]
    status: str                     # blank | partly | also

    @property
    def text(self) -> str:
        r = "∞" if self.ratio == float("inf") else f"{self.ratio:.1f}"
        return f"{STATUS_TEXT[self.status]} ×{r}"

    @property
    def level(self) -> str:
        return STATUS_LEVEL[self.status]


def classify(ratio: float, ratio_limit: float) -> str:
    if ratio < ratio_limit:
        return "blank"
    if ratio < max(10.0, ratio_limit):
        return "partly"
    return "also"


def match(sample_peaks, blank_peaks, *, shift: float, rt_tol: float, blank_run: str = "", scale: float = 1.0,
          ratio_limit: float = 3.0, spectra: Callable[[int, int], Optional[float]] | None = None,
          spectral_min: float = 0.7) -> dict[int, BlankMatch]:
    """``{sample peak index: BlankMatch}``; ``spectra(i, j)`` returns the cosine of the two peaks' spectra."""
    cands = []
    for i, p in enumerate(sample_peaks):
        if p.negative:
            continue
        for j, b in enumerate(blank_peaks):
            if b.negative:
                continue
            bs, be, ba = b.start + shift, b.end + shift, b.apex_rt + shift
            d = p.apex_rt - ba
            if abs(d) > rt_tol:
                continue
            overlap = min(p.end, be) - max(p.start, bs) > 0
            cands.append((0 if overlap else 1, abs(d), i, j, d))
    cands.sort()
    used_s, used_b = set(), set()
    out: dict[int, BlankMatch] = {}
    for _ov, _ad, i, j, d in cands:
        if i in used_s or j in used_b:
            continue
        cos = spectra(i, j) if spectra is not None else None
        if cos is not None and cos < spectral_min:
            continue                         # same time, different substance
        used_s.add(i)
        used_b.add(j)
        area, barea = sample_peaks[i].area, blank_peaks[j].area
        ratio = area / (scale * barea) if barea > 0 else float("inf")
        out[i] = BlankMatch(i, blank_run, j, d, area, barea, ratio, cos, classify(ratio, ratio_limit))
    return out

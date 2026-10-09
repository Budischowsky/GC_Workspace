"""The program side of the comparison: each evaluated run processed by the production job
(gcws.automation.pipeline.run_job) as a single determination with its batch blanks, and the per-peak evidence
of its FID peaks."""
from __future__ import annotations

from typing import Any

#: per-peak values taken from the Peaks panel's getters (gcws.quant.peak_values.VALUES)
PEAK_KEYS = ("num", "rt", "ms_rt", "start", "end", "area", "height", "w50", "sym", "sn", "name", "cas", "score",
             "status", "library", "ri", "istd", "blank_area", "blank_ratio", "in_blank", "area_minus_blank",
             "class_hint", "origin")
N_HITS = 3


def _plain(v: Any):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return str(v)


def collect_peaks(ws, run_id: str) -> list[dict]:
    """The FID peaks of ``run_id`` with PEAK_KEYS and the top library hits, sorted by RT."""
    from gcws.core.model import FID
    from gcws.quant.peak_values import VALUES, rows_for
    out = []
    for row in rows_for(ws, run_id, FID):
        d = {}
        for k in PEAK_KEYS:
            try:
                d[k] = _plain(VALUES[k](row, ws, run_id, FID))
            except Exception:  # noqa: BLE001 - a value the panel cannot give for this peak
                d[k] = None
        hits = getattr(row.ident, "hits", None) or []
        d["hits"] = [{"name": str(h.get("name", "")), "cas": str(h.get("cas", "")), "score": _plain(h.get("score"))}
                     for h in hits[:N_HITS]]
        out.append(d)
    return sorted(out, key=lambda p: p["rt"] if p["rt"] is not None else float("inf"))


def collect(ws, members: list[str]) -> dict:
    """``run_job`` inspect hook: the evidence of the single evaluated run."""
    return {"peaks": collect_peaks(ws, members[0])}

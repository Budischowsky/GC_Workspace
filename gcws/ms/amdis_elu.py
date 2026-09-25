"""Reader for AMDIS ``.ELU`` result files (deconvoluted components).

Each component record starts with ``NAME: |SC952|CN1|MP1-MODN:66(%98.9)|...|RT13.4089|...``
followed by an optional ``RE`` profile block, ``NUM PEAKS: n`` and the spectrum as
``(m/z,abundance flags)`` tuples. Ions carrying a flag (``B`` background, ``N``
noise, ...) are uncertain and kept separately. AMDIS writes several records
for one scan (model-peak variants MP1, MP2, ...); :func:`read_elu` keeps the
first record per scan unless ``dedupe=False``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FIELD = re.compile(r"\|([A-Z]{2})([^|]*)")
_ION = re.compile(r"\((\d+),(\d+)\s*([A-Za-z]?)([\d.]*)\s*\)")


@dataclass
class EluComponent:
    scan: int
    number: int
    rt: float
    model_mz: int | None
    amount: float | None
    purity: float | None                # %
    sn: float | None
    width: float | None                 # scans
    spectrum: dict[int, float] = field(default_factory=dict)       # unflagged ions
    flagged: dict[int, float] = field(default_factory=dict)        # B/N ... flagged ions
    model: str = ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_elu(path, dedupe: bool = True) -> list[EluComponent]:
    text = Path(path).read_text(encoding="latin-1", errors="replace")
    out: list[EluComponent] = []
    cur: EluComponent | None = None
    for line in text.splitlines():
        if line.startswith("NAME:"):
            f = {k: v for k, v in _FIELD.findall(line)}
            mp = f.get("MP", "")
            m = re.search(r"MODN:(\d+)", mp)
            cur = EluComponent(scan=int(f.get("SC", "0") or 0), number=int(f.get("CN", "0") or 0),
                               rt=float(f.get("RT", "nan")), model_mz=int(m.group(1)) if m else None,
                               amount=_num(f.get("AM")), purity=_num(f.get("PC")), sn=_num(f.get("SN")),
                               width=_num((f.get("WD") or "").split()[0] if f.get("WD") else None), model=mp)
            out.append(cur)
            continue
        if cur is None:
            continue
        for mz, ab, flag, _v in _ION.findall(line):
            (cur.flagged if flag else cur.spectrum)[int(mz)] = float(ab)
    if dedupe:
        seen, unique = set(), []
        for c in out:
            if c.scan in seen:
                continue
            seen.add(c.scan)
            unique.append(c)
        out = unique
    return out


def substantial(c: EluComponent) -> bool:
    """A component with enough ions and signal to be a fair benchmark target."""
    return len(c.spectrum) >= 5 and (c.sn or 0) >= 15


def benchmark(components: list[EluComponent], found, rt_tol: float = 0.015, mf_ok: float = 800.0) -> dict:
    """Compare a deconvolution with AMDIS.

    ``found(rt) -> [(rt, {mz: ab})]`` runs the engine around ``rt`` and returns its
    components. For every AMDIS component the best match factor among the
    engine's components within ``rt_tol`` is taken. Returns recall (share of
    AMDIS components found with MF >= ``mf_ok``), the median best MF and the
    per-component details, for all components and for the substantial ones.
    """
    import statistics
    import time
    from gcws.ms.similarity import match_factor
    rows = []
    t0 = time.time()
    for c in components:
        best, drt = 0.0, None
        for rt, spec in found(c.rt):
            if abs(rt - c.rt) > rt_tol or not spec:
                continue
            mf = match_factor(c.spectrum, spec)
            if mf > best:
                best, drt = mf, rt - c.rt
        rows.append({"scan": c.scan, "rt": c.rt, "model": c.model_mz, "ions": len(c.spectrum), "sn": c.sn,
                     "mf": best, "drt": drt, "substantial": substantial(c)})
    elapsed = time.time() - t0

    def summary(sel):
        if not sel:
            return {"n": 0, "recall": 0.0, "median_mf": 0.0}
        return {"n": len(sel), "recall": sum(r["mf"] >= mf_ok for r in sel) / len(sel),
                "median_mf": statistics.median(r["mf"] for r in sel)}
    return {"all": summary(rows), "substantial": summary([r for r in rows if r["substantial"]]),
            "rows": rows, "seconds": elapsed}

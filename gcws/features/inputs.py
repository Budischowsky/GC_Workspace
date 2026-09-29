"""The determinations of a replicate group as :class:`RunInput` (reads the workspace, never changes it).

The features are built on the peaks of the quantification signal (FID by default, see
:func:`gcws.quant.service.quant_detector`), because those peaks carry the areas and
concentrations of the report. Each peak's EI spectrum is taken from the MS at its own
time (FID time minus the run's FID-MS delay), background-subtracted like every spectrum
in the workspace (:func:`gcws.ms.spectra.extract`, "average_bg"; a peak split by
deconvolution uses its component, an analyst's pinned spectrum wins).
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from gcws.features.model import GAPFILL_OPTION, PeakInfo, RunInput


def clean(spec, noise_floor: float = 0.005) -> Optional[tuple]:
    """``(mz, ab)`` without ions below ``noise_floor`` x base peak (mzmine compares mass lists,
    i.e. spectra after a noise level); None when nothing is left."""
    if spec is None:
        return None
    mz = np.asarray(getattr(spec, "mz", spec[0] if isinstance(spec, tuple) else []), dtype=np.int64)
    ab = np.asarray(getattr(spec, "ab", spec[1] if isinstance(spec, tuple) else []), dtype=float)
    if ab.size == 0 or not np.isfinite(ab).all() or ab.max() <= 0:
        return None
    keep = ab >= noise_floor * ab.max()
    return mz[keep], ab[keep]


def gapfill_ranges(st, key: str) -> list[tuple[float, float]]:
    """``(t0, t1)`` of the enabled gap-fill events of ``key``."""
    out = []
    for e in st.events(key):
        if e.enabled and e.option == GAPFILL_OPTION and e.t1 is not None:
            out.append(tuple(sorted((float(e.t0), float(e.t1)))))
    return out


def _spectra_cache(ws) -> dict:
    cache = getattr(ws, "_feature_spectra", None)
    if cache is None:
        cache = {}
        try:
            ws._feature_spectra = cache
        except Exception:  # noqa: BLE001 - a read-only object: no cache
            pass
    return cache


def peak_spectrum(ws, st, key: str, res, i: int, noise_floor: float) -> tuple:
    """``(co-eluting spectrum, full spectrum)`` of peak ``i`` (see :mod:`gcws.features.pseudo`);
    either may be None."""
    from gcws.core.keys import base_key
    from gcws.ms.assignment import override_for
    from gcws.features.pseudo import coeluting
    from gcws.ms.spectra import extract, ms_times
    p = res.peaks[i]
    override = override_for(st, key, p)
    ck = (st.id, key, res.digest, i, round(p.apex_rt, 5), repr(override), noise_floor)
    cache = _spectra_cache(ws)
    if ck not in cache:
        try:
            spec = extract(st.run, p, base_key(key), st.delay_value, "average_bg", override=override)
        except Exception:  # noqa: BLE001 - a peak without a usable spectrum
            spec = None
        full = clean(spec, noise_floor) if spec is not None and spec.ab.size else None
        pure = None
        if full is not None:
            t0, t1, _ta = ms_times(p, base_key(key), st.delay_value)
            pure = coeluting(st.run.ms, t0, t1, full)
        cache[ck] = (pure, full)
        if len(cache) > 20000:
            cache.clear()
    return cache[ck]


def collect_run(ws, run_id: str, key: str, label: str, noise_floor: float = 0.005) -> Optional[RunInput]:
    from gcws.ms.assignment import fragment_id
    st = ws.runs.get(run_id)
    res = ws.result(run_id, key) if st is not None else None
    if st is None or res is None:
        return None
    has_ms = st.run.ms is not None and st.run.ms.n_scans > 0
    idents, _ = st.ident_set(key).bind(res.peaks)
    try:
        istd = set(ws.istd_peaks(run_id, key))
    except Exception:  # noqa: BLE001 - no ISTD table
        istd = set()
    try:
        blank = set(ws.blank_level_peaks(run_id, key))
    except Exception:  # noqa: BLE001 - no blank / blank not integrated
        blank = set()
    filled = gapfill_ranges(st, key)
    peaks = []
    for i, p in enumerate(res.peaks):
        if p.negative:
            continue
        ident = idents.get(i)
        spectra = peak_spectrum(ws, st, key, res, i, noise_floor) if has_ms else (None, None)
        peaks.append(PeakInfo(
            index=i, rt=float(p.apex_rt), start=float(p.start), end=float(p.end), area=float(p.area),
            height=float(p.height), width50=float(p.width50 or 0.0), sn=p.sn, origin=p.origin,
            gapfill=any(t0 - 1e-6 <= p.apex_rt <= t1 + 1e-6 and abs(p.start - t0) < 0.02 for t0, t1 in filled),
            spectrum=spectra[0], full_spectrum=spectra[1],
            ident=ident, hits=list(getattr(ident, "hits", []) or []),
            istd=i in istd or bool(getattr(ident, "istd", "")), blank_level=i in blank,
            fragment=fragment_id(p)))
    peaks.sort(key=lambda q: q.rt)
    return RunInput(run_id, st.name, label, key, float(st.delay_value), peaks, has_ms, ws.solvent_cut(st, key))


def labels_for(ws, members: list[str]) -> list[str]:
    """A, B, ... from the run names (``_A``/``_B``), else by position."""
    from gcws.io.sequence import replicate_label
    out = []
    for i, m in enumerate(members):
        st = ws.runs.get(m)
        lab = replicate_label(st.run.path.name) if st is not None else ""
        if not lab or lab in out:
            lab = chr(ord("A") + i) if i < 26 else str(i + 1)
        out.append(lab)
    return out


def collect(ws, members: list[str], key: str, noise_floor: float = 0.005) -> list[RunInput]:
    labels = labels_for(ws, members)
    out = []
    for m, lab in zip(members, labels):
        ri = collect_run(ws, m, key, lab, noise_floor)
        if ri is not None:
            out.append(ri)
    return out

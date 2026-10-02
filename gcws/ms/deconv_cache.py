"""Deconvolution results cached per run and settings.

A window around one peak takes well under 0.1 s, a whole run ~10 s (worker).
Both are cached on ``RunState.deconv`` under the settings they were made
with; the cache is dropped when the deconvolution settings change.
"""
from __future__ import annotations

import json

from gcws.ms import deconv as D
from gcws.ms.spectra import ms_times


def settings_of(ws) -> D.DeconvSettings:
    return D.DeconvSettings.from_dict((ws.quant or {}).get("deconv"))


def _skey(settings: D.DeconvSettings) -> str:
    return json.dumps(settings.to_dict(), sort_keys=True)


def whole_run(st, settings: D.DeconvSettings):
    """Cached whole-run components or None."""
    return (getattr(st, "deconv", None) or {}).get(("run", _skey(settings)))


def compute_whole_run(st, settings: D.DeconvSettings, progress=None, cancel=None, t_min=None) -> list:
    ms = st.run.ms
    start = float(ms.rt[0]) if t_min is None else max(float(ms.rt[0]), t_min)
    comps = D.deconvolute_range(ms, start, float(ms.rt[-1]), settings, progress=progress, cancel=cancel)
    return comps


def store_whole_run(st, settings: D.DeconvSettings, comps: list) -> None:
    st.deconv[("run", _skey(settings))] = comps


def _window_key(rt_ms: float, settings: D.DeconvSettings) -> tuple:
    return ("win", round(rt_ms, 3), _skey(settings))


def cached_window(st, rt_ms: float, settings: D.DeconvSettings):
    """The cached window result around ``rt_ms`` or None (safe to call from a worker)."""
    return (getattr(st, "deconv", None) or {}).get(_window_key(rt_ms, settings))


def store_window(st, rt_ms: float, settings: D.DeconvSettings, res: D.DeconvResult) -> None:
    if len(st.deconv) > 400:
        st.deconv = {k: v for k, v in st.deconv.items() if k[0] == "run"}
    st.deconv[_window_key(rt_ms, settings)] = res


def window(st, rt_ms: float, settings: D.DeconvSettings) -> D.DeconvResult:
    res = cached_window(st, rt_ms, settings)
    if res is None:
        res = D.deconvolute_window(st.run.ms, rt_ms, settings)
        store_window(st, rt_ms, settings, res)
    return res


def probe(st, t0: float, t1: float, apex: float, settings: D.DeconvSettings) -> list:
    """The components of the closer look at the peak ``t0``..``t1`` (MS time; cached)."""
    from gcws.ms import deconv_probe
    key = ("probe", round(t0, 4), round(t1, 4), round(apex, 4), _skey(settings))
    comps = st.deconv.get(key)
    if comps is None:
        if len(st.deconv) > 400:
            st.deconv = {k: v for k, v in st.deconv.items() if k[0] == "run"}
        comps = st.deconv[key] = deconv_probe.probe(st.run.ms, t0, t1, apex, settings)
    return comps


def for_peak(st, peak, key: str, settings: D.DeconvSettings):
    """The component representing ``peak`` (whole-run result if available, else its window)."""
    allocated = D.allocated_component(st.run.ms, peak)
    if allocated is not None:
        return allocated
    t0, t1, ta = ms_times(peak, key, st.delay_value)
    comps = whole_run(st, settings)
    if comps is None:
        comps = window(st, ta, settings).components
    return D.component_for_peak(comps, t0, t1, ta)


def hidden_components(ws, st, key: str, settings: D.DeconvSettings) -> list:
    """Whole-run components without an integrated peak of ``key`` at their time."""
    comps = whole_run(st, settings)
    res = ws.result(st.id, key)
    if comps is None or res is None:
        return []
    from gcws.core.keys import is_fid
    shift = st.delay_value if is_fid(key) else 0.0
    cut = ws.solvent_cut(st, "TIC")
    return [c for c in comps if (cut is None or c.rt >= cut)
            and res.peak_at(c.rt + shift) is None]

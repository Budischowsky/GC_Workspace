"""Fitting deconvoluted component shapes to a detector trace and planning a split."""
import math
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.core.model import Baseline, Peak, Signal
from gcws.ms import component_fit as F
from gcws.ms.peak_split import plan_split, replan

MS_DT = 0.0075          # MS scan interval (2.2 scans/s)
FID_DT = 1 / 1200.0     # FID sampling (20 Hz)


def component(rt, sigma=0.012, area=1000.0, sn=500.0, mz=57):
    t = np.arange(rt - 0.3, rt + 0.3, MS_DT)
    y = np.exp(-0.5 * ((t - rt) / sigma) ** 2)
    y[y < 1e-3] = 0.0
    return SimpleNamespace(rt=rt, model_mz=mz, purity=.9, area=area, s_n=sn, n_ions=20,
                           spectrum=[(mz, 999), (mz + 14, 400)], profile_rt=t, profile_y=y)


def trace(parts, shift, stretch, sigma=0.012, noise=0.0, seed=3, t0=9.7, t1=10.3):
    """FID-like trace: Gaussians of height ``a`` at ``rt + shift`` with width ``sigma * stretch``."""
    t = np.arange(t0, t1, FID_DT)
    y = np.zeros(t.size)
    for rt, a in parts:
        y += a * np.exp(-0.5 * ((t - rt - shift) / (sigma * stretch)) ** 2)
    return t, y + np.random.default_rng(seed).normal(0, noise, t.size)


def parent(t, start, end, area=None):
    area = float(np.trapezoid(np.ones(2), [start, end])) if area is None else area
    return Peak(start=start, end=end, apex_rt=(start + end) / 2, baseline=Baseline("hold", start, 0.0, end, 0.0),
                area=area, area_raw=area)


def test_pchip_is_exact_at_knots_linear_and_never_overshoots():
    x = np.array([0., 1., 2., 3., 4.])
    np.testing.assert_allclose(F.pchip(x, 2 * x + 1, x), 2 * x + 1)
    np.testing.assert_allclose(F.pchip(x, 2 * x + 1, [.5, 2.25]), [2., 5.5])
    step = np.array([0., 0., 1., 1., 1.])
    fine = np.linspace(0, 4, 401)
    values = F.pchip(x, step, fine)
    assert values.min() >= 0 and values.max() <= 1
    assert np.all(np.diff(values) >= -1e-12)


def test_shape_trims_to_support_and_ends_at_zero():
    c = component(10.0)
    shape = F.Shape.of(c)
    assert shape.y[0] == 0 and shape.y[-1] == 0 and shape.y[1:-1].min() > 0
    assert shape.t[0] > c.profile_rt[0] and shape.t[-1] < c.profile_rt[-1]
    assert F.Shape.of(SimpleNamespace(rt=10., profile_rt=np.empty(0), profile_y=np.empty(0))) is None
    cut = F.Shape.from_arrays(10., [10., 10.01, 10.02], [1., .5, 0.])
    assert cut.t[0] < 10. and cut.y[0] == 0
    again = F.Shape.of({"rt": 10.0, "profile": shape.points()})
    np.testing.assert_array_equal(again.t, shape.t)
    np.testing.assert_array_equal(again.y, shape.y)


@pytest.mark.parametrize("offset,stretch", [(0.0, 1.0), (0.004, 0.85), (-0.006, 1.3)])
def test_fit_recovers_shift_width_and_shares(offset, stretch):
    comps = [component(10.0), component(10.022)]
    delay = 0.0066
    t, y = trace([(10.0, 400.), (10.022, 700.)], delay + offset, stretch, noise=2.0)
    fit = F.fit_trace(t, y, [F.Shape.of(c) for c in comps], delay)
    assert fit.r2 > 0.99
    assert fit.shift == pytest.approx(delay + offset, abs=0.0008)
    assert fit.stretch == pytest.approx(stretch, rel=0.05)
    np.testing.assert_allclose(fit.shares, [400 / 1100, 700 / 1100], atol=0.01)
    assert fit.collinear is None


@pytest.mark.parametrize("count", [1, 2])
def test_fit_does_not_depend_on_the_scale_of_the_ms_profiles(count):
    """MS profiles of millions of counts against a trace of a few hundred pA: the NNLS zero tolerance
    of the NIAS engine grows with max|A| * max|b|, so unscaled profiles had every amplitude (~1e-4)
    taken for zero (seen on a 16-million-count butyl methacrylate peak: R² below 0, nothing split)."""
    comps = [component(10.0), component(10.022)][:count]
    t, y = trace([(10.0, 400.), (10.022, 700.)][:count], 0.0066, 1.0, noise=2.0)
    small = [F.Shape.of(c) for c in comps]
    big = [F.Shape.from_arrays(s.rt, s.t, s.y * 1.7e7) for s in small]
    one, two = F.fit_trace_uncached(t, y, small, 0.0066), F.fit_trace_uncached(t, y, big, 0.0066)
    assert two.r2 > 0.99 and np.all(two.amplitudes > 0)
    assert two.shift == one.shift and two.stretch == one.stretch
    np.testing.assert_allclose(two.areas, one.areas, rtol=1e-9)
    np.testing.assert_allclose(two.amplitudes * 1.7e7, one.amplitudes, rtol=1e-9)


def test_fit_is_deterministic_and_masked_points_are_ignored():
    shapes = [F.Shape.of(component(10.0)), F.Shape.of(component(10.02))]
    t, y = trace([(10.0, 300.), (10.02, 500.)], 0.006, 0.9, noise=1.0)
    one = F.fit_trace(t, y, shapes, 0.006)
    two = F.fit_trace(t, y.copy(), shapes, 0.006)
    assert (one.shift, one.stretch, one.r2) == (two.shift, two.stretch, two.r2)
    np.testing.assert_array_equal(one.areas, two.areas)
    rider = (t > 10.05) & (t < 10.06)
    spiked = y + np.where(rider, 800.0, 0.0)
    masked = F.fit_trace(t, spiked, shapes, 0.006, mask=~rider)
    np.testing.assert_allclose(masked.shares, one.shares, atol=1e-3)
    assert F.fit_trace(t, spiked, shapes, 0.006).r2 < masked.r2


def standard_and_isotopologue():
    """A deuterated standard and its weak D(n-1)H isotopologue 1.2 scans later (IS1 of run 09):
    alike profiles on the MS scale, 19:1."""
    main, minor = component(10.0, sigma=0.0095, mz=66), component(10.0093, sigma=0.0064, mz=275)
    main.profile_y = main.profile_y * 19.0
    return main, minor


def test_an_alignment_the_trace_cannot_decide_follows_the_ms_signal():
    """Either component explains the FID peak with its own time shift, about equally well. Whatever
    the expected delay and wherever the coarse grid falls, the fit lines the trace up with the MS
    signal: the standard takes the peak (the coarse shift step is 0.00375 min)."""
    shapes = [F.Shape.of(c) for c in standard_and_isotopologue()]
    t, y = trace([(10.0, 400.)], 0.0068, 0.93, sigma=0.0095, noise=1.0)
    for shift0 in np.arange(0.0048, 0.0090, 0.0004):
        fit = F.fit_trace(t, y, shapes, float(shift0))
        assert fit.shares[0] > 0.9, shift0
        assert fit.shift == pytest.approx(0.0068, abs=0.001), shift0


def test_components_the_trace_cannot_resolve_are_flagged():
    shapes = [F.Shape.of(component(10.0)), F.Shape.of(component(10.0015))]
    t, y = trace([(10.0, 300.), (10.0015, 300.)], 0.0, 1.0)
    assert F.fit_trace(t, y, shapes, 0.0).collinear == (0, 1)


def test_cut_point_is_the_crossing_of_the_fitted_curves():
    t = np.linspace(0, 2, 2001)
    equal = np.vstack([np.exp(-(t - .8) ** 2 / .02), np.exp(-(t - 1.2) ** 2 / .02)])
    assert F.cut_points(t, equal, [.8, 1.2])[0] == pytest.approx(1.0, abs=1e-3)
    small_right = np.vstack([equal[0], .2 * equal[1]])
    assert 1.0 < F.cut_points(t, small_right, [.8, 1.2])[0] < 1.2
    # no crossing and no valley between the apexes: the midpoint
    flat = np.vstack([np.zeros_like(t), equal[1]])
    assert F.cut_points(t, flat, [.8, 1.2])[0] == pytest.approx(1.0)


def _plan(parts, comps, shift=0.0066, stretch=0.9, noise=1.0, key="FID", checked=None, dip=None):
    t, y = trace(parts, shift, stretch, noise=noise)
    if dip is not None:                 # the trace falls below the baseline around ``dip``
        y -= 30.0 * np.exp(-0.5 * ((t - dip) / 0.01) ** 2)
    sig = Signal(key, t, y + 100.0)
    lo, hi = 9.95, 10.08
    area = float(np.trapezoid(y[(t >= lo) & (t <= hi)], t[(t >= lo) & (t <= hi)] * 60))
    peak = Peak(start=lo, end=hi, apex_rt=10.015, baseline=Baseline("hold", lo, 100.0, hi, 100.0),
                area=area, area_raw=area)
    delay = 0.0066
    return plan_split(sig, peak, key, delay, comps, checked=checked)


def test_plan_suggests_real_components_and_allocates_by_the_trace():
    comps = [component(10.0, area=5000, mz=57), component(10.022, area=100, mz=91),
             component(10.045, area=50, sn=12, mz=207), component(10.2, mz=99)]
    plan = _plan([(10.0, 400.), (10.022, 700.), (10.045, 10.)], comps)
    assert [c.component.model_mz for c in plan.candidates] == [57, 91, 207]   # 10.2 lies outside
    assert plan.checked == [0, 1] and plan.ok, plan.problem
    assert plan.candidates[2].reason == "S/N 12 < 20" and not plan.candidates[2].suggested
    assert plan.basis == "fit" and plan.fit.r2 > 0.99
    # FID relative areas follow the FID signal, not the MS areas (5000:100)
    assert plan.shares == pytest.approx([400 / 1100, 700 / 1100], abs=0.015)
    assert math.fsum(plan.areas) == plan.peak.area
    assert 10.0066 < plan.points[0] < 10.0286
    assert "Fit to FID: R²" in plan.summary()


def test_plan_falls_back_to_ms_proportions_when_the_fit_is_poor():
    comps = [component(10.0, area=300), component(10.02, area=700)]
    t = np.arange(9.7, 10.3, FID_DT)
    y = np.where((t > 9.96) & (t < 10.07), 500.0, 0.0)        # a flat plateau: no component shape
    sig = Signal("FID", t, y)
    peak = Peak(start=9.95, end=10.08, apex_rt=10.01, baseline=Baseline("hold", 9.95, 0., 10.08, 0.),
                area=100.0, area_raw=100.0)
    plan = plan_split(sig, peak, "FID", 0.0066, comps)
    assert plan.ok and plan.basis == "ms" and plan.weights == [300, 700]
    assert "fit R²" in plan.basis_note and "MS component proportions" in plan.summary()
    assert plan.points == pytest.approx([10.0166])


def test_plan_without_profiles_uses_ms_proportions():
    comps = [SimpleNamespace(rt=r, model_mz=m, purity=.8, area=a, s_n=100, spectrum=[(m, 999)])
             for r, m, a in [(10.0, 57, 30), (10.03, 91, 70)]]
    plan = _plan([(10.0, 400.), (10.03, 700.)], comps, key="TIC", shift=0.0)
    assert plan.ok and plan.basis == "ms" and plan.fit is None
    assert plan.shares == pytest.approx([.3, .7])
    assert plan.basis_note == "no elution profile"


def test_plan_refusals_and_replanning():
    comps = [component(10.0), component(10.022), component(10.062, sn=12)]
    plan = _plan([(10.0, 400.), (10.022, 700.)], comps, dip=10.0686)
    one = replan(plan, [0])
    assert not one.ok and "nothing to split" in one.problem
    assert not replan(plan, []).ok
    empty = replan(plan, [0, 1, 2])
    assert "takes no FID signal" in empty.problem
    back = replan(plan, [0, 1])
    assert back.ok and back.shares == pytest.approx(plan.shares)
    assert back.candidates is plan.candidates
    assert "FID and TIC" in _plan([(10.0, 400.)], comps, key="EIC 57").problem
    t, y = trace([(10.0, 400.)], 0.0, 1.0)
    negative = Peak(start=9.95, end=10.08, apex_rt=10.0, baseline=Baseline("hold", 9.95, 0., 10.08, 0.),
                    area=10., area_raw=10., negative=True)
    assert "negative" in plan_split(Signal("FID", t, y), negative, "FID", 0.0066, comps).problem


def test_planned_event_replays_exactly_and_draws_the_reported_area():
    from test_integration import make, method
    from gcws.integration.deconv_split import decode, fragment_curve
    from gcws.integration.engine import integrate
    delay = 0.0066
    sig = make([(3.0, 0.011, 600), (3.025, 0.011, 900)])
    m = method()
    before = integrate(sig, m)
    parent = min(before.peaks, key=lambda p: abs(p.apex_rt - 3.015))
    comps = [component(3.0 - delay, sigma=0.0122), component(3.025 - delay, sigma=0.0122)]
    plan = plan_split(sig, parent, "FID", delay, comps)
    assert plan.ok and plan.basis == "fit", plan.problem
    np.testing.assert_allclose(plan.shares, [0.4, 0.6], atol=0.02)
    event = plan.event()
    payload = decode(event)
    assert payload["version"] == 3 and payload["basis"] == "fit"
    assert payload["fit"]["r2"] == pytest.approx(plan.fit.r2)
    assert all(len(c["profile"]) > 3 for c in payload["components"])
    result = integrate(sig, m, [event])
    parts = sorted((p for p in result.peaks if p.extra.get("deconv_component")), key=lambda p: p.start)
    assert not result.unresolved and len(parts) == 2
    assert math.fsum(p.area for p in parts) == parent.area
    assert [p.area for p in parts] == plan.areas
    assert parts[0].end == pytest.approx(plan.points[0])
    for p in parts:
        dc = p.extra["deconv_component"]
        assert dc["basis"] == "fit" and dc["parent_span"] == [parent.start, parent.end]
        assert "fitted to the FID signal" in p.extra["area_note"]
        t = np.linspace(parent.start, parent.end, 4001)
        drawn = fragment_curve(p, t)
        assert float(np.trapezoid(drawn, t * 60)) == pytest.approx(p.area_raw, rel=2e-3)


# -- the fit cache ---------------------------------------------------------------------------------

def _cache_inputs():
    shapes = [F.Shape.of(component(10.0)), F.Shape.of(component(10.03, mz=71))]
    t, y = trace([(10.0, 500.), (10.03, 300.)], 0.0066, 1.0)
    return t, y, shapes


def _assert_same_fit(a, b):
    for name in ("amplitudes", "curves", "areas"):
        np.testing.assert_array_equal(getattr(a, name), getattr(b, name))
    for name in ("shift", "stretch", "r2", "residual", "collinear"):
        assert getattr(a, name) == getattr(b, name), name


def _count_residuals(monkeypatch):
    # counts the Python grid search's residuals (the Rust one of gcws.ms.rust_fit does not call _residual)
    from gcws.ms import rust_fit
    monkeypatch.setattr(F, "fit_trace_uncached", rust_fit._installed.get("fit_trace_uncached", F.fit_trace_uncached))
    calls = []
    original = F._residual

    def counted(a, y):
        calls.append(1)
        return original(a, y)
    monkeypatch.setattr(F, "_residual", counted)
    return calls


def test_fit_cache_returns_an_equal_independent_copy():
    F.fit_cache_clear()
    t, y, shapes = _cache_inputs()
    a = F.fit_trace(t, y, shapes, 0.0066)
    b = F.fit_trace(t, y, shapes, 0.0066)
    assert a is not b
    reference = F.fit_trace_uncached(t, y, shapes, 0.0066)
    _assert_same_fit(a, reference)
    _assert_same_fit(b, reference)
    a.curves[:] = 0
    a.areas[:] = 0
    a.amplitudes[:] = 0
    _assert_same_fit(F.fit_trace(t, y, shapes, 0.0066), reference)


def test_fit_cache_hit_skips_the_search(monkeypatch):
    F.fit_cache_clear()
    t, y, shapes = _cache_inputs()
    calls = _count_residuals(monkeypatch)
    F.fit_trace(t, y, shapes, 0.0066)
    assert len(calls) > 100
    calls.clear()
    F.fit_trace(t, y, shapes, 0.0066)
    assert len(calls) == 0


def _changed(kind, t, y, shapes):
    """``(t, y, shapes, shift0, scan_dt, mask)`` with one input changed."""
    mask, shift0, scan_dt = None, 0.0066, None
    if kind == "t":
        t = t + 1e-9
    elif kind == "y":
        y = y * (1 + 1e-12)
    elif kind == "mask":
        mask = np.ones(t.size, dtype=bool)
        mask[t.size // 2] = False
    elif kind == "shape_rt":
        s = shapes[0]
        shapes = [F.Shape.from_arrays(s.rt + 1e-9, s.t, s.y), shapes[1]]
    elif kind == "shape_y":
        s = shapes[1]
        shapes = [shapes[0], F.Shape.from_arrays(s.rt, s.t, s.y * 1.001)]
    elif kind == "shift0":
        shift0 = 0.0066 + 1e-6
    elif kind == "scan_dt":
        scan_dt = 0.0074
    return t, y, shapes, shift0, scan_dt, mask


@pytest.mark.parametrize("kind", ["t", "y", "mask", "shape_rt", "shape_y", "shift0", "scan_dt"])
def test_fit_cache_key_covers_every_input(monkeypatch, kind):
    F.fit_cache_clear()
    t, y, shapes = _cache_inputs()
    F.fit_trace(t, y, shapes, 0.0066)
    calls = _count_residuals(monkeypatch)
    t2, y2, shapes2, shift0, scan_dt, mask = _changed(kind, t, y, shapes)
    got = F.fit_trace(t2, y2, shapes2, shift0, scan_dt=scan_dt, mask=mask)
    assert len(calls) > 0
    _assert_same_fit(got, F.fit_trace_uncached(t2, y2, shapes2, shift0, scan_dt=scan_dt, mask=mask))


def test_fit_cache_is_bounded(monkeypatch):
    F.fit_cache_clear()
    monkeypatch.setattr(F, "FIT_CACHE_SIZE", 3)
    t, y, shapes = _cache_inputs()
    for k in range(5):
        F.fit_trace(t, y, shapes, 0.006 + 0.0002 * k)
    assert len(F._FIT_CACHE) == 3


def test_fit_cache_is_thread_safe():
    from concurrent.futures import ThreadPoolExecutor
    F.fit_cache_clear()
    t, y, shapes = _cache_inputs()
    shifts = [0.006, 0.0066, 0.0072]
    reference = {s: F.fit_trace_uncached(t, y, shapes, s) for s in shifts}

    def work(n):
        return [(s, F.fit_trace(t, y, shapes, s)) for s in (shifts * 3)[n % 3:n % 3 + 6]]

    with ThreadPoolExecutor(8) as pool:
        results = [r for part in pool.map(work, range(8)) for r in part]
    assert len(results) == 48
    for s, fit in results:
        _assert_same_fit(fit, reference[s])


def _fit_point_by_point(t, y, shapes, shift0, scan_dt=None, mask=None):
    """The grid search as it was before the grid points were evaluated together (Oct 2026): one
    design matrix and one NNLS of the NIAS engine per point. The reference for the batched fit."""
    t, y = np.asarray(t, dtype=float), np.asarray(y, dtype=float)
    use = np.ones(t.size, dtype=bool) if mask is None else np.asarray(mask, dtype=bool)
    tu, yu = t[use], y[use]
    dt = scan_dt or min(s.scan_dt for s in shapes)
    lo, hi = F.STRETCH_RANGE

    def search(ss_of):
        logs = np.log(lo) + np.log(hi / lo) / 12 * np.arange(13)
        best, profile = None, []
        for shift in F._grid(shift0, dt * F.SHIFT_SPAN_SCANS / 4, 4):
            row = None
            for log_k in logs:
                ss = ss_of(shift, log_k)
                if row is None or ss < row[0]:
                    row = (ss, float(shift), float(log_k))
            profile.append(row)
            if best is None or row[0] < best[0]:
                best = row
        return best, profile

    def refine(ss_of, best):
        shift_step, log_step = dt * F.SHIFT_SPAN_SCANS / 4, np.log(hi / lo) / 12
        for _level in range(2):
            shift_step /= 3
            log_step /= 3
            shifts = F._grid(best[1], shift_step, 2)
            logs = np.clip(F._grid(best[2], log_step, 2), np.log(lo), np.log(hi))
            for shift in shifts:
                for log_k in logs:
                    ss = ss_of(shift, log_k)
                    if ss < best[0]:
                        best = (ss, float(shift), float(log_k))
        return best

    def ss_of(shift, log_k):
        return F._residual(F._design(shapes, tu, shift, float(np.exp(log_k))), yu)[0]

    best, profile = search(ss_of)
    found = [refine(ss_of, best)]
    if len(shapes) > 1:
        found += [refine(ss_of, start) for start in F._other_minima(profile, best, yu)]
    fits = [F.solve(t, y, shapes, shift, float(np.exp(log_k)), use) for _ss, shift, log_k in found]
    top = max(f.r2 for f in fits)
    tied = [f for f in fits if f.r2 >= top - F.ALIGN_TIE_R2]
    if len(tied) == 1:
        return tied[0]

    def ms_ss(shift, log_k):
        return F._residual(F._design(shapes, tu, shift, float(np.exp(log_k))).sum(axis=1)[:, None], yu)[0]

    ms_shift = refine(ms_ss, search(ms_ss)[0])[1]
    return min(tied, key=lambda f: abs(f.shift - ms_shift))


def _same_fit(a, b):
    assert (a.shift, a.stretch, a.r2, a.residual, a.collinear) == (b.shift, b.stretch, b.r2, b.residual, b.collinear)
    for name in ("amplitudes", "curves", "areas"):
        assert getattr(a, name).tobytes() == getattr(b, name).tobytes(), name


@pytest.mark.parametrize("case", ["one", "two", "three", "tie", "masked", "outside", "negative"])
def test_batched_grid_search_is_the_point_by_point_one(case):
    """The grid points are evaluated together (stacked pseudo-inverses for one component), with
    the same result to the last bit as one NNLS per point."""
    mask = None
    if case == "tie":
        shapes = [F.Shape.of(c) for c in standard_and_isotopologue()]
        t, y = trace([(10.0, 400.)], 0.0068, 0.93, sigma=0.0095, noise=1.0)
    else:
        rts = {"one": [10.0], "two": [10.0, 10.02], "three": [9.98, 10.0, 10.03], "masked": [10.0, 10.02],
               "outside": [10.0], "negative": [10.0]}[case]
        shapes = [F.Shape.of(component(rt)) for rt in rts]
        t, y = trace([(rt, 300. + 100 * i) for i, rt in enumerate(rts)], 0.006, 0.9, noise=2.0)
        if case == "masked":
            mask = ~((t > 10.05) & (t < 10.06))
        if case == "outside":                 # the shape never reaches the window: empty designs
            t, y = t + 5.0, y
        if case == "negative":                # no positive amplitude fits: x = 0
            y = -y
    for shift0 in (0.0, 0.006, 0.0071):
        _same_fit(F.fit_trace_uncached(t, y, shapes, shift0, mask=mask),
                  _fit_point_by_point(t, y, shapes, shift0, mask=mask))

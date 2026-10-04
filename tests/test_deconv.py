"""The active engine must reproduce the original NIAS results: the same components and
spectra, with floating-point figures equal up to rounding."""
import numpy as np
import pytest

from gcws.io.ms_matrix import MSMatrix
from gcws.ms import deconv as D

DT = 1 / (2.23 * 60)                      # 2.23 scans/s, like the reference method
N = 400
RT = 10.0 + np.arange(N) * DT
THRESHOLD = 150.0
K_TRUE = 3.0

SPEC_A = {43: 40, 50: 100, 77: 60, 105: 35, 120: 25, 151: 10}
SPEC_B = {43: 30, 57: 100, 71: 70, 85: 45, 99: 25, 142: 12}


def _profile(apex_scan, width_scans=4.0):
    idx = np.arange(N)
    return np.exp(-0.5 * ((idx - apex_scan) / (width_scans / 2.355)) ** 2)


def build(components, background=True, skew=0.0, seed=7, k=K_TRUE):
    """``components``: [(apex scan, spectrum dict, height)]; returns an MSMatrix."""
    rng = np.random.default_rng(seed)
    masses = sorted({m for _a, s, _h in components for m in s} | ({73, 207} if background else set()))
    x = np.zeros((N, len(masses)))
    for apex, spec, height in components:
        for m, rel in spec.items():
            shifted = apex + skew * (m - 100)
            x[:, masses.index(m)] += height * rel / 100.0 * _profile(shifted)
    if background:
        x[:, masses.index(207)] += 2000.0                              # constant column bleed
        x[:, masses.index(73)] += np.linspace(800.0, 1600.0, N)       # sloped background
    noisy = x + rng.normal(0.0, 1.0, x.shape) * k * np.sqrt(np.maximum(x, 1.0))
    noisy[noisy < THRESHOLD] = 0.0                                     # centroided, thresholded data
    mzs, abs_ = [], []
    for i in range(N):
        nz = np.flatnonzero(noisy[i] > 0)
        mzs.append(np.array([masses[j] + 0.1 for j in nz], float))
        abs_.append(noisy[i, nz])
    return MSMatrix._build(RT, noisy.sum(axis=1), mzs, abs_)


class _DataMSAdapter:
    """The DataMS interface the legacy engine reads."""

    def __init__(self, ms):
        self.ms = ms
        self.rt = list(ms.rt)

    def spectrum(self, i):
        mz, ab = self.ms.scan(i)
        return [(float(m), float(a)) for m, a in zip(mz, ab)]


def assert_same(actual, expected):
    """The same components, model ions, apex scans, ion counts and spectra; the
    floating-point figures may differ by rounding only."""
    assert len(actual) == len(expected)
    for a, b in zip(actual, expected):
        for name in ("apex_scan", "model_mz", "spectrum", "n_ions"):
            assert getattr(a, name) == getattr(b, name), name
        for name in ("rt", "area", "purity", "s_n"):
            assert getattr(a, name) == pytest.approx(getattr(b, name), rel=1e-10, abs=0.0), name
        np.testing.assert_array_equal(a.profile_rt, b.profile_rt)
        scale = max(float(np.max(np.abs(b.profile_y), initial=0.0)), 1.0)
        np.testing.assert_allclose(a.profile_y, b.profile_y, rtol=0.0, atol=1e-10 * scale)


@pytest.mark.parametrize("sep", [0.8, 1.0, 1.5, 3.0])
def test_coeluting_pair_matches_original_nias(sep):
    import gc_deconv
    ms = build([(200.0, SPEC_A, 60000.0), (200.0 + sep, SPEC_B, 45000.0)])
    actual = D.deconvolute_window(ms, float(RT[200]))
    expected = gc_deconv.deconvolute(_DataMSAdapter(ms), float(RT[200]), gc_deconv.DeconvParams())
    assert actual.components
    assert_same(actual.components, expected)
    assert_same(D.deconvolute_window(ms, float(RT[200])).components, expected)


def test_repeated_savgol_uses_one_coefficient_calculation(monkeypatch):
    """Different traces with the same smoothing settings need one matrix inversion."""
    import gc_deconv

    pinv = np.linalg.pinv
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return pinv(*args, **kwargs)

    monkeypatch.setattr(np.linalg, "pinv", counted)
    x = np.linspace(-2.0, 2.0, 40)
    a = gc_deconv.savgol(np.sin(x), width=9, order=3)
    b = gc_deconv.savgol(np.cos(x), width=9, order=3)
    assert a.shape == b.shape == x.shape
    assert np.isfinite(a).all() and np.isfinite(b).all()
    assert calls <= 1


def test_zero_quartile_ion_noise_skips_sort(monkeypatch):
    """Sparse, nonnegative ion traces have an exact zero MAD without sorting."""
    import gc_deconv

    calls = 0
    original_sort = np.sort

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_sort(*args, **kwargs)

    monkeypatch.setattr(np, "sort", counted)
    col = np.zeros(80)
    col[::12] = np.arange(1.0, 8.0)
    assert gc_deconv._ion_sigma(col) == 0.0
    assert calls == 0


def test_repeated_savgol_reuses_edge_weights(monkeypatch):
    """Repeated smoothing settings should form each polynomial evaluation weight once."""
    import gc_deconv

    calls = 0
    original = gc_deconv._sg_weights

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(gc_deconv, "_sg_weights", counted)
    x = np.linspace(-2.0, 2.0, 50)
    gc_deconv.savgol(np.sin(x), width=13, order=5)
    gc_deconv.savgol(np.cos(x), width=13, order=5)
    assert calls <= 13


@pytest.mark.parametrize("rt", [10.0, 13.0, 13.409, 20.0, 30.0])
def test_real_data_matches_original_reader(run07, rt):
    import gc_deconv
    expected = gc_deconv.deconvolute(run07.ms_source, rt, gc_deconv.DeconvParams())
    assert_same(D.deconvolute_window(run07.ms, rt).components, expected)


def test_custom_settings_and_settings_migration():
    import gc_deconv
    s = D.DeconvSettings(window=0.2, noise_factor=4.0, shape_r=0.95, min_ions=4, apex_tol=0.4)
    assert D.DeconvSettings.from_dict(s.to_dict()) == s
    assert D.DeconvSettings.from_dict({"shape_r": 0.8, "apex_tol": 0.7,
                                      "residual_passes": 1, "skew": True}) == D.DeconvSettings()
    assert D.DeconvSettings().params() == gc_deconv.DeconvParams()
    ms = build([(200.0, SPEC_A, 60000.0)])
    expected = gc_deconv.deconvolute(_DataMSAdapter(ms), float(RT[200]), s.params())
    assert_same(D.deconvolute_window(ms, float(RT[200]), s).components, expected)


def test_range_deduplicates_window_borders_and_honors_bounds():
    ms = build([(120.0, SPEC_A, 50000.0), (210.0, SPEC_B, 50000.0), (300.0, SPEC_A, 50000.0)])
    t0, t1 = float(RT[20]), float(RT[380])
    comps = D.deconvolute_range(ms, t0, t1)
    for apex in (120, 210, 300):
        assert len([c for c in comps if abs(c.rt - RT[apex]) < 2 * DT]) == 1
    assert all(t0 <= c.rt <= t1 for c in comps)
    assert D.deconvolute_range(ms, t0, t1, cancel=lambda: True) == []
    # A peak exactly on the boundary between adjacent window cores is only kept once.
    boundary = float(RT[210])
    comps = D.deconvolute_range(ms, boundary - 0.3, boundary + 0.3)
    assert len([c for c in comps if abs(c.rt - boundary) < 2 * DT]) == 1


def test_empty_window_and_component_choice():
    ms = build([(200.0, SPEC_A, 60000.0)])
    assert D.deconvolute_window(ms, -10).components == []
    with pytest.raises(ValueError):
        D.deconvolute_range(ms, 10, 12, D.DeconvSettings(window=0))
    big = D.Component(10.00, 0, 57, [(57, 999)], area=100.0, purity=1, n_ions=3, s_n=10)
    small = D.Component(10.02, 0, 91, [(91, 999)], area=5.0, purity=1, n_ions=3, s_n=10)
    assert D.component_for_peak([small, big], 9.95, 10.05, 10.02) is big
    assert D.component_for_peak([small], 10.5, 10.6, 10.55) is None


def test_high_level_recovers_small_shoulder_in_reference_run(run07):
    """The faint MS component on the 19.158 min FID peak must be actionable."""
    from types import SimpleNamespace
    from gcws.integration import auto_deconv as AD
    from gcws.integration.engine import integrate
    from gcws.integration.method import nias_fid_method

    base = D.DeconvSettings()
    normal = D.deconvolute_range(run07.ms, 19.10, 19.25, base)
    high = D.deconvolute_range(run07.ms, 19.10, 19.25, D.settings_for_level(base, 5))
    assert not any(abs(c.rt - 19.216) < 0.008 for c in normal)
    assert any(abs(c.rt - 19.216) < 0.008 for c in high)

    method = nias_fid_method().copy(deconv_split="auto", deconv_level=5)
    result = integrate(run07.fid, method, t_min=5.5)
    peak = min(result.peaks, key=lambda p: abs(p.apex_rt - 19.158))
    plan = AD.plan_peaks(run07.fid, SimpleNamespace(peaks=[peak]), "FID", 0.0066,
                         high, method)
    assert len(plan.events) == 1
    selected = [(plan.plans[0].candidates[i].component.rt, plan.plans[0].shares[j])
                for j, i in enumerate(plan.plans[0].checked)]
    assert any(abs(rt - 19.216) < 0.008 and share > 0.04 for rt, share in selected)
    assert sum(share for _rt, share in selected) == pytest.approx(1.0)

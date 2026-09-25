"""Deconvolution engine: synthetic co-elution, background, noise, skew; AMDIS benchmark."""
import numpy as np
import pytest

from gcws.io.ms_matrix import MSMatrix
from gcws.ms import deconv as D
from gcws.ms.similarity import cosine

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
        return [(float(m), int(a)) for m, a in zip(mz, ab)]


def _best(comps, spec, rt):
    cands = [c for c in comps if abs(c.rt - rt) < 3 * DT]
    return max((cosine(spec, dict(c.spectrum)) for c in cands), default=0.0)


@pytest.mark.parametrize("sep, target", [(1.5, 0.95), (1.0, 0.90), (0.8, 0.80)])
def test_coeluting_pair(sep, target):
    a, b = 200.0, 200.0 + sep
    ms = build([(a, SPEC_A, 60000.0), (b, SPEC_B, 45000.0)])
    res = D.deconvolute_window(ms, float(np.interp(a, np.arange(N), RT)), D.DeconvSettings())
    ta, tb = (float(np.interp(v, np.arange(N), RT)) for v in (a, b))
    assert _best(res.components, SPEC_A, ta) >= target
    assert _best(res.components, SPEC_B, tb) >= target
    # the vendored engine on the same data is never better
    import gc_deconv
    legacy = gc_deconv.deconvolute(_DataMSAdapter(ms), ta, gc_deconv.DeconvParams())
    new_score = _best(res.components, SPEC_A, ta) + _best(res.components, SPEC_B, tb)
    old_score = _best(legacy, SPEC_A, ta) + _best(legacy, SPEC_B, tb)
    assert new_score >= old_score - 1e-6


def test_background_is_not_in_the_spectra():
    ms = build([(200.0, SPEC_A, 60000.0)])
    res = D.deconvolute_window(ms, float(RT[200]), D.DeconvSettings())
    comp = max(res.components, key=lambda c: c.area)
    spec = dict(comp.spectrum)
    assert spec.get(207, 0) < 20 and spec.get(73, 0) < 30           # bleed / slope stay in the baseline
    assert cosine(SPEC_A, spec) > 0.97
    assert 207 in comp.bg_ions or 207 not in spec
    without = D.deconvolute_window(ms, float(RT[200]), D.DeconvSettings(baseline=False))
    worse = max(without.components, key=lambda c: c.area)
    assert cosine(SPEC_A, dict(worse.spectrum)) <= cosine(SPEC_A, spec) + 1e-9


def test_noise_model_and_determinism():
    ms = build([(150.0, SPEC_A, 40000.0), (260.0, SPEC_B, 30000.0)])
    nm = D.estimate_noise(ms)
    assert nm.k == pytest.approx(K_TRUE, rel=0.25)
    r1 = D.deconvolute_window(ms, float(RT[150]), D.DeconvSettings())
    r2 = D.deconvolute_window(ms, float(RT[150]), D.DeconvSettings())
    assert [(c.rt, c.model_mz, c.spectrum) for c in r1.components] == \
           [(c.rt, c.model_mz, c.spectrum) for c in r2.components]
    for c in r1.components:
        assert 0 <= c.quality <= 100 and 0 <= c.purity <= 1 and c.r2 >= 0


def test_skew_is_estimated_and_one_component_remains():
    comps = [(60.0 + 25 * k, SPEC_A if k % 2 else SPEC_B, 80000.0) for k in range(12)]
    ms = build(comps, background=False, skew=0.006)                  # 0.6 scans per 100 u
    skew = D.estimate_skew(ms)
    assert skew == pytest.approx(0.006, abs=0.002)
    res = D.deconvolute_window(ms, float(RT[85]), D.DeconvSettings())
    near = [c for c in res.components if abs(c.rt - RT[85]) < 2 * DT]
    assert len(near) == 1


def test_range_deduplicates_window_borders():
    ms = build([(120.0, SPEC_A, 50000.0), (210.0, SPEC_B, 50000.0), (300.0, SPEC_A, 50000.0)])
    comps = D.deconvolute_range(ms, float(RT[20]), float(RT[380]), D.DeconvSettings(window=0.3))
    for apex in (120, 210, 300):
        close = [c for c in comps if abs(c.rt - RT[apex]) < 2 * DT and c.quality > 30]
        assert len(close) == 1, apex


def test_settings_roundtrip_and_component_choice():
    s = D.DeconvSettings(noise_factor=4.0, exclude_model=(18, 207))
    assert D.DeconvSettings.from_dict(s.to_dict()) == s
    big = D.Component(10.00, 0, 57, [(57, 999)], area=100.0, purity=1, n_ions=3, s_n=10, quality=80)
    small = D.Component(10.02, 0, 91, [(91, 999)], area=5.0, purity=1, n_ions=3, s_n=10, quality=80)
    assert D.component_for_peak([small, big], 9.95, 10.05, 10.02) is big
    assert D.component_for_peak([small], 10.5, 10.6, 10.55) is small


def test_amdis_benchmark(samples):
    from gcws.io.run_loader import load_run
    from gcws.ms.amdis_elu import benchmark, read_elu
    elu = next(samples.glob("07_*.ELU"), None)
    if elu is None:
        pytest.skip("AMDIS reference not available")
    run = load_run(next(samples.glob("07_*.D")))
    comps = read_elu(elu)
    new = benchmark(comps, lambda rt: [(c.rt, c.spectrum_dict())
                                       for c in D.deconvolute_window(run.ms, rt, D.DeconvSettings()).components])
    import gc_deconv
    old = benchmark(comps, lambda rt: [(c.rt, {int(m): float(v) for m, v in c.spectrum})
                                       for c in gc_deconv.deconvolute(run.ms_source, rt, gc_deconv.DeconvParams())])
    assert new["substantial"]["recall"] >= old["substantial"]["recall"]
    assert new["substantial"]["median_mf"] >= old["substantial"]["median_mf"]
    assert new["substantial"]["recall"] >= 0.7
    assert new["seconds"] / len(comps) < 0.5                         # per window

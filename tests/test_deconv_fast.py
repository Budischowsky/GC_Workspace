"""The array-at-once deconvolution must find exactly what the vendored NIAS engine finds."""
import numpy as np
import pytest

import gc_deconv
from gcws.io.ms_matrix import MSMatrix
from gcws.ms import deconv as D
from gcws.ms import deconv_fast as F
from tests.test_deconv import N, RT, SPEC_A, SPEC_B, _DataMSAdapter, assert_same, build


def _level_params(level):
    return D.settings_for_level(D.DeconvSettings(), level).params()


def _mixture(seed):
    """One to six co-eluting compounds with random spectra, heights, skew, noise and background."""
    rng = np.random.default_rng(seed)
    components = []
    for _ in range(int(rng.integers(1, 7))):
        ions = rng.choice(np.arange(40, 160), size=int(rng.integers(3, 12)), replace=False)
        spectrum = {int(m): float(rng.uniform(5, 100)) for m in ions}
        components.append((200.0 + float(rng.normal(0, 3)), spectrum, float(rng.uniform(800, 90000))))
    return build(components, background=bool(rng.integers(0, 2)),
                 skew=float(rng.choice([0.0, 0.01, -0.01])), seed=int(rng.integers(0, 10**6)),
                 k=float(rng.uniform(1, 5)))


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
def test_synthetic_mixtures_match_vendored_engine(level):
    params = _level_params(level)
    found = 0
    for seed in range(30):
        ms = _mixture(seed)
        expected = gc_deconv.deconvolute(_DataMSAdapter(ms), float(RT[200]), params)
        assert_same(F.deconvolute(ms, float(RT[200]), params), expected)
        found += len(expected)
    assert found >= 30


def test_background_without_compounds_matches_vendored_engine():
    ms = build([])
    for level in (3, 5):
        expected = gc_deconv.deconvolute(_DataMSAdapter(ms), float(RT[200]), _level_params(level))
        assert_same(F.deconvolute(ms, float(RT[200]), _level_params(level)), expected)


def test_window_outside_the_run_or_without_ions_is_empty():
    ms = build([(200.0, SPEC_A, 60000.0)])
    assert F.deconvolute(ms, -10.0, gc_deconv.DeconvParams()) == []
    empty = MSMatrix._build(RT, np.zeros(N), [np.zeros(0)] * N, [np.zeros(0)] * N)
    assert F.deconvolute(empty, float(RT[200]), gc_deconv.DeconvParams()) == []


@pytest.mark.parametrize("level", [3, 5])
def test_reference_run_windows_match_vendored_engine(run07, level):
    params = _level_params(level)
    found = 0
    for rt in (7.0, 10.0, 13.409, 19.2, 25.0, 30.0, 35.6):
        expected = gc_deconv.deconvolute(run07.ms_source, rt, params)
        assert_same(F.deconvolute(run07.ms, rt, params), expected)
        found += len(expected)
    assert found >= 10


def test_window_and_whole_run_use_the_array_engine(monkeypatch):
    """The per-ion vendored routine is the test reference only; the workspace must not call it."""
    def per_ion_engine(*_args, **_kwargs):
        raise AssertionError("the per-ion engine was called")

    monkeypatch.setattr(gc_deconv, "deconvolute", per_ion_engine)
    ms = build([(120.0, SPEC_A, 50000.0), (210.0, SPEC_B, 50000.0)])
    high = D.settings_for_level(D.DeconvSettings(), 5)
    assert D.deconvolute_window(ms, float(RT[120]), high).components
    assert D.deconvolute_range(ms, float(RT[20]), float(RT[380]), high)


def _shapes(n, apexes, width):
    idx = np.arange(n, dtype=float)
    return np.column_stack([np.exp(-0.5 * ((idx - a) / width) ** 2) for a in apexes])


def test_nnls_of_many_columns_matches_lawson_hanson():
    rng = np.random.default_rng(5)
    a = _shapes(60, [10, 14, 17, 30, 31.5, 45], 2.0)       # overlapping and nearly collinear
    x_true = rng.uniform(0, 1000, (6, 40)) * (rng.random((6, 40)) < 0.5)
    b = np.maximum(a @ x_true + rng.normal(0, 5, (60, 40)), 0.0)
    b[:, 0] = 0.0
    b[:, 1] = a @ np.array([1000.0, 1e-4, 500.0, 0.0, 0.0, 300.0])   # a faint but real ion
    got = F.nnls_columns(a, b)
    assert got.shape == (6, 40)
    assert np.count_nonzero(got) > 40
    assert got[1, 1] == pytest.approx(1e-4, rel=1e-6)
    for c in range(b.shape[1]):
        want = gc_deconv.nnls(a, b[:, c])
        np.testing.assert_allclose(got[:, c], want, rtol=1e-9, atol=1e-9 * max(want.max(), 1.0))


def test_nnls_of_many_columns_survives_identical_shapes():
    """Two identical model shapes make the problem singular; the fit must stay optimal."""
    a = _shapes(40, [10, 10, 25], 2.0)
    b = a @ np.array([300.0, 200.0, 50.0])
    got = F.nnls_columns(a, b[:, None])[:, 0]
    want = gc_deconv.nnls(a, b)
    assert (got >= 0).all()
    assert np.linalg.norm(a @ got - b) == pytest.approx(np.linalg.norm(a @ want - b), abs=1e-6)
    assert got[0] + got[1] == pytest.approx(500.0, rel=1e-9)


def test_identical_shapes_share_the_mass_as_lawson_hanson_does():
    """How the singular problem splits a mass between the copies depends on the solver's path;
    the window is solved by the original routine."""
    a = _shapes(40, [10, 10, 25], 2.0)
    b = a @ np.array([[300.0, 10.0], [200.0, 0.0], [50.0, 70.0]])
    got = F.nnls_columns(a, b)
    for c in range(b.shape[1]):
        np.testing.assert_array_equal(got[:, c], gc_deconv.nnls(a, b[:, c]))


def _saturated(cap, height=1e6):
    """SPEC_A as one large compound whose ions are cut flat at ``cap`` (a saturated detector)."""
    idx = np.arange(N)
    profile = np.exp(-0.5 * ((idx - 200) / (6.0 / 2.355)) ** 2)
    masses = sorted(SPEC_A) + [207]
    x = np.zeros((N, len(masses)))
    for j, m in enumerate(masses[:-1]):
        x[:, j] = np.minimum(height * SPEC_A[m] / 100 * profile, cap)
    x[:, -1] = 2000.0 + np.random.default_rng(1).normal(0, 30, N)
    x = np.round(x)
    return MSMatrix._build(RT, x.sum(axis=1), [np.array([masses[j] + 0.1 for j in np.flatnonzero(row > 0)])
                                               for row in x], [row[row > 0] for row in x])


@pytest.mark.parametrize("cap,level", [(5e4, 3), (5e4, 5), (1e5, 5)])
def test_saturated_top_is_one_component_as_in_the_vendored_engine(cap, level):
    """A flat top has a smoothed maximum at either end with the same flanks; the second model
    shape is a copy of the first, which the original NNLS leaves empty (no twin component)."""
    ms = _saturated(cap)
    params = _level_params(level)
    expected = gc_deconv.deconvolute(_DataMSAdapter(ms), float(RT[200]), params)
    assert len(expected) == 1
    assert_same(F.deconvolute(ms, float(RT[200]), params), expected)


def test_smoothing_is_the_vendored_savgol_to_the_last_bit():
    """Ions with alike traces tie on score; the tie is decided by the last bit of the smoothed edge
    points, so these must be computed as savgol computes them (on the strided column)."""
    rng = np.random.default_rng(3)
    for m in (1, 2, 7, 40):
        x = np.round(rng.uniform(0, 1e5, (23, m)) * (rng.random((23, m)) < 0.7))
        cols = np.arange(m)[::2]
        got = F._smooth(x, cols)
        for j, c in enumerate(cols):
            np.testing.assert_array_equal(got[j], gc_deconv.savgol(x[:, c], width=5, order=2))


def test_spectrum_figures_on_a_rounding_step_come_from_the_original_nnls():
    a = _shapes(40, [10, 16], 2.0)
    x = a @ np.array([[999.0, 721.5, 0.5, 3.0], [300.0, 100.0, 50.0, 40.0]])
    want = np.column_stack([gc_deconv.nnls(a, x[:, c]) for c in range(x.shape[1])])
    contrib = want * (1.0 + np.array([[1e-13, -1e-13, 1e-13, -1e-13], [-1e-13, 1e-13, -1e-13, 1e-13]]))
    F.settle_spectrum_ties(a, x, contrib)
    np.testing.assert_array_equal(contrib[:, :3], want[:, :3])     # the ties and their base mass
    assert contrib[0, 3] != want[0, 3]                              # no tie: left as it is


def test_ill_conditioned_shapes_are_solved_by_lawson_hanson():
    """Two shapes a two-hundredth of a scan apart: the normal equations would lose the agreement."""
    a = _shapes(40, [10, 10.005, 25], 2.0)
    assert np.linalg.cond(a) > 1e3
    b = np.maximum(a @ np.array([[300.0, 10.0, 0.0], [200.0, 0.0, 5.0], [50.0, 70.0, 1.0]])
                   + np.random.default_rng(2).normal(0, 1, (40, 3)), 0.0)
    got = F.nnls_columns(a, b)
    for c in range(b.shape[1]):
        np.testing.assert_array_equal(got[:, c], gc_deconv.nnls(a, b[:, c]))


def test_variables_at_the_tolerance_are_decided_by_lawson_hanson():
    """A trace of a mass in a second component, at the size of the solver's tolerance: whether
    that component gets it (and so exists at all) depends on the solver's path."""
    a = _shapes(40, [10, 14, 25], 2.0)
    tol = 40 * np.finfo(float).eps * 1000.0
    b = np.column_stack([1000.0 * a[:, 0] + d * tol * a[:, 1] for d in (-2.0, -0.5, 0.5, 1.0, 2.0)])
    got = F.nnls_columns(a, b)
    for c in range(b.shape[1]):
        np.testing.assert_array_equal(got[:, c], gc_deconv.nnls(a, b[:, c]))

"""Rust kernels of the array deconvolution (gcws.ms.rust_deconv): the numpy results, bit for bit."""
import numpy as np
import pytest

import gc_deconv
from gcws.ms import deconv_fast as F
from gcws.ms import rust_deconv as RD
from tests.test_deconv import RT
from tests.test_deconv_fast import _level_params, _mixture, _saturated

rust = pytest.importorskip("gcws_rust", reason="Rust extension not built (tools/build_rust.ps1)")
if not hasattr(rust, "perceive_components"):
    pytest.skip("Rust extension without the deconvolution kernels", allow_module_level=True)

PY = RD._installed


@pytest.fixture
def parts(monkeypatch):
    def switch(value):
        if value is None:
            monkeypatch.delenv("GCWS_RUST_DECONV", raising=False)
        else:
            monkeypatch.setenv("GCWS_RUST_DECONV", value)
        return RD.install()
    yield switch
    monkeypatch.delenv("GCWS_RUST_DECONV", raising=False)
    RD.install()


def _bits(value):
    """Everything of a result, floats by their bits."""
    if isinstance(value, np.ndarray):
        return ("array", value.dtype.str, value.shape, value.tobytes())
    if isinstance(value, (list, tuple)):
        return tuple(_bits(v) for v in value)
    if hasattr(value, "__dict__"):
        return (type(value).__name__, tuple((k, _bits(v)) for k, v in sorted(vars(value).items())))
    return value


def _window(ms, rt, params):
    """The inputs of the window's ion perception, as deconv_fast.deconvolute prepares them."""
    scan_rt = np.asarray(ms.rt, dtype=float)
    sel = np.flatnonzero(np.abs(scan_rt - rt) <= params.window)
    lo, hi = int(sel[0]), int(sel[-1])
    x, mzs = F._ion_matrix(ms, lo, hi)
    sigma, nonzero = F._ion_sigmas(x)
    return x, mzs, np.maximum(sigma, gc_deconv._sigma_floor(sigma)), nonzero, hi - lo + 1


def test_numpy_einsum_order_reproduced():
    rng = np.random.default_rng(4)
    for _ in range(3000):
        c = int(rng.integers(0, 90))
        a, b = rng.standard_normal(c) * 1e3, rng.standard_normal(c)
        assert rust.np_einsum_dot(a, b) == np.einsum("i,i->", a, b)
        rows = np.stack([a, b, a * b]) if c else np.zeros((3, 0))
        np.testing.assert_array_equal(np.einsum("ij,ij->i", rows, rows),
                                      [rust.np_einsum_dot(r, r) for r in rows])


@pytest.mark.parametrize("level", [1, 3, 5])
def test_ion_perception_and_components_equal_python(level):
    params = _level_params(level)
    windows = [_mixture(seed) for seed in range(25)] + [_saturated(cap) for cap in (2e5, 5e5)]
    peaks_seen = groups_seen = 0
    for ms in windows:
        x, mzs, sigma, nonzero, n = _window(ms, float(RT[200]), params)
        a = RD._perceive_ions(x, mzs, sigma, nonzero, params)
        b = PY["ions"](x, mzs, sigma, nonzero, params)
        assert _bits(a) == _bits(b)
        if b is None:
            continue
        peaks_seen += b.mz.size
        groups = RD._perceive_components(b, n, params)
        assert groups == PY["components"](b, n, params)
        groups_seen += len(groups)
        # the links alone, for the first block of seeds against nothing used
        order = np.lexsort((b.apex, b.mz, -b.score))
        rank = np.empty(order.size, dtype=np.int64)
        rank[order] = np.arange(order.size)
        half = np.maximum(gc_deconv.CORR_MIN_HALF_WIDTH, np.round(1.5 * b.width_half).astype(np.int64))
        lo, hi = np.maximum(0, b.apex - half), np.minimum(n, b.apex + half + 1)
        by_apex = np.argsort(b.apex_sub, kind="stable")
        sorted_apex = b.apex_sub[by_apex]
        first = np.searchsorted(sorted_apex, b.apex_sub - params.apex_tolerance - 1e-9, side="left")
        last = np.searchsorted(sorted_apex, b.apex_sub + params.apex_tolerance + 1e-9, side="right")
        args = (order[:64], b, params, rank, np.zeros(order.size, bool), by_apex, first, last, lo, hi, n)
        assert RD._links(*args) == PY["links"](*args)
    assert peaks_seen > 200 and groups_seen > 25


@pytest.mark.parametrize("level", [2, 5])
def test_whole_windows_identical(parts, level):
    params = _level_params(level)
    for seed in range(30, 45):
        ms = _mixture(seed)
        parts("off")
        expected = _bits(F.deconvolute(ms, float(RT[200]), params))
        assert parts(None) == {"ions", "components"}
        assert _bits(F.deconvolute(ms, float(RT[200]), params)) == expected
        assert parts("ions+links") == {"ions", "links"}
        assert _bits(F.deconvolute(ms, float(RT[200]), params)) == expected


def test_off_restores_python(parts):
    assert parts("off") == set()
    assert F._perceive_ions is PY["ions"] and F._links is PY["links"] and F._perceive_components is PY["components"]
    assert parts(None) == {"ions", "components"}
    assert F._perceive_ions is RD._perceive_ions and F._perceive_components is RD._perceive_components

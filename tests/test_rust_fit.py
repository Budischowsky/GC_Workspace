"""The component fit's grid search in Rust (gcws.ms.rust_fit): the Python fit, field for field."""
import numpy as np
import pytest

from gcws.ms import component_fit as F
from gcws.ms import rust_fit as RF

rust = pytest.importorskip("gcws_rust", reason="Rust extension not built (tools/build_rust.ps1)")
if not hasattr(rust, "grid_residuals"):
    pytest.skip("Rust extension without the component fit kernel", allow_module_level=True)

PY = RF._installed["fit_trace_uncached"]
MS_DT, FID_DT = 0.0075, 1 / 1200.0


def shape(rt, sigma=0.012, tail=0.0):
    t = np.arange(rt - 0.3, rt + 0.3, MS_DT)
    y = np.exp(-0.5 * ((t - rt) / sigma) ** 2) + tail * np.exp(-np.clip(t - rt, 0, None) / 0.03) * (t > rt)
    y[y < 1e-3] = 0.0
    return F.Shape.from_arrays(rt, t, y * 1e6)


def trace(parts, shift, stretch, sigma=0.012, noise=1.0, seed=3):
    t = np.arange(9.7, 10.3, FID_DT)
    y = np.zeros(t.size)
    for rt, a in parts:
        y += a * np.exp(-0.5 * ((t - rt - shift) / (sigma * stretch)) ** 2)
    return t, y + np.random.default_rng(seed).normal(0, noise, t.size)


def same(a, b):
    assert (a.shift, a.stretch, a.r2, a.residual, a.collinear) == (b.shift, b.stretch, b.r2, b.residual, b.collinear)
    for name in ("amplitudes", "curves", "areas"):
        assert getattr(a, name).tobytes() == getattr(b, name).tobytes(), name


def test_rust_is_installed_and_switchable(monkeypatch):
    assert F.fit_trace_uncached is RF.fit_trace_uncached
    monkeypatch.setenv("GCWS_RUST_FIT", "off")
    assert RF.install() is False and F.fit_trace_uncached is PY
    monkeypatch.delenv("GCWS_RUST_FIT")
    assert RF.install() is True and F.fit_trace_uncached is RF.fit_trace_uncached


def test_grid_residuals_agree_with_the_engine_nnls():
    rng = np.random.default_rng(5)
    for count in (1, 2, 3, 5):
        rts = sorted(rng.uniform(9.9, 10.1, count))
        shapes = [shape(rt, sigma=rng.uniform(0.008, 0.016), tail=rng.uniform(0, 0.3)) for rt in rts]
        t, y = trace([(rt, rng.uniform(50, 900)) for rt in rts], 0.006, rng.uniform(0.7, 1.5))
        arrays = RF._shape_arrays(shapes)
        shifts = list(rng.uniform(-0.01, 0.02, 40))
        logs = list(rng.uniform(np.log(0.6), np.log(1.8), 40))
        stretches = [float(np.exp(v)) for v in logs]
        for summed in (False, True):
            got = rust.grid_residuals(arrays, t, y, shifts, stretches, summed)
            for g, shift, log_k in zip(got, shifts, logs):
                a = F._curves(shapes[0], t, [(shift, log_k)])[0][:, None] if count == 1 else \
                    np.stack([F._curves(s, t, [(shift, log_k)])[0] for s in shapes], axis=1)
                if summed:
                    a = a.sum(axis=1)[:, None]
                ref = F._residual(a, y)[0]
                assert g == pytest.approx(ref, rel=1e-11, abs=1e-9 * float(y @ y))


@pytest.mark.parametrize("case", ["one", "two", "three", "collinear", "masked", "outside", "negative", "noisy"])
def test_fit_equals_python(case):
    mask = None
    rts = {"one": [10.0], "two": [10.0, 10.02], "three": [9.98, 10.0, 10.03], "collinear": [10.0, 10.0075],
           "masked": [10.0, 10.02], "outside": [10.0], "negative": [10.0], "noisy": [9.99, 10.01]}[case]
    shapes = [shape(rt) for rt in rts]
    t, y = trace([(rt, 300. + 100 * i) for i, rt in enumerate(rts)], 0.006, 0.9,
                 noise=40.0 if case == "noisy" else 2.0)
    if case == "masked":
        mask = ~((t > 10.05) & (t < 10.06))
    if case == "outside":
        t = t + 5.0
    if case == "negative":
        y = -y
    for shift0 in (0.0, 0.006, 0.0071):
        same(RF.fit_trace_uncached(t, y, shapes, shift0, mask=mask), PY(t, y, shapes, shift0, mask=mask))

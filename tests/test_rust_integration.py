"""Rust kernels of the integrator (gcws.integration.rust_integration): the Python results, bit for bit."""
import numpy as np
import pytest

from gcws.core.events import ManualEvent, ManualKind as K
from gcws.core.model import Baseline, Signal
from gcws.integration import engine, rust_integration as RI
from gcws.integration.method import EventKind as E
from gcws.integration.method import IntegrationMethod, TimedEvent, nias_fid_method
from gcws.integration.work import WorkSignal

rust = pytest.importorskip("gcws_rust", reason="Rust extension not built (tools/build_rust.ps1)")
if not hasattr(rust, "width_candidates"):
    pytest.skip("Rust extension without the integrator kernels", allow_module_level=True)

PY = RI._installed


@pytest.fixture
def parts(monkeypatch):
    """Switch the parts: parts('off') -> Python, parts(None) -> the default; restores the default."""
    def switch(value):
        if value is None:
            monkeypatch.delenv("GCWS_RUST_INTEGRATION", raising=False)
        else:
            monkeypatch.setenv("GCWS_RUST_INTEGRATION", value)
        return RI.install()
    yield switch
    monkeypatch.delenv("GCWS_RUST_INTEGRATION", raising=False)
    RI.install()


def chromatogram(seed, n_peaks=40, rate=1200.0, t_end=12.0, noise=1.0, drift=2.0):
    rng = np.random.default_rng(seed)
    t = np.arange(0, t_end, 1 / rate)
    y = 100 + drift * t + rng.normal(0, noise, t.size)
    for _ in range(n_peaks):
        mu, s, a = rng.uniform(0.5, t_end - 0.5), rng.uniform(0.004, 0.03), 10 ** rng.uniform(0.5, 4)
        y = y + a * np.exp(-0.5 * ((t - mu) / s) ** 2)
        if rng.random() < 0.2:                                   # tailing peaks
            y = y + 0.3 * a * np.exp(-np.clip(t - mu, 0, None) / (5 * s)) * (t > mu)
    return Signal("FID", t, y)


def _key(res):
    return repr(res.peaks) + repr(res.resolved) + repr(res.unresolved) + res.digest


def test_numpy_interp_and_sum_reproduced():
    rng = np.random.default_rng(1)
    for n in list(range(0, 300)) + [511, 512, 513, 4097, 8192, 20000]:
        a = rng.standard_normal(n) * 10 ** rng.uniform(-5, 8, n)
        assert rust.np_sum_f64(a) == a.sum()
    for _ in range(5000):
        n = int(rng.integers(1, 12))
        xp = np.sort(rng.uniform(0, 10, n))
        if rng.random() < 0.3:
            xp = np.round(xp)                                     # duplicates
        fp = rng.standard_normal(n)
        x = float(rng.choice([rng.uniform(-1, 11), xp[int(rng.integers(n))]]))
        assert rust.np_interp(x, xp, fp) == np.interp(x, xp, fp)
    xp = np.cumsum(rng.uniform(0.001, 0.01, 5000))
    fp = rng.standard_normal(5000)
    for x in np.concatenate([rng.uniform(-1, xp[-1] + 1, 3000), xp[::7]]):
        assert rust.np_interp(float(x), xp, fp) == np.interp(x, xp, fp)


def test_kernels_equal_python():
    rng = np.random.default_rng(7)
    for seed in range(6):
        sig = chromatogram(seed)
        rt, y = sig.rt, sig.y
        ws = WorkSignal(rt, y, y, np.gradient(y), np.gradient(y))
        # peak width
        for sigma in (0.5, 1.0, 3.0):
            for t_from in (None, 1.0, 11.99):
                assert RI._measure_width(rt, y, sigma, t_from) == PY["width"](rt, y, sigma, t_from)
        # nearest index
        for t in np.concatenate([rng.uniform(-1, 13, 300), rt[::997], (rt[1:] + rt[:-1])[::501] / 2]):
            assert RI._idx(ws, float(t)) == PY["idx"](ws, float(t))
        # area and shape over random windows and baselines
        for _ in range(300):
            t0, t1 = sorted(rng.uniform(-0.1, 12.1, 2))
            if rng.random() < 0.2:
                t0, t1 = t1, t0
            if rng.random() < 0.1:
                t1 = t0
            kind = str(rng.choice(["line", "hold"]))
            bt0 = t0 if rng.random() < 0.9 else t1
            base = Baseline(kind, bt0, float(rng.normal(100, 30)), t1, float(rng.normal(100, 30)))
            neg = bool(rng.random() < 0.2)
            assert RI._raw_area(ws, t0, t1, base, neg) == PY["raw_area"](ws, t0, t1, base, neg)
            assert RI._shape(ws, t0, t1, base, neg) == PY["shape"](ws, t0, t1, base, neg)
        exp = Baseline("exp", 2.0, 150.0, 3.0, 120.0, k=1.5, b=100.0)    # Python path
        assert RI._shape(ws, 2.0, 3.0, exp) == PY["shape"](ws, 2.0, 3.0, exp)


def test_detect_equal_python():
    rng = np.random.default_rng(11)
    for seed in range(6):
        sig = chromatogram(seed, n_peaks=60)
        ys, d1 = sig.y, np.gradient(sig.y) * 1200
        n = ys.size
        for slope_level in (5.0, 20.0, 80.0):
            slope = np.full(n, slope_level)
            on = rng.random(n) > 0.001 if seed % 2 else np.ones(n, bool)
            n_up = rng.integers(2, 6, n) if seed % 3 == 0 else np.full(n, 3)
            n_dn = np.full(n, 5)
            at_base = None if seed < 3 else rng.random(n) > 0.3
            a = RI._detect(ys, d1, slope, on, n_up, n_dn, at_base)
            b = PY["detect"](ys, d1, slope, on, n_up, n_dn, at_base)
            assert a == b


def _methods():
    yield IntegrationMethod(area_unit_factor=1.0)
    yield IntegrationMethod(area_unit_factor=1.0, baseline_mode="valley", shoulders="drop", skim_mode="auto")
    yield IntegrationMethod(area_unit_factor=1.0, negative_peaks=True, baseline_tracking=True,
                            timed_events=[TimedEvent(0.0, E.INTEGRATOR_OFF), TimedEvent(0.6, E.INTEGRATOR_ON),
                                          TimedEvent(4.0, E.SPLIT_PEAK), TimedEvent(6.0, E.BASELINE_NOW)])
    yield nias_fid_method()


def test_whole_integrations_identical(parts):
    events = [ManualEvent(K.SPLIT, 5.0), ManualEvent(K.DELETE, 7.5), ManualEvent(K.ADD_PEAK, 3.0, 3.2),
              ManualEvent(K.DRAW_BASELINE, 9.0, 9.5)]
    for seed in range(4):
        sig = chromatogram(seed + 20, n_peaks=50, noise=0.5 + seed)
        for m in _methods():
            for ev, t_min in (((), None), (events, 0.8)):
                parts("off")
                ref = _key(engine.integrate(sig, m, ev, t_min=t_min))
                assert parts(None) == set(RI.DEFAULT.split("+"))
                assert _key(engine.integrate(sig, m, ev, t_min=t_min)) == ref


def test_off_restores_python(parts):
    from gcws.integration import autoparams, detector, measure, work
    assert parts("off") == set()
    assert measure.shape is PY["shape"] and detector.detect is PY["detect"] and engine.detect is PY["detect"]
    assert autoparams.measure_width is PY["width"] and work.WorkSignal.idx is PY["idx"]
    assert parts("measure") == {"measure"}
    assert measure.raw_area is RI._raw_area and detector.detect is PY["detect"]

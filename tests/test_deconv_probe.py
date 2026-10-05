"""The closer look on the array engine must find exactly what the legacy-helper closer look finds."""
import numpy as np
import pytest

from gcws.ms import deconv as D
from gcws.ms import deconv_probe as P
from tests.test_auto_deconv import MAIN, SHOULDER
from tests.test_deconv import RT, assert_same, build
from tests.test_deconv_fast import _mixture


def _windows():
    for seed in range(30):
        yield _mixture(seed)
    for d in (3, 4, 6):
        for h in (8000.0, 15000.0):
            for s in (1, 3):
                yield build([(200.0, MAIN, 80000.0), (200.0 + d, SHOULDER, h)], background=False, seed=s)


@pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
def test_fast_probe_matches_reference_on_synthetic_windows(level):
    from gcws.ms.deconv_probe_fast import probe_fast
    settings = D.settings_for_level(D.DeconvSettings(), level)
    robust_count = total = 0
    for ms in _windows():
        args = (ms, RT[194], RT[212], RT[200], settings)
        expected = P.probe_reference(*args)
        comps, robust = probe_fast(*args)
        assert_same(P.probe(*args), expected)
        if robust:
            assert_same(comps, expected)
        robust_count += robust
        total += 1
    # noise-free synthetic windows hold exact ties (a component apex on the window bound, equal
    # half widths), which correctly take the reference; real runs are held to 10 % below
    assert robust_count >= 0.75 * total


def test_probe_falls_back_when_not_robust(monkeypatch):
    from gcws.ms import deconv_probe_fast
    ms = build([(200.0, MAIN, 80000.0), (204.0, SHOULDER, 15000.0)], background=False, seed=3)
    args = (ms, RT[194], RT[212], RT[200])
    expected = P.probe_reference(*args)
    assert expected
    monkeypatch.setattr(deconv_probe_fast, "probe_fast", lambda *a, **k: ([], False))
    assert_same(P.probe(*args), expected)


def _gate_case(tiny, big=2):
    diff = np.full(20, -5.0)
    diff[:big] = 50.0
    diff[big:big + 3] = tiny
    x = (10.0 + diff)[:, None]
    diff = diff[:, None]
    return x, diff, np.maximum(diff, 0.0), np.array([1.0])


def test_gate_borderline_detects_rounding_zeros():
    from gcws.ms.deconv_probe_fast import gate_borderline
    assert gate_borderline(*_gate_case(1e-14), 2.0)
    assert not gate_borderline(*_gate_case(-5.0), 2.0)
    assert not gate_borderline(*_gate_case(-5.0, big=3), 2.0)


@pytest.mark.parametrize("prefix", ["07_", "09_"])
def test_fast_probe_matches_reference_on_real_runs(monkeypatch, prefix):
    from tests.conftest import run_dir
    from gcws.integration.engine import integrate
    from gcws.integration.method import IntegrationMethod
    from gcws.io.run_loader import load_run
    run = load_run(run_dir(prefix))
    res = integrate(run.signal("TIC"), IntegrationMethod())
    peaks = [p for p in res.peaks if 6.0 <= p.apex_rt <= 35.0]
    peaks = [peaks[int(k)] for k in np.unique(np.linspace(0, len(peaks) - 1, min(40, len(peaks))).astype(int))]
    reference = P.probe_reference
    fallbacks = []
    monkeypatch.setattr(P, "probe_reference", lambda *a, **k: fallbacks.append(1) or reference(*a, **k))
    calls = 0
    for level in (3, 5):
        settings = D.settings_for_level(D.DeconvSettings(), level)
        for p in peaks:
            args = (run.ms, p.start, p.end, p.apex_rt, settings)
            got = P.probe(*args)
            calls += 1
            assert_same(got, reference(*args))
    # what is left are genuine ties, e.g. an ion fitted to its last bit at one scan
    assert len(fallbacks) <= 0.10 * calls


def test_probe_falls_back_when_the_fast_path_fails(monkeypatch):
    from gcws.ms import deconv_probe_fast
    ms = build([(200.0, MAIN, 80000.0), (204.0, SHOULDER, 15000.0)], background=False, seed=3)
    args = (ms, RT[194], RT[212], RT[200])
    expected = P.probe_reference(*args)

    def broken(*_a, **_k):
        raise RuntimeError("unexpected input")
    monkeypatch.setattr(deconv_probe_fast, "probe_fast", broken)
    assert_same(P.probe(*args), expected)

"""The background of a peak's spectrum comes from the baseline, not from its neighbours.

The spectrum of an integrated peak is its top minus the background. Next to the peak's boundaries
in a cluster the scans sit on the neighbouring peaks; subtracting them took the peak's own ions
away with the neighbours' (worst with neighbours of a similar spectrum). Synthetic clusters with a
known spectrum, a column-bleed baseline, scan skew (the masses of a scan are measured one after
the other) and noise check that the extraction finds the true spectrum again."""
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.io.ms_matrix import MSMatrix
from gcws.ms.similarity import cosine
from gcws.ms.spectra import MODES, extract

DT = 0.0075                                               # min per scan
BLEED = {73: 300.0, 207: 900.0, 281: 300.0, 355: 120.0}
ALKANE = {43: 800.0, 57: 1000.0, 71: 600.0, 85: 350.0, 99: 120.0, 113: 60.0}
ALKANE2 = {43: 900.0, 57: 1000.0, 71: 450.0, 85: 250.0, 56: 180.0, 70: 150.0}   # an isomer
AROMATIC = {91: 1000.0, 92: 600.0, 65: 150.0, 105: 300.0, 39: 80.0}


def _run(peaks, n=400, noise=0.01, skew=0.35, seed=3):
    """An MS run: the bleed baseline plus Gaussian peaks ``(apex scan, height, spectrum)``; each mass
    is measured ``skew`` scans later per 100 u (scan skew), with relative noise ``noise``."""
    rng = np.random.default_rng(seed)
    masses = sorted({m for _, _, s in peaks for m in s} | set(BLEED))
    t = np.arange(n, dtype=float)
    dense = np.zeros((n, len(masses)))
    for j, m in enumerate(masses):
        dense[:, j] += BLEED.get(m, 0.0)
        for apex, height, spec in peaks:
            if m in spec:
                shift = skew * (m - 150) / 100.0
                dense[:, j] += height * spec[m] / 1000.0 * np.exp(-0.5 * ((t + shift - apex) / 2.2) ** 2)
    dense *= 1.0 + noise * rng.standard_normal(dense.shape)
    dense = np.maximum(dense, 0.0)
    mz = np.array(masses, dtype=float)
    ms = MSMatrix._build(t * DT + 5.0, dense.sum(axis=1), [mz] * n, list(dense))
    return SimpleNamespace(ms=ms, signal=lambda _key: None)


def _peak(run, a, b, apex):
    rt = run.ms.rt
    return SimpleNamespace(start=float(rt[a]), end=float(rt[b]), apex_rt=float(rt[apex]), extra={})


def _valleys(run, apex, gap):
    tic = run.ms.tic()
    lo = apex - gap + int(np.argmin(tic[apex - gap:apex]))
    hi = apex + 1 + int(np.argmin(tic[apex + 1:apex + gap + 1]))
    return lo, hi


def _cluster(left, right, gap=6, height=4000.0, mid=ALKANE):
    run = _run([(200 - gap, height, left), (200, height, mid), (200 + gap, height, right)])
    lo, hi = _valleys(run, 200, gap)
    return run, _peak(run, lo, hi, 200)


def test_similar_neighbours_no_longer_spoil_the_spectrum():
    run, peak = _cluster(ALKANE2, ALKANE2)
    new = extract(run, peak, "TIC", 0.0)
    old = extract(run, peak, "TIC", 0.0, "average_bg_classic")
    assert cosine(new, ALKANE) > 0.97
    assert cosine(old, ALKANE) < cosine(new, ALKANE) - 0.05
    assert "baseline" in new.note


def test_the_bleed_of_the_baseline_is_removed():
    run, peak = _cluster(ALKANE2, ALKANE2)
    spec = dict(zip(extract(run, peak, "TIC", 0.0).mz.tolist(), extract(run, peak, "TIC", 0.0).ab.tolist()))
    top = max(spec.values())
    assert all(spec.get(m, 0.0) < 0.05 * top for m in BLEED)


def test_the_ions_of_a_different_neighbour_are_removed():
    run, peak = _cluster(AROMATIC, AROMATIC)
    spec = extract(run, peak, "TIC", 0.0)
    d = dict(zip(spec.mz.tolist(), spec.ab.tolist()))
    assert cosine(spec, ALKANE) > 0.95
    assert d.get(91, 0.0) < 0.05 * max(d.values())


def test_the_background_scans_are_baseline_scans_outside_the_cluster():
    run, peak = _cluster(ALKANE2, ALKANE2)
    spec = extract(run, peak, "TIC", 0.0)
    tic = run.ms.tic()
    pre = [s for s in spec.bg_scans if s < 200]
    post = [s for s in spec.bg_scans if s > 200]
    for group in (pre, post):                      # at most 10 % of the peak height above the baseline
        assert len(group) == 3 and np.mean(tic[group]) < tic.min() + 0.1 * (tic[200] - tic.min())
    assert max(pre) < 200 - 6 and min(post) > 200 + 6                           # beyond both neighbours


def test_an_isolated_peak_keeps_its_spectrum_exactly():
    run = _run([(200, 4000.0, ALKANE)])
    peak = _peak(run, 190, 210, 200)
    new = extract(run, peak, "TIC", 0.0)
    old = extract(run, peak, "TIC", 0.0, "average_bg_classic")
    assert new.bg_scans == old.bg_scans
    assert np.array_equal(new.mz, old.mz) and np.array_equal(new.ab, old.ab)


def test_the_classic_background_stays_selectable():
    assert "average_bg_classic" in MODES


@pytest.fixture(scope="module")
def ab_runs(samples):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from conftest import run_dir
    from gcws.io.run_loader import load_run
    from gcws.ui.workspace import Workspace
    out = []
    for prefix in ("07_", "11_"):
        ws = Workspace()
        ws.deconv_background = False
        st = ws.add_run(load_run(run_dir(prefix)))
        ws.process_runs([st.id])
        out.append(st)
    return out


def test_real_hydrocarbon_cluster_alike_in_both_determinations(ab_runs):
    """The cluster of run 07 at 20.2-20.7 min (TIC) and its B run: the peaks' spectra agree between
    A and B again (the classic background left little more than noise of them)."""
    a, b = ab_runs
    new, old = [], []
    for rt in (20.293, 20.398, 20.458, 20.510):
        pa = min(a.results["TIC"].peaks, key=lambda p: abs(p.apex_rt - rt))
        pb = min(b.results["TIC"].peaks, key=lambda p: abs(p.apex_rt - pa.apex_rt))
        assert abs(pb.apex_rt - pa.apex_rt) < 0.012
        new.append(cosine(extract(a.run, pa, "TIC", a.delay_value), extract(b.run, pb, "TIC", b.delay_value)))
        old.append(cosine(extract(a.run, pa, "TIC", a.delay_value, "average_bg_classic"),
                          extract(b.run, pb, "TIC", b.delay_value, "average_bg_classic")))
    assert min(new) > 0.75 and np.mean(new) > np.mean(old) + 0.2         # measured: 0.77-0.97 vs 0.36-0.96


def test_real_peak_between_two_alkanes_is_an_alkane(ab_runs):
    """19.081 min of run 07 (VV between two hydrocarbon peaks) came out as 112/95/126/119."""
    a = ab_runs[0]
    p = min(a.results["TIC"].peaks, key=lambda p: abs(p.apex_rt - 19.081))
    top = extract(a.run, p, "TIC", a.delay_value).top_ions(4)
    assert {57, 71} <= set(top) and not {112, 95, 126, 119} & set(top)

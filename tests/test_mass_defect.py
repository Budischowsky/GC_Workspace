"""Nominal masses with the mass defect of organic ions taken into account.

Hydrogen-rich ions weigh more than their nominal mass (C35H62O3, Irganox 1076: M+ 530.47) and a
quadrupole reports them a little high again (530.55-530.6). Plain rounding put that molecular ion on
531, so the library search of one determination missed Irganox 1076 (2082-79-3) and named the peak
"unknown (m/z 531, 57, 219)".
"""
import os
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import ROOT

DIARY = Path(os.environ.get("GCWS_DIARY_SAMPLES", ROOT.parent / "Testsample" / "25011662_GIO_Diary"))


def test_hydrogen_rich_high_masses_keep_their_nominal_mass():
    from gcws.io.ms_matrix import nominal
    measured = [530.55, 530.6, 531.6, 646.55, 647.5, 648.5]
    assert nominal(np.array(measured)).tolist() == [530, 530, 531, 646, 647, 648]


def test_low_masses_and_negative_mass_defects_are_unchanged():
    from gcws.io.ms_matrix import nominal
    # C4H9+, Br-79, I-127, siloxane bleed, CCl3+, tetrabromodiphenyl ether M+ (481.71)
    measured = [57.07, 78.92, 126.9, 207.03, 281.05, 116.91, 481.71]
    assert nominal(np.array(measured)).tolist() == [57, 79, 127, 207, 281, 117, 482]


def test_whole_masses_map_to_themselves():
    from gcws.io.ms_matrix import nominal
    masses = np.arange(1, 1001)
    assert (nominal(masses.astype(float)) == masses).all()


def test_legacy_deconvolution_bins_like_the_ms_matrix():
    import gc_deconv
    from gcws.io.ms_matrix import nominal
    for mz in (57.07, 207.03, 530.6, 531.6, 647.5, 648.5, 481.71):
        assert gc_deconv._nominal(mz) == int(nominal(np.array([mz]))[0])


def test_irganox_1076_molecular_ion_on_530():
    run = DIARY / "23_25011675_4891130401_HSL_OPV100m_B.D"
    if not run.is_dir():
        pytest.skip("GIO Diary run B not available (set GCWS_DIARY_SAMPLES)")
    from gcws.io.run_loader import load_run
    ms = load_run(run).ms
    scans = np.nonzero((ms.rt > 26.92) & (ms.rt < 26.95))[0]
    spec = ms.nominal_spectrum(scans)
    assert spec.get(530, 0) > 2 * spec.get(531, 0)          # M+ and its 13C isotope (~39 %)
    top = sorted(spec, key=spec.get, reverse=True)[:3]
    assert 530 in top and 531 not in top

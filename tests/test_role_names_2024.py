"""2024 sequences name their solvent runs with the batch number (01_EtOH_2_24005318, 02_EtOH_STD_24005318):
a leading solvent token makes the run a blank even with a folder number; 'STD' next to the solvent is the
ISTD-spiked solvent. A sample whose name starts with the solvent and then its own number stays a sample."""
import pytest

from gcws.io.sequence import BLANK, BLANK_ISTD, SAMPLE, classify_role


@pytest.mark.parametrize("name,role", [
    ("01_EtOH_2_24005318.D", BLANK),
    ("03_EtOAc_3_24006183", BLANK),
    ("12_EtOH_1_24006183_60grad", BLANK),
    ("07_EtOH_1_2_24008247", BLANK),
    ("02_EtOH_STD_24005318", BLANK_ISTD),
    ("02_EtOAc_STD_24006183", BLANK_ISTD),
    ("02_EtOH_STD_alt_24007908", BLANK_ISTD),
    ("062_EtOH_STD_2_24011405_Fil2", BLANK_ISTD),
    ("03_EtOAc_STD", BLANK_ISTD),
    ("06_EtOH_ISTD", BLANK_ISTD),
    ("08_EtOH", BLANK),
    ("05_Blank_EtOH", BLANK),
    ("06_EtOH_24019114_Lidfoil_1", SAMPLE),
    ("04_EtOH_24017968_A_STD_Gold_1", SAMPLE),
    ("04_EtOAc_24018898_4623320101_Tenax1", SAMPLE),
    ("03_EtOH_14783000002_10mg_L", SAMPLE),
    ("04_24005318_Paper_77g_0_25dm_EtOH_1", SAMPLE),
    ("07_26016606_x_A", SAMPLE),
])
def test_role_from_name(name, role):
    assert classify_role(name) == role


@pytest.mark.parametrize("name,role", [
    ("01_Isoo_2_24008247", BLANK), ("02_Isoo_STD_24008247", BLANK_ISTD), ("04_Isoo3", BLANK),
    ("03_Isoo_STD", BLANK_ISTD), ("02_EtOAcCyclohex_STD", BLANK_ISTD), ("03_EtOAcCyclohex_3", BLANK),
    ("02_Hept_2", BLANK), ("01_STD", BLANK_ISTD), ("02_STD_2", BLANK_ISTD),
    ("04_24008248_Orevac_18750_Isoo_1", SAMPLE), ("05_24021629_Pharmafoil_Isoo_1", SAMPLE),
])
def test_more_solvents(name, role):
    assert classify_role(name) == role

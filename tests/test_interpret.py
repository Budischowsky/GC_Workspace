"""MS interpreter: isotopes, molecular ion, classes, fingerprints, formulas."""
import numpy as np
import pytest

from gcws.ms import isotopes as iso
from gcws.ms.interpret import Context, interpret
from gcws.ms.knowledge import FINGERPRINTS, RULES, SERIES, validate_fingerprint, validate_rule

CTX = Context(mass_range=(35, 700), min_abundance=150)


def spectrum(ions: dict, formulas: dict | None = None, base_abs: float = 2e5):
    """Nominal spectrum with isotope peaks (formula-exact where given), thresholded at 150."""
    formulas = formulas or {}
    d: dict[int, float] = {}
    for m, r in ions.items():
        a = base_abs * r / 100
        p = iso.pattern(formulas[m], 6) if m in formulas else iso.pattern({"C": max(1, m // 14)}, 3)
        for k, f in enumerate(p):
            d[m + k] = d.get(m + k, 0.0) + a * f
    d = {m: v for m, v in d.items() if v >= 150}
    mz = np.array(sorted(d))
    return mz, np.array([d[m] for m in mz])


# -- isotope patterns -----------------------------------------------------------------

def test_known_patterns():
    cl2 = iso.pattern({"Cl": 2}, 5)
    assert cl2[2] == pytest.approx(0.64, abs=0.01) and cl2[4] == pytest.approx(0.10, abs=0.01)
    br = iso.pattern({"Br": 1}, 3)
    assert br[2] == pytest.approx(0.973, abs=0.01)
    c20 = iso.pattern({"C": 20, "H": 42}, 3)
    assert c20[1] == pytest.approx(0.221, abs=0.01)
    si = iso.pattern({"Si": 1}, 3)
    assert si[1] == pytest.approx(0.0508, abs=0.002) and si[2] == pytest.approx(0.0335, abs=0.002)


def test_pattern_order_independent():
    from hypothesis import given, settings, strategies as st

    @settings(max_examples=40, deadline=None)
    @given(st.dictionaries(st.sampled_from(["C", "H", "N", "O", "S", "Cl", "Br", "Si"]),
                           st.integers(min_value=0, max_value=12), min_size=1))
    def check(counts):
        a = iso.pattern(counts, 8)
        b = iso.pattern(dict(reversed(list(counts.items()))), 8)
        assert np.allclose(a, b)
        assert a[0] == pytest.approx(1.0)
        assert (a >= 0).all()

    check()


def test_formula_helpers():
    c = iso.parse_formula("C24H38O4")
    assert c == {"C": 24, "H": 38, "O": 4}
    assert iso.nominal_mass(c) == 390
    assert iso.rdbe(c) == 6
    assert iso.format_formula({"C": 6, "H": 4, "Cl": 2}) == "C6H4Cl2"


def test_carbon_estimate_respects_threshold():
    assert iso.carbon_estimate(10000, 2210, 150) [0] == 20
    assert iso.carbon_estimate(1000, 200, 150) is None         # M+1 too close to the threshold


# -- whole interpretation ------------------------------------------------------------

CASES = {
    "DEHP": ({149: 100, 167: 30, 57: 20, 71: 15, 279: 10, 113: 8, 70: 8, 83: 6, 104: 5, 65: 4, 76: 4, 390: 1},
             {149: {"C": 8, "H": 5, "O": 3}, 390: {"C": 24, "H": 38, "O": 4}}, "phthalate", 390, "C24H38O4"),
    "eicosane": ({57: 100, 43: 80, 71: 65, 85: 40, 99: 12, 113: 8, 127: 6, 141: 5, 155: 4, 169: 3, 183: 3,
                  197: 2, 211: 2, 225: 2, 239: 1.5, 253: 1, 282: 2}, {282: {"C": 20, "H": 42}}, "alkane", 282, "C20H42"),
    "dichlorobenzene": ({146: 100, 111: 35, 75: 25, 50: 10, 74: 10}, {146: {"C": 6, "H": 4, "Cl": 2}},
                        "chlorinated", 146, "C6H4Cl2"),
    "bromobenzene": ({156: 100, 77: 80, 51: 30, 50: 15}, {156: {"C": 6, "H": 5, "Br": 1}}, "brominated", 156, "C6H5Br"),
    "methyl palmitate": ({74: 100, 87: 60, 43: 50, 55: 30, 143: 12, 227: 10, 270: 8, 239: 5, 57: 25, 69: 15},
                         {270: {"C": 17, "H": 34, "O": 2}}, "fame", 270, "C17H34O2"),
    "BHT": ({205: 100, 220: 25, 57: 25, 145: 10, 177: 5, 41: 10, 91: 5},
            {220: {"C": 15, "H": 24, "O": 1}, 205: {"C": 14, "H": 21, "O": 1}}, "hindered_phenol", 220, "C15H24O"),
    "benzophenone": ({105: 100, 77: 80, 182: 60, 51: 25, 181: 25, 152: 5}, {182: {"C": 13, "H": 10, "O": 1}},
                     "aryl_ketone", 182, "C13H10O"),
}


@pytest.mark.parametrize("name", list(CASES))
def test_class_molecular_ion_and_formula(name):
    ions, formulas, cls, m, formula = CASES[name]
    mz, ab = spectrum(ions, formulas)
    r = interpret(mz, ab, CTX)
    assert r.classes and r.classes[0].id == cls, [c.id for c in r.classes]
    assert r.m is not None and r.m.mz == m
    assert r.formulas and r.formulas[0].formula == formula
    assert r.summary


def test_isotope_hints():
    r = interpret(*spectrum(*CASES["dichlorobenzene"][:2]), CTX)
    assert [h.label for h in r.isotopes] == ["Cl2"] and r.m.hetero == {"Cl": 2}
    r = interpret(*spectrum(*CASES["bromobenzene"][:2]), CTX)
    assert [h.label for h in r.isotopes] == ["Br"]
    for name in ("DEHP", "eicosane", "methyl palmitate", "BHT", "benzophenone"):
        r = interpret(*spectrum(*CASES[name][:2]), CTX)
        assert not r.isotopes, (name, r.isotopes)
    d4 = spectrum({281: 100, 73: 20, 265: 15, 249: 10, 133: 10},
                  {281: {"C": 7, "H": 21, "O": 4, "Si": 4}, 265: {"C": 6, "H": 17, "O": 4, "Si": 4}})
    r = interpret(*d4, CTX)
    assert r.classes[0].id == "siloxane_cyclic" and any(h.label == "Si4" for h in r.isotopes)


def test_isotope_peak_never_molecular_ion():
    # strong M with a visible M+1: the M+1 must not be reported as M
    mz, ab = spectrum({128: 100, 102: 8, 127: 10}, {128: {"C": 10, "H": 8}})
    r = interpret(mz, ab, CTX)
    assert r.m.mz == 128 and r.m.mz % 2 == 0
    assert "even" in " ".join(r.m.evidence)


def test_illogical_loss_demotes_molecular_ion():
    clean = interpret(*spectrum({200: 60, 185: 100, 91: 30}), CTX)
    mixed = interpret(*spectrum({200: 60, 190: 100, 91: 30}), CTX)     # loss of 10 u: illogical
    assert mixed.m.score < clean.m.score
    assert any(l.illogical for l in mixed.losses)


def test_alkane_without_molecular_ion():
    ions = {57: 100, 43: 85, 71: 70, 85: 45, 99: 15, 113: 10, 127: 7, 141: 5, 155: 3, 169: 2}
    r = interpret(*spectrum(ions), CTX)
    assert r.classes[0].id == "alkane"
    assert r.m is None and "no molecular ion" in r.m_note


def test_ions_below_scan_start_are_not_absent():
    # a primary amide whose m/z 30 etc. would be below the scan range must still be recognised
    ions = {59: 100, 72: 70, 55: 50, 41: 45, 43: 40, 69: 20, 83: 15, 98: 10, 114: 6, 281: 5}
    r = interpret(*spectrum(ions, {281: {"C": 18, "H": 35, "N": 1, "O": 1}}), Context(mass_range=(50, 700),
                                                                                       min_abundance=150))
    assert r.classes[0].id == "fatty_amide"


def test_library_hit_contradictions():
    mz, ab = spectrum(*CASES["dichlorobenzene"][:2])
    r = interpret(mz, ab, Context(mass_range=(35, 700), min_abundance=150,
                                  library_hit={"name": "Toluene", "formula": "C7H8", "mw": 92}))
    texts = " ".join(t for _lvl, t in r.checks)
    assert "has no Cl" in texts and "above the molecular mass" in texts
    mz, ab = spectrum(*CASES["eicosane"][:2])
    r = interpret(mz, ab, Context(mass_range=(35, 700), min_abundance=150, ri=2000))
    assert any("n-C20" in t for _l, t in r.checks)


def test_fingerprints_rank_substances():
    mz, ab = spectrum(*CASES["DEHP"][:2])
    r = interpret(mz, ab, CTX)
    assert r.compounds[0].name.startswith("Bis(2-ethylhexyl) phthalate")
    assert r.marks()[390][0].startswith("M⁺·")


def test_knowledge_base_is_valid():
    ids = [r["id"] for r in RULES]
    assert len(ids) == len(set(ids))
    for r in RULES:
        assert not validate_rule(r), validate_rule(r)
    for f in FINGERPRINTS:
        assert not validate_fingerprint(f), f["name"]
        assert f.get("cls", "") in ("", *ids), f["name"]
    assert all(len(s["ions"]) >= 4 for s in SERIES.values())


def test_user_rules_file(tmp_path):
    import json
    from gcws.ms import knowledge
    p = tmp_path / "interpret_rules.json"
    p.write_text(json.dumps({"rules": [{"id": "lab_marker", "label": "Lab marker", "require": [{"ion": 123, "min": 50}]},
                                       {"id": "broken", "require": [{"bogus": 1}]}],
                             "fingerprints": [{"name": "Marker X", "mw": 180, "ions": [[123, 100], [180, 40]]}]}),
                 encoding="utf-8")
    rules, fps = knowledge.load(p)
    assert any(r["id"] == "lab_marker" for r in rules) and not any(r["id"] == "broken" for r in rules)
    r = interpret(*spectrum({123: 100, 180: 40, 95: 20}), CTX, rules=rules, fingerprints=fps)
    assert r.classes[0].id == "lab_marker" and r.compounds[0].name == "Marker X"


def test_empty_and_tiny_spectra():
    assert interpret([], [], CTX).summary == "empty spectrum"
    r = interpret([91], [500], CTX)
    assert r.warnings                                   # weak / sparse spectrum

"""Which determination a replicate group is (gcws.quant.determination): plain data, no workspace."""
from gcws.quant import determination as DT

A, B, C = "07_26016606_130m_min_GIOSUN1635_A.D", "11_26016606_130m_min_GIOSUN1635_B.D", "15_26016606_x_C.D"
PLAIN = "07_26016606_130m_min_GIOSUN1635.D"
NAMES = {"a": A, "b": B, "c": C, "p": PLAIN, "one": "03_26016606_x_1.D", "two": "05_26016606_x_2.D"}


def group(*members, **kw):
    return dict({"id": "g", "name": "s", "members": list(members), "policy": "all"}, **kw)


def test_a_pair_is_a_double_determination_with_both_reported():
    d = DT.of({}, group("a", "b"), NAMES)
    assert (d.kind, d.reported, d.control) == (DT.DOUBLE, ("a", "b"), None)
    assert d.compared and not d.single and d.label == "double determination"


def test_a_lone_run_without_a_suffix_is_a_single_determination():
    d = DT.of({}, group("p"), NAMES)
    assert (d.kind, d.reported, d.control, d.note) == (DT.SINGLE, ("p",), None, "")
    assert d.single and not d.compared and d.label == "single determination"


def test_a_lone_suffixed_run_is_an_incomplete_double_determination():
    for rid in ("a", "b", "one"):
        d = DT.of({}, group(rid), NAMES)
        assert (d.kind, d.reported, d.control) == (DT.INCOMPLETE, (rid,), None)
        assert not d.single and d.label == "single determination"


def test_an_n_fold_group_reports_every_member():
    d = DT.of({}, group("a", "b", "c"), NAMES)
    assert (d.kind, d.reported, d.control) == (DT.DOUBLE, ("a", "b", "c"), None)
    assert d.label == "3-fold determination"


def test_the_suffixes():
    assert [DT.suffix(n) for n in (A, B, PLAIN, "x_rep2.D", "x_1", "26016606")] == ["A", "B", "", "REP2", "1", ""]


def test_a_suffixed_run_made_single_by_hand_is_a_single_determination():
    d = DT.of({}, group("a", single=True), NAMES)
    assert (d.kind, d.reported, d.control) == (DT.SINGLE, ("a",), None)
    assert d.single and d.label == "single determination"

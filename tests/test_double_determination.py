"""The double determination's group changes (gcws.quant.double_determination): no GUI, no workspace."""
import copy

from gcws.quant import double_determination as DD

AT = "2026-10-11T09:00:00"


def groups(signoff=True):
    g = {"id": "g1", "name": "pair", "members": ["a", "b"], "policy": "all"}
    if signoff:
        g["signoff"] = {"by": "anna", "at": AT, "open": 0}
    return [g, {"id": "g2", "name": "single", "members": ["c"], "policy": "all"}]


def g1(gs):
    return next(g for g in gs if g["id"] == "g1")


def test_accept_then_is_accepted():
    gs = DD.accept(groups(signoff=False), "g1", "bob", AT, 2)
    assert DD.is_accepted(g1(gs)) and g1(gs)["signoff"] == {"by": "bob", "at": AT, "open": 2}
    assert not DD.is_accepted(None) and not DD.is_accepted({"signoff": {"by": "x", "stale": True}})


def test_every_change_of_what_was_accepted_stales_the_signoff():
    row = {"rt": 10.0, "name": "X", "report": True}
    changed = [
        DD.set_edits(groups(), "g1", "edits", [(row, "comment", "c")], "bob", AT)[0],
        DD.reset_edits(groups(), "g1", "edits"),
        DD.set_members(groups(), "g1", ["a", "d"]),
        DD.set_policy(groups(), "g1", "majority"),
        DD.stale(groups(), "g1"),
        DD.pair(groups(), ["a", "c"], "new"),
    ]
    for gs in changed:
        assert g1(gs)["signoff"]["stale"] and g1(gs)["signoff"]["by"] == "anna"   # who accepted is kept


def test_no_change_keeps_the_acceptance():
    for gs in (DD.set_members(groups(), "g1", ["a", "b"]), DD.set_policy(groups(), "g1", "all"),
               DD.rename(groups(), "g1", "other name")):
        assert DD.is_accepted(g1(gs))


def test_changes_never_touch_the_list_passed_in():
    before = groups()
    kept = copy.deepcopy(before)
    DD.set_members(before, "g1", ["x"])
    DD.set_edits(before, "g1", "edits", [({"rt": 1.0}, "a1", 2.0)], "bob", AT)
    DD.pair(before, ["c", "b"], "p")
    assert before == kept


def test_set_edits_logs_and_drops_empty_edits():
    row = {"rt": 10.0, "name": "X", "a1": 1.0}
    gs, logs = DD.set_edits(groups(), "g1", "hs_edits:mg/m²", [(row, "a1", 2.0)], "bob", AT)
    e = g1(gs)["hs_edits:mg/m²"]
    (key, edit), = e.items()
    assert edit["a1"] == 2.0 and edit["by"] == "bob" and logs == [("X (RT 10.000): a1", "1.0", "2.0")]
    gs, logs = DD.set_edits(gs, "g1", "hs_edits:mg/m²", [(dict(row, edit_key=key), "a1", None)], "bob", AT)
    assert g1(gs)["hs_edits:mg/m²"] == {} and logs[0][2] == "reset"
    assert DD.edits_of(g1(gs), {"mode": "hs_screening"}, "mg/m²") == {}


def test_pair_takes_the_runs_from_other_pairs_and_singles():
    gs = groups() + [{"id": "g3", "name": "three", "members": ["a", "c", "e"], "policy": "all"}]
    out = DD.pair(gs, ["a", "c"], "new")
    assert g1(out)["members"] == ["a", "c"]                        # the pair of a, reused
    assert not any(g["id"] == "g2" for g in out)                  # the single c gave c up: gone
    assert next(g for g in out if g["id"] == "g3")["members"] == ["a", "c", "e"]   # 3+ keeps its own
    out = DD.pair(groups(), ["c", "d"], "new")                     # the single of c becomes the pair
    assert next(g for g in out if g["id"] == "g2")["members"] == ["c", "d"]
    out = DD.pair([], ["x", "y"], "fresh")
    assert out[0]["name"] == "fresh" and out[0]["members"] == ["x", "y"] and out[0]["policy"] == "all"


def test_make_single_takes_a_run_out_of_its_pair():
    gs = DD.make_single(groups(), "a", "a alone")
    (g,) = [g for g in gs if "a" in g["members"]]
    assert g["id"] == "g1" and g["members"] == ["a"] and g["single"] is True
    assert g["signoff"]["stale"] and g["signoff"]["by"] == "anna"         # what was accepted changed
    assert not any("b" in g["members"] for g in gs)                         # B stays unused


def test_make_single_of_a_lone_run_and_accepting_it():
    gs = DD.make_single(groups(), "d", "d")
    (g,) = [g for g in gs if g["members"] == ["d"]]
    assert g["name"] == "d" and g["single"] is True and not g.get("signoff")
    gs = DD.accept(gs, g["id"], "bob", AT, 0)
    assert DD.is_accepted(next(x for x in gs if x["id"] == g["id"]))
    again = DD.make_single(gs, "d", "d")                                    # already single: nothing changes
    assert DD.is_accepted(next(x for x in again if x["id"] == g["id"]))


def test_pairing_a_single_determination_again_is_no_longer_single():
    gs = DD.pair(DD.make_single(groups(), "a", "a"), ["a", "b"], "pair")
    assert g1(gs)["members"] == ["a", "b"] and "single" not in g1(gs)

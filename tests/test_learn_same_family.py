"""Scoring: a program 'Sum of …' line names the analyst's group label also across inflection endings
(German plural 'oligomere', 'Oligomeren') and capitalisation; a different family does not match."""
from gcws.learn.score import same_family


def test_inflections_match():
    s = "Sum of styrene oligomers (estimated)**"
    for label in ("styrene oligomere", "Styrene Oligomere", "Styrene oligomer", "styrene oligomers", "styrene oligomeren"):
        assert same_family(label, s), label
    assert same_family("Hydrocarbon", "Sum of hydrocarbons (alkanes, cyclic alkanes, aliphatic ... estimated)**")


def test_other_families_do_not_match():
    assert not same_family("styrene oligomere", "Sum of hydrocarbons (alkanes, cyclic alkanes, aliphatic ... estimated)**")
    assert not same_family("fatty acid ester", "Sum of fatty alcohols")
    assert not same_family("", "Sum of styrene oligomers")

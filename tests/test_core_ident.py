"""The identification module's rules (gcws.core.ident): the analyst's identification, whether a peak is
unidentified and which peak is near a retention time."""
from types import SimpleNamespace

import pytest

from gcws.core import ident as I


def _searched(**kw):
    d = dict(apex_rt=5.0, name="Styrene", cas="100-42-5", score=91, status="Accepted", formula="C8H8",
             library="nist", hits=[{"name": "Styrene"}, {"name": "Cyclooctatetraene"}], source="EI Atlas",
             method="m", searched_at="t", spectrum_mode="apex", istd="IS1", peak_id="f1")
    d.update(kw)
    return I.Identification(**d)


def test_typed_name_drops_what_described_the_top_hit():
    new = I.typed_by_analyst(_searched(), 5.0, name="Toluene")
    assert (new.name, new.cas, new.status, new.manual) == ("Toluene", "100-42-5", I.TYPED, True)
    assert new.score is None and new.formula == "" and new.library == ""
    assert len(new.hits) == 2 and new.istd == "IS1" and new.peak_id == "f1" and new.source == "manual"


def test_typed_cas_keeps_the_hit_of_the_same_name():
    new = I.typed_by_analyst(_searched(), 5.0, cas="100-42-6", source="double determination")
    assert (new.name, new.cas, new.score, new.formula, new.library) == ("Styrene", "100-42-6", 91, "C8H8", "nist")
    assert new.status == I.TYPED and new.source == "double determination"


def test_typed_on_a_peak_without_identification():
    new = I.typed_by_analyst(None, 7.0, name="X")
    assert (new.apex_rt, new.name, new.cas, new.hits, new.istd) == (7.0, "X", "", [], "")


def test_chosen_hit_keeps_the_search():
    hit = {"name": "Cyclooctatetraene", "cas": "629-20-9", "score": 80, "formula": "C8H8", "library": "nist"}
    new = I.hit_chosen_by_analyst(_searched(), 5.0, hit)
    assert (new.name, new.cas, new.score, new.status, new.manual) == ("Cyclooctatetraene", "629-20-9", 80,
                                                                       I.CHOSEN, True)
    assert (new.source, new.method, new.searched_at, new.istd, new.peak_id) == ("EI Atlas", "m", "t", "IS1", "f1")
    assert I.hit_chosen_by_analyst(None, 5.0, hit).hits == [hit]


@pytest.mark.parametrize("name,status,expected", [
    ("", "", True), ("  ", "", True), ("unknown (m/z 149, 57)", "", True), ("Unbekannt 3", "", True),
    ("no hit found", "", True), ("Kein Treffer", "", True), ("?", "", True), ("n/a", "", True),
    ("Styrene", "", False), ("Styrene", "Unknown", True), ("Sum of styrene oligomers", "", False),
])
def test_unidentified(name, status, expected):
    assert I.is_unidentified(name, status) is expected


def test_an_empty_name_is_not_a_name_of_an_unknown():
    assert not I.names_unknown("") and I.names_unknown("Unknown") and not I.names_unknown("Undecane")


def test_peak_near():
    peaks = [SimpleNamespace(apex_rt=t) for t in (1.0, 2.0, 2.06)]
    assert I.peak_near(peaks, 2.04) == 2
    assert I.peak_near(peaks, 1.05) == 0                     # the limit itself counts
    assert I.peak_near(peaks, 1.5) is None and I.peak_near([], 1.0) is None

"""Substance flags: new to the lab (learned evaluations, register of reported substances, CASINFO) and elements
other than C, H, O, N; shown in the substance table and the replicate table."""
import json
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture()
def refs(tmp_path, monkeypatch):
    """Learned evaluation with BHT, register with DEHP, CASINFO with Irganox 1076."""
    from gcws import paths
    from gcws.quant import service as QS
    from gcws.quant import substance_flags as SF
    monkeypatch.setattr(paths, "DATA", tmp_path)
    corpus = tmp_path / "learn" / "corpus"
    corpus.mkdir(parents=True)
    ev = {"final": [{"label": "Butylated Hydroxytoluene", "cas": "128-37-0"},
                    {"label": "Styrene/aMeStyrene Dimer", "cas": ""},
                    {"label": "unknown m/z 73", "cas": ""}],
          "report": []}
    (corpus / "a.json").write_text(json.dumps({"id": "a", "entry": {}, "evaluation": ev}), encoding="utf-8")
    con = sqlite3.connect(tmp_path / "unknown_register.sqlite")
    con.execute("CREATE TABLE reported_substances (substance_id INTEGER PRIMARY KEY, cas_key TEXT, name_key TEXT, "
                "display_name TEXT, first_seen TEXT, last_seen TEXT)")
    con.execute("INSERT INTO reported_substances (cas_key, name_key) VALUES ('117-81-7', 'dehp')")
    con.commit()
    con.close()
    monkeypatch.setattr(QS, "cas_lookup", lambda: {"2082-79-3": {"sml": 6.0}})
    monkeypatch.setattr(SF, "casinfo_path", lambda: None)
    monkeypatch.setattr(SF, "_KNOWN", SF._Known())
    SF._flags.cache_clear()
    return SF


def test_elements():
    from gcws.quant import substance_flags as SF
    assert SF.elements("C6H5Cl") == ["C", "H", "Cl"]
    assert SF.foreign_elements("C16H34O") == [] and SF.foreign_elements("C17D36") == []
    assert SF.foreign_elements("C14H42O7Si7") == ["Si"]
    assert SF.foreign_elements("C42H63O4P") == ["P"]
    assert SF.foreign_elements("C₈H₈Br₂") == ["Br"]
    assert SF.foreign_elements("") == []


def test_new_to_the_lab(refs):
    SF = refs
    assert SF.is_new("BHT", "128-37-0") is False                     # learned evaluation (by CAS)
    assert SF.is_new("BHT", "0000128-37-0") is False                 # leading zeros
    assert SF.is_new("Bis(2-ethylhexyl) phthalate", "117-81-7") is False      # register
    assert SF.is_new("Irganox 1076", "2082-79-3") is False           # CASINFO
    assert SF.is_new("Styrene/aMeStyrene  dimer", "") is False       # by name, without a CAS
    assert SF.is_new("Some new thing", "999-99-9") is True
    assert SF.is_new("Another one", "") is True
    assert SF.is_new("unknown (m/z 73, 147)", "") is None            # an unknown is no substance
    text, tip = SF.flags("Hexasiloxane, tetradecamethyl-", "107-52-8", "C14H42O5Si6")
    assert text == "new · Si" and "CASINFO" in tip and "Si" in tip
    assert SF.flags("BHT", "128-37-0", "C15H24O") == ("", "")


def _ident(**kw):
    from gcws.core.ident import Identification
    return Identification(apex_rt=1.0, **kw)


def test_formula_from_the_hits():
    from gcws.quant import substance_flags as SF
    i = _ident(name="Tris(2,4-di-tert-butylphenyl) phosphate", cas="95906-11-9",
               hits=[{"name": "x", "cas": "1-11-1", "formula": "C2H6"},
                     {"name": "Tris(2,4-di-tert-butylphenyl) phosphate", "cas": "95906-11-9",
                      "formula": "C42H63O4P"}])
    assert SF.formula_of(i) == "C42H63O4P"
    assert SF.formula_of(_ident(name="a", formula="C2H5Cl")) == "C2H5Cl"
    st = SimpleNamespace(ident_set=lambda key: SimpleNamespace(items=[i]))
    index = SF.formula_index(SimpleNamespace(states=lambda: [st]))
    assert SF.formula_from(index, "whatever", "95906-11-9") == "C42H63O4P"


def test_substance_table_column(refs):
    from gcws.quant import peak_values as PV
    from gcws.ui.models.peak_table import COLUMN_KEYS
    assert "flags" in COLUMN_KEYS
    row = SimpleNamespace(ident=_ident(name="Chlorobenzene", cas="108-90-7", formula="C6H5Cl"))
    assert PV.substance_flags(row)[0] == "new · Cl"
    istd = SimpleNamespace(ident=_ident(name="Perdeutero-Heptadecane", cas="", istd="IS1"))
    assert PV.substance_flags(istd) == ("", "")
    assert PV.substance_flags(SimpleNamespace(ident=None)) == ("", "")


def test_replicate_table_flags(refs):
    from gcws.ui.docks import duplicate as D
    assert D.COLUMN_KEYS[D.C_FLAGS] == "flags" and D.UNIT_OF and D.C_FLAGS not in D.UNIT_OF
    row = {"name": "Octamethylcyclotetrasiloxane", "cas": "556-67-2", "source1": {}, "source2": None}
    text, tip = D.DuplicatePage._flags(row, {"556-67-2": "C8H24O4Si4"})
    assert text == "new · Si"
    assert D.DuplicatePage._flags({"name": "BHT", "cas": "128-37-0"}, {}) == ("", "")

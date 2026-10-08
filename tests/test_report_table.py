"""The Template report's table: columns per view, row rules, sums, SML bold, footnotes (gcws.report.table)."""
import pytest

from gcws.quant import conversion as CV
from gcws.report import table as TB
from gcws.report import template as TP
from gcws.report.template_data import ReportData


def _row(rt, name, cas="", each=(1.0,), mean=None, *, score=90, report=True, sml=None, ref="", footnote="",
         istd=False, dismissed=0, reldiff=None, comment=""):
    n = len(each)
    mean = mean if mean is not None else sum(v for v in each if v is not None) / len([v for v in each if v is not None])
    per = lambda vals, agg: {"each": list(vals), "agg": agg}
    return {"index": 0, "rt": rt, "name": name, "cas": cas, "mean": mean, "dismissed": dismissed,
            "report": report, "deleted": False, "level": "",
            "values": {"rt": per([rt] * n, rt), "name": per([name] * n, name), "cas": per([cas] * n, cas),
                       "score": per([score] * n, score), "conc": per(each, mean), "conc:mg_kg": per(each, mean),
                       "conc:mg_dm2": per([v / 6 if v is not None else None for v in each], mean / 6),
                       "istd": per(["IS1" if istd else ""] * n, ""), "sml": sml, "ref": ref, "footnote": footnote,
                       "reldiff": reldiff, "comment": comment, "verdict": "", "notes": ""},
            "flags": {"istd": istd, "nameless": not name and not cas, "library_sum": name.lower().startswith("sum of"),
                      "unidentified": not name or name.lower().startswith("unknown")},
            "slim": {"rt": rt, "name": name, "cas": cas, "mean": mean}}


def _data(rows, n=1, mode="nias_mgkg", **kw):
    q = {"mode": mode}
    det = "single determination" if n == 1 else "double determination"
    return ReportData(mode=mode, quant=dict(q, _detector="TIC" if mode == "hs_screening" else "FID"), n=n,
                      labels=["A", "B", "C"][:n], names=[f"run{k}" for k in range(n)], rows=rows,
                      values={"sample": "26016605", "determination": det, "analyst": "bda", "operator": "",
                              "migrate": "Ethanol 95 % | 40 °C | 10 d", "sv_ratio": "6"},
                      units=CV.available_units(q), report_units=CV.report_units(q), method_limit=0.01, **kw)


def _texts(table, col):
    i = [c.header for c in table.columns].index(col)
    return [r.cells[i].value for r in table.rows]


def test_the_nias_preset_on_a_single_determination():
    rows = [_row(14.0, "Tributyl acetylcitrate", "77-90-7", (0.5,), sml=60.0, ref="10/2011", footnote="Group SML"),
            _row(12.0, "Bisphenol A", "80-05-7", (0.2,), sml=0.05, ref="10/2011"),
            _row(13.0, "Unknown (m/z 91)", "", (0.004,)),                     # below 0.01 mg/kg
            _row(13.4, "IS1", "", (1.0,), istd=True),
            _row(15.0, "Erucamide", "112-84-5", (0.03,), report=False)]          # not ticked
    t = TB.build(_data(rows), TP.preset("NIAS"))
    assert [c.header for c in t.columns] == ["RT (min)", "Name", "CAS-No.", "% match", "Conc. mg/dm²", "Conc. mg/kg",
                                             "SML (mg/kg)", "Ref."]
    assert _texts(t, "Name") == ["Bisphenol A", "Tributyl acetylcitrate"]            # by RT
    assert _texts(t, "Conc. mg/dm²") == pytest.approx([0.2 / 6, 0.5 / 6])
    mgkg = [c.header for c in t.columns].index("Conc. mg/kg")
    assert [r.cells[mgkg].bold for r in t.rows] == [True, False]                     # BPA above its SML
    ref = [c.header for c in t.columns].index("Ref.")
    assert [r.cells[ref].marker for r in t.rows] == ["", "a"] and t.footnotes == ["Group SML"]
    assert t.rows[0].cells[2].link.endswith("80-05-7")
    assert t.title == "GC-MS/FID – NIAS-Screening –" and t.subtitle == "PA 26.007, single determination"
    assert t.header_lines[1] == [("Migrate:", "Ethanol 95 % | 40 °C | 10 d"), ("S/V-ratio", "6"), ("Operator:", "")]
    assert t.reported == [{"name": "Bisphenol A", "cas": "80-05-7", "rt": 12.0},
                          {"name": "Tributyl acetylcitrate", "cas": "77-90-7", "rt": 14.0}]
    assert t.summary["sml_exceedances"][0]["name"] == "Bisphenol A"
    assert t.orientation == "landscape" and not t.warnings


def test_views_of_a_double_determination():
    tpl = TP.preset("Empty")
    tpl["columns"] = [{"field": "name"}, {"field": "conc:mg_kg", "view": "each_mean", "decimals": 3},
                      {"field": "conc:mg_dm2", "view": "merged", "decimals": 2}, {"field": "name", "header": "Hit",
                                                                                  "view": "each"},
                      {"field": "reldiff"}, {"field": "comment"}]
    rows = [_row(10.0, "A substance", "", (1.2, 2.4), mean=1.2, dismissed=2, reldiff=None, comment="B spiked")]
    t = TB.build(_data(rows, n=2), tpl)
    assert [c.header for c in t.columns] == ["Name", "Conc. mg/kg A", "Conc. mg/kg B", "Conc. mg/kg mean",
                                             "Conc. mg/dm²", "Hit A", "Hit B", "Diff. %", "Comment"]
    cells = [c.value for c in t.rows[0].cells]
    assert cells[1:4] == pytest.approx([1.2, 2.4, 1.2])
    assert cells[4] == "0.20 / (0.40)"                                                # B dismissed
    assert cells[-1] == "B spiked"
    single = TB.build(_data([_row(10.0, "A substance")]), tpl)                        # one determination
    assert [c.header for c in single.columns] == ["Name", "Conc. mg/kg", "Conc. mg/dm²", "Hit"]
    assert not single.warnings                                                        # dd columns left out silently


def test_columns_the_mode_cannot_fill_are_left_out_with_a_warning():
    tpl = TP.preset("NIAS")
    t = TB.build(_data([_row(10.0, "X", "", (1.0,))], mode="hs_screening"), tpl)
    headers = [c.header for c in t.columns]
    assert "Conc. mg/kg" not in headers and "RT (min)" in headers
    assert any("Conc. mg/kg" in w for w in t.warnings)
    assert any("limit not applied" in w for w in t.warnings)


def test_nias_category_and_repeated_sums():
    rows = [_row(10.0, "Hydrocarbons C20-C24", "", (0.05,)), _row(11.0, "Hydrocarbons C25", "", (0.07,)),
            _row(12.0, "Diethyl phthalate", "84-66-2", (0.02,)), _row(13.0, "Diethyl phthalate", "84-66-2", (0.03,)),
            _row(14.0, "Irgafos 168", "31570-04-4", (0.1,))]
    t = TB.build(_data(rows), TP.preset("NIAS"))
    names = _texts(t, "Name")
    assert names[0] == "Irgafos 168"
    assert any(n.startswith("Sum of hydrocarbons") for n in names)
    assert "Sum of Diethyl phthalate" in names
    i = names.index("Sum of Diethyl phthalate")
    assert _texts(t, "Conc. mg/kg")[i] == pytest.approx(0.05)
    assert [r.kind for r in t.rows] == ["substance", "sum", "sum"]
    assert all(c.bold for c in t.rows[1].cells)
    assert any(n.startswith("**") for n in t.notes)


def test_an_empty_report_keeps_its_text_and_empty_columns_can_go():
    t = TB.build(_data([_row(10.0, "X", "", (0.001,))]), TP.preset("NIAS"))
    assert t.rows == [] and t.empty_text == "No substance above 10 ppb detected."
    tpl = TP.preset("NIAS")
    tpl["extras"]["hide_empty_columns"] = True
    t = TB.build(_data([_row(10.0, "X", "", (1.0,))]), tpl)
    assert "Ref." not in [c.header for c in t.columns] and "SML (mg/kg)" not in [c.header for c in t.columns]


def test_row_rules():
    tpl = TP.preset("Empty")
    tpl["columns"] = [{"field": "name"}, {"field": "conc"}]
    tpl["rows"].update(unidentified="hide", min_score=80, sort="conc", only_reported=False)
    rows = [_row(10.0, "Unknown 1", "", (5.0,)), _row(11.0, "Low score", "", (1.0,), score=60),
            _row(12.0, "Small", "", (1.0,)), _row(13.0, "Big", "", (3.0,), report=False)]
    t = TB.build(_data(rows), tpl)
    assert _texts(t, "Name") == ["Big", "Small"]
    tpl["rows"]["limit"] = {"field": "conc", "value": 2.0, "use_method": False}
    assert _texts(TB.build(_data(rows), tpl), "Name") == ["Big"]


def test_the_table_survives_a_round_trip():
    t = TB.build(_data([_row(10.0, "X", "50-00-0", (1.0,), footnote="note")]), TP.preset("NIAS"))
    back = TB.ReportTable.from_dict(t.to_dict())
    assert back == t
    assert TB.fmt(1.23456, 2) == "1.23" and TB.fmt(None, 2) == "" and TB.fmt("x", 2) == "x"

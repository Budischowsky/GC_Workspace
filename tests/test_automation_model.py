"""P57: automation workflows (process chart), their validation, the arrows' filters and the
Report² rules - all without Qt."""
from pathlib import Path

import pytest


@pytest.fixture()
def data(tmp_path, monkeypatch):
    from gcws import paths
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    return tmp_path


def _example(tmp_path):
    from gcws.automation import templates
    src = tmp_path / "watch"
    src.mkdir(exist_ok=True)
    return templates.make("nias", "Example", source=str(src), method="NIAS", folder_a=str(tmp_path / "A"),
                          folder_b=str(tmp_path / "B"))


def test_workflow_json_round_trip(data):
    from gcws.automation import workflow as W
    wf = _example(data)
    path = wf.save()
    assert path.parent == data / "data" / "automation" / "workflows"
    back = W.load(path)
    assert back.to_dict()["nodes"] == wf.to_dict()["nodes"] and back.digest() == wf.digest()
    assert [w.id for w in W.list_workflows()] == [wf.id]
    moved = back.copy()
    moved.nodes[0].x += 50                        # moving a step on the chart changes nothing that matters
    assert moved.digest() == wf.digest()
    moved.edges[0].filter = {"name": "2601*"}
    assert moved.digest() != wf.digest()
    W.delete(wf.id)
    assert W.list_workflows() == []


def test_validation(data):
    from gcws.automation import workflow as W
    wf = _example(data)
    issues = W.validate(wf, method_names=["NIAS"], word=True)
    assert not W.errors(issues), issues
    texts = lambda wf, **kw: " | ".join(i.text for i in W.validate(wf, method_names=["NIAS"], word=True, **kw))
    bad = wf.copy()
    bad.source.params["folder"] = str(data / "missing")
    assert "does not exist" in texts(bad)
    bad = wf.copy()
    bad.by_type("method")[0].params["method"] = "Other"
    assert "'Other' does not exist" in texts(bad)
    bad = wf.copy()
    bad.by_type("folder")[0].params["path"] = str(data / "watch" / "out")
    assert "inside the watched folder" in texts(bad)
    bad.by_type("folder")[0].params["allow_inside_source"] = True
    assert "inside the watched folder" not in texts(bad)
    bad = wf.copy()
    rep = bad.by_type("report")[0]
    e = bad.outgoing(rep.id)[0]
    e.filter["formats"] = ["dd"]
    assert "lets no file through" in texts(bad)
    bad = wf.copy()
    loose = bad.add_node("folder", 0, 0, path=str(data / "C"))
    assert "not connected" in texts(bad)
    assert bad.can_connect(bad.source.id, loose.id) == "Watched folder cannot pass to Target folder"
    assert bad.can_connect(bad.by_type("report")[0].id, loose.id) == ""
    two = wf.copy()
    two.add_node("source", 0, 0, folder=str(data))
    assert "one folder" in texts(two)
    pdf = W.validate(wf, method_names=["NIAS"], word=False)
    assert any("Microsoft Word" in i.text and i.level == "warning" for i in pdf)
    loader = lambda name: {"sections": {"quant": {"mode": "hs_screening"}}}
    both = " | ".join(i.text for i in W.validate(wf, method_names=["NIAS"], word=True, method_loader=loader))
    assert "no migration conditions" in both and "cannot make" in both
    wf.by_type("folder")[0].params["subfolder"] = "{batch}/{nope}"
    assert "Unknown placeholder" in texts(wf)


def test_routing_word_to_b_excel_to_a(data):
    from gcws.automation import routing as R
    wf = _example(data)
    m = wf.by_type("method")[0]
    rep = wf.by_type("report")[0]
    files = {rep.id: {"xlsx": Path("x/S_NIAS_Report.xlsx"), "docx": Path("x/S_NIAS_Report.docx"),
                      "pdf": Path("x/S_NIAS_Report.pdf")}}
    ctx = {"status": "accepted_auto", "name": "26016606", "batch": "26016605_GIOSUN1635"}
    out = R.deliveries(wf, m.id, ctx, files, {"batch": "26016605_GIOSUN1635", "sample": "26016606"})
    where = {(d.fmt, d.dst.parent) for d in out}
    assert where == {("xlsx", data / "A" / "26016605_GIOSUN1635"), ("docx", data / "B" / "26016605_GIOSUN1635"),
                     ("pdf", data / "B" / "26016605_GIOSUN1635")}
    # a report that needs control goes nowhere through an "accepted" arrow
    assert R.deliveries(wf, m.id, dict(ctx, status="control"), files) == []
    assert len(R.deliveries(wf, m.id, dict(ctx, status="accepted_manual"), files)) == 3


def test_filters_and_collisions(tmp_path):
    from gcws.automation import routing as R
    from gcws.automation.workflow import filter_text, passes
    f = {"status": ["control"], "formats": ["docx"], "name": "2601*;x*", "batch": ""}
    assert passes(f, {"status": "control", "fmt": "docx", "name": "26016606"})
    assert not passes(f, {"status": "accepted_auto", "fmt": "docx", "name": "26016606"})
    assert not passes(f, {"status": "control", "fmt": "xlsx", "name": "26016606"})
    assert not passes(f, {"status": "control", "fmt": "docx", "name": "99"})
    assert passes({}, {"status": "control"})
    assert filter_text(f) == "control needed, Word, sample 2601*;x*"
    (tmp_path / "a.docx").write_text("x")
    assert R.resolve_collision(tmp_path / "a.docx", "overwrite") == tmp_path / "a.docx"
    assert R.resolve_collision(tmp_path / "a.docx", "skip") is None
    assert R.resolve_collision(tmp_path / "a.docx", "version") == tmp_path / "a_2.docx"
    assert R.target_dir({"path": str(tmp_path), "subfolder": "{batch}/{status}"},
                        {"batch": "B1", "status": "control"}) == tmp_path / "B1" / "control needed"


def _evidence(**kw):
    ev = {"kind": "nias", "settings": {"reporting_limit": 0.01, "duplicate_max_reldiff": 30},
          "members": [{"name": "07_A", "mean_factor": 1.2, "blank_ok": True,
                       "standards": [{"code": "IS1", "name": "Perdeutero-Heptadecane", "role": "Quantification",
                                      "status": "Found", "fid_area": 9.5e6},
                                     {"code": "IS4", "name": "Dibutyl phthalate-d4", "role": "QC",
                                      "status": "Found", "fid_area": 1e6}]}],
          "rows": [{"rt": 10.0, "name": "Octane", "cas": "111-65-9", "mean": 0.5, "status": "Valid duplicate",
                    "id_status": "Accepted", "reldiff": 5.0, "sml": 60.0}],
          "summary": {"sml_exceedances": []}, "warnings": [], "errors": []}
    ev.update(kw)
    return ev


def test_rules_accept_clean_report_and_flag_problems():
    from gcws.automation import rules as RU
    rules = RU.default_rules()
    assert RU.evaluate(rules, _evidence()).status == RU.ACCEPTED_AUTO     # QC standard exempt from the window
    row = lambda **kw: dict(_evidence()["rows"][0], **kw)
    cases = {
        "manual_check": _evidence(rows=[row(id_status="Manual review")]),
        "sml_exceeded": _evidence(rows=[row(mean=70.0)]),
        "istd_qc": _evidence(members=[dict(_evidence()["members"][0], mean_factor=None)]),
        "processing_warnings": _evidence(warnings=["Word document not created: x"]),
        "no_blank": _evidence(members=[dict(_evidence()["members"][0], blank_ok=False)]),
    }
    for rid, ev in cases.items():
        res = RU.evaluate(rules, ev)
        assert res.status == RU.CONTROL and {f.rule for f in res.findings} == {rid}, (rid, res.findings)
    # deviating duplicate, conflict and artefact
    res = RU.evaluate(rules, _evidence(rows=[row(reldiff=45.0), row(name="B", status="Identification conflict"),
                                             row(name="C", mean=0.001, status="Artefact: only determination 1")]))
    assert len(res.findings) == 3
    # below the reporting limit a deviation does not count
    assert RU.evaluate(rules, _evidence(rows=[row(mean=0.005, reldiff=80.0)])).status == RU.ACCEPTED_AUTO
    # the area window
    low = _evidence()
    low["members"][0]["standards"][0]["fid_area"] = 2e6
    assert "area low" in RU.evaluate(rules, low).findings[0].text
    # the automatic ISTD detection disagrees with the peak the quantification used
    det = _evidence()
    det["members"][0]["istd_detection"] = {"IS2": {"rt": 18.918, "confidence": "medium", "applied": False,
                                                   "used_rt": 18.985},
                                           "IS1": {"rt": 13.417, "confidence": "high", "applied": True,
                                                   "used_rt": 13.417}}
    res = RU.evaluate(rules, det)
    assert [f.substance for f in res.findings] == ["IS2"] and "18.918" in res.findings[0].text
    # SML exceeded as the report counted it
    res = RU.evaluate(rules, _evidence(summary={"sml_exceedances": ["Bisphenol A"]}))
    assert res.findings[0].substance == "Bisphenol A"


def test_optional_rules_off_by_default_and_json(data):
    from gcws.automation import rules as RU
    ev = _evidence(rows=[dict(_evidence()["rows"][0], sml=None, mean=0.2),
                         dict(_evidence()["rows"][0], name="unknown (m/z 57)", sml=None, mean=0.3)])
    assert RU.evaluate(RU.default_rules(), ev).status == RU.ACCEPTED_AUTO
    rules = RU.default_rules()
    for r in rules:
        if r.id in ("no_sml_above_limit", "substance_above", "unidentified_over"):
            r.enabled = True
    rules[[r.id for r in rules].index("substance_above")].params.update(mgkg=0.25)
    rules[[r.id for r in rules].index("unidentified_over")].params.update(max=0)
    res = RU.evaluate(rules, ev)
    assert {f.rule for f in res.findings} == {"no_sml_above_limit", "substance_above", "unidentified_over"}
    info = RU.from_list(RU.to_list(rules))
    info[[r.id for r in info].index("substance_above")].level = "info"
    assert RU.to_list(RU.from_list(RU.to_list(info))) == RU.to_list(info)
    RU.save_default_rules(info)
    assert RU.to_list(RU.load_default_rules()) == RU.to_list(info)
    assert RU.to_list(RU.rules_for({})) == RU.to_list(info)
    assert RU.evaluate(RU.default_rules(), _evidence(), auto_accept=False).status == RU.CONTROL

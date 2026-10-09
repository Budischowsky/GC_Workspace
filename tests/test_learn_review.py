"""gcws.learn.review: disagreements with their evidence, stored verdicts, and their effect on the scores."""
import json

import pytest

from test_learn_baseline import _perfect


def _missing_one(entry, out_dir, method_name="NIAS", **kw):
    """The perfect program, except that it does not report the analyst's 7.07 line."""
    prog = _perfect(entry, out_dir, method_name, **kw)
    prog.reported = [r for r in prog.reported if abs(r["rt"] - 7.07) > 0.001]
    for p in prog.peaks:
        p.update(name="P" + str(p["rt"]), cas="", score=90.0, hits=[], class_hint="-")
    return prog


@pytest.fixture
def root(tmp_path):
    from learn_fixtures import make_workbook
    root = tmp_path / "root"
    make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    return root


def test_build_items(root, tmp_path):
    from gcws.learn.review import build_items, item_id
    items = build_items(root, tmp_path / "out", process_fn=_missing_one)
    (it,) = [i for i in items if i.type == "not_reported"]
    assert (it.batch, it.run, it.analyst, it.rt) == ("B1", "05_X_A.D", "BDa", 7.07)
    assert it.analyst_side["label"] == "Butyl methacrylate" and it.analyst_side["cas"] == "97-88-1"
    assert it.program_side["name"] == "P7.07" and it.program_side["score"] == 90.0
    assert it.id == item_id("B1", "05_X_A.D", "BDa", "not_reported", 7.07) and len(it.id) == 12
    assert [i.id for i in build_items(root, tmp_path / "out", process_fn=_missing_one)] == [i.id for i in items]


def test_verdict_store(root, tmp_path):
    from gcws.learn.review import build_items, load_verdicts, run_verdicts, save_verdict, write_review
    out = tmp_path / "out"
    items = build_items(root, out, process_fn=_missing_one)
    write_review(items, out)
    assert (out / "review" / "items.json").is_file() and "## Per type" in (out / "review" / "review.md").read_text(
        encoding="utf-8")
    it = next(i for i in items if i.type == "not_reported")
    save_verdict(out, it.id, "program", "the line is fine")
    v = load_verdicts(out)[it.id]
    assert v["verdict"] == "program" and v["note"] == "the line is fine" and v["type"] == "not_reported"
    assert run_verdicts(out, "B1", "05_X_A.D", "BDa") == {("not_reported", 7.07): "program"}
    with pytest.raises(ValueError):
        save_verdict(out, it.id, "maybe")
    assert not list((out / "review").glob("*.tmp"))


def test_baseline_uses_verdicts(root, tmp_path):
    from gcws.learn.baseline import run_baseline
    from gcws.learn.review import build_items, save_verdict, write_review
    out = tmp_path / "out"
    before = run_baseline(root, out, process_fn=_missing_one, progress=lambda t: None)["runs"][0]["score"]
    items = build_items(root, out, process_fn=_missing_one)
    write_review(items, out)
    save_verdict(out, next(i for i in items if i.type == "not_reported").id, "program")
    after = run_baseline(root, out, process_fn=_missing_one, progress=lambda t: None)["runs"][0]["score"]
    assert after["client_recall"] > before["client_recall"]


def test_cli_review(root, tmp_path, monkeypatch):
    from gcws.learn import __main__ as cli
    from gcws.learn import review
    monkeypatch.setattr(cli, "_ensure_app", lambda: None)
    real = review.build_items
    monkeypatch.setattr(review, "build_items", lambda r, o, m="NIAS", **k: real(r, o, m, process_fn=_missing_one))
    assert cli.main(["review", str(root), "--out", str(tmp_path / "o")]) == 0
    data = json.loads((tmp_path / "o" / "review" / "items.json").read_text(encoding="utf-8"))
    assert any(d["type"] == "not_reported" for d in data)

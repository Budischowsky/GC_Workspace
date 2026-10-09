"""gcws.learn.consistency: how far two analysts agree on the same run."""
import pytest


def _pair(tmp_path, second_rows):
    from learn_fixtures import make_workbook
    root = tmp_path / "root"
    a = make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BDa_ 05_X_A.xlsx")
    b = make_workbook(root / "B1/05_X_A.D/Auswertung/NIAS-Screening-SYN1_BlM_ 05_X_A.xlsx", final_rows=second_rows)
    return root, a, b


def test_compare_pair(tmp_path):
    from learn_fixtures import FINAL_ROWS
    from gcws.learn.consistency import compare_pair
    from gcws.learn.match import human_items
    from gcws.learn.workbook import parse_workbook
    rows = [r for r in FINAL_ROWS if r.get("A") != 6.917]                     # BlM removed the 6.917 peak
    rows = [dict(r, B="") if r.get("A") == 26.5 else r for r in rows]          # ... and left 26.5 unnamed
    root, a, b = _pair(tmp_path, rows)
    c = compare_pair(parse_workbook(a), parse_workbook(b))
    n = len(human_items(parse_workbook(a)))
    assert c["paired"] == n and c["only_a"] == c["only_b"] == 0
    assert c["keep_agree"] == pytest.approx((n - 1) / n)
    assert c["class_agree"] == pytest.approx((n - 2) / n)
    assert c["cas_agree"] == 1.0
    assert {d["rt"] for d in c["differences"]} == {6.917, 26.5}


def test_not_comparable(tmp_path):
    from gcws.learn.consistency import run_consistency
    root, _, _ = _pair(tmp_path, [{"A": "Ende"}])
    result = run_consistency(root, tmp_path / "out")
    assert result["pairs"][0]["comparable"] is False
    assert "not comparable" in (tmp_path / "out" / "consistency.md").read_text(encoding="utf-8")


def test_cli_consistency(tmp_path):
    from learn_fixtures import FINAL_ROWS
    from gcws.learn.__main__ import main
    root, _, _ = _pair(tmp_path, FINAL_ROWS)
    assert main(["consistency", str(root), "--out", str(tmp_path / "o")]) == 0
    text = (tmp_path / "o" / "consistency.md").read_text(encoding="utf-8")
    assert "## Totals" in text and "BDa" in text and "BlM" in text

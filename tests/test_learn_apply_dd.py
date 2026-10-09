"""Learned families with a double determination (A + B): the feature consensus must keep the family label."""
import copy

import pytest

from test_pipeline import A, B, METHOD, copy_batch, data, lib_oracle, qapp, spec  # noqa: F401 (fixtures)


@pytest.fixture
def batch(samples, tmp_path):
    return copy_batch(samples, tmp_path / "watch", ["06_", "07_", "08_", "11_", "13_"])


def _lines(res):
    from pathlib import Path
    from gcws.learn.runner import parse_program_report
    return parse_program_report(Path(res.files["r1"]["xlsx"]))


def test_learned_family_in_a_double_determination(qapp, data, batch, tmp_path):
    from gcws.automation import pipeline as PL
    plain = PL.run_job(spec(batch, tmp_path / "plain"), identify=lib_oracle)
    named = [l for l in _lines(plain) if l["kind"] == "line" and l["cas"]]
    assert named
    target = named[0]
    method = copy.deepcopy(METHOD)
    method["sections"]["learned_rules"] = {"version": 1, "families": [
        {"label": "hydrocarbon", "row_name": "Hydrocarbon", "sum_text": "Sum of hydrocarbons",
         "names": [target["name"]], "hints": [], "support": 9, "batches": 2}]}
    learned = PL.run_job(spec(batch, tmp_path / "learned", method=method), identify=lib_oracle)
    lines = _lines(learned)
    assert not any(l["name"] == target["name"] and l["rt"] is not None and abs(l["rt"] - target["rt"]) < 0.02
                   for l in lines)
    assert any(l["kind"] == "sum" and l["name"].startswith("Sum of hydrocarbons") for l in lines)

"""Learned families in a real automation job (sample batch copy, library hits from the old LIB)."""
import copy

import pytest

from test_pipeline import METHOD, copy_batch, data, lib_oracle, qapp, spec  # noqa: F401 (fixtures)
from test_learn_runner import _single  # noqa: F401


@pytest.fixture
def batch(samples, tmp_path):
    return copy_batch(samples, tmp_path / "watch", ["06_", "07_", "08_", "13_"])


def _lines(res):
    from pathlib import Path
    from gcws.learn.runner import parse_program_report
    return parse_program_report(Path(res.files["r1"]["xlsx"]))


def test_learned_family_renames_rows_in_the_report(qapp, data, batch, tmp_path):
    from gcws.automation import pipeline as PL
    plain = PL.run_job(_single(batch, tmp_path / "plain"), identify=lib_oracle)
    named = [l for l in _lines(plain) if l["kind"] == "line" and l["cas"]]
    assert named, "the sample needs at least one named report line"
    target = named[0]
    method = copy.deepcopy(METHOD)
    method["sections"]["learned_rules"] = {"version": 1, "families": [
        {"label": "hydrocarbon", "row_name": "Hydrocarbon", "sum_text": "Sum of hydrocarbons",
         "names": [target["name"]], "hints": [], "support": 9, "batches": 2}]}
    learned = PL.run_job(_single(batch, tmp_path / "learned", method=method), identify=lib_oracle)
    lines = _lines(learned)
    assert not any(l["name"] == target["name"] and l["rt"] is not None and abs(l["rt"] - target["rt"]) < 0.01
                   for l in lines)
    assert any(l["kind"] == "sum" and l["name"].startswith("Sum of hydrocarbons") for l in lines)


def test_without_learned_rules_the_report_is_unchanged(qapp, data, batch, tmp_path):
    from gcws.automation import pipeline as PL
    a = PL.run_job(_single(batch, tmp_path / "a"), identify=lib_oracle)
    method = copy.deepcopy(METHOD)
    method["sections"]["learned_rules"] = None
    b = PL.run_job(_single(batch, tmp_path / "b", method=method), identify=lib_oracle)
    assert _lines(a) == _lines(b)

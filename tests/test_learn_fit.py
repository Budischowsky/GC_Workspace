"""gcws.learn.fit: grid search with batch-grouped cross-validation (toy target: score = 1 - |x - optimum|)."""
from types import SimpleNamespace

import pytest

BATCHES = [f"b{i:02d}" for i in range(10)]


def _batches(optimum=3, overrides=None):
    overrides = overrides or {}
    return {b: [SimpleNamespace(optimum=overrides.get(b, optimum))] for b in BATCHES}


def toy(params, runs):
    if not runs:
        return None
    return sum(1 - abs(params["x"] - r.optimum) for r in runs) / len(runs)


def _split():
    from gcws.learn.folds import make_split
    return make_split(BATCHES)


def test_grid_keeps_preference_order():
    from gcws.learn.fit import grid
    assert grid({"a": [1, 2], "b": ["x", "y"]}) == [{"a": 1, "b": "x"}, {"a": 1, "b": "y"}, {"a": 2, "b": "x"},
                                                    {"a": 2, "b": "y"}]


def test_fit_finds_the_optimum():
    from gcws.learn.fit import fit
    r = fit("toy", {"x": [1, 2, 3, 4]}, {"x": 1}, _batches(), toy)
    assert r.best == {"x": 3} and r.cv_best > r.cv_current and r.accept_recommended
    assert r.test_best == 1 and r.test_current == pytest.approx(-1)
    assert [row["params"] for row in r.table] == [{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}]


def test_tie_prefers_first():
    from gcws.learn.fit import fit

    def flat(params, runs):
        return {1: 0.5, 2: 0.9, 3: 0.903, 4: 0.8}[params["x"]]

    assert fit("toy", {"x": [1, 2, 3, 4]}, {"x": 1}, _batches(), flat).best == {"x": 2}


def test_batch_loss_blocks_acceptance():
    from gcws.learn.fit import fit
    loser = next(b for b in BATCHES if b not in _split().test)
    r = fit("toy", {"x": [1, 2, 3, 4]}, {"x": 1}, _batches(overrides={loser: 1}), toy)
    assert r.best == {"x": 3} and not r.accept_recommended
    assert any(loser in n for n in r.notes)
    assert r.per_batch[loser]["current"] > r.per_batch[loser]["best"]


def test_failing_params_are_never_chosen():
    from gcws.learn.fit import fit

    def broken(params, runs):
        return None if params["x"] == 3 else toy(params, runs)

    r = fit("toy", {"x": [1, 2, 3, 4]}, {"x": 1}, _batches(), broken)
    assert r.best == {"x": 2} or r.best == {"x": 4}
    assert next(row for row in r.table if row["params"] == {"x": 3})["cv"] is None


def test_test_set_not_used_for_choice():
    from gcws.learn.fit import fit
    test = _split().test
    r = fit("toy", {"x": [1, 2, 3, 4]}, {"x": 1}, _batches(overrides={b: 4 for b in test}), toy)
    assert r.best == {"x": 3} and r.test_best == 0


def test_batch_no_candidate_can_score_is_left_out():
    from gcws.learn.fit import fit
    dead = next(b for b in BATCHES if b not in _split().test)

    def partial(params, runs):
        return None if runs[0].optimum is None else toy(params, runs)

    r = fit("toy", {"x": [1, 2, 3, 4]}, {"x": 1}, _batches(overrides={dead: None}), partial)
    assert r.best == {"x": 3} and r.cv_best is not None
    assert any(dead in n and "not scorable" in n for n in r.notes)

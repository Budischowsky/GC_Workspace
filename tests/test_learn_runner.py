"""gcws.learn.runner: the production job per evaluated run (single determination) and its per-peak evidence."""
import pytest

from test_pipeline import A, copy_batch, data, lib_oracle, qapp, spec  # noqa: F401 (fixtures)


@pytest.fixture
def one_run_batch(samples, tmp_path):
    return copy_batch(samples, tmp_path / "watch", ["06_", "07_", "08_", "13_"])


def _single(batch, out, **kw):
    return spec(batch, out, group={"key": "k", "name": A[:-2], "members": [A]}, **kw)


def test_run_job_inspect_hook(qapp, data, one_run_batch, tmp_path):
    from gcws.automation import pipeline as PL
    res = PL.run_job(_single(one_run_batch, tmp_path / "job"), identify=lib_oracle,
                     inspect=lambda ws, members: {"n": len(members)})
    assert res.evidence["inspect"] == {"n": 1}


def test_run_job_inspect_error_is_a_warning(qapp, data, one_run_batch, tmp_path):
    from gcws.automation import pipeline as PL

    def boom(ws, members):
        raise RuntimeError("x")

    res = PL.run_job(_single(one_run_batch, tmp_path / "job"), identify=lib_oracle, inspect=boom)
    assert res.state != PL.FAILED
    assert any(w.startswith("Learning evidence failed:") for w in res.warnings)


def test_collect_peaks(qapp, data, one_run_batch, tmp_path):
    from gcws.automation import pipeline as PL
    from gcws.learn.runner import PEAK_KEYS, collect
    res = PL.run_job(_single(one_run_batch, tmp_path / "job"), identify=lib_oracle, inspect=collect)
    peaks = res.evidence["inspect"]["peaks"]
    assert len(peaks) > 20
    assert all(set(PEAK_KEYS) | {"hits"} <= set(p) for p in peaks)
    assert all(p["area"] > 0 for p in peaks)
    assert any(p["name"] for p in peaks)
    assert [p["rt"] for p in peaks] == sorted(p["rt"] for p in peaks)
    named = next(p for p in peaks if p["name"])
    assert named["hits"] and {"name", "cas", "score"} <= set(named["hits"][0])

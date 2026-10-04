"""The fast Compare ends in exactly the state today's code ends in (real batch, auto split, closer look)."""
import pytest

FID_OVERRIDES = {"deconv_split": "auto", "deconv_level": 3, "deconv_probe": True}


@pytest.mark.parametrize("pair", ["07_:11_", "09_:12_"])
def test_compare_end_state_equals_reference(samples, qapp, monkeypatch, pair):
    import tools.compare_benchmark as CB
    from gcws.ms import component_fit as F
    a, b = pair.split(":")
    with monkeypatch.context() as m:
        CB.reference_mode(m.setattr)
        ws = CB.load(samples, ["06_", "08_", a, b], FID_OVERRIDES)
        ids = CB.group(ws, a, b)
        reference = CB.signature(ws, ids, CB.replay(ws, ids)["table"])
    F.fit_cache_clear()
    ws = CB.load(samples, ["06_", "08_", a, b], FID_OVERRIDES)
    ids = CB.group(ws, a, b)
    result = CB.replay(ws, ids)
    assert result["table"].features
    assert CB.signature(ws, ids, result["table"]) == reference
    # the replay went through the costly paths: gap fills made, peaks split by deconvolution
    assert any(origin == "gapfill" for f in reference[-1] for origin, _note in f[3])
    assert any(p[4] == "deconvoluted" for run in reference[:-1:2] for p in run)

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


def _entry(batch, run, blanks):
    from gcws.learn.corpus import CorpusEntry
    return CorpusEntry(workbook=str(batch / run / "Auswertung" / "wb.xlsm"), run_dir=str(batch / run),
                       batch_dir=str(batch), analyst="BDa", run_role="sample", blanks=list(blanks))


def test_build_spec(tmp_path):
    from gcws.learn.runner import build_spec
    e = _entry(tmp_path / "B", "05_X_A.D", ["03_EtOH_ISTD.D", "10_EtOH.D"])
    s = build_spec(e, {"name": "M", "sections": {}}, tmp_path / "job")
    assert s["group"]["members"] == ["05_X_A.D"]
    assert s["blanks"] == {"blank": ["10_EtOH.D"], "blank_istd": ["03_EtOH_ISTD.D"]}
    assert s["override"]["allow_no_blank"] is True
    assert s["reports"] == [{"node": "learn", "kind": "nias", "formats": ["xlsx"]}]
    assert s["batch_folder"] == e.batch_dir and s["method"]["name"] == "M"
    assert (s["search"], s["istd_detect"], s["min_confidence"], s["require_blank"]) == (True, True, "high", "auto")
    assert s["out_dir"] == str(tmp_path / "job")


def test_run_key():
    from gcws.learn.runner import run_key
    k = run_key(r"C:\x\B\05_X A.D", "NIAS")
    assert k == run_key(r"C:\x\B\05_X A.D", "NIAS") != run_key(r"C:\x\B\05_X A.D", "NIAS_decon5")
    assert k != run_key(r"C:\y\B\05_X A.D", "NIAS")
    assert all(c.isalnum() or c in "_.-" for c in k)


def test_process_caches(qapp, data, one_run_batch, tmp_path, monkeypatch):
    from test_pipeline import METHOD
    from gcws.automation import pipeline as PL
    from gcws.learn.runner import process
    e = _entry(one_run_batch, A, ["06_EtOH_ISTD.D", "08_EtOH.D", "13_EtOH.D"])
    out = tmp_path / "learn"
    first = process(e, out, method=METHOD, identify=lib_oracle)
    assert first.state in (PL.ACCEPTED_AUTO, PL.CONTROL), first.reason
    assert first.peaks and first.reported and first.method == METHOD["name"]
    cached = list(out.glob("runs/*/program.json"))
    assert len(cached) == 1

    def no_job(*a, **k):
        raise AssertionError("the job ran again")

    monkeypatch.setattr(PL, "run_job", no_job)
    assert process(e, out, method=METHOD, identify=lib_oracle) == first
    again = process(e, out, method=METHOD, identify=lib_oracle, force=True)
    assert again.state == "failed" and "the job ran again" in again.reason


def test_migration_from_header():
    from gcws.learn.model import Header
    from gcws.learn.runner import migration_from_header
    h = Header(evaluator="BlM", simulant="EtOH 95%", temperature=40, duration="10 d", volume=10, sv_ratio=6,
               cell_area_dm2=0.51, occupancy_factor=1)
    m = migration_from_header(h)
    assert m == {"analyst": "BlM", "simulant": "EtOH 95%", "temperature": "40 °C", "duration": "10 d",
                 "cell_area_dm2": 0.51, "occupancy_factor": 1, "volume_ml": 10, "ov_ratio": 6,
                 "migration_cell": "Zelle groß (0.51 dm²)", "occupancy": "einfach"}
    usb = migration_from_header(Header(temperature_text="USB", cell_area_dm2=None))
    assert usb["temperature"] == "USB" and "cell_area_dm2" not in usb


def test_build_spec_with_migration(tmp_path):
    from gcws.learn.runner import build_spec
    e = _entry(tmp_path / "B", "05_X_A.D", [])
    method = {"name": "M", "sections": {"quant": {"mode": "nias_mgkg"}}}
    s = build_spec(e, method, tmp_path / "job", migration={"simulant": "EtOH 95%"})
    assert s["method"]["sections"]["migration"] == {"simulant": "EtOH 95%"}
    assert "migration" not in method["sections"]

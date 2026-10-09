"""gcws.learn.detection: cached FID traces, re-integration with candidate settings, above-limit detection score."""
import numpy as np
import pytest

FID = {"name": "T", "slope_sensitivity": 8.0, "area_reject": 0.0, "height_reject": 0.0, "min_sn": 3.0,
       "baseline_mode": "drop", "skim_mode": "none", "deconv_split": "auto",
       "timed_events": [{"time": 0.0, "kind": "INTEGRATOR_OFF", "value": None, "enabled": True, "note": ""},
                        {"time": 6.2, "kind": "INTEGRATOR_ON", "value": None, "enabled": True, "note": ""}]}
METHOD = {"name": "T", "sections": {"integration": {"FID": FID}}}


def _item(rt, decision, conc=None, area=None, source="final"):
    from gcws.learn.match import HumanItem
    return HumanItem(rt=rt, area=area, label="", cas="", row_class="", decision=decision, source=source,
                     conc_mgkg=conc)


def _run(items, tmp_path, mgkg_per_area=1e-6):
    from gcws.learn.detection import DetectionRun
    return DetectionRun(key="k", batch="B", run_dir="r", items=items, mgkg_per_area=mgkg_per_area,
                        signal=str(tmp_path / "none.npz"))


def _peak(rt, area):
    return {"rt": rt, "start": rt - 0.01, "end": rt + 0.01, "area": area}


def test_conc_mgkg_on_items(tmp_path):
    from learn_fixtures import make_workbook
    from gcws.learn.match import human_items
    from gcws.learn.workbook import parse_workbook
    items = {i.rt: i for i in human_items(parse_workbook(make_workbook(tmp_path / "w.xlsx")))}
    assert items[7.07].conc_mgkg == 1.458
    assert items[5.132].conc_mgkg is None


def test_detection_score_perfect(tmp_path):
    from gcws.learn.detection import detection_score
    items = [_item(14.0, "istd", 0.1, area=100000), _item(7.0, "reported_named", 0.05, area=50000),
             _item(8.0, "kept_unreported", 0.002, area=2000)]
    run = _run(items, tmp_path)
    peaks = [_peak(14.0, 100000), _peak(7.0, 50000), _peak(8.0, 2000)]
    assert detection_score(run, peaks) == 1


def test_detection_score_counts_misses_and_false(tmp_path):
    from gcws.learn.detection import detection_score
    items = [_item(14.0, "istd", 0.1, area=100000), _item(7.0, "reported_named", 0.05, area=50000),
             _item(9.0, "reported_named", 0.03, area=30000), _item(10.0, "kept_unreported", 0.02, area=20000)]
    peaks = [_peak(14.0, 100000), _peak(7.0, 50000), _peak(9.0, 30000), _peak(11.0, 40000)]
    s = detection_score(_run(items, tmp_path), peaks)
    # targets 7, 9, 10 (found 7, 9); above-limit detections 14 (ISTD peak: an analyst item), 7, 9, 11 (3 right)
    precision, recall = 3 / 4, 2 / 3
    assert s == pytest.approx(2 * precision * recall / (precision + recall))


def test_detection_score_without_istd_is_none(tmp_path):
    from gcws.learn.detection import detection_score
    assert detection_score(_run([_item(7.0, "reported_named", 0.05, area=5)], tmp_path), [_peak(7.0, 5)]) is None


def _trace(path, peaks):
    rt = np.arange(0.0, 20.0, 0.005)
    y = np.random.default_rng(0).normal(0, 20, rt.size) + 1000.0
    for center, height in peaks:
        y += height * np.exp(-0.5 * ((rt - center) / 0.01) ** 2)
    np.savez(path, rt=rt, y=y)
    return str(path)


def test_integrate_peaks_applies_params(tmp_path):
    from gcws.learn.detection import DetectionRun, integrate_peaks
    run = DetectionRun(key="k", batch="B", run_dir="r", items=[], mgkg_per_area=None,
                       signal=_trace(tmp_path / "t.npz", [(5.5, 200000), (10.0, 1000000), (12.0, 20000)]))
    every = integrate_peaks(run, METHOD, {"area_reject": 0, "integrator_on": 5.0})
    rts = [round(p["rt"], 1) for p in every]
    assert 5.5 in rts and 10.0 in rts and 12.0 in rts
    small = next(p for p in every if round(p["rt"], 1) == 12.0)["area"]
    assert 12.0 not in [round(p["rt"], 1) for p in integrate_peaks(run, METHOD, {"area_reject": small * 2})]
    assert 12.0 in [round(p["rt"], 1) for p in integrate_peaks(run, METHOD, {"area_reject": small / 2})]
    assert 5.5 not in [round(p["rt"], 1) for p in integrate_peaks(run, METHOD, {"integrator_on": 6.2})]

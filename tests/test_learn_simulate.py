"""gcws.learn.simulate: a client report built from the cached program evidence and rule parameters."""
import pytest

P = {"min_score": 80.0, "unknowns": "report"}


def _peak(rt, area, name="", cas="", score=None, istd="", in_blank="", blank_ratio=None, hint=""):
    return {"rt": rt, "start": rt - 0.01, "end": rt + 0.01, "area": area, "name": name, "cas": cas, "score": score,
            "istd": istd, "in_blank": in_blank, "blank_ratio": blank_ratio, "class_hint": hint}


def _prog(peaks, reported):
    from gcws.learn.runner import ProgramResult
    return ProgramResult(run_dir="r", method="M", state="control", peaks=peaks, reported=reported)


def _base():
    peaks = [_peak(5.0, 2000, "A", "1-1-1", 95), _peak(6.0, 3000, "B", "2-2-2", 90), _peak(14.0, 50000, "IS", "", 99, istd="IS1"),
             _peak(7.0, 2000, "Blanky", "3-3-3", 95, in_blank="EtOH", blank_ratio=0.5),
             _peak(8.0, 50, "Small", "4-4-4", 95), _peak(9.0, 2000, "Weak", "5-5-5", 60),
             _peak(10.0, 2000, "unknown (m/z 105, 91)", "", None)]
    reported = [{"rt": 5.0, "mean_mgkg": 0.02}, {"rt": 6.0, "mean_mgkg": 0.03}]     # 1e-5 mg/kg per area
    return _prog(peaks, reported)


def test_peak_conc():
    from gcws.learn.simulate import peak_conc
    conc = peak_conc(_base())
    assert conc[0] == pytest.approx(0.02) and conc[4] == pytest.approx(0.0005)
    assert peak_conc(_prog([_peak(5.0, 1000)], [{"rt": 5.0}])) == [None]


def test_simulate_report_rules():
    from gcws.learn.simulate import simulate_report
    lines = simulate_report(_base(), P)
    assert [(l["rt"], l["name"], l["cas"], l["kind"]) for l in lines] == [
        (5.0, "A", "1-1-1", "line"), (6.0, "B", "2-2-2", "line"),
        (9.0, "Weak", "", "line"), (10.0, "unknown (m/z 105, 91)", "", "line")]


def test_unknowns_dropped():
    from gcws.learn.simulate import simulate_report
    assert [l["rt"] for l in simulate_report(_base(), {**P, "unknowns": "drop"})] == [5.0, 6.0]


def test_min_score_zero_names_everything_with_cas():
    from gcws.learn.simulate import simulate_report
    lines = {l["rt"]: l for l in simulate_report(_base(), {**P, "min_score": 0.0})}
    assert lines[9.0]["cas"] == "5-5-5"


def test_no_ratio_no_simulation():
    from gcws.learn.simulate import simulate_report
    assert simulate_report(_prog([_peak(5.0, 1000, "A", "1-1-1", 95)], []), P) is None

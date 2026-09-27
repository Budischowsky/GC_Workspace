"""The NIAS quantification bridge reproduces AutoLib's arithmetic."""
import pytest

from gcws.quant import nias_bridge as NB


def _parsed(d):
    eng = NB.engine()
    tic, fid, pbm = eng.parse_results(str(d / "RESULTS.CSV"))
    lib = eng.parse_library_report(str(d / "LIB")) if (d / "LIB").exists() else {}
    for p in pbm:
        if lib.get(p.peak):
            p.hits = lib[p.peak]
    return eng, tic, fid, pbm


def _determination(d, settings):
    """Our Determination built from the parsed ChemStation lists (test only)."""
    eng, tic, fid, pbm = _parsed(d)
    delay = eng.estimate_delay(tic, fid, settings.solvent_end)
    rows = eng.assign(fid, pbm, delay, settings)
    det = NB.Determination("x", fid, {f.peak: p for f, p, _, _ in rows if p is not None}, delay, {}, {}, {})
    return eng, det


@pytest.mark.parametrize("prefix", ["07_", "11_"])
def test_analyse_equals_autolib(samples, prefix):
    d = next(samples.glob(prefix + "*.D"))
    blank = next(samples.glob("08_*.D"))
    s = NB.make_settings()
    eng, det = _determination(d, s)
    blank_rows, _ = eng.load_blank_reference(str(blank / "RESULTS.CSV"), s) if (blank / "RESULTS.CSV").exists() \
        else ([], None)
    ours = NB.analyse(det, s, blank_rows, [])
    ref = eng.analyse_determination(str(d / "RESULTS.CSV"), str(d / "LIB"), s, blank_rows, [],
                                    require_standards=False)
    assert ours["mean_factor"] == pytest.approx(ref["mean_factor"], rel=1e-12)
    ref_by_peak = {p["fid_peak"]: p for p in ref["peaks"]}
    matched = [p for p in ours["peaks"] if p["pbm_peak"] is not None]
    assert len(matched) == len(ref["peaks"])
    for p in matched:
        r = ref_by_peak[p["fid_peak"]]
        assert p["name"] == r["name"] and p["id_status"] == r["id_status"]
        assert p["area"] == pytest.approx(r["area"])
        assert p["mg_kg"] == pytest.approx(r["mg_kg"], rel=1e-12)
        assert p["blank_area"] == pytest.approx(r["blank_area"])


def test_nias_sample_from_own_integration(run07):
    """End to end on our integration: ISTDs found, mg/kg computed."""
    from gcws.integration.engine import integrate
    from gcws.integration.method import nias_fid_method
    from gcws.core.ident import Identification, IdentificationSet
    from types import SimpleNamespace
    res = integrate(run07.fid, nias_fid_method())
    idents = IdentificationSet()
    for name, rt in (("Perdeutero-Heptadecane", 13.42), ("Benzyl-butyl-phthalate-d4", 18.92),
                     ("Di-n-nonyl-phthalate-d4", 22.47)):
        p = min(res.peaks, key=lambda x: abs(x.apex_rt - rt))
        idents.set(Identification(apex_rt=p.apex_rt, name=name, score=95, hits=[{"name": name, "score": 95}]))
    st = SimpleNamespace(id="r", name="07", run=run07, results={"FID": res}, delay_value=0.005,
                         ident_set=lambda key: idents)
    s = NB.make_settings()
    det = NB.convert(st)
    result = NB.analyse(det, s)
    sample = NB.build_sample(st, det, result, s)
    assert sample.mean_factor and sample.mean_factor > 0
    found = [std for std in sample.standards if std.get("status") == "Found"]
    assert len(found) >= 3
    kg = [r.derived.get("mg_kg") for r in sample.rows if r.derived.get("mg_kg")]
    assert len(kg) > 50


def _peak(rt, name, mg, cas="", status="Accepted", blank=0.0):
    return {"rt": rt, "name": name, "cas": cas, "mg_kg": mg, "id_status": status, "review": "",
            "blank_area": blank, "blank_istd_area": 0.0}


def test_replicates_n2_equals_autolib():
    from gcws.quant.replicates import combine
    eng = NB.engine()
    a = [_peak(10.0, "A", 1.0, "1-1-1"), _peak(12.0, "B", 2.0), _peak(14.0, "unknown", 0.5, status="Unknown")]
    b = [_peak(10.01, "A", 1.2, "1-1-1"), _peak(12.02, "B", 2.2), _peak(16.0, "C", 0.3)]
    ours = combine([a, b], 0.035)
    ref = eng.combine_determinations(a, b, 0.035)
    assert [(r["rt"], r["status"], r["mean"]) for r in ours] == [(r["rt"], r["status"], r["mean"]) for r in ref]
    assert ours[0]["rsd"] is not None and ours[0]["n_detected"] == 2


def test_replicates_n3():
    from gcws.quant.replicates import combine
    a = [_peak(10.0, "A", 1.0, "1-1-1"), _peak(12.0, "B", 2.0)]
    b = [_peak(10.01, "A", 1.2, "1-1-1"), _peak(12.01, "B", 2.2)]
    c = [_peak(10.02, "A", 1.1, "1-1-1"), _peak(15.0, "D", 9.0, blank=5.0)]
    rows = combine([a, b, c], 0.035)
    byname = {r["name"]: r for r in rows}
    assert byname["A"]["status"] == "Valid replicate (3/3)"
    assert byname["A"]["mean"] == pytest.approx(1.1)
    assert byname["A"]["rsd"] == pytest.approx(100 * 0.1 / 1.1)
    assert byname["B"]["status"].startswith("Artefact: only determination 1, 2")
    assert byname["B"]["mean"] is None
    assert byname["D"]["status"] == "Artefact: only determination 3, also in Blank"
    maj = {r["name"]: r for r in combine([a, b, c], 0.035, "majority")}
    assert maj["B"]["status"] == "Valid replicate (2/3)" and maj["B"]["mean"] == pytest.approx(2.1)
    conflict = combine([[_peak(10.0, "A", 1.0, "1-1-1")], [_peak(10.0, "X", 1.0, "2-2-2")],
                        [_peak(10.0, "A", 1.0, "1-1-1")]], 0.035)
    assert len(conflict) == 1 and conflict[0]["status"].startswith("Identification conflict")


def test_quant_detector():
    from gcws.quant.service import quant_detector
    assert quant_detector({}) == "FID"
    assert quant_detector({"mode": "nias_mgkg", "detector": "TIC"}) == "TIC"
    assert quant_detector({"mode": "hs_screening", "detector": "FID"}) == "TIC"

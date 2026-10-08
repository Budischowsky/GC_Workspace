"""The automatic deconvolution split of a whole run (integration stage), its gates, the analyst's
overrides, the double determination (split carried over) and the Report² rule."""
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from gcws.core.model import FID, Run, Signal

pytest.importorskip("pytestqt")

DELAY = 0.006
SPEC_C = {55: 100, 69: 80, 83: 50, 97: 20, 111: 10}


@pytest.fixture
def app(qapp):
    return qapp


def _fid(t, seed, parts):
    from test_deconv import DT, RT
    y = np.full(t.size, 50.0) + np.random.default_rng(seed).normal(0, 2, t.size)
    for scan, height in parts:
        y += height * np.exp(-0.5 * ((t - (RT[0] + scan * DT) - DELAY) / (4 / 2.355 * DT)) ** 2)
    return y


def make_run(name, seed=1, ms_parts=None, fid_parts=None):
    """A run with two co-eluting compounds (MS scans 200 and 203, FID response 1:2) and one alone."""
    from test_deconv import SPEC_A, SPEC_B, build
    ms_parts = ms_parts if ms_parts is not None else [(200.0, SPEC_A, 60000.0), (203.0, SPEC_B, 45000.0),
                                                      (120.0, SPEC_C, 50000.0)]
    fid_parts = fid_parts if fid_parts is not None else [(200, 3000), (203, 6000), (120, 5000)]
    t = np.arange(9.5, 13.5, 1 / 1200)
    from gcws.io.metadata import RunMetadata
    meta = RunMetadata(Path(f"/tmp/{name}.D"), sample_name=name)
    return Run(Path(f"/tmp/{name}.D"), meta, fid=Signal(FID, t, _fid(t, seed, fid_parts)),
               ms=build(ms_parts, background=False, seed=seed))


def workspace(mode="auto", **changes):
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    ws.default_methods[FID] = ws.default_method(FID).copy(deconv_split=mode, timed_events=[], **changes)
    return ws


def load(ws, run):
    from gcws.integration.engine import integrate
    st = ws.add_run(run, {FID: integrate(run.fid, ws.default_method(FID))})
    st.delay_override = DELAY
    ws.integrate(st.id, FID)
    return st


def fragments(ws, st):
    return [p for p in ws.result(st.id, FID).peaks if p.extra.get("deconv_component")]


def test_coeluted_fid_peak_is_split_by_the_fid_fit_and_keeps_its_area(app):
    ws = workspace()
    st = load(ws, make_run("S_A"))
    parts = fragments(ws, st)
    assert len(parts) == 2 and all(p.origin == "deconvoluted" for p in parts)
    parent = next(p for p in st.presplit[FID].peaks if p.start <= parts[0].apex_rt <= p.end)
    assert math.fsum(p.area for p in parts) == parent.area
    # FID response 1:2 (the MS says 60000:45000): the areas follow the FID
    assert parts[0].area / parent.area == pytest.approx(1 / 3, abs=0.03)
    assert all(p.extra["deconv_component"]["basis"] == "fit" for p in parts)
    plan = st.auto_split[FID]
    assert len(plan.events) == 1 and plan.ms_basis == 0
    assert not st.events(FID)                      # nothing is stored as a manual event
    # the fragments keep their identity over re-integrations (identifications bind to it)
    ids = [p.extra["spectrum_id"] for p in parts]
    assert all(i.startswith("auto-") for i in ids)
    ws.integrate(st.id, FID)
    assert [p.extra["spectrum_id"] for p in fragments(ws, st)] == ids


def test_the_split_uses_the_refined_fid_ms_delay(app):
    """Each FID integration refines the FID-MS delay from the FID/TIC apex pairs. The automatic split
    of that integration uses the refined delay, not the estimate it replaces (a run loaded fresh had
    its IS1 peak split with an estimate 0.3 scans off), and the split's fragments do not feed back
    into the refinement."""
    from gcws.core.model import TIC
    from gcws.integration.engine import integrate
    from gcws.signal.delay import DelayEstimate
    from test_deconv import SPEC_A, SPEC_B
    run = make_run("S_D", ms_parts=[(200.0, SPEC_A, 60000.0), (203.0, SPEC_B, 45000.0), (60.0, SPEC_A, 40000.0),
                                    (120.0, SPEC_C, 50000.0), (300.0, SPEC_B, 40000.0)],
                   fid_parts=[(200, 3000), (203, 6000), (60, 8000), (120, 8000), (300, 8000)])
    ws = workspace()
    results = {FID: integrate(run.fid, ws.default_method(FID)), TIC: integrate(run.signal(TIC), ws.default_method(TIC))}
    st = ws.add_run(run, results, delay=DelayEstimate(DELAY - 0.002, 0.9, "cross-correlation"))
    assert st.delay.method == "apex pairs" and st.delay_value == pytest.approx(DELAY, abs=0.0005)
    parts = fragments(ws, st)
    assert len(parts) == 2
    assert all(p.extra["deconv_component"]["delay"] == st.delay_value for p in parts)
    refined = st.delay_value
    ws.integrate(st.id, FID)
    assert st.delay_value == refined


def test_fragments_are_searched_with_their_component_spectrum(app):
    from gcws.identify.service import fragment_items
    from test_deconv import SPEC_A, SPEC_B
    ws = workspace()
    st = load(ws, make_run("S_A"))
    items, _protected = fragment_items(ws, [st.id], FID)
    assert len(items) == 2 and all(it.spectrum_mode == "deconvoluted" for it in items)
    assert [it.peak_id for it in items] == [p.extra["spectrum_id"] for p in fragments(ws, st)]
    for it, spec in zip(items, (SPEC_A, SPEC_B)):
        base = max(it.job.spectrum, key=lambda x: x[1])[0]
        assert round(base) == max(spec, key=spec.get)        # each fragment: its own compound's spectrum


def test_switched_off_nothing_is_split(app):
    ws = workspace("off")
    st = load(ws, make_run("S_A"))
    assert not fragments(ws, st) and FID not in st.auto_split


def test_keep_unsplit_marker_and_timed_events_leave_the_peak_alone(app):
    from gcws.integration import auto_deconv as AD
    from gcws.integration.method import EventKind, TimedEvent
    ws = workspace()
    st = load(ws, make_run("S_A"))
    parent = st.presplit[FID].peaks[-1]
    st.events(FID).append(AD.keep_marker(parent))
    ws.integrate(st.id, FID)
    assert not fragments(ws, st) and not ws.result(st.id, FID).unresolved
    assert st.auto_split[FID].skipped[0][1] == "kept unsplit by the analyst"
    assert "Keep unsplit" in st.events(FID)[0].describe()
    st.events(FID).clear()
    st.methods[FID] = st.methods[FID].copy(timed_events=[TimedEvent(11.0, EventKind.DECONV_SPLIT_OFF),
                                                         TimedEvent(12.0, EventKind.DECONV_SPLIT_ON)])
    ws.integrate(st.id, FID)
    assert not fragments(ws, st)
    st.methods[FID] = st.methods[FID].copy(timed_events=[])
    ws.integrate(st.id, FID)
    assert len(fragments(ws, st)) == 2


def _plan(comps, parts=((10.0, 400.), (10.022, 700.)), **method):
    """plan_peaks on the synthetic trace of test_component_fit (one integrated parent peak)."""
    from test_component_fit import trace
    from gcws.core.model import Baseline, Peak
    from gcws.integration.auto_deconv import plan_peaks
    from gcws.integration.method import IntegrationMethod
    t, y = trace(list(parts), 0.0066, 0.9, noise=1.0)
    lo, hi = 9.95, 10.08
    use = (t >= lo) & (t <= hi)
    area = float(np.trapezoid(y[use], t[use] * 60))
    peak = Peak(start=lo, end=hi, apex_rt=10.03, baseline=Baseline("hold", lo, 100.0, hi, 100.0),
                area=area, area_raw=area)
    result = SimpleNamespace(peaks=[peak])
    m = IntegrationMethod(deconv_split="auto", **method)
    return plan_peaks(Signal(FID, t, y + 100.0), result, FID, 0.0066, comps, m)


def test_bleed_components_and_identical_spectra_are_not_split_off():
    from test_component_fit import component
    assert len(_plan([component(10.0, mz=57), component(10.022, mz=91)]).events) == 1
    # a column bleed component (model ion 207) is never a fragment of its own
    bleed = _plan([component(10.0, mz=57), component(10.022, mz=207)])
    assert not bleed.events
    assert len(_plan([component(10.0, mz=57), component(10.022, mz=207)], deconv_exclude_mz=[]).events) == 1
    # the same spectrum within two scans: one compound
    a, b = component(10.0, mz=57), component(10.012, mz=57)
    assert not _plan([a, b], parts=((10.0, 400.), (10.012, 700.))).events


def test_an_isotopologue_never_takes_the_standards_peak():
    """IS1 of run 09: a weak isotopologue 1.2 scans after the standard (alike profile) and a bleed
    component on the tail. Whatever the FID-MS delay, the standard keeps the FID peak (before, at some
    delays the isotopologue took over 90 % and the standard's fragment carried its spectrum)."""
    from test_component_fit import component, standard_and_isotopologue, trace
    from gcws.core.model import Baseline, Peak
    from gcws.integration.auto_deconv import plan_peaks
    from gcws.integration.method import IntegrationMethod
    main, minor = standard_and_isotopologue()
    bleed = component(10.0426, sigma=0.0064, mz=221)
    bleed.profile_y = bleed.profile_y * 1.2
    t, y = trace([(10.0, 400.), (10.0426, 12.)], 0.0068, 0.93, sigma=0.0095, noise=1.0)
    lo, hi = 9.975, 10.11
    use = (t >= lo) & (t <= hi)
    area = float(np.trapezoid(y[use], t[use] * 60))
    peak = Peak(start=lo, end=hi, apex_rt=10.0068, baseline=Baseline("hold", lo, 100.0, hi, 100.0),
                area=area, area_raw=area)
    m = IntegrationMethod(deconv_split="auto", deconv_level=5, deconv_min_r=0.5, deconv_fit_r2=0.95,
                          deconv_min_sn=1.5, deconv_min_share=0.003)
    for delay in np.arange(0.0040, 0.0100, 0.0004):
        plan = plan_peaks(Signal(FID, t, y + 100.0), SimpleNamespace(peaks=[peak]), FID, float(delay),
                          [main, minor, bleed], m)
        (split,) = plan.plans
        shares = {split.candidates[i].component.model_mz: s for i, s in zip(split.checked, split.shares)}
        assert shares[66] > 0.9 and shares.get(275, 0.0) < 0.05, (delay, shares)


def test_is1_of_run_09_keeps_its_own_component(samples):
    """The reported case: IS1 (heptadecane-d36, 13.42 min) of run 09 at level 5. Whatever the FID-MS
    delay (0.00452 min was the estimate before its refinement), the standard's component (model m/z 66)
    keeps the peak; it is not split into the D35H isotopologue (m/z 275) and a bleed component (221)."""
    from conftest import run_dir
    from gcws.core.model import Baseline, Peak
    from gcws.integration.auto_deconv import plan_peaks
    from gcws.integration.method import IntegrationMethod
    from gcws.io.run_loader import load_run
    from gcws.ms import deconv as D
    run = load_run(run_dir("09_"))
    comps = D.deconvolute_range(run.ms, 13.2, 13.7, D.settings_for_level(D.DeconvSettings(), 5))
    fid = run.fid
    lo, hi = 13.38, 13.5192
    y0, y1 = (float(np.interp(x, fid.rt, fid.y)) for x in (lo, hi))
    peak = Peak(start=lo, end=hi, apex_rt=13.4167, baseline=Baseline("line", lo, y0, hi, y1),
                area=9.67e6, area_raw=9.67e6)
    m = IntegrationMethod(deconv_split="auto", deconv_level=5, deconv_min_r=0.5, deconv_fit_r2=0.95,
                          deconv_min_sn=1.5, deconv_min_share=0.003, deconv_probe=False)
    assert sorted(c.model_mz for c in comps if 13.37 <= c.rt <= 13.51) == [66, 221, 275]
    for delay in np.arange(0.0040, 0.0100, 0.0004).tolist() + [0.00452]:
        plan = plan_peaks(fid, SimpleNamespace(peaks=[peak]), FID, delay, comps, m)
        assert not plan.events, (delay, plan.events[0].comment)


def test_poor_fit_splits_by_ms_proportions_and_is_flagged():
    from test_component_fit import component
    plan = _plan([component(10.0, area=300, mz=57), component(10.022, area=700, mz=91)], deconv_fit_r2=1.0,
                 deconv_min_r=0.0)
    assert len(plan.events) == 1 and plan.ms_basis == 1
    assert "MS component proportions" in plan.events[0].comment


def test_method_keeps_the_settings():
    from gcws.integration.method import IntegrationMethod
    m = IntegrationMethod(deconv_split="auto", deconv_level=5,
                          deconv_min_share=0.05, deconv_exclude_mz=[73, 207])
    back = IntegrationMethod.from_dict(m.to_dict())
    assert (back.deconv_split == "auto" and back.deconv_level == 5 and
            back.deconv_min_share == 0.05 and back.deconv_exclude_mz == [73, 207])
    assert IntegrationMethod().deconv_split == "off"
    back = IntegrationMethod.from_dict(IntegrationMethod(deconv_probe=False, deconv_probe_r2=0.9).to_dict())
    assert back.deconv_probe is False and back.deconv_probe_r2 == 0.9
    assert IntegrationMethod().deconv_probe is True


def test_detection_slider_sets_five_levels_and_keeps_advanced_closed(app, qtbot):
    from gcws.ui.docks.events import EventsDock
    ws = workspace()
    dock = EventsDock(ws)
    qtbot.addWidget(dock)
    assert dock.deconv_advanced.isHidden()
    dock.deconv_level.setValue(5)
    assert dock.deconv_level_label.text() == "5 / 5"
    assert dock.collect().deconv_level == 5
    assert dock.deconv_sn.value() == 1.5
    dock.deconv_advanced_button.setChecked(True)
    assert not dock.deconv_advanced.isHidden()


# -- closer look: a shoulder the whole-run deconvolution does not resolve --------------------------

MAIN = {45: 100, 59: 90, 72: 60, 85: 30, 103: 20, 117: 10}
SHOULDER = {45: 80, 59: 100, 72: 50, 89: 40, 131: 15}       # shares most ions, two of its own


def shoulder_run(name, sep=4, ms_height=20000.0, fid_height=2000):
    return make_run(name, ms_parts=[(200.0, MAIN, 80000.0), (200.0 + sep, SHOULDER, ms_height)],
                    fid_parts=[(200, 6000), (200 + sep, fid_height)])


@pytest.mark.parametrize("sep, fid_height", [(4, 2000), (5, 1500)])
def test_shoulder_on_the_tail_gets_a_closer_look_and_is_split(app, sep, fid_height):
    from gcws.integration import auto_deconv as AD
    from gcws.ms.peak_split import candidates_in
    ws = workspace(deconv_probe=False)
    st = load(ws, shoulder_run("SH0", sep, fid_height=fid_height))
    (peak,) = ws.result(st.id, FID).peaks
    # the whole-run deconvolution sees one component: without the closer look nothing is split
    assert len(candidates_in(AD.components_for(ws, st), peak, FID, DELAY)) == 1
    assert not fragments(ws, st)

    ws = workspace()
    st = load(ws, shoulder_run("SH1", sep, fid_height=fid_height))
    parts = fragments(ws, st)
    assert len(parts) == 2
    parent = st.presplit[FID].peaks[0]
    assert math.fsum(p.area for p in parts) == parent.area
    assert parts[1].area / parent.area == pytest.approx(fid_height / (6000 + fid_height), abs=0.04)
    assert parts[1].extra["deconv_component"]["model_mz"] in (89, 131)
    assert AD.CLOSER in st.auto_split[FID].events[0].comment


def _probe_plan(parts, comps, probe, **method):
    from test_component_fit import trace
    from gcws.core.model import Baseline, Peak
    from gcws.integration.auto_deconv import plan_peaks
    from gcws.integration.method import IntegrationMethod
    t, y = trace(list(parts), 0.0066, 0.9, noise=1.0)
    lo, hi = 9.95, 10.08
    use = (t >= lo) & (t <= hi)
    area = float(np.trapezoid(y[use], t[use] * 60))
    peak = Peak(start=lo, end=hi, apex_rt=10.03, baseline=Baseline("hold", lo, 100.0, hi, 100.0),
                area=area, area_raw=area)
    m = IntegrationMethod(deconv_split="auto", **method)
    return plan_peaks(Signal(FID, t, y + 100.0), SimpleNamespace(peaks=[peak]), FID, 0.0066, comps, m, probe=probe)


def test_closer_look_only_where_the_trace_asks_for_it():
    from test_component_fit import component
    calls = []

    def probe(peak):
        calls.append(peak)
        return [component(10.0, mz=57)]
    # one clean peak that one component explains: no closer look
    plan = _probe_plan([(10.0, 400.)], [component(10.0, mz=57)], probe)
    assert not calls and not plan.events and not plan.skipped
    # a shoulder: a closer look; it finds one component only, which the panel's tooltip says
    plan = _probe_plan([(10.0, 600.), (10.035, 200.)], [component(10.0, mz=57)], probe)
    assert len(calls) == 1 and not plan.events
    assert "shoulder" in plan.skipped[0][1] and "found 1 component" in plan.skipped[0][1]
    # switched off: no closer look
    calls.clear()
    _probe_plan([(10.0, 600.), (10.035, 200.)], [component(10.0, mz=57)], probe, deconv_probe=False)
    assert not calls


def test_trace_shoulders():
    from gcws.ms.deconv_probe import trace_shoulders
    t = np.arange(0, 1, 1 / 1200)
    sd, dt = 0.0127, 0.0075

    def gauss(c, h):
        return h * np.exp(-0.5 * ((t - c) / sd) ** 2)
    tail = np.convolve(gauss(0.5, 1.0), np.exp(-t / 0.02))[:t.size]
    for seed in range(3):
        noise = np.random.default_rng(seed).normal(0, 2, t.size)
        assert trace_shoulders(t, gauss(0.5, 6000) + noise) == []
        assert trace_shoulders(t, 6000 * tail / tail.max() + noise) == []
        (found,) = trace_shoulders(t, gauss(0.5, 6000) + gauss(0.5 + 4 * dt, 2000) + noise)
        assert 0.5 + 2 * dt < found < 0.5 + 6 * dt


def test_probe_finds_the_shoulder_and_nothing_on_a_single_peak():
    from test_deconv import RT, build
    from gcws.ms import deconv as D
    from gcws.ms.deconv_probe import probe
    ms = build([(200.0, MAIN, 80000.0), (204.0, SHOULDER, 15000.0)], background=False, seed=3)
    assert len([c for c in D.deconvolute_range(ms, RT[150], RT[250]) if RT[194] <= c.rt <= RT[212]]) == 1
    comps = probe(ms, RT[194], RT[212], RT[200])
    assert [c.model_mz in (89, 131) for c in comps] == [False, True]
    assert comps[1].rt == pytest.approx(RT[204], abs=0.5 * (RT[1] - RT[0]))
    for seed in range(5):
        single = build([(200.0, MAIN, 80000.0)], seed=seed)
        assert len(probe(single, RT[194], RT[212], RT[200])) == 1


def test_split_in_one_determination_is_carried_over_to_the_other(app):
    from gcws.features import service as SV
    ws = workspace()
    a, b = load(ws, make_run("S_A", 1)), load(ws, make_run("S_B", 2))
    b.methods[FID] = b.methods[FID].copy(deconv_split="off")      # B's MS "does not resolve" them
    ws.integrate(b.id, FID)
    assert len(fragments(ws, a)) == 2 and not fragments(ws, b)
    ws.replicate_groups = [{"id": "g", "name": "S", "members": [a.id, b.id], "policy": "all"}]
    before = SV.build(ws, [a.id, b.id])
    assert any(f.split and any(p.kind == "split" for p in f.proposals) for f in before.features)
    total = next(p.area for p in ws.result(b.id, FID).peaks if 11.4 < p.apex_rt < 11.6)
    table = SV.run(ws, [a.id, b.id])
    assert table.applied["split"] == 1
    parts = fragments(ws, b)
    assert len(parts) == 2 and all(p.extra["spectrum_id"].startswith("sync-") for p in parts)
    assert math.fsum(p.area for p in parts) == pytest.approx(total)
    assert not any(f.split for f in table.features)
    from gcws.features.split_sync import is_sync
    assert any(is_sync(m.peak.fragment) for f in table.features for m in f.found)


def test_harmonise_leaves_fragments_alone():
    from gcws.features import harmonise as HM
    from gcws.features.model import DETECTED, Feature, Member, PeakInfo, Settings
    p = lambda run, rt, start, end: PeakInfo(0, rt, start, end, 100.0, 10.0, 0.01, origin="deconvoluted")
    f = Feature([Member("a", "A", p("a", 10.0, 9.95, 10.05), DETECTED, 10.0),
                 Member("b", "B", p("b", 10.0, 9.90, 10.10), DETECTED, 10.0)], 10.0)
    assert HM.propose(f, SimpleNamespace(input=lambda _r: None), Settings(), runs={}) == []


def test_report2_rule_lists_ms_proportion_splits_and_split_istds():
    from gcws.automation import rules as RU
    ev = {"settings": {"reporting_limit": 0.01}, "members": [{"name": "S_A", "deconvolution": {
        "fragments": 4, "splits": [
            {"rt": 11.5, "basis": "ms", "note": "fit R² 0.9 < 0.97", "names": "x / y", "max_conc": 0.05},
            {"rt": 12.0, "basis": "ms", "note": "", "names": "a / b", "max_conc": 0.001},
            {"rt": 13.0, "basis": "fit", "istd": "IS1", "istd_share": 0.9}]}}]}
    rule = next(r for r in RU.default_rules() if r.id == "deconvolution")
    assert rule.enabled and rule.level == "info"
    found = RU.CHECKS["deconvolution"](rule, ev)
    texts = [f.text for f in found]
    assert sum("MS component proportions" in t for t in texts) == 1        # the one below the limit is left out
    assert any("IS1" in t and "90 %" in t for t in texts)
    assert any("3 peak(s) split automatically into 4 fragments (2 by MS proportions)" in t for t in texts)
    assert RU.evaluate([rule], ev).status == RU.ACCEPTED_AUTO              # note only by default
    assert next(r for r in RU.from_list([]) if r.id == "deconvolution").level == "info"


@pytest.fixture
def win(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QMessageBox
    QCoreApplication.setOrganizationName("GCWorkspaceTest")
    QCoreApplication.setApplicationName("pytest")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    QSettings().clear()
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    from gcws.ui.main_window import MainWindow
    w = MainWindow()
    qtbot.addWidget(w)
    yield w
    w.ws.dirty = False
    w.close()


def test_background_deconvolution_ignores_reloaded_run(qapp, monkeypatch):
    from gcws.integration import auto_deconv as AD
    from gcws.ms import deconv_cache as DC
    from gcws.ui import workers
    from gcws.ui.workspace import Workspace

    ws = Workspace()
    original = SimpleNamespace(id="same", name="old", results={})
    replacement = SimpleNamespace(id="same", name="new", results={})
    ws.runs["same"] = original
    settings = SimpleNamespace(to_dict=lambda: {"window": 1})
    monkeypatch.setattr(AD, "settings_for_method", lambda *_: settings)
    monkeypatch.setattr(ws, "solvent_cut", lambda *_: None)
    callbacks = {}
    monkeypatch.setattr(workers, "submit", lambda *args, **kwargs: callbacks.update(kwargs))
    stored = []
    monkeypatch.setattr(DC, "store_whole_run", lambda st, *_: stored.append(st))

    ws._deconvolute_in_background(original, object())
    ws.runs["same"] = replacement
    callbacks["on_done"]([])
    assert stored == []


def test_panel_switches_the_split_and_the_analyst_keeps_a_peak_unsplit(win):
    ws = win.ws
    ws.deconv_background = False
    st = load(ws, make_run("S_A"))
    ws.set_active(st.id)
    events = win.events
    events.load()
    assert events.deconv_mode.currentText() == "Off"
    events.deconv_mode.setCurrentText("Automatic")
    events.deconv_exclude.setText("73, 207 281")
    m = events.collect()
    assert m.deconv_split == "auto" and m.deconv_exclude_mz == [73, 207, 281]
    events._apply(False)
    parts = fragments(ws, st)
    assert len(parts) == 2 and "split into 2 fragments" in events.deconv_status.text()
    ws.select_peak(ws.result(st.id, FID).peaks.index(parts[1]))
    win.keep_unsplit(True)
    assert not fragments(ws, st) and len(st.events(FID)) == 1
    whole = next(i for i, p in enumerate(ws.result(st.id, FID).peaks) if p.start < 11.51 < p.end)
    ws.select_peak(whole)
    win.keep_unsplit(False)
    assert len(fragments(ws, st)) == 2 and not st.events(FID)
    st.undo.undo()
    assert not fragments(ws, st)


def test_merge_tool_merges_split_peaks_back(win):
    """Merge peaks on deconvoluted peaks: a drag across them, a click on one or the table's action undoes
    the split (an automatic split gets the Keep unsplit mark); Ctrl+Z brings it back."""
    from PySide6.QtCore import Qt
    ws = win.ws
    ws.deconv_background = False
    st = load(ws, make_run("S_A"))
    ws.set_active(st.id)
    win.events.load()
    win.events.deconv_mode.setCurrentText("Automatic")
    win.events._apply(False)
    parts = fragments(ws, st)
    assert len(parts) == 2
    a, b = parts[0].apex_rt, parts[1].apex_rt
    win.tools.set_tool("merge")
    try:
        win.tools.drag_finished(win.chrom.vb, a - .001, 0, b + .001, 0, Qt.NoModifier, (1.0, 0.0), key=FID)
        assert not fragments(ws, st) and len(st.events(FID)) == 1
        st.undo.undo()
        assert len(fragments(ws, st)) == 2 and not st.events(FID)
        win.tools.click(win.chrom.vb, b, 0, Qt.NoModifier, (1.0, 0.0), FID)
        assert not fragments(ws, st)
        st.undo.undo()
        # a drag over plain peaks still merges them as before
        whole = [p for p in ws.result(st.id, FID).peaks if not p.extra.get("deconv_component")]
        assert whole and win.tools.drag_finished(win.chrom.vb, whole[0].start, 0, whole[0].end, 0,
                                                 Qt.NoModifier, (1.0, 0.0), key=FID)
        assert st.events(FID)[-1].kind.name == "MERGE"
        st.undo.undo()
    finally:
        win.tools.set_tool("select")
    ws.select_peak(ws.result(st.id, FID).peaks.index(fragments(ws, st)[0]))
    win.merge_deconvoluted()
    assert not fragments(ws, st)
    st.undo.undo()
    assert len(fragments(ws, st)) == 2


def test_report2_rule_with_an_empty_reporting_limit():
    """A method without a reporting limit (None in the evidence) takes the default 0.01, as the other
    rules do, instead of failing on the comparison."""
    from gcws.automation import rules as RU
    ev = {"settings": {"reporting_limit": None}, "members": [{"name": "S_A", "deconvolution": {
        "fragments": 2, "splits": [{"rt": 11.5, "basis": "ms", "note": "", "names": "x / y", "max_conc": 0.001}]}}]}
    rule = next(r for r in RU.default_rules() if r.id == "deconvolution")
    found = RU.CHECKS["deconvolution"](rule, ev)
    assert not any("MS component proportions" in f.text for f in found)       # below the default limit


def test_spectra_of_unsplit_peaks_use_the_whole_run_at_the_method_level(app, monkeypatch):
    """With the automatic split at another detection level than 3, the whole-run components are kept
    under that level's settings: the deconvoluted spectrum of a peak and the hidden-component markers
    take them from there (no second window deconvolution at level 3, which found other components)."""
    from gcws.identify.service import build_items
    from gcws.ms import deconv_cache as DC
    ws = workspace(deconv_level=5)
    st = load(ws, make_run("S_A"))
    settings = DC.settings_for(ws, st, FID)
    assert DC.whole_run(st, settings) is not None and settings != DC.settings_of(ws)

    def no_window(*_a, **_k):
        raise AssertionError("window deconvolution although the whole run is there")
    monkeypatch.setattr(DC, "window", no_window)
    single = [p for p in ws.result(st.id, FID).peaks if not p.extra.get("deconv_component") and p.area > 0]
    assert single and DC.for_peak(st, single[-1], FID, settings) is not None
    items, _protected = build_items(ws, [st.id], FID, "deconvoluted")
    assert items


def test_reset_range_discards_the_keep_unsplit_marker(app):
    """*Reset range* discards the analyst's manual events in its range, the *Keep unsplit* marker too:
    the automatic split applies again (the marker kept blocking it although it was reset)."""
    from gcws.core.events import ManualEvent, ManualKind as K
    from gcws.integration import auto_deconv as AD
    ws = workspace()
    st = load(ws, make_run("S_A"))
    parent = st.presplit[FID].peaks[-1]
    st.events(FID).append(AD.keep_marker(parent))
    ws.integrate(st.id, FID)
    assert not fragments(ws, st)
    st.events(FID).append(ManualEvent(K.RESET_RANGE, parent.start - .01, parent.end + .01))
    ws.integrate(st.id, FID)
    assert len(fragments(ws, st)) == 2


def test_keep_unsplit_again_after_a_reset_range(win):
    """A *Keep unsplit* marker discarded by *Reset range* does not count as a mark: the analyst can keep
    the peak unsplit again (it said "already kept unsplit" and did nothing)."""
    from gcws.core.events import ManualEvent, ManualKind as K
    from gcws.integration import auto_deconv as AD
    ws = win.ws
    ws.deconv_background = False
    st = load(ws, make_run("S_A"))
    ws.set_active(st.id)
    win.events.load()
    win.events.deconv_mode.setCurrentText("Automatic")
    win.events._apply(False)
    parent = st.presplit[FID].peaks[-1]
    st.events(FID).extend([AD.keep_marker(parent), ManualEvent(K.RESET_RANGE, parent.start - .01, parent.end + .01)])
    ws.integrate(st.id, FID)
    parts = fragments(ws, st)
    assert len(parts) == 2
    ws.select_peak(ws.result(st.id, FID).peaks.index(parts[0]))
    win.keep_unsplit(True)
    assert not fragments(ws, st)
    st.undo.undo()
    assert len(fragments(ws, st)) == 2

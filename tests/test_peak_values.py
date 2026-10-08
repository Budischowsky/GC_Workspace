"""The Peaks panel's values for any run (gcws.quant.peak_values): the same as the table shows for the active run."""
from types import SimpleNamespace

from gcws.quant import peak_values as PV
from test_report import _ws_with, qapp  # noqa: F401  (fixture)


def test_values_of_a_run_that_is_not_active_equal_the_table_of_the_active_one(samples, qapp):
    from gcws.ui.models.peak_table import COLUMNS
    ws = _ws_with(samples, ["06_", "07_", "08_", "11_"], qapp)
    key = "FID"
    ws.signal_key = key
    ids = [s.id for s in ws.states() if s.role == "sample"]
    first, other = ids[0], ids[-1]
    ws.set_active(first)
    rows = PV.rows_for(ws, other, key)
    assert rows and rows[0].quant
    expected = {}
    ws.set_active(other)
    for r in rows[:15]:
        expected[r.index] = {c.key: c.get(r, ws) for c in COLUMNS if c.key != "class_hint"}
    ws.set_active(first)                             # the other run is no longer the active one
    for r in rows[:15]:
        got = {k: PV.VALUES[k](r, ws, other, key) for k in expected[r.index]}
        assert got == expected[r.index]
    assert any(PV.VALUES["ms_rt"](r, ws, other, key) is not None for r in rows)
    assert PV.VALUES["ms_rt"](rows[0], ws, other, "TIC") is None          # FID tables only


def test_class_hint_uses_and_fills_the_workspace_cache():
    calls = []

    class Cache:
        def __init__(self):
            self.d = {}

        def known(self, st, key, peak):
            return self.d.get(peak)

        def store(self, st, key, peak, hint):
            calls.append(peak)
            self.d[peak] = hint

    st = SimpleNamespace(run=SimpleNamespace(ms=None))
    ws = SimpleNamespace(runs={"r": st}, hints=Cache())
    assert PV.class_hint(ws, "r", "FID", "p") == ("", "")            # no MS data: nothing to compute
    st.run.ms = object()
    ws.hints.d["p"] = ("alkane (high)", "details")
    assert PV.class_hint(ws, "r", "FID", "p") == ("alkane (high)", "details") and not calls
    assert PV.class_hint(ws, "missing", "FID", "p") == ("", "")

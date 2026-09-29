"""P55: blanks come from the sample's own batch folder; the blank suggestion does not depend on
the order the runs finished loading; a sample without a blank of its batch is recognised."""
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _run(folder: Path, name: str):
    from gcws.core.model import Run, Signal
    from gcws.io.sequence import classify_role
    rt = np.linspace(0, 30, 3001)
    run = Run(folder / f"{name}.D", None, Signal("FID", rt, np.zeros_like(rt)))
    run.role = classify_role(name)
    return run


def _ws(runs):
    from gcws.ui.workspace import Workspace
    ws = Workspace()
    for r in runs:
        ws.add_run(r)
    return ws


def test_blanks_only_from_the_same_folder(qapp, tmp_path):
    a, b = tmp_path / "26016605_A", tmp_path / "26016777_B"
    s_a = _run(a, "07_26016606_x_A")
    s_b = _run(b, "02_26016778_y_A")
    ws = _ws([_run(a, "06_EtOH_ISTD"), s_a, _run(a, "08_EtOH"), s_b, _run(b, "03_EtOH")])
    names = lambda ids: [ws.runs[i].name for i in ids]
    assert names(ws.runs[s_a.id].blanks) == ["08_EtOH"] and names(ws.runs[s_a.id].blanks_istd) == ["06_EtOH_ISTD"]
    assert names(ws.runs[s_b.id].blanks) == ["03_EtOH"] and ws.runs[s_b.id].blanks_istd == []   # not A's ISTD blank
    chk = ws.blank_readiness(ws.runs[s_b.id], "both")
    assert not chk.ok and chk.missing == ["blank_istd"] and "Blank+ISTD" in chk.text
    assert ws.blank_readiness(ws.runs[s_b.id], "either").ok
    assert ws.blank_readiness(ws.runs[s_a.id], "both").ok
    assert ws.blank_readiness(ws.runs[s_b.id], "none").ok
    # injection order: batches in load order, each in its own order
    assert names(ws.ordered_ids_by_injection()) == ["06_EtOH_ISTD", "07_26016606_x_A", "08_EtOH",
                                                     "02_26016778_y_A", "03_EtOH"]
    # a blank of another folder chosen by the analyst is kept, but flagged
    ws.runs[s_b.id].blanks = [ws.states()[2].id]
    ws.runs[s_b.id].blanks_manual = True
    chk = ws.blank_readiness(ws.runs[s_b.id], "blank")
    assert not chk.ok and chk.foreign == ["08_EtOH"] and "another folder" in chk.text


def test_sample_without_blank_in_its_batch(qapp, tmp_path):
    ws = _ws([_run(tmp_path / "C", "07_26016606_x_A"), _run(tmp_path / "D", "08_EtOH")])
    st = ws.states()[0]
    assert st.blanks == [] and st.blanks_istd == []
    chk = ws.blank_readiness(st)                              # auto: what the subtraction uses
    assert not chk.ok and "from the same batch" in chk.text


def test_resuggest_after_loading_in_any_order(qapp, tmp_path):
    folder = tmp_path / "26016605_GIOSUN1635"
    s07, s11 = _run(folder, "07_26016606_x_A"), _run(folder, "11_26016606_x_B")
    # 13 finishes loading before 08: the first suggestion for 07 is the far blank
    ws = _ws([s07, _run(folder, "13_EtOH"), s11, _run(folder, "08_EtOH")])
    names = lambda ids: [ws.runs[i].name for i in ids]
    assert names(ws.runs[s07.id].blanks) == ["13_EtOH"]
    changed = ws.resuggest_blanks()
    assert changed == [s07.id]
    assert names(ws.runs[s07.id].blanks) == ["08_EtOH"] and names(ws.runs[s11.id].blanks) == ["13_EtOH"]
    ws.runs[s11.id].blanks_manual = True                    # the analyst's choice is never changed
    ws.runs[s11.id].blanks = [ws.runs[s07.id].blanks[0]]
    assert ws.resuggest_blanks() == []


def test_report_warns_about_missing_blank(qapp, tmp_path, monkeypatch):
    from gcws.report import assemble as AS
    ws = _ws([_run(tmp_path / "C", "07_26016606_x_A")])
    st = ws.states()[0]
    assert AS.blank_warnings(ws, [st.id]) == ["07_26016606_x_A: no Blank from the same batch"]


# -- sequence logs per batch folder -------------------------------------------------------------

BATCH = ["06_EtOH_ISTD", "07_26016606_130m_min_GIOSUN1635_A", "08_EtOH", "09_26016607_170m_min_GIOSUN1635_A",
         "10_EtOH", "11_26016606_130m_min_GIOSUN1635_B", "12_26016607_170m_min_GIOSUN1635_B", "13_EtOH"]
TSV_HEAD = "_seqline\t_dataname$\t_datapath$\tSdatafile$\t_runtype$\n"


def write_log(folder: Path, names, other=(), completed=False):
    """A sequence log like the instrument's; ``other`` runs write into another folder."""
    rows = [f"{i}\t{n}\tC:\\Messdaten\\2026\\August\\26017729_other\\\t{n}.D\tBlank\n" for i, n in enumerate(other, 1)]
    rows += [f"{i}\t{n}\tC:\\Messdaten\\2026\\August\\{folder.name}\\\t{n}.D\tBlank\n"
             for i, n in enumerate(names, len(other) + 1)]
    (folder / "2026 Aug 27 1345 Sequence Log .TSV").write_text("Starting sequence Thu Aug 27 13:45:37 2026\n"
                                                               + TSV_HEAD + "".join(rows), encoding="utf-8")
    (folder / "2026 Aug 27 1345 Sequence Log .LOG").write_text(
        "Starting sequence\n" + ("    Sequence completed Fri Aug 28 01:35:16 2026\n" if completed else ""),
        encoding="utf-8")


def test_sequence_log_per_folder(tmp_path):
    from gcws.io import sequence as SQ
    b = tmp_path / "26016605_GIOSUN1635"
    b.mkdir()
    write_log(b, BATCH, other=["02_EtOH_ISTD2", "03_EtOH2"])
    info = SQ.read_sequence(b)
    assert info.stems == [n.casefold() for n in BATCH] and not info.finished
    assert SQ.parse_sequence_log(b)[0] == "06_etoh_istd"          # the other folder's lines are not ours
    write_log(b, BATCH, other=["02_EtOH_ISTD2"], completed=True)
    assert SQ.read_sequence(b).completed
    # the other folder of the sequence finds its lines in the neighbour's log
    other = tmp_path / "26017729_other"
    other.mkdir()
    assert SQ.read_sequence(other).stems == ["02_etoh_istd2"]
    assert SQ.read_sequence(other, siblings=False).stems == []
    # a copied (renamed) folder takes the data path whose runs it holds
    copy = tmp_path / "copy_of_batch"
    copy.mkdir()
    (copy / "x Sequence Log .TSV").write_text((b / "2026 Aug 27 1345 Sequence Log .TSV").read_text())
    assert SQ.read_sequence(copy, present=[BATCH[1] + ".D"]).stems == [n.casefold() for n in BATCH]
    assert SQ.read_sequence(copy).stems == []                         # nothing to match it with


def test_real_sequence_log(samples):
    from gcws.io import sequence as SQ
    info = SQ.read_sequence(samples)
    assert info.completed and info.stems[0] == "06_etoh_istd" and info.stems[-1] == "13_etoh"
    assert len(info.stems) == 8                                      # 02..05 wrote into another folder

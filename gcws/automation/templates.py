"""Ready-made workflows to start from (the chart editor's *New* menu)."""
from __future__ import annotations

from gcws.automation.workflow import Workflow

TEMPLATES = {
    "nias": "NIAS: Excel to folder A, Word + PDF to folder B",
    "nias_control": "NIAS: accepted reports out, reports to check to a review folder",
    "simple": "One report into one folder",
    "empty": "Empty chart",
}


def make(kind: str, name: str = "", *, source: str = "", method: str = "", folder_a: str = "",
         folder_b: str = "") -> Workflow:
    wf = Workflow(name=name or TEMPLATES.get(kind, "Workflow").split(":")[0])
    if kind == "empty":
        return wf
    src = wf.add_node("source", 40, 160, folder=source)
    m = wf.add_node("method", 280, 160, method=method)
    wf.connect(src.id, m.id)
    if kind == "simple":
        rep = wf.add_node("report", 520, 160, kind="nias", formats=["xlsx", "docx"])
        out = wf.add_node("folder", 760, 160, path=folder_a)
        wf.connect(m.id, rep.id)
        wf.connect(rep.id, out.id)
        return wf
    r2 = wf.add_node("report2", 520, 160)
    wf.connect(m.id, r2.id)
    formats = ["xlsx", "docx", "pdf", "batch_docx", "batch_xlsx"]
    if kind == "nias_control":
        rep = wf.add_node("report", 760, 160, kind="nias", formats=formats)
        wf.connect(r2.id, rep.id)
        ok = wf.add_node("folder", 1000, 80, path=folder_a)
        review = wf.add_node("folder", 1000, 260, path=folder_b, subfolder="{batch}")
        wf.connect(rep.id, ok.id, status=["accepted"])
        wf.connect(rep.id, review.id, status=["control"], formats=["docx", "xlsx"])
        return wf
    # "nias": the user's example - Excel into folder A, Word (and PDF) into folder B
    rep = wf.add_node("report", 760, 160, kind="nias", formats=formats)
    wf.connect(r2.id, rep.id, status=["accepted"])
    a = wf.add_node("folder", 1000, 80, path=folder_a)
    b = wf.add_node("folder", 1000, 260, path=folder_b)
    wf.connect(rep.id, a.id, formats=["xlsx", "batch_xlsx"])
    wf.connect(rep.id, b.id, formats=["docx", "pdf", "batch_docx"])
    return wf

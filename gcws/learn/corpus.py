"""Find the human evaluation workbooks under a training root: batch folder -> *.D -> Auswertung/*.xlsm,
with the run they evaluate and the blank runs of the same batch. Read-only."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from gcws.io.sequence import BLANK, BLANK_ISTD, classify_role

_RE_ANALYST = re.compile(r"SYN\d+_([A-Za-z]{2,4})_", re.IGNORECASE)


@dataclass
class CorpusEntry:
    workbook: str
    run_dir: str
    batch_dir: str
    analyst: str = ""
    run_role: str = ""
    blanks: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def find_workbooks(root: Path) -> list[Path]:
    """Workbooks in an 'Auswertung' folder directly inside a *.D run; Excel lock files (~$…) and the
    ChemStation method files under AcqData are not evaluations."""
    found = []
    for path in Path(root).rglob("*.xls*"):
        if path.suffix.lower() not in (".xlsm", ".xlsx", ".xls") or path.name.startswith("~$"):
            continue
        if path.parent.name.casefold() == "auswertung" and path.parent.parent.suffix.casefold() == ".d":
            found.append(path)
    return sorted(found)


def analyst_from_name(name: str) -> str:
    """'NIAS-Screening-SYN25011889_BlM_ 05_….xlsm' -> 'BlM'."""
    m = _RE_ANALYST.search(name)
    return m.group(1) if m else ""


def scan(root: Path) -> list[CorpusEntry]:
    entries = []
    for wb in find_workbooks(root):
        run = wb.parent.parent
        batch = run.parent
        role = classify_role(run.name)
        blanks = sorted(p.name for p in batch.iterdir()
                        if p.is_dir() and p.suffix.casefold() == ".d" and p != run
                        and classify_role(p.name) in (BLANK, BLANK_ISTD))
        e = CorpusEntry(workbook=str(wb), run_dir=str(run), batch_dir=str(batch),
                        analyst=analyst_from_name(wb.name), run_role=role, blanks=blanks)
        if role in (BLANK, BLANK_ISTD):
            e.problems.append("evaluated run is a blank")
        if not blanks:
            e.problems.append("no blank run in batch")
        entries.append(e)
    return entries

"""Injection order, run roles and suggestions for blanks and replicates.

The role rules are copied from NIAS ``classify_batch_folder`` so a folder is
classified exactly as the NIAS batch processor does: an 8-digit Syneris number
makes a determination, solvent names make a blank, ISTD markers a Blank+ISTD,
and an explicit blank keyword always wins.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Iterable, Optional

SAMPLE = "sample"
BLANK = "blank"
BLANK_ISTD = "blank_istd"
STANDARD = "standard"
LADDER = "ladder"
ROLES = (SAMPLE, BLANK, BLANK_ISTD, STANDARD, LADDER)
ROLE_LABELS = {SAMPLE: "Sample", BLANK: "Blank", BLANK_ISTD: "Blank + ISTD",
               STANDARD: "Standard", LADDER: "Alkane ladder"}

BATCH_SOLVENT_TOKENS = {
    "etoh", "ethanol", "meoh", "methanol",
    "ipa", "ipoh", "isopropanol", "2propanol", "propan2ol",
    "etoac", "ethylacetat", "ethylacetate", "ethylacetic",
    "dcm", "dichlormethan", "dichloromethan", "dichloromethane",
    "methylenchlorid", "methylenechloride",
    "hex", "hexan", "hexane", "nhexan", "nhexane",
    "hep", "heptan", "heptane", "nheptan", "nheptane",
    "acn", "mecn", "acetonitril", "acetonitrile", "thf", "tetrahydrofuran",
    "tenax", "essigsaure", "essigsaeure", "aceticacid", "hac",
}
BATCH_BLANK_TOKENS = {"blank", "blanc", "blind", "leerwert"}
BATCH_ISTD_MARKERS = ("istd", "internalstandard")
LADDER_TOKENS = {"alkane", "alkanes", "alkan", "alkanes", "ladder", "alkanreihe", "c7c40", "c8c40", "c10c40"}
SYNERIS_FOLDER_NUMBER = re.compile(r"(?<!\d)(\d{8})(?!\d)")
LEADING_NUMBER = re.compile(r"^\s*(\d+)")
_UMLAUTS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss", "Ä": "a", "Ö": "o", "Ü": "u"})


def _fold(value) -> str:
    folded = ("" if value is None else str(value).strip()).translate(_UMLAUTS)
    folded = unicodedata.normalize("NFKD", folded)
    return "".join(c for c in folded if not unicodedata.combining(c))


def _parts(value) -> tuple[set[str], str]:
    name = re.sub(r"\.(?:d|qgd)$", "", _fold(value), flags=re.IGNORECASE)
    tokens = {p.casefold() for p in re.split(r"[^0-9A-Za-z]+", name) if p}
    return tokens, re.sub(r"[^a-z0-9]+", "", name.casefold())


def classify_role(name) -> str:
    tokens, normalized = _parts(name)
    if tokens & LADDER_TOKENS:
        return LADDER
    has_istd = any(m in normalized for m in BATCH_ISTD_MARKERS)
    core = normalized
    for m in BATCH_ISTD_MARKERS:
        core = core.replace(m, "")
    core = core.strip("0123456789")
    has_solvent = bool(tokens & BATCH_SOLVENT_TOKENS) or core in BATCH_SOLVENT_TOKENS
    has_blank = bool(tokens & BATCH_BLANK_TOKENS) or any(t in normalized for t in BATCH_BLANK_TOKENS)
    if has_blank:
        return BLANK_ISTD if has_istd else BLANK
    if SYNERIS_FOLDER_NUMBER.search(_fold(name)):
        return SAMPLE
    if has_solvent or has_istd:
        return BLANK_ISTD if has_istd else BLANK
    return SAMPLE


def sample_number(name) -> str:
    m = SYNERIS_FOLDER_NUMBER.search(_fold(name))
    return m.group(1) if m else ""


def replicate_stem(name) -> str:
    """Name without injection prefix and trailing replicate letter/number."""
    stem = re.sub(r"\.(?:d|qgd)$", "", _fold(name), flags=re.IGNORECASE)
    stem = LEADING_NUMBER.sub("", stem).lstrip("_- ")
    stem = re.sub(r"[_\- ]+(?:[A-Za-z]|\d{1,2}|rep\d+)$", "", stem, flags=re.IGNORECASE)
    return stem.casefold()


def replicate_label(name) -> str:
    stem = re.sub(r"\.(?:d|qgd)$", "", _fold(name), flags=re.IGNORECASE)
    m = re.search(r"[_\- ]+([A-Za-z]|\d{1,2})$", stem)
    return m.group(1).upper() if m else ""


# -- injection order -------------------------------------------------------

@dataclass
class SeqLine:
    """One planned injection of a sequence log."""
    index: int                      # line number of the sequence
    datafile: str                   # "07_..._A.D"
    datapath: str = ""              # acquisition data path on the instrument PC
    sample: str = ""                # sample name

    @property
    def stem(self) -> str:
        return Path(self.datafile).stem.casefold()


@dataclass
class SequenceInfo:
    """What the sequence log says about one batch folder.

    ``lines`` are the injections planned for this folder only (a sequence can write into several
    folders), in injection order. ``completed`` / ``aborted`` come from the ``.LOG`` beside it."""
    tsv: Optional[Path] = None
    log: Optional[Path] = None
    lines: list[SeqLine] = field(default_factory=list)
    started: str = ""
    completed: bool = False
    aborted: bool = False

    @property
    def stems(self) -> list[str]:
        return [ln.stem for ln in self.lines]

    @property
    def finished(self) -> bool:
        return self.completed or self.aborted


def sequence_files(folder) -> list[Path]:
    """Sequence logs (``*Sequence Log*.TSV``, ``*sequence*.tsv``) in ``folder``, newest first."""
    folder = Path(folder)
    try:
        found = {p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".tsv"
                 and "sequence" in p.name.casefold()}
    except OSError:
        return []

    def mtime(p):
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0
    return sorted(found, key=lambda p: (-mtime(p), p.name))


def _read_lines(tsv: Path) -> tuple[list[SeqLine], str]:
    try:
        text = tsv.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return [], ""
    rows = text.splitlines()
    started = rows[0].strip() if rows and "\t" not in rows[0] else ""
    lines = [ln for ln in rows if "\t" in ln]
    out = []
    for n, row in enumerate(csv.DictReader(lines, delimiter="\t"), 1):
        name = (row.get("Sdatafile$") or row.get("_dataname$") or "").strip()
        if not name:
            continue
        if not name.lower().endswith(".d"):
            name += ".D"
        try:
            index = int((row.get("_seqline") or "").strip() or n)
        except ValueError:
            index = n
        out.append(SeqLine(index, name, (row.get("_datapath$") or "").strip(),
                           (row.get("_dataname$") or "").strip()))
    return out, started


def _folder_name(datapath: str) -> str:
    return PureWindowsPath(datapath.rstrip("\\/")).name.casefold() if datapath else ""


def lines_for_folder(lines: list[SeqLine], folder: Path, present: Iterable[str] = ()) -> list[SeqLine]:
    """The lines of a sequence log that write into ``folder``.

    Matched by the last part of the acquisition data path; a copied or renamed folder takes the
    data path whose runs it holds. Logs without data paths belong to their folder."""
    if not lines:
        return []
    groups: dict[str, list[SeqLine]] = {}
    for ln in lines:
        groups.setdefault(_folder_name(ln.datapath), []).append(ln)
    if set(groups) == {""}:
        return list(lines)
    name = Path(folder).name.casefold()
    if name in groups:
        return groups[name]
    have = {Path(p).stem.casefold() for p in present}
    best = max(groups.values(), key=lambda g: len(have & {ln.stem for ln in g}))
    if have & {ln.stem for ln in best}:
        return best
    return list(lines) if len(groups) == 1 else []


def _log_state(tsv: Path) -> tuple[Optional[Path], bool, bool]:
    log = tsv.with_suffix(".LOG")
    if not log.is_file():
        log = next((p for p in tsv.parent.glob(tsv.stem + ".*") if p.suffix.lower() == ".log"), None)
    if log is None:
        return None, False, False
    try:
        text = log.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return log, False, False
    completed = re.search(r"sequence\s+completed", text, re.IGNORECASE) is not None
    aborted = re.search(r"sequence\s+(?:aborted|stopped|terminated|abgebrochen)", text, re.IGNORECASE) is not None
    return log, completed, aborted


def read_sequence(folder, present: Iterable[str] = (), siblings: bool = True) -> SequenceInfo:
    """The sequence log of the batch ``folder`` (see :class:`SequenceInfo`).

    ``present`` are run names already in the folder (to match a copied folder). Without a log
    of its own, the logs in neighbouring folders are searched for lines writing into it (a
    sequence keeps its log in the folder it started in)."""
    folder = Path(folder)
    present = list(present)
    candidates = [(t, True) for t in sequence_files(folder)]
    if not candidates and siblings:
        try:
            neighbours = sorted(p for p in folder.parent.iterdir() if p.is_dir() and p != folder
                                and not p.name.lower().endswith(".d"))[:200]
        except OSError:
            neighbours = []
        candidates = [(t, False) for n in neighbours for t in sequence_files(n)]
    for tsv, own in candidates:
        lines, started = _read_lines(tsv)
        if not own:
            lines = [ln for ln in lines if _folder_name(ln.datapath) == folder.name.casefold()]
        else:
            lines = lines_for_folder(lines, folder, present)
        if not lines:
            continue
        log, completed, aborted = _log_state(tsv)
        return SequenceInfo(tsv, log, lines, started, completed, aborted)
    return SequenceInfo()


def parse_sequence_log(folder) -> list[str]:
    """Data-file stems of ``folder`` in injection order from its ``Sequence Log .TSV``."""
    return read_sequence(folder, siblings=False).stems


def log_stamp(folder) -> tuple:
    """Changes when a sequence log of ``folder`` changes (cache key)."""
    out = []
    for p in sequence_files(folder):
        try:
            out.append((p.name, p.stat().st_mtime_ns))
        except OSError:
            continue
    return tuple(out)


def order_key(path, sequence: list[str] | None = None):
    p = Path(path)
    stem = p.stem.casefold()
    if sequence and stem in sequence:
        return (0, sequence.index(stem), stem)
    m = LEADING_NUMBER.match(p.name)
    return (1, int(m.group(1)) if m else 10 ** 9, stem)


def run_order(paths: Iterable, sequence: list[str] | None = None) -> list[Path]:
    paths = [Path(p) for p in paths]
    if sequence is None and paths:
        sequence = parse_sequence_log(paths[0].parent)
    return sorted(paths, key=lambda p: order_key(p, sequence))


# -- suggestions -------------------------------------------------------------

def suggest_blanks(sample, runs: dict, roles: dict, order: list) -> tuple[list, list]:
    """(blank ids, blank+ISTD ids) proposed for ``sample``.

    ``runs`` maps id -> path, ``roles`` id -> role, ``order`` is the list of
    ids in injection order. Blank: the nearest *following* blank (the solvent
    injected after the sample carries its carry-over), else the nearest
    preceding one. Blank+ISTD: the nearest preceding one, else any.
    """
    if sample not in order:
        return [], []
    i = order.index(sample)
    after = order[i + 1:]
    before = list(reversed(order[:i]))
    blanks = [r for r in after if roles.get(r) == BLANK] or [r for r in before if roles.get(r) == BLANK]
    istd = [r for r in before if roles.get(r) == BLANK_ISTD] or [r for r in after if roles.get(r) == BLANK_ISTD]
    return blanks[:1], istd[:1]


def suggest_replicates(names: dict) -> list[list]:
    """Groups of ids whose names differ only by the replicate suffix."""
    groups: dict[tuple[str, str], list] = {}
    for rid, name in names.items():
        if classify_role(name) != SAMPLE:
            continue
        key = (sample_number(name), replicate_stem(name))
        groups.setdefault(key, []).append(rid)
    return [ids for ids in groups.values() if len(ids) > 1]

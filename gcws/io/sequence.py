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
from pathlib import Path
from typing import Iterable

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
    name = re.sub(r"\.d$", "", _fold(value), flags=re.IGNORECASE)
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
    stem = re.sub(r"\.d$", "", _fold(name), flags=re.IGNORECASE)
    stem = LEADING_NUMBER.sub("", stem).lstrip("_- ")
    stem = re.sub(r"[_\- ]+(?:[A-Za-z]|\d{1,2}|rep\d+)$", "", stem, flags=re.IGNORECASE)
    return stem.casefold()


def replicate_label(name) -> str:
    stem = re.sub(r"\.d$", "", _fold(name), flags=re.IGNORECASE)
    m = re.search(r"[_\- ]+([A-Za-z]|\d{1,2})$", stem)
    return m.group(1).upper() if m else ""


# -- injection order -------------------------------------------------------

def parse_sequence_log(folder) -> list[str]:
    """Data-file names in injection order from a ``Sequence Log .TSV``."""
    folder = Path(folder)
    for log in sorted(folder.glob("*Sequence Log*.TSV")) + sorted(folder.glob("*sequence*.tsv")):
        try:
            text = log.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        lines = [ln for ln in text.splitlines() if "\t" in ln]
        if not lines:
            continue
        reader = csv.DictReader(lines, delimiter="\t")
        order = []
        for row in reader:
            name = (row.get("Sdatafile$") or row.get("_dataname$") or "").strip()
            if name:
                order.append(Path(name).stem.casefold())
        if order:
            return order
    return []


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

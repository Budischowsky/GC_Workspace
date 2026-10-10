"""Flags of an identified substance for the substance (peak) table and the replicate table.

* **new**: the substance is in none of the lab's references -- not in an analyst's evaluation of the learned
  data (``data/learn/corpus``), not in the register of the substances GC Workspace reported
  (``unknown_register.sqlite``) and not in CASINFO.xlsx. A CAS is matched by CAS, a substance without one by
  its name.
* **elements**: the formula has elements other than C, H, O and N (Si, Cl, P, S, ...); deuterium counts as H.

The references are read once and again only when one of their files changed (checked at most every
:data:`RECHECK_S` seconds), so a table can ask for every cell.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

#: the elements that are not flagged
CHON = frozenset("CHON")
RECHECK_S = 30.0
_ELEMENT = re.compile(r"([A-Z][a-z]?)(\d*)")
_CAS = re.compile(r"0*(\d{2,7})-(\d{2})-(\d)")
_SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")


def cas_key(value) -> str:
    """``0000108-88-3`` and ``108-88-3`` are one CAS; anything else (``0``, ``n/a``, a name) is ``""``."""
    text = str(value or "").strip().replace("/", "-").split(";")[0].split(",")[0].strip()
    m = _CAS.fullmatch(text)
    return f"{int(m.group(1))}-{m.group(2)}-{m.group(3)}" if m else ""


def name_key(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def elements(formula) -> list[str]:
    """The elements of ``formula`` in their order ("C6H5Cl" -> C, H, Cl); D (deuterium) is H."""
    text = re.sub(r"[\s·.()\[\]]", "", str(formula or "").translate(_SUBSCRIPTS))
    out = []
    for sym, _n in _ELEMENT.findall(text):
        sym = "H" if sym == "D" else sym
        if sym not in out:
            out.append(sym)
    return out


def foreign_elements(formula) -> list[str]:
    """The elements of ``formula`` other than C, H, O and N."""
    return [e for e in elements(formula) if e not in CHON]


def is_unknown(name) -> bool:
    n = name_key(name)
    return (not n or n.startswith(("unknown", "unbekannt", "no hit", "kein treffer", "?"))
            or n in ("-", "n/a"))


# -- the references ------------------------------------------------------------------------------

class _Known:
    def __init__(self):
        self.lock = threading.Lock()
        self.cas: set[str] = set()
        self.names: set[str] = set()
        self.stamp = None
        self.checked = 0.0

    def sources(self) -> list[Path]:
        from gcws import paths
        out = [paths.DATA / "learn" / "corpus", register_path()]
        cas = casinfo_path()
        if cas is not None:
            out.append(cas)
        return out

    def ensure(self):
        now = time.monotonic()
        if self.stamp is not None and now - self.checked < RECHECK_S:
            return
        with self.lock:
            if self.stamp is not None and now - self.checked < RECHECK_S:
                return
            stamp = tuple((str(p), _mtime(p)) for p in self.sources())
            self.checked = now
            if stamp == self.stamp:
                return
            cas, names = set(), set()
            _add_corpus(cas, names)
            _add_register(cas, names)
            _add_casinfo(cas)
            self.cas, self.names, self.stamp = cas, names, stamp
            _flags.cache_clear()


_KNOWN = _Known()


def _mtime(p: Path) -> float:
    try:
        if p.is_dir():                      # a record added or replaced changes the folder or a file in it
            return max([p.stat().st_mtime] + [f.stat().st_mtime for f in p.glob("*.json")])
        return p.stat().st_mtime
    except OSError:
        return 0.0


def register_path() -> Path:
    from gcws import paths
    return paths.DATA / "unknown_register.sqlite"


def casinfo_path() -> Optional[Path]:
    from gcws import paths
    try:
        from gcws.ui.dialogs.preferences import load_settings
        raw = load_settings().get("standard_cas_path", "CASINFO.xlsx")
    except Exception:  # noqa: BLE001
        raw = "CASINFO.xlsx"
    p = Path(raw)
    if not p.is_absolute():
        p = next((b / p for b in (paths.RESOURCES, paths.ROOT, paths.DATA) if (b / p).exists()), p)
    return p if p.exists() else None


def _add(cas: set, names: set, c, n):
    k = cas_key(c)
    if k:
        cas.add(k)
    elif not is_unknown(n):
        names.add(name_key(n))


def _add_corpus(cas: set, names: set):
    from gcws import paths
    for f in sorted((paths.DATA / "learn" / "corpus").glob("*.json")):
        try:
            ev = json.loads(f.read_text(encoding="utf-8")).get("evaluation") or {}
        except (OSError, ValueError):
            continue
        for row in (ev.get("final") or []) + (ev.get("report") or []):
            if isinstance(row, dict):
                _add(cas, names, row.get("cas"), row.get("label"))


def _add_register(cas: set, names: set):
    p = register_path()
    if not p.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            for c, n in con.execute("SELECT cas_key, name_key FROM reported_substances"):
                if c:
                    cas.add(cas_key(c) or c)
                elif n and not is_unknown(n):
                    names.add(name_key(n))
        finally:
            con.close()
    except sqlite3.Error:
        return                              # no register (or locked): the other references still count


def _add_casinfo(cas: set):
    try:
        from gcws.quant.service import cas_lookup
        lookup = cas_lookup()
    except Exception:  # noqa: BLE001
        return
    for c in lookup or {}:
        k = cas_key(c)
        if k:
            cas.add(k)


def is_new(name, cas) -> Optional[bool]:
    """True when the substance is in none of the references; None for an unknown (no substance)."""
    k = cas_key(cas)
    if not k and is_unknown(name):
        return None
    _KNOWN.ensure()
    return k not in _KNOWN.cas if k else name_key(name) not in _KNOWN.names


# -- the flag text -------------------------------------------------------------------------------

@lru_cache(maxsize=8192)
def _flags(name: str, cas: str, formula: str) -> tuple[str, str]:
    parts, tips = [], []
    new = is_new(name, cas)
    if new:
        parts.append("new")
        tips.append("New: not in the learned evaluations, not in the register of reported substances and not "
                    "in CASINFO.xlsx" + ("" if cas_key(cas) else " (compared by name: no CAS)") + ".")
    other = foreign_elements(formula)
    if other:
        parts.append(", ".join(other))
        tips.append(f"Formula {formula}: elements other than C, H, O, N ({', '.join(other)}).")
    return " · ".join(parts), "\n".join(tips)


def flags(name, cas, formula) -> tuple[str, str]:
    """``(text, tooltip)`` of a substance, e.g. ``("new · Si", "...")``; ``("", "")`` without a flag."""
    _KNOWN.ensure()
    return _flags(str(name or ""), str(cas or ""), str(formula or ""))


def formula_of(ident) -> str:
    """The formula of an identification: its own, else the one of its hit with its CAS (or name)."""
    if ident is None:
        return ""
    if getattr(ident, "formula", ""):
        return ident.formula
    k, n = cas_key(ident.cas), name_key(ident.name)
    for h in getattr(ident, "hits", None) or []:
        if h.get("formula") and ((k and cas_key(h.get("cas")) == k) or (not k and name_key(h.get("name")) == n)):
            return str(h["formula"])
    return ""


def formula_index(ws) -> dict[str, str]:
    """CAS key / name key -> formula from every identification (and its hits) of the workspace, for the rows
    of the replicate table (their name may come from the consensus of the determinations)."""
    out: dict[str, str] = {}
    for st in getattr(ws, "states", lambda: [])():
        for key in ("FID", "TIC"):
            try:
                items = st.ident_set(key).items
            except Exception:  # noqa: BLE001
                continue
            for i in items:
                for d in [{"name": i.name, "cas": i.cas, "formula": i.formula}] + list(i.hits or []):
                    f = str(d.get("formula") or "")
                    if not f:
                        continue
                    k = cas_key(d.get("cas"))
                    out.setdefault(k or "name:" + name_key(d.get("name")), f)
    return out


def formula_from(index: dict, name, cas) -> str:
    k = cas_key(cas)
    return index.get(k, "") if k else index.get("name:" + name_key(name), "")

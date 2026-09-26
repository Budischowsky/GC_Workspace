"""Finding and sharing unknowns of the register (``gc_register``, unchanged, read-only here).

* :func:`search` - by text (ID, name, CAS, note), by sample name, or by m/z values: an entry
  matches when every given ion reaches ``min_rel`` % of the base peak in its best spectrum
  (optionally the first ion must be the base peak); entries filed without a spectrum are
  matched on their list of significant ions;
* :func:`entry_record` / :func:`export_msp` - entries as MSP records (the best spectrum, with
  ID, label, RT, samples, status and note in the comment), to share with colleagues or to
  search in NIST MS Search / another program.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

MODES = {"text": "Text (ID, name, CAS, note)", "sample": "Sample name", "mz": "m/z values"}


def parse_mz(text: str) -> list[int]:
    """``"91, 105 77"`` -> ``[91, 105, 77]`` (nominal masses, in the given order)."""
    out = []
    for tok in re.findall(r"\d+(?:[.,]\d+)?", text or ""):
        m = int(round(float(tok.replace(",", "."))))
        if 1 <= m <= 10000 and m not in out:
            out.append(m)
    return out


def relative(spectrum) -> dict[int, float]:
    """``{nominal m/z: % of the base peak}`` of a spectrum ``[(m/z, abundance), ...]``."""
    acc: dict[int, float] = {}
    for m, a in spectrum or []:
        k = int(round(float(m)))
        acc[k] = acc.get(k, 0.0) + float(a)
    top = max(acc.values(), default=0.0)
    return {k: 100.0 * v / top for k, v in acc.items()} if top > 0 else {}


def spectrum_matches(spectrum, ions: list[int], min_rel: float = 5.0, base_first: bool = False) -> bool:
    rel = relative(spectrum)
    if not rel or not ions:
        return False
    if base_first and max(rel, key=rel.get) != ions[0]:
        return False
    return all(rel.get(m, 0.0) >= min_rel for m in ions)


def _sample_ids(con, text: str) -> set[int]:
    like = f"%{text.strip().casefold()}%"
    ids = {int(r[0]) for r in con.execute(
        "SELECT DISTINCT entry_id FROM sightings WHERE LOWER(sample) LIKE ? OR LOWER(sample_name) LIKE ? "
        "OR LOWER(source_file) LIKE ?", (like, like, like))}
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='entry_samples'").fetchone():
        ids |= {int(r[0]) for r in con.execute(
            "SELECT DISTINCT entry_id FROM entry_samples WHERE LOWER(sample) LIKE ?", (like,))}
    return ids


def search(con, mode: str = "text", text: str = "", *, min_rel: float = 5.0, base_first: bool = False,
           with_spectra_only: bool = False, spectra: Optional[dict] = None) -> list[dict]:
    """Register rows (``gc_register.browse_entries`` shape) that match.

    ``spectra`` caches the best spectrum per entry id between searches."""
    import gc_register as R
    text = str(text or "").strip()
    if mode == "text" or not text:
        return R.browse_entries(con, search=text if mode == "text" else "", with_spectra_only=with_spectra_only)
    rows = R.browse_entries(con, with_spectra_only=with_spectra_only)
    if mode == "sample":
        ids = _sample_ids(con, text)
        return [r for r in rows if int(r["entry_id"]) in ids]
    ions = parse_mz(text)
    if not ions:
        return []
    cache = spectra if spectra is not None else {}
    out = []
    for r in rows:
        eid = int(r["entry_id"])
        if eid not in cache:
            cache[eid] = (R.best_spectrum_for_entry(con, eid) or []) if r.get("n_spectra") else []
        if cache[eid]:
            hit = spectrum_matches(cache[eid], ions, min_rel, base_first)
        else:
            hit = ranked_matches(r.get("ranked_mz") or r.get("mz_key") or "", ions, base_first)
        if hit:
            out.append(r)
    return out


def ranked_matches(ranked: str, ions: list[int], base_first: bool = False) -> bool:
    """Entries filed without a spectrum (older registers) keep their significant ions, strongest
    first, as "149/167/279": every searched ion must be among them."""
    listed = parse_mz(ranked)
    if not listed or not ions:
        return False
    if base_first and listed[0] != ions[0]:
        return False
    return set(ions) <= set(listed)


def _samples(con, entry_id: int, limit: int = 12) -> list[str]:
    rows = con.execute("SELECT DISTINCT COALESCE(NULLIF(sample_name, ''), sample) FROM sightings "
                       "WHERE entry_id = ? ORDER BY sighting_id", (int(entry_id),)).fetchall()
    return [str(r[0]) for r in rows if r[0]][:limit]


def entry_record(con, entry_id: int):
    """One entry as an MSP record (``gcws.identify.library_edit.MspRecord``), or None without spectrum."""
    import gc_register as R
    from gcws.identify import library_edit as LE
    row = R.entry_row(con, entry_id)
    spec = R.best_spectrum_for_entry(con, entry_id)
    if row is None or not spec:
        return None
    uid = str(row.get("unknown_id") or f"#{entry_id}")
    name = row.get("assigned_name") or row.get("label") or ""
    title = f"{uid} {name}".strip() if name and name != uid else uid
    rt = row.get("rt_mean")
    samples = _samples(con, entry_id)
    parts = [f"GC Workspace unknown register {uid}"]
    for label, v in (("label", row.get("label")), ("status", row.get("status")),
                     ("RT", f"{rt:.3f} min" if isinstance(rt, (int, float)) else ""),
                     ("sightings", row.get("n_sightings")), ("samples", "; ".join(samples)),
                     ("note", row.get("note"))):
        if v not in (None, "", 0):
            parts.append(f"{label}: {v}")
    cas = str(row.get("assigned_cas") or "").strip()
    if cas and not LE.format_cas(cas):
        parts.append(f"CAS: {cas}")                   # not a valid registry number: kept as text
        cas = ""
    rec = LE.new_record(title, [(float(m), float(a)) for m, a in spec], cas=cas,
                        rt=f"{rt:.3f}" if isinstance(rt, (int, float)) else None, comment=" | ".join(parts),
                        synonyms=[uid] if title != uid else [])
    return rec


def export_msp(con, entry_ids, path) -> int:
    """Write the entries with a spectrum to ``path``; returns how many were written."""
    from gcws.identify import library_edit as LE
    recs = [r for r in (entry_record(con, e) for e in entry_ids) if r is not None]
    Path(path).write_text(LE.write_msp(recs), encoding=LE.ENCODING, errors="replace", newline="")
    return len(recs)


def msp_text(con, entry_ids) -> str:
    from gcws.identify import library_edit as LE
    return LE.write_msp([r for r in (entry_record(con, e) for e in entry_ids) if r is not None])

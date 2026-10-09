"""Learned rules applied in the real report pipeline (spec §3, phase 6).

Family 4: a NIAS row whose peak matches a learned family (by the workspace identification's name or the MS
class hint) gets the family's row name, the label the analysts write in their worksheets, so the NIAS report's
category sums report it as the analysts do. The CAS is cleared (a group label has none) and the reason is kept in
``row.derived["learned"]``. Rows the analyst named by hand and ISTD rows are never changed."""
from __future__ import annotations

from gcws.learn.families import family_of


def apply_families(sample, evidence: dict, families: list[dict]) -> int:
    """Rename the rows of ``sample`` that match ``families``. ``evidence``: row_id -> {"name", "hint", "manual"}.
    Returns the number of renamed rows."""
    istd_rows = {s.get("row_id") for s in getattr(sample, "standards", None) or [] if s.get("row_id") is not None}
    renamed = 0
    for row in sample.rows:
        ev = evidence.get(row.row_id)
        if ev is None or ev.get("manual") or row.row_id in istd_rows:
            continue
        fam = family_of({"name": ev.get("name") or "", "class_hint": ev.get("hint") or ""}, families)
        if fam is None:
            continue
        name = (ev.get("name") or "").strip()
        why = f"name '{name}'" if name in fam.get("names", []) else f"hint '{(ev.get('hint') or '').strip()}'"
        row_name = fam.get("row_name") or fam["label"]
        row.derived["learned"] = (f"family v1 ({fam['label']}): {why} -> '{row_name}'; "
                                  f"was '{row.name}' ({row.cas})")
        row.name, row.cas = row_name, "0"
        renamed += 1
    return renamed


def row_evidence(ws, st, det, key, sample) -> dict:
    """row_id -> the workspace's identification name, manual flag and MS class hint of the row's peak."""
    from gcws.quant.peak_values import class_hint
    out = {}
    for row in sample.rows:
        n = row.derived.get("fid_peak")
        if n is None:
            continue
        ident = det.idents.get(n)
        try:
            hint = class_hint(ws, st.id, key, det.peaks[n])[0]
        except Exception:  # noqa: BLE001 - no MS data or no hint: matched by name only
            hint = ""
        out[row.row_id] = {"name": ident.name if ident is not None else "",
                           "manual": bool(ident is not None and ident.manual), "hint": hint}
    return out

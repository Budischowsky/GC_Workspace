"""Signal-scoped spectrum overrides, with replay-stable split identities."""
from __future__ import annotations

import json


def fragment_id(peak) -> str:
    return (getattr(peak, "extra", None) or {}).get("spectrum_id", "")


def override_key(key: str, peak) -> str:
    identity = fragment_id(peak) or f"rt:{peak.apex_rt:.4f}"
    return "peak:" + json.dumps([key, identity], separators=(",", ":"))


def override_for(st, key: str, peak):
    scoped = override_key(key, peak)
    if scoped in st.spectrum_overrides:
        return st.spectrum_overrides[scoped]
    # Old projects used unscoped RT keys. Never inherit these on a split child.
    if not fragment_id(peak):
        return st.spectrum_overrides.get(round(peak.apex_rt, 4))
    return None


def restore_overrides(values: dict) -> dict:
    return {k if k.startswith("peak:") else float(k): v for k, v in values.items()}

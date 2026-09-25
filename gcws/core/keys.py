"""Signal keys and the keys derived from them.

A signal key names one trace of a run: ``"FID"``, ``"TIC"``, ``"BPC"`` or
``"EIC 149+57"``. A *derived* key is a base key plus a suffix, e.g.
``"FID - Blank"`` (the sample trace minus its assigned blank). Derived traces
are integrated with the method of their base kind and share its
identifications, so every comparison must go through these helpers instead of
testing ``key == "FID"``.
"""
from __future__ import annotations

FID = "FID"
TIC = "TIC"
BLANK_SUFFIX = " - Blank"
DERIVED_SUFFIXES = (BLANK_SUFFIX,)


def split_key(key: str) -> tuple[str, str | None]:
    """``(base, suffix)``; the suffix is ``None`` for a plain key."""
    key = (key or "").strip()
    for suffix in DERIVED_SUFFIXES:
        if key.lower().endswith(suffix.lower()):
            return key[: -len(suffix)].strip(), suffix
    return key, None


def base_key(key: str) -> str:
    return split_key(key)[0]


def is_derived(key: str) -> bool:
    return split_key(key)[1] is not None


def is_blank_key(key: str) -> bool:
    return split_key(key)[1] == BLANK_SUFFIX


def derived_key(base: str, suffix: str = BLANK_SUFFIX) -> str:
    return base_key(base) + suffix


def is_fid(key: str) -> bool:
    """True for the FID trace and everything derived from it."""
    return base_key(key).upper() == FID


def method_kind(key: str) -> str:
    """Integration-method family of a key: ``"FID"`` or ``"TIC"`` (all MS traces)."""
    return FID if is_fid(key) else TIC

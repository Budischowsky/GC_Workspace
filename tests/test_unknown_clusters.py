"""Clustering the unknown register: each entry's ion profile is built once, not once per pair.

The register grew to 500+ entries (2026-10-10) and every report clusters all of it; with the profile
(settings read, mkdir, register stat) rebuilt for both sides of every pair, one NIAS report took 2-3 min.
"""
import random

from gcws.report.legacy_api import main_script


def _register(n: int) -> dict:
    rnd = random.Random(7)
    unknowns = {}
    for i in range(n):
        group = i % 6                               # members of a group share most of their ions
        ions = [43 + 14 * group, 57 + 14 * group, 71 + 14 * group, 99 + group]
        ions += rnd.sample(range(150, 300), 1 if i % 3 else 4)
        unknowns[f"k{i:03d}"] = {"id": f"UNK-{i + 1:04d}", "ranked_mz": ions,
                                 "rt_mean": 8.0 + group + 0.01 * (i % 3), "samples": [f"s{i % 4}"]}
    return {"unknowns": unknowns}


def _expected(main, register, threshold):
    entries = register["unknowns"]
    keys = sorted(entries)
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if main.unknown_entry_similarity(entries[a], entries[b])["total"] >= threshold:
                parent[find(b)] = find(a)
    groups = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)


def test_clustering_builds_each_entry_profile_once(monkeypatch):
    main = main_script()
    monkeypatch.setattr(main, "measured_entry_spectrum", lambda entry: None)   # no register file in a test
    register = _register(40)
    expected = _expected(main, register, main.UNKNOWN_CLUSTER_THRESHOLD)
    assert expected                                  # the data has real clusters

    calls = []
    profile = main.entry_ion_profile
    monkeypatch.setattr(main, "entry_ion_profile", lambda entry: calls.append(1) or profile(entry))
    n = main.compute_unknown_clusters(register)

    assert len(calls) == 40
    got = {}
    for key, entry in register["unknowns"].items():
        if entry["cluster"]:
            got.setdefault(entry["cluster"], []).append(key)
    assert n == len(expected)
    assert sorted(sorted(g) for g in got.values()) == expected

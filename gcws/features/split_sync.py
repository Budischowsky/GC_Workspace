"""The same deconvolution split in every determination.

The automatic deconvolution split (:mod:`gcws.integration.auto_deconv`) works on each run alone.
When the MS of one injection resolves two co-eluting components and the other's does not (a
weaker signal, one scan less of separation), one determination has two fragments where the other
has one peak: the feature is red ("integrated as one peak here and as two there") and both
substances lose their pair.

Like gap filling, the split is then searched for in the other determination with evidence
instead of being invented: the components of the split determination (their elution profiles,
spectra and model ions) are carried over through the drift map and fitted to the other
determination's trace (:func:`gcws.ms.peak_split.plan_split`). Only a trusted fit (R² at least
the integration method's limit, every component taking signal, no two curves alike) becomes a
proposal: a replayable split event, made automatically as one undo step with the gap fills. The
feature is yellow afterwards ("split as in A"); otherwise it stays red.
"""
from __future__ import annotations

import copy
import hashlib

from gcws.core.keys import is_fid
from gcws.features.model import FeatureTable, Proposal, Settings

#: prefix of the uid of a carried-over split event (its fragments' ids start with it)
SYNC_UID = "sync"


def _uid(run_id: str, key: str, peak, comps) -> str:
    h = hashlib.sha1(f"{run_id}|{key}|{peak.start:.5f}|{peak.end:.5f}".encode())
    for c in comps:
        h.update(f"|{c['rt']:.4f}:{c['model_mz']}".encode())
    return f"{SYNC_UID}-{h.hexdigest()[:10]}"


def is_sync(fragment: str) -> bool:
    return bool(fragment) and fragment.startswith(SYNC_UID + "-")


def _carried(dc: dict, shift: float) -> dict:
    """A fragment's component moved by ``shift`` (MS minutes) onto the other run."""
    out = copy.deepcopy({k: dc[k] for k in ("rt", "model_mz", "purity", "area", "spectrum", "profile") if k in dc})
    out["rt"] = float(dc["rt"]) + shift
    if out.get("profile"):
        out["profile"] = [[float(t) + shift, float(y)] for t, y in out["profile"]]
    return out


def propose(ws, table: FeatureTable, settings: Settings) -> list[Proposal]:
    """Split proposals for the determinations whose peak covers a split of another determination."""
    from gcws.integration.auto_deconv import limits_of
    from gcws.ms.peak_split import plan_split
    out: list[Proposal] = []
    done: set = set()
    key = table.key
    for f in table.features:
        if not f.split:
            continue
        for m in f.found:
            if m.peak.fragment or (m.run_id, m.peak.index) in done:
                continue                           # already split here
            for x in f.found:
                if x.run_id == m.run_id or not x.peak.fragment:
                    continue
                src = table.input(x.run_id)
                uid = x.peak.fragment.rsplit(":", 1)[0]
                parts = [p for p in src.peaks if p.fragment.startswith(uid + ":")]
                tmap = table.maps.get(x.run_id)
                if len(parts) < 2 or tmap is None:
                    continue
                inside = []
                for y in parts:
                    t = m.peak.rt + (float(tmap.to_ref(y.rt)) - m.rt_ref)
                    if m.peak.start < t < m.peak.end:
                        inside.append((y, t))
                if len(inside) < 2:
                    continue
                st_r, st_s = ws.runs.get(m.run_id), ws.runs.get(x.run_id)
                res_r, res_s = ws.result(m.run_id, key), ws.result(x.run_id, key)
                if None in (st_r, st_s, res_r, res_s) or st_r.run.ms is None:
                    continue
                peak = res_r.peaks[m.peak.index]
                fid = is_fid(key)
                comps = []
                for y, t in inside:
                    dc = res_s.peaks[y.index].extra.get("deconv_component") or {}
                    if "rt" not in dc or "model_mz" not in dc:
                        break
                    # detector time t in R <- y.rt in S; MS time = detector time - delay (FID)
                    shift = (t - y.rt) + ((st_s.delay_value - st_r.delay_value) if fid else 0.0)
                    comps.append(_carried(dc, shift))
                if len(comps) != len(inside):
                    continue
                sig = st_r.run.signal(key)
                riders = [(p.start, p.end) for p in res_r.peaks if p.parent == m.peak.index]
                method = ws.method_for(st_r, key)
                plan = plan_split(sig, peak, key, st_r.delay_value, comps, checked=list(range(len(comps))),
                                  riders=riders, limits=limits_of(method))
                if not plan.ok or plan.basis != "fit":
                    m.note = m.note or (f"split as in {x.label} not confirmed by the {plan.signal_name}: "
                                        f"{plan.problem or plan.basis_note}")
                    continue
                event = plan.event()
                text = (f"{f.id} {m.label}: split into {len(comps)} components as in {x.label} "
                        f"({plan.summary()})")
                event = event.with_(uid=_uid(m.run_id, key, peak, comps), comment=text, user="automatic")
                out.append(Proposal("split", m.run_id, key, text, event=event, rt=float(peak.apex_rt)))
                done.add((m.run_id, m.peak.index))
                break
    return out


def propose_table(ws, table: FeatureTable, settings: Settings) -> None:
    """Attach the split proposals to the features they resolve."""
    for p in propose(ws, table, settings):
        f = next((f for f in table.features if f.split and any(
            m.run_id == p.run_id and m.found and abs(m.peak.rt - p.rt) < 1e-9 for m in f.members)), None)
        if f is not None:
            f.proposals.append(p)

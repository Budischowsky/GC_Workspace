"""The integrator: raw signal + method (+ manual events) -> peak table.

Pipeline
  1. auto parameters (noise, peak width, smoothing, slope, threshold)
  2. timed events compiled to per-point arrays
  3. Savitzky-Golay smoothing and derivatives
  4. slope-based detection -> clusters of peaks joined by valleys
  5. timed baseline events (split, baseline now, next valley)
  6. baselines: common baseline with drop lines or valley-to-valley,
     penetration correction, hold / backward
  7. solvent flags, shoulders, tangent/exponential skims, area sums
  8. negative peaks
  9. measurement and rejects
 10. manual events, final measurement, area %, numbering, digest
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Iterable

import numpy as np

import gcws
from gcws.core.events import ManualEvent
from gcws.core.model import Baseline, Peak, Signal
from gcws.integration import manual as MAN
from gcws.integration import measure as MS
from gcws.integration import skim as SK
from gcws.integration.autoparams import Resolved, resolve
from gcws.integration.detector import detect
from gcws.integration.method import IntegrationMethod
from gcws.integration.timeline import Timeline, compile_timeline
from gcws.integration.work import WP, WorkSignal, apex_of, line_between
from gcws.signal import savgol
from gcws.signal.envelope import envelope

#: auto valley depth in multiples of the height threshold
VALLEY_DEPTH_MULT = 1.0

#: a shoulder flattens the slope to at most this fraction of the steepest slope
SHOULDER_FLATTENING = 0.6

#: points (as a fraction of the peak width) needed to confirm a rise / a flat end
UP_DIV = 8.0
DN_DIV = 4.0


@dataclass
class IntegrationResult:
    peaks: list[Peak]
    resolved: Resolved
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    digest: str = ""
    method_name: str = ""

    def peak_at(self, t: float):
        for p in self.peaks:
            if p.start <= t <= p.end:
                return p
        return None

    def nearest(self, t: float):
        if not self.peaks:
            return None
        return min(self.peaks, key=lambda p: abs(p.apex_rt - t))


# -- helpers -----------------------------------------------------------------

def prepare(signal: Signal, method: IntegrationMethod, t_min=None) -> tuple[WorkSignal, Resolved, Timeline]:
    rt = np.asarray(signal.rt, float)
    y = np.asarray(signal.y, float)
    res = resolve(rt, y, method, t_min)
    tl = compile_timeline(rt, method, res)
    if t_min is not None:
        tl.on &= rt >= t_min
    step = float(np.median(np.diff(rt))) if rt.size > 1 else 1.0
    ys = savgol.smooth(y, res.window, res.order)
    d1 = savgol.derivative(y, res.window, res.order, 1, step)
    d2 = savgol.derivative(y, res.window, max(res.order, 3), 2, step)
    return WorkSignal(rt, y, ys, d1, d2), res, tl


def _merge_shallow(cl, ys: np.ndarray, depth: float):
    """Join neighbours whose separating valley is shallower than ``depth``."""
    if depth <= 0 or len(cl) < 2:
        return cl
    out = [cl[0]]
    for s in cl[1:]:
        p = out[-1]
        v = p.i1
        lower_apex = min(ys[p.ia], ys[s.ia])
        if lower_apex - ys[v] < depth:
            apex = p.ia if ys[p.ia] >= ys[s.ia] else s.ia
            out[-1] = type(p)(p.i0, apex, s.i1, p.ts, s.te)
        else:
            out.append(s)
    return out


def _cluster_line(sig: WorkSignal, t0: float, t1: float) -> Baseline:
    return line_between(sig, t0, t1)


def _dev(sig: WorkSignal, a: int, b: int, base: Baseline) -> np.ndarray:
    return sig.ys[a:b + 1] - base.eval(sig.rt[a:b + 1])


def _assign_base(peaks: list[WP], base: Baseline) -> None:
    for p in peaks:
        p.base = replace(base)


def _split_at(sig: WorkSignal, cluster: list[WP], t: float, code: str) -> tuple[list[WP], list[WP]]:
    """Split a cluster at time t into two independently baselined clusters."""
    left, right = [], []
    for p in cluster:
        if p.t1 <= t + 1e-12:
            left.append(p)
        elif p.t0 >= t - 1e-12:
            right.append(p)
        else:
            q = WP(t, p.t1, p.ta, replace(p.base), ts=code, te=p.te, flags=p.flags,
                   negative=p.negative)
            p.t1, p.te = t, code
            left.append(p)
            right.append(q)
    if left:
        left[-1].te = code
    if right:
        right[0].ts = code
    for part in (left, right):
        if part:
            _assign_base(part, _cluster_line(sig, part[0].t0, part[-1].t1))
    return left, right


def _correct_penetration(sig: WorkSignal, cluster: list[WP], tol: float) -> list[list[WP]]:
    """Redraw the baseline wherever the signal falls below it (code P)."""
    todo, done = [cluster], []
    guard = 0
    while todo and guard < 200:
        guard += 1
        cl = todo.pop()
        a, b = sig.idx(cl[0].t0), sig.idx(cl[-1].t1)
        if b - a < 2:
            done.append(cl)
            continue
        dev = _dev(sig, a, b, cl[0].base)
        k = int(np.argmin(dev[1:-1])) + 1 if dev.size > 2 else 0
        if dev.size <= 2 or dev[k] >= -tol:
            done.append(cl)
            continue
        tp = float(sig.rt[a + k])
        # nearest peak boundary (valley) to the penetration point
        bounds = [p.t1 for p in cl[:-1]]
        if tp < cl[0].ta:
            cl[0].t0, cl[0].ts = tp, "P"
            _assign_base(cl, _cluster_line(sig, cl[0].t0, cl[-1].t1))
            todo.append(cl)
        elif tp > cl[-1].ta:
            cl[-1].t1, cl[-1].te = tp, "P"
            _assign_base(cl, _cluster_line(sig, cl[0].t0, cl[-1].t1))
            todo.append(cl)
        elif bounds:
            tv = min(bounds, key=lambda v: abs(v - tp))
            left, right = _split_at(sig, cl, tv, "P")
            todo.extend([x for x in (left, right) if x])
        else:
            done.append(cl)
    done.extend(todo)
    return done


def _valley_baselines(sig: WorkSignal, cluster: list[WP], tl: Timeline, tol: float) -> None:
    for p in cluster:
        if tl.valley_mode[sig.idx(p.ta)]:
            p.base = _cluster_line(sig, p.t0, p.t1)
            # single-peak penetration: move a bound to the lowest point
            for _ in range(5):
                a, b = sig.idx(p.t0), sig.idx(p.t1)
                if b - a < 3:
                    break
                dev = _dev(sig, a, b, p.base)
                k = int(np.argmin(dev[1:-1])) + 1
                if dev[k] >= -tol:
                    break
                tp = float(sig.rt[a + k])
                if tp < p.ta:
                    p.t0, p.ts = tp, "P"
                else:
                    p.t1, p.te = tp, "P"
                p.base = _cluster_line(sig, p.t0, p.t1)


def _heights(sig: WorkSignal, peaks: Iterable[WP]) -> None:
    for p in peaks:
        a, b = sig.idx(p.t0), sig.idx(p.t1)
        if b < a:
            a, b = b, a
        seg = _dev(sig, a, b, p.base)
        p.height = float((-seg if p.negative else seg).max()) if seg.size else 0.0


def _shoulders(sig: WorkSignal, cluster: list[WP], tl: Timeline, res: Resolved) -> list[WP]:
    out: list[WP] = []
    for p in cluster:
        mode = tl.shoulders[sig.idx(p.ta)]
        if mode == "off" or p.height <= 0:
            out.append(p)
            continue
        ia, i0, i1 = sig.idx(p.ta), sig.idx(p.t0), sig.idx(p.t1)
        d1, d2 = sig.d1, sig.d2
        rear = front = None
        # rear shoulder: d1 local maximum (flattening) on the falling side
        steepest = 0.0
        for i in range(ia + 2, i1 - 2):
            steepest = min(steepest, d1[i - 1])
            if d2[i - 1] > 0 >= d2[i] and d1[i] < 0 and steepest < 0 and d1[i] > SHOULDER_FLATTENING * steepest:
                if float(_dev(sig, i, i, p.base)[0]) > 0.1 * p.height:
                    rear = i
                    break
        steepest = 0.0
        for i in range(ia - 2, i0 + 2, -1):
            steepest = max(steepest, d1[i + 1])
            if d2[i + 1] > 0 >= d2[i] and d1[i] > 0 and steepest > 0 and d1[i] < SHOULDER_FLATTENING * steepest:
                if float(_dev(sig, i, i, p.base)[0]) > 0.1 * p.height:
                    front = i
                    break
        pieces = [p]
        if front is not None:
            t = float(sig.rt[front])
            f = WP(p.t0, t, p.ta, replace(p.base), ts=p.ts, te="V", flags="F")
            p.t0, p.ts = t, "V"
            f.ta = apex_of(sig, f.t0, f.t1, f.base)
            pieces.insert(0, f)
            if mode == "tangent":
                res_t = SK.tangent_front(sig, t, f.ta, f.t0)
                if res_t:
                    f.t0, f.base = res_t
                    f.flags = "f"
                    pieces.remove(f)
                    f.parent = p
                    p.children.append(f)
        if rear is not None:
            t = float(sig.rt[rear])
            r = WP(t, p.t1, p.ta, replace(p.base), ts="V", te=p.te, flags="R")
            p.t1, p.te = t, "V"
            r.ta = apex_of(sig, r.t0, r.t1, r.base)
            pieces.append(r)
            if mode == "tangent":
                res_t = SK.tangent_tail(sig, t, r.ta, r.t1)
                if res_t:
                    r.t1, r.base = res_t
                    r.flags = "r"
                    pieces.remove(r)
                    r.parent = p
                    p.children.append(r)
                    p.t1 = max(p.t1, r.t1)
        p.ta = apex_of(sig, p.t0, p.t1, p.base)
        _heights(sig, pieces)
        out.extend(pieces)
    return out


def _skims(sig: WorkSignal, cluster: list[WP], tl: Timeline, method: IntegrationMethod,
           width: float) -> list[WP]:
    if len(cluster) < 2:
        return cluster
    chain = list(cluster)
    # tail riders
    i = 1
    while i < len(chain):
        child = chain[i]
        prev = chain[i - 1]
        parent = prev.parent if prev.parent is not None else prev
        if parent.parent is not None:
            parent = parent.parent
        mode = tl.skim_mode[sig.idx(child.ta)]
        ic = sig.idx(child.ta)
        if (mode != "none" and not child.children and child.parent is None and child is not parent
                and parent.height > child.height
                and SK.should_skim(parent, child, sig, child.t0, tl.tail_ratio[ic], tl.valley_ratio[ic])):
            limit = child.t1
            result = None
            if mode in ("exponential", "auto"):
                ex = SK.exponential_tail(sig, parent, child.t0, child.ta, limit, width)
                if ex and (mode == "exponential" or ex[2] >= method.exp_skim_r2):
                    result = (ex[0], ex[1], "X")
            if result is None and mode in ("tangent", "auto", "exponential"):
                tg = SK.tangent_tail(sig, child.t0, child.ta, limit)
                if tg:
                    result = (tg[0], tg[1], "T")
            if result is not None:
                child.t1, child.base, flag = result
                child.add_flag(flag)
                child.parent = parent
                parent.children.append(child)
                parent.t1 = max(parent.t1, child.t1)
                parent.te = "B" if child.te == "B" else parent.te
                chain.pop(i)
                continue
        i += 1
    # front riders
    i = len(chain) - 2
    while i >= 0 and len(chain) > 1:
        child = chain[i]
        parent = chain[i + 1]
        mode = tl.skim_mode[sig.idx(child.ta)]
        ic = sig.idx(child.ta)
        if (mode != "none" and not child.children and child.parent is None
                and parent.height > child.height
                and SK.should_skim(parent, child, sig, child.t1, tl.front_ratio[ic], tl.valley_ratio[ic])):
            tg = SK.tangent_front(sig, child.t1, child.ta, child.t0)
            if tg:
                child.t0, child.base = tg
                child.add_flag("T")
                child.parent = parent
                parent.children.append(child)
                parent.t0 = min(parent.t0, child.t0)
                chain.pop(i)
        i -= 1
    return chain


def _area_sums(sig: WorkSignal, peaks: list[WP], tl: Timeline) -> list[WP]:
    for lo, hi in tl.area_sum:
        hits = sorted((p for p in peaks if lo <= p.ta <= hi), key=lambda p: p.ta)
        if len(hits) < 2:
            continue
        first, last = hits[0], hits[-1]
        b0 = float(first.base.eval([first.t0])[0])
        b1 = float(last.base.eval([last.t1])[0])
        m = WP(first.t0, last.t1, first.ta, Baseline("line", first.t0, b0, last.t1, b1),
               ts=first.ts, te=last.te, flags="+", origin="auto")
        for h in hits:
            m.children.extend(h.children)
            peaks.remove(h)
        for c in m.children:
            c.parent = m
        m.ta = apex_of(sig, m.t0, m.t1, m.base)
        peaks.append(m)
    return peaks


def _negative(sig: WorkSignal, positive: list[WP], tl: Timeline, res: Resolved,
              n_up, n_dn, slope) -> list[WP]:
    if not tl.negative.any():
        return []
    on = tl.on & tl.negative
    clusters = detect(-sig.ys, -sig.d1, slope, on, n_up, n_dn)
    out = []
    for cl in clusters:
        t0, t1 = float(sig.rt[cl[0].i0]), float(sig.rt[cl[-1].i1])
        if any(p.region_start < t1 and t0 < p.region_end for p in positive):
            continue
        base = _cluster_line(sig, t0, t1)
        # a genuine negative peak dips below both of its baseline points (the
        # falling tail of a positive peak does not)
        bottom = float(sig.ys[sig.idx(t0):sig.idx(t1) + 1].min())
        if bottom > min(sig.at(t0), sig.at(t1)) - res.threshold:
            continue
        for s in cl:
            p = WP(float(sig.rt[s.i0]), float(sig.rt[s.i1]), float(sig.rt[s.ia]), replace(base),
                   ts=s.ts, te=s.te, flags="N", negative=True)
            out.append(p)
    _heights(sig, out)
    return out


def _measure(sig: WorkSignal, p: WP, factor: float, noise_pp: float) -> dict:
    area = MS.raw_area(sig, p.t0, p.t1, p.base, p.negative)
    for c in p.children:
        area -= MS.raw_area(sig, c.t0, c.t1, c.base, c.negative)
    if p.allocated_area_raw is not None:
        area = p.allocated_area_raw
    reported_area = area * factor
    if p.area_allocation is not None:
        from gcws.integration.deconv_split import share_exactly
        total, weights, index = p.area_allocation
        reported_area = share_exactly(total * factor, weights)[index]
    shp = MS.shape(sig, p.t0, p.t1 if not p.children else p.t1, p.base, p.negative)
    p.area_raw = area
    p.height = shp["height"]
    sn = 2.0 * shp["height"] / noise_pp if noise_pp > 0 else None
    return {**shp, "area_raw": area, "area": reported_area, "sn": sn}


def _accept(p: WP, m: dict, tl: Timeline, sig: WorkSignal, res: Resolved, method: IntegrationMethod) -> bool:
    if p.manual:
        return True
    i = sig.idx(p.ta)
    # a rise whose maximum sits on a baseline bound is a baseline step, not a
    # peak (fragments ending at a drop line or shoulder are fine)
    if not p.children and not any(f in p.flags for f in "FRfr"):
        if i <= sig.idx(p.t0) and p.ts in "BPH":
            return False
        if i >= sig.idx(p.t1) and p.te in "BPH":
            return False
    if m["height"] < max(tl.threshold[i], tl.height_reject[i]):
        return False
    if m["area"] < tl.area_reject[i] or m["area"] <= 0:
        return False
    if tl.min_sn[i] and (m["sn"] or 0) < tl.min_sn[i]:
        return False
    if m["width50"] and m["width50"] < method.min_width_fraction * tl.peak_width[i]:
        return False
    return True


# -- main entry --------------------------------------------------------------

def integrate(signal: Signal, method: IntegrationMethod,
              manual_events: Iterable[ManualEvent] = (), t_min=None) -> IntegrationResult:
    sig, res, tl = prepare(signal, method, t_min)
    rt = sig.rt
    step = float(np.median(np.diff(rt))) if rt.size > 1 else 1.0
    pw_pts = tl.peak_width / step
    n_up = np.maximum(2, np.round(pw_pts / UP_DIV)).astype(int)
    n_dn = np.maximum(2, np.round(pw_pts / DN_DIV)).astype(int)
    slope = tl.slope_mult * res.sigma_d1
    width = res.peak_width
    tol = max(3.0 * res.noise.sigma, 0.5 * res.threshold)

    # 4. detection (with baseline tracking)
    at_base = None
    if method.baseline_tracking:
        win = method.baseline_window or max(1.0, 40.0 * width)
        env = envelope(rt, sig.ys, win)
        btol = method.baseline_tolerance if method.baseline_tolerance is not None else 2.0 * tl.threshold
        at_base = (sig.ys - env) <= btol
        sig.env = env
    negatives = _negative(sig, [], tl, res, n_up, n_dn, slope)
    on_pos = tl.on.copy()
    for q in negatives:
        on_pos[sig.idx(q.t0):sig.idx(q.t1) + 1] = False
    clusters_idx = detect(sig.ys, sig.d1, slope, on_pos, n_up, n_dn, at_base)
    depth = method.min_valley_depth
    if depth is None:
        depth = VALLEY_DEPTH_MULT * res.threshold
    clusters_idx = [_merge_shallow(cl, sig.ys, depth) for cl in clusters_idx]
    clusters: list[list[WP]] = []
    for cl in clusters_idx:
        t0, t1 = float(rt[cl[0].i0]), float(rt[cl[-1].i1])
        if t1 <= t0:
            continue
        base = _cluster_line(sig, t0, t1)
        wps = [WP(float(rt[s.i0]), float(rt[s.i1]), float(rt[s.ia]), replace(base), ts=s.ts, te=s.te)
               for s in cl if s.i1 > s.i0]
        if wps:
            clusters.append(wps)

    # 5. timed baseline events
    for t in tl.split:
        for cl in clusters:
            for p in cl:
                if p.t0 < t < p.t1:
                    q = WP(t, p.t1, p.ta, replace(p.base), ts="V", te=p.te)
                    p.t1, p.te = t, "V"
                    cl.insert(cl.index(p) + 1, q)
                    break
    for t in list(tl.baseline_now) + [None] * 0:
        new = []
        for cl in clusters:
            if cl[0].t0 < t < cl[-1].t1:
                new.extend(x for x in _split_at(sig, cl, t, "B") if x)
            else:
                new.append(cl)
        clusters = new
    for t in tl.next_valley:
        new = []
        done = False
        for cl in clusters:
            if not done:
                vs = [p.t1 for p in cl[:-1] if p.t1 >= t]
                if vs:
                    new.extend(x for x in _split_at(sig, cl, vs[0], "B") if x)
                    done = True
                    continue
            new.append(cl)
        clusters = new

    # 6. baselines
    fixed: list[list[WP]] = []
    for cl in clusters:
        fixed.extend(_correct_penetration(sig, cl, tol))
    clusters = sorted(fixed, key=lambda c: c[0].t0)
    for cl in clusters:
        _valley_baselines(sig, cl, tl, tol)
        i0 = sig.idx(cl[0].t0)
        if tl.hold[i0]:
            starts = [h for h in tl.hold_starts if h <= cl[0].t0]
            level = sig.at(starts[-1]) if starts else sig.at(cl[0].t0)
            _assign_base(cl, Baseline("hold", cl[0].t0, level, cl[-1].t1, level))
            for p in cl:
                p.ts = p.ts if p.ts == "V" else "H"
    for t in tl.backward:
        after = [cl for cl in clusters if cl[0].t0 >= t]
        if after:
            cl = after[0]
            level = sig.at(cl[-1].t1)
            _assign_base(cl, Baseline("hold", cl[0].t0, level, cl[-1].t1, level))
            cl[0].ts = "H"

    # 7. solvent, shoulders, skims, sums
    for cl in clusters:
        _heights(sig, cl)
    all_h = [p.height for cl in clusters for p in cl if p.height > 0]
    med = float(np.median(all_h)) if all_h else 0.0
    for cl in clusters:
        for p in cl:
            if tl.solvent[sig.idx(p.ta)] or (method.solvent_height_factor > 0 and med > 0
                                              and p.height > method.solvent_height_factor * med):
                p.add_flag("S")
    peaks: list[WP] = []
    for cid, cl in enumerate(clusters):
        cl = _shoulders(sig, cl, tl, res)
        cl = _skims(sig, cl, tl, method, width)
        for p in cl:
            p.cluster = cid
        peaks.extend(cl)
    peaks = _area_sums(sig, peaks, tl)
    peaks.extend(q for q in negatives
                 if not any(p.region_start < q.t1 and q.t0 < p.region_end for p in peaks))

    # 9. measurement and rejects
    factor = method.area_unit_factor
    pp = res.noise.pp
    kept = []
    for p in peaks:
        m = _measure(sig, p, factor, pp)
        p.children = [c for c in p.children if _accept(c, _measure(sig, c, factor, pp), tl, sig, res, method)]
        if _accept(p, m, tl, sig, res, method):
            kept.append(p)
    peaks = kept

    # 10. manual events
    events = list(manual_events)
    unresolved: list[tuple[str, str]] = []
    if events:
        peaks, unresolved = MAN.apply(peaks, sig, events, width)

    if t_min is not None:
        peaks = [p for p in peaks if p.ta >= t_min]
        for p in peaks:
            p.t0 = max(p.t0, t_min)
            p.children = [c for c in p.children if c.ta >= t_min]
            for c in p.children:
                c.t0 = max(c.t0, t_min)
    return _finalise(sig, peaks, res, method, unresolved)


def _finalise(sig: WorkSignal, peaks: list[WP], res: Resolved, method: IntegrationMethod,
              unresolved) -> IntegrationResult:
    factor = method.area_unit_factor
    flat: list[tuple[WP, dict]] = []
    for p in peaks:
        m = _measure(sig, p, factor, res.noise.pp)
        flat.append((p, m))
        for c in p.children:
            flat.append((c, _measure(sig, c, factor, res.noise.pp)))
    flat.sort(key=lambda x: x[0].ta)
    index = {id(p): i for i, (p, _) in enumerate(flat)}
    total = sum(m["area"] for p, m in flat if not p.negative and "S" not in p.flags and m["area"] > 0)
    out: list[Peak] = []
    for i, (p, m) in enumerate(flat):
        pk = Peak(start=p.t0, end=p.t1, apex_rt=p.ta, baseline=p.base, area=m["area"],
                  area_raw=m["area_raw"], height=m["height"], width50=m["width50"],
                  width5=m["width5"], symmetry=m["symmetry"], asymmetry=m["asymmetry"],
                  sn=m["sn"], type_start=p.ts, type_end=p.te, flags=p.flags, origin=p.origin,
                  parent=index.get(id(p.parent)) if p.parent is not None else None,
                  number=i + 1, negative=p.negative)
        pk.area_pct = (100.0 * pk.area / total) if total > 0 and not p.negative and "S" not in p.flags else 0.0
        pk.extra["cluster"] = p.cluster
        if p.spectrum_id:
            pk.extra["spectrum_id"] = p.spectrum_id
        if p.deconv_component:
            from gcws.core.keys import base_key
            pk.extra["deconv_component"] = dict(p.deconv_component)
            pk.extra["area_note"] = (
                f"Modeled {base_key(p.deconv_component.get('signal_key', 'FID'))} area from MS deconvolution: "
                f"{p.deconv_component['weight']:.2%} "
                f"of the original peak; component {p.deconv_component['rt']:.4f} min (MS), "
                f"model m/z {p.deconv_component['model_mz']}. "
                "Allocation estimated from MS component proportions.")
        out.append(pk)
    h = hashlib.sha1()
    h.update(str(gcws.INTEGRATOR_VERSION).encode())
    for pk in out:
        h.update(f"{pk.start:.5f}|{pk.end:.5f}|{pk.apex_rt:.5f}|{pk.area:.6g}|{pk.type_code};".encode())
    return IntegrationResult(peaks=out, resolved=res, unresolved=unresolved,
                             digest=h.hexdigest()[:16], method_name=method.name)

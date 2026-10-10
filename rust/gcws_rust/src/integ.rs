//! Kernels of the chromatogram integrator (`gcws.integration.rust_integration` uses them).
//!
//! * `detect`: the slope detector's state machine (`gcws.integration.detector.detect`).
//! * `width_candidates`: the loop of `autoparams.measure_width` (height and FWHM of each local
//!   maximum that is high enough and well defined).
//! * `raw_area` / `shape`: `gcws.integration.measure` for straight-line and horizontal baselines.
//! * `nearest_index`: `WorkSignal.idx`.
//!
//! Every result equals the Python one bit for bit: the same float operations in the same order,
//! numpy's `interp` (2-point and full-array) and the pairwise summation of `ndarray.sum`.

use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// -- numpy arithmetic, reproduced -------------------------------------------

/// `np.interp(x, xp, fp)` for one `x` (numpy's `arr_interp`, default left / right).
pub fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let len = xp.len();
    let (lval, rval) = (fp[0], fp[len - 1]);
    if x.is_nan() {
        return x;
    }
    if len == 1 {
        return if x < xp[0] { lval } else if x > xp[0] { rval } else { fp[0] };
    }
    // binary_search_with_guess: the last index j with xp[j] <= x (-1 below, len above)
    let j: isize = if x > xp[len - 1] {
        len as isize
    } else if x < xp[0] {
        -1
    } else if len <= 4 {
        let mut i = 1;
        while i < len && x >= xp[i] {
            i += 1;
        }
        i as isize - 1
    } else {
        xp.partition_point(|&v| x >= v) as isize - 1
    };
    if j == -1 {
        return lval;
    }
    if j == len as isize {
        return rval;
    }
    let j = j as usize;
    if j == len - 1 {
        return fp[j];
    }
    if xp[j] == x {
        return fp[j];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut r = slope * (x - xp[j]) + fp[j];
    if r.is_nan() {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if r.is_nan() && fp[j] == fp[j + 1] {
            r = fp[j];
        }
    }
    r
}

/// `np.interp(x, [x0, x1], [f0, f1])`.
#[inline]
pub fn interp2(x: f64, x0: f64, x1: f64, f0: f64, f1: f64) -> f64 {
    interp(x, &[x0, x1], &[f0, f1])
}

/// numpy's pairwise summation (`DOUBLE_pairwise_sum`, blocks of 128, 8 accumulators).
pub fn pairwise_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0;
        for &v in a {
            res += v;
        }
        res
    } else if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8;
        while i < n - n % 8 {
            for k in 0..8 {
                r[k] += a[i + k];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
    }
}

/// `ndarray.sum()` of a contiguous 1-d float64 array: one pairwise sum over all of it.
pub fn np_sum(a: &[f64]) -> f64 {
    pairwise_sum(a)
}

/// `ndarray.min()` (NaN propagates).
fn np_min(a: &[f64]) -> f64 {
    let mut m = a[0];
    for &v in &a[1..] {
        if v.is_nan() {
            return v;
        }
        if v < m {
            m = v;
        }
    }
    m
}

/// `ndarray.argmax()` (first maximum; the first NaN wins).
fn np_argmax(a: &[f64]) -> usize {
    let mut k = 0;
    if a[0].is_nan() {
        return 0;
    }
    for (i, &v) in a.iter().enumerate().skip(1) {
        if v.is_nan() {
            return i;
        }
        if v > a[k] {
            k = i;
        }
    }
    k
}

// -- detector -----------------------------------------------------------------

type Seg = (usize, usize, usize, &'static str, &'static str);

/// `max(range(lo, hi), key=ys.__getitem__)`: the first maximum.
fn arg_first_max(ys: &[f64], lo: usize, hi: usize) -> usize {
    let mut k = lo;
    for i in lo + 1..hi {
        if ys[i] > ys[k] {
            k = i;
        }
    }
    k
}

/// `min(range(lo, hi), key=ys.__getitem__)`: the first minimum.
fn arg_first_min(ys: &[f64], lo: usize, hi: usize) -> usize {
    let mut k = lo;
    for i in lo + 1..hi {
        if ys[i] < ys[k] {
            k = i;
        }
    }
    k
}

/// The state machine of `detector.detect`; clusters of (i0, ia, i1, ts, te).
#[allow(clippy::too_many_arguments)]
fn detect_impl(ys: &[f64], d: &[f64], s_: &[f64], on_: &[bool], nu: &[i64], nd: &[i64],
               ab: Option<&[bool]>) -> Vec<Vec<Seg>> {
    let n = ys.len();
    let ab_at = |i: usize| ab.map_or(true, |a| a[i]);
    let mut clusters: Vec<Vec<Seg>> = Vec::new();
    let mut cur: Vec<Seg> = Vec::new();
    let mut state = 0u8;
    let (mut up, mut dn, mut flat): (i64, i64, i64) = (0, 0, 0);
    let (mut start, mut apex) = (0usize, 0usize);
    let mut last_end: i64 = 0;
    let mut i = 0usize;
    while i < n {
        if !on_[i] {
            if state != 0 {
                let end = i;
                let a = if state == 1 { arg_first_max(ys, start, end + 1) } else { apex };
                let ts = if cur.is_empty() { "B" } else { "V" };
                cur.push((start, a, end.max(a + 1), ts, "B"));
                if cur.len() > 1 {
                    cur.last_mut().unwrap().3 = "V";
                }
                if !cur.is_empty() {
                    clusters.push(std::mem::take(&mut cur));
                }
                last_end = end as i64;
                state = 0;
            }
            up = 0;
            i += 1;
            continue;
        }
        let s = s_[i];
        let di = d[i];
        if state == 0 {
            up = if di > s { up + 1 } else { 0 };
            if up >= nu[i] {
                let mut st = i as i64 - up + 1;
                let floor = last_end.max(st - 4 * nd[i] - 4 * nu[i]);
                while st > floor && st > 0 && d[(st - 1) as usize] > 0.0 && on_[(st - 1) as usize] {
                    st -= 1;
                }
                start = st as usize;
                state = 1;
                up = 0;
            }
        } else if state == 1 {
            if di <= 0.0 {
                apex = arg_first_max(ys, start, i + 1);
                state = 2;
                dn = 0;
                flat = 0;
            }
        } else {
            dn = if di > s { dn + 1 } else { 0 };
            if dn >= nu[i] {
                let seg_lo = apex as i64;
                let seg_hi = i as i64 - dn + 1;
                let hi = seg_hi.max(seg_lo + 1) + 1;
                let valley = arg_first_min(ys, seg_lo as usize, hi as usize);
                let ts = if cur.is_empty() { "B" } else { "V" };
                cur.push((start, apex, valley, ts, "V"));
                start = valley;
                state = 1;
                dn = 0;
                i += 1;
                continue;
            }
            flat = if di.abs() <= s { flat + 1 } else { 0 };
            if flat >= nd[i] && ab_at(i) {
                let end = (i as i64 - nd[i] + 1).max(i as i64 - flat + 1);
                let ts = if cur.is_empty() { "B" } else { "V" };
                cur.push((start, apex, end.max(apex as i64 + 1) as usize, ts, "B"));
                if !cur.is_empty() {
                    clusters.push(std::mem::take(&mut cur));
                }
                last_end = end;
                state = 0;
                flat = 0;
            }
        }
        i += 1;
    }
    if state != 0 {
        let end = n - 1;
        let a = if state == 2 { apex } else { arg_first_max(ys, start, n) };
        let ts = if cur.is_empty() { "B" } else { "V" };
        cur.push((start, a, end.max(a), ts, "B"));
    }
    if !cur.is_empty() {
        clusters.push(cur);
    }
    clusters
}

/// detect(ys, d1, slope, on, n_up, n_dn, at_base=None) -> [[(i0, ia, i1, ts, te)]]
#[pyfunction]
#[pyo3(signature = (ys, d1, slope, on, n_up, n_dn, at_base=None))]
#[allow(clippy::too_many_arguments)]
pub fn detect(py: Python<'_>, ys: PyReadonlyArray1<f64>, d1: PyReadonlyArray1<f64>,
              slope: PyReadonlyArray1<f64>, on: PyReadonlyArray1<bool>, n_up: PyReadonlyArray1<i64>,
              n_dn: PyReadonlyArray1<i64>, at_base: Option<PyReadonlyArray1<bool>>) -> PyResult<Vec<Vec<Seg>>> {
    let (ys, d, s, o, nu, nd) = (ys.as_slice()?, d1.as_slice()?, slope.as_slice()?, on.as_slice()?,
                                 n_up.as_slice()?, n_dn.as_slice()?);
    let n = ys.len();
    let ab = match &at_base {
        Some(a) => Some(a.as_slice()?),
        None => None,
    };
    if [d.len(), s.len(), o.len(), nu.len(), nd.len()].iter().any(|&l| l != n) || ab.is_some_and(|a| a.len() != n) {
        return Err(PyValueError::new_err("detect: arrays of different lengths"));
    }
    Ok(py.detach(|| detect_impl(ys, d, s, o, nu, nd, ab)))
}

// -- measure_width ------------------------------------------------------------

/// The (height, FWHM in minutes) candidates of `measure_width` over the smoothed segment `seg`.
#[pyfunction]
pub fn width_candidates(py: Python<'_>, seg: PyReadonlyArray1<f64>, win: usize, min_height: f64,
                        step: f64) -> PyResult<Vec<(f64, f64)>> {
    let seg = seg.as_slice()?;
    Ok(py.detach(|| {
        let n = seg.len();
        let mut out = Vec::new();
        if n < 3 {
            return out;
        }
        for m in 1..n - 1 {
            if !(seg[m] > seg[m - 1] && seg[m] >= seg[m + 1]) {
                continue;
            }
            let a = m.saturating_sub(win);
            let b = (m + win + 1).min(n);
            let l = np_min(&seg[a..m + 1]);
            let r = np_min(&seg[m..b]);
            let base = if r > l { r } else { l };
            let h = seg[m] - base;
            if h < min_height {
                continue;
            }
            let half = base + h / 2.0;
            let mut left = m;
            while left > a && seg[left] > half {
                left -= 1;
            }
            let mut right = m;
            while right < b - 1 && seg[right] > half {
                right += 1;
            }
            if seg[left] > half || seg[right] > half {
                continue;
            }
            let tl = interp2(half, seg[left], seg[left + 1], left as f64, (left + 1) as f64);
            let tr = interp2(half, seg[right], seg[right - 1], right as f64, (right - 1) as f64);
            let w = (tr - tl) * step;
            if w > 0.0 {
                out.push((h, w));
            }
        }
        out
    }))
}

// -- measure ------------------------------------------------------------------

/// A straight-line (`line`) or horizontal (`hold`) baseline, as `Baseline.eval` computes it.
#[derive(Clone, Copy)]
struct Line {
    hold: bool,
    t0: f64,
    y0: f64,
    t1: f64,
    y1: f64,
}

impl Line {
    #[inline]
    fn eval(&self, t: f64) -> f64 {
        if self.hold || self.t1 == self.t0 {
            self.y0
        } else {
            self.y0 + (self.y1 - self.y0) * (t - self.t0) / (self.t1 - self.t0)
        }
    }
}

/// `measure.profile` minus the baseline (negated for negative peaks): (t, d).
fn profile_dev(rt: &[f64], y: &[f64], mut t0: f64, mut t1: f64, base: &Line, negative: bool) -> (Vec<f64>, Vec<f64>) {
    if t1 < t0 {
        std::mem::swap(&mut t0, &mut t1);
    }
    let a = rt.partition_point(|&v| v <= t0); // searchsorted side="right"
    let b = rt.partition_point(|&v| v < t1); // side="left"
    let mut t = Vec::with_capacity(b.saturating_sub(a) + 2);
    let mut v = Vec::with_capacity(t.capacity());
    t.push(t0);
    v.push(interp(t0, rt, y));
    if b > a {
        t.extend_from_slice(&rt[a..b]);
        v.extend_from_slice(&y[a..b]);
    }
    t.push(t1);
    v.push(interp(t1, rt, y));
    let d = t.iter().zip(&v).map(|(&ti, &vi)| {
        let e = vi - base.eval(ti);
        if negative { -e } else { e }
    }).collect();
    (t, d)
}

fn line_of(kind: &str, t0: f64, y0: f64, t1: f64, y1: f64) -> PyResult<Line> {
    match kind {
        "line" => Ok(Line { hold: false, t0, y0, t1, y1 }),
        "hold" => Ok(Line { hold: true, t0, y0, t1, y1 }),
        _ => Err(PyValueError::new_err("only line and hold baselines")),
    }
}

/// `measure.raw_area`: trapezoid over the raw profile, x in seconds.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn raw_area(rt: PyReadonlyArray1<f64>, y: PyReadonlyArray1<f64>, t0: f64, t1: f64, kind: &str,
                bt0: f64, by0: f64, bt1: f64, by1: f64, negative: bool) -> PyResult<f64> {
    let base = line_of(kind, bt0, by0, bt1, by1)?;
    let (rt, y) = (rt.as_slice()?, y.as_slice()?);
    let (t, d) = profile_dev(rt, y, t0, t1, &base, negative);
    let x: Vec<f64> = t.iter().map(|&v| v * 60.0).collect();
    let terms: Vec<f64> = (0..d.len() - 1).map(|i| (x[i + 1] - x[i]) * (d[i + 1] + d[i]) / 2.0).collect();
    Ok(np_sum(&terms))
}

/// `measure._crossing`.
fn crossing(t: &[f64], d: &[f64], level: f64, i_apex: usize, direction: i32) -> Option<f64> {
    let mut i = i_apex;
    if direction < 0 {
        while i > 0 && d[i] > level {
            i -= 1;
        }
        if d[i] > level {
            return None;
        }
        return Some(interp2(level, d[i], d[i + 1], t[i], t[i + 1]));
    }
    while i < d.len() - 1 && d[i] > level {
        i += 1;
    }
    if d[i] > level {
        return None;
    }
    Some(interp2(level, d[i], d[i - 1], t[i], t[i - 1]))
}

/// `measure.shape`: (height, width50, width5, symmetry, asymmetry).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn shape(rt: PyReadonlyArray1<f64>, y: PyReadonlyArray1<f64>, t0: f64, t1: f64, kind: &str,
             bt0: f64, by0: f64, bt1: f64, by1: f64, negative: bool)
             -> PyResult<(f64, f64, f64, Option<f64>, Option<f64>)> {
    let base = line_of(kind, bt0, by0, bt1, by1)?;
    let (rt, y) = (rt.as_slice()?, y.as_slice()?);
    let (t, d) = profile_dev(rt, y, t0, t1, &base, negative);
    if d.len() < 3 {
        // the profile always has its two end points: height = d.max()
        return Ok((d[np_argmax(&d)], 0.0, 0.0, None, None));
    }
    let k = np_argmax(&d);
    let h = d[k];
    if h <= 0.0 {
        return Ok((h, 0.0, 0.0, None, None));
    }
    let ta = t[k];
    let (mut w50, mut w5, mut sym, mut asym) = (0.0, 0.0, None, None);
    if let (Some(l), Some(r)) = (crossing(&t, &d, 0.5 * h, k, -1), crossing(&t, &d, 0.5 * h, k, 1)) {
        w50 = r - l;
    }
    if let (Some(l), Some(r)) = (crossing(&t, &d, 0.05 * h, k, -1), crossing(&t, &d, 0.05 * h, k, 1)) {
        w5 = r - l;
        let f = ta - l;
        if f > 0.0 {
            sym = Some((r - l) / (2.0 * f));
        }
    }
    if let (Some(l), Some(r)) = (crossing(&t, &d, 0.10 * h, k, -1), crossing(&t, &d, 0.10 * h, k, 1)) {
        if ta - l > 0.0 {
            asym = Some((r - ta) / (ta - l));
        }
    }
    Ok((h, w50, w5, sym, asym))
}

/// `WorkSignal.idx`: the index of the sample nearest to `t` (the earlier one on a tie).
#[pyfunction]
pub fn nearest_index(rt: PyReadonlyArray1<f64>, t: f64) -> PyResult<usize> {
    let rt = rt.as_slice()?;
    let n = rt.len();
    let j = rt.partition_point(|&v| v < t); // searchsorted side="left"
    if j == 0 {
        return Ok(0);
    }
    if j >= n {
        return Ok(n - 1);
    }
    Ok(if (rt[j] - t).abs() < (rt[j - 1] - t).abs() { j } else { j - 1 })
}

/// `np.interp` and the pairwise `sum`, exposed for the tests.
#[pyfunction]
pub fn np_interp(x: f64, xp: PyReadonlyArray1<f64>, fp: PyReadonlyArray1<f64>) -> PyResult<f64> {
    Ok(interp(x, xp.as_slice()?, fp.as_slice()?))
}

#[pyfunction]
pub fn np_sum_f64(a: PyReadonlyArray1<f64>) -> PyResult<f64> {
    Ok(np_sum(a.as_slice()?))
}

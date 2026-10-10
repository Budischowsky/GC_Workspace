//! Kernels of the array deconvolution engine (`gcws.ms.rust_deconv` uses them).
//!
//! * `perceive_ions`: the ion-peak perception of `deconv_fast._perceive_ions` after smoothing
//!   (maxima, prominence gates, flanking minima, sub-scan apex, half-height width, score).
//! * `perceive_components`: `deconv_fast._perceive_components` (seed blocks, links, merging).
//! * `link_r`: the Pearson r of `deconv_fast._links` over the seed's slice, on the slices padded to
//!   a common width as numpy computes them (pairwise row sums, `einsum` row dot products).
//!
//! Both equal the numpy results bit for bit: element-wise operations in numpy's order, numpy's
//! pairwise summation and the summation order of numpy's `einsum` on its x86-64 baseline (two
//! float64 lanes, multiply then add, blocks of eight taken last to first).

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

use crate::integ::pairwise_sum;

/// `np.einsum("i,i->", a, b)` for contiguous float64 vectors (numpy 2's `sum_of_products`
/// `contig_contig_outstride0_two` with two lanes and no fused multiply-add).
pub fn einsum_dot(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len();
    let mut acc = [0.0f64; 2];
    let mut i = 0;
    while n - i >= 8 {
        for (l, v) in acc.iter_mut().enumerate() {
            let mut x = *v;
            for blk in [3usize, 2, 1, 0] {
                let k = i + blk * 2 + l;
                x = a[k] * b[k] + x;
            }
            *v = x;
        }
        i += 8;
    }
    while i < n {
        for (l, v) in acc.iter_mut().enumerate() {
            let k = i + l;
            if k < n {
                *v = a[k] * b[k] + *v;
            }
        }
        i += 2;
    }
    0.0 + (acc[0] + acc[1])
}

type IonArrays<'py> = (
    Bound<'py, PyArray1<i64>>,   // row
    Bound<'py, PyArray1<i64>>,   // apex
    Bound<'py, PyArray1<f64>>,   // apex_sub
    Bound<'py, PyArray1<f64>>,   // base
    Bound<'py, PyArray1<f64>>,   // width_half
    Bound<'py, PyArray1<f64>>,   // s_n
    Bound<'py, PyArray1<f64>>,   // score
    Bound<'py, PyArray1<i64>>,   // left
    Bound<'py, PyArray1<i64>>,   // right
);

#[derive(Default)]
struct Ions {
    row: Vec<i64>,
    apex: Vec<i64>,
    apex_sub: Vec<f64>,
    base: Vec<f64>,
    width: Vec<f64>,
    s_n: Vec<f64>,
    score: Vec<f64>,
    left: Vec<i64>,
    right: Vec<i64>,
}

/// One row's peaks (rows are independent).
fn perceive_row(rr: usize, g: &[f64], sig: f64, noise_factor: f64, out: &mut Ions) {
    let n = g.len();
    if n < 3 {
        return;
    }
    let mut smin = g[0];
    for &v in &g[1..] {
        if v.is_nan() {
            smin = v;
            break;
        }
        if v < smin {
            smin = v;
        }
    }
    let threshold = noise_factor * sig;
    for i in 1..n - 1 {
        let h = g[i];
        if !(h > g[i - 1] && h >= g[i + 1]) {
            continue;
        }
        if (h - smin) < threshold {
            continue;
        }
        // flanking minima: walk out to the first higher point; the minimum nearest the apex wins
        let stop_l: isize = (0..i).rev().find(|&j| g[j] > h).map_or(-1, |j| j as isize);
        let mut lo_v = f64::INFINITY;
        for j in (stop_l + 1) as usize..i {
            if g[j] < lo_v {
                lo_v = g[j];
            }
        }
        // left = the last index whose masked value (inf outside the flank) equals lo_v: with an
        // empty flank every masked entry matches and numpy's reversed argmax gives n - 1
        let left: usize = if lo_v == f64::INFINITY {
            n - 1
        } else {
            ((stop_l + 1) as usize..i).rev().find(|&j| g[j] == lo_v).unwrap()
        };
        let stop_r = (i + 1..n).find(|&j| g[j] > h).unwrap_or(n);
        let mut min_r = f64::INFINITY;
        for &v in &g[i + 1..stop_r] {
            if v < min_r {
                min_r = v;
            }
        }
        let lower = min_r < h;
        let hi_v = if lower { min_r } else { h };
        let right = if lower { (i + 1..stop_r).find(|&j| g[j] == min_r).unwrap() } else { i };
        let base = if lo_v.is_nan() || hi_v.is_nan() { f64::NAN } else { lo_v.max(hi_v) };
        let prominence = h - base;
        if prominence < threshold {
            continue;
        }
        // sub-scan apex and curvature
        let (before_v, after_v) = (g[i - 1], g[i + 1]);
        let den = before_v - 2.0 * h + after_v;
        let mut shift = if den == 0.0 { 0.0 } else { 0.5 * (before_v - after_v) / den };
        shift = np_minimum(0.5, np_maximum(-0.5, shift));
        let curvature = 2.0 * h - before_v - after_v;
        // half-height width on the smoothed trace
        let level = base + 0.5 * prominence;
        let jl_found = (left..i).rev().find(|&j| g[j] <= level);
        let (has_l, jl) = match jl_found {
            Some(j) => (true, j),
            None => (false, 0),
        };
        let jl1 = (jl + 1).min(n - 1);
        let span_l = g[jl1] - g[jl];
        let jr_found = (i + 1..(right + 1).min(n)).find(|&j| g[j] <= level);
        let (has_r, jr) = match jr_found {
            Some(j) => (true, j),
            None => (false, 1),
        };
        let span_r = g[jr - 1] - g[jr];
        let lo_w = if has_l {
            if span_l > 0.0 { jl as f64 + (level - g[jl]) / span_l } else { jl1 as f64 }
        } else {
            left as f64
        };
        let hi_w = if has_r {
            if span_r > 0.0 { jr as f64 - (level - g[jr]) / span_r } else { (jr - 1) as f64 }
        } else {
            right as f64
        };
        let width = np_maximum(hi_w - lo_w, 1e-6);
        let s_n = prominence / sig;
        out.row.push(rr as i64);
        out.apex.push(i as i64);
        out.apex_sub.push(i as f64 + shift);
        out.base.push(base);
        out.width.push(width);
        out.s_n.push(s_n);
        out.score.push((np_maximum(curvature, 0.0) / prominence) * s_n);
        out.left.push(left as i64);
        out.right.push(right as i64);
    }
}

/// `np.maximum` (NaN propagates).
#[inline]
fn np_maximum(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if a >= b { a } else { b }
}

/// `np.minimum` (NaN propagates).
#[inline]
fn np_minimum(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if a <= b { a } else { b }
}

/// The perceived ion peaks of the smoothed traces `s` (one row per ion, `sigma` per row).
/// Returns (row, apex, apex_sub, base, width_half, s_n, score, left, right), rows ascending and
/// apexes ascending within a row (np.nonzero order); empty arrays when no peak survives.
#[pyfunction]
pub fn perceive_ions<'py>(py: Python<'py>, s: PyReadonlyArray2<'py, f64>, sigma: PyReadonlyArray1<'py, f64>,
                          noise_factor: f64) -> PyResult<IonArrays<'py>> {
    let shape = s.shape();
    let (k, n) = (shape[0], shape[1]);
    let s = s.as_slice()?;
    let sigma = sigma.as_slice()?;
    if sigma.len() != k {
        return Err(PyValueError::new_err("one sigma per row"));
    }
    let parts: Vec<Ions> = py.detach(|| {
        (0..k).into_par_iter().map(|rr| {
            let mut out = Ions::default();
            perceive_row(rr, &s[rr * n..(rr + 1) * n], sigma[rr], noise_factor, &mut out);
            out
        }).collect()
    });
    let mut all = Ions::default();
    for p in parts {
        all.row.extend(p.row);
        all.apex.extend(p.apex);
        all.apex_sub.extend(p.apex_sub);
        all.base.extend(p.base);
        all.width.extend(p.width);
        all.s_n.extend(p.s_n);
        all.score.extend(p.score);
        all.left.extend(p.left);
        all.right.extend(p.right);
    }
    Ok((all.row.into_pyarray(py), all.apex.into_pyarray(py), all.apex_sub.into_pyarray(py),
        all.base.into_pyarray(py), all.width.into_pyarray(py), all.s_n.into_pyarray(py),
        all.score.into_pyarray(py), all.left.into_pyarray(py), all.right.into_pyarray(py)))
}

/// The Pearson r of `_links` for one pair over the seed's slice `lo..hi`, laid out on the padded
/// width `sa.len()` (the scratch rows `sa`, `sb`).
#[allow(clippy::too_many_arguments)]
fn pearson(sm: &[f64], n: usize, seed_row: usize, cand_row: usize, lo: i64, hi: i64,
           sa: &mut [f64], sb: &mut [f64]) -> f64 {
    let width = sa.len();
    let length = hi - lo;
    let fill = |row: usize, out: &mut [f64]| {
        for (t, o) in out.iter_mut().enumerate() {
            let off = lo + t as i64;
            *o = if off < hi { sm[row * n + (off.min(n as i64 - 1)) as usize] } else { 0.0 };
        }
    };
    fill(seed_row, sa);
    fill(cand_row, sb);
    let (ma, mb) = (pairwise_sum(sa) / length as f64, pairwise_sum(sb) / length as f64);
    for t in 0..width {
        let inside = lo + (t as i64) < hi;
        sa[t] = if inside { sa[t] - ma } else { 0.0 };
        sb[t] = if inside { sb[t] - mb } else { 0.0 };
    }
    let na = einsum_dot(sa, sa).sqrt();
    let nb = einsum_dot(sb, sb).sqrt();
    let r = einsum_dot(sa, sb) / (na * nb);
    if na > 0.0 && nb > 0.0 { r } else { 0.0 }
}

/// numpy's sort order of float64 (NaN last; -0.0 equals 0.0).
#[inline]
fn np_cmp(a: f64, b: f64) -> std::cmp::Ordering {
    use std::cmp::Ordering::*;
    if a < b {
        Less
    } else if a > b {
        Greater
    } else if a.is_nan() && !b.is_nan() {
        Greater
    } else if b.is_nan() && !a.is_nan() {
        Less
    } else {
        Equal
    }
}

/// Python's comparison of floats in a sort key (`<` decides; NaN compares equal to everything).
#[inline]
fn py_cmp(a: f64, b: f64) -> std::cmp::Ordering {
    a.partial_cmp(&b).unwrap_or(std::cmp::Ordering::Equal)
}

/// The peaks a component perception reads (`deconv_fast._Peaks`).
struct PeakView<'a> {
    smooth: &'a [f64],
    n_smooth: usize,
    row: &'a [i64],
    mz: &'a [i64],
    apex: &'a [i64],
    apex_sub: &'a [f64],
    width_half: &'a [f64],
    score: &'a [f64],
}

/// Grouping constants: the window's DeconvParams and the engine's limits.
struct GroupParams {
    apex_tolerance: f64,
    shape_r: f64,
    min_ions: usize,
    corr_min_half: i64,
    min_separation: f64,
    max_components: usize,
}

/// `deconv_fast._perceive_components`: seed blocks of 8 doubling to 256, `_links` per block
/// against the peaks unused at the block's start, then `_merge_groups`.
fn perceive_components_impl(p: &PeakView, n: i64, gp: &GroupParams) -> Vec<Vec<usize>> {
    let count = p.mz.len();
    // order = lexsort((apex, mz, -score)): score descending, then m/z, then apex (stable)
    let mut order: Vec<usize> = (0..count).collect();
    order.sort_by(|&a, &b| {
        np_cmp(-p.score[a], -p.score[b]).then(p.mz[a].cmp(&p.mz[b])).then(p.apex[a].cmp(&p.apex[b]))
    });
    let mut rank = vec![0i64; count];
    for (r, &k) in order.iter().enumerate() {
        rank[k] = r as i64;
    }
    let mut lo = vec![0i64; count];
    let mut hi = vec![0i64; count];
    for k in 0..count {
        let half = gp.corr_min_half.max((1.5 * p.width_half[k]).round_ties_even() as i64);
        let (l, h) = (0i64.max(p.apex[k] - half), n.min(p.apex[k] + half + 1));
        if h - l < 3 {
            lo[k] = 0;
            hi[k] = n;
        } else {
            lo[k] = l;
            hi[k] = h;
        }
    }
    let mut by_apex: Vec<usize> = (0..count).collect();
    by_apex.sort_by(|&a, &b| np_cmp(p.apex_sub[a], p.apex_sub[b]));
    let sorted: Vec<f64> = by_apex.iter().map(|&k| p.apex_sub[k]).collect();
    let tol = gp.apex_tolerance;
    let first: Vec<usize> = (0..count).map(|k| {
        let v = p.apex_sub[k] - tol - 1e-9;
        sorted.partition_point(|&x| x < v)
    }).collect();
    let last: Vec<usize> = (0..count).map(|k| {
        let v = p.apex_sub[k] + tol + 1e-9;
        sorted.partition_point(|&x| x <= v)
    }).collect();

    let mut used = vec![false; count];
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let (mut pos, mut block) = (0usize, 8usize);
    let (mut sa, mut sb) = (Vec::new(), Vec::new());
    while pos < count {
        let mut seeds = Vec::new();
        while pos < count && seeds.len() < block {
            let k = order[pos];
            pos += 1;
            if !used[k] {
                seeds.push(k);
            }
        }
        block = (2 * block).min(256);
        if seeds.is_empty() {
            continue;
        }
        // _links: the pairs passing the rank, usage and apex tests, then r over the padded slices
        let mut pairs: Vec<(usize, usize)> = Vec::new();
        for &s in &seeds {
            for &c in &by_apex[first[s]..last[s].max(first[s])] {
                if rank[c] > rank[s] && !used[c] && !((p.apex_sub[c] - p.apex_sub[s]).abs() > tol) {
                    pairs.push((s, c));
                }
            }
        }
        let mut links: std::collections::HashMap<usize, Vec<usize>> = std::collections::HashMap::new();
        if !pairs.is_empty() {
            let width = pairs.iter().map(|&(s, _)| hi[s] - lo[s]).max().unwrap().max(0) as usize;
            sa.resize(width, 0.0);
            sb.resize(width, 0.0);
            let mut kept: Vec<(usize, usize)> = Vec::new();
            for &(s, c) in &pairs {
                let r = pearson(p.smooth, p.n_smooth, p.row[s] as usize, p.row[c] as usize, lo[s], hi[s],
                                &mut sa[..width], &mut sb[..width]);
                if !(r < gp.shape_r) {
                    kept.push((s, c));
                }
            }
            kept.sort_by(|&(s1, c1), &(s2, c2)| s1.cmp(&s2).then(rank[c1].cmp(&rank[c2])));
            for (s, c) in kept {
                links.entry(s).or_default().push(c);
            }
        }
        for &k in &seeds {
            if used[k] {
                continue;
            }
            used[k] = true;
            let mut group = vec![k];
            if let Some(js) = links.get(&k) {
                for &j in js {
                    if !used[j] {
                        used[j] = true;
                        group.push(j);
                    }
                }
            }
            groups.push(group);
        }
    }
    merge_groups(p, groups, gp)
}

/// `deconv_fast._merge_groups`.
fn merge_groups(p: &PeakView, groups: Vec<Vec<usize>>, gp: &GroupParams) -> Vec<Vec<usize>> {
    let distinct = |g: &Vec<usize>| {
        let mut m: Vec<i64> = g.iter().map(|&q| p.mz[q]).collect();
        m.sort_unstable();
        m.dedup();
        m.len()
    };
    let mut groups: Vec<Vec<usize>> = groups.into_iter().filter(|g| distinct(g) >= gp.min_ions).collect();
    let key_sub = |a: &Vec<usize>, b: &Vec<usize>| {
        let (x, y) = (a[0], b[0]);
        py_cmp(p.apex_sub[x], p.apex_sub[y]).then(py_cmp(-p.score[x], -p.score[y])).then(p.mz[x].cmp(&p.mz[y]))
    };
    groups.sort_by(key_sub);
    let mut merged: Vec<Vec<usize>> = Vec::new();
    for g in groups {
        if let Some(prev) = merged.last() {
            if (p.apex_sub[g[0]] - p.apex_sub[prev[0]]).abs() < gp.min_separation {
                let model = if p.score[prev[0]] >= p.score[g[0]] { prev[0] } else { g[0] };
                let mut next = vec![model];
                next.extend(prev.iter().chain(&g).copied().filter(|&q| q != model));
                *merged.last_mut().unwrap() = next;
                continue;
            }
        }
        merged.push(g);
    }
    if merged.len() > gp.max_components {
        merged.sort_by(|a, b| py_cmp(-p.score[a[0]], -p.score[b[0]]).then(p.mz[a[0]].cmp(&p.mz[b[0]])));
        merged.truncate(gp.max_components);
        merged.sort_by(key_sub);
    }
    merged
}

/// `deconv_fast._perceive_components(p, n, params)`: the components as lists of peak indices.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn perceive_components<'py>(py: Python<'py>, smooth: PyReadonlyArray2<'py, f64>, row: PyReadonlyArray1<'py, i64>,
                                mz: PyReadonlyArray1<'py, i64>, apex: PyReadonlyArray1<'py, i64>,
                                apex_sub: PyReadonlyArray1<'py, f64>, width_half: PyReadonlyArray1<'py, f64>,
                                score: PyReadonlyArray1<'py, f64>, n: i64, apex_tolerance: f64, shape_r: f64,
                                min_ions: usize, corr_min_half: i64, min_separation: f64,
                                max_components: usize) -> PyResult<Vec<Vec<usize>>> {
    let shape = smooth.shape();
    let (rows, n_smooth) = (shape[0], shape[1]);
    let view = PeakView {
        smooth: smooth.as_slice()?,
        n_smooth,
        row: row.as_slice()?,
        mz: mz.as_slice()?,
        apex: apex.as_slice()?,
        apex_sub: apex_sub.as_slice()?,
        width_half: width_half.as_slice()?,
        score: score.as_slice()?,
    };
    let count = view.mz.len();
    if [view.row.len(), view.apex.len(), view.apex_sub.len(), view.width_half.len(), view.score.len()]
        .iter().any(|&l| l != count) {
        return Err(PyValueError::new_err("perceive_components: arrays of different lengths"));
    }
    if n < 0 || n as usize != n_smooth || view.row.iter().any(|&r| r < 0 || r as usize >= rows) {
        return Err(PyValueError::new_err("perceive_components: rows or scans out of range"));
    }
    let gp = GroupParams { apex_tolerance, shape_r, min_ions, corr_min_half, min_separation, max_components };
    Ok(py.detach(|| perceive_components_impl(&view, n, &gp)))
}

/// Pearson r of each (seed, candidate) pair over the seed's slice `lo..hi` of the smoothed traces,
/// the slices padded with zeros to the longest one (as `_links` lays them out).
#[pyfunction]
pub fn link_r<'py>(py: Python<'py>, smooth: PyReadonlyArray2<'py, f64>, seed_rows: PyReadonlyArray1<'py, i64>,
                   cand_rows: PyReadonlyArray1<'py, i64>, lo: PyReadonlyArray1<'py, i64>,
                   hi: PyReadonlyArray1<'py, i64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let shape = smooth.shape();
    let (rows, n) = (shape[0], shape[1]);
    let sm = smooth.as_slice()?;
    let (sr, cr, lo, hi) = (seed_rows.as_slice()?, cand_rows.as_slice()?, lo.as_slice()?, hi.as_slice()?);
    let count = sr.len();
    if cr.len() != count || lo.len() != count || hi.len() != count {
        return Err(PyValueError::new_err("link_r: arrays of different lengths"));
    }
    if sr.iter().chain(cr).any(|&r| r < 0 || r as usize >= rows) || n == 0 {
        return Err(PyValueError::new_err("link_r: row out of range"));
    }
    let width = (0..count).map(|q| hi[q] - lo[q]).max().unwrap_or(0).max(0) as usize;
    let r: Vec<f64> = py.detach(|| {
        (0..count).into_par_iter().map_init(|| (vec![0.0; width], vec![0.0; width]), |(sa, sb), q| {
            pearson(sm, n, sr[q] as usize, cr[q] as usize, lo[q], hi[q], sa, sb)
        }).collect()
    });
    Ok(r.into_pyarray(py))
}

/// `np.einsum("i,i->", a, b)`, exposed for the tests.
#[pyfunction]
pub fn np_einsum_dot(a: PyReadonlyArray1<f64>, b: PyReadonlyArray1<f64>) -> PyResult<f64> {
    let (a, b) = (a.as_slice()?, b.as_slice()?);
    if a.len() != b.len() {
        return Err(PyValueError::new_err("lengths differ"));
    }
    Ok(einsum_dot(a, b))
}

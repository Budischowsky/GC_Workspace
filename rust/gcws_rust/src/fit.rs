//! The grid search of `gcws.ms.component_fit.fit_trace_uncached` (`gcws.ms.rust_fit` uses it).
//!
//! `grid_residuals` gives the residual sum of squares of the trace against the component curves
//! for many (shift, width factor) grid points: the curves as `component_fit._curves` computes
//! them (PCHIP, the same operations), the columns scaled to unit maximum and the Lawson-Hanson
//! NNLS of the NIAS engine (`gc_deconv._nnls`: the same pivot rule, tolerance and steps).
//!
//! Only the least-squares solves differ from Python: numpy's pseudo-inverse comes from LAPACK's
//! SVD, here from a one-sided Jacobi SVD with the same cutoff (1e-15 x the largest singular
//! value). The residuals agree to about 1e-15 relative; the grid search keeps the first minimum,
//! and the final fit at the chosen point is computed by the Python code itself.

use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

/// A component's elution profile: `component_fit.Shape` (rt, t, y, PCHIP slopes d).
struct Shape<'a> {
    rt: f64,
    t: &'a [f64],
    y: &'a [f64],
    d: &'a [f64],
}

impl Shape<'_> {
    /// `pchip_eval` at one point inside the profile.
    #[inline]
    fn eval(&self, xi: f64) -> f64 {
        let x = self.t;
        let n = x.len();
        let k = (x.partition_point(|&v| v <= xi) as isize - 1).clamp(0, n as isize - 2) as usize;
        let h = x[k + 1] - x[k];
        let s = (xi - x[k]) / h;
        let (s2, s3) = (s * s, s * s * s);
        (2.0 * s3 - 3.0 * s2 + 1.0) * self.y[k] + (s3 - 2.0 * s2 + s) * h * self.d[k]
            + (-2.0 * s3 + 3.0 * s2) * self.y[k + 1] + (s3 - s2) * h * self.d[k + 1]
    }

    /// `_curves` row: the profile on the trace axis `t` for one (shift, stretch).
    fn curve(&self, t: &[f64], shift: f64, stretch: f64, out: &mut [f64]) {
        let (lo, hi) = (self.t[0], self.t[self.t.len() - 1]);
        for (o, &ti) in out.iter_mut().zip(t) {
            let src = self.rt + (ti - shift - self.rt) / stretch;
            *o = if src > lo && src < hi { self.eval(src).max(0.0) } else { 0.0 };
        }
    }
}

/// Column-major m x n matrix.
struct Mat {
    m: usize,
    n: usize,
    a: Vec<f64>,
}

impl Mat {
    #[inline]
    fn col(&self, j: usize) -> &[f64] {
        &self.a[j * self.m..(j + 1) * self.m]
    }
}

#[inline]
fn dot(a: &[f64], b: &[f64]) -> f64 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

/// x = pinv(A[:, cols]) @ b, the pseudo-inverse from a one-sided Jacobi SVD (cutoff 1e-15 x s_max).
fn pinv_solve(a: &Mat, cols: &[usize], b: &[f64]) -> Vec<f64> {
    let (m, p) = (a.m, cols.len());
    let mut u: Vec<f64> = Vec::with_capacity(m * p);
    for &j in cols {
        u.extend_from_slice(a.col(j));
    }
    let mut v = vec![0.0; p * p];
    for i in 0..p {
        v[i * p + i] = 1.0;
    }
    for _sweep in 0..60 {
        let mut rotated = false;
        for i in 0..p {
            for j in i + 1..p {
                let (ui, uj) = (&u[i * m..(i + 1) * m], &u[j * m..(j + 1) * m]);
                let alpha = dot(ui, ui);
                let beta = dot(uj, uj);
                let gamma = dot(ui, uj);
                if gamma == 0.0 || gamma.abs() <= f64::EPSILON * (alpha * beta).sqrt() {
                    continue;
                }
                rotated = true;
                let zeta = (beta - alpha) / (2.0 * gamma);
                let tan = zeta.signum() / (zeta.abs() + (1.0 + zeta * zeta).sqrt());
                let tan = if zeta == 0.0 { 1.0 } else { tan };
                let c = 1.0 / (1.0 + tan * tan).sqrt();
                let s = c * tan;
                for k in 0..m {
                    let (x, y) = (u[i * m + k], u[j * m + k]);
                    u[i * m + k] = c * x - s * y;
                    u[j * m + k] = s * x + c * y;
                }
                for k in 0..p {
                    let (x, y) = (v[i * p + k], v[j * p + k]);
                    v[i * p + k] = c * x - s * y;
                    v[j * p + k] = s * x + c * y;
                }
            }
        }
        if !rotated {
            break;
        }
    }
    // singular values are the column norms of u; x = sum_i v_i (u_i . b) / s_i^2
    let norms2: Vec<f64> = (0..p).map(|i| dot(&u[i * m..(i + 1) * m], &u[i * m..(i + 1) * m])).collect();
    let smax = norms2.iter().cloned().fold(0.0f64, f64::max).sqrt();
    let cutoff = 1e-15 * smax;
    let mut x = vec![0.0; p];
    for i in 0..p {
        let s = norms2[i].sqrt();
        if !(s > cutoff) {
            continue;
        }
        let coef = dot(&u[i * m..(i + 1) * m], b) / norms2[i];
        for k in 0..p {
            x[k] += v[i * p + k] * coef;
        }
    }
    x
}

/// `gc_deconv._nnls(a, b)`: Lawson-Hanson, ties broken by the lowest column index.
fn nnls(a: &Mat, b: &[f64]) -> Vec<f64> {
    let (m, n) = (a.m, a.n);
    if n == 0 {
        return Vec::new();
    }
    let max_iter = 3 * n;
    let amax = a.a.iter().fold(0.0f64, |acc, v| acc.max(v.abs()));
    let bmax = b.iter().fold(0.0f64, |acc, v| acc.max(v.abs()));
    let scale = amax.max(1.0) * bmax.max(1.0);
    let tol = m.max(n) as f64 * f64::EPSILON * scale;
    let mut x = vec![0.0; n];
    let mut passive = vec![false; n];
    let gradient = |x: &[f64]| -> Vec<f64> {
        let mut r = b.to_vec();
        for j in 0..n {
            if x[j] != 0.0 {
                for (rk, ak) in r.iter_mut().zip(a.col(j)) {
                    *rk -= ak * x[j];
                }
            }
        }
        (0..n).map(|j| dot(a.col(j), &r)).collect()
    };
    let mut w: Vec<f64> = (0..n).map(|j| dot(a.col(j), b)).collect();
    let mut outer = 0;
    while outer < max_iter {
        // argmax of w over the active set, lowest index on ties
        let mut j_best: Option<usize> = None;
        for j in 0..n {
            if !passive[j] && j_best.is_none_or(|k| w[j] > w[k]) {
                j_best = Some(j);
            }
        }
        let Some(j) = j_best else { break };
        if w[j] <= tol {
            break;
        }
        outer += 1;
        passive[j] = true;
        let mut inner = 0;
        while inner <= 3 * n {
            inner += 1;
            let key: Vec<usize> = (0..n).filter(|&k| passive[k]).collect();
            if key.is_empty() {
                x = vec![0.0; n];
                break;
            }
            let mut s = vec![0.0; n];
            for (&k, v) in key.iter().zip(pinv_solve(a, &key, b)) {
                s[k] = v;
            }
            let smin = key.iter().map(|&k| s[k]).fold(f64::INFINITY, f64::min);
            if smin > tol {
                x = s;
                break;
            }
            let mut alpha = f64::INFINITY;
            let mut any = false;
            for &k in &key {
                if s[k] <= tol {
                    any = true;
                    let denom = x[k] - s[k];
                    let ratio = if denom > 0.0 { x[k] / denom } else { 0.0 };
                    alpha = alpha.min(ratio);
                }
            }
            let alpha = if any { alpha } else { 0.0 };
            for k in 0..n {
                x[k] += alpha * (s[k] - x[k]);
            }
            for k in 0..n {
                if passive[k] && x[k].abs() <= tol {
                    passive[k] = false;
                }
                if !passive[k] {
                    x[k] = 0.0;
                }
            }
        }
        w = gradient(&x);
    }
    for v in x.iter_mut() {
        if *v < 0.0 {
            *v = 0.0;
        }
    }
    x
}

/// `component_fit._residual(a, y)[0]`.
fn residual(mut a: Mat, y: &[f64]) -> f64 {
    if a.a.iter().all(|&v| v == 0.0) {
        return dot(y, y);
    }
    for j in 0..a.n {
        let col = &mut a.a[j * a.m..(j + 1) * a.m];
        let s = col.iter().fold(0.0f64, |acc, v| acc.max(v.abs()));
        let s = if s > 0.0 { s } else { 1.0 };
        for v in col.iter_mut() {
            *v /= s;
        }
    }
    let x = nnls(&a, y);
    let mut r = y.to_vec();
    for j in 0..a.n {
        for (rk, ak) in r.iter_mut().zip(a.col(j)) {
            *rk -= ak * x[j];
        }
    }
    dot(&r, &r)
}

/// Residual sums of squares of `y` (on `t`) per grid point (shifts[i], stretches[i]).
///
/// shapes: [(rt, t, y, d)]. `summed`: one column, the sum of the curves (the MS signal as a
/// whole), else one column per shape.
#[pyfunction]
#[pyo3(signature = (shapes, t, y, shifts, stretches, summed=false))]
#[allow(clippy::type_complexity)]
pub fn grid_residuals(
    py: Python<'_>,
    shapes: Vec<(f64, PyReadonlyArray1<f64>, PyReadonlyArray1<f64>, PyReadonlyArray1<f64>)>,
    t: PyReadonlyArray1<f64>,
    y: PyReadonlyArray1<f64>,
    shifts: Vec<f64>,
    stretches: Vec<f64>,
    summed: bool,
) -> PyResult<Vec<f64>> {
    if shifts.len() != stretches.len() {
        return Err(PyValueError::new_err("shifts and stretches differ in length"));
    }
    let mut views = Vec::with_capacity(shapes.len());
    for (rt, st, sy, sd) in &shapes {
        let (st, sy, sd) = (st.as_slice()?, sy.as_slice()?, sd.as_slice()?);
        if st.len() < 2 || sy.len() != st.len() || sd.len() != st.len() {
            return Err(PyValueError::new_err("a shape needs t, y and d of one length >= 2"));
        }
        views.push(Shape { rt: *rt, t: st, y: sy, d: sd });
    }
    if views.is_empty() {
        return Err(PyValueError::new_err("no shapes"));
    }
    let (t, y) = (t.as_slice()?, y.as_slice()?);
    if t.len() != y.len() {
        return Err(PyValueError::new_err("t and y differ in length"));
    }
    let m = t.len();
    let views = &views;
    Ok(py.detach(|| {
        (0..shifts.len()).into_par_iter().map(|i| {
            let (shift, stretch) = (shifts[i], stretches[i]);
            let a = if summed {
                let mut col = vec![0.0; m];
                let mut c = vec![0.0; m];
                for s in views {
                    s.curve(t, shift, stretch, &mut c);
                    for (o, v) in col.iter_mut().zip(&c) {
                        *o += v;
                    }
                }
                Mat { m, n: 1, a: col }
            } else {
                let mut a = vec![0.0; m * views.len()];
                for (j, s) in views.iter().enumerate() {
                    s.curve(t, shift, stretch, &mut a[j * m..(j + 1) * m]);
                }
                Mat { m, n: views.len(), a }
            };
            residual(a, y)
        }).collect()
    }))
}

//! The prefilter norms of `gcws.libsearch.norms`: per reference row the running sum of
//! `I * (m/100)^2` over the masses `first..=last` of the inverted index, added mass by mass.
//!
//! numpy adds `out[rows] += I * f*f` one mass after the other, so every row gets its terms in
//! ascending mass order. The index lists a mass's rows in ascending order (`rows_ordered` checks
//! it once per shard), so a range of rows is one contiguous slice of every mass's postings:
//! threads take ranges of rows and each row still adds its terms in the same order, as the same
//! float64 operations (`f = m / 100`, `term = I * (f * f)`, `out + term`). The sums are numpy's
//! bit for bit.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

/// True when every mass lists its rows strictly ascending and below `count`.
#[pyfunction]
pub fn rows_ordered<'py>(
    py: Python<'py>,
    pointers: PyReadonlyArray1<'py, i64>,
    rows: PyReadonlyArray1<'py, i32>,
    count: usize,
) -> PyResult<bool> {
    let p = pointers.as_slice()?;
    let r = rows.as_slice()?;
    if p.is_empty() {
        return Ok(true);
    }
    Ok(py.detach(|| {
        (0..p.len() - 1).into_par_iter().all(|m| {
            let (a, b) = (p[m], p[m + 1]);
            if a < 0 || b < a || b as usize > r.len() {
                return false;
            }
            let s = &r[a as usize..b as usize];
            s.iter().all(|&x| x >= 0 && (x as usize) < count) && s.windows(2).all(|w| w[0] < w[1])
        })
    }))
}

/// `start` plus the terms of the masses `first..=last`, as a new array.
/// The caller has checked the shard with `rows_ordered`.
#[pyfunction]
pub fn fold_norms<'py>(
    py: Python<'py>,
    pointers: PyReadonlyArray1<'py, i64>,
    rows: PyReadonlyArray1<'py, i32>,
    intensities: PyReadonlyArray1<'py, f32>,
    start: PyReadonlyArray1<'py, f64>,
    first: usize,
    last: usize,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let p = pointers.as_slice()?;
    let r = rows.as_slice()?;
    let it = intensities.as_slice()?;
    let mut out = start.as_slice()?.to_vec();
    let count = out.len();
    if first > last {
        return Ok(out.into_pyarray(py));
    }
    if last + 1 >= p.len() {
        return Err(PyValueError::new_err("mass range beyond the index"));
    }
    if r.len() != it.len() || p[first] < 0 || p[last + 1] as usize > r.len() || p[first] > p[last + 1] {
        return Err(PyValueError::new_err("inconsistent index arrays"));
    }
    let run = |out: &mut [f64], r0: usize| {
        let r1 = r0 + out.len();
        for m in first..=last {
            let (a, b) = (p[m] as usize, p[m + 1] as usize);
            if b <= a {
                continue;
            }
            let s = &r[a..b];
            let i0 = s.partition_point(|&x| (x as usize) < r0);
            let i1 = s.partition_point(|&x| (x as usize) < r1);
            let f = m as f64 / 100.0;
            let ff = f * f;
            for k in i0..i1 {
                out[s[k] as usize - r0] += it[a + k] as f64 * ff;
            }
        }
    };
    py.detach(|| {
        let chunk = (count / (rayon::current_num_threads() * 4)).max(4096);
        if count <= chunk {
            run(&mut out, 0);
        } else {
            out.par_chunks_mut(chunk).enumerate().for_each(|(n, c)| run(c, n * chunk));
        }
    });
    Ok(out.into_pyarray(py))
}

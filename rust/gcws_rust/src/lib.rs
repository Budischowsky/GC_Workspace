//! Rust kernels of the GC Workspace library search (`gcws.libsearch.rust_search` uses them).
//!
//! * `prefilter`: stage 1, the exact prefilter of a batch of peaks with the standard selection
//!   (variants "sparse" and "tiled").
//! * `Refs`: stage 2, reference spectra decoded from the library files (cached), their PBM side
//!   and the PBM scores of one peak against its candidates.
//! * `integ`: kernels of the chromatogram integrator (`gcws.integration.rust_integration`).
//!
//! Both reproduce the Python results bit for bit (see the module docs).

mod integ;
mod kernel;
mod refs;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyFrozenSet, PyTuple};
use rayon::prelude::*;

use kernel::{Pool, Query, Selection, ShardView};
use refs::{Bytes, Decoded, Kind, Reader, Stats, Unknown};

type SelectionArrays<'py> = (
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i32>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<i64>>,
);

/// Stage 1 for a batch of peaks over a group of shards.
///
/// shards: [(pointers int64, rows int32, intensities float32, [refnorm float64 per range], start, id)]
/// queries: [(masses int64 ascending, sqrt(i) float32, weight float32, i*weight float64, qnorm, range)]
/// Returns per peak None (fewer than k positive values in a direction) or
/// (rows, shard ids, forward, reverse, maybe rows), rows ascending.
#[pyfunction]
#[pyo3(signature = (shards, queries, k, mode, tile=4096, chunk=16, segment=65536))]
fn prefilter<'py>(
    py: Python<'py>,
    shards: Vec<Bound<'py, PyTuple>>,
    queries: Vec<Bound<'py, PyTuple>>,
    k: usize,
    mode: &str,
    tile: usize,
    chunk: usize,
    segment: usize,
) -> PyResult<Vec<Option<SelectionArrays<'py>>>> {
    // borrow the shard arrays
    let mut held = Vec::with_capacity(shards.len());
    for s in &shards {
        let pointers: PyReadonlyArray1<i64> = s.get_item(0)?.extract()?;
        let rows: PyReadonlyArray1<i32> = s.get_item(1)?.extract()?;
        let intensities: PyReadonlyArray1<f32> = s.get_item(2)?.extract()?;
        let refnorms: Vec<PyReadonlyArray1<f64>> = s.get_item(3)?.extract()?;
        let start: i64 = s.get_item(4)?.extract()?;
        let id: i32 = s.get_item(5)?.extract()?;
        held.push((pointers, rows, intensities, refnorms, start, id));
    }
    let mut views = Vec::with_capacity(held.len());
    for (pointers, rows, intensities, refnorms, start, id) in &held {
        let pointers = pointers.as_slice()?;
        let rows = rows.as_slice()?;
        let intensities = intensities.as_slice()?;
        if pointers.len() < 10002 || rows.len() != intensities.len() {
            return Err(PyValueError::new_err("inconsistent shard arrays"));
        }
        let refnorms = refnorms.iter().map(|r| r.as_slice()).collect::<Result<Vec<_>, _>>()?;
        let count = refnorms.first().map(|r| r.len()).unwrap_or(0);
        if refnorms.iter().any(|r| r.len() != count) {
            return Err(PyValueError::new_err("inconsistent reference norms"));
        }
        if rows.iter().any(|&r| r < 0 || r as usize >= count) {
            return Err(PyValueError::new_err("reference row out of range"));
        }
        views.push(ShardView { pointers, rows, intensities, refnorms, start: *start, id: *id, count });
    }
    let mut qs = Vec::with_capacity(queries.len());
    for q in &queries {
        let masses: PyReadonlyArray1<i64> = q.get_item(0)?.extract()?;
        let si: PyReadonlyArray1<f32> = q.get_item(1)?.extract()?;
        let w: PyReadonlyArray1<f32> = q.get_item(2)?.extract()?;
        let iw: PyReadonlyArray1<f64> = q.get_item(3)?.extract()?;
        let masses: Vec<usize> = masses.as_slice()?.iter().map(|&m| m as usize).collect();
        if masses.iter().any(|&m| m > 10000) || masses.windows(2).any(|p| p[1] <= p[0]) {
            return Err(PyValueError::new_err("query ions must be ascending m/z in 0..=10000"));
        }
        let range: usize = q.get_item(5)?.extract()?;
        if views.iter().any(|v| range >= v.refnorms.len()) {
            return Err(PyValueError::new_err("range index out of bounds"));
        }
        qs.push(Query {
            masses,
            si: si.as_slice()?.to_vec(),
            w: w.as_slice()?.to_vec(),
            iw: iw.as_slice()?.to_vec(),
            qnorm: q.get_item(4)?.extract()?,
            range,
        });
    }
    let tile = tile.max(64);
    let chunk = chunk.max(1);
    let segment = segment.max(tile);
    let selections: Vec<Option<Selection>> = py.detach(|| match mode {
        "sparse" => qs
            .par_iter()
            .map_init(
                || (Vec::new(), Vec::new(), Vec::new()),
                |(dots, rn, touched), q| kernel::sparse_query(q, &views, k, dots, rn, touched).finish(),
            )
            .collect(),
        _ => {
            let refs: Vec<&Query> = qs.iter().collect();
            let chunks: Vec<(usize, &[&Query])> =
                refs.chunks(chunk).enumerate().map(|(n, c)| (n * chunk, c)).collect();
            let mut tasks = Vec::new();
            for &(first, c) in &chunks {
                for s in &views {
                    let mut a = 0;
                    while a < s.count {
                        let b = (a + segment).min(s.count);
                        tasks.push((first, c, s, a, b));
                        a = b;
                    }
                }
            }
            let done: Vec<(usize, Vec<Pool>)> = tasks
                .into_par_iter()
                .map(|(first, c, s, a, b)| {
                    let pools = if mode == "tiled2" {
                        kernel::tiled2_task(c, s, a, b, tile, k)
                    } else {
                        kernel::tiled_task(c, s, a, b, tile, k)
                    };
                    (first, pools)
                })
                .collect();
            let mut pools: Vec<Option<Pool>> = (0..qs.len()).map(|_| None).collect();
            for (first, list) in done {
                for (n, p) in list.into_iter().enumerate() {
                    match &mut pools[first + n] {
                        Some(existing) => existing.merge(p),
                        slot => *slot = Some(p),
                    }
                }
            }
            pools.into_par_iter().map(|p| p.and_then(|p| p.finish())).collect()
        }
    });
    Ok(selections
        .into_iter()
        .map(|s| {
            s.map(|s| {
                (
                    s.rows.into_pyarray(py),
                    s.shards.into_pyarray(py),
                    s.forward.into_pyarray(py),
                    s.reverse.into_pyarray(py),
                    s.maybe.into_pyarray(py),
                )
            })
        })
        .collect())
}

/// `Engine._prefilter_shard` of one peak against one shard: (forward, reverse) for every reference.
///
/// The same float32 terms, added in float64 in the order of the peak's ions (as `np.bincount`
/// adds them), the same divisions; a reference without a shared ion gets 0.0, as there.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn prefilter_dense<'py>(
    py: Python<'py>,
    pointers: PyReadonlyArray1<'py, i64>,
    rows: PyReadonlyArray1<'py, i32>,
    intensities: PyReadonlyArray1<'py, f32>,
    refnorm: PyReadonlyArray1<'py, f64>,
    masses: PyReadonlyArray1<'py, i64>,
    si: PyReadonlyArray1<'py, f32>,
    w: PyReadonlyArray1<'py, f32>,
    iw: PyReadonlyArray1<'py, f64>,
    qnorm: f64,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (pointers, rows, intensities) = (pointers.as_slice()?, rows.as_slice()?, intensities.as_slice()?);
    let refnorm = refnorm.as_slice()?;
    let (masses, si, w, iw) = (masses.as_slice()?, si.as_slice()?, w.as_slice()?, iw.as_slice()?);
    let count = refnorm.len();
    if pointers.len() < 10002 || rows.len() != intensities.len() || si.len() != masses.len()
        || w.len() != masses.len() || iw.len() != masses.len() {
        return Err(PyValueError::new_err("inconsistent arrays"));
    }
    if masses.iter().any(|&m| !(0..=10000).contains(&m)) {
        return Err(PyValueError::new_err("m/z out of range"));
    }
    for &m in masses {
        let (a, b) = (pointers[m as usize], pointers[m as usize + 1]);
        // (a reference row beyond the norms would panic on the indexing below: a PanicException)
        if a < 0 || b < a || b as usize > rows.len() {
            return Err(PyValueError::new_err("posting out of range"));
        }
    }
    let (forward, reverse) = py.detach(|| {
        let mut dots = vec![0.0f64; count];
        let mut rn = vec![0.0f64; count];
        for (j, &m) in masses.iter().enumerate() {
            let a = pointers[m as usize] as usize;
            let b = pointers[m as usize + 1] as usize;
            let (s, wt, x) = (si[j], w[j], iw[j]);
            for (&r, &v) in rows[a..b].iter().zip(&intensities[a..b]) {
                let t = (v.sqrt() * s) * wt;
                dots[r as usize] += t as f64;
                rn[r as usize] += x;
            }
        }
        for r in 0..count {
            let (d, x, norm) = (dots[r], rn[r], refnorm[r]);
            dots[r] = if norm > 0.0 { d / (qnorm * norm).sqrt() } else { 0.0 };
            let p = x * norm;
            rn[r] = if p > 0.0 { d / p.sqrt() } else { 0.0 };
        }
        (dots, rn)
    });
    Ok((forward.into_pyarray(py), reverse.into_pyarray(py)))
}

/// Decoded reference spectra of an engine's native libraries, with their PBM sides.
#[pyclass(frozen)]
struct Refs {
    readers: Vec<Reader>,
    stats: Option<Stats>,
    cache: Mutex<HashMap<(i64, i64, i64), Arc<Decoded>>>,
    capacity: usize,
    _keep: Vec<Py<PyAny>>,
}

impl Refs {
    fn reader(&self, row: i64) -> Option<(&Reader, usize)> {
        let at = self.readers.partition_point(|r| r.start <= row);
        let r = self.readers.get(at.checked_sub(1)?)?;
        if row < r.start + r.count {
            Some((r, (row - r.start) as usize))
        } else {
            None
        }
    }

    fn decode_row(&self, row: i64, minimum: i64, maximum: i64, mz: &mut Vec<f64>, it: &mut Vec<f64>) -> Option<Decoded> {
        let (reader, local) = self.reader(row)?;
        reader.peaks(local, mz, it)?;
        refs::decode(mz, it, minimum, maximum, self.stats.as_ref())
    }

    /// The rows' decoded spectra (cached), or None if one cannot be decoded here.
    fn ensure(&self, py: Python<'_>, rows: &[i64], minimum: i64, maximum: i64) -> Option<(Vec<Arc<Decoded>>, usize)> {
        let mut out: Vec<Option<Arc<Decoded>>> = Vec::with_capacity(rows.len());
        let mut missing = Vec::new();
        {
            let cache = self.cache.lock().unwrap();
            for (n, &row) in rows.iter().enumerate() {
                let hit = cache.get(&(row, minimum, maximum)).cloned();
                if hit.is_none() {
                    missing.push(n);
                }
                out.push(hit);
            }
        }
        let decoded: Vec<Option<Decoded>> = py.detach(|| {
            missing
                .par_iter()
                .with_min_len(32)
                .map_init(
                    || (Vec::new(), Vec::new()),
                    |(mz, it), &n| self.decode_row(rows[n], minimum, maximum, mz, it),
                )
                .collect()
        });
        if decoded.iter().any(|d| d.is_none()) {
            return None;
        }
        let count = missing.len();
        {
            let mut cache = self.cache.lock().unwrap();
            if cache.len() + count > self.capacity {
                cache.clear();
            }
            for (n, d) in missing.into_iter().zip(decoded) {
                let d = Arc::new(d.unwrap());
                cache.insert((rows[n], minimum, maximum), d.clone());
                out[n] = Some(d);
            }
        }
        Some((out.into_iter().map(|d| d.unwrap()).collect(), count))
    }
}

fn bytes_of(b: &Bound<'_, PyBytes>) -> Bytes {
    let s = b.as_bytes();
    Bytes::new(s.as_ptr(), s.len())
}

#[pymethods]
impl Refs {
    /// readers: [(start, count, kind, data bytes, offsets int64, ends int64 or None, spc start,
    /// info bytes or None, info start)], kind "agilent" | "shimadzu" | "nist".
    #[new]
    #[pyo3(signature = (readers, uniqueness=None, abundance=None, capacity=200000))]
    fn new(
        readers: Vec<Bound<'_, PyTuple>>,
        uniqueness: Option<PyReadonlyArray1<f64>>,
        abundance: Option<PyReadonlyArray1<f64>>,
        capacity: usize,
    ) -> PyResult<Self> {
        let mut out = Vec::new();
        let mut keep = Vec::new();
        for t in &readers {
            let start: i64 = t.get_item(0)?.extract()?;
            let count: i64 = t.get_item(1)?.extract()?;
            let kind: String = t.get_item(2)?.extract()?;
            let data = t.get_item(3)?.cast_into::<PyBytes>()?;
            let offsets: PyReadonlyArray1<i64> = t.get_item(4)?.extract()?;
            let offsets = offsets.as_slice()?.to_vec();
            let kind = match kind.as_str() {
                "agilent" => Kind::Agilent { data: bytes_of(&data), offsets },
                "nist" => Kind::Nist { data: bytes_of(&data), offsets },
                "shimadzu" => {
                    let ends: PyReadonlyArray1<i64> = t.get_item(5)?.extract()?;
                    let info = t.get_item(7)?.cast_into::<PyBytes>()?;
                    let k = Kind::Shimadzu {
                        data: bytes_of(&data),
                        start: t.get_item(6)?.extract()?,
                        offsets,
                        ends: ends.as_slice()?.to_vec(),
                        info: bytes_of(&info),
                        info_start: t.get_item(8)?.extract()?,
                    };
                    keep.push(info.into_any().unbind());
                    k
                }
                other => return Err(PyValueError::new_err(format!("unknown reader kind {other}"))),
            };
            keep.push(data.into_any().unbind());
            out.push(Reader { start, count, kind });
        }
        out.sort_by_key(|r| r.start);
        let stats = match (uniqueness, abundance) {
            (Some(u), Some(a)) => {
                let abundance = a.as_slice()?.to_vec();
                if abundance.len() < 101 {
                    return Err(PyValueError::new_err("abundance needs 101 values"));
                }
                Some(Stats { uniqueness: u.as_slice()?.to_vec(), abundance })
            }
            _ => None,
        };
        Ok(Refs { readers: out, stats, cache: Mutex::new(HashMap::new()), capacity, _keep: keep })
    }

    /// Decode (and cache) the rows; the number decoded now, or None if one cannot be decoded here.
    fn decode(&self, py: Python<'_>, rows: PyReadonlyArray1<i64>, minimum: i64, maximum: i64) -> PyResult<Option<usize>> {
        Ok(self.ensure(py, rows.as_slice()?, minimum, maximum).map(|(_, n)| n))
    }

    /// PBM of one unknown against the rows: (confidence, reverse, forward, decoded now), or None.
    #[allow(clippy::too_many_arguments)]
    fn pbm<'py>(
        &self,
        py: Python<'py>,
        rows: PyReadonlyArray1<i64>,
        minimum: i64,
        maximum: i64,
        dense: PyReadonlyArray1<f64>,
        log2inv: PyReadonlyArray1<f64>,
        qpeaks: PyReadonlyArray1<i64>,
        qweights: PyReadonlyArray1<f64>,
        limits: PyReadonlyArray1<f64>,
        qattainable: f64,
    ) -> PyResult<Option<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, usize)>> {
        let stats = self.stats.as_ref().ok_or_else(|| PyValueError::new_err("no PBM statistics"))?;
        let Some((decoded, count)) = self.ensure(py, rows.as_slice()?, minimum, maximum) else {
            return Ok(None);
        };
        let u = Unknown {
            dense: dense.as_slice()?,
            log2inv: log2inv.as_slice()?,
            qpeaks: qpeaks.as_slice()?,
            qweights: qweights.as_slice()?,
            limits: limits.as_slice()?,
            qattainable,
        };
        if u.dense.len() != u.log2inv.len() || u.dense.is_empty() {
            return Err(PyValueError::new_err("dense arrays differ"));
        }
        let n = decoded.len();
        let (mut c, mut r, mut f) = (vec![0.0; n], vec![0.0; n], vec![0.0; n]);
        for (i, d) in decoded.iter().enumerate() {
            if let Some(side) = &d.side {
                let (ci, ri, fi) = refs::pbm(&u, side, stats);
                c[i] = ci;
                r[i] = ri;
                f[i] = fi;
            }
        }
        Ok(Some((c.into_pyarray(py), r.into_pyarray(py), f.into_pyarray(py), count)))
    }

    /// The decoded spectrum as ``fast._decode_many`` returns it: ({m/z: value}, None if every
    /// value is positive else the frozenset of m/z with a positive value), or None.
    fn reference<'py>(&self, py: Python<'py>, row: i64, minimum: i64, maximum: i64)
                      -> PyResult<Option<(Bound<'py, PyDict>, Option<Bound<'py, PyFrozenSet>>)>> {
        let Some((d, _)) = self.ensure(py, &[row], minimum, maximum) else {
            return Ok(None);
        };
        let d = &d[0];
        let dict = PyDict::new(py);
        for (&m, &v) in d.masses.iter().zip(&d.values) {
            dict.set_item(m, v)?;
        }
        let positive = if d.values.iter().all(|&v| v > 0.0) {
            None
        } else {
            let masses: Vec<i64> = d.masses.iter().zip(&d.values).filter(|(_, &v)| v > 0.0).map(|(&m, _)| m).collect();
            Some(PyFrozenSet::new(py, &masses)?)
        };
        Ok(Some((dict, positive)))
    }

    fn clear(&self) {
        self.cache.lock().unwrap().clear();
    }

    fn __len__(&self) -> usize {
        self.cache.lock().unwrap().len()
    }
}

#[pymodule]
fn gcws_rust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(prefilter, m)?)?;
    m.add_function(wrap_pyfunction!(prefilter_dense, m)?)?;
    m.add_class::<Refs>()?;
    m.add_function(wrap_pyfunction!(integ::detect, m)?)?;
    m.add_function(wrap_pyfunction!(integ::width_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(integ::raw_area, m)?)?;
    m.add_function(wrap_pyfunction!(integ::shape, m)?)?;
    m.add_function(wrap_pyfunction!(integ::nearest_index, m)?)?;
    m.add_function(wrap_pyfunction!(integ::np_interp, m)?)?;
    m.add_function(wrap_pyfunction!(integ::np_sum_f64, m)?)?;
    Ok(())
}

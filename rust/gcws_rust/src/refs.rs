//! Stage 2 of the library search: reference spectra decoded from the library files, their PBM
//! side, and the PBM scores of one unknown against many references.
//!
//! Every function repeats the Python it replaces operation by operation in float64:
//! `fast._peak_arrays` / the vendored readers' `decode`, `fast._decode_many` (nominal binning,
//! `100.0 * i / base`), `fast._reference_sides` (`pbm._percent`, `pbm._significant`, the
//! attainable bits summed like Python's `sum()`), and `fast._pbm_many`. A record the Rust
//! decoder does not accept is reported as failed; the caller then uses the Python path, which
//! raises the readers' own errors.

pub const SIGNIFICANT_PEAKS: usize = 20;
pub const WINDOW: f64 = 1.5;
// FORWARD_WINDOW (2.0) is applied in Python: the forward limits arrive as `unknown[m] / 2.0`
pub const UNIQUENESS_WEIGHT: f64 = 0.5;
pub const DEVIATION_PENALTY: f64 = 2.0;
pub const MIN_ABUNDANCE: f64 = 1.0;
const NIST_MAX_STEP: u8 = 204;
const NIST_MAX_MZ: i64 = 4000;

/// Raw bytes owned by a Python `bytes` object that the reader keeps alive.
#[derive(Clone, Copy)]
pub struct Bytes {
    ptr: *const u8,
    len: usize,
}
unsafe impl Send for Bytes {}
unsafe impl Sync for Bytes {}

impl Bytes {
    pub fn new(ptr: *const u8, len: usize) -> Self {
        Bytes { ptr, len }
    }
    #[inline(always)]
    fn s(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
    }
}

pub enum Kind {
    /// ChemStation FULL.D: scan offsets per row
    Agilent { data: Bytes, offsets: Vec<i64> },
    /// Wiley / Shimadzu: SPC records `start + offsets[row] .. start + ends[row]`, compound table in `info`
    Shimadzu { data: Bytes, start: usize, offsets: Vec<i64>, ends: Vec<i64>, info: Bytes, info_start: usize },
    /// NIST MS Search record file: record offsets per row
    Nist { data: Bytes, offsets: Vec<i64> },
}

pub struct Reader {
    pub start: i64,
    pub count: i64,
    pub kind: Kind,
}

#[inline(always)]
fn u16be(d: &[u8], o: usize) -> Option<u16> {
    Some(u16::from_be_bytes([*d.get(o)?, *d.get(o + 1)?]))
}
#[inline(always)]
fn u16le(d: &[u8], o: usize) -> Option<u16> {
    Some(u16::from_le_bytes([*d.get(o)?, *d.get(o + 1)?]))
}
#[inline(always)]
fn u32le(d: &[u8], o: usize) -> Option<u32> {
    Some(u32::from_le_bytes([*d.get(o)?, *d.get(o + 1)?, *d.get(o + 2)?, *d.get(o + 3)?]))
}

impl Reader {
    /// The record's (m/z, intensity) pairs as `fast._peak_arrays` yields them, or None.
    pub fn peaks(&self, local: usize, mz: &mut Vec<f64>, it: &mut Vec<f64>) -> Option<()> {
        mz.clear();
        it.clear();
        match &self.kind {
            Kind::Agilent { data, offsets } => {
                let d = data.s();
                let offset = usize::try_from(*offsets.get(local)?).ok()?;
                let count = u16be(d, offset + 12)? as usize;
                for n in 0..count {
                    let p = offset + 18 + 4 * n;
                    let m = u16be(d, p)?;
                    let raw = u16be(d, p + 2)? as u32;
                    mz.push(m as f64 / 20.0);
                    it.push(((raw & 16383) * 8u32.pow(raw >> 14)) as f64);
                }
                Some(())
            }
            Kind::Shimadzu { data, start, offsets, ends, info, info_start } => {
                let d = data.s();
                let a = start + usize::try_from(*offsets.get(local)?).ok()?;
                let b = start + usize::try_from(*ends.get(local)?).ok()?;
                let rec = d.get(a..b)?;
                let mw = u16le(rec, 0)?;
                let count = u16le(rec, 2)? as usize;
                let compound = u32le(rec, 4)? as usize;
                let base = u16le(rec, 8)? as i64;
                if rec.len() != 10 + count * 2 || count == 0 {
                    return None;
                }
                let mut page: i64 = 0;
                let mut has_base = false;
                let mut max_i: u8 = 0;
                for n in 0..count {
                    let intensity = rec[10 + 2 * n];
                    let mass = rec[11 + 2 * n];
                    if intensity == 0 && mass == 0 {
                        page += 255;
                        continue;
                    }
                    let m = page + mass as i64;
                    if m <= 0 || m > 10000 {
                        return None;
                    }
                    if let Some(&last) = mz.last() {
                        if last >= m as f64 {
                            return None;
                        }
                    }
                    if m == base && intensity == 250 {
                        has_base = true;
                    }
                    max_i = max_i.max(intensity);
                    mz.push(m as f64);
                    it.push(intensity as f64);
                }
                if mz.is_empty() || max_i != 250 || !has_base || compound == 0 {
                    return None;
                }
                if u16le(info.s(), info_start + (compound - 1) * 16 + 8)? != mw {
                    return None;
                }
                Some(())
            }
            Kind::Nist { data, offsets } => {
                let d = data.s();
                let offset = usize::try_from(*offsets.get(local)?).ok()?;
                let end = offset + u32le(d, offset + 2)? as usize;
                if end > d.len() || offset + 28 > end {
                    return None;
                }
                let name_end = offset + 28 + d[offset + 28..end].iter().position(|&c| c == 0)?;
                let formula_end = name_end + 1 + d.get(name_end + 1..end)?.iter().position(|&c| c == 0)?;
                let count = u16le(d, formula_end + 1)? as i64;
                let mut i = formula_end + 3;
                if !(0 < count && count <= NIST_MAX_MZ) {
                    return None;
                }
                let count = count as usize;
                let mut m0: i64 = 0;
                while i < end && d[i] == 0xFF {
                    m0 += 255;
                    i += 1;
                }
                if i >= end {
                    return None;
                }
                let mut masses: Vec<i64> = Vec::with_capacity(count);
                masses.push(m0 + d[i] as i64);
                i += 1;
                while masses.len() < count && i < end {
                    let code = d[i];
                    i += 1;
                    let last = *masses.last().unwrap();
                    if code == NIST_MAX_STEP {
                        if i >= end {
                            return None;
                        }
                        masses.push(last + NIST_MAX_STEP as i64 + d[i] as i64);
                        i += 1;
                    } else if code < NIST_MAX_STEP {
                        masses.push(last + code as i64);
                    } else {
                        for step in 1..(258 - code as i64) {
                            masses.push(last + step);
                        }
                    }
                }
                masses.truncate(count);
                if masses.len() != count || masses.windows(2).any(|w| w[1] <= w[0]) {
                    return None;
                }
                let mut max_i: i64 = i64::MIN;
                while it.len() < count && i < end {
                    let code = d[i];
                    if code >= 0xFB {
                        if i + 1 >= end {
                            return None;
                        }
                        let v = 5 * d[i + 1] as i64 + (255 - code as i64);
                        max_i = max_i.max(v);
                        it.push(v as f64);
                        i += 2;
                    } else {
                        max_i = max_i.max(code as i64);
                        it.push(code as f64);
                        i += 1;
                    }
                }
                if it.len() != count {
                    return None;
                }
                if !(0 < masses[0]) || masses[count - 1] > NIST_MAX_MZ || max_i != 999 {
                    return None;
                }
                mz.extend(masses.iter().map(|&m| m as f64));
                Some(())
            }
        }
    }
}

/// The PBM side of a reference (`fast._reference_sides`, one spectrum).
pub struct Side {
    pub base_mz: i64,
    pub attainable: f64,
    /// significant peaks in order: (m/z, weight, percent)
    pub top: Vec<(i64, f64, f64)>,
    /// every m/z from 1 % (ascending) with its percent
    pub all_m: Vec<i64>,
    pub all_p: Vec<f64>,
}

/// One decoded reference within a search range.
pub struct Decoded {
    pub masses: Vec<i64>,
    pub values: Vec<f64>,
    pub side: Option<Side>,
}

/// Neumaier-compensated sum as Python's `sum()` of floats computes it.
#[inline(always)]
fn compensated(values: impl Iterator<Item = f64>) -> f64 {
    let (mut total, mut comp) = (0.0f64, 0.0f64);
    for x in values {
        let t = total + x;
        comp += if total.abs() >= x.abs() { (total - t) + x } else { (x - t) + total };
        total = t;
    }
    if comp != 0.0 && comp.is_finite() {
        total + comp
    } else {
        total
    }
}

pub struct Stats {
    pub uniqueness: Vec<f64>,
    pub abundance: Vec<f64>,
}

impl Stats {
    #[inline(always)]
    pub fn a(&self, percent: f64) -> f64 {
        let i = (percent as i64).clamp(1, 100) as usize;
        self.abundance[i]
    }
    #[inline(always)]
    fn u(&self, m: i64) -> f64 {
        let n = self.uniqueness.len() as i64 - 1;
        self.uniqueness[m.min(n).max(0) as usize]
    }
}

/// `_decode_many` for one row: the in-range spectrum, or None (Python decides).
pub fn decode(mz: &[f64], it: &[f64], minimum: i64, maximum: i64, stats: Option<&Stats>) -> Option<Decoded> {
    // nominal binning; masses arrive ascending from every reader except possibly Agilent's
    // fractional m/z, so merge by sorting (m, original position)
    let mut binned: Vec<(i64, usize)> = Vec::with_capacity(mz.len());
    for (n, &x) in mz.iter().enumerate() {
        let m = (x + 0.5).floor();
        if m > 0.0 {
            binned.push((m as i64, n));
        }
    }
    binned.sort_unstable();
    let mut masses: Vec<i64> = Vec::with_capacity(binned.len());
    let mut sums: Vec<f64> = Vec::with_capacity(binned.len());
    for &(m, n) in &binned {
        if masses.last() == Some(&m) {
            *sums.last_mut().unwrap() += it[n];
        } else {
            masses.push(m);
            sums.push(0.0 + it[n]);
        }
    }
    if masses.is_empty() {
        return None;
    }
    let base = sums.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    if !(base > 0.0) {
        return None;
    }
    let mut out_m = Vec::new();
    let mut out_v = Vec::new();
    for (&m, &s) in masses.iter().zip(&sums) {
        if m >= minimum && m <= maximum {
            out_m.push(m);
            out_v.push(100.0 * s / base);
        }
    }
    let side = stats.and_then(|st| side_of(&out_m, &out_v, st));
    Some(Decoded { masses: out_m, values: out_v, side })
}

fn side_of(masses: &[i64], values: &[f64], st: &Stats) -> Option<Side> {
    if masses.is_empty() {
        return None;
    }
    let base = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    if !(base > 0.0) {
        return None;
    }
    let mut all_m = Vec::new();
    let mut all_p = Vec::new();
    for (&m, &v) in masses.iter().zip(values) {
        let p = 100.0 * v / base;
        if p >= MIN_ABUNDANCE {
            all_m.push(m);
            all_p.push(p);
        }
    }
    if all_m.is_empty() {
        return None;
    }
    let mut weighted: Vec<(f64, i64, f64)> = all_m
        .iter()
        .zip(&all_p)
        .map(|(&m, &p)| (UNIQUENESS_WEIGHT * st.u(m) + st.a(p), m, p))
        .collect();
    weighted.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap().then(a.1.cmp(&b.1)));
    weighted.truncate(SIGNIFICANT_PEAKS);
    let largest = all_p.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let base_mz = all_m[all_p.iter().position(|&p| p == largest).unwrap()];
    let attainable = compensated(weighted.iter().map(|x| x.0));
    Some(Side { base_mz, attainable, top: weighted.into_iter().map(|(w, m, p)| (m, w, p)).collect(), all_m, all_p })
}

/// The unknown's side of PBM, prepared in Python.
pub struct Unknown<'a> {
    /// percent by m/z (0 where absent), 10002 values
    pub dense: &'a [f64],
    /// `math.log2(1 / dilution)` by base m/z where the dilution is positive
    pub log2inv: &'a [f64],
    pub qpeaks: &'a [i64],
    pub qweights: &'a [f64],
    /// `unknown[m] / FORWARD_WINDOW` of each significant peak
    pub limits: &'a [f64],
    pub qattainable: f64,
}

/// `fast._pbm` (as `_pbm_many` computes it): (confidence, reverse, forward).
pub fn pbm(u: &Unknown, side: &Side, st: &Stats) -> (f64, f64, f64) {
    let n = u.dense.len() as i64 - 1;
    let at = |m: i64| u.dense[m.clamp(0, n) as usize];
    // reverse
    let dilution = (at(side.base_mz) / 100.0).min(1.0);
    let rev = if dilution > 0.0 && side.attainable > 0.0 {
        let mut bits = 0.0f64;
        for &(m, weight, value) in &side.top {
            let expected = value * dilution;
            let found = at(m);
            if found >= expected / WINDOW {
                bits += weight;
            } else if found > 0.0 {
                let partial = weight - DEVIATION_PENALTY * (st.a(expected) - st.a(found));
                bits += if partial > 0.0 { partial } else { 0.0 };
            } else {
                bits += 0.0;
            }
        }
        bits -= u.log2inv[side.base_mz.clamp(0, n) as usize];
        let r = bits / side.attainable;
        if r > 0.0 { r } else { 0.0 }
    } else {
        0.0
    };
    // forward
    let fwd = if u.qattainable > 0.0 && !u.qpeaks.is_empty() {
        let (mut total, mut comp) = (0.0f64, 0.0f64);
        for (j, &m) in u.qpeaks.iter().enumerate() {
            let counts = match side.all_m.binary_search(&m) {
                Ok(p) => {
                    let percent = side.all_p[p];
                    percent >= MIN_ABUNDANCE && percent >= u.limits[j]
                }
                Err(_) => false,
            };
            if counts {
                let x = u.qweights[j];
                let t = total + x;
                comp += if total >= x.abs() { (total - t) + x } else { (x - t) + total };
                total = t;
            }
        }
        if comp != 0.0 && comp.is_finite() {
            total += comp;
        }
        total / u.qattainable
    } else {
        0.0
    };
    ((rev + fwd) / 2.0, rev, fwd)
}

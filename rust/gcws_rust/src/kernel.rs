//! Stage 1 of the library search: the exact forward / reverse weighted cosines of a batch of peaks
//! against every reference of a group of shards, and the standard candidate selection.
//!
//! The arithmetic is the standard engine's (`Engine._prefilter_shard`): per posting the float32
//! term `(sqrt(I) * sqrt(i)) * weight`, widened to float64 and added to the reference's dot product
//! in the query's ion order (ascending m/z), starting from 0.0; the reverse norm adds the float64
//! `i * weight` the same way. `forward = dots / sqrt(qnorm * refnorm)`,
//! `reverse = dots / sqrt(reverse_norm * refnorm)`, 0 where the product is not positive.
//! No bounds or screening are involved, so every value is the standard one, bit for bit.
//!
//! Selection is `fast._select` over all references: the k best of each direction, every
//! reference tied with the k-th, and the rows only such a tie brings in ("maybe").

/// One shard of the inverted index (borrowed from numpy arrays).
pub struct ShardView<'a> {
    pub pointers: &'a [i64],
    pub rows: &'a [i32],
    pub intensities: &'a [f32],
    /// per m/z range of the batch: the shard's float64 reference norms
    pub refnorms: Vec<&'a [f64]>,
    pub start: i64,
    pub id: i32,
    pub count: usize,
}

/// One peak: its ions in ascending m/z with the standard terms.
pub struct Query {
    pub masses: Vec<usize>,
    pub si: Vec<f32>,
    pub w: Vec<f32>,
    pub iw: Vec<f64>,
    pub qnorm: f64,
    pub range: usize,
}

#[derive(Clone, Copy)]
pub struct Entry {
    pub row: i64,
    pub shard: i32,
    pub f: f64,
    pub r: f64,
}

/// The selection of one peak: candidate rows (ascending) with shard and cosines, and the rows only
/// a tie at the k-th place brings in.
pub struct Selection {
    pub rows: Vec<i64>,
    pub shards: Vec<i32>,
    pub forward: Vec<f64>,
    pub reverse: Vec<f64>,
    pub maybe: Vec<i64>,
}

/// The references of one peak that can still be among its k best in either direction.
///
/// `floor_*` is the k-th best value of some subset of the peak's references, so the final k-th
/// best is at least that; an entry strictly below both floors can be neither above nor tied with
/// the final k-th value and is dropped. Every positive value is counted (the standard requires k).
pub struct Pool {
    e: Vec<Entry>,
    npos_f: usize,
    npos_r: usize,
    floor_f: f64,
    floor_r: f64,
    k: usize,
    limit: usize,
}

fn kth_largest(values: &mut Vec<f64>, k: usize) -> Option<f64> {
    if k == 0 || values.len() < k {
        return None;
    }
    let at = values.len() - k;
    let (_, v, _) = values.select_nth_unstable_by(at, |a, b| a.partial_cmp(b).unwrap());
    Some(*v)
}

impl Pool {
    pub fn new(k: usize) -> Self {
        Pool { e: Vec::new(), npos_f: 0, npos_r: 0, floor_f: 0.0, floor_r: 0.0, k, limit: (8 * k).max(4096) }
    }

    #[inline(always)]
    fn keeps(&self, e: &Entry) -> bool {
        (e.f > 0.0 && e.f >= self.floor_f) || (e.r > 0.0 && e.r >= self.floor_r)
    }

    #[inline(always)]
    pub fn push(&mut self, e: Entry) {
        self.npos_f += (e.f > 0.0) as usize;
        self.npos_r += (e.r > 0.0) as usize;
        if self.keeps(&e) {
            self.e.push(e);
            if self.e.len() > self.limit {
                self.prune();
            }
        }
    }

    fn kth(&self, forward: bool) -> Option<f64> {
        let mut v: Vec<f64> = self
            .e
            .iter()
            .map(|e| if forward { e.f } else { e.r })
            .filter(|&x| x > 0.0)
            .collect();
        kth_largest(&mut v, self.k)
    }

    fn prune(&mut self) {
        if let Some(t) = self.kth(true) {
            self.floor_f = self.floor_f.max(t);
        }
        if let Some(t) = self.kth(false) {
            self.floor_r = self.floor_r.max(t);
        }
        let (ff, fr) = (self.floor_f, self.floor_r);
        self.e.retain(|e| (e.f > 0.0 && e.f >= ff) || (e.r > 0.0 && e.r >= fr));
        self.limit = (8 * self.k).max(4096).max(2 * self.e.len());
    }

    /// Merge another pool of the same peak (disjoint references).
    pub fn merge(&mut self, other: Pool) {
        self.npos_f += other.npos_f;
        self.npos_r += other.npos_r;
        self.floor_f = self.floor_f.max(other.floor_f);
        self.floor_r = self.floor_r.max(other.floor_r);
        for e in other.e {
            if self.keeps(&e) {
                self.e.push(e);
            }
        }
        if self.e.len() > self.limit {
            self.prune();
        }
    }

    /// `fast._select`: None when a direction has fewer than k positive values.
    pub fn finish(self) -> Option<Selection> {
        let k = self.k;
        if self.npos_f < k || self.npos_r < k {
            return None;
        }
        let kf = self.kth(true)?;
        let kr = self.kth(false)?;
        let (mut above_f, mut tied_f, mut above_r, mut tied_r) = (0usize, 0usize, 0usize, 0usize);
        for e in &self.e {
            above_f += (e.f > kf) as usize;
            tied_f += (e.f == kf) as usize;
            above_r += (e.r > kr) as usize;
            tied_r += (e.r == kr) as usize;
        }
        let exact_f = above_f + tied_f == k;
        let exact_r = above_r + tied_r == k;
        let mut chosen: Vec<(Entry, bool)> = self
            .e
            .iter()
            .filter(|e| e.f >= kf || e.r >= kr)
            .map(|e| {
                let definite = e.f > kf || (e.f == kf && exact_f) || e.r > kr || (e.r == kr && exact_r);
                (*e, definite)
            })
            .collect();
        chosen.sort_unstable_by_key(|(e, _)| e.row);
        let mut s = Selection {
            rows: Vec::with_capacity(chosen.len()),
            shards: Vec::with_capacity(chosen.len()),
            forward: Vec::with_capacity(chosen.len()),
            reverse: Vec::with_capacity(chosen.len()),
            maybe: Vec::new(),
        };
        for (e, definite) in chosen {
            s.rows.push(e.row);
            s.shards.push(e.shard);
            s.forward.push(e.f);
            s.reverse.push(e.r);
            if !definite {
                s.maybe.push(e.row);
            }
        }
        Some(s)
    }
}

#[inline(always)]
fn cosines(d: f64, rnorm: f64, refnorm: f64, qnorm: f64) -> (f64, f64) {
    let f = if refnorm > 0.0 { d / (qnorm * refnorm).sqrt() } else { 0.0 };
    let p = rnorm * refnorm;
    let r = if p > 0.0 { d / p.sqrt() } else { 0.0 };
    (f, r)
}

/// Variant "sparse": one peak at a time over whole shards (accumulators per reference).
pub fn sparse_query(q: &Query, shards: &[ShardView], k: usize, dots: &mut Vec<f64>, rn: &mut Vec<f64>,
                    touched: &mut Vec<u32>) -> Pool {
    let mut pool = Pool::new(k);
    for s in shards {
        if dots.len() < s.count {
            dots.resize(s.count, 0.0);
            rn.resize(s.count, 0.0);
        }
        touched.clear();
        for (j, &m) in q.masses.iter().enumerate() {
            let a = s.pointers[m] as usize;
            let b = s.pointers[m + 1] as usize;
            let (si, w, iw) = (q.si[j], q.w[j], q.iw[j]);
            let rows = &s.rows[a..b];
            let vals = &s.intensities[a..b];
            for (&r, &v) in rows.iter().zip(vals) {
                let r = r as usize;
                let t = (v.sqrt() * si) * w;
                let x = &mut rn[r];
                if *x == 0.0 {
                    touched.push(r as u32);
                }
                *x += iw;
                dots[r] += t as f64;
            }
        }
        let refnorm = s.refnorms[q.range];
        for &r in touched.iter() {
            let r = r as usize;
            let (d, x) = (dots[r], rn[r]);
            dots[r] = 0.0;
            rn[r] = 0.0;
            let (f, rv) = cosines(d, x, refnorm[r], q.qnorm);
            if f > 0.0 || rv > 0.0 {
                pool.push(Entry { row: s.start + r as i64, shard: s.id, f, r: rv });
            }
        }
    }
    pool
}

/// Variant "tiled2": as `tiled_task`, with the dot product and the reverse norm of a (peak,
/// reference) pair side by side (one cache line), the ions' posting ranges of the whole segment
/// found once, and no bounds checks in the inner loop (every index is checked up front).
pub fn tiled2_task(queries: &[&Query], s: &ShardView, seg0: usize, seg1: usize, tile: usize, k: usize) -> Vec<Pool> {
    let nq = queries.len();
    // a peak's row of cells: a tile plus 4 cells (64 bytes), so rows do not start in the same cache set
    let stride = tile + 4;
    let mut pools: Vec<Pool> = (0..nq).map(|_| Pool::new(k)).collect();
    let mut ions: Vec<(usize, u32, f32, f32, f64)> = Vec::new();
    for (qi, q) in queries.iter().enumerate() {
        for (j, &m) in q.masses.iter().enumerate() {
            ions.push((m, qi as u32, q.si[j], q.w[j], q.iw[j]));
        }
    }
    ions.sort_by_key(|x| (x.0, x.1));
    // flat lists: per ion (posting range in the segment, peaks with it)
    let mut terms: Vec<(u32, f32, f32, f64)> = Vec::with_capacity(ions.len());
    let mut groups: Vec<(usize, usize, usize, usize)> = Vec::new(); // (cursor, end, first term, last term)
    let mut n = 0;
    while n < ions.len() {
        let m = ions[n].0;
        let first = terms.len();
        while n < ions.len() && ions[n].0 == m {
            let (_, qi, si, w, iw) = ions[n];
            terms.push((qi * stride as u32, si, w, iw));
            n += 1;
        }
        let a = s.pointers[m] as usize;
        let b = s.pointers[m + 1] as usize;
        let lo = a + s.rows[a..b].partition_point(|&r| (r as usize) < seg0);
        let hi = a + s.rows[a..b].partition_point(|&r| (r as usize) < seg1);
        if hi > lo {
            groups.push((lo, hi, first, terms.len()));
        }
    }
    let mut acc = vec![[0.0f64; 2]; nq * stride];
    let rows = s.rows;
    let vals = s.intensities;
    let mut t0 = seg0;
    while t0 < seg1 {
        let t1 = (t0 + tile).min(seg1);
        for g in groups.iter_mut() {
            let (mut p, e, f, l) = *g;
            let list = &terms[f..l];
            while p < e {
                // SAFETY: lo <= p < hi <= rows.len() (posting ranges come from the shard's pointers)
                let r = unsafe { *rows.get_unchecked(p) } as usize;
                if r >= t1 {
                    break;
                }
                let v = unsafe { *vals.get_unchecked(p) }.sqrt();
                let off = r - t0;
                for &(base, si, w, iw) in list {
                    let t = (v * si) * w;
                    // SAFETY: base = qi * stride with qi < nq, off < tile < stride
                    let cell = unsafe { acc.get_unchecked_mut(base as usize + off) };
                    cell[0] += t as f64;
                    cell[1] += iw;
                }
                p += 1;
            }
            g.0 = p;
        }
        let width = t1 - t0;
        for (qi, q) in queries.iter().enumerate() {
            let refnorm = &s.refnorms[q.range][t0..t1];
            let cells = &mut acc[qi * stride..qi * stride + width];
            let pool = &mut pools[qi];
            for (off, cell) in cells.iter_mut().enumerate() {
                let x = cell[1];
                if x != 0.0 {
                    let d = cell[0];
                    *cell = [0.0, 0.0];
                    let (f, rv) = cosines(d, x, refnorm[off], q.qnorm);
                    if f > 0.0 || rv > 0.0 {
                        pool.push(Entry { row: s.start + (t0 + off) as i64, shard: s.id, f, r: rv });
                    }
                }
            }
        }
        t0 = t1;
    }
    pools
}

/// Variant "tiled": a chunk of peaks against one segment of one shard, reference tile by tile.
/// Each posting is read once for all peaks of the chunk that have its ion; per (peak, reference)
/// the terms are still added in ascending m/z (the peaks' ions are ascending).
pub fn tiled_task(queries: &[&Query], s: &ShardView, seg0: usize, seg1: usize, tile: usize, k: usize) -> Vec<Pool> {
    let nq = queries.len();
    let mut pools: Vec<Pool> = (0..nq).map(|_| Pool::new(k)).collect();
    // the chunk's ions ascending, each with the peaks that have it
    let mut ions: Vec<(usize, u32, f32, f32, f64)> = Vec::new();
    for (qi, q) in queries.iter().enumerate() {
        for (j, &m) in q.masses.iter().enumerate() {
            ions.push((m, qi as u32, q.si[j], q.w[j], q.iw[j]));
        }
    }
    ions.sort_by_key(|x| (x.0, x.1));
    let mut masses: Vec<usize> = Vec::new();
    let mut lists: Vec<Vec<(usize, f32, f32, f64)>> = Vec::new();
    for &(m, qi, si, w, iw) in &ions {
        if masses.last() != Some(&m) {
            masses.push(m);
            lists.push(Vec::new());
        }
        lists.last_mut().unwrap().push((qi as usize * tile, si, w, iw));
    }
    // posting cursors at the segment start
    let mut cursor: Vec<usize> = Vec::with_capacity(masses.len());
    let mut end: Vec<usize> = Vec::with_capacity(masses.len());
    for &m in &masses {
        let a = s.pointers[m] as usize;
        let b = s.pointers[m + 1] as usize;
        let skip = s.rows[a..b].partition_point(|&r| (r as usize) < seg0);
        cursor.push(a + skip);
        end.push(b);
    }
    let mut dots = vec![0.0f64; nq * tile];
    let mut rn = vec![0.0f64; nq * tile];
    let mut t0 = seg0;
    while t0 < seg1 {
        let t1 = (t0 + tile).min(seg1);
        for (idx, list) in lists.iter().enumerate() {
            let mut p = cursor[idx];
            let e = end[idx];
            while p < e {
                let r = s.rows[p] as usize;
                if r >= t1 {
                    break;
                }
                let v = s.intensities[p].sqrt();
                let off = r - t0;
                for &(base, si, w, iw) in list {
                    let t = (v * si) * w;
                    let ix = base + off;
                    dots[ix] += t as f64;
                    rn[ix] += iw;
                }
                p += 1;
            }
            cursor[idx] = p;
        }
        let width = t1 - t0;
        for (qi, q) in queries.iter().enumerate() {
            let refnorm = s.refnorms[q.range];
            let base = qi * tile;
            let pool = &mut pools[qi];
            for off in 0..width {
                let x = rn[base + off];
                if x != 0.0 {
                    let d = dots[base + off];
                    dots[base + off] = 0.0;
                    rn[base + off] = 0.0;
                    let r = t0 + off;
                    let (f, rv) = cosines(d, x, refnorm[r], q.qnorm);
                    if f > 0.0 || rv > 0.0 {
                        pool.push(Entry { row: s.start + r as i64, shard: s.id, f, r: rv });
                    }
                }
            }
        }
        t0 = t1;
    }
    pools
}

"""EI investigations in the shared unknown register, independent of the GUI.

Each review is append-only and retains the exact floating-point search input.
An EI candidate is never promoted to an assigned identity by this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import uuid

import gc_register as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS ei_investigations (
 investigation_id TEXT PRIMARY KEY,
 entry_id INTEGER NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
 spectrum_id INTEGER REFERENCES spectra(spectrum_id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 decision TEXT NOT NULL,
 candidate_name TEXT NOT NULL,
 candidate_cas TEXT NOT NULL,
 note TEXT NOT NULL,
 next_steps TEXT NOT NULL,
 payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ei_investigations_entry ON ei_investigations(entry_id, created_at);
"""
DECISIONS = ('offen', 'Kandidat', 'verworfen')


def open_db(path):
    path = Path(path)
    # Respect the application's legacy migration gate; never create a competing
    # empty SQLite register when a legacy JSON register awaits migration.
    R.require_migrated(path.with_suffix('.json'), path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = R.connect(path)
    try:
        R.create_schema(con)
        con.executescript(SCHEMA)
    except Exception:
        con.close()
        raise
    return con


def clean_spectrum(points):
    result = []
    for mass, intensity in points:
        mass, intensity = float(mass), float(intensity)
        if not math.isfinite(mass) or not math.isfinite(intensity) or mass <= 0 or intensity < 0:
            raise ValueError('Das Spektrum enthält ungültige m/z- oder Intensitätswerte.')
        if intensity > 0:
            result.append((mass, intensity))
    if not result:
        raise ValueError('Kein gemessenes Spektrum mit positiven Intensitäten vorhanden.')
    return result


def _vector(points):
    bins = {}
    for mass, intensity in clean_spectrum(points):
        nominal = math.floor(mass + 0.5)
        bins[nominal] = bins.get(nominal, 0) + intensity
    return {m: math.sqrt(i) for m, i in bins.items()}


def related_entries(path, points):
    """Compare every stored measured/component spectrum; keep best per entry.

    Square-root cosine over the union of nominal masses, without RT filtering.
    Scores are recognition aids, not EI Atlas scores or automatic identities.
    """
    if not Path(path).is_file():
        return []
    query = _vector(points)
    qnorm = math.sqrt(sum(v*v for v in query.values()))
    con = R.connect(Path(path), create=False)
    try:
        best = {}
        for row in con.execute("SELECT s.*, e.unknown_id, e.label, e.assigned_name, e.status "
                               "FROM spectra s JOIN entries e USING(entry_id) "
                               "WHERE s.kind IN ('measured','component','apex')"):
            try:
                ref = _vector(R.decode_spectrum(row['mz_blob'], row['intensity_blob']))
            except ValueError:
                continue
            score = 100 * sum(v * ref.get(m, 0) for m, v in query.items()) / (
                qnorm * math.sqrt(sum(v*v for v in ref.values())))
            eid = row['entry_id']
            if eid not in best or score > best[eid]['score']:
                best[eid] = dict(entry_id=eid, unknown_id=row['unknown_id'],
                                 name=row['assigned_name'] or row['label'], status=row['status'],
                                 score=min(score, 100), rt=row['rt'], spectrum_id=row['spectrum_id'])
        return sorted(best.values(), key=lambda r: (-r['score'], r['entry_id']))[:15]
    finally:
        con.close()


def history(path, entry_id):
    con = open_db(path)
    try:
        return [dict(row) for row in con.execute(
            'SELECT * FROM ei_investigations WHERE entry_id=? ORDER BY created_at DESC', (int(entry_id),))]
    finally:
        con.close()


def save_investigation(path, snapshot, context, result, *, entry_id=None,
                       spectrum_id=None, candidate_index=None, decision='offen',
                       note='', next_steps='', investigation_id=None):
    points = clean_spectrum(snapshot.get('spectrum') or [])
    if decision not in DECISIONS:
        raise ValueError('Ungültige Kandidatenbewertung.')
    hits = result.get('hits', [])
    if candidate_index is not None and not 0 <= int(candidate_index) < len(hits):
        raise ValueError('Ungültiger Kandidat. Bitte einen Treffer aus dieser Suche wählen.')
    candidate = None if candidate_index is None else hits[int(candidate_index)]
    if decision != 'offen' and candidate is None:
        raise ValueError('Für diese Bewertung muss ein Kandidat gewählt sein.')
    stamp = datetime.now(timezone.utc).isoformat()
    record_id = investigation_id or uuid.uuid4().hex
    payload = dict(schema=1, snapshot=snapshot, context=context, analysis=result,
                   candidate=candidate, candidate_index=candidate_index, decision=decision,
                   note=note, next_steps=next_steps)
    payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    con = open_db(path)
    try:
        with R.writing(con):
            previous = con.execute('SELECT entry_id, spectrum_id FROM ei_investigations WHERE investigation_id=?',
                                   (record_id,)).fetchone()
            if previous:
                entry = R.entry_row(con, previous['entry_id'])
                return dict(entry_id=previous['entry_id'], spectrum_id=previous['spectrum_id'],
                            unknown_id=entry['unknown_id'], investigation_id=record_id)
            ions = R.significant_ions(points)
            identity, ranked = R.identity_key(ions)
            if entry_id is None:
                # Equal strongest-ion lists are only a recognition hint. The
                # analyst explicitly chooses whether to attach or create.
                entry_id = max(int(R.meta_get(con, 'next_id', 1)),
                               con.execute('SELECT COALESCE(MAX(entry_id),0)+1 FROM entries').fetchone()[0])
                mz_key = identity
                if con.execute('SELECT 1 FROM entries WHERE mz_key=?', (mz_key,)).fetchone():
                    mz_key += '#' + str(entry_id)
                label = 'unknown (m/z ' + '/'.join(map(str, ranked[:4])) + ')'
                con.execute("INSERT INTO entries(entry_id,unknown_id,mz_key,identity_key,label,canonical_mz,"
                            "ranked_mz,base_peak,status,search_blob) VALUES(?,?,?,?,?,?,?,?,'offen',?)",
                            (entry_id, f'UNK-{entry_id:04d}', mz_key, identity, label, identity,
                             '/'.join(map(str, ranked)), ranked[0], label.casefold()))
                con.executemany('INSERT INTO entry_ions(entry_id,mz,rank) VALUES(?,?,?)',
                                [(entry_id, m, r) for m, _, r in ions])
                R.meta_set(con, 'next_id', entry_id + 1)
            entry = R.entry_row(con, int(entry_id))
            if not entry:
                raise ValueError('Der gewählte Registereintrag wurde gelöscht. Bitte neu auswählen.')
            sighting_id = None
            if spectrum_id is not None:
                spec = con.execute('SELECT * FROM spectra WHERE spectrum_id=? AND entry_id=?',
                                   (int(spectrum_id), int(entry_id))).fetchone()
                if not spec:
                    raise ValueError('Das gewählte Registerspektrum existiert nicht mehr.')
                # Compare using the register encoding (float32 / integer) while
                # retaining the original floating-point input in payload_json.
                if spec['checksum'] != R.encode_spectrum(points)['checksum']:
                    raise ValueError('Registerspektrum und Suchspektrum stimmen nicht überein.')
                sighting_id = spec['sighting_id']
            else:
                sighting = dict(context, rt=snapshot.get('rt'), name_raw=snapshot.get('name', ''),
                                ranked_mz=ranked)
                sighting.setdefault('date', datetime.now().strftime('%d.%m.%Y'))
                sighting.setdefault('report_type', 'Workspace')
                R._insert_sighting(con, entry_id, sighting)
                rt = sighting['rt']
                sighting_row = con.execute('SELECT sighting_id FROM sightings WHERE entry_id=? '
                    'AND sample_key=? AND report_type_key=? AND rt_key IS ?',
                    (entry_id, str(sighting.get('sample', '')).casefold(),
                     sighting['report_type'].casefold(), round(rt, 4) if rt is not None else None)).fetchone()
                sighting_id = sighting_row[0]
                checksum = R.encode_spectrum(points)['checksum']
                kind = 'component' if snapshot.get('deconvoluted') else 'measured'
                existing = con.execute('SELECT spectrum_id FROM spectra WHERE entry_id=? '
                    'AND sighting_id=? AND checksum=? AND kind=? AND source_path=? AND bg_scan IS ?',
                    (entry_id, sighting_id, checksum, kind, context.get('source_file', ''),
                     snapshot.get('bg_scan'))).fetchone()
                spectrum_id = existing[0] if existing else R.attach_spectrum(
                    con, entry_id, sighting_id, points, source_path=context.get('source_file', ''),
                    apex_scan=context.get('apex_scan'), bg_scan=snapshot.get('bg_scan'),
                    bounds_rule=context.get('bounds_rule', ''), rt=rt, kind=kind)
                if not existing and context.get('tic'):
                    rts, intensities = context['tic']
                    R.attach_tic_slice(con, sighting_id, spectrum_id, rts, intensities, apex_rt=rt)
                sample = str(context.get('sample') or '')
                if sample:
                    con.execute('INSERT OR IGNORE INTO entry_samples(entry_id,sample) VALUES(?,?)', (entry_id, sample))
                con.execute("UPDATE entries SET n_sightings=(SELECT COUNT(*) FROM sightings WHERE entry_id=?), "
                    "n_samples=(SELECT COUNT(DISTINCT sample_key) FROM sightings WHERE entry_id=?), "
                    "rt_mean=(SELECT AVG(rt) FROM sightings WHERE entry_id=?), "
                    "rt_min=(SELECT MIN(rt) FROM sightings WHERE entry_id=?), "
                    "rt_max=(SELECT MAX(rt) FROM sightings WHERE entry_id=?), "
                    "first_seen_iso=(SELECT MIN(date_iso) FROM sightings WHERE entry_id=?), "
                    "last_seen_iso=(SELECT MAX(date_iso) FROM sightings WHERE entry_id=?), "
                    "first_seen=COALESCE((SELECT date_text FROM sightings WHERE entry_id=? AND date_iso IS NOT NULL ORDER BY date_iso LIMIT 1),first_seen), "
                    "last_seen=COALESCE((SELECT date_text FROM sightings WHERE entry_id=? AND date_iso IS NOT NULL ORDER BY date_iso DESC LIMIT 1),last_seen) "
                    "WHERE entry_id=?", [entry_id]*10)
            con.execute('INSERT INTO ei_investigations VALUES(?,?,?,?,?,?,?,?,?,?)',
                (record_id, entry_id, spectrum_id, stamp, decision,
                 (candidate or {}).get('name', ''), (candidate or {}).get('cas', ''), note, next_steps, payload_json))
            R.meta_set(con, 'updated_at', stamp)
        return dict(entry_id=entry_id, spectrum_id=spectrum_id, unknown_id=entry['unknown_id'], investigation_id=record_id)
    finally:
        con.close()

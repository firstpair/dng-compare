#!/usr/bin/env python3
"""Check a dngc archive against docs/dngc-archive-spec.md (dngc-archive-1).

Written from the specification alone: it imports nothing from dng-compare, so a
reading of the format that the implementations share, but the specification
does not, shows up here. Only the standard library is used, and xdelta3 to
decode deltas when rebuilding.

    validate_archive.py ARCHIVE [--rebuild | --sample N] [--reference-root DIR]
                                [--no-payload-hashes] [--json]

Every problem names the rule it breaks (the spec section in brackets). Exit
status 0 means none, 1 problems, 2 a usage error. The archive must be idle:
databases are opened with immutable=1 (spec section 9)."""
import argparse, gzip, hashlib, json, os, pathlib, random, re, sqlite3, subprocess, sys, tempfile, time, zlib

SCHEMA = 'dngc-archive-1'

RULES = {
    'layout':              '[3] the archive and its byte section can be found',
    'manifest':            '[4.1] ARCHIVE.json is an object with the members the spec gives',
    'manifest-schema':     '[4.1] ARCHIVE.json schema is dngc-archive-1',
    'migration-log':       '[4.2] MIGRATION.jsonl lines are known steps, starting with begin',
    'migration-state':     '[4.2] no migration or undo is left half done',
    'tables':              '[5.1] recovery.sqlite3 has the tables and columns of the spec',
    'record-json':         '[5.1] a record is a JSON object, written as json.dumps writes it',
    'record-path':         '[5.2] a record names the relative path it is stored under',
    'record-fields':       '[5.2] a record has every field, with its type',
    'record-kind':         '[5.2] a record is a consistent identical, delta or full record',
    'record-verified':     '[5.2] reconstruction_verified is true',
    'payload-path':        '[5.3] payload paths are relative and named as the spec gives',
    'payload-missing':     '[5.3] every payload a record names exists',
    'payload-hash':        '[5.3] a payload matches the SHA-256 and size its record gives',
    'version-label':       '[5.3] an earlier version is stored under its own label',
    'rebuild':             '[5.5] a record rebuilds its source, checked against source_sha256',
    'reference':           '[5.5] the reference of an identical or delta record exists and matches reference_sha256',
    'report':              '[6] report files are well formed',
    'batch-format':        '[7.1] batches are gzip JSON lines with the fields of the spec',
    'batch-flags':         '[7.1] no_metadata and same_bytes_sha256 rows are consistent',
    'engine':              '[7.2] all metadata records come from one engine',
    'index-tables':        '[7.3] differences.sqlite3 has the tables of the spec',
    'index-batches':       '[7.3] an indexed batch has the digest of its file',
    'index-files':         '[7.3] the index holds one files row per batch row, and no other',
    'index-differences':   '[7.3] the index holds each row\'s differences and values exactly',
    'index-resume':        '[7.3] resume columns match the batch rows',
    'tombstone':           '[8.2] no tombstoned path has a current record',
    'trash':               '[8.1] trashed rows belong to trashed drops, and their payloads exist',
    'drops':               '[4.1, 8] drops in ARCHIVE.json agree with the trash and tombstones',
}

HEX64 = re.compile(r'[0-9a-f]{64}\Z')
LABEL = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{12}\Z')
DROP_ID = re.compile(r'\d{8}T\d{6}Z(-\d+)?\Z')
BATCH = re.compile(r'(phindex-)?\d{6}\.jsonl\.gz\Z')
ENGINE_TAG = 'ExifTool:DngCompareEngine'
RECORD_FIELDS = {
    'relative_path': str, 'source': str, 'reference': (str, type(None)), 'source_identity': list,
    'reference_identity': (list, type(None)), 'source_attrs': dict, 'source_sha256': str,
    'reference_sha256': (str, type(None)), 'original': (str, type(None)), 'whole_file_equal': bool,
    'preserved_bytes': int, 'reconstruction_verified': bool, 'reference_relative_path': (str, type(None)),
}
ATTRS = ('mode', 'uid', 'gid', 'mtime_ns', 'atime_ns', 'flags', 'xattrs')
MIGRATION_OPS = {'begin', 'mkdir', 'move', 'copy', 'done', 'undo-begin', 'unmove', 'remove', 'rmdir', 'undo-done'}


class Problems:
    def __init__(self, max_details):
        self.found, self.max = {}, max_details

    def add(self, rule, detail):
        assert rule in RULES, rule
        self.found.setdefault(rule, []).append(detail)

    def as_json(self):
        return {r: dict(rule=RULES[r], count=len(d), examples=d[:self.max]) for r, d in sorted(self.found.items())}


def ro(path):
    return sqlite3.connect(f'file:{path}?immutable=1', uri=True)


def columns(db, table):
    return [r[1] for r in db.execute(f'PRAGMA table_info({table})')]


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def sha_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def label_of(record):
    t = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(record['source_identity'][2] // 10**9))
    return t + '-' + record['source_sha256'][:12]


def kind_of(r):
    return 'delta' if r.get('delta') else 'full' if r.get('original') else 'identical'


# ---- [3] layout ----

def locate(given):
    p = pathlib.Path(given).absolute()
    if (p / 'recovery.sqlite3').exists() and not (p / 'bytes').exists():
        return p, p, True
    if p.name == 'bytes':
        return p.parent, p, False
    return p, p / 'bytes', False


# ---- [4] ARCHIVE.json, MIGRATION.jsonl ----

def check_manifest(top, out):
    path = top / 'ARCHIVE.json'
    if not path.exists():
        return {'schema': SCHEMA, 'runs': []}
    try:
        m = json.loads(path.read_text())
    except ValueError as e:
        out.add('manifest', f'ARCHIVE.json: {e}')
        return {}
    if not isinstance(m, dict):
        out.add('manifest', 'ARCHIVE.json is not an object')
        return {}
    if m.get('schema') != SCHEMA:
        out.add('manifest-schema', f'schema is {m.get("schema")!r}')
    if not isinstance(m.get('runs'), list):
        out.add('manifest', 'runs is not a list')
    for i, r in enumerate(m.get('runs') or []):
        missing = {'started', 'finished', 'left', 'right', 'pairs', 'bytes', 'metadata', 'impl', 'index', 'exit_statuses'} - set(r)
        if missing:
            out.add('manifest', f'runs[{i}] lacks {sorted(missing)}')
    for i, mg in enumerate(m.get('migrations') or []):
        if not isinstance(mg.get('moved'), list) or 'migrated' not in mg:
            out.add('manifest', f'migrations[{i}] lacks migrated or moved')
    for i, d in enumerate(m.get('drops') or []):
        missing = {'id', 'time', 'state', 'globs', 'listed', 'records', 'earlier_versions', 'payloads', 'payload_bytes'} - set(d)
        if missing:
            out.add('manifest', f'drops[{i}] lacks {sorted(missing)}')
        elif not DROP_ID.match(d['id']) or d['state'] not in ('trashed', 'deleted', 'restored', 'emptied'):
            out.add('manifest', f'drops[{i}]: id {d["id"]!r}, state {d["state"]!r}')
    for i, k in enumerate(m.get('keeps') or []):
        missing = {'time', 'globs', 'listed', 'files', 'bytes', 'errors'} - set(k)
        if missing:
            out.add('manifest', f'keeps[{i}] lacks {sorted(missing)}')
    return m


def check_migration_log(top, out):
    path = top / 'MIGRATION.jsonl'
    if not path.exists():
        return
    entries = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            out.add('migration-log', f'line {n} is not JSON')
            continue
        if e.get('op') not in MIGRATION_OPS:
            out.add('migration-log', f'line {n}: unknown op {e.get("op")!r}')
        if e.get('op') == 'move' and e.get('to') != 'bytes/' + str(e.get('from')):
            out.add('migration-log', f'line {n}: move {e.get("from")!r} to {e.get("to")!r}')
        entries.append(e)
    if entries and entries[0].get('op') != 'begin':
        out.add('migration-log', 'the log does not start with begin')
    begins = [i for i, e in enumerate(entries) if e.get('op') == 'begin']
    if begins:
        ops = {e.get('op') for e in entries[begins[-1]:]}
        if 'undo-begin' in ops and 'undo-done' not in ops:
            out.add('migration-state', 'an undo was begun and not finished')
        elif 'undo-begin' not in ops and 'done' not in ops:
            out.add('migration-state', 'a migration was begun and not finished')


# ---- [5] byte section ----

def check_record(rec_text, path, out, where):
    try:
        r = json.loads(rec_text)
    except ValueError:
        out.add('record-json', f'{where} {path}: not JSON')
        return None
    if not isinstance(r, dict):
        out.add('record-json', f'{where} {path}: not an object')
        return None
    if json.dumps(r) != rec_text:
        out.add('record-json', f'{where} {path}: not in json.dumps form')
    if r.get('relative_path') != path:
        out.add('record-path', f'{where} {path}: relative_path is {r.get("relative_path")!r}')
    bad = [k for k, t in RECORD_FIELDS.items() if k not in r or not isinstance(r[k], t)]
    if bad:
        out.add('record-fields', f'{where} {path}: {bad}')
        return None
    for k in ('source_identity', 'reference_identity'):
        v = r[k]
        if v is not None and (len(v) != 4 or not all(is_int(x) for x in v)):
            out.add('record-fields', f'{where} {path}: {k} is not four integers')
            return None
    if set(ATTRS) - set(r['source_attrs']) or not isinstance(r['source_attrs'].get('xattrs'), dict):
        out.add('record-fields', f'{where} {path}: source_attrs lacks {sorted(set(ATTRS) - set(r["source_attrs"]))}')
    for k in ('source_sha256', 'reference_sha256'):
        if r[k] is not None and not HEX64.match(r[k]):
            out.add('record-fields', f'{where} {path}: {k} is not a SHA-256')
    if r['preserved_bytes'] < 0:
        out.add('record-fields', f'{where} {path}: negative preserved_bytes')
    k = kind_of(r)
    if k == 'delta':
        if not isinstance(r.get('delta_sha256'), str) or not HEX64.match(r['delta_sha256']) \
                or r.get('delta_format') != 'VCDIFF RFC 3284':
            out.add('record-kind', f'{where} {path}: delta record without delta_sha256 or delta_format "VCDIFF RFC 3284"')
        if r['reference'] is None or r['reference_sha256'] is None:
            out.add('record-kind', f'{where} {path}: delta record without a reference')
    elif k == 'identical':
        if not r['whole_file_equal'] or r['reference'] is None or r['reference_sha256'] != r['source_sha256']:
            out.add('record-kind', f'{where} {path}: identical record whose reference does not have the same SHA-256')
        if r['preserved_bytes'] != 0:
            out.add('record-kind', f'{where} {path}: identical record with preserved_bytes {r["preserved_bytes"]}')
    if not r['reconstruction_verified']:
        out.add('record-verified', f'{where} {path}')
    return r


def payload_shape(f, rel, out, where):
    """[5.3] a payload path: relative, inside the section, and named for its path."""
    if f.startswith('/') or '..' in pathlib.PurePosixPath(f).parts:
        out.add('payload-path', f'{where} {rel}: {f}')
        return
    rest = f
    m = re.match(r'versions/([^/]+)/', rest)
    if m:
        if not LABEL.match(m.group(1)):
            out.add('payload-path', f'{where} {rel}: {f} is under no version label')
        rest = rest[m.end():]
    if rest not in ('deltas/' + rel + '.vcdiff', 'originals/' + rel):
        out.add('payload-path', f'{where} {rel}: {f} is not deltas/{rel}.vcdiff or originals/{rel}')


def check_payloads(r, base, fallback, out, where, hashes):
    """[5.3] payloads exist, with their SHA-256 and size; base is where they
    should be, fallback where they MAY still be (an interrupted drop)."""
    rel = r['relative_path']
    for key in ('delta', 'original'):
        f = r.get(key)
        if not f:
            continue
        payload_shape(f, rel, out, where)
        p = base / f
        if not p.exists() and fallback is not None and (fallback / f).exists():
            p = fallback / f
        if not p.exists():
            out.add('payload-missing', f'{where} {rel}: {f}')
            continue
        if p.stat().st_size != r['preserved_bytes']:
            out.add('payload-hash', f'{where} {rel}: {f} has {p.stat().st_size} bytes, the record says {r["preserved_bytes"]}')
        if hashes:
            want = r['delta_sha256'] if key == 'delta' else r['source_sha256']
            if sha_file(p) != want:
                out.add('payload-hash', f'{where} {rel}: {f} SHA-256 differs from the record')


def reference_of(r, reference_root):
    if reference_root and r.get('reference_relative_path'):
        return pathlib.Path(reference_root) / r['reference_relative_path']
    return pathlib.Path(r['reference']) if r.get('reference') else None


def rebuild(r, bytes_dir, reference_root, out, where):
    """[5.5] rebuild one record's source in memory and check its SHA-256."""
    rel, k = r['relative_path'], kind_of(r)
    if k == 'full':
        return  # the payload hash check covers it
    ref = reference_of(r, reference_root)
    if ref is None or not ref.exists():
        out.add('reference', f'{where} {rel}: {ref} is missing')
        return
    if sha_file(ref) != r['reference_sha256']:
        out.add('reference', f'{where} {rel}: {ref} does not match reference_sha256')
        return
    if k == 'identical':
        return  # reference_sha256 == source_sha256 is a record-kind check
    delta = bytes_dir / r['delta']
    if not delta.exists():
        return
    proc = subprocess.run(['xdelta3', '-d', '-c', '-s', str(ref), str(delta)], capture_output=True)
    if proc.returncode or hashlib.sha256(proc.stdout).hexdigest() != r['source_sha256']:
        out.add('rebuild', f'{where} {rel}: the delta does not rebuild the source')


def check_bytes(top, bytes_dir, manifest, args, out):
    rec = bytes_dir / 'recovery.sqlite3'
    if not rec.exists():
        return set()
    db = ro(rec)
    for table, cols in (('files', ['path', 'record']), ('errors', ['path', 'error'])):
        if not has_table(db, table) or columns(db, table) != cols:
            out.add('tables', f'{table}: columns {columns(db, table) if has_table(db, table) else "missing"}')
    optional = {'versions': ['path', 'version', 'record'], 'trash': ['drop_id', 'state', 'path', 'version', 'record'],
                'dropped': ['path', 'drop_id']}
    for table, cols in optional.items():
        if has_table(db, table) and columns(db, table) != cols:
            out.add('tables', f'{table}: columns {columns(db, table)}')
    if not has_table(db, 'files'):
        return set()
    # Streamed from the cursor, so a large archive is checked in little memory.
    chosen = None
    if args.sample is not None:
        total = db.execute('SELECT count(*) FROM files').fetchone()[0]
        chosen = set(random.Random(0).sample(range(total), min(args.sample, total)))
    for i, (p, t) in enumerate(db.execute('SELECT path, record FROM files ORDER BY path')):
        r = check_record(t, p, out, 'files')
        if r is None:
            continue
        if chosen is None or i in chosen:
            check_payloads(r, bytes_dir, None, out, 'files', args.payload_hashes)
            if args.rebuild or chosen is not None:
                rebuild(r, bytes_dir, args.reference_root, out, 'files')
    earlier = db.execute('SELECT path, version, record FROM versions ORDER BY path, version') \
        if has_table(db, 'versions') else []
    for p, v, t in earlier:
        r = check_record(t, p, out, 'versions')
        if r is None:
            continue
        if not LABEL.match(v) or v != label_of(r):
            out.add('version-label', f'{p}: stored as {v!r}, its label is {label_of(r)!r}')
        check_payloads(r, bytes_dir, None, out, 'versions', args.payload_hashes)
    # [8] tombstones and trash
    drops = {d.get('id'): d for d in manifest.get('drops') or []}
    dropped = dict(db.execute('SELECT path, drop_id FROM dropped')) if has_table(db, 'dropped') else {}
    if dropped:
        for p, d in db.execute('SELECT d.path, d.drop_id FROM dropped d JOIN files f ON f.path = d.path'):
            out.add('tombstone', f'{p} has a tombstone (drop {d}) and a current record')
    for p, d in dropped.items():
        if d not in drops:
            out.add('drops', f'tombstone of {p} names drop {d}, which ARCHIVE.json does not list')
    if has_table(db, 'trash'):
        for did, state, p, v, t in db.execute('SELECT drop_id, state, path, version, record FROM trash'):
            if state not in ('current', 'earlier'):
                out.add('trash', f'{p}: state {state!r}')
            if drops.get(did, {}).get('state') != 'trashed':
                out.add('trash', f'{p}: in the trash under drop {did}, which is not a trashed drop')
            r = check_record(t, p, out, 'trash')
            if r is not None:
                check_payloads(r, top / 'trash' / did, bytes_dir, out, 'trash', args.payload_hashes)
    trash_dir = top / 'trash'
    if trash_dir.is_dir():
        for d in sorted(trash_dir.iterdir()):
            if drops.get(d.name, {}).get('state') != 'trashed':
                out.add('drops', f'trash/{d.name} belongs to no trashed drop')
    return set(dropped)


# ---- [6] report ----

def check_report(top, out):
    report = top / 'report'
    if not report.is_dir():
        return
    pairs = report / 'pairs.jsonl'
    if pairs.exists():
        with pairs.open() as f:
            for n, line in enumerate(f, 1):
                try:
                    p = json.loads(line)
                except ValueError:
                    out.add('report', f'pairs.jsonl line {n} is not JSON')
                    continue
                # stamps may be absent only for a pair that failed before stamping, with an error
                if not {'left', 'right'} <= set(p) or ('stamps' not in p and not p.get('error')) or \
                        p.get('raw', {}).get('status') not in (None, 'equal', 'different', 'unverified', 'not_compared'):
                    out.add('report', f'pairs.jsonl line {n}: fields {sorted(p)[:8]}')
    cache = report / 'cache.sqlite3'
    if cache.exists():
        db = ro(cache)
        if not has_table(db, 'results') or columns(db, 'results') != ['key', 'stamps', 'result']:
            out.add('report', 'cache.sqlite3 has no results(key, stamps, result) table')


# ---- [7] metadata ----

def canonical(v):
    return json.dumps(v, sort_keys=True, separators=(',', ':'))


def check_metadata(top, dropped, out):
    """[7] batches, engine, and the index against the batches, one batch at a
    time so that a large archive is checked in little memory."""
    md = top / 'metadata'
    batches_dir = md / 'batches'
    if not batches_dir.is_dir():
        return
    index = md / 'differences.sqlite3'
    db = ro(index) if index.exists() else None
    if db is not None:
        for t in ('batches', 'files', 'fields', 'vals', 'differences', 'coverage'):
            if not has_table(db, t):
                out.add('index-tables', f'no {t} table')
        if not has_table(db, 'files') or not has_table(db, 'differences'):
            db = None
    indexed, cols, resume, trashed_paths = {}, [], False, set()
    groups = iter(())
    if db is not None:
        cols = columns(db, 'files')
        resume = all(c in cols for c in ('apo_size', 'apo_mtime_ns', 'no_metadata', 'same_bytes'))
        indexed = dict(db.execute('SELECT name, sha256 FROM batches'))
        trashed_paths = {p for p, in db.execute('SELECT path FROM trash_files')} if has_table(db, 'trash_files') else set()
        def grouped():
            """files rows one batch at a time, in batch-name order (a second
            connection, so that the per-batch queries do not disturb it)."""
            name, group = None, {}
            for row in ro(index).execute(f'SELECT {", ".join(cols)} FROM files ORDER BY batch, id'):
                f = dict(zip(cols, row))
                if f['batch'] != name:
                    if name is not None:
                        yield name, group
                    name, group = f['batch'], {}
                group[(f['path'], f['version'])] = f
            if name is not None:
                yield name, group
        groups = grouped()
    pending = next(groups, None)
    def files_of(batch):
        """The index's files rows of this batch; rows of batches with no file are reported."""
        nonlocal pending
        while pending is not None and pending[0] < batch:
            out.add('index-files', f'files rows of batch {pending[0]}, which has no batch file')
            pending = next(groups, None)
        if pending is not None and pending[0] == batch:
            rows, pending = pending[1], next(groups, None)
            return rows
        return {}
    engines, seen = set(), set()
    for b in sorted(batches_dir.iterdir()):
        if not BATCH.match(b.name):
            out.add('batch-format', f'{b.name}: not a batch name')
            continue
        seen.add(b.name)
        try:
            data = b.read_bytes()
            rows = [json.loads(l) for l in gzip.decompress(data).decode().splitlines() if l]
        except (OSError, ValueError, EOFError) as e:
            out.add('batch-format', f'{b.name}: {e}')
            continue
        want = {}
        for r in rows:
            where = f'{b.name} {r.get("relative_path")}'
            if r.get('ignored_system_file'):
                continue
            need = {'relative_path', 'apo', 'bumer', 'apo_stamp', 'bumer_stamp', 'apo_metadata', 'bumer_metadata',
                    'differences', 'errors', 'warnings'}
            if need - set(r):
                out.add('batch-format', f'{where}: lacks {sorted(need - set(r))}')
                continue
            for d in r['differences']:
                if d.get('kind') not in ('changed_in_bumer', 'added_in_bumer', 'missing_in_bumer') or \
                        d.get('category') not in ('embedded', 'filesystem'):
                    out.add('batch-format', f'{where}: difference {d.get("tag")!r} kind {d.get("kind")!r}')
                tag = d.get('tag', '')
                if tag == 'SourceFile' or tag.startswith(('ExifTool:', 'Composite:')):
                    out.add('batch-format', f'{where}: compared tag {tag!r}')
            if 'same_bytes_sha256' in r and (r['apo_metadata'] or r['bumer_metadata'] or r['differences']):
                out.add('batch-flags', f'{where}: byte-identical row with records or differences')
            if 'no_metadata' in r and (not set(r['no_metadata']) <= {'apo', 'bumer'} or r['differences']):
                out.add('batch-flags', f'{where}: no_metadata {r["no_metadata"]!r} with differences')
            for side in ('apo_metadata', 'bumer_metadata'):
                if r[side]:
                    engines.add(r[side].get(ENGINE_TAG, 'Perl ExifTool'))
            if pathlib.PurePosixPath(r.get('apo') or '').name != '.DS_Store' and \
                    r['relative_path'] not in dropped and r['relative_path'] not in trashed_paths:
                want[(r['relative_path'], r.get('version', ''))] = r
        if db is None:
            continue
        have = files_of(b.name)
        if b.name not in indexed:
            if have:
                out.add('index-files', f'files rows of batch {b.name}, which is not indexed')
            continue
        if hashlib.sha256(data).hexdigest() != indexed[b.name]:
            out.add('index-batches', f'{b.name}: digest differs from the file')
        for key in sorted(set(want) - set(have)):
            out.add('index-files', f'{b.name}: {key[0]} has no files row')
        for key in sorted(set(have) - set(want)):
            out.add('index-files', f'{b.name}: files row {key[0]} has no batch row')
        ids = [f['id'] for f in have.values()]
        diffs = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for fid, tag, kind, a, z in db.execute(
                    'SELECT d.file_id, t.tag, d.kind, va.sha256, vb.sha256 FROM differences d JOIN fields t ON t.id=d.field_id '
                    'LEFT JOIN vals va ON va.id=d.apo_value LEFT JOIN vals vb ON vb.id=d.bumer_value '
                    f'WHERE d.file_id IN ({",".join("?" * len(chunk))})', chunk):
                diffs.setdefault(fid, set()).add((tag, kind, a, z))
        for key in sorted(set(want) & set(have)):
            r, f = want[key], have[key]
            errors = len(r['errors'])
            compared = int(bool(r['bumer']) and not errors)
            if (f['compared'], f['errors']) != (compared, errors):
                out.add('index-files', f'{b.name}: {key[0]} compared/errors {f["compared"]}/{f["errors"]}, rows say {compared}/{errors}')
            expect = set()
            if compared:
                expect = {(d['tag'], d['kind'],
                           hashlib.sha256(canonical(d['apo']).encode()).hexdigest() if 'apo' in d else None,
                           hashlib.sha256(canonical(d['bumer']).encode()).hexdigest() if 'bumer' in d else None)
                          for d in r['differences']}
            if diffs.get(f['id'], set()) != expect:
                out.add('index-differences', f'{b.name}: {key[0]}')
            if resume and f['no_metadata'] is not None:
                st = r.get('apo_stamp')
                got = (f['apo_size'], f['apo_mtime_ns'], f['no_metadata'], f['same_bytes'])
                exp = (st[1] if st else None, st[2] if st else None, int(bool(r.get('no_metadata'))),
                       int(bool(r.get('same_bytes_sha256'))))
                if got != exp:
                    out.add('index-resume', f'{b.name}: {key[0]} {got} != {exp}')
    if len(engines) > 1:
        out.add('engine', f'records from {sorted(engines)}')
    for name in sorted(set(indexed) - seen):
        out.add('index-batches', f'{name} is indexed but its batch file is missing')
    while pending is not None:
        out.add('index-files', f'files rows of batch {pending[0]}, which has no batch file')
        pending = next(groups, None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('archive')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--rebuild', action='store_true', help='rebuild every record from its reference (reads them all)')
    g.add_argument('--sample', type=int, metavar='N', help='check payloads and rebuild only N records at random')
    ap.add_argument('--reference-root', help='find references here, by reference_relative_path')
    ap.add_argument('--no-payload-hashes', dest='payload_hashes', action='store_false',
                    help='check payload sizes, not their SHA-256')
    ap.add_argument('--max-details', type=int, default=10)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    top, bytes_dir, legacy = locate(a.archive)
    out = Problems(a.max_details)
    if not top.is_dir():
        out.add('layout', f'{top} is not a directory')
    else:
        manifest = check_manifest(top, out)
        check_migration_log(top, out)
        dropped = check_bytes(top, bytes_dir, manifest, a, out)
        check_report(top, out)
        check_metadata(top, dropped, out)
    found = out.as_json()
    if a.json:
        print(json.dumps(dict(archive=str(top), legacy=legacy, problems=found), indent=2))
    else:
        print(f'{top}: {"legacy layout, " if legacy else ""}{"no problems" if not found else f"{sum(v["count"] for v in found.values())} problems"}')
        for rule, v in found.items():
            print(f'  {rule} ({v["count"]}): {v["rule"]}')
            for e in v['examples']:
                print(f'    {e}')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())

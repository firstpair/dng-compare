"""dngc ls, drop, trash and keep: list an archive's records, move some of them
to the archive's trash (or delete them), and turn records into full copies.

A record lives in bytes/recovery.sqlite3, table files (the current content of a
path) or versions (earlier contents). Its payload, a delta or a full copy, is
named relative to the byte section. A drop moves the records into table trash
and the payloads under ARCHIVE/trash/<drop id>/, keeps the metadata index rows
in its own trash tables, and leaves a tombstone per path in table dropped, which
later runs read to skip those paths. --force deletes instead of moving, and
still leaves the tombstones. Every drop and keep is recorded in ARCHIVE.json.
The OCaml front end (ocaml-archive/lib/archive_ops.ml) does the same, with the
same output."""
import fnmatch, json, os, pathlib, shutil, sqlite3, sys, time

from archive_layout import open_readonly

TRASH_DIR = 'trash'
KINDS = ('identical', 'delta', 'full')


def die(msg, status=2):
    print(f'dngc: error: {msg}', file=sys.stderr)
    sys.exit(status)


def count(n, word, plural=None):
    """'1 record', '2 records', with thousands separators."""
    return f'{n:,} {word if n == 1 else plural or word + "s"}'


def kind(record):
    return 'delta' if record.get('delta') else 'full' if record.get('original') else 'identical'


def payloads(record):
    """The byte-section paths of a record's payloads."""
    return [record[k] for k in ('delta', 'original') if record.get(k)]


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def recovery(arc):
    path = arc.bytes / 'recovery.sqlite3'
    if not path.exists():
        die(f'no byte section in {arc.root}')
    return path


def dropped_paths(arc):
    """Relative paths with a tombstone: later runs skip them."""
    path = arc.bytes / 'recovery.sqlite3'
    if not path.exists():
        return set()
    db = open_readonly(path)
    try:
        return {p for p, in db.execute('SELECT path FROM dropped')} if table_exists(db, 'dropped') else set()
    finally:
        db.close()


def read_list(source, nul):
    """Relative paths, one per line (or NUL-separated), from a file or stdin ('-')."""
    text = sys.stdin.buffer.read() if source == '-' else pathlib.Path(source).read_bytes()
    parts = text.split(b'\0' if nul else b'\n')
    return [p.decode('utf-8', 'surrogateescape') for p in parts if p.strip(b'\r')]


def select(db, globs, listed):
    """(path, record) of the current records that match a glob or are listed, by path."""
    rows = []
    for path, text in db.execute('SELECT path, record FROM files ORDER BY path'):
        if (listed is not None and path in listed) or any(fnmatch.fnmatchcase(path, g) for g in globs):
            rows.append((path, json.loads(text)))
    return rows


def changed_paths(arc, tag):
    index = arc.metadata / 'differences.sqlite3'
    if not index.exists():
        die(f'no metadata index in {arc.root}')
    db = open_readonly(index)
    try:
        return {p for p, in db.execute(
            'SELECT DISTINCT f.path FROM differences d JOIN fields t ON t.id=d.field_id JOIN files f ON f.id=d.file_id '
            "WHERE t.tag=? OR t.tag LIKE '%:' || ?", (tag, tag))}
    finally:
        db.close()


def emit(lines, nul):
    out = sys.stdout.buffer
    for line in lines:
        out.write(line.encode('utf-8', 'surrogateescape') + (b'\0' if nul else b'\n'))
    out.flush()


# ---- ls ----

def cmd_ls(arc, a):
    db = open_readonly(recovery(arc))
    if a.trashed:
        rows = ([(p, json.loads(r), d) for p, r, d in
                 db.execute("SELECT path, record, drop_id FROM trash WHERE state='current' ORDER BY path, drop_id")]
                if table_exists(db, 'trash') else [])
    else:
        rows = [(p, json.loads(r), None) for p, r in db.execute('SELECT path, record FROM files ORDER BY path')]
    db.close()
    if a.glob:
        rows = [r for r in rows if any(fnmatch.fnmatchcase(r[0], g) for g in a.glob)]
    if a.kind:
        rows = [r for r in rows if kind(r[1]) == a.kind]
    if a.changed:
        changed = changed_paths(arc, a.changed)
        rows = [r for r in rows if r[0] in changed]
    if a.json:
        emit([json.dumps(dict(path=p, kind=kind(r), **({'drop': d} if d else {}), record=r)) for p, r, d in rows], a.null)
    else:
        key = {'path': None, 'source': 'source', 'reference': 'reference'}[a.print]
        emit([p if key is None else r.get(key) for p, r, _ in rows if key is None or r.get(key)], a.null)
    return 0


# ---- ARCHIVE.json ----

def manifest_append(arc, key, entry):
    m = arc.read_manifest()
    m.setdefault(key, []).append(entry)
    write_manifest(arc, m)


def manifest_update(arc, drop_id, changes):
    m = arc.read_manifest()
    for d in m.get('drops', []):
        if d['id'] == drop_id:
            d.update(changes)
    write_manifest(arc, m)


def write_manifest(arc, m):
    tmp = arc.manifest.with_suffix('.tmp')
    tmp.write_text(json.dumps(m, indent=2) + '\n')
    tmp.replace(arc.manifest)


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def drop_id(arc):
    taken = {d['id'] for d in arc.read_manifest().get('drops', [])}
    base = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    n, candidate = 1, base
    while candidate in taken or (arc.root / TRASH_DIR / candidate).exists():
        n += 1
        candidate = f'{base}-{n}'
    return candidate


def size_of(p):
    try:
        return p.stat().st_size
    except OSError:
        return 0


def prune(directory, stop):
    """Remove empty directories from directory up to, not including, stop."""
    d = pathlib.Path(directory)
    while d != stop and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


# ---- drop ----

def resolve_selection(arc, a, db):
    globs = a.glob or []
    listed = set(read_list(a.from_, a.null)) if a.from_ else None
    if not globs and listed is None:
        die('nothing selected: give --glob PATTERN or --from FILE')
    rows = select(db, globs, listed)
    if listed is not None:
        present = {p for p, _ in rows}
        for p in sorted(listed - present):
            print(f'not in the archive: {p}', file=sys.stderr)
    return rows


def cmd_drop(arc, a, hold_locks):
    path = recovery(arc)
    db = open_readonly(path)
    rows = resolve_selection(arc, a, db)
    earlier = []
    if table_exists(db, 'versions'):
        wanted = {p for p, _ in rows}
        earlier = [(p, v, json.loads(r)) for p, v, r in db.execute('SELECT path, version, record FROM versions ORDER BY path, version')
                   if p in wanted]
    db.close()
    files = [f for _, r in rows for f in payloads(r)] + [f for _, _, r in earlier for f in payloads(r)]
    total = sum(size_of(arc.bytes / f) for f in files)
    counts = (f"{count(len(rows), 'record')} ({count(len(earlier), 'earlier version')}) and "
              f"{count(len(files), 'payload')} ({count(total, 'byte')})")
    if not (a.yes or a.force):
        emit([p for p, _ in rows], False)
        print(f'would move {counts} to the trash; --yes to do it, --force to delete them instead')
        return 0
    if not rows:
        print('nothing to drop')
        return 0
    held = hold_locks(arc)
    did = drop_id(arc)
    db = sqlite3.connect(path)
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS dropped (path TEXT PRIMARY KEY, drop_id TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS trash (drop_id TEXT NOT NULL, state TEXT NOT NULL, path TEXT NOT NULL, '
                   'version TEXT NOT NULL, record TEXT NOT NULL)')
        for p, r in rows:
            if not a.force:
                db.execute('INSERT INTO trash VALUES (?,?,?,?,?)', (did, 'current', p, '', json.dumps(r)))
            db.execute('DELETE FROM files WHERE path=?', (p,))
            db.execute('INSERT OR REPLACE INTO dropped VALUES (?,?)', (p, did))
        for p, v, r in earlier:
            if not a.force:
                db.execute('INSERT INTO trash VALUES (?,?,?,?,?)', (did, 'earlier', p, v, json.dumps(r)))
            db.execute('DELETE FROM versions WHERE path=? AND version=?', (p, v))
    db.close()
    for f in files:
        src = arc.bytes / f
        if not src.exists():
            continue
        if a.force:
            src.unlink()
        else:
            dst = arc.root / TRASH_DIR / did / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
        prune(src.parent, arc.bytes)
    drop_metadata(arc, did, {p for p, _ in rows}, a.force)
    manifest_append(arc, 'drops', dict(id=did, time=now(), state='deleted' if a.force else 'trashed',
                                       globs=a.glob or [], listed=bool(a.from_), records=len(rows),
                                       earlier_versions=len(earlier), payloads=len(files), payload_bytes=total))
    for h in held:
        h.close()
    verb = 'deleted' if a.force else 'moved'
    print(f'drop {did}: {verb} {counts}{"" if a.force else " to the trash"}; later runs skip these paths')
    return 0


def metadata_db(arc):
    index = arc.metadata / 'differences.sqlite3'
    return sqlite3.connect(index) if index.exists() else None


def drop_metadata(arc, did, paths, force):
    """Move (or delete) the metadata index rows of the dropped paths."""
    db = metadata_db(arc)
    if db is None:
        return
    cols = [r[1] for r in db.execute('PRAGMA table_info(files)')]
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS trash_files (drop_id TEXT NOT NULL, path TEXT NOT NULL, '
                   'row TEXT NOT NULL, differences TEXT NOT NULL)')
        for p in sorted(paths):
            for row in db.execute(f'SELECT {", ".join(cols)} FROM files WHERE path=? ORDER BY id', (p,)).fetchall():
                fid = row[cols.index('id')]
                diffs = [list(d) for d in db.execute(
                    'SELECT field_id, kind, apo_value, bumer_value FROM differences WHERE file_id=? ORDER BY rowid', (fid,))]
                if not force:
                    db.execute('INSERT INTO trash_files VALUES (?,?,?,?)',
                               (did, p, json.dumps(dict(zip(cols, row))), json.dumps(diffs)))
                db.execute('DELETE FROM differences WHERE file_id=?', (fid,))
                db.execute('DELETE FROM files WHERE id=?', (fid,))
    db.close()


def apply_tombstones(arc):
    """After the metadata index is rebuilt from the batches: rows of dropped paths go again."""
    dropped = dropped_paths(arc)
    db = metadata_db(arc)
    if db is None or not dropped:
        return
    with db:
        for p in sorted(dropped):
            for fid, in db.execute('SELECT id FROM files WHERE path=?', (p,)).fetchall():
                db.execute('DELETE FROM differences WHERE file_id=?', (fid,))
                db.execute('DELETE FROM files WHERE id=?', (fid,))
    db.close()


# ---- trash ----

def find_drop(arc, did):
    for d in arc.read_manifest().get('drops', []):
        if d['id'] == did:
            return d
    die(f'no drop {did} in {arc.root}', 1)


def cmd_trash(arc, a, hold_locks):
    drops = arc.read_manifest().get('drops', [])
    if a.action == 'list':
        print('drop\tstate\trecords\tearlier\tpayload_bytes\ttime')
        for d in drops:
            print(f"{d['id']}\t{d['state']}\t{d['records']}\t{d['earlier_versions']}\t{d['payload_bytes']}\t{d['time']}")
        return 0
    if a.action == 'empty':
        targets = [find_drop(arc, a.drop)] if a.drop else [d for d in drops if d['state'] == 'trashed']
        targets = [d for d in targets if d['state'] == 'trashed']
        if not a.yes:
            for d in targets:
                print(f"would delete drop {d['id']}: {count(d['records'], 'record')} "
                      f"({count(d['earlier_versions'], 'earlier version')}) and {count(d['payloads'], 'payload')} "
                      f"({count(d['payload_bytes'], 'byte')}); --yes to do it")
            return 0
        held = hold_locks(arc)
        for d in targets:
            db = sqlite3.connect(recovery(arc))
            with db:
                db.execute('DELETE FROM trash WHERE drop_id=?', (d['id'],))
            db.close()
            m = metadata_db(arc)
            if m is not None:
                with m:
                    if table_exists(m, 'trash_files'):
                        m.execute('DELETE FROM trash_files WHERE drop_id=?', (d['id'],))
                m.close()
            shutil.rmtree(arc.root / TRASH_DIR / d['id'], ignore_errors=True)
            prune(arc.root / TRASH_DIR, arc.root)
            manifest_update(arc, d['id'], dict(state='emptied', emptied=now()))
            print(f"drop {d['id']} emptied: {count(d['records'], 'record')} and {count(d['payload_bytes'], 'byte')} deleted; "
                  'later runs still skip these paths')
        for h in held:
            h.close()
        return 0
    if not a.drop:
        die(f'trash {a.action} needs a drop id (see dngc trash ARCHIVE)')
    d = find_drop(arc, a.drop)
    if a.action == 'release':
        held = hold_locks(arc)
        db = sqlite3.connect(recovery(arc))
        with db:
            n = db.execute('DELETE FROM dropped WHERE drop_id=?', (d['id'],)).rowcount if table_exists(db, 'dropped') else 0
        db.close()
        manifest_update(arc, d['id'], dict(released=now()))
        for h in held:
            h.close()
        print(f"drop {d['id']}: {count(n, 'path')} released; later runs preserve them again")
        return 0
    # restore
    if d['state'] != 'trashed':
        die(f"drop {d['id']} is {d['state']}, not in the trash", 1)
    held = hold_locks(arc)
    path = recovery(arc)
    db = sqlite3.connect(path)
    rows = db.execute('SELECT state, path, version, record FROM trash WHERE drop_id=? ORDER BY rowid', (d['id'],)).fetchall()
    clash = [p for s, p, _, _ in rows if s == 'current' and db.execute('SELECT 1 FROM files WHERE path=?', (p,)).fetchone()]
    if clash:
        db.close()
        die(f'{clash[0]} has a newer record; not restoring drop {d["id"]}', 1)
    for _, _, _, r in rows:
        for f in payloads(json.loads(r)):
            src, dst = arc.root / TRASH_DIR / d['id'] / f, arc.bytes / f
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
    with db:
        for s, p, v, r in rows:
            if s == 'current':
                db.execute('INSERT INTO files VALUES (?,?)', (p, r))
            else:
                db.execute('INSERT OR REPLACE INTO versions VALUES (?,?,?)', (p, v, r))
        db.execute('DELETE FROM trash WHERE drop_id=?', (d['id'],))
        db.execute('DELETE FROM dropped WHERE drop_id=?', (d['id'],))
    db.close()
    restore_metadata(arc, d['id'])
    shutil.rmtree(arc.root / TRASH_DIR / d['id'], ignore_errors=True)
    prune(arc.root / TRASH_DIR, arc.root)
    manifest_update(arc, d['id'], dict(state='restored', restored=now()))
    for h in held:
        h.close()
    current = sum(1 for s, *_ in rows if s == 'current')
    print(f"drop {d['id']} restored: {count(current, 'record')} ({count(len(rows) - current, 'earlier version')})")
    return 0


def restore_metadata(arc, did):
    db = metadata_db(arc)
    if db is None:
        return
    with db:
        if table_exists(db, 'trash_files'):
            for row, diffs in db.execute('SELECT row, differences FROM trash_files WHERE drop_id=? ORDER BY rowid',
                                         (did,)).fetchall():
                row = json.loads(row)
                row.pop('id')
                cur = db.execute(f'INSERT INTO files ({", ".join(row)}) VALUES ({", ".join("?" * len(row))})',
                                 list(row.values()))
                for field_id, k, apo, bumer in json.loads(diffs):
                    db.execute('INSERT INTO differences VALUES (?,?,?,?,?)', (cur.lastrowid, field_id, k, apo, bumer))
            db.execute('DELETE FROM trash_files WHERE drop_id=?', (did,))
    db.close()


# ---- keep ----

def cmd_keep(arc, a, hold_locks, reconstruct):
    path = recovery(arc)
    db = open_readonly(path)
    rows = resolve_selection(arc, a, db)
    db.close()
    todo = [(p, r) for p, r in rows if kind(r) != 'full']
    full = len(rows) - len(todo)
    total = sum(r['source_identity'][1] for _, r in todo)
    if not a.yes:
        emit([p for p, _ in todo], False)
        print(f"would keep {count(len(todo), 'file')} ({count(total, 'byte')}) as full copies in the archive; "
              f"{count(full, 'file')} already {'is' if full == 1 else 'are'}; --yes to do it")
        return 0
    held = hold_locks(arc)
    kept = errors = 0
    kept_bytes = 0
    db = sqlite3.connect(path)
    for p, r in todo:
        target = arc.bytes / 'originals' / p
        try:
            if target.exists():
                raise FileExistsError(f'{target} exists')
            target.parent.mkdir(parents=True, exist_ok=True)
            reconstruct(r, arc.bytes, target)
        except Exception as e:
            print(f'{p}: {e}', file=sys.stderr)
            errors += 1
            continue
        delta = r.get('delta')
        r['original'] = f'originals/{p}'
        r['preserved_bytes'] = target.stat().st_size
        for k in ('delta', 'delta_sha256', 'delta_format'):
            if k in r:
                r[k] = None
        with db:
            db.execute('UPDATE files SET record=? WHERE path=?', (json.dumps(r), p))
        if delta and (arc.bytes / delta).exists():
            (arc.bytes / delta).unlink()
            prune((arc.bytes / delta).parent, arc.bytes)
        kept += 1
        kept_bytes += r['preserved_bytes']
    db.close()
    manifest_append(arc, 'keeps', dict(time=now(), globs=a.glob or [], listed=bool(a.from_),
                                       files=kept, bytes=kept_bytes, errors=errors))
    for h in held:
        h.close()
    print(f"kept {count(kept, 'file')} ({count(kept_bytes, 'byte')}) as full copies; "
          f"{count(full, 'file')} already {'was' if full == 1 else 'were'}; {count(errors, 'error')}")
    return 1 if errors else 0

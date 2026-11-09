#!/usr/bin/env python3
"""dngc: one command for dng-compare's comparison, preservation and metadata tools.

    dngc run LEFT RIGHT ARCHIVE [--pairs FILE] [--bytes delta|full|none]
             [--metadata exiftool|phindex|none] [--index LEFT.sqlite RIGHT.sqlite]
             [--workers N] [--impl ocaml|rust|python] [--max-mib R]
    dngc restore ARCHIVE RELATIVE_PATH OUTPUT [--version LABEL]
    dngc versions ARCHIVE [RELATIVE_PATH]
    dngc index ARCHIVE
    dngc query ARCHIVE [changes | files TAG | values TAG | SQL]
    dngc verify ARCHIVE
    dngc status ARCHIVE
    dngc migrate ARCHIVE [--dry-run] [--report DIR] [--undo] [--no-verify]

An archive is one directory with optional sections (docs/dngc.md):
report/ (comparison), bytes/ (preservation), metadata/ (searchable metadata),
and ARCHIVE.json describing the runs. Archives written before dngc, with
recovery.sqlite3 at the top, are read as they are.

dngc runs the existing tools: `dng-archive` (OxCaml, the default), the Rust
`dng-compare-all`, or the Python modules, which stay the reference.
"""
import argparse, fcntl, gzip, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, time

from archive_layout import archive_top, byte_section, open_readonly, version_label

HERE = pathlib.Path(__file__).resolve().parent
VENV_PYTHON = HERE / '.venv/bin/python'
SCHEMA = 'dngc-archive-1'


def python():
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def ocaml_binary():
    candidates = [os.environ.get('DNG_ARCHIVE'), HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/main.exe',
                  *sorted(HERE.glob('ocaml-archive/_build*/default/bin/main.exe')), shutil.which('dng-archive')]
    return next((str(c) for c in candidates if c and os.access(c, os.X_OK)), None)


def rust_binary():
    c = os.environ.get('DNG_COMPARE_ALL_RUST') or HERE / 'rust-scanner/target/release/dng-compare-all'
    return str(c) if os.access(c, os.X_OK) else None


def die(msg, status=2):
    print(f'dngc: error: {msg}', file=sys.stderr)
    sys.exit(status)


def call(command, log=None):
    """Run a tool, passing its output through; returns its exit status."""
    print('+ ' + ' '.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), cwd=HERE).returncode


# ---- Archive layout ----

class Archive:
    def __init__(self, root):
        # The sections by the rule every tool shares (archive_layout.py): archives
        # from before dngc keep their byte section at the top; ARCHIVE/bytes given
        # directly is the byte section of its parent.
        given = pathlib.Path(root).absolute()
        self.root = archive_top(given)
        self.bytes = byte_section(given)
        self.legacy = self.bytes == self.root
        self.report = self.root / 'report'
        self.metadata = self.root / 'metadata'
        self.manifest = self.root / 'ARCHIVE.json'

    def read_manifest(self):
        return json.loads(self.manifest.read_text()) if self.manifest.exists() else {'schema': SCHEMA, 'runs': []}

    def record_run(self, entry):
        m = self.read_manifest()
        m['runs'].append(entry)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest.with_suffix('.tmp')
        tmp.write_text(json.dumps(m, indent=2) + '\n')
        tmp.replace(self.manifest)


def phindex_binary():
    candidates = [os.environ.get('PHINDEX'), HERE.parent / 'phindex/rust/target/release/phindex', shutil.which('phindex')]
    return next((str(c) for c in candidates if c and os.access(c, os.X_OK)), None)


def walk_files(root, progress_secs=30):
    """Every regular file under root, relative, sorted; symlinks and .DS_Store are left out.
    Prints a count every progress_secs, and the total at the end."""
    out, folders = [], 0
    start = last = time.monotonic()
    for d, dirs, names in os.walk(root, onerror=lambda e: (_ for _ in ()).throw(e)):
        dirs.sort()
        folders += 1
        now = time.monotonic()
        if now - last >= progress_secs:
            last = now
            print(f'  walked {len(out):,} files in {folders:,} folders under {root} '
                  f'({now - start:.0f}s; now in {os.path.relpath(d, root)})', flush=True)
        for n in sorted(names):
            p = os.path.join(d, n)
            if n != '.DS_Store' and os.path.isfile(p) and not os.path.islink(p):
                out.append(os.path.relpath(p, root))
    print(f'  walked {len(out):,} files under {root} in {time.monotonic() - start:.0f}s', flush=True)
    return out


def whole_tree_pairs(left, right, index, tmp):
    """Every file under LEFT with its counterpart under RIGHT, or None.

    With phindex indexes, `phindex align` matches by content (moved and renamed
    files too); otherwise the same relative path, then a file name that is
    unique under RIGHT, as compare_dng.py matches DNGs."""
    left, right = os.path.realpath(left), os.path.realpath(right)
    if index:
        binary = phindex_binary()
        if not binary:
            die('--index needs the phindex binary (PHINDEX, a sibling phindex checkout, or PATH)')
        out = tmp / 'align'
        if call([binary, 'align', '--left', index[0], '--left-root', left, '--right', index[1],
                 '--right-root', right, '--out', out]):
            die('phindex align failed', 1)
        pairs = []
        with open(out / 'files.tsv') as f:
            next(f)
            for line in f:
                l, r, tier, _ = line.rstrip('\n').split('\t')
                if tier == 'not-indexed':
                    die(f'{l}: not in the left phindex index; index LEFT first', 1)
                pairs.append((os.path.join(left, l), os.path.join(right, r) if r else None))
        known = {p for p, _ in pairs}
        print(f'Checking that every file under {left} is in its index ...', flush=True)
        missing = [f for f in walk_files(left) if os.path.join(left, f) not in known]
        if missing:
            die(f'{len(missing)} files under LEFT are not in its phindex index (e.g. {missing[0]}); index LEFT first', 1)
        return pairs
    print(f'Walking {left} and {right} ...', flush=True)
    lefts, rights = walk_files(left), walk_files(right)
    right_set = set(rights)
    by_name = {}
    for r in rights:
        by_name.setdefault(os.path.basename(r), []).append(r)
    pairs = []
    for l in lefts:
        if l in right_set:
            r = l
        else:
            candidates = by_name.get(os.path.basename(l), [])
            r = candidates[0] if len(candidates) == 1 else None
        pairs.append((os.path.join(left, l), os.path.join(right, r) if r else None))
    return pairs


def read_pairs(path):
    pairs = []
    for line in pathlib.Path(path).read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            a, b = line.split('\t')
            pairs.append((a, b))
    return pairs


# ---- run ----

def cmd_run(a):
    arc = Archive(a.archive)
    if a.bytes == 'none' and a.metadata == 'none':
        die('nothing to do: --bytes none and --metadata none')
    if a.metadata == 'phindex' and not a.index:
        die('--metadata phindex needs --index LEFT.sqlite RIGHT.sqlite')

    for p in a.index or []:
        if not pathlib.Path(p).is_file():
            die(f'phindex index not found: {p}')
    rate = str(a.max_mib or 100000)
    given_pairs = os.path.abspath(a.pairs) if a.pairs else None
    started = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    statuses = {}
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-'))
    try:
        if a.pairs:
            pairs = read_pairs(a.pairs)
        else:
            # An emptied source is an empty directory: covered with 0 files.
            if not os.path.isdir(a.left):
                die(f'{a.left}: no such directory; an emptied source must still exist (empty) for a whole-tree run')
            pairs = whole_tree_pairs(a.left, a.right, a.index, tmp)
            listed = tmp / 'whole-tree-pairs.tsv'
            listed.write_text(''.join(f'{l}\t{r}\n' for l, r in pairs if r))
            a.pairs = str(listed)  # the metadata step reads the matched pairs
            print(f'{len(pairs):,} files under LEFT; {sum(1 for _, r in pairs if r):,} with a counterpart', flush=True)
        is_dng = lambda p: p is not None and p.lower().endswith('.dng')
        dng = [p for p in pairs if is_dng(p[0]) and is_dng(p[1])]
        dng_set = set(dng)
        other = [p for p in pairs if p not in dng_set]
        # A repeated run trusts the list it checked before and reuses everything still valid.
        repeat = (arc.report / 'pairs.jsonl').exists()
        engine_metadata = a.metadata == 'exiftool'
        index_args = ['--left-index', a.index[0], '--right-index', a.index[1]] if a.index else []
        if dng:
            dng_file = tmp / 'dng-pairs.tsv'
            dng_file.write_text(''.join(f'{l}\t{r}\n' for l, r in dng))
            if a.impl in ('ocaml', 'rust'):
                binary = ocaml_binary() if a.impl == 'ocaml' else rust_binary()
                if not binary:
                    die(f'no {a.impl} build; see docs/native-all.md')
                if a.bytes == 'full':
                    die('--bytes full needs --impl python')
                command = [binary, *(['all'] if a.impl == 'ocaml' else []), a.left, a.right, '--pairs', dng_file,
                           '--report', arc.report,
                           *(['--archive', arc.bytes] if a.bytes == 'delta' else ['--compare-only']),
                           '--workers', a.workers, '--exiftool-processes', a.workers, '--batch-size', 128,
                           '--progress-secs', 60, '--rate-mib', rate, *index_args,
                           *(['--trust-pairs'] if repeat else []), *([] if engine_metadata else ['--skip-metadata'])]
            else:
                command = [python(), HERE / 'compare_dng.py', a.left, a.right, '--output', arc.report,
                           '--workers', a.workers, '--exiftool-processes', a.workers, '--batch-size', 128,
                           '--progress-secs', 60,
                           '--pairs-only', '--pairs', dng_file,
                           *(['--archive', arc.bytes] if a.bytes != 'none' else ['--compare-only'])]
                if a.index:
                    command += index_args
                if a.bytes == 'full':
                    die('--bytes full is not wired yet; use preserve_apo.py scan --full-copy')
            statuses['compare_and_preserve'] = call(command)
            if statuses['compare_and_preserve'] == 130:
                return 130
        if other and a.bytes != 'none':
            jsonl = tmp / 'other-pairs.jsonl'
            # A file with no counterpart is listed without a reference: kept as a full copy.
            jsonl.write_text(''.join(json.dumps({'source': os.path.join(os.path.abspath(a.left), l),
                                                 'reference': os.path.join(os.path.abspath(a.right), r) if r else None}) + '\n'
                                     for l, r in other))
            command = [python(), HERE / 'preserve_apo.py', 'scan', a.left, a.right, arc.bytes, '--delta',
                       '--pairs', jsonl, '--workers', min(int(a.workers), 36), '--rate-mib', rate]
            if a.index:
                command += ['--source-index', a.index[0], '--reference-index', a.index[1]]
            statuses['preserve_other'] = call(command)
        if a.metadata == 'phindex':
            all_pairs = tmp / 'all-pairs.tsv'
            all_pairs.write_text(pathlib.Path(a.pairs).read_text())
            binary = ocaml_binary() if a.impl == 'ocaml' else None
            command = ([binary, 'phindex-metadata'] if binary else [python(), HERE / 'phindex_metadata.py']) + \
                [a.left, a.right, arc.root, '--pairs', all_pairs, '--left-index', a.index[0], '--right-index', a.index[1]]
            statuses['metadata'] = call(command)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    arc.record_run(dict(started=started, finished=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        left=os.path.abspath(a.left), right=os.path.abspath(a.right),
                        pairs=given_pairs or 'whole tree', bytes=a.bytes, metadata=a.metadata,
                        impl=a.impl, index=[os.path.abspath(p) for p in a.index] if a.index else None,
                        exit_statuses=statuses))
    return max(statuses.values(), default=0)


# ---- restore, index, query ----

def cmd_restore(a):
    arc = Archive(a.archive)
    return call([python(), HERE / 'preserve_apo.py', 'restore', arc.bytes, a.relative_path, a.output,
                 *(['--version', a.version] if a.version else [])])


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def cmd_versions(a):
    """Every preserved content of each relative path: the current record and
    the earlier ones a refilled source left (restore --version LABEL)."""
    arc = Archive(a.archive)
    path = arc.bytes / 'recovery.sqlite3'
    if not path.exists():
        die(f'no byte section in {a.archive}')
    db = open_readonly(path)
    where, args = (' WHERE path=?', (a.relative_path,)) if a.relative_path else ('', ())
    rows = [(p, version_label(json.loads(r)), 'current') for p, r in db.execute('SELECT path, record FROM files' + where, args)]
    if table_exists(db, 'versions'):
        rows += [(p, v, 'earlier') for p, v in db.execute('SELECT path, version FROM versions' + where, args)]
    print('path\tversion\tstate')
    for row in sorted(rows):
        print('\t'.join(row))
    return 0


def cmd_index(a):
    arc = Archive(a.archive)
    binary = ocaml_binary()
    return call([binary, 'index', arc.root] if binary else [python(), HERE / 'metadata_index.py', arc.root])


PRESETS = {
    'changes': 'SELECT tag, category, kind, affected_files, percent_compared FROM field_changes ORDER BY affected_files DESC LIMIT 50',
    'files': "SELECT f.path, d.kind FROM differences d JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id "
             "WHERE t.tag=? ORDER BY f.path",
    'values': "SELECT f.path, d.kind, a.json_zlib, b.json_zlib FROM differences d JOIN files f ON f.id=d.file_id "
              "JOIN fields t ON t.id=d.field_id LEFT JOIN vals a ON a.id=d.apo_value LEFT JOIN vals b ON b.id=d.bumer_value "
              "WHERE t.tag=? ORDER BY f.path",
}


def cmd_query(a):
    import zlib
    db_path = Archive(a.archive).metadata / 'differences.sqlite3'
    if not db_path.exists():
        die(f'no metadata index in {a.archive}; run `dngc run ... --metadata` or `dngc index`')
    db = open_readonly(db_path)
    words = a.query or ['changes']
    if words[0] in PRESETS:
        rows = db.execute(PRESETS[words[0]], words[1:2] if words[0] != 'changes' else [])
    else:
        rows = db.execute(' '.join(words))
    decode = lambda v: json.loads(zlib.decompress(v)) if isinstance(v, bytes) else v
    names = [d[0] for d in rows.description]
    print('\t'.join(names))
    for row in rows:
        print('\t'.join(json.dumps(decode(v), ensure_ascii=False) if isinstance(v, bytes) else str(v) for v in row))
    return 0


# ---- status, verify ----

def sections(arc):
    out = {}
    if (arc.report / 'summary.json').exists():
        s = json.loads((arc.report / 'summary.json').read_text())
        out['report'] = dict(complete=s.get('complete'), pairs=s.get('counts', {}).get('pairs_reported'),
                             raw_equal=s.get('counts', {}).get('raw_equal'), selected=s.get('selected_pairs'))
    if (arc.bytes / 'recovery.sqlite3').exists():
        s = json.loads((arc.bytes / 'summary.json').read_text()) if (arc.bytes / 'summary.json').exists() else {}
        db = open_readonly(arc.bytes / "recovery.sqlite3")
        out['bytes'] = dict(files=db.execute('SELECT count(*) FROM files').fetchone()[0],
                            errors=db.execute('SELECT count(*) FROM errors').fetchone()[0],
                            earlier_versions=db.execute('SELECT count(*) FROM versions').fetchone()[0]
                            if table_exists(db, 'versions') else 0,
                            last_run_total=s.get('total_files'), last_run_complete=s.get('byte_preservation_complete'),
                            layout='legacy (top level)' if arc.legacy else 'bytes/')
    if (arc.metadata / 'differences.sqlite3').exists():
        db = open_readonly(arc.metadata / "differences.sqlite3")
        prov = arc.metadata / 'provenance.json'
        top = arc.root / 'provenance.json'
        engine = (json.loads(prov.read_text()).get('engine') if prov.exists() else
                  json.loads(top.read_text()).get('engine') if top.exists() else None)
        out['metadata'] = dict(files=db.execute('SELECT count(*) FROM files').fetchone()[0],
                               differences=db.execute('SELECT count(*) FROM differences').fetchone()[0],
                               engine=engine or 'Perl ExifTool (no engine stamp)')
    return out


def cmd_status(a):
    arc = Archive(a.archive)
    if not arc.root.is_dir():
        die(f'{arc.root}: no such archive')
    print(json.dumps(dict(archive=str(arc.root), sections=sections(arc),
                          runs=len(arc.read_manifest()['runs'])), indent=2))
    return 0


def cmd_verify(a):
    arc = Archive(a.archive)
    problems = []
    s = sections(arc)
    if 'bytes' in s:
        b = s['bytes']
        if b['errors']:
            problems.append(f"bytes: {b['errors']} files with preservation errors")
        db = open_readonly(arc.bytes / "recovery.sqlite3")
        missing = 0
        # Payloads of the current records and of the earlier versions; the source is never read.
        records = list(db.execute('SELECT path, record FROM files'))
        if table_exists(db, 'versions'):
            records += list(db.execute('SELECT path, record FROM versions'))
        for path, record in records:
            r = json.loads(record)
            for key in ('delta', 'original'):
                if r.get(key) and not (arc.bytes / r[key]).exists():
                    missing += 1
        if missing:
            problems.append(f'bytes: {missing} deltas or originals named in records are missing')
    if 'metadata' in s:
        db = open_readonly(arc.metadata / "differences.sqlite3")
        ok = db.execute('PRAGMA quick_check').fetchone()[0]
        if ok != 'ok':
            problems.append(f'metadata index: {ok}')
    if 'report' in s and not s['report'].get('complete'):
        problems.append('report: the comparison has not finished')
    print(json.dumps(dict(archive=str(arc.root), sections=sorted(s), problems=problems), indent=2))
    if a.restore_sample and 'bytes' in s:
        db = open_readonly(arc.bytes / "recovery.sqlite3")
        paths = [p for p, in db.execute('SELECT path FROM files ORDER BY random() LIMIT ?', (a.restore_sample,))]
        with tempfile.TemporaryDirectory() as td:
            for i, p in enumerate(paths):
                if call([python(), HERE / 'preserve_apo.py', 'restore', arc.bytes, p, pathlib.Path(td) / str(i)]):
                    problems.append(f'restore failed: {p}')
    return 1 if problems else 0


# ---- migrate ----

# What the byte tools (preserve_apo.py, dng-archive preserve/all, compare_dng.py's
# archive step, preserve_years.py, finalize_year.py, database_backend.py) write at
# the top of a legacy archive: the byte section, moved into bytes/.
BYTE_ENTRIES = ['recovery.sqlite3', 'recovery.sqlite3-wal', 'recovery.sqlite3-shm', 'recovery.sqlite3-journal',
                'recovery.sqlite3.engine.json', 'recovery.sqlite3.engine.tmp', 'recovery.sqlite3.before-turso',
                'deltas', 'originals', 'manifest.jsonl', 'inventory.json', 'directories.json', 'summary.json',
                'inventory.json.tmp', 'directories.json.tmp', 'summary.json.tmp', 'matching.json',
                'xmp-original', 'image-comparison', 'finalization-backend.json', '.byte-preservation.lock',
                'versions']
# The flocks the tools hold while they write (preserve_apo.archive_lock, compare_dng.py's report lock).
LOCKS = ['.byte-preservation.lock', '.metadata-preservation.lock', 'bytes/.byte-preservation.lock', 'report/.lock']
MIGRATION_LOG = 'MIGRATION.jsonl'


def write_manifest(arc, m):
    arc.root.mkdir(parents=True, exist_ok=True)
    tmp = arc.manifest.with_suffix('.tmp')
    tmp.write_text(json.dumps(m, indent=2) + '\n')
    tmp.replace(arc.manifest)


def migration_state(arc):
    """The entries of the log since its last begin, and the state they leave:
    None, migrating, migrated, undoing or undone."""
    log = arc.root / MIGRATION_LOG
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
    begins = [i for i, e in enumerate(entries) if e['op'] == 'begin']
    if not begins:
        return None, []
    current = entries[begins[-1]:]
    ops = {e['op'] for e in current}
    state = ('undone' if 'undo-done' in ops else 'undoing' if 'undo-begin' in ops else
             'migrated' if 'done' in ops else 'migrating')
    return state, current


def log_step(arc, entry):
    """One line per step, on disk before the step happens."""
    with open(arc.root / MIGRATION_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n'); f.flush(); os.fsync(f.fileno())


def hold_locks(arc):
    """Every tool lock in the archive, taken without waiting; refuses while one is held."""
    held = []
    for name in LOCKS:
        p = arc.root / name
        if not p.is_file():
            continue
        f = open(p, 'rb')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            for h in held:
                h.close()
            die(f'{name} is held: another process is writing this archive', 1)
        held.append(f)
    if (arc.root / '.running').exists():
        die('.running exists: preserve_years.py is writing this archive', 1)
    for d in ('', 'bytes/'):
        marker = arc.root / (d + 'recovery.sqlite3.engine.json')
        pid = json.loads(marker.read_text()).get('active_pid') if marker.exists() else None
        if pid and pid != os.getpid():
            try:
                os.kill(pid, 0)
                die(f'{d}recovery.sqlite3 is open in Turso MVCC by process {pid}', 1)
            except ProcessLookupError:
                pass
    for d in ('', 'bytes/'):
        wal = arc.root / (d + 'recovery.sqlite3-wal')
        if wal.exists() and wal.stat().st_size and not (arc.root / (d + '.byte-preservation.lock')).exists():
            die(f'{d}recovery.sqlite3-wal is not empty and there is no {d}.byte-preservation.lock '
                'to show that no writer is active', 1)
    return held


def migration_summary(arc):
    """Sources, engine and counts from the archive's own records."""
    def read(p):
        try:
            v = json.loads(p.read_text()) if p.exists() else {}
        except ValueError:
            v = {}
        return v if isinstance(v, dict) else {}
    prov, mprov = read(arc.root / 'provenance.json'), read(arc.root / 'metadata/provenance.json')
    s = read(arc.root / 'bytes/summary.json')
    runs = arc.root / 'runs.jsonl'
    return dict(source=s.get('source') or prov.get('source'), reference=s.get('reference') or prov.get('reference'),
                engine=mprov.get('engine') or prov.get('engine'), total_files=s.get('total_files'),
                verified_files=s.get('verified_files'), errors=s.get('errors'),
                byte_preservation_complete=s.get('byte_preservation_complete'),
                runs_logged=sum(1 for l in runs.read_text().splitlines() if l.strip()) if runs.exists() else None,
                metadata_files=open_readonly(index).execute('SELECT count(*) FROM files').fetchone()[0]
                if (index := arc.root / 'metadata/differences.sqlite3').exists() else None)


def migrate_undo(arc, dry_run):
    root = arc.root
    state, entries = migration_state(arc)
    if state not in ('migrating', 'migrated', 'undoing'):
        die(f'{root}: {MIGRATION_LOG} records no migration to undo', 1)
    held = hold_locks(arc)
    plan = []
    for e in reversed([e for e in entries if e['op'] == 'move']):
        src, dst = root / e['to'], root / e['from']
        if os.path.lexists(src) and not os.path.lexists(dst):
            plan.append(('move', e['to'], e['from']))
        elif os.path.lexists(dst) and not os.path.lexists(src):
            continue  # not moved, or moved back already
        else:
            die(f"cannot undo the move of {e['from']}: {e['to']} and {e['from']} "
                f"{'both exist' if os.path.lexists(src) else 'are both missing'}", 1)
    if any(e['op'] == 'copy' for e in entries):
        plan += [('remove', n) for n in ('report.migrating', 'report') if os.path.lexists(root / n)]
    if any(e['op'] == 'mkdir' for e in entries) and (root / 'bytes').is_dir():
        plan.append(('rmdir', 'bytes'))
    # ARCHIVE.json that a metadata-only migration wrote; the log keeps the record.
    manifest_made = any(e['op'] == 'manifest' for e in entries)
    if manifest_made and arc.manifest.exists():
        plan.append(('remove', arc.manifest.name))
    print(f'dngc migrate --undo {root}' + (' (dry run: nothing is changed)' if dry_run else ''), flush=True)
    if not dry_run and state != 'undoing':
        log_step(arc, dict(op='undo-begin', time=time.strftime('%Y-%m-%dT%H:%M:%S%z')))
    for step in plan:
        print(f'move {step[1]} -> {step[2]}' if step[0] == 'move' else f'{step[0]} {step[1]}', flush=True)
        if dry_run:
            continue
        if step[0] == 'move':
            log_step(arc, dict(op='unmove', **{'from': step[1], 'to': step[2]}))
            os.rename(root / step[1], root / step[2])
        elif step[0] == 'remove':
            log_step(arc, dict(op='remove', path=step[1]))
            (root / step[1]).unlink() if step[1] == arc.manifest.name else shutil.rmtree(root / step[1])
        elif os.listdir(root / 'bytes'):
            print('bytes/ is not empty; left in place', flush=True)
        else:
            log_step(arc, dict(op='rmdir', path='bytes'))
            os.rmdir(root / 'bytes')
    if dry_run:
        return 0
    if manifest_made:
        log_step(arc, dict(op='undo-done', time=time.strftime('%Y-%m-%dT%H:%M:%S%z')))
        for h in held:
            h.close()
        print(f'Undone: {arc.manifest.name} removed', flush=True)
        return 0
    m = arc.read_manifest()
    m.setdefault('migrations', []).append(dict(undone=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                               restored=[s[2] for s in plan if s[0] == 'move'],
                                               report_removed=any(s == ('remove', 'report') for s in plan)))
    write_manifest(arc, m)
    log_step(arc, dict(op='undo-done', time=time.strftime('%Y-%m-%dT%H:%M:%S%z')))
    for h in held:
        h.close()
    print(f'Undone: {sum(1 for s in plan if s[0] == "move")} paths moved back', flush=True)
    return 0


def cmd_migrate(a):
    arc = Archive(a.archive)
    root = arc.root
    if not root.is_dir():
        die(f'{root}: no such archive')
    if a.undo:
        if a.report:
            die('--report does not go with --undo')
        return migrate_undo(arc, a.dry_run)
    state, entries = migration_state(arc)
    if state == 'undoing':
        die(f'an undo was interrupted; finish it with dngc migrate --undo {root}', 1)
    resuming = state == 'migrating'
    report = os.path.abspath(a.report) if a.report else None
    has_bytes = True  # False: a metadata-only archive, which only gains ARCHIVE.json
    if resuming:
        has_bytes = entries[0].get('byte_section', True)
        logged = entries[0].get('report')
        if report and report != logged:
            die(f'the interrupted migration was started with --report {logged}; rerun it with that', 1)
        report = logged
    else:
        # An archive with metadata but no byte section and no ARCHIVE.json (such as
        # a Semantic Unison semdiff archive) gains ARCHIVE.json and nothing else.
        metadata_only = (not os.path.lexists(root / 'recovery.sqlite3') and not os.path.lexists(root / 'bytes')
                         and not arc.manifest.exists()
                         and (os.path.lexists(root / 'metadata') or os.path.lexists(root / 'provenance.json')))
        if metadata_only:
            has_bytes = False
        elif not os.path.lexists(root / 'recovery.sqlite3'):
            if report:
                die(f'{root} is not a legacy archive (no recovery.sqlite3 at the top); nothing to migrate', 1)
            print(f'{root} is not a legacy archive (no recovery.sqlite3 at the top); nothing to migrate')
            return 0
        elif os.path.lexists(root / 'bytes'):
            die(f'{root} has recovery.sqlite3 at the top and a bytes/ directory; resolve by hand', 1)
    if report:
        if not os.path.isdir(report):
            die(f'--report: {report} is not a directory')
        if os.path.lexists(root / 'report') and not resuming:
            die(f'{root / "report"} exists; not copying {report}', 1)
    held = hold_locks(arc)
    if report and os.path.isfile(os.path.join(report, '.lock')):
        with open(os.path.join(report, '.lock'), 'rb') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                die(f'{report}/.lock is held: a comparison is writing that report', 1)
    names = [n for n in BYTE_ENTRIES if os.path.lexists(root / n)] if has_bytes else []
    device = root.stat().st_dev
    for n in names + (['bytes'] if os.path.lexists(root / 'bytes') else []):
        if os.lstat(root / n).st_dev != device:
            die(f'{n} is on another device than {root}; a rename cannot move it and migrate does not copy', 1)
    plan = ([('mkdir', 'bytes')] if has_bytes and not os.path.lexists(root / 'bytes') else []) + \
        [('move', n, 'bytes/' + n) for n in names] + \
        ([('copy', report, 'report')] if report and not os.path.lexists(root / 'report') else [])
    keep = sorted(set(os.listdir(root)) - set(names) - ({'bytes'} if has_bytes else set()))
    print(f'dngc migrate {root}' + (' (dry run: nothing is changed)' if a.dry_run else ''), flush=True)
    if resuming:
        print(f'resuming the migration that {MIGRATION_LOG} records', flush=True)
    if not a.dry_run and not resuming:
        log_step(arc, dict(op='begin', time=time.strftime('%Y-%m-%dT%H:%M:%S%z'), archive=str(root), report=report,
                           byte_section=has_bytes))
    for step in plan:
        print('mkdir bytes' if step[0] == 'mkdir' else f'{step[0]} {step[1]} -> {step[2]}', flush=True)
        if a.dry_run:
            continue
        if step[0] == 'mkdir':
            log_step(arc, dict(op='mkdir', path='bytes'))
            os.mkdir(root / 'bytes')
        elif step[0] == 'move':
            log_step(arc, dict(op='move', **{'from': step[1], 'to': step[2]}))
            os.rename(root / step[1], root / step[2])
        else:
            log_step(arc, dict(op='copy', **{'from': step[1], 'to': 'report'}))
            partial = root / 'report.migrating'
            if os.path.lexists(partial):
                shutil.rmtree(partial)
            shutil.copytree(step[1], partial, symlinks=True)
            os.rename(partial, root / 'report')
    for k in keep:
        print(f'keep {k}', flush=True)
    print(f'write {arc.manifest.name} ({"append" if arc.manifest.exists() else "new"})', flush=True)
    if a.dry_run:
        return 0
    _, entries = migration_state(arc)
    moved, seen = [], set()
    for e in entries:
        if e['op'] == 'move' and e['to'] not in seen:
            seen.add(e['to']); moved.append({'path': e['to'], 'from': e['from']})
    if not has_bytes:
        log_step(arc, dict(op='manifest', path=arc.manifest.name))  # undo removes it
    m = arc.read_manifest()
    m.setdefault('migrations', []).append(dict(
        migrated=time.strftime('%Y-%m-%dT%H:%M:%S%z'), moved=moved,
        report={'path': 'report', 'from': report} if report else None, summary=migration_summary(arc)))
    write_manifest(arc, m)
    log_step(arc, dict(op='done', time=time.strftime('%Y-%m-%dT%H:%M:%S%z')))
    for h in held:
        h.close()
    print(f'Migrated {len(moved)} paths into {root / "bytes"}' if has_bytes else
          f'Wrote {arc.manifest}; there is no byte section to move', flush=True)
    if a.no_verify:
        return 0
    if cmd_verify(argparse.Namespace(archive=str(root), restore_sample=3)):
        print('dngc: error: verify found problems after the migration; it is kept '
              f'(dngc migrate --undo {root} reverses it)', file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='dngc', description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='verb', required=True)
    r = sub.add_parser('run', help='compare, preserve and extract metadata in one pass')
    r.add_argument('left'); r.add_argument('right'); r.add_argument('archive')
    r.add_argument('--pairs', help='LEFT<TAB>RIGHT lines (e.g. phindex align output); default: the whole tree')
    r.add_argument('--bytes', choices=['delta', 'full', 'none'], default='delta')
    r.add_argument('--metadata', choices=['exiftool', 'phindex', 'none'], default='exiftool')
    r.add_argument('--index', nargs=2, metavar=('LEFT.sqlite', 'RIGHT.sqlite'), help='phindex indexes of the two trees')
    r.add_argument('--workers', type=int, default=os.cpu_count() // 4 or 2)
    r.add_argument('--impl', choices=['ocaml', 'rust', 'python'], default='ocaml')
    r.add_argument('--max-mib', type=float, help='limit reads to this many MiB/s (default: no limit)')
    x = sub.add_parser('restore', help='rebuild a preserved file byte for byte')
    x.add_argument('archive'); x.add_argument('relative_path'); x.add_argument('output')
    x.add_argument('--version', metavar='LABEL', help='an earlier content of the path (see dngc versions)')
    x = sub.add_parser('versions', help='the preserved contents of each path, current and earlier')
    x.add_argument('archive'); x.add_argument('relative_path', nargs='?')
    x = sub.add_parser('index', help='(re)build the searchable metadata index')
    x.add_argument('archive')
    x = sub.add_parser('query', help='query the metadata index: changes | files TAG | values TAG | SQL')
    x.add_argument('archive'); x.add_argument('query', nargs='*')
    x = sub.add_parser('verify', help='check the archive: records, files, index; optionally restore a sample')
    x.add_argument('archive'); x.add_argument('--restore-sample', type=int, default=0, metavar='N')
    x = sub.add_parser('status', help="what the archive holds")
    x.add_argument('archive')
    x = sub.add_parser('migrate', help="move a legacy archive's byte section into bytes/")
    x.add_argument('archive')
    x.add_argument('--dry-run', action='store_true', help='print the plan only')
    x.add_argument('--report', metavar='DIR', help='also copy this comparison report into report/')
    x.add_argument('--undo', action='store_true', help='move back what MIGRATION.jsonl records')
    x.add_argument('--no-verify', action='store_true', help='skip verify --restore-sample 3 afterwards')
    a = ap.parse_args(argv)
    return {'run': cmd_run, 'restore': cmd_restore, 'index': cmd_index, 'query': cmd_query,
            'verify': cmd_verify, 'status': cmd_status, 'migrate': cmd_migrate, 'versions': cmd_versions}[a.verb](a)


if __name__ == '__main__':
    sys.exit(main())

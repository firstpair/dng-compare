#!/usr/bin/env python3
"""dngc: one command for dng-compare's comparison, preservation and metadata tools.

    dngc run LEFT RIGHT ARCHIVE [--pairs FILE] [--bytes delta|full|none]
             [--metadata exiftool|phindex|none] [--index LEFT.sqlite RIGHT.sqlite]
             [--workers N] [--impl ocaml|rust|python] [--max-mib R]
    dngc restore ARCHIVE RELATIVE_PATH OUTPUT
    dngc index ARCHIVE
    dngc query ARCHIVE [changes | files TAG | values TAG | SQL]
    dngc verify ARCHIVE
    dngc status ARCHIVE

An archive is one directory with optional sections (docs/dngc.md):
report/ (comparison), bytes/ (preservation), metadata/ (searchable metadata),
and ARCHIVE.json describing the runs. Archives written before dngc, with
recovery.sqlite3 at the top, are read as they are.

dngc runs the existing tools: `dng-archive` (OxCaml, the default), the Rust
`dng-compare-all`, or the Python modules, which stay the reference.
"""
import argparse, gzip, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, time

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
        self.root = pathlib.Path(root).absolute()
        # Archives from before dngc keep their byte section at the top.
        self.legacy = (self.root / 'recovery.sqlite3').exists() and not (self.root / 'bytes').exists()
        self.report = self.root / 'report'
        self.bytes = self.root if self.legacy else self.root / 'bytes'
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


def walk_files(root):
    """Every regular file under root, relative, sorted; symlinks and .DS_Store are left out."""
    out = []
    for d, dirs, names in os.walk(root, onerror=lambda e: (_ for _ in ()).throw(e)):
        dirs.sort()
        for n in sorted(names):
            p = os.path.join(d, n)
            if n != '.DS_Store' and os.path.isfile(p) and not os.path.islink(p):
                out.append(os.path.relpath(p, root))
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
    return call([python(), HERE / 'preserve_apo.py', 'restore', arc.bytes, a.relative_path, a.output])


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
    db = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
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
        db = sqlite3.connect(f'file:{arc.bytes / "recovery.sqlite3"}?mode=ro', uri=True)
        out['bytes'] = dict(files=db.execute('SELECT count(*) FROM files').fetchone()[0],
                            errors=db.execute('SELECT count(*) FROM errors').fetchone()[0],
                            last_run_total=s.get('total_files'), last_run_complete=s.get('byte_preservation_complete'),
                            layout='legacy (top level)' if arc.legacy else 'bytes/')
    if (arc.metadata / 'differences.sqlite3').exists():
        db = sqlite3.connect(f'file:{arc.metadata / "differences.sqlite3"}?mode=ro', uri=True)
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
        db = sqlite3.connect(f'file:{arc.bytes / "recovery.sqlite3"}?mode=ro', uri=True)
        missing = 0
        for path, record in db.execute('SELECT path, record FROM files'):
            r = json.loads(record)
            for key in ('delta', 'original'):
                if r.get(key) and not (arc.bytes / r[key]).exists():
                    missing += 1
        if missing:
            problems.append(f'bytes: {missing} deltas or originals named in records are missing')
    if 'metadata' in s:
        db = sqlite3.connect(f'file:{arc.metadata / "differences.sqlite3"}?mode=ro', uri=True)
        ok = db.execute('PRAGMA quick_check').fetchone()[0]
        if ok != 'ok':
            problems.append(f'metadata index: {ok}')
    if 'report' in s and not s['report'].get('complete'):
        problems.append('report: the comparison has not finished')
    print(json.dumps(dict(archive=str(arc.root), sections=sorted(s), problems=problems), indent=2))
    if a.restore_sample and 'bytes' in s:
        db = sqlite3.connect(f'file:{arc.bytes / "recovery.sqlite3"}?mode=ro', uri=True)
        paths = [p for p, in db.execute('SELECT path FROM files ORDER BY random() LIMIT ?', (a.restore_sample,))]
        with tempfile.TemporaryDirectory() as td:
            for i, p in enumerate(paths):
                if call([python(), HERE / 'preserve_apo.py', 'restore', arc.bytes, p, pathlib.Path(td) / str(i)]):
                    problems.append(f'restore failed: {p}')
    return 1 if problems else 0


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
    x = sub.add_parser('index', help='(re)build the searchable metadata index')
    x.add_argument('archive')
    x = sub.add_parser('query', help='query the metadata index: changes | files TAG | values TAG | SQL')
    x.add_argument('archive'); x.add_argument('query', nargs='*')
    x = sub.add_parser('verify', help='check the archive: records, files, index; optionally restore a sample')
    x.add_argument('archive'); x.add_argument('--restore-sample', type=int, default=0, metavar='N')
    x = sub.add_parser('status', help="what the archive holds")
    x.add_argument('archive')
    a = ap.parse_args(argv)
    return {'run': cmd_run, 'restore': cmd_restore, 'index': cmd_index, 'query': cmd_query,
            'verify': cmd_verify, 'status': cmd_status}[a.verb](a)


if __name__ == '__main__':
    sys.exit(main())

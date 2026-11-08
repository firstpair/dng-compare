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
    if a.metadata == 'phindex' and not a.pairs:
        die('--metadata phindex needs --pairs')
    for p in a.index or []:
        if not pathlib.Path(p).is_file():
            die(f'phindex index not found: {p}')
    rate = str(a.max_mib or 100000)
    started = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    statuses = {}
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-'))
    try:
        if a.pairs:
            pairs = read_pairs(a.pairs)
            dng = [p for p in pairs if p[0].lower().endswith('.dng') and p[1].lower().endswith('.dng')]
            other = [p for p in pairs if p not in dng]
        else:
            if a.impl != 'python':
                die('a whole-tree run (no --pairs) needs --impl python for now')
            dng, other = None, []
        # A repeated run trusts the list it checked before and reuses everything still valid.
        repeat = (arc.report / 'pairs.jsonl').exists()
        engine_metadata = a.metadata == 'exiftool'
        index_args = ['--left-index', a.index[0], '--right-index', a.index[1]] if a.index else []
        if dng is None or dng:
            dng_file = tmp / 'dng-pairs.tsv'
            if dng is not None:
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
                           *(['--pairs-only', '--pairs', dng_file] if dng is not None else []),
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
            jsonl.write_text(''.join(json.dumps({'source': os.path.join(os.path.abspath(a.left), l) if not os.path.isabs(l) else l,
                                                 'reference': os.path.join(os.path.abspath(a.right), r) if not os.path.isabs(r) else r}) + '\n'
                                     for l, r in other))
            command = [python(), HERE / 'preserve_apo.py', 'scan', a.left, a.right, arc.bytes, '--delta',
                       '--pairs', jsonl, '--workers', min(int(a.workers), 36), '--rate-mib', rate]
            if a.index:
                command += ['--source-index', a.index[0], '--reference-index', a.index[1]]
            statuses['preserve_other'] = call(command)
        if a.metadata == 'phindex':
            all_pairs = tmp / 'all-pairs.tsv'
            all_pairs.write_text(pathlib.Path(a.pairs).read_text() if a.pairs else '')
            binary = ocaml_binary() if a.impl == 'ocaml' else None
            command = ([binary, 'phindex-metadata'] if binary else [python(), HERE / 'phindex_metadata.py']) + \
                [a.left, a.right, arc.root, '--pairs', all_pairs, '--left-index', a.index[0], '--right-index', a.index[1]]
            statuses['metadata'] = call(command)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    arc.record_run(dict(started=started, finished=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        left=os.path.abspath(a.left), right=os.path.abspath(a.right),
                        pairs=os.path.abspath(a.pairs) if a.pairs else None, bytes=a.bytes, metadata=a.metadata,
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

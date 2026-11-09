#!/usr/bin/env python3
"""Build the conformance corpus: small dngc archives, valid and broken, each
with the source and reference trees it was made from and expected.json, the
rules validate_archive.py must report (none for a valid archive).

Records hold absolute paths, so every case is built at
/private/tmp/dngc-conformance/<case>/ (L, R, archive and the phindex indexes)
and then copied into conformance/corpus/<case>/; tests materialize a case at
that same path before using it. Contents and file times are fixed, so the
records and labels come out the same each time; inode numbers and run times
do not, and the corpus is regenerated only on purpose:

    uv run --no-project python conformance/make_corpus.py"""
import json, os, pathlib, random, shutil, sqlite3, subprocess, sys, zlib

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
BUILD = pathlib.Path('/private/tmp/dngc-conformance')
CORPUS = HERE / 'corpus'
ENGINE = 'exiftool-rs 0.12.0 (ExifTool 13.59 tables)'
T0 = 1_700_000_000  # file times: 2023-11-14


def dngc(*args, check=True):
    r = subprocess.run([sys.executable, REPO / 'dngc.py', *map(str, args)], capture_output=True, text=True)
    if check and r.returncode not in (0, 1):
        raise SystemExit(f'dngc {args[0]} failed:\n{r.stdout}\n{r.stderr}')
    return r


def write(p, data, t):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.utime(p, (t, t))


def deflate(text):
    c = zlib.compressobj(wbits=-15)
    return c.compress(text.encode()) + c.flush()


def trees(case):
    """The source and reference trees: identical, empty, delta, full-copy,
    non-ASCII and unread-type files, and one extended attribute."""
    L, R = case / 'L', case / 'R'
    rng = random.Random(7)
    big = bytes(rng.getrandbits(8) for _ in range(40000))
    files = {
        'a/same.txt': ('same\n', 'same\n'),
        'a/empty.txt': ('', ''),
        'b/big.bin': (big + b'tail', big),
        'b/only.dat': ('only here\n', None),
        'c/Ünïcode name.txt': ('naïve\n', 'naïve\n'),
        'c/proxy.cop': ('cache one\n', 'cache two\n'),
    }
    for i, (rel, (left, right)) in enumerate(sorted(files.items())):
        write(L / rel, left, T0 + i)
        if right is not None:
            write(R / rel, right, T0 + i)
    subprocess.run(['xattr', '-w', 'user.conformance', 'kept', L / 'a/same.txt'], check=True)
    return L, R, files


def phindex_indexes(case, L, R, files):
    """Synthetic phindex indexes for --metadata phindex: metadata records for
    the image-like files, none for the unread .cop type."""
    import hashlib
    pairs = []
    for side, root in ((0, L), (1, R)):
        db = sqlite3.connect(case / f'{root.name.lower()}.sqlite')
        db.execute('CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, gone INTEGER, '
                   'sha256 TEXT, md_digest TEXT, md_err TEXT)')
        db.execute('CREATE TABLE metadata (path TEXT PRIMARY KEY, engine TEXT NOT NULL, record_deflate BLOB NOT NULL)')
        for rel in sorted(files):
            p = root / rel
            if not p.exists():
                continue
            st = p.stat()
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
            read = not rel.endswith('.cop')
            db.execute('INSERT INTO files VALUES (?,?,?,0,?,?,NULL)', (str(p), st.st_size, st.st_mtime_ns, sha,
                                                                     'md' if read else None))
            if read:
                record = {'SourceFile': str(p), 'ExifTool:DngCompareEngine': ENGINE, 'EXIF:IFD0:Make': 'Conformance',
                          'XMP:XMP-xmp:Rating': 1 + side if rel == 'b/big.bin' else 1,
                          'XMP:XMP-dc:Title': {'b/big.bin': ['left', 'right'][side]}.get(rel, 'same')}
                db.execute('INSERT INTO metadata VALUES (?,?,?)', (str(p), 'exiftool-rs (via phidentify)',
                                                                  deflate(json.dumps(record))))
        db.commit(); db.close()
    for rel in sorted(files):
        if (R / rel).exists():
            pairs.append(f'{L / rel}\t{R / rel}\n')
    (case / 'pairs.tsv').write_text(''.join(pairs))


def base(name):
    """A current-layout archive with every record kind and a metadata section."""
    case = BUILD / name
    shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True)
    L, R, files = trees(case)
    dngc('run', L, R, case / 'archive', '--impl', 'python', '--metadata', 'none')
    phindex_indexes(case, L, R, files)
    r = subprocess.run([sys.executable, REPO / 'phindex_metadata.py', L, R, case / 'archive', '--pairs', case / 'pairs.tsv',
                        '--left-index', case / 'l.sqlite', '--right-index', case / 'r.sqlite'], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise SystemExit(f'phindex_metadata failed:\n{r.stdout}\n{r.stderr}')
    return case


def db_exec(path, *statements):
    db = sqlite3.connect(path)
    for s in statements:
        db.execute(*s) if isinstance(s, tuple) else db.execute(s)
    db.commit(); db.close()


def record(case, rel):
    db = sqlite3.connect(case / 'archive/bytes/recovery.sqlite3')
    r = json.loads(db.execute('SELECT record FROM files WHERE path=?', (rel,)).fetchone()[0])
    db.close()
    return r


CASES = {}


def case(valid, rules=(), note=''):
    def register(f):
        CASES[f.__name__.replace('_', '-')] = (f, dict(valid=valid, rules=sorted(rules), note=note))
        return f
    return register


@case(True, note='every record kind, a metadata section with a byte-identical and an unread-type row')
def current():
    return base('current')


@case(True, note='a path whose content changed: its earlier content in versions, the new payload under versions/LABEL/')
def versions():
    c = base('versions')
    write(c / 'L/b/big.bin', (c / 'L/b/big.bin').read_bytes()[:-4] + b'TAIL', T0 + 100)
    dngc('run', c / 'L', c / 'R', c / 'archive', '--impl', 'python', '--metadata', 'none')
    return c


@case(True, note='one drop in the trash (with its metadata rows), one forced; both leave tombstones')
def dropped():
    c = base('dropped')
    dngc('drop', c / 'archive', '--glob', 'b/big.bin', '--yes')
    dngc('drop', c / 'archive', '--glob', 'b/only.dat', '--force')
    return c


def to_legacy(c):
    """The archive as tools wrote it before the bytes/ layout: the byte section
    at the top, and neither ARCHIVE.json nor report/."""
    a = c / 'archive'
    for p in sorted((a / 'bytes').iterdir()):
        p.rename(a / p.name)
    (a / 'bytes').rmdir()
    for p in ('ARCHIVE.json', 'report'):
        if (a / p).is_dir():
            shutil.rmtree(a / p)
        elif (a / p).exists():
            (a / p).unlink()
    return c


@case(True, note='an archive from before the bytes/ layout: the byte section at the top, no ARCHIVE.json')
def legacy():
    return to_legacy(base('legacy'))


@case(True, note='a legacy archive after dngc migrate: MIGRATION.jsonl and a migrations entry')
def migrated():
    c = to_legacy(base('migrated'))
    dngc('migrate', c / 'archive', '--no-verify')
    return c


@case(True, note='a metadata index from before the resume columns (their values NULL)')
def index_before_resume_columns():
    c = base('index-before-resume-columns')
    db_exec(c / 'archive/metadata/differences.sqlite3',
            'UPDATE files SET apo_size=NULL, apo_mtime_ns=NULL, no_metadata=NULL, same_bytes=NULL')
    return c


@case(False, ['payload-hash', 'rebuild'], 'one byte of a delta changed')
def broken_delta():
    c = base('broken-delta')
    p = c / 'archive/bytes' / record(c, 'b/big.bin')['delta']
    data = bytearray(p.read_bytes()); data[-1] ^= 0xFF; p.write_bytes(bytes(data))
    return c


@case(False, ['payload-hash'], 'a full copy changed, same size')
def broken_original():
    c = base('broken-original')
    p = c / 'archive/bytes' / record(c, 'b/only.dat')['original']
    p.write_bytes(b'ONLY here\n')
    return c


@case(False, ['payload-missing'], 'a full copy deleted')
def missing_payload():
    c = base('missing-payload')
    (c / 'archive/bytes' / record(c, 'b/only.dat')['original']).unlink()
    return c


@case(False, ['reference'], "an identical record's reference deleted")
def missing_reference():
    c = base('missing-reference')
    (c / 'R/a/same.txt').unlink()
    return c


@case(False, ['manifest-schema'], 'ARCHIVE.json names another format version')
def unknown_schema():
    c = base('unknown-schema')
    m = json.loads((c / 'archive/ARCHIVE.json').read_text()); m['schema'] = 'dngc-archive-99'
    (c / 'archive/ARCHIVE.json').write_text(json.dumps(m, indent=2) + '\n')
    return c


@case(False, ['drops', 'index-files', 'tombstone'], 'a tombstone for a path that has a current record and index rows, from no listed drop')
def tombstone_with_record():
    c = base('tombstone-with-record')
    db_exec(c / 'archive/bytes/recovery.sqlite3',
            'CREATE TABLE dropped (path TEXT, drop_id TEXT)',
            ("INSERT INTO dropped VALUES ('a/same.txt', '20230101T000000Z')",))
    return c


@case(False, ['index-differences'], 'a difference row missing from the index')
def index_missing_difference():
    c = base('index-missing-difference')
    db_exec(c / 'archive/metadata/differences.sqlite3', 'DELETE FROM differences WHERE rowid = (SELECT max(rowid) FROM differences)')
    return c


@case(False, ['migration-state'], 'a migration log without done')
def half_migration():
    c = base('half-migration')
    (c / 'archive/MIGRATION.jsonl').write_text(json.dumps(dict(op='begin', time='2023-11-14T00:00:00+0000', archive='x',
                                                               report=None, byte_section=True)) + '\n' +
                                               json.dumps(dict(op='mkdir', path='bytes')) + '\n')
    return c


def main():
    shutil.rmtree(CORPUS, ignore_errors=True)
    CORPUS.mkdir(parents=True)
    for name, (build, expected) in CASES.items():
        c = build()
        (c / 'expected.json').write_text(json.dumps(expected, indent=2) + '\n')
        shutil.copytree(c, CORPUS / name, symlinks=True)
        print(f'{name}: {"valid" if expected["valid"] else "broken: " + ", ".join(expected["rules"])}')


if __name__ == '__main__':
    main()

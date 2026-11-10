#!/usr/bin/env python3
"""Build the conformance corpus: small dngc archives, valid and broken, each
with the source and reference trees it was made from and expected.json, the
rules validate_archive.py must report (none for a valid archive).

Records hold absolute paths, so every case is built at
/private/tmp/dngc-conformance/<case>/ (L, R, archive and the phindex indexes)
and then copied into conformance/corpus/<case>/; tests materialize a case at
that same path before using it. Contents and file times are fixed, so the
records and labels come out the same each time; inode numbers and run times
do not, and the corpus is regenerated only on purpose, all of it or the cases
named:

    uv run --no-project python conformance/make_corpus.py [CASE ...]"""
import hashlib, json, os, pathlib, random, shutil, sqlite3, struct, subprocess, sys, zlib

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


# ---- the skeleton section (spec section 13) ----

def noise(n, seed):
    return random.Random(seed).randbytes(n)


def tiff(xmp, order='<', moved=False, gap=0):
    """A classic TIFF with something of every kind section 13.1 names. IFD0:
    three strips (the first two a pad byte apart, the third elsewhere), XMP, a
    large private tag, a SubIFD with a JPEG stream, an Exif IFD with a large
    maker note and a small date; IFD1, next in the chain: two tiles. With gap,
    that many bytes no tag points at. A longer XMP packet or moved=True puts
    every block at another offset; the blocks' bytes stay the same."""
    out = bytearray((b'II*\0' if order == '<' else b'MM\0*') + bytes(4))
    at = {}

    def place(name, data):
        at[name] = len(out)
        out.extend(data)
        if len(out) % 2:
            out.append(0)

    parts = dict(xmp=xmp, private=noise(800, 1), strips12=noise(2999, 2) + b'\0' + noise(3001, 3), strip3=noise(2000, 4),
                 preview=noise(5000, 5), maker=noise(600, 6), date=b'2023:11:14 22:13:20\0', tiles=noise(3000, 7),
                 unreached=noise(gap, 8))
    names = ['private', 'tiles', 'preview', 'unreached', 'maker', 'strips12', 'date', 'xmp', 'strip3'] if moved else \
            ['xmp', 'private', 'strips12', 'preview', 'strip3', 'maker', 'date', 'tiles', 'unreached']
    for name in names:
        place(name, parts[name])
    longs = lambda *v: struct.pack(f'{order}{len(v)}I', *v)
    place('strip_offsets', longs(at['strips12'], at['strips12'] + 3000, at['strip3']))
    place('strip_counts', longs(2999, 3001, 2000))
    place('tile_offsets', longs(at['tiles'], at['tiles'] + 1500))
    place('tile_counts', longs(1500, 1500))

    def ifd(name, entries, following=0):
        at[name] = len(out)
        out.extend(struct.pack(order + 'H', len(entries)))
        for tag, typ, count, value in sorted(entries):
            out.extend(struct.pack(order + 'HHI', tag, typ, count))
            out.extend(struct.pack(order + 'H', value) + bytes(2) if typ == 3 else struct.pack(order + 'I', value))
        out.extend(struct.pack(order + 'I', following))

    ifd('sub', [(513, 4, 1, at['preview']), (514, 4, 1, 5000)])
    ifd('exif', [(36867, 2, 20, at['date']), (37500, 7, 600, at['maker'])])
    ifd('ifd1', [(256, 3, 1, 64), (257, 3, 1, 32), (324, 4, 2, at['tile_offsets']), (325, 4, 2, at['tile_counts'])])
    ifd('ifd0', [(256, 3, 1, 64), (257, 3, 1, 48), (273, 4, 3, at['strip_offsets']), (279, 4, 3, at['strip_counts']),
                 (330, 4, 1, at['sub']), (700, 1, len(xmp), at['xmp']), (34665, 4, 1, at['exif']),
                 (50740, 1, 800, at['private'])], at['ifd1'])
    out[4:8] = struct.pack(order + 'I', at['ifd0'])
    return bytes(out)


def jpeg(segments, scan=None):
    """A JPEG: the given (marker, data) segments, a start of scan, the scan
    (12,000 bytes, so its mark does not cover its middle) and the end marker."""
    out = bytearray(b'\xff\xd8')
    for marker, data in segments:
        out += bytes([0xFF, marker]) + struct.pack('>H', len(data) + 2) + data
    return bytes(out + b'\xff\xda\x00\x02' + (noise(12000, 9) if scan is None else scan) + b'\xff\xd9')


XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">rating 0</x:xmpmeta>'
XMP_EDITED = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">rating 5, exposure +0.35, blacks +12' + b' ' * 2049 + b'</x:xmpmeta>'
JPEG_FIXED = [(0xE2, b'ICC_PROFILE\0\x01\x01' + noise(3000, 10)), (0xDB, noise(130, 11)), (0xC0, noise(15, 12))]
JPEG = [(0xE0, b'JFIF\0\x01\x02\0\0\x01\0\x01\0\0'), (0xE1, b'Exif\0\0as shot'), *JPEG_FIXED]
JPEG_EDITED = [(0xE0, b'JFIF\0\x01\x02\0\0\x01\0\x01\0\0'), (0xE1, b'Exif\0\0with a caption, and longer'),
               (0xED, b'Photoshop 3.0\0' + noise(700, 13)), (0xE1, b'http://ns.adobe.com/xap/1.0/\0' + XMP_EDITED),
               (0xFE, b'a comment'), *JPEG_FIXED]
# What an editor of the reference tree leaves at each path, after the archive
# and its skeletons were written.
EDITS = {
    'a/sidecar.txt': b'rating 5\n',
    'a/notes.txt': b'notes, as the reference had them, and a line added since\n',
    'p/same.tif': tiff(XMP_EDITED, moved=True),
    'p/changed.tif': tiff(XMP_EDITED + b'<!-- once more -->'),
    'p/motorola.tif': tiff(XMP_EDITED, order='>'),
    'p/same.jpg': jpeg(JPEG_EDITED),
    'p/changed.jpg': jpeg(JPEG_EDITED),
}


def skeleton_trees(case):
    """Source and reference trees whose reference files have skeletons of each
    format: identical and delta records over TIFFs (both byte orders), JPEGs and
    small text files, and a full copy, which needs none."""
    L, R = case / 'L', case / 'R'
    files = {
        'a/sidecar.txt': (b'rating 0\n', b'rating 0\n'),
        'a/notes.txt': (b'notes, as the source has them\n', b'notes, as the reference had them\n'),
        'b/only.dat': (b'only here\n', None),
        'p/same.tif': (tiff(XMP), tiff(XMP)),
        'p/changed.tif': (tiff(XMP.replace(b'rating 0', b'rating 3')), tiff(XMP)),
        'p/motorola.tif': (tiff(XMP, order='>'), tiff(XMP, order='>')),
        'p/same.jpg': (jpeg(JPEG), jpeg(JPEG)),
        'p/changed.jpg': (jpeg([(0xE1, b'Exif\0\0in the source'), *JPEG[1:]]), jpeg(JPEG)),
    }
    for i, (rel, (left, right)) in enumerate(sorted(files.items())):
        write(L / rel, left, T0 + i)
        if right is not None:
            write(R / rel, right, T0 + i)
    return L, R


def write_skeletons(case):
    """The skeleton section, as section 13.2 gives it: a skeleton of every
    content that a current record names as its reference. p/same and p/changed
    have one reference content, so one skeleton, read from p/changed, serves
    both records, each with the file at its own reference."""
    sys.path.insert(0, str(REPO))
    import skeleton
    top = case / 'archive'
    (top / 'skeletons').mkdir()
    for name in ('skipped.tsv', '.lock'):
        (top / 'skeletons' / name).touch()
    records = sqlite3.connect(top / 'bytes/recovery.sqlite3')
    db = sqlite3.connect(top / 'skeletons/skeletons.sqlite3')
    db.execute('CREATE TABLE skeletons(sha256 TEXT PRIMARY KEY, record TEXT NOT NULL, skeleton BLOB NOT NULL)')
    for text, in records.execute('SELECT record FROM files ORDER BY path'):
        r = json.loads(text)
        if r.get('original') and not r.get('delta') or not r.get('reference'):
            continue
        sha = r['reference_sha256'] if r.get('delta') else r['source_sha256']
        reference = pathlib.Path(r['reference'])
        st = reference.stat()
        assert [st.st_size, st.st_mtime_ns] == r['reference_identity'][1:3], reference
        sk = skeleton.build(reference)
        assert hashlib.sha256(reference.read_bytes()).hexdigest() == sha
        db.execute('INSERT OR IGNORE INTO skeletons VALUES (?,?,?)',
                   (sha, *skeleton.encode(sha, reference, [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns], sk)))
    db.commit(); db.close(); records.close()


def skeleton_base(name, edits=EDITS):
    """An archive with a skeleton section whose reference files were all
    edited after it was written: no identical or delta record rebuilds without
    its skeleton."""
    case = BUILD / name
    shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True)
    L, R = skeleton_trees(case)
    dngc('run', L, R, case / 'archive', '--impl', 'python', '--metadata', 'none')
    write_skeletons(case)
    for rel, data in edits.items():
        write(R / rel, data, T0 + 200)
    return case


def skeleton_row(case, rel):
    """The key of the skeleton of the content that rel's record names as its reference."""
    r = record(case, rel)
    return r['reference_sha256'] if r.get('delta') else r['source_sha256']


def edit_skeleton_record(case, rel, change):
    sha = skeleton_row(case, rel)
    path = case / 'archive/skeletons/skeletons.sqlite3'
    db = sqlite3.connect(path)
    r = json.loads(db.execute('SELECT record FROM skeletons WHERE sha256=?', (sha,)).fetchone()[0])
    change(r)
    db.close()
    db_exec(path, ('UPDATE skeletons SET record=? WHERE sha256=?', (json.dumps(r), sha)))


@case(True, note='a skeleton section, and every reference file edited since: TIFFs of both byte orders with moved blocks, '
                 'JPEGs with new metadata segments, text files; each record restores through its skeleton')
def skeletons():
    return skeleton_base('skeletons')


@case(False, ['reference', 'skeleton-bytes'], "a skeleton's blob cut short: not a zstd frame, and its record cannot be rebuilt")
def skeleton_corrupt():
    c = skeleton_base('skeleton-corrupt')
    sha = skeleton_row(c, 'p/same.tif')
    db_exec(c / 'archive/skeletons/skeletons.sqlite3',
            ('UPDATE skeletons SET skeleton=substr(skeleton, 1, length(skeleton) - 9) WHERE sha256=?', (sha,)))
    return c


@case(False, ['reference', 'skeleton-bytes'], "a skeleton record's skeleton_sha256 is not that of its skeleton")
def skeleton_wrong_sha256():
    c = skeleton_base('skeleton-wrong-sha256')
    edit_skeleton_record(c, 'p/changed.jpg', lambda r: r.update(skeleton_sha256=hashlib.sha256(b'other').hexdigest()))
    return c


@case(False, ['reference', 'skeleton-blocks'], "a skeleton record's first two blocks listed in the wrong order")
def skeleton_blocks_out_of_order():
    c = skeleton_base('skeleton-blocks-out-of-order')
    edit_skeleton_record(c, 'p/changed.tif', lambda r: r.update(blocks=[r['blocks'][1], r['blocks'][0], *r['blocks'][2:]]))
    return c


@case(False, ['reference', 'skeleton-rebuild'], 'the edit also changed the first byte of a strip: that block is in the file no longer')
def skeleton_lost_block():
    c = skeleton_base('skeleton-lost-block')
    data = bytearray(EDITS['p/changed.tif'])
    at = data.index(noise(2999, 2))
    data[at] ^= 0xFF
    write(c / 'R/p/changed.tif', bytes(data), T0 + 200)
    return c


@case(False, ['reference', 'skeleton-rebuild'], "the edit also changed a byte in the middle of the scan, which no mark covers: "
                                                'every block is found, and the SHA-256 of the result differs')
def skeleton_changed_middle():
    c = skeleton_base('skeleton-changed-middle')
    scan = bytearray(noise(12000, 9)); scan[6000] ^= 0xFF
    write(c / 'R/p/changed.jpg', jpeg(JPEG_EDITED, bytes(scan)), T0 + 200)
    return c


def main():
    names = sys.argv[1:] or list(CASES)
    unknown = sorted(set(names) - set(CASES))
    if unknown:
        raise SystemExit(f'no such case: {", ".join(unknown)}; the cases are {", ".join(CASES)}')
    if not sys.argv[1:]:
        shutil.rmtree(CORPUS, ignore_errors=True)
    CORPUS.mkdir(parents=True, exist_ok=True)
    for name in names:
        build, expected = CASES[name]
        c = build()
        (c / 'expected.json').write_text(json.dumps(expected, indent=2) + '\n')
        shutil.rmtree(CORPUS / name, ignore_errors=True)
        shutil.copytree(c, CORPUS / name, symlinks=True)
        print(f'{name}: {"valid" if expected["valid"] else "broken: " + ", ".join(expected["rules"])}')


if __name__ == '__main__':
    main()

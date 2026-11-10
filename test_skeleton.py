"""The skeleton section in Python (docs/dngc-archive-spec.md, section 13):
skeleton.py's walkers, rebuilding and store, on synthetic TIFFs and JPEGs; the
validator's own walkers, written from the spec, against them; and, when the
Rust tool dngc-skeleton has been built, the rows it writes for the same files."""
import hashlib, importlib.util, io, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest

import skeleton

HERE = pathlib.Path(__file__).resolve().parent


def load(name, path):
    """A script of conformance/ as a module, leaving no bytecode beside it."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = writes
    return module


fixtures = load('dngc_make_corpus', HERE / 'conformance/make_corpus.py')
validator = load('dngc_validate_archive', HERE / 'conformance/validate_archive.py')
RUST = next((str(c) for c in (os.environ.get('DNGC_SKELETON'), HERE / 'rust-archive/target/release/dngc-skeleton')
             if c and os.access(c, os.X_OK)), None)
TIFF_ROLES = ['0/tag50740', '0/strips', '0/330.0/jpeg', '0/strips', '0/34665.0/tag37500', '0+1/tiles']
FILES = {
    'plain.tif': fixtures.tiff(fixtures.XMP),
    'edited.tif': fixtures.tiff(fixtures.XMP_EDITED),
    'moved.tif': fixtures.tiff(fixtures.XMP_EDITED, moved=True),
    'motorola.tif': fixtures.tiff(fixtures.XMP, order='>'),
    'gap.tif': fixtures.tiff(fixtures.XMP, gap=70001),
    'gap-moved.tif': fixtures.tiff(fixtures.XMP_EDITED, moved=True, gap=70001),
    'short-gap.tif': fixtures.tiff(fixtures.XMP, gap=60000),
    'truncated.tif': fixtures.tiff(fixtures.XMP)[:9000],
    'looped.tif': fixtures.tiff(fixtures.XMP)[:4] + b'\x08\0\0\0\x01\0\0\x01\x03\0\x01\0\0\0\x01\0\0\0\x08\0\0\0',
    'plain.jpg': fixtures.jpeg(fixtures.JPEG),
    'edited.jpg': fixtures.jpeg(fixtures.JPEG_EDITED),
    'filled.jpg': b'\xff\xd8\xff\xff\xff\xe2\x02\x10' + bytes(526) + b'\xff\xda\0\x02scan\xff\xd9',
    'no-scan.jpg': b'\xff\xd8\xff\xe2\x02\x10' + bytes(526),
    'cut.jpg': fixtures.jpeg(fixtures.JPEG)[:2000],
    'bare.jpg': b'\xff\xd8',
    'sidecar.xmp': b'<x:xmpmeta>sidecar</x:xmpmeta>',
    'empty.txt': b'',
    'short.tif': b'II*\0\x08\0',
}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-skeleton-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        for name, data in FILES.items():
            (self.tmp / name).write_bytes(data)

    def round(self, old, new):
        """The skeleton of old, and old's content rebuilt from new."""
        sk = skeleton.build(self.tmp / old)
        out = io.BytesIO()
        sha = skeleton.rebuild(sk, self.tmp / new, out)
        self.assertEqual(sha, hashlib.sha256(out.getvalue()).hexdigest())
        return sk, out.getvalue()


class Walkers(Case):
    def test_a_tiff_has_the_blocks_of_the_spec(self):
        sk = skeleton.build(self.tmp / 'plain.tif')
        self.assertEqual((sk.format, [b[3] for b in sk.blocks]), ('tiff', TIFF_ROLES))
        self.assertEqual([b[1] for b in sk.blocks], [800, 6001, 5000, 2000, 600, 3000])
        self.assertLess(len(sk.bytes), 400, 'the skeleton holds the header, the XMP, small values and the IFDs only')
        self.assertEqual([b[3] for b in skeleton.build(self.tmp / 'motorola.tif').blocks], TIFF_ROLES)

    def test_a_long_run_no_tag_points_at_is_a_gap(self):
        self.assertIn([70002, 'gap'], [[b[1], b[3]] for b in skeleton.build(self.tmp / 'gap.tif').blocks])
        self.assertNotIn('gap', [b[3] for b in skeleton.build(self.tmp / 'short-gap.tif').blocks])

    def test_a_tiff_comes_back_from_edited_ones_with_moved_blocks(self):
        for old, new in (('plain.tif', 'edited.tif'), ('plain.tif', 'moved.tif'), ('moved.tif', 'plain.tif'),
                         ('gap.tif', 'gap-moved.tif'), ('plain.tif', 'gap-moved.tif')):
            with self.subTest(old=old, new=new):
                self.assertEqual(self.round(old, new)[1], FILES[old])

    def test_a_changed_block_is_reported_not_rebuilt(self):
        data = bytearray(FILES['edited.tif'])
        data[data.index(fixtures.noise(2999, 2))] ^= 1
        (self.tmp / 'changed.tif').write_bytes(data)
        with self.assertRaisesRegex(ValueError, r'block 0/strips \(6001 bytes\) is not in '):
            self.round('plain.tif', 'changed.tif')
        with self.assertRaisesRegex(ValueError, 'no skeleton walker'):
            self.round('plain.tif', 'sidecar.xmp')

    def test_a_change_between_a_blocks_marked_ends_fails_the_sha256(self):
        data = bytearray(FILES['edited.jpg'])
        data[data.index(fixtures.noise(12000, 9)) + 6000] ^= 1
        (self.tmp / 'middle.jpg').write_bytes(data)
        self.assertNotEqual(self.round('plain.jpg', 'middle.jpg')[1], FILES['plain.jpg'])

    def test_a_jpeg_comes_back_after_its_metadata_segments_change(self):
        sk, got = self.round('plain.jpg', 'edited.jpg')
        self.assertEqual(got, FILES['plain.jpg'])
        self.assertEqual((sk.format, [b[3] for b in sk.blocks]), ('jpeg', ['sege2', 'scan']))
        self.assertEqual([b[3] for b in skeleton.build(self.tmp / 'filled.jpg').blocks], ['sege2', 'scan'])
        self.assertEqual([b[3] for b in skeleton.build(self.tmp / 'no-scan.jpg').blocks], ['sege2'])

    def test_a_small_file_without_a_walker_is_its_own_skeleton(self):
        for name in ('sidecar.xmp', 'empty.txt', 'short.tif'):
            sk, got = self.round(name, 'plain.tif')
            self.assertEqual((sk.format, sk.blocks, got), ('whole', [], FILES[name]))
        big = self.tmp / 'big.bin'
        big.write_bytes(bytes(skeleton.WHOLE + 1))
        with self.assertRaisesRegex(ValueError, 'no skeleton walker'):
            skeleton.build(big)

    def test_the_validators_walkers_find_the_same_blocks(self):
        """conformance/validate_archive.py has its own walkers, written from the spec."""
        for name, data in FILES.items():
            with self.subTest(file=name), open(self.tmp / name, 'rb') as f:
                try:
                    mine = [(at, length, skeleton.mark(f, at, length), role) for at, length, role in skeleton.walk(f, len(data))[1]]
                except ValueError:
                    with self.assertRaises(validator.Unbuildable):
                        validator.file_blocks(data)
                    continue
                self.assertEqual([(at, length, validator.block_mark(data, at, length), role)
                                  for at, length, role in validator.file_blocks(data)], mine)


class Rows(Case):
    def test_a_record_is_written_as_the_spec_gives_it(self):
        sk = skeleton.build(self.tmp / 'plain.tif')
        sha = hashlib.sha256(FILES['plain.tif']).hexdigest()
        text, packed = skeleton.encode(sha, '/somewhere/plain.tif', [1, len(FILES['plain.tif']), 2, 3], sk)
        r = json.loads(text)
        self.assertEqual(list(r), ['sha256', 'size', 'format', 'path', 'identity', 'blocks', 'skeleton_bytes',
                                   'skeleton_sha256', 'skeleton_format'])
        self.assertEqual(skeleton.from_record(r, skeleton.decompress(packed)), sk)
        with self.assertRaisesRegex(ValueError, 'out of order'):
            skeleton.from_record(dict(r, blocks=r['blocks'][::-1]), sk.bytes)

    def records(self):
        """A byte section whose identical records name the fixture files as their references."""
        top = self.tmp / 'archive'
        (top / 'bytes').mkdir(parents=True)
        db = sqlite3.connect(top / 'bytes/recovery.sqlite3')
        db.execute('CREATE TABLE files (path TEXT PRIMARY KEY, record TEXT NOT NULL)')
        db.execute('CREATE TABLE errors (path TEXT PRIMARY KEY, error TEXT NOT NULL)')
        for name, data in FILES.items():
            st = (self.tmp / name).stat()
            sha = hashlib.sha256(data).hexdigest()
            db.execute('INSERT INTO files VALUES (?,?)', (name, json.dumps(dict(
                relative_path=name, source='/nowhere/' + name, reference=str(self.tmp / name), source_identity=[0, len(data), 0, 0],
                reference_identity=[st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns],
                source_attrs=dict(mode=420, uid=0, gid=0, mtime_ns=0, atime_ns=0, flags=0, xattrs={}), source_sha256=sha,
                reference_sha256=sha, original=None, whole_file_equal=True, preserved_bytes=0, reconstruction_verified=True,
                reference_relative_path=name))))
        db.commit(); db.close()
        return top

    @unittest.skipUnless(RUST, 'needs the Rust dngc-skeleton (cargo build --release in rust-archive)')
    def test_the_rust_tool_writes_the_rows_python_does(self):
        top = self.records()
        r = subprocess.run([RUST, 'build', top], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((top / 'skeletons/skipped.tsv').read_text(), '')
        store = skeleton.Store.open(top)
        self.addCleanup(store.close)
        rows = dict((sha, (text, bytes(blob))) for sha, text, blob in store.db.execute('SELECT * FROM skeletons'))
        self.assertEqual(len(rows), len({hashlib.sha256(d).hexdigest() for d in FILES.values()}))
        for name, data in FILES.items():
            with self.subTest(file=name):
                sha = hashlib.sha256(data).hexdigest()
                st = (self.tmp / name).stat()
                sk = skeleton.build(self.tmp / name)
                text, _ = skeleton.encode(sha, self.tmp / name, [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns], sk)
                self.assertEqual(rows[sha][0], text)
                self.assertEqual(skeleton.decompress(rows[sha][1]), sk.bytes)
                self.assertEqual(store.get(sha), sk)
        problems = subprocess.run([sys.executable, HERE / 'conformance/validate_archive.py', top, '--rebuild', '--json'],
                                  capture_output=True, text=True)
        self.assertEqual(json.loads(problems.stdout)['problems'], {}, problems.stdout + problems.stderr)


if __name__ == '__main__':
    unittest.main()

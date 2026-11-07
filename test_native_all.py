"""The one-pass native `all` command (docs/native-all.md) against the Python tools:
compare_dng.py --pairs-only and preserve_apo.py scan --delta --pairs on the same
synthetic pairs must give the same report and the same archive, and Python
restores what the native command preserved. Each implementation is skipped when
its binary is missing: DNG_COMPARE_ALL_RUST (else rust-scanner's release build)
and DNG_ARCHIVE (else ocaml-archive's builds, else PATH)."""
import contextlib, hashlib, io, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest

import numpy as np
import tifffile

import preserve_apo

HERE = pathlib.Path(__file__).parent


def first_executable(candidates):
    return next((str(c) for c in candidates if c and os.access(c, os.X_OK)), None)


RUST = first_executable([os.environ.get('DNG_COMPARE_ALL_RUST'),
                         HERE / 'rust-scanner/target/release/dng-compare-all'])
OCAML = first_executable([os.environ.get('DNG_ARCHIVE'),
                          *sorted(HERE.glob('ocaml-archive/_build*/default/bin/main.exe')),
                          shutil.which('dng-archive')])
TOOLS = shutil.which('exiftool') and shutil.which('xdelta3')
# An older dng-archive without the `all` command (its usage text lists the commands).
if OCAML and 'dng-archive all' not in subprocess.run([OCAML], capture_output=True, text=True).stderr:
    OCAML = None


def records(report):
    return {(r['left'], r['right']): r for r in map(json.loads, (report / 'pairs.jsonl').read_text().splitlines())}


def summary(report):
    s = json.loads((report / 'summary.json').read_text())
    s.pop('elapsed_seconds')
    return s


def archive_records(archive):
    with sqlite3.connect(archive / 'recovery.sqlite3') as db:
        rows = {path: json.loads(record) for path, record in db.execute('SELECT path, record FROM files')}
        errors = dict(db.execute('SELECT path, error FROM errors'))
    for r in rows.values():
        r['source_attrs'].pop('atime_ns', None)  # reading a file moves its access time
    return rows, errors


class Fixture:
    """Synthetic DNG pairs covering each outcome of the comparison and of preservation."""

    def __init__(self, root):
        self.left, self.right = root / 'left', root / 'right'
        pixels = np.arange(4096, dtype=np.uint16).reshape(64, 64)
        changed = pixels.copy()
        changed[9, 4] += 1
        cases = {
            'same.dng': ({}, {}),
            'meta.dng': ({'description': 'rated 5'}, {}),
            'recoded.dng': ({}, {'compression': 'deflate', 'rowsperstrip': 8}),
            'pixel.dng': ({'pixels': changed}, {}),
            'endian.dng': ({'byteorder': '>'}, {}),
            'sub/renamed.dng': ({}, {'name': 'other/Renamed.dng'}),
            'truncated.dng': ({}, {'truncate': 100}),
        }
        self.pairs = []
        for rel, (left, right) in cases.items():
            a = self.write(self.left / rel, left.pop('pixels', pixels), **left)
            b = self.write(self.right / right.pop('name', rel), pixels, **right)
            self.pairs.append((a, b))
        os.setxattr(self.left / 'meta.dng', 'user.dng-compare-test', b'kept\x00bytes')

    @staticmethod
    def write(path, pixels, description='first', truncate=0, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        tifffile.imwrite(path, pixels, description=description, metadata=None,
                         extratags=[(50706, 'B', 4, (1, 4, 0, 0), False)], **kwargs)
        if truncate:
            with open(path, 'r+b') as f:
                f.truncate(path.stat().st_size - truncate)
        return path


@unittest.skipUnless(TOOLS, 'needs exiftool and xdelta3')
class NativeAllTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dng-native-all-')).resolve()
        cls.fixture = Fixture(cls.tmp)
        cls.tsv = cls.tmp / 'pairs.tsv'
        cls.tsv.write_text(''.join(f'{a}\t{b}\n' for a, b in cls.fixture.pairs))
        # Python reference outputs.
        cls.py_report, cls.py_archive = cls.tmp / 'py-report', cls.tmp / 'py-archive'
        run = subprocess.run([sys.executable, str(HERE / 'compare_dng.py'), str(cls.fixture.left),
                              str(cls.fixture.right), '--pairs-only', '--pairs', str(cls.tsv),
                              '--output', str(cls.py_report), '--batch-size', '3'],
                             capture_output=True, text=True)
        assert run.returncode in (0, 1), run.stdout + run.stderr
        assert preserve_apo.scan(cls.fixture.left, cls.fixture.right, cls.py_archive, HERE, 200, delta_mode=True,
                                 pairs=[(str(a), str(b)) for a, b in cls.fixture.pairs])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_native(self, command, name):
        report, archive = self.tmp / f'{name}-report', self.tmp / f'{name}-archive'
        run = subprocess.run([*command, str(self.fixture.left), str(self.fixture.right), '--pairs', str(self.tsv),
                              '--report', str(report), '--archive', str(archive), '--batch-size', '3'],
                             capture_output=True, text=True)
        self.assertIn(run.returncode, (0, 1), run.stdout + run.stderr)
        return report, archive, run

    def check(self, command, name):
        report, archive, run = self.run_native(command, name)
        expected, got = records(self.py_report), records(report)
        self.assertEqual(sorted(got), sorted(expected))
        for key in expected:
            self.assertEqual(got[key], expected[key], key)
        self.assertEqual(summary(report), summary(self.py_report))
        self.assertEqual(archive_records(archive), archive_records(self.py_archive))
        for name_ in ('inventory.json', 'directories.json'):
            self.assertEqual(json.loads((archive / name_).read_text()), json.loads((self.py_archive / name_).read_text()))
        # Python restores every file the native command preserved, byte for byte.
        for a, _ in self.fixture.pairs:
            rel = str(a.relative_to(self.fixture.left))
            out = self.tmp / f'{name}-restored' / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            restored = subprocess.run([sys.executable, str(HERE / 'preserve_apo.py'), 'restore', str(archive), rel, str(out)],
                                      capture_output=True, text=True)
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertEqual(out.read_bytes(), a.read_bytes(), rel)
        # A second run reuses everything: cached results, preserved records.
        report2, archive2, run2 = self.run_native(command, name)
        self.assertEqual(summary(report2)['cached_pairs_reused'], len(self.fixture.pairs) - 1)  # all but the truncated
        self.assertEqual(records(report2), got)

    @unittest.skipUnless(RUST, 'needs dng-compare-all (make build-rust)')
    def test_rust(self):
        self.check([RUST], 'rust')

    @unittest.skipUnless(OCAML, 'needs dng-archive (ocaml-archive)')
    def test_ocaml(self):
        self.check([OCAML, 'all'], 'ocaml')


class IndexedFixture:
    """Pairs with phindex-like indexes (docs/native-all.md): identical files with
    current rows, equal dng_blocks over differing metadata, a stale row, and a
    pair whose blocks differ."""

    def __init__(self, root, blocks):
        self.left, self.right = root / 'left', root / 'right'
        pixels = np.arange(4096, dtype=np.uint16).reshape(64, 64)
        changed = pixels.copy()
        changed[3, 3] += 1
        cases = {'same.dng': ({}, {}), 'meta.dng': ({'description': 'rated 5'}, {}),
                 'stale.dng': ({'description': 'rated 4'}, {}), 'pixel.dng': ({'pixels': changed}, {})}
        self.pairs = []
        for rel, (left, right) in cases.items():
            a = Fixture.write(self.left / rel, left.pop('pixels', pixels), **left)
            b = Fixture.write(self.right / rel, pixels, **right)
            self.pairs.append((a, b))
        self.indexes = [root / 'left.sqlite', root / 'right.sqlite']
        for side, index in zip((self.left, self.right), self.indexes):
            db = sqlite3.connect(index)
            db.execute('CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, '
                       'sha256 TEXT, dng_blocks TEXT, gone INTEGER NOT NULL DEFAULT 0)')
            for name in cases:
                path = side / name
                st = path.stat()
                db.execute('INSERT INTO files VALUES (?,?,?,?,?,0)',
                           (str(path), st.st_size + (side == self.left and name == 'stale.dng'), st.st_mtime_ns,
                            hashlib.sha256(path.read_bytes()).hexdigest(),
                            ('changed' if side == self.left and name == 'pixel.dng' else 'b-' + name) if blocks else None))
            db.commit()
            db.close()


@unittest.skipUnless(TOOLS, 'needs exiftool and xdelta3')
class NativeAllIndexTest(unittest.TestCase):
    """--left-index/--right-index: compare-only settles equal SHA-256 and equal
    dng_blocks; with --archive only equal SHA-256, and preservation records
    those pairs from the index hashes, as preserve_apo's open_indexes does."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dng-native-index-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)

    def python_report(self, fx, tsv, output):
        run = subprocess.run([sys.executable, str(HERE / 'compare_dng.py'), str(fx.left), str(fx.right),
                              '--pairs-only', '--pairs', str(tsv), '--output', str(output),
                              '--left-index', str(fx.indexes[0]), '--right-index', str(fx.indexes[1])],
                             capture_output=True, text=True)
        self.assertIn(run.returncode, (0, 1), run.stdout + run.stderr)

    def native(self, command, fx, tsv, report, archive=None):
        run = subprocess.run([*command, str(fx.left), str(fx.right), '--pairs', str(tsv), '--report', str(report),
                              '--left-index', str(fx.indexes[0]), '--right-index', str(fx.indexes[1])]
                             + (['--archive', str(archive)] if archive else []), capture_output=True, text=True)
        self.assertIn(run.returncode, (0, 1), run.stdout + run.stderr)

    def check(self, command):
        # Compare-only: both kinds of settled pairs, as compare_dng.py settles them.
        fx = IndexedFixture(self.tmp / 'blocks', blocks=True)
        tsv = self.tmp / 'blocks.tsv'
        tsv.write_text(''.join(f'{a}\t{b}\n' for a, b in fx.pairs))
        self.python_report(fx, tsv, self.tmp / 'py-blocks')
        self.native(command, fx, tsv, self.tmp / 'native-blocks')
        got, expected = records(self.tmp / 'native-blocks'), records(self.tmp / 'py-blocks')
        self.assertEqual(got, expected)
        self.assertEqual(summary(self.tmp / 'native-blocks'), summary(self.tmp / 'py-blocks'))
        methods = {pathlib.Path(k[0]).name: r['raw'].get('method') for k, r in got.items()}
        self.assertEqual((methods['same.dng'], methods['meta.dng']), ('phindex_sha256', 'phindex_dng_blocks'))
        # With --archive: only equal SHA-256 settles (no dng_blocks in these indexes,
        # so compare_dng.py settles the same pairs), and preservation reads no
        # identical file.
        fx = IndexedFixture(self.tmp / 'sha', blocks=False)
        tsv = self.tmp / 'sha.tsv'
        tsv.write_text(''.join(f'{a}\t{b}\n' for a, b in fx.pairs))
        self.python_report(fx, tsv, self.tmp / 'py-sha')
        preserve_apo.open_indexes(*fx.indexes)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(preserve_apo.scan(fx.left, fx.right, self.tmp / 'py-archive', HERE, 200, delta_mode=True,
                                                  pairs=[(str(a), str(b)) for a, b in fx.pairs]))
        finally:
            for db in preserve_apo.INDEXES:
                db.close()
            preserve_apo.INDEXES = None
        self.native(command, fx, tsv, self.tmp / 'native-sha', self.tmp / 'native-archive')
        got = records(self.tmp / 'native-sha')
        self.assertEqual(got, records(self.tmp / 'py-sha'))
        self.assertEqual({pathlib.Path(k[0]).name: r['raw'].get('method') for k, r in got.items()}['same.dng'],
                         'phindex_sha256')
        self.assertEqual(summary(self.tmp / 'native-sha'), summary(self.tmp / 'py-sha'))
        self.assertEqual(archive_records(self.tmp / 'native-archive'), archive_records(self.tmp / 'py-archive'))

    @unittest.skipUnless(RUST, 'needs dng-compare-all (make build-rust)')
    def test_rust(self):
        self.check([RUST])

    @unittest.skipUnless(OCAML, 'needs dng-archive (ocaml-archive)')
    def test_ocaml(self):
        self.check([OCAML, 'all'])


if __name__ == '__main__':
    unittest.main()

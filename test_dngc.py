"""dngc on the synthetic pairs of test_native_all: each implementation writes an
archive with report/ and bytes/ that restores every left file; status, verify,
index and query work; a phindex metadata section is searchable.

Every case runs against both front ends: dngc.py, the reference, and the native
binary (DNGC_NATIVE, else ocaml-archive's OxCaml build, else any other
ocaml-archive build; its cases are skipped when there is none). The parity
cases require equal output from the two on the same archive."""
import json, os, pathlib, shutil, subprocess, sys, tempfile, unittest

import test_native_all as native
import test_phindex_metadata as phx

HERE = pathlib.Path(__file__).parent
PYTHON_DNGC = [sys.executable, str(HERE / 'dngc.py')]
NATIVE = native.first_executable([os.environ.get('DNGC_NATIVE'),
                                  HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/dngc.exe',
                                  *sorted(HERE.glob('ocaml-archive/_build*/default/bin/dngc.exe'))])
NATIVE_DNGC = [NATIVE] if NATIVE else None


def run_dngc(front, *args):
    return subprocess.run([*front, *map(str, args)], capture_output=True, text=True)


class Front:
    """The front end a case runs against; the Python one unless a subclass says otherwise."""
    FRONT = PYTHON_DNGC

    def dngc(self, *args):
        return run_dngc(self.FRONT, *args)

    def needs_dng_archive(self):
        # dngc.py runs dng-archive for --impl ocaml; the native binary is dng-archive.
        if self.FRONT is PYTHON_DNGC and not native.OCAML:
            self.skipTest('needs dng-archive')


def native_variant(cls):
    """The same cases against the native binary."""
    variant = type(cls.__name__ + 'Native', (cls,), {'FRONT': NATIVE_DNGC, '__doc__': cls.__doc__})
    return unittest.skipUnless(NATIVE, 'needs the native dngc build (DNGC_NATIVE)')(variant)


class DngcRunCases(Front):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fx = native.Fixture(self.tmp)
        self.pairs = self.tmp / 'pairs.tsv'
        self.pairs.write_text(''.join(f'{a}\t{b}\n' for a, b in self.fx.pairs))

    def check(self, impl):
        arc = self.tmp / f'{impl}-archive'
        run = self.dngc('run', self.fx.left, self.fx.right, arc, '--pairs', self.pairs, '--impl', impl, '--workers', 2)
        self.assertIn(run.returncode, (0, 1), run.stdout + run.stderr)
        status = json.loads(self.dngc('status', arc).stdout)
        self.assertEqual(sorted(status['sections']), ['bytes', 'report'])
        self.assertEqual(status['sections']['bytes']['files'], len(self.fx.pairs))
        self.assertEqual(status['runs'], 1)
        for a, _ in self.fx.pairs:
            rel = str(a.relative_to(self.fx.left)); out = self.tmp / f'{impl}-restored' / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            self.assertEqual(self.dngc('restore', arc, rel, out).returncode, 0, rel)
            self.assertEqual(out.read_bytes(), a.read_bytes(), rel)
        verify = self.dngc('verify', arc, '--restore-sample', 2)
        self.assertEqual(json.loads(verify.stdout.split('\n+ ')[0])['problems'], [], verify.stdout)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        # A repeat run reuses everything.
        again = self.dngc('run', self.fx.left, self.fx.right, arc, '--pairs', self.pairs, '--impl', impl, '--workers', 2)
        self.assertIn(again.returncode, (0, 1), again.stderr)
        self.assertEqual(json.loads(self.dngc('status', arc).stdout)['runs'], 2)
        manifest = json.loads((arc / 'ARCHIVE.json').read_text())
        self.assertEqual(manifest['schema'], 'dngc-archive-1')
        self.assertEqual(list(manifest['runs'][0]), ['started', 'finished', 'left', 'right', 'pairs', 'bytes', 'metadata',
                                                     'impl', 'index', 'exit_statuses'])
        self.assertEqual(manifest['runs'][0]['pairs'], str(self.pairs))
        self.assertEqual(list(manifest['runs'][0]['exit_statuses']), ['compare_and_preserve'])
        return arc

    def test_python(self):
        self.check('python')

    def test_ocaml(self):
        self.needs_dng_archive()
        self.check('ocaml')

    @unittest.skipUnless(native.RUST, 'needs dng-compare-all')
    def test_rust(self):
        self.check('rust')


@unittest.skipUnless(native.TOOLS, 'needs exiftool and xdelta3')
class DngcRunTest(DngcRunCases, unittest.TestCase):
    pass


DngcRunNativeTest = native_variant(DngcRunTest)


PHINDEX = next((str(c) for c in [os.environ.get('PHINDEX'), HERE.parent / 'phindex/rust/target/release/phindex',
                                 shutil.which('phindex')] if c and os.access(c, os.X_OK)), None)


class DngcWholeTreeCases(Front):
    """Without --pairs every file under LEFT is kept: matched ones against their
    counterpart, the rest as full copies."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-tree-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fx = native.Fixture(self.tmp)
        (self.fx.left / 'notes.txt').write_text('only on the left')
        (self.fx.left / 'x.jpg').write_bytes(b'\xff\xd8left jpeg')
        (self.fx.right / 'x.jpg').write_bytes(b'\xff\xd8right jpeg')
        # Left out of the walk: .DS_Store, a symlink, and a symlinked directory's contents.
        (self.fx.left / '.DS_Store').write_bytes(b'finder')
        (self.fx.left / 'link.jpg').symlink_to(self.fx.left / 'x.jpg')
        (self.fx.left / 'linked-dir').symlink_to(self.fx.left / 'sub')
        self.lefts = sorted(p for p in self.fx.left.rglob('*')
                            if p.is_file() and not p.is_symlink() and p.name != '.DS_Store'
                            and 'linked-dir' not in p.relative_to(self.fx.left).parts)

    def check(self, impl, *extra):
        arc = self.tmp / f'{impl}-tree'
        run = self.dngc('run', self.fx.left, self.fx.right, arc, '--impl', impl, '--workers', 2, *extra)
        self.assertIn(run.returncode, (0, 1), run.stdout + run.stderr)
        status = json.loads(self.dngc('status', arc).stdout)
        self.assertEqual(status['sections']['bytes']['files'], len(self.lefts), run.stdout)
        for p in self.lefts:
            rel = str(p.relative_to(self.fx.left)); out = self.tmp / f'{impl}-out' / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            self.assertEqual(self.dngc('restore', arc, rel, out).returncode, 0, rel)
            self.assertEqual(out.read_bytes(), p.read_bytes(), rel)
        if not extra:
            self.assertIn(f'{len(self.lefts):,} files under LEFT; ', run.stdout)
            manifest = json.loads((arc / 'ARCHIVE.json').read_text())
            self.assertEqual(manifest['runs'][0]['pairs'], 'whole tree')
            self.assertEqual(list(manifest['runs'][0]['exit_statuses']), ['compare_and_preserve', 'preserve_other'])
            rows = {r['relative_path']: r for r in map(json.loads, (arc / 'bytes/manifest.jsonl').read_text().splitlines())}
            self.assertIsNone(rows['notes.txt']['reference'])
            self.assertEqual(rows['notes.txt']['original'], 'originals/notes.txt')
            self.assertEqual(rows['x.jpg']['reference_relative_path'], 'x.jpg')
        return arc

    def test_python(self):
        self.check('python')

    def test_ocaml(self):
        self.needs_dng_archive()
        self.check('ocaml')

    @unittest.skipUnless(PHINDEX, 'needs phindex')
    def test_ocaml_with_phindex(self):
        self.needs_dng_archive()
        for p in (self.fx.left / '.DS_Store', self.fx.left / 'link.jpg', self.fx.left / 'linked-dir'):
            p.unlink()
        for side in ('left', 'right'):
            r = subprocess.run([PHINDEX, '--db', str(self.tmp / f'{side}.sqlite'), '--root', str(getattr(self.fx, side)),
                                '--workers', '2'], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        arc = self.check('ocaml', '--index', self.tmp / 'left.sqlite', self.tmp / 'right.sqlite')
        # Matched by content: the renamed DNG, which has no counterpart by name, gets one with
        # equal bytes and is kept as a reference instead of a full copy.
        rows = {r['relative_path']: r for r in map(json.loads, (arc / 'bytes/manifest.jsonl').read_text().splitlines())}
        self.assertTrue(rows['sub/renamed.dng']['whole_file_equal'])
        self.assertIsNone(rows['notes.txt']['reference'])


@unittest.skipUnless(native.TOOLS, 'needs exiftool and xdelta3')
class DngcWholeTreeTest(DngcWholeTreeCases, unittest.TestCase):
    pass


DngcWholeTreeNativeTest = native_variant(DngcWholeTreeTest)


class DngcMetadataCases(Front):
    def check(self, impl):
        case = phx.PhindexMetadataTest('test_batches_and_index')
        case.setUp()
        self.addCleanup(case.doCleanups)
        arc = case.tmp / 'archive'
        run = self.dngc('run', case.left, case.right, arc, '--pairs', case.pairs, '--bytes', 'none',
                        '--metadata', 'phindex', '--index', case.tmp / 'left.sqlite', case.tmp / 'right.sqlite',
                        '--impl', impl)
        self.assertIn('phindex', run.stdout, run.stderr)
        self.assertIn('metadata', json.loads(self.dngc('status', arc).stdout)['sections'])
        q = self.dngc('query', arc, 'files', 'EXIF:ExifIFD:LensModel')
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn('a.dng', q.stdout)
        changes = self.dngc('query', arc)
        self.assertIn('XMP:XMP-crs:Exposure2012', changes.stdout)
        self.assertEqual(self.dngc('index', arc).returncode, 0)

    def test_phindex_metadata_section_is_queryable(self):
        self.check('python')

    def test_phindex_metadata_in_ocaml(self):
        self.needs_dng_archive()
        self.check('ocaml')


class DngcMetadataTest(DngcMetadataCases, unittest.TestCase):
    pass


DngcMetadataNativeTest = native_variant(DngcMetadataTest)


@unittest.skipUnless(NATIVE, 'needs the native dngc build (DNGC_NATIVE)')
class DngcParityTest(unittest.TestCase):
    """Both front ends on the same archive: the same output and exit status."""

    def same(self, *args, json_only=False):
        py, nat = run_dngc(PYTHON_DNGC, *args), run_dngc(NATIVE_DNGC, *args)
        self.assertEqual(py.returncode, nat.returncode, (args, py.stderr, nat.stderr))
        if json_only:
            # Lines after the JSON report name the restore commands each runs.
            self.assertEqual(json.loads(py.stdout.split('\n+ ')[0]), json.loads(nat.stdout.split('\n+ ')[0]))
        else:
            self.assertEqual(py.stdout, nat.stdout, args)
            self.assertEqual(py.stderr, nat.stderr, args)
        return nat

    def test_metadata_archive(self):
        case = phx.PhindexMetadataTest('test_batches_and_index')
        case.setUp()
        self.addCleanup(case.doCleanups)
        arc = case.tmp / 'archive'
        run = run_dngc(PYTHON_DNGC, 'run', case.left, case.right, arc, '--pairs', case.pairs, '--bytes', 'none',
                       '--metadata', 'phindex', '--index', case.tmp / 'left.sqlite', case.tmp / 'right.sqlite',
                       '--impl', 'python')
        self.assertIn('metadata', json.loads(run_dngc(PYTHON_DNGC, 'status', arc).stdout)['sections'], run.stderr)
        self.same('status', arc)
        self.same('verify', arc)
        for words in ([], ['changes'], ['files', 'EXIF:ExifIFD:LensModel'], ['values', 'EXIF:ExifIFD:LensModel'],
                      ['values', 'XMP:XMP-crs:Exposure2012'], ['files', 'No:Such:Tag'],
                      ['SELECT', 'tag, category FROM fields ORDER BY tag'],
                      ["SELECT 1.5, 0.1, 1e20, NULL, 7, x'00' IS NULL, 'é'"]):
            self.same('query', arc, *words)
        # A preset without its tag, and bad SQL: an error, status 1, from both.
        for words in (['files'], ['SELEC nonsense']):
            py, nat = run_dngc(PYTHON_DNGC, 'query', arc, *words), run_dngc(NATIVE_DNGC, 'query', arc, *words)
            self.assertEqual((py.returncode, nat.returncode), (1, 1), (py.stderr, nat.stderr))
        # A relative archive path is spelled as Python spells it.
        self.same('status', os.path.relpath(arc, os.getcwd()))

    @unittest.skipUnless(native.TOOLS, 'needs exiftool and xdelta3')
    def test_byte_archive(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-parity-')).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        fx = native.Fixture(tmp)
        pairs = tmp / 'pairs.tsv'
        pairs.write_text(''.join(f'{a}\t{b}\n' for a, b in fx.pairs))
        arc = tmp / 'archive'
        run = run_dngc(PYTHON_DNGC, 'run', fx.left, fx.right, arc, '--pairs', pairs, '--impl', 'python', '--workers', 2)
        self.assertIn(run.returncode, (0, 1), run.stdout + run.stderr)
        self.same('status', arc)
        self.same('verify', arc)
        self.same('verify', arc, '--restore-sample', 3, json_only=True)
        # A missing delta is a problem for both.
        delta = next((arc / 'bytes/deltas').rglob('*.vcdiff'))
        delta.rename(delta.with_suffix('.moved'))
        nat = self.same('verify', arc)
        self.assertEqual(json.loads(nat.stdout)['problems'], ['bytes: 1 deltas or originals named in records are missing'])
        # The legacy layout: the byte section at the top.
        legacy = tmp / 'legacy'
        shutil.copytree(arc / 'bytes', legacy)
        self.assertEqual(json.loads(self.same('status', legacy).stdout)['sections']['bytes']['layout'], 'legacy (top level)')

    def test_usage_errors(self):
        for args in ([], ['-h'], ['bogus'], ['run'], ['run', '-h'], ['verify', '-h'], ['query', '-h'],
                     ['run', 'a', 'b'], ['run', 'a', 'b', 'c', '--bytes', 'x'], ['run', 'a', 'b', 'c', '--workers', 'x'],
                     ['run', 'a', 'b', 'c', '--index', 'x'], ['run', 'a', 'b', 'c', 'd'], ['run', 'a', 'b', 'c', '--frob'],
                     ['run', 'a', 'b', 'c', '--bytes', 'none', '--metadata', 'none'],
                     ['run', 'a', 'b', 'c', '--metadata', 'phindex'],
                     ['run', 'a', 'b', 'c', '--metadata', 'phindex', '--index', '/nonexistent/l', '/nonexistent/r'],
                     ['run', '--pairs=x', 'a', 'b', 'c', '--by=none', '--meta', 'none'],
                     ['status', '/nonexistent/archive'], ['query', '/nonexistent/archive'],
                     ['verify', '/nonexistent/archive'], ['verify', 'a', '--restore-sample', 'z'], ['status', 'a', 'b']):
            self.same(*args)


if __name__ == '__main__':
    unittest.main()

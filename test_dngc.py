"""dngc on the synthetic pairs of test_native_all: each implementation writes an
archive with report/ and bytes/ that restores every left file; status, verify,
index and query work; a phindex metadata section is searchable."""
import json, pathlib, shutil, subprocess, sys, tempfile, unittest

import test_native_all as native
import test_phindex_metadata as phx

HERE = pathlib.Path(__file__).parent
DNGC = [sys.executable, str(HERE / 'dngc.py')]


def dngc(*args):
    return subprocess.run([*DNGC, *map(str, args)], capture_output=True, text=True)


@unittest.skipUnless(native.TOOLS, 'needs exiftool and xdelta3')
class DngcRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fx = native.Fixture(self.tmp)
        self.pairs = self.tmp / 'pairs.tsv'
        self.pairs.write_text(''.join(f'{a}\t{b}\n' for a, b in self.fx.pairs))

    def check(self, impl):
        arc = self.tmp / f'{impl}-archive'
        run = dngc('run', self.fx.left, self.fx.right, arc, '--pairs', self.pairs, '--impl', impl, '--workers', 2)
        self.assertIn(run.returncode, (0, 1), run.stdout + run.stderr)
        status = json.loads(dngc('status', arc).stdout)
        self.assertEqual(sorted(status['sections']), ['bytes', 'report'])
        self.assertEqual(status['sections']['bytes']['files'], len(self.fx.pairs))
        self.assertEqual(status['runs'], 1)
        for a, _ in self.fx.pairs:
            rel = str(a.relative_to(self.fx.left)); out = self.tmp / f'{impl}-restored' / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            self.assertEqual(dngc('restore', arc, rel, out).returncode, 0, rel)
            self.assertEqual(out.read_bytes(), a.read_bytes(), rel)
        verify = dngc('verify', arc, '--restore-sample', 2)
        self.assertEqual(json.loads(verify.stdout.split('\n+ ')[0])['problems'], [], verify.stdout)
        # A repeat run reuses everything.
        again = dngc('run', self.fx.left, self.fx.right, arc, '--pairs', self.pairs, '--impl', impl, '--workers', 2)
        self.assertIn(again.returncode, (0, 1), again.stderr)
        self.assertEqual(json.loads(dngc('status', arc).stdout)['runs'], 2)

    def test_python(self):
        self.check('python')

    @unittest.skipUnless(native.OCAML, 'needs dng-archive')
    def test_ocaml(self):
        self.check('ocaml')

    @unittest.skipUnless(native.RUST, 'needs dng-compare-all')
    def test_rust(self):
        self.check('rust')


class DngcMetadataTest(unittest.TestCase):
    def test_phindex_metadata_section_is_queryable(self):
        case = phx.PhindexMetadataTest('test_batches_and_index')
        case.setUp()
        arc = case.tmp / 'archive'
        run = dngc('run', case.left, case.right, arc, '--pairs', case.pairs, '--bytes', 'none',
                   '--metadata', 'phindex', '--index', case.tmp / 'left.sqlite', case.tmp / 'right.sqlite', '--impl', 'python')
        self.assertIn('phindex', run.stdout, run.stderr)
        self.assertIn('metadata', json.loads(dngc('status', arc).stdout)['sections'])
        q = dngc('query', arc, 'files', 'EXIF:ExifIFD:LensModel')
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertIn('a.dng', q.stdout)
        changes = dngc('query', arc)
        self.assertIn('XMP:XMP-crs:Exposure2012', changes.stdout)
        self.assertEqual(dngc('index', arc).returncode, 0)


if __name__ == '__main__':
    unittest.main()

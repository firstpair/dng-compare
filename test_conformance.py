"""The archive format against its specification (docs/dngc-archive-spec.md).

conformance/validate_archive.py checks an archive against the spec and imports
nothing from dng-compare. conformance/corpus/ holds small archives, valid and
broken, each with the rules the validator must report. This test

- validates every corpus case and compares the rules reported with expected.json;
- writes archives with each implementation (python, ocaml, rust) and the native
  metadata step, and validates them;
- reads the valid cases with both front ends (dngc.py and the native dngc):
  every current record restores byte for byte, dropped paths are refused, and
  verify passes.
- reads the skeleton cases (spec section 13) with dngc.py: after every
  reference file was edited, each record restores byte for byte through the
  skeleton section, and fails as it always did without one.

Cases are materialized at /private/tmp/dngc-conformance/<case>/, where they
were built, because records hold absolute paths."""
import hashlib, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest

import test_native_all as native

HERE = pathlib.Path(__file__).parent
CORPUS = HERE / 'conformance/corpus'
BUILD = pathlib.Path('/private/tmp/dngc-conformance')
VALIDATOR = HERE / 'conformance/validate_archive.py'
NATIVE = native.first_executable([os.environ.get('DNGC_NATIVE'),
                                  HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/dngc.exe',
                                  *sorted(HERE.glob('ocaml-archive/_build*/default/bin/dngc.exe'))])
DNG_ARCHIVE = native.first_executable([os.environ.get('DNG_ARCHIVE'),
                                       HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/main.exe',
                                       *sorted(HERE.glob('ocaml-archive/_build*/default/bin/main.exe'))])
RUST = native.first_executable([HERE / 'rust-scanner/target/release/dng-compare-all'])
XDELTA = shutil.which('xdelta3')
CASES = sorted(p.name for p in CORPUS.iterdir() if (p / 'expected.json').exists()) if CORPUS.is_dir() else []


def materialize(name):
    """The case at the path its records name, copied fresh from the corpus."""
    dst = BUILD / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(CORPUS / name, dst, symlinks=True)
    return dst


def validate(archive, *extra):
    r = subprocess.run([sys.executable, VALIDATOR, archive, '--json', *map(str, extra)], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise AssertionError(r.stdout + r.stderr)
    return json.loads(r.stdout)['problems']


@unittest.skipUnless(XDELTA and CASES, 'needs xdelta3 and the corpus')
class Corpus(unittest.TestCase):
    def test_every_case_reports_exactly_its_rules(self):
        for name in CASES:
            with self.subTest(case=name):
                case = materialize(name)
                expected = json.loads((case / 'expected.json').read_text())
                problems = validate(case / 'archive', '--rebuild')
                self.assertEqual(sorted(problems), expected['rules'], json.dumps(problems, indent=1)[:2000])
                self.assertEqual(expected['valid'], not expected['rules'])


@unittest.skipUnless(XDELTA and shutil.which('exiftool'), 'needs xdelta3 and exiftool')
class Writers(unittest.TestCase):
    """What each implementation writes passes the validator."""
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-conformance-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fx = native.Fixture(self.tmp)
        self.pairs = self.tmp / 'pairs.tsv'
        self.pairs.write_text(''.join(f'{a}\t{b}\n' for a, b in self.fx.pairs))

    def write_and_validate(self, front, impl):
        arc = self.tmp / f'{impl}-archive'
        r = subprocess.run([*front, 'run', self.fx.left, self.fx.right, arc, '--pairs', self.pairs, '--impl', impl,
                            '--workers', '2'], capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        self.assertEqual(validate(arc, '--rebuild'), {})

    def test_python(self):
        self.write_and_validate([sys.executable, HERE / 'dngc.py'], 'python')

    @unittest.skipUnless(NATIVE, 'needs the native dngc')
    def test_ocaml(self):
        self.write_and_validate([NATIVE], 'ocaml')

    @unittest.skipUnless(NATIVE and RUST, 'needs the native dngc and the Rust dng-compare-all')
    def test_rust(self):
        self.write_and_validate([NATIVE], 'rust')

    @unittest.skipUnless(DNG_ARCHIVE and 'current' in CASES, 'needs dng-archive and the corpus')
    def test_native_metadata(self):
        case = materialize('current')
        arc = self.tmp / 'metadata-archive'
        r = subprocess.run([DNG_ARCHIVE, 'phindex-metadata', case / 'L', case / 'R', arc, '--pairs', case / 'pairs.tsv',
                            '--left-index', case / 'l.sqlite', '--right-index', case / 'r.sqlite', '--workers', '2'],
                           capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        self.assertEqual(validate(arc), {})


@unittest.skipUnless(XDELTA and CASES, 'needs xdelta3 and the corpus')
class Readers(unittest.TestCase):
    """Both front ends read the valid cases as the spec says."""
    def fronts(self):
        yield 'python', [sys.executable, HERE / 'dngc.py']
        if NATIVE:
            yield 'native', [NATIVE]

    def test_restore_every_record(self):
        for name in CASES:
            case = CORPUS / name
            if not json.loads((case / 'expected.json').read_text())['valid']:
                continue
            for front, command in self.fronts():
                with self.subTest(case=name, front=front):
                    c = materialize(name)
                    arc = c / 'archive'
                    paths = subprocess.run([*command, 'ls', arc, '-0'], capture_output=True).stdout.decode().split('\0')
                    paths = [p for p in paths if p]
                    self.assertTrue(paths)
                    out = c / 'restored'
                    for p in paths:
                        (out / p).parent.mkdir(parents=True, exist_ok=True)
                        r = subprocess.run([*command, 'restore', arc, p, out / p], capture_output=True, text=True)
                        self.assertEqual(r.returncode, 0, f'{p}: {r.stdout}{r.stderr}')
                        self.assertEqual((out / p).read_bytes(), (c / 'L' / p).read_bytes(), p)
                    v = subprocess.run([*command, 'verify', arc], capture_output=True, text=True)
                    self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
                    trashed = subprocess.run([*command, 'ls', arc, '--trashed'], capture_output=True, text=True).stdout.split()
                    for p in trashed:
                        r = subprocess.run([*command, 'restore', arc, p, out / ('x-' + p.replace('/', '-'))],
                                           capture_output=True, text=True)
                        self.assertEqual(r.returncode, 1, p)
                        self.assertIn('was dropped from the archive', r.stderr)


@unittest.skipUnless(XDELTA and 'skeletons' in CASES, 'needs xdelta3 and the corpus')
class Skeletons(unittest.TestCase):
    """The Python front end reads the skeleton section (spec 5.5 and 13)."""
    DNGC = [sys.executable, HERE / 'dngc.py']

    def records(self, case):
        db = sqlite3.connect(f'file:{case}/archive/bytes/recovery.sqlite3?immutable=1', uri=True)
        self.addCleanup(db.close)
        return {p: json.loads(r) for p, r in db.execute('SELECT path, record FROM files')}

    def restore(self, case, path, name='restored'):
        out = case / name / path
        out.parent.mkdir(parents=True, exist_ok=True)
        return out, subprocess.run([*self.DNGC, 'restore', case / 'archive', path, out], capture_output=True, text=True)

    def test_every_record_restores_after_its_reference_was_edited(self):
        case = materialize('skeletons')
        records = self.records(case)
        formats = set()
        for path, r in sorted(records.items()):
            with self.subTest(path=path):
                if r['reference']:
                    # The case is what it says: no reference holds its record's content any more.
                    self.assertNotEqual(hashlib.sha256(pathlib.Path(r['reference']).read_bytes()).hexdigest(), r['reference_sha256'])
                    formats.add(pathlib.Path(path).suffix)
                out, done = self.restore(case, path)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(out.read_bytes(), (case / 'L' / path).read_bytes())
                self.assertFalse(out.with_name(out.name + '.recovery-partial').exists())
        self.assertEqual(formats, {'.tif', '.jpg', '.txt'})
        self.assertFalse(list((case / 'archive/skeletons').glob('skeletons.sqlite3-*')), 'reading changes nothing')

    def test_verify_counts_the_skeletons(self):
        case = materialize('skeletons')
        v = subprocess.run([*self.DNGC, 'verify', case / 'archive'], capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
        report = json.loads(v.stdout)
        self.assertEqual((report['skeletons'], report['problems']), (5, []))
        self.assertIn('skeletons', report['sections'])
        (case / 'archive/skeletons/skeletons.sqlite3').write_bytes(b'not a database')
        v = subprocess.run([*self.DNGC, 'verify', case / 'archive'], capture_output=True, text=True)
        self.assertEqual(v.returncode, 1, v.stdout + v.stderr)
        self.assertIn('skeletons: ', json.loads(v.stdout)['problems'][0])

    def test_without_a_skeleton_restore_fails_as_before(self):
        """Edited references and no skeleton of their contents: the errors of
        an archive without the section, whether the section or only the row is gone."""
        for gone in ('row', 'section'):
            case = materialize('skeletons')
            if gone == 'section':
                shutil.rmtree(case / 'archive/skeletons')
            else:
                db = sqlite3.connect(case / 'archive/skeletons/skeletons.sqlite3')
                db.execute('DELETE FROM skeletons'); db.commit(); db.close()
            for path, message in (('p/changed.tif', 'Bumer reference SHA-256 mismatch'),
                                  ('p/same.jpg', 'Original file recovery hash mismatch'), ('b/only.dat', None)):
                with self.subTest(gone=gone, path=path):
                    out, done = self.restore(case, path)
                    self.assertEqual(done.returncode != 0, bool(message), done.stdout + done.stderr)
                    if message:
                        self.assertIn(message, done.stderr)
                        self.assertFalse(out.exists() or out.with_name(out.name + '.recovery-partial').exists())
            (case / 'R/p/same.tif').unlink()
            self.assertIn('FileNotFoundError', self.restore(case, 'p/same.tif')[1].stderr)

    def test_an_unsound_skeleton_restores_nothing(self):
        expected = {'skeleton-corrupt': ('p/same.tif', 'its skeleton cannot be used: zstd'),
                    'skeleton-wrong-sha256': ('p/changed.jpg', 'does not match its skeleton_sha256'),
                    'skeleton-blocks-out-of-order': ('p/changed.tif', 'blocks out of order in a skeleton record'),
                    'skeleton-lost-block': ('p/changed.tif', 'its skeleton does not rebuild it: block 0/strips (6001 bytes) is not in '),
                    'skeleton-changed-middle': ('p/changed.jpg', 'its skeleton rebuilds other bytes')}
        for name, (path, message) in expected.items():
            with self.subTest(case=name):
                case = materialize(name)
                out, done = self.restore(case, path)
                self.assertNotEqual(done.returncode, 0, done.stdout)
                self.assertIn(message, done.stderr)
                self.assertFalse(out.exists() or out.with_name(out.name + '.recovery-partial').exists())
                # A record whose own skeleton and reference are sound still restores.
                out, done = self.restore(case, 'p/motorola.tif')
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertEqual(out.read_bytes(), (case / 'L/p/motorola.tif').read_bytes())


if __name__ == '__main__':
    unittest.main()

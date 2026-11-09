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

Cases are materialized at /private/tmp/dngc-conformance/<case>/, where they
were built, because records hold absolute paths."""
import json, os, pathlib, shutil, subprocess, sys, tempfile, unittest

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


if __name__ == '__main__':
    unittest.main()

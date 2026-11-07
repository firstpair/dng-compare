"""The one-pass native `all` command (docs/native-all.md) against the Python tools:
compare_dng.py --pairs-only and preserve_apo.py scan --delta --pairs on the same
synthetic pairs must give the same report and the same archive, and Python
restores what the native command preserved. Each implementation is skipped when
its binary is missing: DNG_COMPARE_ALL_RUST (else rust-scanner's release build)
and DNG_ARCHIVE (else ocaml-archive's builds, else PATH)."""
import json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest

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


if __name__ == '__main__':
    unittest.main()

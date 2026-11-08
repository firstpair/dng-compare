"""The byte section of an archive, one rule for every tool (archive_layout.py,
docs/dngc.md): a new archive gets bytes/, a legacy archive (recovery.sqlite3
at its top, no bytes/) keeps its top, and ARCHIVE/bytes given directly is used
as it is, never nested. Every writer in Python, OCaml and Rust lays out the
same files; readers and the metadata tools find the sections from the top;
Semantic Unison's reader rule agrees."""
import json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest

import test_native_all as native
import test_phindex_metadata as phx
from archive_layout import archive_top, byte_section

HERE = pathlib.Path(__file__).parent
PY = sys.executable
DNGC_NATIVE = native.first_executable([os.environ.get('DNGC_NATIVE'),
                                       HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/dngc.exe',
                                       *sorted(HERE.glob('ocaml-archive/_build*/default/bin/dngc.exe'))])


def unison_reader(archive):
    """Semantic Unison's semdiff rule (U-36): ARCHIVE itself if it has
    recovery.sqlite3 and no bytes/, otherwise ARCHIVE/bytes."""
    a = pathlib.Path(archive)
    return a if (a / 'recovery.sqlite3').exists() and not (a / 'bytes').exists() else a / 'bytes'


def byte_files(section):
    """The byte section's files, apart from SQLite's transient -wal and -shm."""
    return sorted(str(p.relative_to(section)) for p in section.rglob('*')
                  if p.is_file() and not p.name.endswith(('-wal', '-shm')))


class RuleTest(unittest.TestCase):
    def test_rule(self):
        with tempfile.TemporaryDirectory() as t:
            t = pathlib.Path(t)
            new, legacy, unified = t / 'new', t / 'legacy', t / 'unified'
            new.mkdir(); legacy.mkdir(); (unified / 'bytes').mkdir(parents=True)
            (legacy / 'recovery.sqlite3').write_bytes(b'')
            (unified / 'bytes/recovery.sqlite3').write_bytes(b'')
            self.assertEqual(byte_section(new), new / 'bytes')
            self.assertEqual(byte_section(t / 'missing'), t / 'missing/bytes')
            self.assertEqual(byte_section(legacy), legacy)
            self.assertEqual(byte_section(unified), unified / 'bytes')
            self.assertEqual(byte_section(unified / 'bytes'), unified / 'bytes')
            self.assertEqual(byte_section(new / 'bytes'), new / 'bytes')
            self.assertEqual(byte_section(str(new) + '/'), new / 'bytes')
            for top in (new, legacy, unified):
                self.assertEqual(byte_section(top), unison_reader(top), top)
                self.assertEqual(archive_top(byte_section(top) if top != legacy else top), top)


@unittest.skipUnless(native.TOOLS and native.OCAML, 'needs exiftool, xdelta3 and dng-archive')
class WritersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dng-layout-')).resolve()
        cls.fx = native.Fixture(cls.tmp)
        cls.tsv = cls.tmp / 'pairs.tsv'
        cls.tsv.write_text(''.join(f'{a}\t{b}\n' for a, b in cls.fx.pairs))
        cls.jsonl = cls.tmp / 'pairs.jsonl'
        cls.jsonl.write_text(''.join(json.dumps({'source': str(a), 'reference': str(b)}) + '\n' for a, b in cls.fx.pairs))
        cls.runs = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def writers(self):
        left, right = str(self.fx.left), str(self.fx.right)
        w = {
            'preserve_apo.py': lambda a: [PY, str(HERE / 'preserve_apo.py'), 'scan', left, right, str(a), '--delta',
                                          '--pairs', str(self.jsonl)],
            'dng-archive preserve': lambda a: [native.OCAML, 'preserve', left, right, str(a), '--delta', '--pairs', str(self.jsonl)],
            'compare_dng.py': lambda a: [PY, str(HERE / 'compare_dng.py'), left, right, '--pairs-only', '--pairs', str(self.tsv),
                                         '--output', str(self.report()), '--archive', str(a)],
            'dng-archive all': lambda a: [native.OCAML, 'all', left, right, '--pairs', str(self.tsv), '--report', str(self.report()),
                                          '--archive', str(a)],
        }
        if native.RUST:
            w['dng-compare-all'] = lambda a: [native.RUST, left, right, '--pairs', str(self.tsv), '--report', str(self.report()),
                                              '--archive', str(a)]
        return w

    def report(self):
        type(self).runs += 1
        return self.tmp / f'report-{self.runs}'

    def run_writer(self, name, command):
        r = subprocess.run(command, capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 1), (name, r.stdout, r.stderr))
        return r

    def restores(self, archive, tag):
        """Every left file restored from the archive's top by each reader."""
        readers = {'preserve_apo.py': lambda rel, out: [PY, str(HERE / 'preserve_apo.py'), 'restore', str(archive), rel, str(out)],
                   'dng-archive': lambda rel, out: [native.OCAML, 'restore', str(archive), rel, str(out)],
                   'dngc.py': lambda rel, out: [PY, str(HERE / 'dngc.py'), 'restore', str(archive), rel, str(out)]}
        if DNGC_NATIVE:
            readers['dngc'] = lambda rel, out: [DNGC_NATIVE, 'restore', str(archive), rel, str(out)]
        for reader, command in readers.items():
            for a, _ in self.fx.pairs:
                rel = str(a.relative_to(self.fx.left))
                out = self.tmp / f'{tag}-{reader}' / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(command(rel, out), capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, (reader, rel, r.stdout, r.stderr))
                self.assertEqual(out.read_bytes(), a.read_bytes(), (reader, rel))

    def status(self, archive):
        out = {}
        for front, cmd in (('dngc.py', [PY, str(HERE / 'dngc.py')]), ('dngc', [DNGC_NATIVE] if DNGC_NATIVE else None)):
            if cmd:
                for verb in ('status', 'verify'):
                    r = subprocess.run([*cmd, verb, str(archive)], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, (front, verb, r.stdout, r.stderr))
                    out[front, verb] = json.loads(r.stdout)
        return out

    def test_new_archive_gets_bytes(self):
        layouts = {}
        for name, command in self.writers().items():
            archive = self.tmp / ('new-' + name.replace(' ', '-'))
            r = self.run_writer(name, command(archive))
            self.assertEqual(sorted(os.listdir(archive)), ['bytes'], name)
            self.assertIn(f'Byte section: {archive / "bytes"}', r.stdout, name)
            layouts[name] = byte_files(archive / 'bytes')
            self.assertEqual(unison_reader(archive), archive / 'bytes')
            self.restores(archive, 'new-' + name.replace(' ', '-'))
            statuses = self.status(archive)
            for (front, verb), s in statuses.items():
                if verb == 'status':
                    self.assertEqual(s['sections']['bytes']['layout'], 'bytes/', (name, front))
                    self.assertEqual(s['sections']['bytes']['files'], len(self.fx.pairs), (name, front))
                else:
                    self.assertEqual(s['problems'], [], (name, front))
        first = next(iter(layouts.values()))
        for name, files in layouts.items():
            self.assertEqual(files, first, name)
        self.assertIn('recovery.sqlite3', first)

    def test_legacy_archive_keeps_its_top(self):
        import preserve_apo
        for name, command in self.writers().items():
            archive = self.tmp / ('legacy-' + name.replace(' ', '-'))
            with open(os.devnull, 'w') as null:
                old = sys.stdout; sys.stdout = null
                try:  # the API writes where it is told: a legacy archive
                    self.assertTrue(preserve_apo.scan(self.fx.left, self.fx.right, archive, HERE, 200, delta_mode=True,
                                                      pairs=[(str(a), str(b)) for a, b in self.fx.pairs]))
                finally:
                    sys.stdout = old
            r = self.run_writer(name, command(archive))
            self.assertIn(f'Byte section: {archive}\n', r.stdout, name)
            self.assertFalse((archive / 'bytes').exists(), name)
            self.assertTrue((archive / 'recovery.sqlite3').exists(), name)
            self.assertEqual(unison_reader(archive), archive)
            self.restores(archive, 'legacy-' + name.replace(' ', '-'))
            for (front, verb), s in self.status(archive).items():
                if verb == 'status':
                    self.assertEqual(s['sections']['bytes']['layout'], 'legacy (top level)', (name, front))

    def test_bytes_given_directly_is_not_nested(self):
        for name, command in self.writers().items():
            top = self.tmp / ('direct-' + name.replace(' ', '-'))
            r = self.run_writer(name, command(top / 'bytes'))
            self.assertFalse((top / 'bytes/bytes').exists(), name)
            self.assertTrue((top / 'bytes/recovery.sqlite3').exists(), name)
            self.assertIn(f'Byte section: {top / "bytes"}\n', r.stdout, name)
            self.restores(top / 'bytes', 'direct-' + name.replace(' ', '-'))
            self.restores(top, 'direct-top-' + name.replace(' ', '-'))


@unittest.skipUnless(native.OCAML, 'needs dng-archive')
class MetadataTopTest(unittest.TestCase):
    """index and phindex-metadata write the metadata section at the top, also
    when given ARCHIVE/bytes; they never create bytes/."""

    def setUp(self):
        self.case = phx.PhindexMetadataTest('test_batches_and_index')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def phindex_metadata(self, command, archive):
        c = self.case
        r = subprocess.run([*command, str(c.left), str(c.right), str(archive), '--pairs', str(c.pairs),
                            '--left-index', str(c.tmp / 'left.sqlite'), '--right-index', str(c.tmp / 'right.sqlite')],
                           capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)

    def test_metadata_section_at_the_top(self):
        for tag, command in (('py', [PY, str(HERE / 'phindex_metadata.py')]), ('ml', [native.OCAML, 'phindex-metadata'])):
            for given in ('top', 'bytes'):
                top = self.case.tmp / f'{tag}-{given}'
                self.phindex_metadata(command, top if given == 'top' else top / 'bytes')
                self.assertEqual(sorted(os.listdir(top)), ['.metadata-preservation.lock', 'metadata'], (tag, given))
                os.remove(top / 'metadata/differences.sqlite3')
                for index in ([PY, str(HERE / 'metadata_index.py')], [native.OCAML, 'index']):
                    r = subprocess.run([*index, str(top / 'bytes')], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(json.loads(r.stdout.splitlines()[-1])['indexed_files'], 3, (tag, given, index))
                    self.assertFalse((top / 'bytes').exists(), (tag, given, index))
                with sqlite3.connect(f'file:{top / "metadata/differences.sqlite3"}?mode=ro', uri=True) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 3)


if __name__ == '__main__':
    unittest.main()

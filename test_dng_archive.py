"""dng-archive (the OCaml index and preservation command) against the Python
tools: each restores what the other preserved, and both build the same
metadata index from the same batches. Skipped without a dng-archive binary
(DNG_ARCHIVE, else ocaml-archive's build, else PATH) or xdelta3."""
import gzip, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest, zlib

import metadata_index, preserve_apo

HERE = pathlib.Path(__file__).parent


def find_binary():
    candidates = [os.environ.get('DNG_ARCHIVE')]
    candidates += [str(p) for p in sorted(HERE.glob('ocaml-archive/_build*/default/bin/main.exe'))]
    candidates.append(shutil.which('dng-archive'))
    return next((c for c in candidates if c and os.access(c, os.X_OK)), None)


BINARY = find_binary()


def files_in(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


@unittest.skipUnless(BINARY and shutil.which('xdelta3'), 'needs dng-archive and xdelta3')
class DngArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dng-archive-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.source, self.reference = self.tmp / 'shuttle', self.tmp / 'photo'
        body = bytes((i * 31 + 7) % 256 for i in range(3_000_000))
        files = {
            'a/same.DNG': (body, body),
            'a/meta.DNG': (body[:1000] + b'rating=5' + body[1008:], body),
            'b/grown.jpg': (body + b'trailing preview', body),
            'b/alone.DNG': (body[::-1], None),
        }
        self.pairs = []
        for rel, (src, ref) in files.items():
            s = self.source / rel; s.parent.mkdir(parents=True, exist_ok=True); s.write_bytes(src)
            r = None
            if ref is not None:
                r = self.reference / ('elsewhere/' + rel); r.parent.mkdir(parents=True, exist_ok=True); r.write_bytes(ref)
            self.pairs.append((s, r))
        os.setxattr(self.source / 'a/meta.DNG', 'user.dng-archive-test', b'kept\x00bytes') if hasattr(os, 'setxattr') else None
        self.reference.mkdir(exist_ok=True)
        self.pairs_file = self.tmp / 'pairs.jsonl'
        self.pairs_file.write_text(''.join(json.dumps(dict(source=str(s), reference=str(r) if r else None)) + '\n'
                                           for s, r in self.pairs))
        self.originals = files_in(self.source)

    def ml(self, *args):
        r = subprocess.run([BINARY, *map(str, args)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def py_preserve(self, archive):
        pairs = [(str(s), str(r) if r else None) for s, r in self.pairs]
        self.assertTrue(preserve_apo.scan(self.source, self.reference, archive, HERE, 200, delta_mode=True, pairs=pairs))

    def py_restore(self, archive, rel, out):
        r = subprocess.run([sys.executable, str(HERE / 'preserve_apo.py'), 'restore', str(archive), rel, str(out)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def records(self, archive):
        db = sqlite3.connect(archive / 'recovery.sqlite3')
        return {p: json.loads(r) for p, r in db.execute('SELECT path,record FROM files')}

    def check_restores(self, archive, restore):
        for rel, data in self.originals.items():
            out = self.tmp / ('restored-' + rel.replace('/', '_'))
            restore(archive, rel, out)
            self.assertEqual(out.read_bytes(), data, rel)
            self.assertEqual(os.stat(out).st_mtime_ns, os.stat(self.source / rel).st_mtime_ns, rel)
            if rel == 'a/meta.DNG' and hasattr(os, 'getxattr'):
                self.assertEqual(os.getxattr(out, 'user.dng-archive-test'), b'kept\x00bytes')
            out.unlink()

    def test_ocaml_preserves_python_restores(self):
        archive = self.tmp / 'archive-ml'
        self.ml('preserve', self.source, self.reference, archive, '--delta', '--pairs', self.pairs_file, '--workers', 2)
        records = self.records(archive)
        self.assertEqual(sorted(records), sorted(self.originals))
        self.assertTrue(records['a/same.DNG']['whole_file_equal'])
        self.assertTrue(records['a/meta.DNG']['delta'].endswith('.vcdiff'))
        self.assertEqual(records['b/alone.DNG']['original'], 'originals/b/alone.DNG')
        self.assertEqual(records['a/meta.DNG']['reference_relative_path'], 'elsewhere/a/meta.DNG')
        self.check_restores(archive, self.py_restore)
        self.check_restores(archive, lambda a, rel, out: self.ml('restore', a, rel, out))
        # A second run finds every record still valid and changes none.
        before = (archive / 'recovery.sqlite3').read_bytes() and self.records(archive)
        self.ml('preserve', self.source, self.reference, archive, '--delta', '--pairs', self.pairs_file)
        self.assertEqual(self.records(archive), before)
        self.assertEqual(files_in(self.source), self.originals, 'source tree written')

    def test_python_preserves_ocaml_restores_and_agrees(self):
        py, ml = self.tmp / 'archive-py', self.tmp / 'archive-ml'
        self.py_preserve(py)
        self.check_restores(py, lambda a, rel, out: self.ml('restore', a, rel, out))
        self.ml('preserve', self.source, self.reference, ml, '--delta', '--pairs', self.pairs_file)
        # The same records, field for field, apart from where the archive is.
        a, b = self.records(py), self.records(ml)
        self.assertEqual(sorted(a), sorted(b))
        for rel in a:
            self.assertEqual(list(a[rel]), list(b[rel]), rel)
            for k in a[rel]:
                if k != 'delta_sha256' and k != 'preserved_bytes':
                    self.assertEqual(a[rel][k], b[rel][k], (rel, k))
        # An OCaml run over a Python archive keeps its records.
        self.ml('preserve', self.source, self.reference, py, '--delta', '--pairs', self.pairs_file)
        self.assertEqual(self.records(py), a)

    def test_index_matches_python(self):
        rows = []
        for i, (s, r) in enumerate(self.pairs):
            rows.append(dict(relative_path=str(s.relative_to(self.source)), version='' if i else '20261030T000000Z',
                             apo=str(s), bumer=str(r) if r else '',
                             apo_metadata={'XMP:XMP-xmp:Rating': i, 'File:System:FileSize': 3 + i, 'SourceFile': 'x'},
                             bumer_metadata={'XMP:XMP-xmp:Rating': 0, 'EXIF:IFD0:Make': 'Leica Camera AG ü 📷'},
                             differences=[dict(tag='XMP:XMP-xmp:Rating', kind='changed_in_bumer', apo=i, bumer=0, category='embedded'),
                                          dict(tag='EXIF:IFD0:Make', kind='added_in_bumer', bumer={'b': [1.5, 1e-05, 1e16, 'ü\n'], 'a': None}, category='embedded')],
                             errors=[], warnings=[]))
        rows.append(dict(relative_path='x/.DS_Store', apo='/x/.DS_Store', bumer='', apo_metadata={}, bumer_metadata={},
                         differences=[], errors=[], warnings=[]))
        for name in ('py', 'ml'):
            batches = self.tmp / name / 'metadata/batches'; batches.mkdir(parents=True)
            (batches / 'b-000001.jsonl.gz').write_bytes(gzip.compress(''.join(json.dumps(r) + '\n' for r in rows).encode()))
        db = metadata_index.open_index(self.tmp / 'py')
        for p in sorted((self.tmp / 'py/metadata/batches').glob('*.jsonl.gz')):
            metadata_index.ingest(db, p)
        db.close()
        self.ml('index', self.tmp / 'ml')
        r = subprocess.run([sys.executable, str(HERE / 'ocaml-archive/test/compare_index.py'),
                            str(self.tmp / 'py/metadata/differences.sqlite3'), str(self.tmp / 'ml/metadata/differences.sqlite3')],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        # Adding the same batch again changes nothing, whichever tool built the index.
        self.ml('index', self.tmp / 'py')
        r = subprocess.run([sys.executable, str(HERE / 'ocaml-archive/test/compare_index.py'),
                            str(self.tmp / 'py/metadata/differences.sqlite3'), str(self.tmp / 'ml/metadata/differences.sqlite3')],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == '__main__':
    unittest.main()

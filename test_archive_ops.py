"""dngc ls, drop, trash and keep on a synthetic archive, in both front ends:
dngc.py and the native dngc (DNGC_NATIVE, else ocaml-archive's build; skipped
without it). The same steps run on two copies of one archive; outputs (drop ids
and times normalized) and the resulting databases, trash and payloads must be
equal, and each step must do what it says."""
import json, os, pathlib, re, shutil, sqlite3, subprocess, sys, tempfile, unittest

import test_native_all as native

HERE = pathlib.Path(__file__).parent
NATIVE = native.first_executable([os.environ.get('DNGC_NATIVE'),
                                  HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/dngc.exe',
                                  *sorted(HERE.glob('ocaml-archive/_build*/default/bin/dngc.exe'))])


def normalize(text, archive):
    text = text.replace(str(archive), 'ARCHIVE')
    text = re.sub(r'\d{8}T\d{6}Z(-\d+)?', 'DROP', text)
    return re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}', 'TIME', text)


def state(archive):
    """The byte and metadata databases' rows, the trash and payload files, and the
    drops and keeps of ARCHIVE.json, with drop ids and times normalized."""
    out = {}
    db = sqlite3.connect(f'file:{archive}/bytes/recovery.sqlite3?immutable=1', uri=True)
    for t in ('files', 'versions', 'trash', 'dropped'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
            out[t] = sorted(map(repr, db.execute(f'SELECT * FROM {t}')))
    db = sqlite3.connect(f'file:{archive}/metadata/differences.sqlite3?immutable=1', uri=True)
    for t in ('files', 'differences', 'trash_files'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
            cols = 'path, version, extension, compared, errors' if t == 'files' else \
                'kind, apo_value, bumer_value' if t == 'differences' else 'drop_id, path, differences'
            out['metadata.' + t] = sorted(map(repr, db.execute(f'SELECT {cols} FROM {t}')))
    out['tree'] = sorted(str(p.relative_to(archive)) for p in archive.rglob('*')
                         if p.is_file() and 'trash' in p.parts or (p.is_file() and p.parts[-3:-1] and
                                                                   ('originals' in p.parts or 'deltas' in p.parts)))
    m = json.loads((archive / 'ARCHIVE.json').read_text())
    out['manifest'] = {k: m.get(k) for k in ('drops', 'keeps')}
    return json.loads(normalize(json.dumps(out), archive))


@unittest.skipUnless(NATIVE, 'no native dngc build')
class ArchiveOpsParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-ops-')).resolve()
        L, R = cls.tmp / 'L', cls.tmp / 'R'
        for d in ('L/a', 'L/b', 'R/a', 'R/b'):
            (cls.tmp / d).mkdir(parents=True)
        (L / 'a/same.txt').write_text('same\n'); shutil.copy(L / 'a/same.txt', R / 'a/same.txt')
        big = os.urandom(50000)
        (R / 'b/big.bin').write_bytes(big); (L / 'b/big.bin').write_bytes(big + b'tail')
        (L / 'b/only.dat').write_text('only here\n')
        for d, text in (('a', '[x]\n'), ('b', '[y]\n')):
            (L / d / 'Picasa.ini').write_text(text); shutil.copy(L / d / 'Picasa.ini', R / d / 'Picasa.ini')
        cls.base = cls.tmp / 'base'
        run = subprocess.run([sys.executable, HERE / 'dngc.py', 'run', L, R, cls.base, '--impl', 'python',
                              '--metadata', 'none'], capture_output=True, text=True)
        assert run.returncode == 0, run.stdout + run.stderr
        # An earlier content of b/only.dat, as a refilled source leaves it.
        db = sqlite3.connect(cls.base / 'bytes/recovery.sqlite3')
        rec = json.loads(db.execute("SELECT record FROM files WHERE path='b/only.dat'").fetchone()[0])
        (cls.base / 'bytes/versions/OLD/originals/b').mkdir(parents=True)
        shutil.copy(cls.base / 'bytes' / rec['original'], cls.base / 'bytes/versions/OLD/originals/b/only.dat')
        db.execute('INSERT INTO versions VALUES (?,?,?)',
                   ('b/only.dat', 'OLD', json.dumps(dict(rec, original='versions/OLD/originals/b/only.dat'))))
        db.commit(); db.close()
        # A metadata index with a changed tag for two of the files.
        import metadata_index
        (cls.base / 'metadata').mkdir(exist_ok=True)
        idx = metadata_index.open_index(cls.base)
        idx.execute("INSERT INTO batches VALUES ('b0', 'x')")
        idx.execute("INSERT INTO fields(tag, category) VALUES ('XMP:XMP-xmp:Rating', 'embedded')")
        for p in ('a/Picasa.ini', 'b/big.bin'):
            fid = idx.execute("INSERT INTO files(path, version, extension, batch, compared, errors) VALUES (?, '', ?, 'b0', 1, 0)",
                              (p, pathlib.Path(p).suffix)).lastrowid
            idx.execute('INSERT INTO differences VALUES (?, 1, ?, NULL, NULL)', (fid, 'changed_in_bumer'))
        idx.commit(); idx.close()
        cls.L, cls.R = L, R

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def steps(self, command, archive):
        """Every verb in turn; (step, exit status, stdout, stderr) normalized."""
        out = []
        def run(name, *args, stdin=None):
            r = subprocess.run([*command, *map(str, args)], capture_output=True, input=stdin)
            out.append((name, r.returncode, normalize(r.stdout.decode('utf-8', 'replace'), archive),
                        normalize(r.stderr.decode('utf-8', 'replace'), archive)))
            return r
        A = archive
        run('ls', 'ls', A)
        run('ls json', 'ls', A, '--kind', 'delta', '--json')
        run('ls references', 'ls', A, '--glob', '*.ini', '--print', 'reference', '-0')
        run('ls changed', 'ls', A, '--changed', 'Rating')
        run('drop dry', 'drop', A, '--glob', '*.ini')
        run('drop', 'drop', A, '--glob', '*.ini', '--yes')
        run('ls after', 'ls', A)
        run('ls trashed', 'ls', A, '--trashed')
        run('restore refused', 'restore', A, 'a/Picasa.ini', self.tmp / f'{A.name}-out')
        run('verify', 'verify', A)
        run('rerun skips', 'run', self.L, self.R, A, '--impl', 'python', '--metadata', 'none')
        drop = json.loads((A / 'ARCHIVE.json').read_text())['drops'][0]['id']
        run('drop payloads', 'drop', A, '--from', '-', '--yes', stdin=b'b/only.dat\nb/big.bin\nnope.txt\n')
        run('trash list', 'trash', A)
        second = json.loads((A / 'ARCHIVE.json').read_text())['drops'][1]['id']
        run('trash restore', 'trash', A, 'restore', second)
        run('keep dry', 'keep', A, '--glob', 'a/*', '--glob', 'b/big.bin')
        run('keep', 'keep', A, '--glob', 'a/same.txt', '--glob', 'b/big.bin', '--yes')
        run('empty dry', 'trash', A, 'empty')
        run('empty', 'trash', A, 'empty', drop, '--yes')
        run('force', 'drop', A, '--glob', 'b/only.dat', '--force')
        third = json.loads((A / 'ARCHIVE.json').read_text())['drops'][2]['id']
        run('release', 'trash', A, 'release', third)
        run('status', 'status', A)
        run('verify end', 'verify', A)
        run('bad action', 'trash', A, 'nope')
        return out

    def test_front_ends_agree(self):
        py, ml = self.tmp / 'py', self.tmp / 'ml'
        shutil.copytree(self.base, py); shutil.copytree(self.base, ml)
        a = self.steps([sys.executable, HERE / 'dngc.py'], py)
        b = self.steps([NATIVE], ml)
        for x, y in zip(a, b):
            if x[0] in ('rerun skips',):  # each front end names its own tools
                self.assertEqual(x[1], y[1], x[0])
                continue
            self.assertEqual(y, x, x[0])
        self.assertEqual(state(ml), state(py))
        steps = {s[0]: s for s in a}
        self.assertEqual(steps['ls'][2].split(), ['a/Picasa.ini', 'a/same.txt', 'b/Picasa.ini', 'b/big.bin', 'b/only.dat'])
        self.assertEqual(steps['ls changed'][2].split(), ['a/Picasa.ini', 'b/big.bin'])
        self.assertEqual(steps['ls references'][2].count('\0'), 2)
        self.assertEqual(steps['ls after'][2].split(), ['a/same.txt', 'b/big.bin', 'b/only.dat'])
        self.assertEqual(steps['restore refused'][1], 1)
        self.assertIn('was dropped from the archive (drop DROP); dngc trash restore DROP puts it back', steps['restore refused'][3])
        self.assertIn('2 listed files were dropped from the archive (dngc drop) and are skipped', steps['rerun skips'][2])
        self.assertIn('not in the archive: nope.txt', steps['drop payloads'][3])
        self.assertIn('2 records (1 earlier version) and 3 payloads', steps['drop payloads'][2])
        self.assertIn('kept 2 files', steps['keep'][2])
        self.assertIn('"problems": []', steps['verify end'][2])
        self.assertEqual(steps['bad action'][1], 2)
        st = state(py)
        self.assertEqual(len(st['dropped']), 2)  # the .ini drop (emptied keeps its tombstones); only.dat's released
        self.assertEqual(st.get('trash', []), [])
        self.assertEqual([d['state'] for d in st['manifest']['drops']], ['emptied', 'restored', 'deleted'])
        # The kept files restore with the reference tree gone.
        for p in ('a/same.txt', 'b/big.bin'):
            out = self.tmp / ('kept-' + p.replace('/', '-'))
            r = subprocess.run([NATIVE, 'restore', ml, p, out], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(out.read_bytes(), (self.L / p).read_bytes())

    def test_help_agrees(self):
        for verb in ('ls', 'drop', 'trash', 'keep'):
            a = subprocess.run([sys.executable, HERE / 'dngc.py', verb, '--help'], capture_output=True, text=True)
            b = subprocess.run([NATIVE, verb, '--help'], capture_output=True, text=True)
            self.assertEqual(b.stdout, a.stdout, verb)
        a = subprocess.run([sys.executable, HERE / 'dngc.py', '--help'], capture_output=True, text=True)
        b = subprocess.run([NATIVE, '--help'], capture_output=True, text=True)
        self.assertEqual(b.stdout, a.stdout)


if __name__ == '__main__':
    unittest.main()

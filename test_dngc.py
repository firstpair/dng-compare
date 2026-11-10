"""dngc on the synthetic pairs of test_native_all: each implementation writes an
archive with report/ and bytes/ that restores every left file; status, verify,
index and query work; a phindex metadata section is searchable.

Every case runs against both front ends: dngc.py, the reference, and the native
binary (DNGC_NATIVE, else ocaml-archive's OxCaml build, else any other
ocaml-archive build; its cases are skipped when there is none). The parity
cases require equal output from the two on the same archive."""
import fcntl, json, os, pathlib, re, shutil, subprocess, sys, tempfile, unittest

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
        # A hidden file (an interrupted copy's temp file): phindex align leaves names
        # beginning with a dot out, so dngc keeps it as a full copy instead of refusing.
        hidden = self.fx.left / 'sub' / '.renamed.dng.Xy12'
        hidden.parent.mkdir(exist_ok=True)
        hidden.write_bytes(b'II*\x00partial copy')
        self.lefts = sorted(self.lefts + [hidden])
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
        self.assertIsNone(rows['sub/.renamed.dng.Xy12']['reference'])


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
        # A skeleton section, written by the native dngc: both count and check it.
        built = run_dngc(NATIVE_DNGC, 'skeleton', 'build', arc, '--workers', 2)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertGreater(json.loads(self.same('verify', arc).stdout)['skeletons'], 0)
        self.assertEqual(sorted(json.loads(self.same('status', arc).stdout)['sections']), ['bytes', 'report', 'skeletons'])
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

    @unittest.skipUnless(native.TOOLS, 'needs exiftool and xdelta3')
    def test_new_archive_layout(self):
        """Both front ends, each implementation: a new archive's bytes in bytes/,
        the same files, ARCHIVE.json and report/ at the top."""
        from test_archive_layout import byte_files
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-layout-')).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        fx = native.Fixture(tmp)
        (fx.left / 'notes.txt').write_text('only on the left')
        layouts = {}
        for front_name, front in (('python', PYTHON_DNGC), ('native', NATIVE_DNGC)):
            for impl in ('python', 'ocaml', *(['rust'] if native.RUST else [])):
                if front is PYTHON_DNGC and impl == 'ocaml' and not native.OCAML:
                    continue
                arc = tmp / f'{front_name}-{impl}'
                r = run_dngc(front, 'run', fx.left, fx.right, arc, '--impl', impl, '--workers', 2)
                self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
                self.assertEqual(sorted(os.listdir(arc)), ['ARCHIVE.json', 'bytes', 'report'], (front_name, impl))
                self.assertIn(f'Byte section: {arc / "bytes"}', r.stdout, (front_name, impl))
                layouts[front_name, impl] = byte_files(arc / 'bytes')
        first = next(iter(layouts.values()))
        for k, v in layouts.items():
            self.assertEqual(v, first, k)

    def test_usage_errors(self):
        for args in ([], ['-h'], ['bogus'], ['run'], ['run', '-h'], ['verify', '-h'], ['query', '-h'],
                     ['run', 'a', 'b'], ['run', 'a', 'b', 'c', '--bytes', 'x'], ['run', 'a', 'b', 'c', '--workers', 'x'],
                     ['run', 'a', 'b', 'c', '--index', 'x'], ['run', 'a', 'b', 'c', 'd'], ['run', 'a', 'b', 'c', '--frob'],
                     ['run', 'a', 'b', 'c', '--bytes', 'none', '--metadata', 'none'],
                     ['run', 'a', 'b', 'c', '--metadata', 'phindex'],
                     ['run', 'a', 'b', 'c', '--metadata', 'phindex', '--index', '/nonexistent/l', '/nonexistent/r'],
                     ['run', '--pairs=x', 'a', 'b', 'c', '--by=none', '--meta', 'none'],
                     ['status', '/nonexistent/archive'], ['query', '/nonexistent/archive'],
                     ['verify', '/nonexistent/archive'], ['verify', 'a', '--restore-sample', 'z'], ['status', 'a', 'b'],
                     ['migrate'], ['migrate', '-h'], ['migrate', 'a', '--dry-run=1'], ['migrate', 'a', '--report'],
                     ['skeleton'], ['skeleton', '-h'], ['skeleton', 'frob', 'a'], ['skeleton', 'build'],
                     ['skeleton', 'build', 'a', '--limit', 'x']):
            self.same(*args)


def listing(root):
    """Every path under root with its kind and size, symlinks not followed."""
    out = []
    for d, dirs, names in os.walk(root):
        dirs.sort()
        for n in sorted(dirs + names):
            p = os.path.join(d, n); st = os.lstat(p)
            kind = 'link' if os.path.islink(p) else 'dir' if os.path.isdir(p) else 'file'
            out.append((os.path.relpath(p, root), kind, st.st_size if kind == 'file' else None))
    return out


RECORDS = ('ARCHIVE.json', 'MIGRATION.jsonl')


def legacy_archive(tmp, metadata=False):
    """A legacy archive made by preserve_apo.py: the byte section at the top, the
    files a year archive keeps beside it, and a comparison report next to it.
    With metadata, the phindex fixture's pairs and a metadata/ section from
    phindex_metadata.py; else test_native_all's DNG pairs."""
    import preserve_apo
    arc = tmp / 'legacy'
    if metadata:
        case = phx.PhindexMetadataTest('test_batches_and_index')
        case.setUp()
        left, right = case.left, case.right
        pairs = [tuple(line.split('\t')) for line in case.pairs.read_text().splitlines()]
        r = subprocess.run([sys.executable, str(HERE / 'phindex_metadata.py'), str(left), str(right), str(arc),
                            '--pairs', str(case.pairs), '--left-index', str(case.tmp / 'left.sqlite'),
                            '--right-index', str(case.tmp / 'right.sqlite')], capture_output=True, text=True)
        assert (arc / 'metadata/differences.sqlite3').exists(), r.stdout + r.stderr
    else:
        case = None
        fx = native.Fixture(tmp)
        left, right = fx.left, fx.right
        pairs = [(str(a), str(b)) for a, b in fx.pairs]
    with open(os.devnull, 'w') as null:
        old = sys.stdout; sys.stdout = null
        try:
            assert preserve_apo.scan(left, right, arc, HERE, 200, delta_mode=True, pairs=pairs)
        finally:
            sys.stdout = old
    (arc / 'provenance.json').write_text(json.dumps({'source': 'from-provenance', 'engine': 'Perl ExifTool 13.59'}))
    (arc / 'runs.jsonl').write_text('{"run": 1}\n{"run": 2}\n')
    (arc / 'README.md').write_text('readme\n')
    (arc / 'comparison.csv').write_text('a,b\n')
    (arc / 'prior-evidence').mkdir()
    (arc / 'prior-evidence/note.txt').write_text('kept at the top\n')
    report = tmp / 'report'
    report.mkdir()
    (report / 'summary.json').write_text(json.dumps({'complete': True, 'counts': {'pairs_reported': len(pairs)},
                                                     'selected_pairs': len(pairs)}))
    (report / 'pairs.jsonl').write_text('{}\n')
    (report / '.lock').write_text('')
    return arc, report, left, [pathlib.Path(a) for a, _ in pairs], case


class DngcMigrateCases(Front):
    METADATA = False

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-migrate-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.arc, self.report, self.left, self.lefts, case = legacy_archive(self.tmp, self.METADATA)
        if case:
            self.addCleanup(case.doCleanups)

    def restore_all(self, arc, tag, byte_section='bytes'):
        for a in self.lefts:
            rel = str(a.relative_to(self.left))
            out = self.tmp / tag / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            r = self.dngc('restore', arc, rel, out)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(out.read_bytes(), a.read_bytes(), rel)
            if native.OCAML:
                out2 = self.tmp / (tag + '-dng-archive') / rel
                out2.parent.mkdir(parents=True, exist_ok=True)
                r = subprocess.run([native.OCAML, 'restore', str(arc / byte_section), rel, str(out2)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(out2.read_bytes(), a.read_bytes(), rel)

    def test_dry_run_changes_nothing(self):
        before = listing(self.arc)
        r = self.dngc('migrate', self.arc, '--dry-run', '--report', self.report)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('move recovery.sqlite3 -> bytes/recovery.sqlite3', r.stdout)
        self.assertIn('move deltas -> bytes/deltas', r.stdout)
        self.assertIn(f'copy {self.report} -> report', r.stdout)
        self.assertIn('keep provenance.json', r.stdout)
        self.assertEqual(listing(self.arc), before)

    def test_migrate_verify_restore(self):
        before = json.loads(self.dngc('status', self.arc).stdout)
        self.assertEqual(before['sections']['bytes']['layout'], 'legacy (top level)')
        r = self.dngc('migrate', self.arc, '--report', self.report)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.split('\n{\n', 1)[1].split('\n}\n')[0].join(['{\n', '\n}']))['problems'], [])
        after = json.loads(self.dngc('status', self.arc).stdout)
        self.assertEqual(after['sections']['bytes']['layout'], 'bytes/')
        self.assertEqual(after['sections']['bytes']['files'], before['sections']['bytes']['files'])
        self.assertEqual(after['sections'].get('metadata'), before['sections'].get('metadata'))
        self.assertTrue(after['sections']['report']['complete'])
        top = set(os.listdir(self.arc))
        self.assertTrue({'provenance.json', 'runs.jsonl', 'README.md', 'comparison.csv', 'prior-evidence', 'bytes',
                         'report', *RECORDS} <= top, top)
        self.assertFalse({'recovery.sqlite3', 'deltas', 'manifest.jsonl', 'summary.json', '.byte-preservation.lock'} & top)
        if self.METADATA:
            self.assertIn('metadata', top)
        m = json.loads((self.arc / 'ARCHIVE.json').read_text())
        self.assertEqual(m['schema'], 'dngc-archive-1')
        entry = m['migrations'][0]
        self.assertIn({'path': 'bytes/recovery.sqlite3', 'from': 'recovery.sqlite3'}, entry['moved'])
        self.assertEqual(entry['report'], {'path': 'report', 'from': str(self.report)})
        self.assertEqual(entry['summary']['source'], str(self.left))
        self.assertEqual(entry['summary']['runs_logged'], 2)
        self.assertEqual(entry['summary']['engine'], phx.ENGINE if self.METADATA else 'Perl ExifTool 13.59')
        self.assertEqual(entry['summary']['verified_files'], len(self.lefts))
        self.restore_all(self.arc, 'restored')
        # A second migration has nothing to do.
        again = self.dngc('migrate', self.arc)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('not a legacy archive', again.stdout)

    def test_undo_restores_the_tree(self):
        before = listing(self.arc)
        r = self.dngc('migrate', self.arc, '--report', self.report, '--no-verify')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        u = self.dngc('migrate', self.arc, '--undo')
        self.assertEqual(u.returncode, 0, u.stdout + u.stderr)
        self.assertIn('move bytes/recovery.sqlite3 -> recovery.sqlite3', u.stdout)
        self.assertEqual([e for e in listing(self.arc) if e[0] not in RECORDS], before)
        m = json.loads((self.arc / 'ARCHIVE.json').read_text())
        self.assertEqual([sorted(e) for e in m['migrations']],
                         [['migrated', 'moved', 'report', 'summary'], ['report_removed', 'restored', 'undone']])
        self.assertTrue(m['migrations'][1]['report_removed'])
        log = [json.loads(l)['op'] for l in (self.arc / 'MIGRATION.jsonl').read_text().splitlines()]
        self.assertEqual(log[0], 'begin'); self.assertEqual(log[-1], 'undo-done')
        self.assertEqual(self.dngc('migrate', self.arc, '--undo').returncode, 1)
        self.restore_all(self.arc, 'restored-after-undo', byte_section='')

    def test_interrupted_migration_resumes(self):
        before = listing(self.arc)
        # As if interrupted after two logged renames.
        with open(self.arc / 'MIGRATION.jsonl', 'w') as f:
            for e in [{'op': 'begin', 'time': 't', 'archive': str(self.arc), 'report': None},
                      {'op': 'mkdir', 'path': 'bytes'}, {'op': 'move', 'from': 'deltas', 'to': 'bytes/deltas'},
                      {'op': 'move', 'from': 'manifest.jsonl', 'to': 'bytes/manifest.jsonl'}]:
                f.write(json.dumps(e) + '\n')
        (self.arc / 'bytes').mkdir()
        os.rename(self.arc / 'deltas', self.arc / 'bytes/deltas')
        r = self.dngc('migrate', self.arc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('resuming the migration', r.stdout)
        self.restore_all(self.arc, 'resumed')
        moved = json.loads((self.arc / 'ARCHIVE.json').read_text())['migrations'][0]['moved']
        self.assertIn({'path': 'bytes/deltas', 'from': 'deltas'}, moved)

    def test_refusals(self):
        before = listing(self.arc)
        with open(self.arc / '.byte-preservation.lock', 'rb') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = self.dngc('migrate', self.arc)
        self.assertEqual(r.returncode, 1)
        self.assertIn('.byte-preservation.lock is held', r.stderr)
        with open(self.report / '.lock', 'rb') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = self.dngc('migrate', self.arc, '--report', self.report)
        self.assertEqual(r.returncode, 1)
        self.assertIn('a comparison is writing that report', r.stderr)
        self.assertEqual(listing(self.arc), before)
        (self.arc / 'bytes').mkdir()
        r = self.dngc('migrate', self.arc)
        self.assertEqual(r.returncode, 1)
        self.assertIn('resolve by hand', r.stderr)
        (self.arc / 'bytes').rmdir()
        (self.arc / 'recovery.sqlite3-wal').write_bytes(b'x')
        (self.arc / '.byte-preservation.lock').unlink()
        r = self.dngc('migrate', self.arc)
        self.assertEqual(r.returncode, 1)
        self.assertIn('recovery.sqlite3-wal is not empty', r.stderr)
        self.assertEqual(self.dngc('migrate', self.arc, '--undo').returncode, 1)
        self.assertEqual(self.dngc('migrate', self.arc, '--undo', '--report', self.report).returncode, 2)


@unittest.skipUnless(native.TOOLS, 'needs exiftool and xdelta3')
class DngcMigrateTest(DngcMigrateCases, unittest.TestCase):
    pass


DngcMigrateNativeTest = native_variant(DngcMigrateTest)


@unittest.skipUnless(shutil.which('xdelta3'), 'needs xdelta3')
class DngcMigrateMetadataTest(DngcMigrateCases, unittest.TestCase):
    METADATA = True


DngcMigrateMetadataNativeTest = native_variant(DngcMigrateMetadataTest)


@unittest.skipUnless(NATIVE and shutil.which('xdelta3'), 'needs the native dngc build and xdelta3')
class DngcMigrateParityTest(unittest.TestCase):
    """The two front ends migrate the same archive, at the same path, alike."""

    def test_migrate_and_undo(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-migrate-parity-')).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        arc, report, *_ , case = legacy_archive(tmp, metadata=True)
        self.addCleanup(case.doCleanups)
        pristine = tmp / 'pristine'
        shutil.copytree(arc, pristine, symlinks=True)
        stamp = re.compile(r'"(time|migrated|undone)": "[^"]*"')
        results = {}
        for name, front in (('python', PYTHON_DNGC), ('native', NATIVE_DNGC)):
            shutil.rmtree(arc, ignore_errors=True)
            shutil.copytree(pristine, arc, symlinks=True)
            out = []
            for args in (['--dry-run', '--report', report], ['--report', report, '--no-verify'], [],
                         ['--undo', '--dry-run'], ['--undo'], ['--undo'], ['--report', report, '--undo']):
                r = run_dngc(front, 'migrate', arc, *args)
                out.append((args, r.returncode, r.stdout, r.stderr))
            out.append(stamp.sub('T', (arc / 'ARCHIVE.json').read_text()))
            out.append(stamp.sub('T', (arc / 'MIGRATION.jsonl').read_text()))
            out.append(listing(arc))
            results[name] = out
        for p, n in zip(results['python'], results['native']):
            self.assertEqual(p, n)
        for args in (['/nonexistent/archive'], [str(tmp / 'pristine'), '--dry-run=x']):
            p, n = run_dngc(PYTHON_DNGC, 'migrate', *args), run_dngc(NATIVE_DNGC, 'migrate', *args)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (n.returncode, n.stdout, n.stderr))


class DngcReadOnlyCases(Front):
    """status, verify, query, versions, restore and migrate's verify leave the
    archive's file listing as it was: no -wal or -shm from a read-only open."""

    def test_reads_leave_nothing_behind(self):
        case = phx.PhindexMetadataTest('test_batches_and_index')
        case.setUp()
        self.addCleanup(case.doCleanups)
        arc = case.tmp / 'archive'
        r = run_dngc(PYTHON_DNGC, 'run', case.left, case.right, arc, '--pairs', case.pairs, '--bytes', 'none',
                     '--metadata', 'phindex', '--index', case.tmp / 'left.sqlite', case.tmp / 'right.sqlite', '--impl', 'python')
        jsonl = case.tmp / 'pairs.jsonl'
        jsonl.write_text(''.join(json.dumps({'source': a, 'reference': b}) + '\n'
                                 for a, b in (l.split('\t') for l in case.pairs.read_text().splitlines())))
        r = subprocess.run([sys.executable, str(HERE / 'preserve_apo.py'), 'scan', str(case.left), str(case.right), str(arc),
                            '--delta', '--pairs', str(jsonl)], capture_output=True, text=True)
        self.assertTrue((arc / 'bytes/recovery.sqlite3').exists(), r.stdout + r.stderr)
        before = listing(arc)
        self.assertFalse([e for e in before if e[0].endswith(('-wal', '-shm'))], before)
        out = case.tmp / 'out'
        for verb in (['status', arc], ['verify', arc, '--restore-sample', 3], ['query', arc], ['versions', arc],
                     ['query', arc, 'files', 'EXIF:ExifIFD:LensModel'], ['restore', arc, 'a.dng', out]):
            v = self.dngc(*verb)
            self.assertIn(v.returncode, (0, 1), (verb, v.stdout, v.stderr))
            self.assertEqual(listing(arc), before, verb)
            out.unlink(missing_ok=True)

    @unittest.skipUnless(shutil.which('xdelta3'), 'needs xdelta3')
    def test_migrate_verify_leaves_nothing_behind(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-ro-')).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        arc, report, *_ = legacy_archive(tmp, metadata=True)
        r = self.dngc('migrate', arc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        leftovers = [e for e in listing(arc) if e[0].endswith(('-wal', '-shm'))]
        self.assertEqual(leftovers, [])


class DngcReadOnlyTest(DngcReadOnlyCases, unittest.TestCase):
    pass


DngcReadOnlyNativeTest = native_variant(DngcReadOnlyTest)


def metadata_only_archive(tmp):
    """A Semantic Unison semdiff-like archive: metadata/, provenance.json and
    runs.jsonl at the top, no byte section."""
    case = phx.PhindexMetadataTest('test_batches_and_index')
    case.setUp()
    arc = tmp / 'semdiff'
    r = subprocess.run([sys.executable, str(HERE / 'phindex_metadata.py'), str(case.left), str(case.right), str(arc),
                        '--pairs', str(case.pairs), '--left-index', str(case.tmp / 'left.sqlite'),
                        '--right-index', str(case.tmp / 'right.sqlite')], capture_output=True, text=True)
    assert (arc / 'metadata/differences.sqlite3').exists(), r.stdout + r.stderr
    (arc / 'provenance.json').write_text(json.dumps({'tool': 'unison-dngscan -semdiff', 'source': '/shuttle/foto',
                                                     'reference': '/photo/foto', 'engine': phx.ENGINE}))
    (arc / 'runs.jsonl').write_text('{"run": "20261108T192851Z"}\n')
    return arc, case


class DngcMigrateMetadataOnlyCases(Front):
    def test_metadata_only(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-semdiff-')).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        arc, case = metadata_only_archive(tmp)
        self.addCleanup(case.doCleanups)
        before = listing(arc)
        dry = self.dngc('migrate', arc, '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(dry.stdout.splitlines()[-1], 'write ARCHIVE.json (new)')
        self.assertNotIn('move ', dry.stdout)
        self.assertNotIn('mkdir', dry.stdout)
        self.assertEqual(listing(arc), before)
        r = self.dngc('migrate', arc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('there is no byte section to move', r.stdout)
        self.assertFalse((arc / 'bytes').exists())
        m = json.loads((arc / 'ARCHIVE.json').read_text())
        entry = m['migrations'][0]
        self.assertEqual(entry['moved'], [])
        self.assertEqual((entry['summary']['source'], entry['summary']['engine'], entry['summary']['runs_logged'],
                          entry['summary']['metadata_files']), ('/shuttle/foto', phx.ENGINE, 1, 3))
        ops = [json.loads(l)['op'] for l in (arc / 'MIGRATION.jsonl').read_text().splitlines()]
        self.assertEqual(ops, ['begin', 'manifest', 'done'])
        self.assertEqual(json.loads(self.dngc('status', arc).stdout)['runs'], 0)
        self.assertIn('not a legacy archive', self.dngc('migrate', arc).stdout)  # once only
        u = self.dngc('migrate', arc, '--undo')
        self.assertEqual(u.returncode, 0, u.stdout + u.stderr)
        self.assertIn('remove ARCHIVE.json', u.stdout)
        self.assertEqual([e for e in listing(arc) if e[0] != 'MIGRATION.jsonl'], before)


class DngcMigrateMetadataOnlyTest(DngcMigrateMetadataOnlyCases, unittest.TestCase):
    pass


DngcMigrateMetadataOnlyNativeTest = native_variant(DngcMigrateMetadataOnlyTest)


@unittest.skipUnless(NATIVE, 'needs the native dngc build (DNGC_NATIVE)')
class DngcMigrateMetadataOnlyParityTest(unittest.TestCase):
    def test_same_output(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-semdiff-parity-')).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        arc, case = metadata_only_archive(tmp)
        self.addCleanup(case.doCleanups)
        pristine = tmp / 'pristine'
        shutil.copytree(arc, pristine)
        stamp = re.compile(r'"(time|migrated|undone)": "[^"]*"')
        results = {}
        for name, front in (('python', PYTHON_DNGC), ('native', NATIVE_DNGC)):
            shutil.rmtree(arc)
            shutil.copytree(pristine, arc)
            out = []
            for args in (['--dry-run'], ['--no-verify'], [], ['--undo', '--dry-run'], ['--undo']):
                r = run_dngc(front, 'migrate', arc, *args)
                out.append((args, r.returncode, r.stdout, r.stderr))
            out.append(stamp.sub('T', (arc / 'MIGRATION.jsonl').read_text()))
            out.append(listing(arc))
            results[name] = out
        for a, b in zip(results['python'], results['native']):
            self.assertEqual(a, b)


if __name__ == '__main__':
    unittest.main()

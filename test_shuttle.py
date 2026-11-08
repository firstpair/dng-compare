"""A shuttle source that is emptied and refilled (docs/dngc.md, "Wiped and
refilled sources"): every writer in Python, OCaml and Rust, and both dngc front
ends, keep an archive's earlier records when the source is gone, report missing
listed sources, never turn earlier records into errors, and keep a refilled
path's earlier content as a version that restores byte for byte."""
import json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest

import test_native_all as native
import test_phindex_metadata as phx
from archive_layout import byte_section, version_label

HERE = pathlib.Path(__file__).parent
PY = sys.executable
DNGC_NATIVE = native.first_executable([os.environ.get('DNGC_NATIVE'),
                                       HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/dngc.exe',
                                       *sorted(HERE.glob('ocaml-archive/_build*/default/bin/dngc.exe'))])
FRONTS = {'dngc.py': [PY, str(HERE / 'dngc.py')], **({'dngc': [DNGC_NATIVE]} if DNGC_NATIVE else {})}


def run(*command):
    return subprocess.run([*map(str, command)], capture_output=True, text=True)


def tables(archive):
    """files, errors and versions of an archive's byte section."""
    with sqlite3.connect(f'file:{byte_section(archive) / "recovery.sqlite3"}?mode=ro', uri=True) as db:
        files = {p: json.loads(r) for p, r in db.execute('SELECT path, record FROM files')}
        errors = dict(db.execute('SELECT path, error FROM errors'))
        has = db.execute("SELECT 1 FROM sqlite_master WHERE name='versions'").fetchone()
        versions = {(p, v): json.loads(r) for p, v, r in db.execute('SELECT path, version, record FROM versions')} if has else {}
    return files, errors, versions


def wipe(root):
    for p in sorted(root.rglob('*'), reverse=True):
        p.rmdir() if p.is_dir() and not p.is_symlink() else p.unlink()


@unittest.skipUnless(native.TOOLS and native.OCAML, 'needs exiftool, xdelta3 and dng-archive')
class WritersTest(unittest.TestCase):
    """Each writer: build, wipe, rerun, refill, rerun."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dng-shuttle-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)

    def writers(self):
        w = {
            'preserve_apo.py': lambda fx, a, rep, pairs, trust: [PY, HERE / 'preserve_apo.py', 'scan', fx.left, fx.right, a,
                                                                 '--delta', '--pairs', self.jsonl(pairs)],
            'dng-archive preserve': lambda fx, a, rep, pairs, trust: [native.OCAML, 'preserve', fx.left, fx.right, a, '--delta',
                                                                      '--pairs', self.jsonl(pairs)],
            'compare_dng.py': lambda fx, a, rep, pairs, trust: [PY, HERE / 'compare_dng.py', fx.left, fx.right, '--pairs-only',
                                                                '--pairs', self.tsv(pairs), '--output', rep, '--archive', a],
            'dng-archive all': lambda fx, a, rep, pairs, trust: [native.OCAML, 'all', fx.left, fx.right, '--pairs', self.tsv(pairs),
                                                                 '--report', rep, '--archive', a, *(['--trust-pairs'] if trust else [])],
        }
        if native.RUST:
            w['dng-compare-all'] = lambda fx, a, rep, pairs, trust: [native.RUST, fx.left, fx.right, '--pairs', self.tsv(pairs),
                                                                     '--report', rep, '--archive', a,
                                                                     *(['--trust-pairs'] if trust else [])]
        return w

    def jsonl(self, pairs):
        p = self.tmp / f'pairs-{len(list(self.tmp.iterdir()))}.jsonl'
        p.write_text(''.join(json.dumps({'source': str(a), 'reference': str(b)}) + '\n' for a, b in pairs))
        return p

    def tsv(self, pairs):
        p = self.tmp / f'pairs-{len(list(self.tmp.iterdir()))}.tsv'
        p.write_text(''.join(f'{a}\t{b}\n' for a, b in pairs))
        return p

    def restore(self, archive, rel, version=None):
        out = self.tmp / f'restored-{len(list(self.tmp.iterdir()))}'
        r = run(native.OCAML, 'restore', archive, rel, out, *(['--version', version] if version else []))
        self.assertEqual(r.returncode, 0, r.stderr)
        p = run(PY, HERE / 'preserve_apo.py', 'restore', archive, rel, str(out) + '.py', *(['--version', version] if version else []))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(out.read_bytes(), pathlib.Path(str(out) + '.py').read_bytes())
        return out.read_bytes()

    def test_writers(self):
        for name, command in self.writers().items():
            with self.subTest(writer=name):
                root = self.tmp / name.replace(' ', '-')
                root.mkdir()
                fx = native.Fixture(root)
                archive, report = root / 'archive', root / 'report'
                originals = {str(a.relative_to(fx.left)): a.read_bytes() for a, _ in fx.pairs}
                first = run(*command(fx, archive, report, fx.pairs, False))
                self.assertIn(first.returncode, (0, 1), first.stdout + first.stderr)
                files, errors, versions = tables(archive)
                self.assertEqual(sorted(files), sorted(originals))
                self.assertEqual((errors, versions), ({}, {}))
                # Emptied: the records stay, and restore needs only the reference and the archive.
                wipe(fx.left)
                for rel, data in originals.items():
                    self.assertEqual(self.restore(archive, rel), data, rel)
                again = run(*command(fx, archive, report, fx.pairs, True))
                if name == 'compare_dng.py':  # checks its list first, without --trust-pairs
                    self.assertEqual(again.returncode, 2, again.stderr)
                    self.assertIn('missing under', again.stderr)
                else:
                    self.assertEqual(again.returncode, 1, again.stdout + again.stderr)
                    self.assertNotIn('Traceback', again.stderr)
                    for rel in originals:
                        self.assertIn(f'MISSING {rel}\n', again.stdout)
                    summary = json.loads((byte_section(archive) / 'summary.json').read_text())
                    self.assertEqual(summary['missing_sources'], len(originals))
                self.assertEqual(tables(archive), (files, {}, {}), 'earlier records untouched')
                # Refilled: other contents at two old paths, and a new path.
                native.Fixture.write(fx.left / 'meta.dng', native.np.arange(4096, dtype=native.np.uint16).reshape(64, 64),
                                     description='refilled meta')
                native.Fixture.write(fx.left / 'same.dng', native.np.arange(4096, dtype=native.np.uint16).reshape(64, 64),
                                     description='refilled same')
                native.Fixture.write(fx.left / 'new.dng', native.np.arange(4096, dtype=native.np.uint16).reshape(64, 64))
                native.Fixture.write(fx.right / 'new.dng', native.np.arange(4096, dtype=native.np.uint16).reshape(64, 64),
                                     description='other')
                pairs = [(fx.left / n, fx.right / n) for n in ('meta.dng', 'same.dng', 'new.dng')]
                refilled = run(*command(fx, archive, report, pairs, False))
                self.assertIn(refilled.returncode, (0, 1), refilled.stdout + refilled.stderr)
                files2, errors2, versions2 = tables(archive)
                self.assertEqual(errors2, {})
                self.assertEqual(sorted(files2), sorted([*originals, 'new.dng']))
                self.assertEqual(sorted(versions2), sorted((rel, version_label(files[rel])) for rel in ('meta.dng', 'same.dng')))
                for (rel, label), record in versions2.items():
                    self.assertEqual(record, files[rel])  # the earlier record, as it was
                    self.assertEqual(self.restore(archive, rel, label), originals[rel], rel)
                for rel in ('meta.dng', 'same.dng'):
                    new = files2[rel]
                    self.assertEqual(new['source_sha256'], __import__('hashlib').sha256((fx.left / rel).read_bytes()).hexdigest())
                    self.assertTrue(new['delta'].startswith(f'versions/{version_label(new)}/deltas/'), new)
                    self.assertEqual(self.restore(archive, rel), (fx.left / rel).read_bytes(), rel)
                self.assertEqual(files2['new.dng'].get('delta'), 'deltas/new.dng.vcdiff')
                for rel in set(originals) - {'meta.dng', 'same.dng'}:  # still gone; their records stay
                    self.assertEqual(files2[rel], files[rel])
                    self.assertEqual(self.restore(archive, rel), originals[rel], rel)
                for front, cmd in FRONTS.items():
                    s = json.loads(run(*cmd, 'status', archive).stdout)['sections']['bytes']
                    self.assertEqual((s['files'], s['errors'], s['earlier_versions']), (len(originals) + 1, 0, 2), front)
                    v = run(*cmd, 'verify', archive, '--restore-sample', 3)
                    self.assertEqual(v.returncode, 0, (front, v.stdout, v.stderr))


@unittest.skipUnless(native.TOOLS, 'needs exiftool and xdelta3')
class DngcShuttleTest(unittest.TestCase):
    """dngc run over the whole tree, both front ends and implementations."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='dngc-shuttle-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)

    def impls(self, front):
        out = ['python']
        if front != 'dngc.py' or native.OCAML:
            out.append('ocaml')
        return out

    def test_whole_tree(self):
        outputs = {}
        for front, cmd in FRONTS.items():
            for impl in self.impls(front):
                with self.subTest(front=front, impl=impl):
                    root = self.tmp / f'{front}-{impl}'
                    root.mkdir()
                    fx = native.Fixture(root)
                    (fx.left / 'notes.txt').write_text('first notes')
                    archive = root / 'archive'
                    originals = {str(p.relative_to(fx.left)): p.read_bytes() for p in fx.left.rglob('*') if p.is_file()}
                    r = run(*cmd, 'run', fx.left, fx.right, archive, '--impl', impl, '--workers', 2)
                    self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
                    files, _, _ = tables(archive)
                    # Emptied, then gone entirely: the read verbs work and see nothing wrong.
                    wipe(fx.left)
                    for state in ('empty', 'missing'):
                        if state == 'missing':
                            fx.left.rmdir()
                        for verb in (['status', archive], ['verify', archive, '--restore-sample', 2]):
                            v = run(*cmd, *verb)
                            self.assertEqual(v.returncode, 0, (state, verb, v.stdout, v.stderr))
                        for rel, data in originals.items():
                            out = root / f'out-{state}' / rel
                            out.parent.mkdir(parents=True, exist_ok=True)
                            self.assertEqual(run(*cmd, 'restore', archive, rel, out).returncode, 0, rel)
                            self.assertEqual(out.read_bytes(), data, rel)
                    gone = run(*cmd, 'run', fx.left, fx.right, archive, '--impl', impl)
                    self.assertEqual(gone.returncode, 2, gone.stderr)
                    self.assertIn('no such directory', gone.stderr)
                    fx.left.mkdir()
                    empty = run(*cmd, 'run', fx.left, fx.right, archive, '--impl', impl)
                    self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
                    self.assertIn('0 files under LEFT; 0 with a counterpart', empty.stdout)
                    self.assertEqual(tables(archive), (files, {}, {}))
                    # Refilled with other contents at old paths.
                    (fx.left / 'notes.txt').write_text('second notes, longer')
                    native.Fixture.write(fx.left / 'meta.dng', native.np.arange(4096, dtype=native.np.uint16).reshape(64, 64),
                                         description='refilled')
                    r = run(*cmd, 'run', fx.left, fx.right, archive, '--impl', impl, '--workers', 2)
                    self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
                    files2, errors2, versions2 = tables(archive)
                    self.assertEqual(errors2, {})
                    self.assertEqual(sorted(p for p, _ in versions2), ['meta.dng', 'notes.txt'])
                    listing = run(*cmd, 'versions', archive)
                    self.assertEqual(listing.returncode, 0, listing.stderr)
                    lines = listing.stdout.splitlines()
                    self.assertEqual(lines[0], 'path\tversion\tstate')
                    for (rel, label) in versions2:
                        self.assertIn(f'{rel}\t{label}\tearlier', lines)
                        out = root / 'out-old' / rel
                        out.parent.mkdir(parents=True, exist_ok=True)
                        r = run(*cmd, 'restore', archive, rel, out, '--version', label)
                        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                        self.assertEqual(out.read_bytes(), originals[rel], rel)
                    self.assertEqual(json.loads(run(*cmd, 'status', archive).stdout)['sections']['bytes']['earlier_versions'], 2)
                    self.assertEqual(run(*cmd, 'verify', archive).returncode, 0)
                    # The same archive, read by the other front end, gives the same output.
                    for other, ocmd in FRONTS.items():
                        for verb in (['versions', archive], ['versions', archive, 'notes.txt'], ['status', archive],
                                     ['verify', archive]):
                            a, b = run(*cmd, *verb), run(*ocmd, *verb)
                            self.assertEqual((a.returncode, a.stdout), (b.returncode, b.stdout), (front, other, verb))
                    r = run(*cmd, 'restore', archive, 'notes.txt', root / 'nope', '--version', 'nonexistent')
                    self.assertEqual(r.returncode, 1)
                    # migrate, on an archive whose source is gone.
                    wipe(fx.left)
                    fx.left.rmdir()
                    m = run(*cmd, 'migrate', archive, '--dry-run')
                    self.assertEqual(m.returncode, 0, m.stderr)
                    outputs[front, impl] = sorted(versions2)

    def test_metadata_verbs_on_a_wiped_source(self):
        case = phx.PhindexMetadataTest('test_batches_and_index')
        case.setUp()
        self.addCleanup(case.doCleanups)
        arc = case.tmp / 'archive'
        r = run(PY, HERE / 'dngc.py', 'run', case.left, case.right, arc, '--pairs', case.pairs, '--bytes', 'none',
                '--metadata', 'phindex', '--index', case.tmp / 'left.sqlite', case.tmp / 'right.sqlite', '--impl', 'python')
        self.assertIn('metadata', json.loads(run(PY, HERE / 'dngc.py', 'status', arc).stdout)['sections'], r.stderr)
        wipe(case.left)
        case.left.rmdir()
        for front, cmd in FRONTS.items():
            for verb in (['index', arc], ['query', arc], ['query', arc, 'files', 'EXIF:ExifIFD:LensModel'], ['status', arc],
                         ['verify', arc]):
                v = run(*cmd, *verb)
                self.assertEqual(v.returncode, 0, (front, verb, v.stdout, v.stderr))
            self.assertIn('a.dng', run(*cmd, 'query', arc, 'files', 'EXIF:ExifIFD:LensModel').stdout)


@unittest.skipUnless(shutil.which('xdelta3'), 'needs xdelta3')
class MigrateWipedTest(unittest.TestCase):
    def test_migrate_after_wipe(self):
        import preserve_apo, test_dngc
        for front, cmd in FRONTS.items():
            with tempfile.TemporaryDirectory() as t:
                t = pathlib.Path(t).resolve()
                arc, report, left, lefts, case = test_dngc.legacy_archive(t)
                data = {a: a.read_bytes() for a in lefts}
                wipe(left)
                left.rmdir()
                m = run(*cmd, 'migrate', arc, '--report', report)
                self.assertEqual(m.returncode, 0, (front, m.stdout, m.stderr))
                for a, b in data.items():
                    out = t / 'out' / a.name
                    out.parent.mkdir(exist_ok=True)
                    self.assertEqual(run(*cmd, 'restore', arc, a.relative_to(left), out).returncode, 0)
                    self.assertEqual(out.read_bytes(), b)
                    out.unlink()
                self.assertEqual(run(*cmd, 'migrate', arc, '--undo').returncode, 0)


    def test_migrate_moves_versions(self):
        """A legacy archive re-preserved before migrating has versions/ at its top;
        migrate moves it into bytes/ and both contents still restore."""
        import contextlib, io, preserve_apo
        for front, cmd in FRONTS.items():
            with tempfile.TemporaryDirectory() as t:
                t = pathlib.Path(t).resolve()
                src, ref, arc = t / 'src', t / 'ref', t / 'arc'
                src.mkdir(); ref.mkdir()
                body = bytes(range(256)) * 512
                (ref / 'a.dng').write_bytes(body)
                (src / 'a.dng').write_bytes(body + b'first')
                pairs = [(str(src / 'a.dng'), str(ref / 'a.dng'))]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(preserve_apo.scan(src, ref, arc, HERE, 10000, delta_mode=True, pairs=pairs))
                    first = (src / 'a.dng').read_bytes()
                    (src / 'a.dng').write_bytes(body + b'second, refilled')
                    os.utime(src / 'a.dng', ns=(10**18, 10**18))
                    self.assertTrue(preserve_apo.scan(src, ref, arc, HERE, 10000, delta_mode=True, pairs=pairs))
                self.assertTrue((arc / 'versions').is_dir() and (arc / 'recovery.sqlite3').exists())
                (_, version), = tables(arc)[2]
                m = run(*cmd, 'migrate', arc)
                self.assertEqual(m.returncode, 0, (front, m.stdout, m.stderr))
                self.assertFalse((arc / 'versions').exists(), front)
                self.assertTrue((arc / 'bytes/versions').is_dir(), front)
                for extra, want in (((), (src / 'a.dng').read_bytes()), (('--version', version), first)):
                    out = t / 'out.dng'
                    r = run(*cmd, 'restore', arc, 'a.dng', out, *extra)
                    self.assertEqual(r.returncode, 0, (front, extra, r.stderr))
                    self.assertEqual(out.read_bytes(), want, (front, extra))
                    out.unlink()


class UsageContractTest(unittest.TestCase):
    """Semantic Unison detects version support from restore's usage line (U-39)."""

    @unittest.skipUnless(native.OCAML, 'needs dng-archive')
    def test_restore_usage_names_version(self):
        self.assertIn('restore ARCHIVE RELATIVE_PATH OUTPUT [--reference-root DIR] [--version LABEL]',
                      run(native.OCAML).stderr)


if __name__ == '__main__':
    unittest.main()

"""Finding a reference's content through phindex indexes (docs/dngc-archive-spec.md,
5.5) after its file was moved, renamed or edited, in the Python reference
(dngc.py, preserve_apo.py, content_index.py) and, when it has been built, the
native dngc (the OCaml library): small synthetic trees, an archive with
identical and delta records, and a hand-made index with phindex's `files`
table. The cases are those of the Rust library's index_tests.rs."""
import hashlib, json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile, unittest

import content_index
import skeleton
import test_native_all as native
from test_skeleton import fixtures

HERE = pathlib.Path(__file__).resolve().parent
NATIVE = native.first_executable([os.environ.get('DNGC_NATIVE'),
                                  HERE / 'ocaml-archive/_build-5.4.0+ox/default/bin/dngc.exe',
                                  *sorted(HERE.glob('ocaml-archive/_build*/default/bin/dngc.exe'))])
XDELTA = shutil.which('xdelta3')
FRONTS = [('python', [sys.executable, str(HERE / 'dngc.py')])] + ([('native', [NATIVE])] if NATIVE else [])
REBUILDS = 'and its skeleton does not rebuild it'

FILES = """
CREATE TABLE files (
  path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ino INTEGER NOT NULL,
  dev INTEGER NOT NULL, sha256 TEXT, hashed_unix INTEGER, seen_unix INTEGER NOT NULL, gone INTEGER NOT NULL DEFAULT 0,
  dng_blocks TEXT, dng_blocks_err TEXT, lr_digest TEXT, lr_err TEXT, md_digest TEXT, md_err TEXT, ids_rev TEXT,
  dngsem_profile TEXT, dngsem_err TEXT, preserved TEXT, preserved_err TEXT, image_data TEXT, image_err TEXT);
CREATE INDEX files_sha ON files(sha256);
CREATE INDEX files_size ON files(size);
CREATE INDEX files_dng_blocks ON files(dng_blocks);
CREATE INDEX files_lr ON files(lr_digest);
CREATE INDEX files_md ON files(md_digest);
CREATE INDEX files_preserved ON files(preserved);
CREATE INDEX files_image ON files(image_data);"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def xmp(text, pad=0):
    return f'<x:xmpmeta>{text}{" " * pad}</x:xmpmeta>'.encode()


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


class Made:
    """A hand-made index. row lists a path with a SHA-256 and identities."""
    def __init__(self, directory, name, schema=FILES):
        self.path = directory / name
        self.db = sqlite3.connect(self.path)
        self.db.executescript(schema)

    def row(self, path, size, sha256, gone=False, dng_blocks=None, image_data=None):
        self.db.execute('INSERT OR REPLACE INTO files(path, size, mtime_ns, ino, dev, sha256, seen_unix, gone, dng_blocks, image_data) '
                        'VALUES(?, ?, 0, 0, 0, ?, 0, ?, ?, ?)', (path, size, sha256, int(gone), dng_blocks, image_data))

    def live(self, path, dng_blocks=None, image_data=None):
        """Lists the file at path as it is now."""
        data = pathlib.Path(path).read_bytes()
        self.row(path, len(data), sha(data), False, dng_blocks, image_data)

    def done(self):
        self.db.commit()
        self.db.close()
        return self.path


def identity(path):
    st = os.stat(path)
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


class Archive:
    """An archive of identical records and deltas, with a skeleton section
    once skeleton() has been called."""
    ATTRS = dict(mode=0o644, uid=os.getuid(), gid=os.getgid(), mtime_ns=10 ** 18, atime_ns=10 ** 18, flags=0, xattrs={})

    def __init__(self, directory):
        self.top = directory / 'archive'
        (self.top / 'bytes').mkdir(parents=True)
        self.out = directory / 'out'
        self.out.mkdir()
        self.n = 0
        db = sqlite3.connect(self.top / 'bytes/recovery.sqlite3')
        db.execute('CREATE TABLE files (path TEXT PRIMARY KEY, record TEXT NOT NULL)')
        db.commit(); db.close()

    def add(self, rel, **record):
        db = sqlite3.connect(self.top / 'bytes/recovery.sqlite3')
        db.execute('INSERT INTO files VALUES (?, ?)', (rel, json.dumps(dict(record, relative_path=rel, source_attrs=self.ATTRS))))
        db.commit(); db.close()

    def identical(self, rel, reference):
        """The source was byte-identical to the reference file, as it is now."""
        self.add(rel, reference=reference, reference_identity=identity(reference),
                 source_sha256=sha(pathlib.Path(reference).read_bytes()))

    def delta(self, rel, reference, source):
        """source as a delta against the reference file, as it is now."""
        origin, payload = self.top / 'source.tmp', rel + '.vcdiff'
        origin.write_bytes(source)
        subprocess.run([XDELTA, '-e', '-f', '-s', reference, origin, self.top / 'bytes' / payload], check=True)
        origin.unlink()
        self.add(rel, reference=reference, reference_identity=identity(reference),
                 reference_sha256=sha(pathlib.Path(reference).read_bytes()), source_sha256=sha(source),
                 delta=payload, delta_sha256=sha((self.top / 'bytes' / payload).read_bytes()))

    def skeleton(self, path):
        """A skeleton of the file at path, as it is now."""
        (self.top / 'skeletons').mkdir(exist_ok=True)
        db = sqlite3.connect(self.top / 'skeletons/skeletons.sqlite3')
        db.execute('CREATE TABLE IF NOT EXISTS skeletons(sha256 TEXT PRIMARY KEY, record TEXT NOT NULL, skeleton BLOB NOT NULL)')
        digest = sha(pathlib.Path(path).read_bytes())
        db.execute('INSERT OR IGNORE INTO skeletons VALUES (?,?,?)',
                   (digest, *skeleton.encode(digest, path, identity(path), skeleton.build(pathlib.Path(path)))))
        db.commit(); db.close()

    def restore(self, front, rel, indexes=(), candidates=None):
        """(content, None) when restore succeeds, else (None, what it said)."""
        self.n += 1
        out = self.out / str(self.n)
        command = [*front, 'restore', str(self.top), rel, str(out)]
        for i in indexes:
            command += ['--index', str(i)]
        if candidates is not None:
            command += ['--candidates', str(candidates)]
        r = subprocess.run(command, capture_output=True, text=True)
        if r.returncode == 0:
            return out.read_bytes(), None
        if out.exists():
            raise AssertionError(f'{out} left by a failed restore')
        return None, r.stdout + r.stderr


def alone(test, index):
    """No file beside an index: an immutable read leaves no -wal, -shm or journal."""
    beside = [p.name for p in index.parent.iterdir() if p.name != index.name and p.name.startswith(index.name)]
    test.assertEqual(beside, [], 'files left beside the index')


class Lookup(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix='dngc-content-index-')).resolve()
        self.addCleanup(shutil.rmtree, self.d)
        self.old = fixtures.tiff(xmp('rating 0'))
        self.source = self.old[:1000] + b'the source differs here' + self.old[1000:]

    def each(self):
        """Each front end, on its own copy of the directory setUp gave."""
        for name, front in FRONTS:
            with self.subTest(front=name):
                yield front

    def ok(self, archive, front, rel, want, indexes=(), candidates=None):
        got, said = archive.restore(front, rel, indexes, candidates)
        self.assertIsNone(said, said)
        self.assertEqual(got, want)

    def fails(self, archive, front, rel, indexes=(), candidates=None):
        got, said = archive.restore(front, rel, indexes, candidates)
        self.assertIsNone(got, f'{rel} restored')
        return said

    def test_a_moved_reference_is_found_by_its_sha256(self):
        d, old = self.d, self.old
        was = put(d / 'tree/a/one.tif', old)
        archive = Archive(d)
        archive.identical('one.tif', was)
        if XDELTA:
            archive.delta('two.tif', was, self.source)
        # Moved and renamed, unchanged; the index saw both states.
        now = put(d / 'tree/b/renamed.tif', old)
        os.unlink(was)
        made = Made(d, 'index.sqlite')
        made.row(was, len(old), sha(old), True, None, 'image-1')
        made.live(now, None, 'image-1')
        index = made.done()
        for front in self.each():
            self.fails(archive, front, 'one.tif')
            self.ok(archive, front, 'one.tif', old, [index])
            if XDELTA:
                self.fails(archive, front, 'two.tif')
                self.ok(archive, front, 'two.tif', self.source, [index])
            alone(self, index)

    def test_a_moved_and_edited_reference_is_rebuilt_from_its_skeleton(self):
        d, old = self.d, self.old
        edited = fixtures.tiff(xmp('rating 5, exposure +0.35', 3001))
        was = put(d / 'tree/a/one.tif', old)
        archive = Archive(d)
        archive.identical('one.tif', was)
        if XDELTA:
            archive.delta('two.tif', was, self.source)
        archive.skeleton(was)
        # Moved, renamed and edited. Beside it, another picture, and a file with
        # the same image identity whose strip differs (a wrong candidate).
        os.unlink(was)
        now = put(d / 'tree/b/renamed.tif', edited)
        other = bytearray(edited)
        other[len(other) // 2] ^= 1
        wrong = put(d / 'tree/b/damaged.tif', bytes(other))
        unrelated = put(d / 'tree/b/unrelated.jpg', fixtures.jpeg(fixtures.JPEG))
        made = Made(d, 'index.sqlite')
        made.row(was, len(old), sha(old), True, None, 'image-1')
        made.live(wrong, None, 'image-1')
        made.live(now, None, 'image-1')
        made.live(unrelated, None, 'image-2')
        index = made.done()
        for front in self.each():
            self.assertIn(REBUILDS, self.fails(archive, front, 'one.tif'))
            self.ok(archive, front, 'one.tif', old, [index])
            if XDELTA:
                self.ok(archive, front, 'two.tif', self.source, [index])
            alone(self, index)

    def test_an_identity_comes_from_the_old_paths_row_when_no_row_has_the_sha256(self):
        # Edited in place, indexed (the row's SHA-256 replaced), then moved: the
        # row at the recorded path is gone, lists the edited bytes, and still
        # carries the image identity.
        d, old = self.d, self.old
        edited = fixtures.tiff(xmp('rating 3', 2000))
        was = put(d / 'tree/a/one.dng', old)
        archive = Archive(d)
        archive.identical('one.dng', was)
        archive.skeleton(was)
        os.unlink(was)
        now = put(d / 'tree/c/one-edited.dng', edited)
        made = Made(d, 'index.sqlite')
        made.row(was, len(edited), sha(edited), True, 'raw-1', None)
        made.live(now, 'raw-1', None)
        index = made.done()
        for front in self.each():
            self.ok(archive, front, 'one.dng', old, [index])

    def test_a_stale_row_is_skipped(self):
        d, old = self.d, self.old
        was = put(d / 'tree/a/one.tif', old)
        archive = Archive(d)
        archive.identical('one.tif', was)
        os.unlink(was)
        # Three rows list the content: a file since overwritten at the same size,
        # one since truncated, one since deleted. A fourth still holds it.
        changed = bytearray(old)
        changed[len(old) // 2] ^= 1
        same_size = put(d / 'tree/b/1-same-size.tif', bytes(changed))
        truncated = put(d / 'tree/b/2-truncated.tif', old[:-1])
        deleted = str(d / 'tree/b/3-deleted.tif')
        good = put(d / 'tree/b/4-good.tif', old)
        stale = Made(d, 'stale.sqlite')
        for p in (same_size, truncated, deleted):
            stale.row(p, len(old), sha(old), False, None, 'image-1')
        stale = stale.done()
        fresh = Made(d, 'fresh.sqlite')
        fresh.live(good, None, 'image-1')
        fresh = fresh.done()
        for front in self.each():
            # Only stale rows: restore fails as it does without an index.
            without = self.fails(archive, front, 'one.tif')
            self.assertIn('No such file', without)
            self.assertIn('No such file', self.fails(archive, front, 'one.tif', [stale]))
            # A second index lists a file that holds it.
            self.ok(archive, front, 'one.tif', old, [stale, fresh])
        # With a skeleton section, the stale files are tried as edited versions
        # (same identity) and none passes the SHA-256 check.
        put(pathlib.Path(was), old)
        archive.skeleton(was)
        os.unlink(was)
        for front in self.each():
            self.assertIn(REBUILDS, self.fails(archive, front, 'one.tif', [stale]))

    def test_without_an_index_restore_is_as_before(self):
        d, old = self.d, self.old
        edited = fixtures.tiff(xmp('rating 5', 3001))
        was = put(d / 'tree/a/one.tif', old)
        archive = Archive(d)
        archive.identical('one.tif', was)
        if XDELTA:
            archive.delta('two.tif', was, self.source)
        empty = Made(d, 'empty.sqlite').done()
        for front in self.each():
            # In place, unchanged.
            put(pathlib.Path(was), old)
            self.ok(archive, front, 'one.tif', old)
            # No skeleton section: an edited reference fails with the texts of
            # 5.5, the same with an index that knows nothing of it.
            put(pathlib.Path(was), edited)
            for indexes in ([], [empty]):
                self.assertIn('Original file recovery hash mismatch', self.fails(archive, front, 'one.tif', indexes))
                if XDELTA:
                    self.assertIn('reference SHA-256 mismatch', self.fails(archive, front, 'two.tif', indexes))
            # A missing reference: the same error with and without the index.
            os.unlink(was)
            for indexes in ([], [empty]):
                self.assertIn('No such file', self.fails(archive, front, 'one.tif', indexes))
        # With a skeleton section, the edited file in place rebuilds, as before.
        put(pathlib.Path(was), old)
        archive.skeleton(was)
        put(pathlib.Path(was), edited)
        for front in self.each():
            self.ok(archive, front, 'one.tif', old)
            self.ok(archive, front, 'one.tif', old, [empty])

    def test_at_most_candidates_files_are_tried(self):
        d, old = self.d, self.old
        cap = content_index.CANDIDATES
        edited = fixtures.tiff(xmp('rating 1', 100))
        was = put(d / 'tree/a/one.tif', old)
        archive = Archive(d)
        archive.identical('one.tif', was)
        archive.skeleton(was)
        os.unlink(was)
        made = Made(d, 'index.sqlite')
        made.row(was, len(old), sha(old), True, None, 'image-1')
        # More wrong candidates than the cap, each with its own SHA-256, listed
        # before the right one.
        for i in range(cap):
            wrong = bytearray(edited)
            wrong[len(wrong) // 2] = i
            made.live(put(d / f'tree/b/wrong-{i}.tif', bytes(wrong)), None, 'image-1')
        made.live(put(d / 'tree/b/right.tif', edited), None, 'image-1')
        index = made.done()
        for front in self.each():
            self.assertIn(REBUILDS, self.fails(archive, front, 'one.tif', [index]))
            # The limit is settable: one more candidate reaches the right file,
            # and none uses no index.
            self.ok(archive, front, 'one.tif', old, [index], cap + 1)
            self.fails(archive, front, 'one.tif', [index], 0)
        # Byte-identical copies count once: with the wrong ones all alike, the
        # right one is reached.
        for i in range(1, cap):
            shutil.copyfile(d / 'tree/b/wrong-0.tif', d / f'tree/b/wrong-{i}.tif')
        made = Made(d, 'index-2.sqlite')
        made.row(was, len(old), sha(old), True, None, 'image-1')
        for i in range(cap):
            made.live(str(d / f'tree/b/wrong-{i}.tif'), None, 'image-1')
        made.live(str(d / 'tree/b/right.tif'), None, 'image-1')
        index = made.done()
        for front in self.each():
            self.ok(archive, front, 'one.tif', old, [index])

    def test_every_lookup_uses_an_index_and_an_unindexed_column_is_not_looked_up(self):
        index = content_index.Index(Made(self.d, 'index.sqlite').done())
        self.assertEqual(index.unindexed, [])
        plans = index.plans()
        self.assertEqual(len(plans), 5)
        for query, plan in plans:
            self.assertIn('USING', plan, query)
            self.assertNotIn('SCAN', plan, query)
        index.close()
        # An index from before image_data was indexed: that column is not asked.
        bare = Made(self.d, 'bare.sqlite', FILES.replace('CREATE INDEX files_image ON files(image_data);', ''))
        bare.row('/nowhere/a', 1, 'aa', False, 'raw-1', 'image-1')
        index = content_index.Index(bare.done())
        self.assertEqual(index.unindexed, ['image_data'])
        self.assertEqual(index.identity_values('aa', [], 8), [('dng_blocks', 'raw-1')])
        self.assertEqual(index.sharing('image_data', 'image-1', 8), [])
        index.close()
        # Not an index at all.
        other = self.d / 'other.sqlite'
        db = sqlite3.connect(other); db.execute('CREATE TABLE files(path TEXT)'); db.commit(); db.close()
        with self.assertRaises(ValueError):
            content_index.Index(other)
        with self.assertRaises(ValueError):
            content_index.Index(self.d / 'missing.sqlite')
        for front in self.each():
            r = subprocess.run([*front, 'restore', str(self.d / 'none'), 'x', str(self.d / 'y'), '--index', str(other)],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)


@unittest.skipUnless(XDELTA and (HERE / 'conformance/corpus/current').is_dir(), 'needs xdelta3 and the corpus')
class Moved(unittest.TestCase):
    """restore, verify --restore-sample and keep after the whole reference
    tree has moved: below --reference-root, or through --index, in both front
    ends, on the corpus case `current`."""
    def moved(self):
        """The case with its reference tree renamed, its records and a
        hand-made index of the tree where it is now."""
        from test_conformance import materialize
        c = materialize('current')
        self.addCleanup(shutil.rmtree, c, True)
        os.rename(c / 'R', c / 'R2')
        made = Made(c, 'moved.sqlite')
        for p in sorted((c / 'R2').rglob('*')):
            if p.is_file() and not p.is_symlink():
                made.live(str(p), None, None)
        db = sqlite3.connect(f'file:{c}/archive/bytes/recovery.sqlite3?immutable=1', uri=True)
        records = {p: json.loads(r) for p, r in db.execute('SELECT path, record FROM files')}
        db.close()
        return c, made.done(), records

    def run_(self, front, *args):
        return subprocess.run([*front, *map(str, args)], capture_output=True, text=True)

    def test_each_command_finds_the_moved_tree(self):
        for name, front in FRONTS:
            for how in ('root', 'index'):
                with self.subTest(front=name, how=how):
                    c, index, records = self.moved()
                    arc = c / 'archive'
                    where = ['--reference-root', c / 'R2'] if how == 'root' else ['--index', index]
                    needing = sorted(p for p, r in records.items() if not r.get('original'))
                    self.assertTrue(any(records[p].get('delta') for p in needing), 'the case has a delta')
                    self.assertTrue(any(not records[p].get('delta') for p in needing), 'the case has an identical record')
                    # restore
                    for i, p in enumerate(needing):
                        r = self.run_(front, 'restore', arc, p, c / f'plain-{i}')
                        self.assertNotEqual(r.returncode, 0, p)
                        r = self.run_(front, 'restore', arc, p, c / f'found-{i}', *where)
                        self.assertEqual(r.returncode, 0, f'{p}: {r.stdout}{r.stderr}')
                        self.assertEqual((c / f'found-{i}').read_bytes(), (c / 'L' / p).read_bytes(), p)
                    # verify --restore-sample
                    self.assertEqual(self.run_(front, 'verify', arc, '--restore-sample', 100).returncode, 1)
                    r = self.run_(front, 'verify', arc, '--restore-sample', 100, *where)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    # keep: without the option nothing is kept; with it, every
                    # record becomes a full copy and needs no reference.
                    chosen = c / 'chosen'
                    chosen.write_text(''.join(p + '\n' for p in needing))
                    r = self.run_(front, 'keep', arc, '--from', chosen, '--yes')
                    self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                    r = self.run_(front, 'keep', arc, '--from', chosen, '--yes', *where)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    shutil.rmtree(c / 'R2')
                    r = self.run_(front, 'verify', arc, '--restore-sample', 100)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    alone(self, index) if (c / 'moved.sqlite').exists() else None

    def test_a_bad_option_is_refused(self):
        for name, front in FRONTS:
            with self.subTest(front=name):
                c, index, records = self.moved()
                for command in (['restore', c / 'archive', sorted(records)[0], c / 'out'], ['verify', c / 'archive'],
                                ['keep', c / 'archive', '--glob', '*']):
                    r = self.run_(front, *command, '--candidates', -1)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertIn('--candidates', r.stderr)


if __name__ == '__main__':
    unittest.main()

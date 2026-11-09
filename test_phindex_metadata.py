"""phindex_metadata.py on synthetic phindex-like indexes: batches in
portable_metadata's format, an index metadata_index can query, errors for stale
rows, and refusal to mix engines. The same cases run against `dng-archive
phindex-metadata` (DNG_ARCHIVE, else ocaml-archive's build, else PATH; skipped
when its usage text lacks the command), whose rows, index, summary and
messages must equal the Python tool's."""
import gzip, json, os, pathlib, re, shutil, sqlite3, subprocess, sys, tempfile, unittest, zlib

import xattr_portable

HERE = pathlib.Path(__file__).parent
ENGINE = 'exiftool-rs 0.12.0 (ExifTool 13.59 tables)'
xattr_portable.install()


def find_binary():
    candidates = [os.environ.get('DNG_ARCHIVE')]
    candidates += [str(p) for p in sorted(HERE.glob('ocaml-archive/_build*/default/bin/main.exe'))]
    candidates.append(shutil.which('dng-archive'))
    binary = next((c for c in candidates if c and os.access(c, os.X_OK)), None)
    if binary and 'phindex-metadata' not in subprocess.run([binary], capture_output=True, text=True).stderr:
        return None
    return binary


BINARY = find_binary()


def deflate(text):
    c = zlib.compressobj(wbits=-15)
    return c.compress(text.encode()) + c.flush()


def batch_texts(archive):
    return {p.name: gzip.decompress(p.read_bytes()).decode()
            for p in sorted((archive / 'metadata/batches').glob('*.jsonl.gz'))}


def index_content(archive):
    """The index row for row without rowids; batches by name only, since the
    gzip bytes (and so their SHA-256) of the two tools differ in the header."""
    db = sqlite3.connect(archive / 'metadata/differences.sqlite3')
    q = lambda sql: sorted(db.execute(sql).fetchall())
    vals = {i: (h, n, zlib.decompress(z)) for i, h, z, n in db.execute('SELECT id,sha256,json_zlib,json_bytes FROM vals')}
    out = dict(
        batches=q('SELECT name FROM batches'),
        files=q('SELECT path,version,extension,batch,compared,errors FROM files'),
        fields=q('SELECT tag,category FROM fields'),
        vals=sorted(vals.values()),
        differences=sorted((p, v, t, k, vals[a][0] if a else None, vals[b][0] if b else None) for p, v, t, k, a, b in db.execute(
            'SELECT f.path,f.version,t.tag,d.kind,d.apo_value,d.bumer_value FROM differences d '
            'JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id')),
        coverage=q('SELECT c.batch,t.tag,c.extension,c.apo_present,c.bumer_present FROM coverage c JOIN fields t ON t.id=c.field_id'),
        field_changes=q('SELECT * FROM field_changes'),
        schema=q("SELECT type,name,sql FROM sqlite_master"))
    db.close()
    return out


def summary(archive):
    s = json.loads((archive / 'metadata/summary.json').read_text())
    s.pop('elapsed_seconds')
    return s


def provenance(archive):
    p = json.loads((archive / 'metadata/provenance.json').read_text())
    p.pop('tool')
    return p


def without_seconds(stdout):
    return re.sub(r'\| \d+s$', '| Ns', stdout, flags=re.M)


# Record JSON as phindex keeps it, written as text so that number spellings,
# escapes and repeated keys reach the tools unchanged.
RICH_APO = ('{"SourceFile": "%s", "ExifTool:DngCompareEngine": "' + ENGINE + '", "EXIF:IFD0:Make": "LEICA",'
            ' "XMP:XMP-crs:Exposure2012": 1.50, "XMP:XMP-crs:Tint": 1E3, "EXIF:ExifIFD:ExposureTime": 0.0000125,'
            ' "MakerNotes:Leica:Big": 123456789012345678901234567890, "XMP:XMP-dc:Title": "caf\\u00e9 \\ud83d\\udcf7 é\\t",'
            ' "XMP:XMP-x:Struct": {"b": [1, 2.0, {"z": null, "a": true}], "a": "x\\/y"}, "EXIF:IFD0:Neg": -0.0,'
            ' "XMP:XMP-x:Dup": 1, "XMP:XMP-x:Dup": 2, "ExifTool:Warning": "Minor issue", "File:System:FileSize": 10,'
            ' "Composite:Lens": "x", "XMP:XMP-x:Inf": 1e400, "XMP:XMP-x:Small": 1e-7, "XMP:XMP-x:Huge": 1e16,'
            ' "EXIF:IFD0:Thumb": "long-sha256:' + 'ab' * 32 + ':2048"}')
RICH_BUMER = ('{"SourceFile": "%s", "ExifTool:DngCompareEngine": "' + ENGINE + '", "EXIF:IFD0:Make": "LEICA",'
              ' "XMP:XMP-crs:Exposure2012": 1.5, "XMP:XMP-crs:Tint": 1000, "EXIF:ExifIFD:ExposureTime": 1.25e-5,'
              ' "XMP:XMP-dc:Title": "café \U0001f4f7", "XMP:XMP-x:Struct": {"a": "x/y", "b": [1, 2, {"a": 1, "z": null}]},'
              ' "EXIF:IFD0:Neg": 0, "XMP:XMP-x:Dup": 3, "File:System:FileSize": 11, "XMP:XMP-x:New": [1, "\\u0001", 0.1],'
              ' "XMP:XMP-x:Inf": 1e999, "XMP:XMP-x:Small": 1.0e-07, "XMP:XMP-x:Huge": 1.5e16,'
              ' "EXIF:IFD0:Thumb": "long-sha256:' + 'cd' * 32 + ':2048"}')


class Tool:
    """How a test runs the tool; Python's phindex_metadata.py by default."""
    def command(self):
        return [sys.executable, str(HERE / 'phindex_metadata.py')]


class PhindexMetadataCases:
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='phindex-metadata-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.left, self.right = self.tmp / 'left', self.tmp / 'right'
        records = {
            'a.dng': ({'EXIF:ExifIFD:LensModel': 'Old', 'EXIF:IFD0:Make': 'LEICA'},
                      {'EXIF:ExifIFD:LensModel': 'New', 'EXIF:IFD0:Make': 'LEICA', 'XMP:XMP-crs:Exposure2012': 0.5}),
            'b.dng': ({'EXIF:IFD0:Make': 'LEICA'}, {'EXIF:IFD0:Make': 'LEICA'}),
            'stale.dng': ({'EXIF:IFD0:Make': 'X'}, {'EXIF:IFD0:Make': 'Y'}),
        }
        for side, root in ((0, self.left), (1, self.right)):
            root.mkdir()
            db = sqlite3.connect(self.tmp / f'{root.name}.sqlite')
            db.execute('CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, gone INTEGER)')
            db.execute('CREATE TABLE metadata (path TEXT PRIMARY KEY, engine TEXT NOT NULL, record_deflate BLOB NOT NULL)')
            for name, recs in records.items():
                p = root / name
                p.write_bytes(name.encode() * (side + 1))
                st = p.stat()
                record = {'SourceFile': str(p), 'ExifTool:DngCompareEngine': ENGINE, **recs[side]}
                size = st.st_size + (name == 'stale.dng' and side == 0)
                db.execute('INSERT INTO files VALUES (?,?,?,0)', (str(p), size, st.st_mtime_ns))
                db.execute('INSERT INTO metadata VALUES (?,?,?)', (str(p), 'exiftool-rs (via phidentify)', deflate(json.dumps(record))))
            db.commit(); db.close()
        self.pairs = self.tmp / 'pairs.tsv'
        self.pairs.write_text(''.join(f'{self.left / n}\t{self.right / n}\n' for n in records))

    def test_wiped_and_refilled_source(self):
        """A shuttle source emptied, then refilled with other contents at an old path:
        earlier rows stay as they are, missing sources are reported, and the new
        content of a.dng is recorded beside the old one under a version."""
        archive = self.tmp / 'shuttle'
        first = self.run_tool(archive)
        self.assertEqual(first.returncode, 1, first.stderr)  # stale.dng's index row is out of date
        batches, index = batch_texts(archive), index_content(archive)
        for f in self.left.iterdir():
            f.unlink()
        wiped = self.run_tool(archive)
        self.assertEqual(wiped.returncode, 1, wiped.stderr)
        for name in ('a.dng', 'b.dng', 'stale.dng'):
            self.assertIn(f'MISSING {name}\n', wiped.stdout)
        self.assertEqual(batch_texts(archive), batches)
        self.assertEqual(index_content(archive), index)
        self.assertEqual(json.loads((archive / 'metadata/summary.json').read_text())['missing_sources'], 3)
        # Refilled: a.dng with another content, the other paths still empty.
        p = self.left / 'a.dng'
        p.write_bytes(b'a new shuttle file')
        st = p.stat()
        db = sqlite3.connect(self.tmp / 'left.sqlite')
        db.execute('UPDATE files SET size=?, mtime_ns=? WHERE path=?', (st.st_size, st.st_mtime_ns, str(p)))
        db.execute('UPDATE metadata SET record_deflate=? WHERE path=?', (deflate(json.dumps(
            {'SourceFile': str(p), 'ExifTool:DngCompareEngine': ENGINE, 'EXIF:ExifIFD:LensModel': 'Newer'})), str(p)))
        db.commit(); db.close()
        refilled = self.run_tool(archive)
        self.assertEqual(refilled.returncode, 1, refilled.stderr)  # b.dng and stale.dng are missing
        self.assertIn('1/1 new pairs', refilled.stdout)
        new = sorted(set(batch_texts(archive)) - set(batches))
        self.assertEqual(new, ['phindex-000002.jsonl.gz'])
        rows = [json.loads(l) for l in gzip.decompress((archive / 'metadata/batches' / new[0]).read_bytes()).decode().splitlines()]
        self.assertEqual(list(rows[0])[:3], ['relative_path', 'version', 'apo'])
        self.assertRegex(rows[0]['version'], r'^\d{8}T\d{6}Z$')
        with sqlite3.connect(archive / 'metadata/differences.sqlite3') as db:
            self.assertEqual(sorted(v for v, in db.execute("SELECT version FROM files WHERE path='a.dng'"))[0], '')
            self.assertEqual(db.execute("SELECT count(*) FROM files WHERE path='a.dng'").fetchone()[0], 2)
        # A rerun finds everything recorded and adds nothing.
        again = self.run_tool(archive)
        self.assertEqual(sorted(batch_texts(archive)), sorted(set(batches) | set(new)))
        self.assertNotIn('new pairs', again.stdout)

    def run_tool(self, archive, command=None, batch_size='2', *extra):
        return subprocess.run([*(command or self.command()), str(self.left), str(self.right), str(archive),
                               '--pairs', str(self.pairs), '--left-index', str(self.tmp / 'left.sqlite'),
                               '--right-index', str(self.tmp / 'right.sqlite'), '--batch-size', batch_size, *extra],
                              capture_output=True, text=True)

    def test_batches_and_index(self):
        archive = self.tmp / 'archive'
        run = self.run_tool(archive)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)  # stale.dng is an error
        rows = [json.loads(l) for p in sorted((archive / 'metadata/batches').glob('*.jsonl.gz'))
                for l in gzip.open(p, 'rt')]
        by = {r['relative_path']: r for r in rows}
        self.assertEqual(sorted(by), ['a.dng', 'b.dng', 'stale.dng'])
        self.assertEqual({(d['tag'], d['kind']) for d in by['a.dng']['differences']},
                         {('EXIF:ExifIFD:LensModel', 'changed_in_bumer'), ('XMP:XMP-crs:Exposure2012', 'added_in_bumer')})
        self.assertEqual(by['b.dng']['differences'], [])
        self.assertEqual(by['stale.dng']['errors'], [{'side': 'apo', 'error': 'phindex row is out of date'}])
        db = sqlite3.connect(archive / 'metadata/differences.sqlite3')
        got = db.execute("SELECT tag, kind, affected_files FROM field_changes ORDER BY tag").fetchall()
        self.assertEqual(got, [('EXIF:ExifIFD:LensModel', 'changed_in_bumer', 1), ('XMP:XMP-crs:Exposure2012', 'added_in_bumer', 1)])
        self.assertEqual(json.loads((archive / 'metadata/provenance.json').read_text())['engine'], ENGINE)
        # A rerun keeps the batches and the index.
        self.assertEqual(self.run_tool(archive).returncode, 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 3)

    def test_refuses_an_archive_with_another_engine(self):
        archive = self.tmp / 'perl-archive'
        (archive / 'metadata/batches').mkdir(parents=True)
        with gzip.open(archive / 'metadata/batches/000000.jsonl.gz', 'wt') as f:
            f.write(json.dumps(dict(relative_path='x.dng', apo='x', bumer='y', apo_metadata={'EXIF:IFD0:Make': 'X'},
                                    bumer_metadata={}, differences=[], errors=[], warnings=[])) + '\n')
        run = self.run_tool(archive)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('not mixing engines', run.stderr + run.stdout)


class PhindexMetadataTest(PhindexMetadataCases, Tool, unittest.TestCase):
    pass


@unittest.skipUnless(BINARY, 'needs a dng-archive with phindex-metadata')
class NativePhindexMetadataTest(PhindexMetadataCases, unittest.TestCase):
    """The same cases against dng-archive, and its outputs against Python's."""
    def command(self):
        return [BINARY, 'phindex-metadata']

    def python(self):
        return Tool().command()

    def add_rich_pairs(self):
        """Pairs with typed, escaped and repeated values, a warning, an error
        record, a missing file, a file without an index row or record, an
        extended attribute, a subdirectory, and relative and commented lines."""
        (self.left / 'sub').mkdir(); (self.right / 'sub').mkdir()
        extra = {'rich.dng': (RICH_APO, RICH_BUMER),
                 'sub/err.dng': ('{"SourceFile": "%s", "ExifTool:DngCompareEngine": "' + ENGINE + '", "ExifTool:Error": "Bad IFD",'
                                 ' "EXIF:IFD0:Make": "A"}', '{"SourceFile": "%s", "ExifTool:DngCompareEngine": "' + ENGINE + '",'
                                 ' "EXIF:IFD0:Make": "B"}'),
                 'gone.dng': None, 'unindexed.dng': 'files', 'norecord.dng': 'metadata', 'proxy.DNG.cop': 'not read', 'same.jpg': 'same'}
        for side, root in ((0, self.left), (1, self.right)):
            db = sqlite3.connect(self.tmp / f'{root.name}.sqlite')
            # phindex's identity columns: a row identified (sha256) with neither a
            # metadata digest nor an error is a type the engine does not read.
            for column in ('sha256', 'md_digest', 'md_err'):
                db.execute(f'ALTER TABLE files ADD COLUMN {column} TEXT')
            for name, recs in extra.items():
                p = root / name
                if recs is None and side == 1:
                    continue  # the right copy does not exist
                p.write_bytes(b'same bytes' if recs == 'same' else b'x' * (7 + side))
                st = p.stat()
                if recs == 'files' and side == 1:
                    continue  # no index row
                db.execute('INSERT INTO files (path, size, mtime_ns, gone, sha256) VALUES (?,?,?,0,?)',
                           (str(p), st.st_size, st.st_mtime_ns,
                            ('ab', 'ef')[side] * 32 if recs == 'not read' else 'cd' * 32 if recs == 'same' else None))
                if recs == 'not read':
                    continue
                if isinstance(recs, tuple):
                    db.execute('INSERT INTO metadata VALUES (?,?,?)', (str(p), 'exiftool-rs (via phidentify)',
                                                                         deflate(recs[side] % p)))
                elif recs != 'metadata':
                    db.execute('INSERT INTO metadata VALUES (?,?,?)', (str(p), 'exiftool-rs (via phidentify)', deflate(
                        json.dumps({'SourceFile': str(p), 'ExifTool:DngCompareEngine': ENGINE, 'EXIF:IFD0:Make': 'C'}))))
            db.commit(); db.close()
        os.setxattr(self.left / 'rich.dng', 'user.phindex-test', b'kept\x00\xffbytes')
        with self.pairs.open('a') as f:
            f.write('# a comment line\n\nrich.dng\trich.dng\n')
            f.write(f'{self.left}/sub/err.dng\tsub/err.dng\r\n')
            f.write(''.join(f'{n}\t{self.right / n}\n' for n in ('gone.dng', 'unindexed.dng', 'norecord.dng', 'proxy.DNG.cop', 'same.jpg')))

    def assert_same_as_python(self, name, batch_size='2'):
        py, ml = self.tmp / f'{name}-py', self.tmp / f'{name}-ml'
        a = self.run_tool(py, self.python(), batch_size)
        b = self.run_tool(ml, None, batch_size)
        self.assertEqual((b.returncode, b.stderr), (a.returncode, a.stderr), b.stdout + b.stderr)
        self.assertEqual(without_seconds(b.stdout.replace(str(ml), str(py))), without_seconds(a.stdout))
        # The lines are json.dumps(row, ensure_ascii=True)'s text, so the rows parse alike.
        self.assertEqual(batch_texts(ml), batch_texts(py))
        self.assertEqual(index_content(ml), index_content(py))
        self.assertEqual(summary(ml), summary(py))
        self.assertEqual(provenance(ml), provenance(py))
        self.assertEqual(json.loads((ml / 'metadata/provenance.json').read_text())['tool'], 'dng-archive phindex-metadata')
        return py, ml

    def test_outputs_equal_python(self):
        self.add_rich_pairs()
        py, ml = self.assert_same_as_python('rich')
        rows = {r['relative_path']: r for t in batch_texts(ml).values() for r in map(json.loads, t.splitlines())}
        self.assertEqual(rows['gone.dng']['errors'], [{'side': 'bumer', 'error':
                         f"[Errno 2] No such file or directory: '{self.right / 'gone.dng'}'"}])
        self.assertEqual(rows['unindexed.dng']['errors'], [{'side': 'bumer', 'error': 'not in the phindex index'}])
        self.assertEqual(rows['norecord.dng']['errors'], [{'side': 'apo', 'error': 'phindex has no metadata record'},
                                                          {'side': 'bumer', 'error': 'phindex has no metadata record'}])
        self.assertEqual(rows['sub/err.dng']['errors'], [{'side': 'apo', 'error': 'Bad IFD'}])
        # A type phindex does not read metadata from is listed, not an error.
        self.assertEqual((rows['proxy.DNG.cop']['errors'], rows['proxy.DNG.cop']['no_metadata'],
                          rows['proxy.DNG.cop']['differences']), ([], ['apo', 'bumer'], []))
        self.assertNotIn('no_metadata', rows['norecord.dng'])
        # Byte-identical sides: the hash instead of the records, which are not read.
        same = rows['same.jpg']
        self.assertEqual((same['same_bytes_sha256'], same['apo_metadata'], same['bumer_metadata'], same['differences'],
                          same['errors']), ('cd' * 32, {}, {}, [], []))
        self.assertEqual(summary(ml)['files_byte_identical'], 1)
        self.assertEqual((summary(ml)['files_without_metadata'], summary(ml)['metadata_errors']), (1, 5))
        self.assertIn('user.phindex-test', rows['rich.dng']['apo_filesystem']['xattrs'])
        self.assertEqual(rows['rich.dng']['warnings'], [{'side': 'apo', 'warning': 'Minor issue'}])
        self.assertEqual({d['tag'] for d in rows['rich.dng']['differences']},
                         {'XMP:XMP-dc:Title', 'XMP:XMP-x:Dup', 'File:System:FileSize', 'XMP:XMP-x:New', 'XMP:XMP-x:Huge',
                          'MakerNotes:Leica:Big', 'EXIF:IFD0:Thumb'})
        # Each tool resumes the other's batches, and the index keeps its rows.
        for archive, other in ((py, self.command()), (ml, self.python())):
            before = index_content(archive)
            run = self.run_tool(archive, other)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertEqual(index_content(archive), before)
        # One batch for all pairs gives the same as well.
        self.assert_same_as_python('one', batch_size='100')

    def test_refusals_equal_python(self):
        # An archive with metadata of another engine. (A batch listing other
        # files is no refusal: new rows go to the next batch numbers.)
        for batch, needle in (('000000.jsonl.gz', 'its metadata comes from'),):
            for name in ('py', 'ml'):
                batches = self.tmp / f'{needle[:5]}-{name}/metadata/batches'; batches.mkdir(parents=True)
                (batches / batch).write_bytes(gzip.compress((json.dumps(dict(
                    relative_path='x.dng', apo='x', bumer='y', apo_metadata={'ExifTool:DngCompareEngine': 'exiftool-rs 0.1'},
                    bumer_metadata={}, differences=[], errors=[], warnings=[])) + '\n').encode()))
            a = self.run_tool(self.tmp / f'{needle[:5]}-py', self.python())
            b = self.run_tool(self.tmp / f'{needle[:5]}-ml')
            self.assertEqual(b.returncode, a.returncode)
            self.assertEqual(b.stderr.replace(f'{needle[:5]}-ml', f'{needle[:5]}-py'), a.stderr)
            self.assertIn(needle, a.stderr)
        # Records of two engines in one run.
        db = sqlite3.connect(self.tmp / 'right.sqlite')
        p = self.right / 'b.dng'
        db.execute('UPDATE metadata SET record_deflate=? WHERE path=?', (deflate(json.dumps(
            {'SourceFile': str(p), 'ExifTool:DngCompareEngine': 'exiftool-rs 0.13.0'})), str(p)))
        db.commit(); db.close()
        a = self.run_tool(self.tmp / 'mixed-py', self.python())
        b = self.run_tool(self.tmp / 'mixed-ml')
        self.assertEqual((b.returncode, b.stderr), (a.returncode, a.stderr))
        self.assertIn('not mixing engines', a.stderr)
        self.assertEqual(batch_texts(self.tmp / 'mixed-ml'), batch_texts(self.tmp / 'mixed-py'))

    def test_usage_errors_equal_python(self):
        bad = self.tmp / 'bad.tsv'
        bad.write_text(f'{self.left}/a.dng\n')
        outside = self.tmp / 'outside.tsv'
        outside.write_text(f'{self.right}/a.dng\t{self.right}/a.dng\n')
        for args in (['--pairs', str(bad)], ['--pairs', str(self.tmp / 'missing.tsv')], ['--pairs', str(outside)],
                     ['--batch-size', '0'], ['--left-index', str(self.tmp / 'missing.sqlite')]):
            a = self.run_tool(self.tmp / 'u', self.python(), '2', *args)
            b = self.run_tool(self.tmp / 'u', None, '2', *args)
            self.assertEqual((a.returncode, b.returncode), (2, 2), args)
            self.assertEqual(b.stderr.splitlines()[-1].split('error: ', 1)[1], a.stderr.splitlines()[-1].split('error: ', 1)[1])


if __name__ == '__main__':
    unittest.main()

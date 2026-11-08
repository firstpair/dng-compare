"""phindex_metadata.py on synthetic phindex-like indexes: batches in
portable_metadata's format, an index metadata_index can query, errors for stale
rows, and refusal to mix engines."""
import gzip, json, os, pathlib, sqlite3, subprocess, sys, tempfile, unittest, zlib

HERE = pathlib.Path(__file__).parent
ENGINE = 'exiftool-rs 0.12.0 (ExifTool 13.59 tables)'


def deflate(text):
    c = zlib.compressobj(wbits=-15)
    return c.compress(text.encode()) + c.flush()


class PhindexMetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='phindex-metadata-')).resolve()
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

    def run_tool(self, archive):
        return subprocess.run([sys.executable, str(HERE / 'phindex_metadata.py'), str(self.left), str(self.right), str(archive),
                               '--pairs', str(self.pairs), '--left-index', str(self.tmp / 'left.sqlite'),
                               '--right-index', str(self.tmp / 'right.sqlite'), '--batch-size', '2'],
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


if __name__ == '__main__':
    unittest.main()

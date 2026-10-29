import gzip, json, pathlib, sqlite3 as plain, tempfile, unittest
import metadata_index

def row(path, version=None, rating=1):
    r = dict(relative_path=path, apo='/s/' + path, bumer='/r/' + path,
             apo_metadata={'XMP:XMP-xmp:Rating': rating}, bumer_metadata={'XMP:XMP-xmp:Rating': 0},
             differences=[dict(tag='XMP:XMP-xmp:Rating', kind='changed_in_bumer', apo=rating, bumer=0, category='embedded')],
             errors=[], warnings=[])
    if version is not None: r['version'] = version
    return r

def batch(archive, name, rows):
    d = archive / 'metadata/batches'; d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(gzip.compress(''.join(json.dumps(r) + '\n' for r in rows).encode()))
    return d / name

class MetadataVersions(unittest.TestCase):
    def test_versions_of_one_path(self):
        with tempfile.TemporaryDirectory() as td:
            a = pathlib.Path(td); (a / 'metadata').mkdir()
            db = metadata_index.open_index(a)
            metadata_index.ingest(db, batch(a, '000000.jsonl.gz', [row('x.dng')]))
            metadata_index.ingest(db, batch(a, '000001.jsonl.gz', [row('x.dng', '20261029T000000Z', 3)]))
            self.assertEqual(sorted(db.execute('SELECT path,version FROM files')),
                             [('x.dng', ''), ('x.dng', '20261029T000000Z')])
            db.close()

    def test_existing_index_is_migrated(self):
        with tempfile.TemporaryDirectory() as td:
            a = pathlib.Path(td); (a / 'metadata').mkdir()
            old = plain.connect(a / 'metadata/differences.sqlite3')
            old.executescript(metadata_index.SCHEMA.replace(
                "path TEXT NOT NULL, version TEXT NOT NULL DEFAULT '',", "path TEXT UNIQUE NOT NULL,").replace(", UNIQUE(path,version))", ")"))
            old.close()
            db = metadata_index.open_index(a)
            metadata_index.ingest(db, batch(a, '000000.jsonl.gz', [row('y.dng')]))
            self.assertIn('version', [r[1] for r in db.execute('PRAGMA table_info(files)')])
            metadata_index.ingest(db, batch(a, '000001.jsonl.gz', [row('y.dng', 'later')]))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM differences').fetchone()[0], 2)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            db.close()

if __name__ == '__main__': unittest.main()

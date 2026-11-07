import contextlib, io, json, pathlib, tempfile, unittest
from unittest.mock import patch
import preserve_apo as p

class PreservationPairs(unittest.TestCase):
    def test_listed_subset_with_relocated_references(self):
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td).resolve();src=root/'source';ref=root/'reference';arc=root/'archive'
            (src/'s1').mkdir(parents=True);(ref/'s1').mkdir(parents=True);(ref/'moved').mkdir()
            data=bytes(range(256))*4096
            (src/'s1/a.dng').write_bytes(data+b'left metadata')
            (ref/'s1/a.dng').write_bytes(data+b'right metadata')
            (src/'s1/b.dng').write_bytes(data+b'left b')
            (ref/'moved/b.dng').write_bytes(data+b'right b')
            (src/'s1/unlisted.dng').write_bytes(b'not preserved')
            pairs=[(str(src/'s1/a.dng'),str(ref/'s1/a.dng')),(str(src/'s1/b.dng'),str(ref/'moved/b.dng'))]
            with patch.object(p,'load_known',return_value={}),contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(p.scan(src,ref,arc,root,10000,delta_mode=True,pairs=pairs))
                records={r['relative_path']:r for r in map(json.loads,(arc/'manifest.jsonl').read_text().splitlines())}
                self.assertEqual(set(records),{'s1/a.dng','s1/b.dng'})
                self.assertEqual(records['s1/b.dng']['reference_relative_path'],'moved/b.dng')
                for rel,r in records.items():
                    self.assertTrue(r.get('delta'),rel)
                    self.assertEqual(p.reconstruct(r,arc),p.digest(src/rel))
                # A later subset adds to the same archive and leaves earlier records.
                (src/'s1/c.dng').write_bytes(data+b'later')
                (ref/'s1/c.dng').write_bytes(data+b'later ref')
                self.assertTrue(p.scan(src,ref,arc,root,10000,delta_mode=True,pairs=[(str(src/'s1/c.dng'),str(ref/'s1/c.dng'))]))
                records={r['relative_path'] for r in map(json.loads,(arc/'manifest.jsonl').read_text().splitlines())}
                self.assertEqual(records,{'s1/a.dng','s1/b.dng','s1/c.dng'})
            with self.assertRaises(ValueError):
                p.scan(src,ref,arc,root,10000,delta_mode=True,pairs=[(str(root/'elsewhere.dng'),None)])

if __name__=='__main__':unittest.main()

class PreservationWithPhindex(unittest.TestCase):
    def test_identical_files_with_current_index_rows_are_recorded_without_reading(self):
        import hashlib, os, sqlite3
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td).resolve();src=root/'source';ref=root/'reference'
            src.mkdir();ref.mkdir()
            data=bytes(range(256))*4096
            for d in (src,ref):(d/'same.dng').write_bytes(data)
            (src/'diff.dng').write_bytes(data+b'left');(ref/'diff.dng').write_bytes(data+b'right')
            for d,name in ((src,'s.sqlite'),(ref,'r.sqlite')):
                db=sqlite3.connect(root/name)
                db.execute('CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT, gone INTEGER)')
                for f in ('same.dng','diff.dng'):
                    st=os.stat(d/f);db.execute('INSERT INTO files VALUES (?,?,?,?,0)',(str(d/f),st.st_size,st.st_mtime_ns,hashlib.sha256((d/f).read_bytes()).hexdigest()))
                db.commit();db.close()
            pairs=[(str(src/f),str(ref/f)) for f in ('same.dng','diff.dng')]
            def preserve(arc,indexed):
                p.INDEXES=None
                if indexed:p.open_indexes(root/'s.sqlite',root/'r.sqlite')
                with patch.object(p,'load_known',return_value={}),contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(p.scan(src,ref,arc,root,10000,delta_mode=True,pairs=pairs))
                p.INDEXES=None
                rows={r['relative_path']:r for r in map(json.loads,(arc/'manifest.jsonl').read_text().splitlines())}
                for r in rows.values():r['source_attrs'].pop('atime_ns')
                return rows
            plain=preserve(root/'plain',False)
            with patch.object(p,'native_call',side_effect=AssertionError('no native reads')):
                real_open=pathlib.Path.open
                opened=[]
                def tracking(self,*a,**k):
                    opened.append(self.name);return real_open(self,*a,**k)
                with patch.object(pathlib.Path,'open',tracking):
                    indexed=preserve(root/'indexed',True)
            self.assertEqual(indexed,plain)
            self.assertNotIn('same.dng',opened)
            self.assertTrue(indexed['same.dng']['whole_file_equal'])
            self.assertTrue(indexed['diff.dng'].get('delta'))

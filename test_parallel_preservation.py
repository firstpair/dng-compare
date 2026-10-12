import contextlib, io, json, pathlib, tempfile, unittest
from unittest.mock import patch
import preserve_apo as p

class ParallelPreservation(unittest.TestCase):
    def assert_summary_matches_manifest(self, archive):
        records=[json.loads(s) for s in (archive/'manifest.jsonl').read_text().splitlines()]
        summary=json.loads((archive/'summary.json').read_text())
        self.assertEqual(summary['verified_files'],len(records))
        self.assertEqual(summary['source_bytes_verified'],sum(r['source_identity'][1] for r in records))
        self.assertEqual(summary['preserved_payload_bytes'],sum(r['preserved_bytes'] for r in records))
        self.assertEqual(summary['byte_different_files'],sum(not r['whole_file_equal'] for r in records))

    def test_parallel_restore_resume_and_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td);src=root/'source';ref=root/'reference';arc=root/'archive'
            src.mkdir();ref.mkdir()
            for i in range(12):
                data=bytes(range(256))*4096
                (src/str(i)).write_bytes(data+(b'changed' if i%3==0 else b''))
                if i%3!=2:(ref/str(i)).write_bytes(data)
            with patch.object(p,'load_known',return_value={}),contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(p.scan(src,ref,arc,root,10000,delta_mode=True,workers=4))
                self.assert_summary_matches_manifest(arc)
                records=[json.loads(s) for s in (arc/'manifest.jsonl').read_text().splitlines()]
                self.assertEqual(len(records),12)
                for r in records:self.assertEqual(p.reconstruct(r,arc),p.digest(src/r['relative_path']))
                statements=[];connect=p.sqlite3.connect
                class ObservedConnection:
                    def __init__(self, connection):self.connection=connection
                    def __getattr__(self, name):return getattr(self.connection,name)
                    def execute(self, sql, *args):
                        statements.append(sql)
                        return self.connection.execute(sql,*args)
                with patch.object(p,'preserve_file',side_effect=AssertionError('resume recomputed valid file')), \
                     patch.object(p.sqlite3,'connect',side_effect=lambda *a,**kw:ObservedConnection(connect(*a,**kw))):
                    self.assertTrue(p.scan(src,ref,arc,root,10000,delta_mode=True,workers=4))
                self.assertFalse(any(s.lstrip().split()[0].upper() in ('INSERT','DELETE','UPDATE','REPLACE') for s in statements),statements)
                self.assertFalse(any('SELECT record FROM files WHERE path=' in s for s in statements),statements)
                self.assert_summary_matches_manifest(arc)
                def fail_callback(rel):
                    if rel=='0':raise ValueError('Simulated checkpoint notification failure')
                self.assertFalse(p.scan(src,ref,arc,root,10000,delta_mode=True,workers=4,on_verified=fail_callback))
                self.assert_summary_matches_manifest(arc)
                self.assertTrue(p.scan(src,ref,arc,root,10000,delta_mode=True,workers=4))
                self.assert_summary_matches_manifest(arc)
                changed=next(r for r in records if r.get('delta'))
                (arc/changed['delta']).write_bytes(b'broken')
                self.assertTrue(p.scan(src,ref,arc,root,10000,delta_mode=True,workers=4))
                self.assert_summary_matches_manifest(arc)
                for r in map(json.loads,(arc/'manifest.jsonl').read_text().splitlines()):
                    self.assertEqual(p.reconstruct(r,arc),p.digest(src/r['relative_path']))

if __name__=='__main__':unittest.main()

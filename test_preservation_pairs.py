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

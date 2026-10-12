import contextlib,io,json,pathlib,tempfile,unittest
from unittest.mock import patch
import numpy as np
import tifffile
import database_backend as database
import image_categories as images
from preserve_apo import atomic_json,identity,digest

class ParallelImages(unittest.TestCase):
    def test_parallel_parity_resume_and_changed_input(self):
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td);source=root/'source';reference=root/'reference'
            source.mkdir();reference.mkdir();records=[]
            for i in range(8):
                a=source/f'{i}.DNG';b=reference/a.name
                if i==6:
                    a.write_bytes(b'invalid DNG');b.write_bytes(b'other invalid DNG')
                else:
                    data=np.arange(16,dtype=np.uint16).reshape(4,4)
                    tifffile.imwrite(a,data,metadata=None,extratags=[(50706,'B',4,(1,4,0,0),False)])
                    tifffile.imwrite(b,data+(i==5),metadata=None,extratags=[(50706,'B',4,(1,4,0,0),False)])
                records.append(dict(relative_path=a.name,source=str(a),reference=str(b) if i!=7 else None,
                    source_identity=identity(a),reference_identity=identity(b) if i!=7 else None,
                    source_sha256=digest(a),reference_sha256=digest(b) if i!=7 else None,whole_file_equal=i==0))
            outputs=[]
            for workers in [1,4]:
                archive=root/f'archive{workers}';archive.mkdir()
                atomic_json(archive/'summary.json',dict(byte_preservation_complete=True))
                db=database.connect(archive/'recovery.sqlite3')
                db.execute('CREATE TABLE files(path TEXT PRIMARY KEY,record TEXT)')
                for rec in records:db.execute('INSERT INTO files VALUES (?,?)',(rec['relative_path'],json.dumps(rec)))
                db.commit();db.close()
                with contextlib.redirect_stdout(io.StringIO()):images.categorize(archive,workers,1024)
                outputs.append((archive/'image-comparison/pairs.jsonl').read_text())
                summary=json.loads((archive/'image-comparison/summary.json').read_text())
                self.assertTrue(summary['complete']);self.assertEqual(summary['dng_files'],8)
                self.assertEqual(summary['counts']['raw_different'],1)
                self.assertEqual(summary['counts']['raw_unverified'],1)
                self.assertEqual(summary['counts']['raw_missing_reference'],1)
                with patch.object(images,'compare_pixels',side_effect=AssertionError('cached result recomputed')):
                    with contextlib.redirect_stdout(io.StringIO()):images.categorize(archive,workers,1024)
                self.assertEqual(outputs[-1],(archive/'image-comparison/pairs.jsonl').read_text())
            self.assertEqual(outputs[0],outputs[1])
            (source/'1.DNG').write_bytes(b'changed input')
            with self.assertRaisesRegex(ValueError,'Image input changed'):
                with contextlib.redirect_stdout(io.StringIO()):images.categorize(root/'archive4',4,1024)

if __name__=='__main__':unittest.main()

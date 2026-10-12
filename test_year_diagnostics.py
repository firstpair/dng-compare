import contextlib,gzip,io,json,pathlib,shutil,subprocess,sys,tempfile,unittest
import numpy as np
import tifffile
import preserve_apo as preservation
import portable_metadata as metadata
from metadata_index import open_index,ingest

PROJECT=pathlib.Path(__file__).resolve().parent

class DiagnosticVerification(unittest.TestCase):
    def test_format_diagnostics_and_archive_failures(self):
        with tempfile.TemporaryDirectory(prefix='year-diagnostic-test-') as td:
            t=pathlib.Path(td);source=t/'source';reference=t/'reference';root=t/'archive';archive=root/'Fixture/2020';tools=root/'tools'
            for directory in [source,reference,archive,tools]:directory.mkdir(parents=True,exist_ok=True)
            (source/'bad.DNG').write_bytes(b'7\x8d\xf0\xaa'+bytes(range(128)))
            tifffile.imwrite(reference/'bad.DNG',np.arange(16,dtype=np.uint16).reshape(4,4),metadata=None,extratags=[(50706,'B',4,(1,4,0,0),False)])
            for name in ['preserve_apo.py','portable_metadata.py','metadata_index.py','xattr_portable.py','preserve_years.py','finalize_year.py']:
                shutil.copy2(PROJECT/name,tools/name)
            shutil.copy2(PROJECT/'rust-scanner/target/release/dng-subset',tools/'dng-subset')
            provenance=dict(tools={p.name:preservation.digest(p) for p in tools.iterdir()},source=str(source),reference=str(reference))
            preservation.atomic_json(root/'provenance.json',provenance);preservation.atomic_json(archive/'provenance.json',provenance)
            old_preservation,old_metadata=preservation.load_known,metadata.known_pairs
            try:
                preservation.load_known=lambda project:{};metadata.known_pairs=lambda project:{}
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(preservation.scan(source,reference,archive,PROJECT,200,delta_mode=True))
                    metadata.snapshot(source,reference,archive,PROJECT,16)
            finally:preservation.load_known,metadata.known_pairs=old_preservation,old_metadata
            subprocess.run([str(tools/'dng-subset'),'--archive-xmp',str(source),str(archive/'xmp-original')],check=True,capture_output=True)
            command=[sys.executable,str(PROJECT/'finalize_year.py'),str(archive)]
            def verify():
                result=subprocess.run(command,capture_output=True,text=True)
                self.assertTrue((archive/'verification.json').exists(),result.stdout+result.stderr)
                return result,json.loads((archive/'verification.json').read_text())
            result,verification=verify()
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertTrue(verification['archive_verified'],verification)
            self.assertTrue(verification['comparison_complete'],verification)
            self.assertFalse(verification['verified'],verification)
            self.assertTrue(verification['input_diagnostics'],verification)
            self.assertTrue(verification['xmp_input_diagnostics'],verification)
            self.assertEqual(verification['execution_issues'],[],verification)
            self.assertEqual(verification['image_comparison']['counts']['raw_unverified'],1)
            self.assertEqual(verification['files_with_embedded_metadata_differences'],0)
            record=json.loads((archive/'manifest.jsonl').read_text().strip())
            self.assertEqual(preservation.reconstruct(record,archive),preservation.digest(source/'bad.DNG'))
            delta=archive/record['delta'];original_delta=delta.read_bytes();delta.write_bytes(b'corrupted-delta')
            result,verification=verify()
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse(verification['archive_verified'],result.stdout+result.stderr+str(verification))
            self.assertTrue(any('corrupted VCDIFF' in issue for issue in verification['execution_issues']),verification)
            delta.write_bytes(original_delta)
            batch=next((archive/'metadata/batches').glob('*.jsonl.gz'))
            with gzip.open(batch,'rt') as f:rows=[json.loads(line) for line in f]
            rows[0]['errors']=[dict(side='apo',error='ExifTool returned no record')]
            with gzip.open(batch,'wt') as f:
                for row in rows:f.write(json.dumps(row)+'\n')
            preservation.atomic_json(archive/'metadata/checksums.json',[dict(path=str(batch.relative_to(archive)),bytes=batch.stat().st_size,sha256=preservation.digest(batch))])
            db=open_index(archive);ingest(db,batch,rows);db.close()
            result,verification=verify()
            self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertFalse(verification['comparison_complete'],verification)
            self.assertTrue(any('Unresolved metadata errors' in issue for issue in verification['execution_issues']),(verification,result.stdout,result.stderr))

if __name__=='__main__':unittest.main()

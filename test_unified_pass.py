import contextlib
import gzip
import io
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import tifffile
import portable_metadata as metadata
import preserve_apo as preservation
import unified_pass

PROJECT = pathlib.Path(__file__).resolve().parent


def rows(archive):
    result = []
    for batch in sorted((archive / 'metadata/batches').glob('*.jsonl.gz')):
        with gzip.open(batch, 'rt') as f:result.extend(map(json.loads, f))
    return result


def comparable(row):
    result = {k:row[k] for k in ('relative_path', 'differences', 'errors', 'warnings')}
    result['differences'] = [r for r in result['differences'] if not r['tag'].endswith(':FileAccessDate')]
    for side in ('apo_metadata', 'bumer_metadata'):
        result[side] = {k:v for k,v in row[side].items() if not k.endswith(':FileAccessDate')}
    return result


class UnifiedPass(unittest.TestCase):
    def test_bulk_snapshot_reader_checks_gzip_integrity(self):
        rows=[{'typed':[None,True,1,1.5,'é\nvalue']},{'nested':{'empty':[]}}]
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory)/'batch.jsonl.gz'
            saved=gzip.compress((''.join(json.dumps(row)+'\n' for row in rows)).encode())
            path.write_bytes(saved)
            self.assertEqual(metadata.read_rows(path),rows)
            corrupt=bytearray(saved);corrupt[-8]^=1;path.write_bytes(corrupt)
            with self.assertRaises(gzip.BadGzipFile):metadata.read_rows(path)
            path.write_bytes(saved[:-4])
            with self.assertRaises(EOFError):metadata.read_rows(path)

    def test_interruption_cancels_metadata_even_when_all_bytes_are_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root=pathlib.Path(td);source=root/'source';reference=root/'reference'
            source.mkdir();reference.mkdir();(source/'file').write_bytes(b'input')
            release=threading.Event();acknowledged=threading.Event()
            def scan(*args,**kwargs):
                kwargs['on_verified']('file')
                threading.Timer(.01,release.set).start()
                raise KeyboardInterrupt('Controlled interruption')
            def snapshot(*args,**kwargs):
                self.assertTrue(release.wait(2))
                try:kwargs['wait_verified'](['file'])
                except ValueError as error:
                    self.assertIn('interrupted',str(error));acknowledged.set()
            with patch.object(preservation,'scan',side_effect=scan),patch.object(metadata,'snapshot',side_effect=snapshot):
                with self.assertRaises(KeyboardInterrupt):
                    unified_pass.run(source,reference,root/'archive',PROJECT,{},10000,4)
            self.assertTrue(acknowledged.is_set(),'Already-ready metadata ignored cancellation')

    def test_native_backends_overlap_parity_resume_and_failure(self):
        for backend in ('rust', 'oxcaml'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td);source = root/'source';reference = root/'reference'
                source.mkdir();reference.mkdir()
                for i in range(12):
                    data = np.arange(1024, dtype=np.uint16).reshape(32,32)
                    tifffile.imwrite(source/f'{i:02}.tif', data + (i % 3 == 0), metadata=None)
                    if i % 3 != 2:tifffile.imwrite(reference/f'{i:02}.tif', data, metadata=None)
                env = {'DNG_BYTE_BACKEND':str(PROJECT/('byte-verify-oxcaml' if backend=='oxcaml' else 'rust-scanner/target/release/byte-verify')),
                       'DNG_METADATA_BACKEND':str(PROJECT/('metadata-extract-oxcaml' if backend=='oxcaml' else 'rust-scanner/target/release/metadata-extract')),
                       'DNG_METADATA_WORKERS':'4', 'DNG_METADATA_PREFETCH_BATCHES':'1'}
                with patch.dict(os.environ, env), patch.object(preservation,'load_known',return_value={}), \
                     patch.object(metadata,'known_pairs',return_value={}), contextlib.redirect_stdout(io.StringIO()):
                    serial=root/'serial';combined=root/'combined'
                    self.assertTrue(preservation.scan(source,reference,serial,PROJECT,10000,delta_mode=True,workers=4))
                    metadata.snapshot(source,reference,serial,PROJECT,2)
                    extracted=threading.Event();original_ingest=metadata.ingest;original_preserve=preservation.preserve_file
                    def ingest(*args):
                        result=original_ingest(*args);extracted.set();return result
                    def preserve(path,*args,**kwargs):
                        if path.name=='11.tif':
                            self.assertTrue(extracted.wait(20),'Metadata did not overlap byte verification')
                        return original_preserve(path,*args,**kwargs)
                    with patch.object(metadata,'ingest',side_effect=ingest),patch.object(preservation,'preserve_file',side_effect=preserve):
                        self.assertTrue(unified_pass.run(source,reference,combined,PROJECT,{},10000,4,batch_size=2))
                    self.assertEqual(list(map(comparable,rows(serial))),list(map(comparable,rows(combined))))
                    def indexed(archive):
                        path=archive/'metadata/differences.sqlite3'
                        with sqlite3.connect('file:'+str(path)+'?immutable=1',uri=True) as db:
                            differences=db.execute("SELECT f.path,t.tag,t.category,d.kind,a.json_zlib,b.json_zlib FROM differences d JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id LEFT JOIN vals a ON a.id=d.apo_value LEFT JOIN vals b ON b.id=d.bumer_value WHERE t.tag NOT LIKE '%:FileAccessDate' ORDER BY f.path,t.tag").fetchall()
                            coverage=db.execute('SELECT * FROM field_coverage ORDER BY tag,category,extension').fetchall()
                            return differences,coverage
                    self.assertEqual(indexed(serial),indexed(combined),'Isolated indexing changed typed differences or coverage')
                    records=list(map(json.loads,(combined/'manifest.jsonl').read_text().splitlines()))
                    self.assertEqual(len(records),12)
                    for record in records:
                        self.assertEqual(preservation.reconstruct(record,combined),preservation.digest(source/record['relative_path']))
                    with patch.object(preservation,'preserve_file',side_effect=AssertionError('Recomputed saved byte record')), \
                         patch.object(metadata,'extract',side_effect=AssertionError('Recomputed saved metadata')):
                        self.assertTrue(unified_pass.run(source,reference,combined,PROJECT,{},10000,4,batch_size=2))
                    # Failed byte work must release a waiting metadata consumer and fail the run.
                    with patch.object(preservation,'preserve_file',side_effect=ValueError('Fixture byte failure')):
                        with self.assertRaisesRegex(ValueError,'Byte verification did not succeed'):
                            unified_pass.run(source,reference,root/'failed',PROJECT,{},10000,4,batch_size=2)


if __name__=='__main__':unittest.main()

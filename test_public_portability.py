"""Fixture-only checks for public CLI portability and protected input scopes."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import tifffile
import preserve_apo as preservation
import portable_metadata as metadata
import benchmark_metadata_workers as benchmark

PROJECT=Path(__file__).resolve().parent


class PublicPortability(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='dng-public-portability-')
        self.root=Path(self.temporary.name)
        self.source=self.root/'source';self.reference=self.root/'reference'
        self.source.mkdir();self.reference.mkdir()

    def tearDown(self):self.temporary.cleanup()

    def command(self,script,*arguments):
        return subprocess.run([sys.executable,str(PROJECT/script),*map(str,arguments)],capture_output=True,text=True)

    def test_clean_clone_scan_preserves_and_restores_without_legacy_ledgers(self):
        payload=b'public fixture\x00\xff'*1024
        (self.source/'file.bin').write_bytes(payload)
        (self.reference/'file.bin').write_bytes(payload+b'changed')
        archive=self.root/'archive';empty_project=self.root/'empty-project';empty_project.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(preservation.scan(self.source,self.reference,archive,empty_project,10000,full_copy=True))
        record=json.loads((archive/'manifest.jsonl').read_text().strip())
        target=self.root/'restored.bin';preservation.reconstruct(record,archive,target)
        self.assertEqual(target.read_bytes(),payload)
        self.assertEqual((self.source/'file.bin').read_bytes(),payload)
        self.assertEqual((self.reference/'file.bin').read_bytes(),payload+b'changed')
        self.assertEqual(metadata.known_pairs(empty_project),{})

    def test_overlapping_output_rejected_before_source_write(self):
        (self.source/'file.bin').write_bytes(b'protected')
        for archive in (self.source,self.source/'archive',self.reference/'archive',self.root):
            with self.subTest(archive=archive):
                with self.assertRaisesRegex(ValueError,'outside and disjoint'):
                    preservation.scan(self.source,self.reference,archive,PROJECT,100)
        self.assertEqual(sorted(path.name for path in self.source.iterdir()),['file.bin'])
        self.assertEqual(list(self.reference.iterdir()),[])

    def test_output_symlink_into_input_is_rejected(self):
        alias=self.root/'output-alias';alias.symlink_to(self.source,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'outside and disjoint'):
            metadata.snapshot(self.source,self.reference,alias,PROJECT,16)
        self.assertEqual(list(self.source.iterdir()),[])

    def test_duplicate_byte_phase_owner_is_rejected(self):
        archive=self.root/'archive'
        with preservation.archive_lock(archive,'.byte-preservation.lock'):
            with self.assertRaisesRegex(ValueError,'Another process owns'):
                preservation.scan(self.source,self.reference,archive,PROJECT,100)
        self.assertFalse((archive/'recovery.sqlite3').exists())

    def test_prepared_inventory_preserves_literal_source_alias(self):
        (self.source/'file.bin').write_bytes(b'alias fixture')
        (self.reference/'file.bin').write_bytes(b'alias fixture')
        alias=self.root/'source-alias';alias.symlink_to(self.source,target_is_directory=True)
        rows=[{'apo':str(alias/'file.bin')}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(preservation.scan(alias,self.reference,self.root/'archive',PROJECT,10000,
                                              prepared_inventory=([],rows)))
        record=json.loads((self.root/'archive/manifest.jsonl').read_text().strip())
        self.assertEqual(record['source'],str(alias/'file.bin'))

    def test_year_cli_requires_explicit_roots_without_creating_archive(self):
        archive=self.root/'not-created'
        result=self.command('preserve_years.py',archive)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertFalse(archive.exists())

    def test_generic_year_pipeline_runs_with_new_labels(self):
        for side in (self.source,self.reference):(side/'2020').mkdir()
        data=np.arange(64,dtype=np.uint16).reshape(8,8)
        for side in (self.source,self.reference):
            tifffile.imwrite(side/'2020/same.DNG',data,metadata=None,
                             extratags=[(50706,'B',4,(1,4,0,0),False)])
        archive=self.root/'year-archive'
        result=self.command('preserve_years.py',archive,'--source-root',f'Fixture={self.source}',
                            '--reference-root',self.reference,'--database-backend','sqlite',
                            '--preservation-workers','2','--rate-mib','10000')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        verification=json.loads((archive/'Fixture/2020/verification.json').read_text())
        self.assertTrue(verification['archive_verified'],verification)
        self.assertTrue(verification['comparison_complete'],verification)
        self.assertEqual(json.loads((archive/'scope.json').read_text())['reference'],str(self.reference.resolve()))
        self.assertEqual((self.source/'2020/same.DNG').read_bytes(),(self.reference/'2020/same.DNG').read_bytes())
        self.assertTrue(json.loads((archive/'summary.json').read_text())['complete'])

    def test_cache_export_is_scoped_and_never_overwrites(self):
        for side in (self.source,self.reference):(side/'file.DNG').write_bytes(b'fixture')
        report=self.root/'reports';report.mkdir();cache=report/'cache.sqlite3'
        with sqlite3.connect(cache) as db:
            db.execute('CREATE TABLE results(key TEXT, result TEXT)')
            for version,left,right in ((4,self.source/'file.DNG',self.reference/'file.DNG'),
                                       (3,self.source/'file.DNG',self.reference/'file.DNG'),
                                       (4,self.root/'outside.DNG',self.reference/'file.DNG')):
                stamps=[[1,2,3,4,5],[6,7,8,9,10]]
                db.execute('INSERT INTO results VALUES (?,?)',(json.dumps([version,str(left),str(right)]),
                           json.dumps({'raw':{'status':'equal'},'stamps':stamps})))
        output=report/'export.tsv'
        args=(cache,output,'--left-root',self.source,'--right-root',self.reference)
        result=self.command('export_verified.py',*args)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(output.read_text().splitlines()),1)
        before=output.read_bytes();retry=self.command('export_verified.py',*args)
        self.assertNotEqual(retry.returncode,0)
        self.assertEqual(output.read_bytes(),before)

    def test_export_does_not_remove_foreign_partial(self):
        report=self.root/'reports';report.mkdir();cache=report/'cache.sqlite3'
        with sqlite3.connect(cache) as db:db.execute('CREATE TABLE results(key TEXT, result TEXT)')
        output=report/'export.tsv';partial=report/'export.tsv.partial';partial.write_bytes(b'other owner')
        result=self.command('export_verified.py',cache,output,'--left-root',self.source,'--right-root',self.reference)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(partial.read_bytes(),b'other owner')
        self.assertFalse(output.exists())

    def test_benchmark_never_signals_without_explicit_pause(self):
        with patch.object(benchmark.os,'kill') as kill:
            self.assertEqual(benchmark.freeze(None),set())
            with self.assertRaisesRegex(ValueError,'requires --pause-live-job'):benchmark.freeze(987654)
            kill.assert_not_called()

    def test_benchmark_rejects_foreign_uid_without_signal(self):
        with patch.object(benchmark.subprocess,'check_output',return_value=f'{os.getuid()+1} python {PROJECT}/preserve_years.py'), \
             patch.object(benchmark.os,'kill') as kill:
            with self.assertRaisesRegex(ValueError,'current user'):benchmark.freeze(987654,True)
            kill.assert_not_called()

    def test_benchmark_rejects_unrelated_same_user_command(self):
        with patch.object(benchmark.subprocess,'check_output',return_value=f'{os.getuid()} /bin/sleep {PROJECT}/preserve_years.py'), \
             patch.object(benchmark.os,'kill') as kill:
            with self.assertRaisesRegex(ValueError,'this project'):benchmark.freeze(987654,True)
            kill.assert_not_called()

    def test_benchmark_freeze_failure_restores_owned_parent(self):
        with patch.object(benchmark,'checked_owner',return_value='owned'), \
             patch.object(benchmark,'descendants',return_value={987654,987655}), \
             patch.object(benchmark.subprocess,'check_output',return_value=str(os.getuid()+1)), \
             patch.object(benchmark.os,'kill') as kill:
            with self.assertRaisesRegex(ValueError,'different owner'):benchmark.freeze(987654,True)
            self.assertEqual(kill.call_args_list[0].args,(987654,signal.SIGSTOP))
            self.assertEqual(kill.call_args_list[-1].args,(987654,signal.SIGCONT))


if __name__=='__main__':unittest.main()

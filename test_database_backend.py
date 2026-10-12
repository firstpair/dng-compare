import json
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import os
import sys

import database_backend as database


class TursoDatabase(unittest.TestCase):
    def test_mvcc_snapshot_typed_values_checkpoint_and_migration(self):
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{'DNG_DATABASE_BACKEND':'turso'}):
            path=pathlib.Path(td)/'checkpoint.sqlite3'
            old=sqlite3.connect(path);old.execute('CREATE TABLE vals(id INTEGER PRIMARY KEY, value BLOB, stamp INTEGER)')
            old.execute('INSERT INTO vals VALUES (?,?,?)',(1,b'legacy',1));old.commit();old.close()
            first=database.connect(path)
            self.assertTrue(path.with_name(path.name+'.before-turso').exists())
            self.assertEqual(first.execute('PRAGMA journal_mode').fetchone()[0],'mvcc')
            second=database.connect(path)
            first.execute('BEGIN CONCURRENT')
            self.assertEqual(first.execute('SELECT count(*) FROM vals').fetchone()[0],1)
            second.execute('INSERT INTO vals VALUES (?,?,?)',(2,b'\x00\xffblob',1726000000000000001));second.commit()
            self.assertEqual(first.execute('SELECT count(*) FROM vals').fetchone()[0],1)
            first.rollback()
            self.assertEqual(first.execute('SELECT count(*) FROM vals').fetchone()[0],2)
            self.assertEqual(first.execute('SELECT value,stamp FROM vals WHERE id=2').fetchone(),(b'\x00\xffblob',1726000000000000001))
            second.close();first.close()
            self.assertIsNone(json.loads(path.with_name(path.name+'.engine.json').read_text())['active_pid'])
            # A fresh process must be able to consume the fully closed checkpoint.
            command=[sys.executable,'-c',
                     'import database_backend as d,sys; c=d.connect(sys.argv[1]); assert c.execute("select count(*) from vals").fetchone()[0]==2; c.close()',str(path)]
            subprocess.run(command,check=True,capture_output=True)
            portable=sqlite3.connect('file:'+str(path)+'?mode=ro&immutable=1',uri=True)
            self.assertEqual(portable.execute('SELECT count(*) FROM vals').fetchone()[0],2)
            self.assertEqual(portable.execute('PRAGMA integrity_check').fetchone()[0],'ok');portable.close()


if __name__=='__main__':unittest.main()

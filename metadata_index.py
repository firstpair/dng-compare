#!/usr/bin/env python3
"""Portable, incremental SQLite classification of Apo -> Bumer metadata changes."""
import argparse, collections, gzip, hashlib, json, pathlib, zlib, multiprocessing
from concurrent.futures import ProcessPoolExecutor
import database_backend as sqlite3

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS batches(name TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', extension TEXT NOT NULL, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE, compared INTEGER NOT NULL, errors INTEGER NOT NULL, UNIQUE(path,version));
CREATE TABLE IF NOT EXISTS fields(id INTEGER PRIMARY KEY, tag TEXT UNIQUE NOT NULL, category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vals(id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, json_zlib BLOB NOT NULL, json_bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS differences(file_id INTEGER REFERENCES files(id) ON DELETE CASCADE, field_id INTEGER REFERENCES fields(id), kind TEXT NOT NULL, apo_value INTEGER REFERENCES vals(id), bumer_value INTEGER REFERENCES vals(id), PRIMARY KEY(file_id,field_id));
CREATE INDEX IF NOT EXISTS differences_field_kind ON differences(field_id,kind,file_id);
CREATE INDEX IF NOT EXISTS differences_values ON differences(field_id,apo_value,bumer_value);
CREATE INDEX IF NOT EXISTS files_extension ON files(extension,id);
CREATE TABLE IF NOT EXISTS coverage(batch TEXT REFERENCES batches(name) ON DELETE CASCADE, field_id INTEGER REFERENCES fields(id), extension TEXT, apo_present INTEGER, bumer_present INTEGER, PRIMARY KEY(batch,field_id,extension));
CREATE VIEW IF NOT EXISTS field_coverage AS SELECT t.tag,t.category,c.extension,SUM(c.apo_present) apo_present,SUM(c.bumer_present) bumer_present FROM coverage c JOIN fields t ON t.id=c.field_id GROUP BY c.field_id,c.extension;
CREATE VIEW IF NOT EXISTS change_counts AS SELECT t.tag,t.category,d.kind,f.extension,COUNT(*) affected_files FROM differences d JOIN fields t ON t.id=d.field_id JOIN files f ON f.id=d.file_id GROUP BY d.field_id,d.kind,f.extension;
CREATE VIEW IF NOT EXISTS field_changes AS SELECT t.tag,t.category,d.kind,COUNT(*) affected_files,(SELECT COUNT(*) FROM files WHERE compared=1) compared_files,ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM files WHERE compared=1),4) percent_compared FROM differences d JOIN fields t ON t.id=d.field_id JOIN files f ON f.id=d.file_id WHERE f.compared=1 GROUP BY d.field_id,d.kind;
'''

def read_rows(path):
    # Batches are bounded; decompress in bulk to avoid thousands of interpreter
    # lock handoffs while byte-verification threads are active. gzip.decompress
    # still verifies the complete stream, including its CRC and length trailer.
    raw=gzip.decompress(pathlib.Path(path).read_bytes())
    return [json.loads(line) for line in raw.splitlines()]

MIGRATE_VERSION = '''
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE files_versioned(id INTEGER PRIMARY KEY, path TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', extension TEXT NOT NULL, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE, compared INTEGER NOT NULL, errors INTEGER NOT NULL, UNIQUE(path,version));
INSERT INTO files_versioned(id,path,version,extension,batch,compared,errors) SELECT id,path,'',extension,batch,compared,errors FROM files;
DROP VIEW IF EXISTS field_coverage;
DROP VIEW IF EXISTS change_counts;
DROP VIEW IF EXISTS field_changes;
DROP INDEX IF EXISTS files_extension;
DROP TABLE files;
ALTER TABLE files_versioned RENAME TO files;
COMMIT;
PRAGMA foreign_keys=ON;
'''

def open_index(archive):
    db=sqlite3.connect(pathlib.Path(archive)/'metadata/differences.sqlite3',timeout=60)
    # Indexes made before file versions keep their rows, as version ''.
    columns=[row[1] for row in db.execute('PRAGMA table_info(files)')]
    if columns and 'version' not in columns:db.executescript(MIGRATE_VERSION)
    db.executescript(SCHEMA)
    return db

_worker_index=None

def _index_worker_open(archive):
    global _worker_index
    _worker_index=open_index(archive)

def _index_worker_ingest(path):
    ingest(_worker_index,pathlib.Path(path))

def _index_worker_close():
    global _worker_index
    _worker_index.close()
    _worker_index=None

class ProcessIndex:
    """One database owner outside the verification process's interpreter lock."""
    def __init__(self,archive):
        self.closed=False
        self.pool=ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn'),
                                      initializer=_index_worker_open,initargs=(str(archive),))
    def ingest(self,path):
        # Pass only the atomic gzip pathname; the owner verifies and reads it.
        self.pool.submit(_index_worker_ingest,str(path)).result()
    def close(self):
        if self.closed:return
        try:self.pool.submit(_index_worker_close).result()
        finally:
            self.pool.shutdown(wait=True)
            self.closed=True

def ingest(db,path,rows=None):
    if isinstance(db,ProcessIndex):return db.ingest(path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if db.execute('SELECT sha256 FROM batches WHERE name=?',(path.name,)).fetchone()==(digest,):return
    if rows is None:
        rows=read_rows(path)
    fields={};values={};coverage=collections.Counter()
    def field(tag,category):
        if tag not in fields:
            db.execute('INSERT OR IGNORE INTO fields(tag,category) VALUES (?,?)',(tag,category))
            fields[tag]=db.execute('SELECT id FROM fields WHERE tag=?',(tag,)).fetchone()[0]
        return fields[tag]
    def value(v):
        raw=json.dumps(v,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()
        h=hashlib.sha256(raw).hexdigest()
        if h not in values:
            db.execute('INSERT OR IGNORE INTO vals(sha256,json_zlib,json_bytes) VALUES (?,?,?)',(h,zlib.compress(raw,9),len(raw)))
            values[h]=db.execute('SELECT id FROM vals WHERE sha256=?',(h,)).fetchone()[0]
        return values[h]
    with db:
        db.execute('DELETE FROM batches WHERE name=?',(path.name,))
        db.execute('INSERT INTO batches VALUES (?,?)',(path.name,digest))
        for r in rows:
            if r.get('ignored_system_file') or pathlib.Path(r['apo']).name=='.DS_Store':continue
            ext=pathlib.Path(r['relative_path']).suffix.lower()
            compared=int(bool(r['bumer']) and not r['errors'])
            # A version tells apart later contents of the same relative path,
            # such as a shuttle disk refilled after a wipe; plain rows use ''.
            fid=db.execute('INSERT INTO files(path,version,extension,batch,compared,errors) VALUES (?,?,?,?,?,?)',(r['relative_path'],r.get('version',''),ext,path.name,compared,len(r['errors']))).lastrowid
            if not compared:continue
            for side in ('apo','bumer'):
                for tag in r[side+'_metadata']:
                    if tag=='SourceFile' or tag.startswith(('ExifTool:','Composite:')):continue
                    tid=field(tag,'filesystem' if tag.startswith('File:') else 'embedded')
                    coverage[tid,ext,side]+=1
            for d in r['differences']:
                db.execute('INSERT INTO differences VALUES (?,?,?,?,?)',(fid,field(d['tag'],d['category']),d['kind'],value(d['apo']) if 'apo' in d else None,value(d['bumer']) if 'bumer' in d else None))
        for tid,ext in {(tid,ext) for tid,ext,side in coverage}:
            db.execute('INSERT INTO coverage VALUES (?,?,?,?,?)',(path.name,tid,ext,coverage[tid,ext,'apo'],coverage[tid,ext,'bumer']))

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('archive',type=pathlib.Path);a=ap.parse_args()
    db=open_index(a.archive)
    for p in sorted((a.archive/'metadata/batches').glob('*.jsonl.gz')):ingest(db,p)
    print(json.dumps(dict(indexed_files=db.execute('SELECT COUNT(*) FROM files').fetchone()[0],differences=db.execute('SELECT COUNT(*) FROM differences').fetchone()[0],integrity=db.execute('PRAGMA integrity_check').fetchone()[0])))
    db.close()

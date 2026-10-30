"""Compare two metadata indexes row for row, ignoring rowids: the one
metadata_index.py built and the one dng-archive built from the same batches."""
import sqlite3, sys, zlib

def content(path):
    db = sqlite3.connect(path)
    q = lambda sql: sorted(db.execute(sql).fetchall())
    vals = {i: (h, n, zlib.decompress(z)) for i, h, z, n in db.execute('SELECT id,sha256,json_zlib,json_bytes FROM vals')}
    out = dict(
        batches=q('SELECT name,sha256 FROM batches'),
        files=q('SELECT path,version,extension,batch,compared,errors FROM files'),
        fields=q('SELECT tag,category FROM fields'),
        vals=sorted(vals.values()),
        differences=sorted((p, v, t, k, vals[a][0] if a else None, vals[b][0] if b else None) for p, v, t, k, a, b in db.execute(
            'SELECT f.path,f.version,t.tag,d.kind,d.apo_value,d.bumer_value FROM differences d JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id')),
        coverage=q('SELECT c.batch,t.tag,c.extension,c.apo_present,c.bumer_present FROM coverage c JOIN fields t ON t.id=c.field_id'),
        views=q("SELECT name,sql FROM sqlite_master WHERE type IN ('view','index','table')"))
    return out

a, b = content(sys.argv[1]), content(sys.argv[2])
bad = [k for k in a if a[k] != b[k]]
for k in a:
    print('%-12s %8d rows  %s' % (k, len(a[k]), 'same' if a[k] == b[k] else 'DIFFERENT'))
if bad:
    k = bad[0]; x, y = set(a[k]), set(b[k])
    print('only in first:', list(x - y)[:3]); print('only in second:', list(y - x)[:3])
sys.exit(1 if bad else 0)

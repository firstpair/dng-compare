#!/usr/bin/env python3
"""Categorize DNG sample changes using the original comparison implementation."""
import argparse, collections, concurrent.futures, json, os, pathlib, time
import database_backend as sqlite3
from compare_dng import compare_pixels
from preserve_apo import identity, atomic_json, Budget

def compare_record(rec, cached, budget):
    source=pathlib.Path(rec['source']);reference=pathlib.Path(rec['reference']) if rec['reference'] else None
    if identity(source)!=rec['source_identity'] or (reference and identity(reference)!=rec['reference_identity']):
        raise ValueError('Image input changed: '+rec['relative_path'])
    if cached is not None:return cached
    if not reference:
        result=dict(raw=dict(status='missing_reference'),auxiliary=dict(status='missing_reference'),method='exact_original_preserved')
    elif rec['whole_file_equal']:
        result=dict(raw=dict(status='equal'),auxiliary=dict(status='equal'),method='verified_whole_file_sha256')
    else:
        budget.charge(rec['source_identity'][1]+rec['reference_identity'][1])
        result=compare_pixels((str(source),str(reference)))
    result.update(relative_path=rec['relative_path'],source=str(source),reference=str(reference) if reference else None)
    return result

def ordered_results(recovery, cache, workers, budget):
    # Workers only read photos. All SQL access stays in this coordinator.
    pending=collections.deque()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for rel,raw in recovery.execute('SELECT path,record FROM files ORDER BY path'):
            if pathlib.Path(rel).suffix.lower()!='.dng':continue
            rec=json.loads(raw)
            inputs=json.dumps([rec['source_identity'],rec['reference_identity'],rec['source_sha256'],rec['reference_sha256']])
            cached=cache.execute('SELECT inputs,result FROM results WHERE path=?',(rel,)).fetchone()
            saved=json.loads(cached[1]) if cached and cached[0]==inputs else None
            pending.append((rel,inputs,saved is None,pool.submit(compare_record,rec,saved,budget)))
            if len(pending)>=workers*2:
                path,stamp,write,future=pending.popleft()
                yield path,stamp,write,future.result()
        while pending:
            path,stamp,write,future=pending.popleft()
            yield path,stamp,write,future.result()

def categorize(archive, workers=None, rate_mib=None):
    workers=workers or int(os.environ.get('DNG_IMAGE_WORKERS','18'))
    rate_mib=rate_mib or float(os.environ.get('DNG_IMAGE_RATE_MIB','1024'))
    if not 1<=workers<=36 or rate_mib<=0:raise ValueError('Image workers must be 1..36 and rate positive')
    budget=Budget(rate_mib)
    byte_summary=json.loads((archive/'summary.json').read_text())
    if not byte_summary.get('byte_preservation_complete'):raise ValueError('Byte preservation must finish before image classification')
    recovery_path=archive/'recovery.sqlite3'
    wal=recovery_path.with_name(recovery_path.name+'-wal')
    if wal.exists() and wal.stat().st_size:raise ValueError('Recovery checkpoint has uncheckpointed WAL data')
    # The writer is closed and its WAL is checkpointed. Avoid recreating journal
    # files while reading this completed checkpoint with a different SQLite build.
    recovery=sqlite3.connect('file:'+str(recovery_path)+'?mode=ro&immutable=1',uri=True)
    output=archive/'image-comparison';output.mkdir(exist_ok=True)
    cache=sqlite3.connect(output/'cache.sqlite3')
    cache.execute('CREATE TABLE IF NOT EXISTS results(path TEXT PRIMARY KEY, inputs TEXT NOT NULL, result TEXT NOT NULL)')
    counts=collections.Counter();total=0;start=time.monotonic()
    with (output/'pairs.jsonl.tmp').open('w') as stream:
        for rel,inputs,write,result in ordered_results(recovery,cache,workers,budget):
            total+=1
            if write:
                cache.execute('INSERT OR REPLACE INTO results VALUES (?,?,?)',(rel,inputs,json.dumps(result)));cache.commit()
            stream.write(json.dumps(result)+'\n')
            for category in ['raw','auxiliary']:counts[category+'_'+result.get(category,{}).get('status','unverified')]+=1
            if total%100==0:
                atomic_json(output/'summary.json',dict(complete=False,dng_files=total,counts=dict(counts),workers=workers,rate_mib=rate_mib,elapsed_seconds=time.monotonic()-start))
                print('Image categories',archive,total,dict(counts),flush=True)
    (output/'pairs.jsonl.tmp').replace(output/'pairs.jsonl')
    atomic_json(output/'summary.json',dict(complete=True,dng_files=total,counts=dict(counts),workers=workers,rate_mib=rate_mib,elapsed_seconds=time.monotonic()-start,
        method='Original dng-compare: compare encoded image blocks plus decoding parameters, decode differing encodings; failures remain unverified. SHA-256-identical originals prove all byte content equal.',
        note='Raw sensor samples and auxiliary previews are separate from rendering/edit metadata. Exact byte recovery is independently verified.'))
    cache.close();recovery.close()
    print('Image categorization complete',archive,total,dict(counts),flush=True)

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('archive',type=pathlib.Path);args=ap.parse_args();categorize(args.archive)

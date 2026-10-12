"""Bounded independent ExifTool/compression shards; coordinator indexes results."""
import gzip
import json
import multiprocessing
import os
import pathlib
from concurrent.futures import ProcessPoolExecutor

import portable_metadata as metadata


def extract_batch(chunk, dest, backend, workers):
    if backend:os.environ['DNG_METADATA_BACKEND']=backend
    os.environ['DNG_METADATA_WORKERS']=str(workers)
    paths=[]
    for r in chunk:
        if metadata.stamp(pathlib.Path(r['apo']))!=r['apo_stamp']:raise ValueError('Source changed before shard extraction')
        if pathlib.Path(r['apo']).name=='.DS_Store':continue
        paths.append(r['apo'])
        if r['bumer']:
            if metadata.stamp(pathlib.Path(r['bumer']))!=r['bumer_stamp']:raise ValueError('Reference changed before shard extraction')
            paths.append(r['bumer'])
    tags,stderr=metadata.extract(paths) if paths else ({},'')
    rows=[]
    for r in chunk:
        if pathlib.Path(r['apo']).name=='.DS_Store':
            rows.append(dict(**r,ignored_system_file=True,apo_metadata={},bumer_metadata={},differences=[],errors=[],warnings=[]))
            continue
        a=tags.get(r['apo'],{});b=tags.get(r['bumer'],{}) if r['bumer'] else {};errors=[];warnings=[]
        for side,vals in [('apo',a)]+([('bumer',b)] if r['bumer'] else []):
            if not vals:errors.append(dict(side=side,error='ExifTool returned no record'))
            for k,v in vals.items():
                if k.rsplit(':',1)[-1]=='Error':errors.append(dict(side=side,error=v))
                if k.rsplit(':',1)[-1]=='Warning':warnings.append(dict(side=side,warning=v))
        changes=metadata.compare(a,b) if r['bumer'] else []
        for change in changes:change['category']='filesystem' if change['tag'].startswith('File:') else 'embedded'
        if metadata.stamp(pathlib.Path(r['apo']))!=r['apo_stamp']:raise ValueError('Source changed during shard extraction')
        if r['bumer'] and metadata.stamp(pathlib.Path(r['bumer']))!=r['bumer_stamp']:raise ValueError('Reference changed during shard extraction')
        rows.append(dict(**r,apo_metadata=a,bumer_metadata=b,differences=changes,errors=errors,warnings=warnings))
    dest=pathlib.Path(dest);temp=dest.with_suffix('.tmp')
    with gzip.open(temp,'wt',encoding='utf8',compresslevel=1) as f:
        for row in rows:f.write(json.dumps(row,ensure_ascii=True)+'\n')
    with temp.open('rb') as f:os.fsync(f.fileno())
    with gzip.open(temp,'rt',encoding='utf8') as f:
        if [json.loads(line) for line in f]!=rows:raise ValueError('Shard metadata read-back failed')
    temp.replace(dest)
    return str(dest)


def schedule(inventory, batch_size, batches, shards, wait_verified=None):
    total=(len(inventory)+batch_size-1)//batch_size
    backend=os.environ.get('DNG_METADATA_BACKEND')
    total_workers=int(os.environ.get('DNG_METADATA_WORKERS',str(shards)))
    if not 1<=shards<=min(16,total_workers):raise ValueError('Metadata shards must be 1..min(16, metadata workers)')
    # Allocate exactly the configured total native workers across shards.
    allocations=[total_workers//shards+int(i<total_workers%shards) for i in range(shards)]
    pending={}
    with ProcessPoolExecutor(max_workers=shards,mp_context=multiprocessing.get_context('spawn')) as pool:
        def submit(number):
            if number>=total:return
            index=number*batch_size;chunk=inventory[index:index+batch_size]
            if wait_verified:wait_verified([r['relative_path'] for r in chunk])
            dest=batches/(f'{number:06}.jsonl.gz')
            pending[number]=None if dest.exists() else pool.submit(extract_batch,chunk,str(dest),backend,allocations[number%shards])
        for number in range(min(shards,total)):submit(number)
        for number in range(total):
            future=pending.pop(number)
            if future is not None:future.result()
            yield number,number*batch_size
            submit(number+shards)

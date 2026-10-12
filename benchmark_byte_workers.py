#!/usr/bin/env python3
"""Matched full byte-preservation trials; source trees remain read-only."""
import argparse,concurrent.futures,json,os,pathlib,random,signal,sqlite3,statistics,subprocess,tempfile,time
import preserve_apo as p
from benchmark_metadata_workers import freeze,thaw,year_path,validate_pause_options
ROOT=pathlib.Path(__file__).resolve().parent

def main():
    ap=argparse.ArgumentParser();ap.add_argument('archive',type=pathlib.Path);ap.add_argument('--live-pid',type=int);ap.add_argument('--pause-live-job',action='store_true');ap.add_argument('--year',required=True,help='Archive year as LABEL/YYYY');args=ap.parse_args();validate_pause_options(ap,args)
    db=sqlite3.connect('file:'+str(year_path(args.archive,args.year)/'recovery.sqlite3')+'?mode=ro',uri=True)
    scope=json.loads((year_path(args.archive,args.year)/'summary.json').read_text())
    p.validate_output(pathlib.Path(scope['source']),pathlib.Path(scope['reference']),args.archive)
    sample=[];counts={True:0,False:0}
    for text, in db.execute('SELECT record FROM files ORDER BY path'):
        r=json.loads(text);eq=r['whole_file_equal']
        if r['reference'] and r['source_identity'][1]>10_000_000 and counts[eq]<12:
            sample.append(r);counts[eq]+=1
        if all(n==12 for n in counts.values()):break
    db.close()
    if len(sample)!=24:raise ValueError('Need 12 equal and 12 changed files')
    for r in sample:
        if p.identity(pathlib.Path(r['source']))!=r['source_identity'] or p.identity(pathlib.Path(r['reference']))!=r['reference_identity']:raise ValueError('Sample identity changed')
    bins={'rust':ROOT/'rust-scanner/target/release/byte-verify','oxcaml':ROOT/'byte-verify-oxcaml'}
    report=dict(complete=False,scope='24 real source/reference pairs, 12 equal and 12 differing; warm-cache trials; aggregate rate uncapped for measurement',inputs=sample,binaries={k:dict(path=str(v),sha256=p.digest(v)) for k,v in bins.items()},trials=[])
    output=args.archive/'byte-worker-benchmark.json';p.atomic_json(output,report)
    frozen=freeze(args.live_pid,args.pause_live_job)
    try:
        def trial(backend,workers):
            os.environ['DNG_BYTE_BACKEND']=str(bins[backend]);budget=p.Budget(1000000)
            with tempfile.TemporaryDirectory(prefix='byte-worker-benchmark-',dir=str(args.archive)) as td:
                arc=pathlib.Path(td)
                def one(r):
                    got=p.preserve_file(pathlib.Path(r['source']),pathlib.Path(r['reference']),arc,r['relative_path'],budget,delta_mode=True)
                    for key in ['source_sha256','reference_sha256','whole_file_equal']:
                        if got[key]!=r[key]:raise ValueError('Parity mismatch: '+key)
                    if p.reconstruct(got,arc)!=r['source_sha256']:raise ValueError('Independent restore mismatch')
                    return got
                start=time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:results=list(pool.map(one,sample))
                elapsed=time.monotonic()-start
            item=dict(backend=backend,workers=workers,seconds=elapsed,files=len(results),parity=True,source_bytes=sum(r['source_identity'][1] for r in sample))
            report['trials'].append(item);p.atomic_json(output,report);print(json.dumps(item),flush=True)
        jobs=[(b,w) for b in bins for w in [1,2,4,8,12,18,24,36]];random.Random(42).shuffle(jobs)
        for b,w in jobs:trial(b,w)
        for b in bins:
            best=sorted([t for t in report['trials'] if t['backend']==b],key=lambda t:t['seconds'])[:2]
            for item in best:
                for _ in range(2):trial(b,item['workers'])
        report['fastest_measured']={b:min((dict(workers=w,median_seconds=statistics.median(t['seconds'] for t in report['trials'] if t['backend']==b and t['workers']==w)) for w in [1,2,4,8,12,18,24,36] if sum(t['backend']==b and t['workers']==w for t in report['trials'])>=3),key=lambda x:x['median_seconds']) for b in bins}
        report['complete']=True;p.atomic_json(output,report)
    finally:
        thaw(frozen)
if __name__=='__main__':main()

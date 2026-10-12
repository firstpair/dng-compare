#!/usr/bin/env python3
"""Sample worker-pool and child-process RSS on the existing parity benchmark."""
import argparse,json,pathlib,subprocess,tempfile,time
from benchmark_metadata_workers import freeze,thaw,stable_hash,validate_pause_options,PROJECT
from portable_metadata import atomic_json

def rss_tree(pid):
    rows=[x.split(None,3) for x in subprocess.check_output(['ps','-axo','pid,ppid,rss,stat'],text=True).splitlines()[1:]]
    ids={pid}
    for _ in range(4):ids.update(int(r[0]) for r in rows if int(r[1]) in ids)
    selected=[r for r in rows if int(r[0]) in ids]
    return sum(int(r[2])*1024 for r in selected if int(r[0])==pid),sum(int(r[2])*1024 for r in selected),sum('Z' not in r[3] and int(r[0])!=pid for r in selected)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('archive',type=pathlib.Path);ap.add_argument('--live-pid',type=int);ap.add_argument('--pause-live-job',action='store_true')
    args=ap.parse_args();validate_pause_options(ap,args);timing=json.loads((args.archive/'metadata-worker-benchmark.json').read_text())
    from preserve_apo import validate_output
    validate_output(pathlib.Path(timing['source']),pathlib.Path(timing['reference']),args.archive)
    paths=timing['paths'];expected=timing['trials'][0]['stable_metadata_sha256']
    backends={'rust':PROJECT/'rust-scanner/target/release/metadata-extract','oxcaml':PROJECT/'metadata-extract-oxcaml'}
    report={'schema':'metadata-memory-benchmark-v1','workers':36,'paths':paths,'trials':[],'complete':False,
        'method':'ps RSS sampled about every 50 ms. Pool RSS excludes child ExifTool processes. Tree RSS sums pool and live descendants and therefore can count shared pages more than once. Python caller memory is excluded. Child stdout is streamed to a temporary file for measurement, then full typed metadata parity is checked. Live production tree is stopped only during each trial and resumed in finally.'}
    dest=args.archive/'metadata-memory-benchmark.json'
    for repeat in range(2):
        for name,backend in backends.items():
            frozen=freeze(args.live_pid,args.pause_live_job);pool_peak=tree_peak=children_peak=0
            try:
                with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
                    start=time.monotonic()
                    proc=subprocess.Popen([str(backend),'36'],stdin=subprocess.PIPE,stdout=output,stderr=errors)
                    payload=('\0'.join(paths)+'\0').encode() if name=='oxcaml' else json.dumps(paths).encode()
                    proc.stdin.write(payload);proc.stdin.close()
                    while proc.poll() is None:
                        pool,tree,children=rss_tree(proc.pid);pool_peak=max(pool_peak,pool);tree_peak=max(tree_peak,tree);children_peak=max(children_peak,children)
                        time.sleep(.05)
                    elapsed=time.monotonic()-start
                    if proc.returncode:errors.seek(0);raise RuntimeError(errors.read().decode())
                    output.seek(0);records=json.load(output)
            finally:thaw(frozen)
            tags={r['SourceFile']:r for r in records}
            if set(tags)!=set(paths) or stable_hash(tags)!=expected:raise ValueError('Memory benchmark parity failed')
            trial=dict(backend=name,repeat=repeat,seconds=elapsed,pool_peak_rss_bytes=pool_peak,tree_peak_rss_bytes=tree_peak,max_child_processes=children_peak,parity=True)
            report['trials'].append(trial);atomic_json(dest,report);print(json.dumps(trial),flush=True)
    report['complete']=True;atomic_json(dest,report)
if __name__=='__main__':main()

#!/usr/bin/env python3
"""Controlled Rust/OxCaml ExifTool worker sweep; full typed-value parity required."""
import argparse,hashlib,json,os,pathlib,random,re,shlex,signal,statistics,subprocess,time
import portable_metadata as metadata

PROJECT=pathlib.Path(__file__).resolve().parent
def descendants(pid):
    rows=[x.split() for x in subprocess.check_output(['ps','-axo','pid,ppid'],text=True).splitlines()[1:]]
    ids={pid}
    for _ in range(6):ids.update(int(r[0]) for r in rows if int(r[1]) in ids)
    return ids
def checked_owner(pid):
    if pid <= 1 or pid == os.getpid():raise ValueError('Invalid live job PID')
    line=subprocess.check_output(['ps','-p',str(pid),'-o','uid=,command='],text=True).strip()
    uid,command=line.split(None,1)
    if int(uid)!=os.getuid():raise ValueError('Live job must belong to the current user')
    args=shlex.split(command)
    allowed={str((PROJECT/name).resolve()) for name in ('preserve_years.py','preserve_apo.py','portable_metadata.py')}
    script=next((argument for argument in args[1:] if not argument.startswith('-')),None)
    if not pathlib.Path(args[0]).name.lower().startswith('python') or '-c' in args or '-m' in args or script is None or str(pathlib.Path(script).resolve()) not in allowed:
        raise ValueError('Live PID must invoke this project preservation or metadata script')
    return line

def validate_pause_options(parser,args):
    if bool(args.live_pid)!=bool(args.pause_live_job):
        parser.error('--live-pid and --pause-live-job must be supplied together; default does not signal any process')

def year_path(archive,name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*/[0-9]{4}',name):
        raise ValueError('Year must be LABEL/YYYY')
    return archive/name

def freeze(pid,pause=False):
    if pid is None:
        if pause:raise ValueError('A live PID is required for explicit pause')
        return set()
    if not pause:raise ValueError('Stopping a live job requires --pause-live-job')
    baseline=checked_owner(pid)
    if checked_owner(pid)!=baseline:raise ValueError('Live PID ownership changed')
    frozen=set()
    try:
        os.kill(pid,signal.SIGSTOP);frozen.add(pid)
        # Stop the confirmed scheduler before its same-user children.
        for _ in range(4):
            new=descendants(pid)-frozen
            if not new:break
            for child in new:
                try:
                    uid=subprocess.check_output(['ps','-p',str(child),'-o','uid='],text=True).strip()
                    if not uid:continue
                    if int(uid)!=os.getuid():raise ValueError('Live descendant has a different owner')
                    os.kill(child,signal.SIGSTOP);frozen.add(child)
                except ProcessLookupError:pass
        return frozen
    except BaseException:
        thaw(frozen)
        raise
def thaw(ids):
    for p in ids:
        try:os.kill(p,signal.SIGCONT)
        except ProcessLookupError:pass
def stable_hash(tags):
    for vals in tags.values():vals.pop('File:System:FileAccessDate',None)
    return hashlib.sha256(json.dumps(tags,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
def main():
    ap=argparse.ArgumentParser();ap.add_argument('archive',type=pathlib.Path);ap.add_argument('--live-pid',type=int);ap.add_argument('--pause-live-job',action='store_true');ap.add_argument('--year',required=True,help='Archive year as LABEL/YYYY');ap.add_argument('--path-contains',default='',help='Optional relative-path substring for an explicit dataset')
    args=ap.parse_args();validate_pause_options(ap,args);root=args.archive;year=year_path(root,args.year)
    inventory=json.loads((year/'metadata/inventory.json').read_text())
    scope=json.loads((year/'summary.json').read_text())
    from preserve_apo import validate_output
    validate_output(pathlib.Path(scope['source']),pathlib.Path(scope['reference']),root)
    selected=[r for r in inventory if args.path_contains in r['relative_path'] and r['apo'].lower().endswith('.dng') and r['bumer']][:18]
    if len(selected)!=18:raise ValueError('Need 18 selected DNG source/reference pairs')
    paths=[p for r in selected for p in (r['apo'],r['bumer'])]
    stamps={p:metadata.stamp(pathlib.Path(p)) for p in paths}
    backends={'rust':PROJECT/'rust-scanner/target/release/metadata-extract','oxcaml':PROJECT/'metadata-extract-oxcaml'}
    report={'schema':'metadata-worker-benchmark-v1','source':scope['source'],'reference':scope['reference'],'paths':paths,'pairs':selected,'workers_tested':[1,2,4,8,12,18,24,36],
        'compiler_oxcaml':subprocess.check_output([os.environ.get('OXCAMLOPT','ocamlopt'),'-version'],text=True).strip(),
        'rustc':subprocess.check_output(['rustc','--version'],text=True).strip(),
        'cpu':subprocess.check_output(['sysctl','-n','machdep.cpu.brand_string','hw.physicalcpu','hw.logicalcpu'],text=True).splitlines(),
        'exiftool_version':subprocess.check_output(['exiftool','-ver'],text=True).strip(),
        'backend_sha256':{k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in backends.items()},
        'method':'36 distinct DNG paths from the explicitly selected archive year; same flags, complete JSON values, warm filesystem cache after first trial. Live archive process tree stopped only during each timed extraction and resumed in finally. Timing includes process startup and Python JSON parsing; canonical equality excludes filesystem access date only. First sweep, then repeat the two fastest worker counts twice for each backend.',
        'trials':[],'complete':False}
    dest=root/'metadata-worker-benchmark.json';expected=None
    def trial(name,workers,phase):
        nonlocal expected
        os.environ['DNG_METADATA_BACKEND']=str(backends[name]);os.environ['DNG_METADATA_WORKERS']=str(workers)
        frozen=freeze(args.live_pid,args.pause_live_job)
        try:
            start=time.monotonic();tags,stderr=metadata.extract(paths);elapsed=time.monotonic()-start
        finally:thaw(frozen)
        if set(tags)!=set(paths):raise ValueError('Incomplete benchmark output')
        digest=stable_hash(tags)
        if expected is None:expected=digest
        if digest!=expected:raise ValueError('Full typed metadata parity failed')
        if any(metadata.stamp(pathlib.Path(p))!=s for p,s in stamps.items()):raise ValueError('Benchmark input changed')
        entry=dict(backend=name,workers=workers,phase=phase,seconds=elapsed,files_per_second=len(paths)/elapsed,stable_metadata_sha256=digest,
                   fields=sum(len(t) for t in tags.values()),stderr_bytes=len(stderr.encode()),parity=True)
        report['trials'].append(entry);metadata.atomic_json(dest,report);print(json.dumps(entry),flush=True)
    # Start at 18 to warm inputs quickly, then interleave implementations and randomize counts.
    trial('rust',18,'warmup');trial('oxcaml',18,'warmup')
    counts=list(report['workers_tested']);random.Random(20261010).shuffle(counts)
    for workers in counts:
        for name in backends:trial(name,workers,'sweep')
    best={}
    for name in backends:
        ranked=sorted((t for t in report['trials'] if t['backend']==name and t['phase']=='sweep'),key=lambda t:t['seconds'])
        finalists=[t['workers'] for t in ranked[:2]]
        for repeat in range(2):
            for workers in finalists:trial(name,workers,'finalist-repeat')
        medians={w:statistics.median(t['seconds'] for t in report['trials'] if t['backend']==name and t['workers']==w and t['phase']!='warmup') for w in finalists}
        best[name]={'workers':min(medians,key=medians.get),'median_seconds':min(medians.values()),'finalist_medians_seconds':medians}
    report['optimal_measured']=best;report['complete']=True;metadata.atomic_json(dest,report);print(json.dumps(best),flush=True)
if __name__=='__main__':main()

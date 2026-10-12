#!/usr/bin/env python3
"""Resume exact, year-scoped preservation using the established Apo tools."""
import argparse, collections, hashlib, json, math, os, pathlib, re, shutil, subprocess, sys, time
import preserve_apo as preservation
import portable_metadata as metadata

PROJECT=pathlib.Path(__file__).resolve().parent

def git_state():
    try:
        return dict(commit=subprocess.check_output(['git','-C',str(PROJECT),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip(),
                    working_tree=subprocess.check_output(['git','-C',str(PROJECT),'status','--porcelain'],text=True).splitlines())
    except (OSError,subprocess.CalledProcessError):return dict(available=False)

def walk(root):
    if not root.is_dir():raise ValueError('Missing root: '+str(root))
    for d,ds,ns in os.walk(root,onerror=lambda e:(_ for _ in ()).throw(e)):
        ds.sort();ns.sort()
        for n in ns:
            p=pathlib.Path(d,n)
            if p.is_symlink() or not p.is_file():raise ValueError('Unsupported file: '+str(p))
            yield p

def mapping(source,reference,archive):
    names=collections.defaultdict(list)
    for p in walk(reference):names[p.name].append(p)
    pairs={};decisions=[]
    for p in walk(source):
        rel=str(p.relative_to(source));same=reference/rel;candidates=names[p.name]
        base=same if same.is_file() else candidates[0] if len(candidates)==1 else None
        if base:pairs[str(p)]=str(base)
        decisions.append(dict(relative_path=rel,source=str(p),reference=str(base) if base else None,
                              method='same_relative_path' if same.is_file() else 'unique_filename' if base else 'original_fallback',
                              candidates=[str(c) for c in candidates]))
    preservation.atomic_json(archive/'matching.json',decisions)
    return pairs

def main():
    ap=argparse.ArgumentParser();ap.add_argument('archive',type=pathlib.Path);ap.add_argument('--rate-mib',type=float,default=200)
    ap.add_argument('--source-root',action='append',required=True,metavar='LABEL=PATH',help='Repeatable labeled source containing YYYY directories')
    ap.add_argument('--reference-root',type=pathlib.Path,required=True,help='Reference tree containing corresponding YYYY directories')
    ap.add_argument('--start-at',help='Resume at VOLUME/YEAR after already validated earlier years')
    ap.add_argument('--metadata-workers',type=int,default=0)
    ap.add_argument('--preservation-workers',type=int,default=1)
    ap.add_argument('--preservation-backend',choices=['python','rust','oxcaml'],default='python')
    ap.add_argument('--metadata-backend',choices=['rust','oxcaml'],default='rust')
    ap.add_argument('--unified-pass',action='store_true',default=True,help='Overlap byte verification and metadata using a shared inventory (default)')
    ap.add_argument('--sequential-pass',action='store_false',dest='unified_pass',help='Run byte verification followed by metadata separately')
    ap.add_argument('--database-backend',choices=['sqlite','turso'],default='sqlite')
    ap.add_argument('--metadata-shards',type=int,default=0,help='Parallel extraction/compression shards (default: up to 4)')
    args=ap.parse_args()
    if not math.isfinite(args.rate_mib) or args.rate_mib<=0:ap.error('rate must be finite and positive')
    sources={}
    for declaration in args.source_root:
        label,separator,path=declaration.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',label):ap.error('source roots require a simple LABEL=PATH')
        if label in sources:ap.error('duplicate source label: '+label)
        sources[label]=pathlib.Path(path).resolve(strict=True)
        if not sources[label].is_dir():ap.error('source root is not a directory: '+path)
    reference_root=args.reference_root.resolve(strict=True)
    for source in sources.values():preservation.validate_output(source,reference_root,args.archive)
    args.metadata_shards=args.metadata_shards or min(4,args.metadata_workers or 1)
    if args.database_backend=='turso':
        import importlib.util
        if importlib.util.find_spec('turso') is None:
            python=PROJECT/'.venv/bin/python'
            if not python.exists():raise ValueError('Install requirements.txt in the project .venv before using Turso')
            os.execv(str(python),[str(python),str(PROJECT/'preserve_years.py'),*sys.argv[1:]])
    if not 1<=args.preservation_workers<=36:ap.error('preservation workers must be 1..36')
    if not 1<=args.metadata_shards<=min(16,args.metadata_workers or 1):
        ap.error('metadata shards must be 1..min(16, metadata workers); use --metadata-shards 1 for serial extraction')
    tasks=[]
    for label,source in sources.items():
        for year in sorted(source.iterdir()):
            if year.is_dir() and not year.is_symlink() and len(year.name)==4 and year.name.isdigit():
                if not (reference_root/year.name).is_dir():ap.error('missing reference year: '+year.name)
                tasks.append((label,year.name))
    if not tasks:ap.error('source roots contain no YYYY directories')
    if args.start_at and args.start_at not in [v+'/'+y for v,y in tasks]:ap.error('unknown start year')
    root=args.archive.resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['DNG_DATABASE_BACKEND']=args.database_backend
    os.environ['DNG_METADATA_SHARDS']=str(args.metadata_shards)
    if args.metadata_workers:
        if not 1<=args.metadata_workers<=36:ap.error('metadata workers must be 1..36')
        os.environ['DNG_METADATA_BACKEND']=str(PROJECT/('metadata-extract-oxcaml' if args.metadata_backend=='oxcaml' else 'rust-scanner/target/release/metadata-extract'))
        os.environ['DNG_METADATA_WORKERS']=str(args.metadata_workers)
    if args.preservation_backend!='python':
        os.environ['DNG_BYTE_BACKEND']=str(PROJECT/('byte-verify-oxcaml' if args.preservation_backend=='oxcaml' else 'rust-scanner/target/release/byte-verify'))
    lock=root/'.running';lock.mkdir()
    try:
        scope=dict(tasks=[list(t) for t in tasks],source_roots={label:str(path) for label,path in sources.items()},
                   included='Direct YYYY directory children only; all non-year siblings excluded.',reference=str(reference_root))
        scope_path=root/'scope.json'
        if scope_path.exists() and json.loads(scope_path.read_text())!=scope:raise ValueError('Archive scope changed; use its archived tools or a new output directory')
        preservation.atomic_json(scope_path,scope)
        tools=root/'tools';tools.mkdir(exist_ok=True)
        for name in ['preserve_apo.py','portable_metadata.py','metadata_index.py','xattr_portable.py','preserve_years.py','finalize_year.py','database_backend.py','metadata_shards.py']:
            if (PROJECT/name).resolve()!=(tools/name).resolve():shutil.copy2(PROJECT/name,tools/name)
        if args.unified_pass:shutil.copy2(PROJECT/'unified_pass.py',tools/'unified_pass.py')
        if args.preservation_backend!='python':
            shutil.copy2(os.environ['DNG_BYTE_BACKEND'],tools/('byte-verify-'+args.preservation_backend))
            for path in ['rust-scanner/src/bin/byte-verify.rs','byte_verify_oxcaml.ml','byte_verify_sha_stubs.c']:
                shutil.copy2(PROJECT/path,tools/pathlib.Path(path).name)
        binary=PROJECT/'rust-scanner/target/release/dng-subset'
        # Archive the actual executable, so XMP extraction is reproducible on this platform.
        if binary.exists():shutil.copy2(binary,tools/'dng-subset')
        if args.metadata_workers:
            shutil.copy2(PROJECT/'rust-scanner/target/release/metadata-extract',tools/'metadata-extract')
            shutil.copy2(PROJECT/'rust-scanner/src/bin/metadata-extract.rs',tools/'metadata-extract.rs')
            if args.metadata_backend=='oxcaml':
                shutil.copy2(PROJECT/'metadata-extract-oxcaml',tools/'metadata-extract-oxcaml')
                shutil.copy2(PROJECT/'metadata_extract_oxcaml.ml',tools/'metadata_extract_oxcaml.ml')
        shutil.copy2(PROJECT/'YEAR-PRESERVATION.md',root/'README.md')
        shutil.copy2(PROJECT/'METADATA-QUERIES.md',root/'METADATA-QUERIES.md')
        provenance=dict(created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),tasks=tasks,
            methodology='dng-compare-year-preservation-v1',exiftool_version=subprocess.check_output(['exiftool','-ver'],text=True).strip(),
            exiftool_args=['-j','-a','-G0:1:4','-n','-struct','-b','-u','-U','-api','LargeFileSupport=1','-all','-DNGPrivateData','-MakerNotes'],
            image_fields_excluded=metadata.EXCLUDE,xdelta_version=subprocess.run(['xdelta3','-V'],capture_output=True,text=True).stderr,
            matching='Same relative path, then unique case-sensitive filename within reference year; ambiguous or missing references preserve complete originals.',
            legacy_schema='apo fields and SQL columns mean the selected source; bumer means reference.',
            git=git_state(),metadata_backend=os.environ.get('DNG_METADATA_BACKEND','serial ExifTool'),metadata_workers=args.metadata_workers or 1,
            metadata_prefetch_batches=int(os.environ.get('DNG_METADATA_PREFETCH_BATCHES','4')) if args.metadata_workers else 1,
            preservation_backend=args.preservation_backend,preservation_workers=args.preservation_workers,rate_mib=args.rate_mib,
            workflow='unified-byte-metadata' if args.unified_pass else 'sequential-byte-metadata',
            database_backend=args.database_backend,database_version='0.8.2' if args.database_backend=='turso' else None,
            database_journal='mvcc' if args.database_backend=='turso' else 'sqlite-default',
            metadata_shards=args.metadata_shards,
            metadata_index_owner='separate-process' if args.unified_pass else 'coordinator',
            python_thread_switch_interval_seconds=sys.getswitchinterval(),
            tools={p.name:preservation.digest(p) for p in tools.iterdir() if p.is_file()})
        preservation.atomic_json(root/'provenance.json',provenance)
        started=not args.start_at
        for volume,year in tasks:
            if not started and volume+'/'+year==args.start_at:started=True
            if not started:
                prior=json.loads((root/volume/year/'verification.json').read_text())
                if not prior.get('comparison_complete') or not prior.get('archive_verified'):raise ValueError('Earlier year not validated: '+volume+'/'+year)
                continue
            source=sources[volume]/year;reference=reference_root/year
            archive=root/volume/year;archive.mkdir(parents=True,exist_ok=True)
            print('START',volume,year,flush=True)
            pairs=mapping(source,reference,archive)
            preservation.load_known=lambda project:{k:pathlib.Path(v) for k,v in pairs.items()}
            metadata.known_pairs=lambda project:pairs
            preservation.atomic_json(archive/'provenance.json',dict(provenance,source=str(source),reference=str(reference)))
            if args.unified_pass:
                from unified_pass import run
                run(source,reference,archive,PROJECT,pairs,args.rate_mib,args.preservation_workers)
            else:
                if not preservation.scan(source,reference,archive,PROJECT,args.rate_mib,delta_mode=True,workers=args.preservation_workers):
                    raise ValueError('Byte preservation incomplete: '+str(archive))
                metadata.snapshot(source,reference,archive,PROJECT,16)
            subprocess.run([str(tools/'dng-subset'),'--archive-xmp',str(source),str(archive/'xmp-original')],check=True)
            subprocess.run([sys.executable,str(PROJECT/'finalize_year.py'),str(archive)],check=True)
            print('DONE',volume,year,flush=True)
        summaries={volume+'/'+year:json.loads((root/volume/year/'verification.json').read_text()) for volume,year in tasks}
        preservation.atomic_json(root/'summary.json',dict(complete=True,years=summaries,all_verified=all(v['verified'] for v in summaries.values())))
    finally:lock.rmdir()

if __name__=='__main__':main()

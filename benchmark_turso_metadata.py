#!/usr/bin/env python3
"""Matched full metadata workflow benchmark; photos and live indexes are untouched."""
import argparse
import collections
import gzip
import hashlib
import json
import os
import pathlib
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import zlib

import portable_metadata as metadata
from benchmark_metadata_workers import freeze, thaw, year_path, validate_pause_options
from benchmark_metadata_memory import rss_tree

PROJECT = pathlib.Path(__file__).resolve().parent
COUNTS = [1, 2, 4, 8, 12, 18, 24, 36]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def validate(archive, inventory):
    """Read portable exports independently and prove every indexed difference."""
    rows = []
    for path in sorted((archive/'metadata/batches').glob('*.jsonl.gz')):
        with gzip.open(path, 'rt') as stream:
            rows.extend(json.loads(line) for line in stream)
    if [r['relative_path'] for r in rows] != [r['relative_path'] for r in inventory]:
        raise ValueError('Snapshot coverage mismatch')
    summary = json.loads((archive/'metadata/summary.json').read_text())
    if not summary['complete'] or summary.get('metadata_errors', 0):
        raise ValueError('Incomplete or failed extraction')
    for expected, row in zip(inventory, rows):
        for key in ('apo', 'bumer', 'apo_stamp', 'bumer_stamp'):
            if row[key] != expected[key]:raise ValueError('Snapshot identity mismatch')
        for side in ('apo', 'bumer'):
            if metadata.stamp(pathlib.Path(expected[side])) != expected[side+'_stamp']:
                raise ValueError('Input identity changed')
    db = sqlite3.connect('file:'+str(archive/'metadata/differences.sqlite3')+'?mode=ro&immutable=1', uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Database integrity failure')
        expected_differences = {}
        expected_coverage = collections.Counter()
        for row in rows:
            for d in row['differences']:
                expected_differences[row['relative_path'], d['tag']] = (
                    d['category'], d['kind'], canonical(d['apo']) if 'apo' in d else None,
                    canonical(d['bumer']) if 'bumer' in d else None)
            for side in ('apo', 'bumer'):
                for tag in row[side+'_metadata']:
                    if tag == 'SourceFile' or tag.startswith(('ExifTool:', 'Composite:')):continue
                    expected_coverage[tag, pathlib.Path(row['relative_path']).suffix.lower(), side] += 1
        got_differences = {}
        for path, tag, category, kind, a, b in db.execute('''
            SELECT f.path,t.tag,t.category,d.kind,a.json_zlib,b.json_zlib
            FROM differences d JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id
            LEFT JOIN vals a ON a.id=d.apo_value LEFT JOIN vals b ON b.id=d.bumer_value'''):
            got_differences[path, tag] = (category, kind, zlib.decompress(a) if a is not None else None,
                                        zlib.decompress(b) if b is not None else None)
        if got_differences != expected_differences:raise ValueError('Typed indexed differences mismatch')
        got_coverage = collections.Counter()
        for tag, extension, a, b in db.execute('SELECT tag,extension,apo_present,bumer_present FROM field_coverage'):
            got_coverage[tag, extension, 'apo'] += a
            got_coverage[tag, extension, 'bumer'] += b
        if got_coverage != expected_coverage:raise ValueError('Indexed field coverage mismatch')
        if db.execute('SELECT count(*) FROM files').fetchone()[0] != len(rows):
            raise ValueError('Indexed file coverage mismatch')
    finally:db.close()
    # Access times change as a consequence of reads; keep them in exports but omit
    # only their metadata tag from cross-implementation equality.
    stable = []
    for row in rows:
        item = {k:row[k] for k in ('relative_path', 'errors', 'warnings')}
        item['differences'] = [d for d in row['differences'] if not d['tag'].endswith(':FileAccessDate')]
        for side in ('apo_metadata', 'bumer_metadata'):
            item[side] = {k:v for k,v in row[side].items() if not k.endswith(':FileAccessDate')}
        stable.append(item)
    return dict(stable_metadata_sha256=hashlib.sha256(canonical(stable)).hexdigest(),
                files=len(rows), fields=sum(len(r[s]) for r in rows for s in ('apo_metadata','bumer_metadata')),
                differences=len(expected_differences), parity=True,
                snapshot_bytes=sum(p.stat().st_size for p in (archive/'metadata/batches').glob('*.jsonl.gz')),
                database_bytes=(archive/'metadata/differences.sqlite3').stat().st_size,
                index_verified=True, engine=json.loads((archive/'metadata/differences.sqlite3.engine.json').read_text()))


def child(args):
    config = json.loads(args.config.read_text())
    backend = PROJECT/('metadata-extract-oxcaml' if args.backend=='oxcaml' else 'rust-scanner/target/release/metadata-extract')
    os.environ.update(DNG_DATABASE_BACKEND='turso', DNG_METADATA_BACKEND=str(backend),
                      DNG_METADATA_WORKERS=str(args.workers), DNG_METADATA_SHARDS=str(min(4,args.workers)))
    metadata.known_pairs = lambda project: {r['apo']:r['bumer'] for r in config['inventory']}
    start = time.monotonic()
    metadata.snapshot(pathlib.Path(config['source']), pathlib.Path(config['reference']), args.output,
                      PROJECT, 4, prepared_inventory=config['inventory'])
    seconds = time.monotonic()-start
    result = dict(seconds=seconds, **validate(args.output, config['inventory']))
    metadata.atomic_json(args.output/'trial-result.json', result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=pathlib.Path, nargs='?')
    parser.add_argument('--live-pid', type=int)
    parser.add_argument('--pause-live-job',action='store_true')
    parser.add_argument('--year',help='Archive year as LABEL/YYYY')
    parser.add_argument('--path-contains',default='',help='Optional relative-path substring')
    parser.add_argument('--trial', action='store_true')
    parser.add_argument('--config', type=pathlib.Path)
    parser.add_argument('--backend', choices=['rust','oxcaml'])
    parser.add_argument('--workers', type=int)
    parser.add_argument('--output', type=pathlib.Path)
    parser.add_argument('--dataset', choices=['leica','fuji'], default='leica')
    args = parser.parse_args()
    if args.trial:return child(args)
    if not args.archive or not args.year:parser.error('archive and --year are required')
    validate_pause_options(parser,args)
    year=year_path(args.archive,args.year)
    inventory = json.loads((year/'metadata/inventory.json').read_text())
    groups = {'.dng':[], '.jpg':[]}
    for row in inventory:
        if args.path_contains not in row['relative_path']:continue
        extension = pathlib.Path(row['relative_path']).suffix.lower()
        if extension in groups and row['bumer'] and len(groups[extension])<8:
            groups[extension].append(row)
        if all(len(g)==8 for g in groups.values()):break
    if any(len(g)!=8 for g in groups.values()):raise ValueError('Need eight DNG and eight JPEG pairs')
    selected = [r for pair in zip(groups['.dng'], groups['.jpg']) for r in pair]
    for row in selected:
        for side in ('apo','bumer'):
            if metadata.stamp(pathlib.Path(row[side])) != row[side+'_stamp']:raise ValueError('Input changed')
    scope=json.loads((year/'summary.json').read_text())
    from preserve_apo import validate_output
    validate_output(pathlib.Path(scope['source']),pathlib.Path(scope['reference']),args.archive)
    config = dict(source=scope['source'], reference=scope['reference'], inventory=selected)
    output = args.archive/('turso-metadata-workflow-benchmark-fuji.json' if args.dataset=='fuji' else 'turso-metadata-workflow-benchmark.json')
    bins = {'rust':PROJECT/'rust-scanner/target/release/metadata-extract', 'oxcaml':PROJECT/'metadata-extract-oxcaml'}
    report = dict(schema='turso-metadata-workflow-benchmark-v1', complete=False, dataset=args.dataset, inputs=selected,
                  database_engine='pyturso 0.8.2 embedded MVCC', workers_tested=COUNTS,
                  backend_sha256={k:hashlib.sha256(v.read_bytes()).hexdigest() for k,v in bins.items()},
                  cpu=subprocess.check_output(['sysctl','-n','machdep.cpu.brand_string','hw.physicalcpu','hw.logicalcpu'],text=True).splitlines(),
                  method='16 real source/reference pairs: 8 DNG and 8 JPEG, full metadata; warm-cache measurements. '
                         'Timed work includes native ExifTool pool, comparison, parallel gzip/readback, full Turso classification ingestion, '
                         'checkpoint and snapshot hashes; each trial uses a fresh archive under the selected output root. Up to four shards, four source files per batch. '
                         'The shared Python coordinator embeds Turso for both backends. Independent snapshot and typed SQL audit is outside timed seconds. '
                         'Only FileAccessDate is excluded from cross-trial equality. Tree RSS sums shared pages and includes validation. '
                         'Live comparison is paused during each trial and always resumed; randomized sweeps and three measurements of each finalist.',
                  trials=[])
    metadata.atomic_json(output, report)
    expected = None
    with tempfile.TemporaryDirectory(prefix='turso-workflow-benchmark-', dir=args.archive) as temp:
        scratch = pathlib.Path(temp)
        config_path = scratch/'inputs.json';metadata.atomic_json(config_path, config)
        def trial(name, workers, phase):
            nonlocal expected
            dest = scratch/f'trial-{len(report["trials"]):03}'
            frozen = freeze(args.live_pid,args.pause_live_job)
            process = None
            root_peak = tree_peak = child_peak = 0
            try:
                with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                    process = subprocess.Popen([sys.executable,str(PROJECT/'benchmark_turso_metadata.py'),'--trial',
                                                '--config',str(config_path),'--backend',name,'--workers',str(workers),'--output',str(dest)],
                                               stdout=stdout,stderr=stderr)
                    while process.poll() is None:
                        root_rss, tree_rss, children = rss_tree(process.pid)
                        root_peak=max(root_peak,root_rss);tree_peak=max(tree_peak,tree_rss);child_peak=max(child_peak,children)
                        time.sleep(.25)
                    if process.returncode:
                        stderr.seek(0);stdout.seek(0)
                        raise RuntimeError(stdout.read().decode()+stderr.read().decode())
            finally:
                if process is not None and process.poll() is None:
                    process.terminate();process.wait()
                thaw(frozen)
            item = json.loads((dest/'trial-result.json').read_text())
            if item['engine']['journal_mode'] != 'mvcc' or item['engine']['active_pid'] is not None:
                raise ValueError('Turso engine or final checkpoint mismatch')
            digest = item['stable_metadata_sha256']
            if expected is None:expected=digest
            if digest != expected:raise ValueError('Complete typed metadata parity failed')
            item.update(backend=name,workers=workers,shards=min(4,workers),phase=phase,
                        files_per_second=item['files']/item['seconds'],coordinator_peak_rss_bytes=root_peak,
                        tree_peak_rss_bytes=tree_peak,max_child_processes=child_peak)
            report['trials'].append(item);metadata.atomic_json(output,report);print(json.dumps(item),flush=True)
            # Trial outputs are scratch copies; retain their verification and input evidence in report.
            import shutil
            shutil.rmtree(dest)
        for name in bins:trial(name,18,'warmup')
        counts=list(COUNTS);random.Random(20261011).shuffle(counts)
        for number,workers in enumerate(counts):
            for name in (list(bins) if number%2==0 else list(reversed(bins))):trial(name,workers,'sweep')
        best={}
        for name in bins:
            ranked=sorted((t for t in report['trials'] if t['backend']==name and t['phase']=='sweep'),key=lambda t:t['seconds'])
            finalists=[t['workers'] for t in ranked[:2]]
            for _ in range(2):
                for workers in finalists:trial(name,workers,'finalist-repeat')
            medians={w:statistics.median(t['seconds'] for t in report['trials'] if t['backend']==name and t['workers']==w and t['phase']!='warmup') for w in finalists}
            best[name]=dict(workers=min(medians,key=medians.get),median_seconds=min(medians.values()),finalist_medians_seconds=medians)
        report.update(complete=True,optimal_measured=best)
        metadata.atomic_json(output,report);print(json.dumps(best),flush=True)


if __name__=='__main__':main()

#!/usr/bin/env python3
"""Systematic stratified pilot before a multi-terabyte preservation decision."""
import argparse,collections,hashlib,json,pathlib,subprocess,time,gzip
from preserve_apo import atomic_json,identity,validate_output
from portable_metadata import extract,compare

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('archive',type=pathlib.Path)
parser.add_argument('--output',type=pathlib.Path)
args=parser.parse_args();archive=args.archive.resolve(strict=True)
preserved=json.loads((archive/'summary.json').read_text())
out=validate_output(pathlib.Path(preserved['source']),pathlib.Path(preserved['reference']),args.output or archive/'storage-pilot')
out.mkdir(parents=True,exist_ok=True)
inventory=json.loads((archive/'metadata/inventory.json').read_text());groups=collections.defaultdict(list)
for row in inventory:groups[pathlib.Path(row['relative_path']).suffix.lower() or '(none)'].append(row)
limits={'.dng':48,'.jpg':16,'.mp4':4,'.mov':4,'.3fr':5,'.thm':4,'(none)':12}
selected=[]
for ext,rows in sorted(groups.items()):
 rows.sort(key=lambda r:r['relative_path']);n=min(limits.get(ext,4),len(rows))
 for i in range(n):selected.append((ext,rows[min(len(rows)-1,int((i+0.5)*len(rows)/n))]))
atomic_json(out/'selection.json',dict(method='Systematic evenly spaced selection by relative path within each file extension. Estimates weighted by total source bytes per extension.',files=[dict(extension=e,**r) for e,r in selected]))
rows=[];start=time.monotonic()

def hashfile(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  while b:=f.read(1024*1024):h.update(b)
 return h.hexdigest()

def summary(complete=False):
 by={};estimated_copy=0;estimated_delta=0;estimated_metadata=0
 for ext,items in groups.items():
  sample=[r for r in rows if r['extension']==ext];total=sum(r['apo_stamp'][1] for r in items)
  if not sample:continue
  sampled=sum(r['source_bytes'] for r in sample)
  copy=sum(r['original_backup_bytes'] for r in sample);delta=sum(r['delta_bytes'] for r in sample)
  metadata=sum(r['metadata_archive_bytes'] for r in sample)
  cr=copy/sampled if sampled else 0;dr=delta/sampled if sampled else 0
  mr=metadata/sampled if sampled else 0
  estimated_copy+=total*cr;estimated_delta+=total*dr;estimated_metadata+=total*mr
  by[ext]=dict(sample_files=len(sample),total_files=len(items),total_source_bytes=total,sample_source_bytes=sampled,
               byte_different_files=sum(not r['whole_file_equal'] for r in sample),original_copy_ratio=cr,
               vcdiff_ratio=dr,metadata_export_ratio=mr,estimated_original_copy_bytes=total*cr,
               estimated_vcdiff_bytes=total*dr,estimated_metadata_bytes=total*mr)
 total=sum(r['apo_stamp'][1] for r in inventory)
 result=dict(complete=complete,files_checked=len(rows),sample_size=len(selected),elapsed_seconds=time.monotonic()-start,
             total_source_bytes=total,estimated_differing_original_backup_bytes=estimated_copy,
             estimated_vcdiff_bytes=estimated_delta,estimated_full_metadata_export_bytes=estimated_metadata,
             estimated_differing_originals_plus_metadata_ratio=(estimated_copy+estimated_metadata)/total if total else None,
             estimated_vcdiff_plus_metadata_ratio=(estimated_delta+estimated_metadata)/total if total else None,
             by_extension=by,note='Stratified pilot, not a final exact ratio or permission to delete source. VCDIFF needs unchanged hash-identical reference files.')
 atomic_json(out/'summary.json',result);print(json.dumps({k:v for k,v in result.items() if k!='by_extension'}),flush=True)

for i,(ext,r) in enumerate(selected):
 a=pathlib.Path(r['apo']);b=pathlib.Path(r['bumer']) if r['bumer'] else None
 checkpoint=out/(f'{i:03}.json')
 if checkpoint.exists():
  saved=json.loads(checkpoint.read_text())
  if saved.get('source_identity')!=identity(a) or saved.get('reference_identity')!=(identity(b) if b else None):raise ValueError('Pilot checkpoint input changed or legacy identity absent; preserve old output and use a fresh output directory')
  if saved.get('delta_file') and hashfile(out/saved['delta_file'])!=saved.get('delta_sha256'):raise ValueError('Pilot delta checkpoint corrupted')
  metadata_path=out/(f'{i:03}.metadata.json.gz')
  if hashfile(metadata_path)!=saved.get('metadata_sha256'):raise ValueError('Pilot metadata checkpoint corrupted')
  rows.append(saved);continue
 print('Checking',i+1,'/',len(selected),r['relative_path'],flush=True)
 sa=identity(a);sb=identity(b) if b else None;ah=hashfile(a);bh=hashfile(b) if b else None
 equal=ah==bh;delta_bytes=0;delta_name=None
 if not equal and b:
  delta=out/(f'{i:03}.vcdiff');restore=out/(f'{i:03}.restored')
  subprocess.run(['xdelta3','-e','-1','-S','none','-n','-D','-R','-A','-B','16777216','-W','8388608','-f','-s',str(b),str(a),str(delta)],check=True)
  subprocess.run(['xdelta3','-d','-D','-R','-f','-s',str(b),str(delta),str(restore)],check=True)
  if hashfile(restore)!=ah:raise ValueError('VCDIFF round-trip SHA-256 failed')
  delta_bytes=delta.stat().st_size;delta_name=delta.name;restore.unlink()
 elif not b:delta_bytes=a.stat().st_size
 if identity(a)!=sa or (b and identity(b)!=sb):raise ValueError('Input changed during pilot')
 snapshots,stderr=extract([str(a)]+([str(b)] if b else []));meta=out/(f'{i:03}.metadata.json.gz')
 data=dict(relative_path=r['relative_path'],apo_metadata=snapshots.get(str(a),{}),bumer_metadata=snapshots.get(str(b),{}) if b else {},
           differences=compare(snapshots.get(str(a),{}),snapshots.get(str(b),{})) if b else [],stderr=stderr)
 with gzip.open(meta,'wt',encoding='utf8',compresslevel=1) as f:json.dump(data,f)
 item=dict(extension=ext,relative_path=r['relative_path'],source_identity=sa,reference_identity=sb,
           delta_sha256=hashfile(out/delta_name) if delta_name else None,metadata_sha256=hashfile(meta),source_bytes=sa[1],apo_sha256=ah,bumer_sha256=bh,
           whole_file_equal=equal,original_backup_bytes=0 if equal else sa[1],delta_bytes=delta_bytes,
           delta_file=delta_name,reconstruction_verified=bool(b or not equal),metadata_archive_bytes=meta.stat().st_size)
 atomic_json(checkpoint,item);rows.append(item);summary()
summary(True)

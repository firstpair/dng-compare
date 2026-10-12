#!/usr/bin/env python3
"""Produce a deletion-readiness report after both preservation stages finish."""
import csv,gzip,hashlib,json,os,pathlib,sys
import database_backend as sqlite3
from portable_metadata import stamp
from preserve_apo import atomic_json,identity,digest,validate_output

archive=pathlib.Path(sys.argv[1])
metadata=json.loads((archive/'metadata/summary.json').read_text());preserved=json.loads((archive/'summary.json').read_text())
source=pathlib.Path(preserved['source']);reference=pathlib.Path(preserved['reference'])
archive=validate_output(source,reference,archive)
issues=[]
if not metadata['complete']:issues.append('Metadata comparison incomplete')
if not preserved['byte_preservation_complete']:issues.append('Original-file preservation incomplete or errors')
inventory=json.loads((archive/'metadata/inventory.json').read_text())
current=sorted(str(pathlib.Path(d,n).relative_to(source)) for d,ds,ns in os.walk(source) for n in ns)
if current!=sorted(r['relative_path'] for r in inventory):issues.append('Source inventory changed')
db=sqlite3.connect(archive/'recovery.sqlite3')
records={rel:json.loads(text) for rel,text in db.execute('SELECT path,record FROM files')}
if set(current)!=set(records):issues.append('Recovery inventory does not cover every source file')
for r in inventory:
 p=pathlib.Path(r['apo']);base=pathlib.Path(r['bumer']) if r['bumer'] else None
 if stamp(p)!=r['apo_stamp'] or (base and stamp(base)!=r['bumer_stamp']):issues.append('Metadata input changed: '+r['relative_path'])
for rel,r in records.items():
 p=source/rel;base=pathlib.Path(r['reference']) if r['reference'] else None
 if identity(p)!=r['source_identity']:issues.append('Original changed: '+rel)
 if base and identity(base)!=r['reference_identity']:issues.append('Reference changed: '+rel)
 if r.get('original'):
  original=archive/r['original']
  if not original.is_file() or digest(original)!=r['source_sha256']:issues.append('Missing or corrupted archived original: '+rel)
 if r.get('delta'):
  delta=archive/r['delta']
  if not delta.is_file() or digest(delta)!=r['delta_sha256']:issues.append('Missing or corrupted VCDIFF delta: '+rel)
 if not r.get('reconstruction_verified'):issues.append('Missing byte verification: '+rel)
errors=[];changes=0
with (archive/'comparison.csv').open('w',newline='') as f:
 out=csv.writer(f);out.writerow(['relative_path','Bumer_reference','whole_file_identical','preserved_original','vcdiff_delta','source_sha256','reference_sha256','embedded_metadata_changes','metadata_warnings','metadata_errors'])
 for path in sorted((archive/'metadata/batches').glob('*.jsonl.gz')):
  with gzip.open(path,'rt') as stream:
   for line in stream:
    r=json.loads(line);rec=records.get(r['relative_path'],{})
    embedded=sum(c['category']=='embedded' for c in r['differences']);changes+=int(embedded>0)
    # Unknown .DS_Store metadata is opaque filesystem bookkeeping; all original
    # bytes/xattrs are still preserved. Media decoding errors remain blockers.
    if r['errors'] and pathlib.Path(r['apo']).name!='.DS_Store':errors.append(dict(path=r['relative_path'],errors=r['errors']))
    out.writerow([r['relative_path'],r['bumer'],rec.get('whole_file_equal'),rec.get('original'),rec.get('delta'),rec.get('source_sha256'),rec.get('reference_sha256'),embedded,0 if r.get('ignored_system_file') else len(r['warnings']),0 if r.get('ignored_system_file') else len(r['errors'])])
if errors:issues.append('Unresolved metadata errors in '+str(len(errors))+' media files')
# The historical XMP archive is portable evidence, not a replacement for the current full bytes.
xmp=archive/'xmp-original';packets=0
xmp_summary=json.loads((xmp/'summary.json').read_text())
if not xmp_summary.get('all_xmp_saved') or xmp_summary.get('errors'):issues.append('XMP preservation incomplete or errors')
for line in (xmp/'manifest.jsonl').open():
 row=json.loads(line)
 for packet in row.get('packets',[]):
  b=(xmp/packet['archive_rel']).read_bytes();packets+=1
  if len(b)!=packet['bytes'] or hashlib.sha256(b).hexdigest()!=packet['sha256']:issues.append('XMP archive hash mismatch')
archive_bytes=sum(p.stat().st_size for d,ds,ns in os.walk(archive) for n in ns for p in [pathlib.Path(d,n)] if p.is_file())
original_bytes=sum(r['source_identity'][1] for r in records.values())
readiness=dict(archive_verified=not issues,safe_to_delete_source=False,archive_logical_bytes=archive_bytes,
               original_bytes=original_bytes,archive_to_original_ratio=archive_bytes/original_bytes if original_bytes else None,source=str(source),reference=str(reference),total_source_files=len(current),
               preserved_files=len(records),files_with_embedded_metadata_differences=changes,
               verified_xmp_packets=packets,issues=issues,media_metadata_errors=errors,
               condition='Keep this archive and every referenced file unchanged or hash-identical. Verify restoration independently and retain a second copy before any operator deletion. This report never authorizes deletion.')
atomic_json(archive/'deletion-readiness.json',readiness)
print(json.dumps(readiness,indent=2),flush=True)
if issues:sys.exit(1)

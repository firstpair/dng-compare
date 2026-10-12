#!/usr/bin/env python3
"""Verify a year archive and report byte, metadata, XMP, and image coverage."""
import csv,gzip,hashlib,json,os,pathlib,sys,subprocess,shutil,importlib.metadata
import database_backend as sqlite3
from portable_metadata import stamp
from preserve_apo import atomic_json,identity,digest,validate_output

archive=pathlib.Path(sys.argv[1])
if os.environ.get('DNG_DATABASE_BACKEND')=='turso' or (archive/'recovery.sqlite3.engine.json').exists():
 import importlib.util
 if importlib.util.find_spec('turso') is None:
  python=pathlib.Path(__file__).resolve().parent/'.venv/bin/python'
  if not python.exists():raise ValueError('Run finalization with a Python environment containing pyturso==0.8.2')
  os.execv(str(python),[str(python),*sys.argv])
source=pathlib.Path(json.loads((archive/'summary.json').read_text())['source']);reference=pathlib.Path(json.loads((archive/'summary.json').read_text())['reference'])
archive=validate_output(source,reference,archive)
atomic_json(archive/'verification.json',dict(status='validating',verified=False,archive_verified=False,comparison_complete=False,safe_to_delete_source=False,execution_issues=['Verification has not finished']))
# Compare raw samples and auxiliary images separately using the original DNG tool.
project=pathlib.Path(__file__).resolve().parent
phase_backend=archive/'finalization-backend.json'
phase_execution=None
if phase_backend.exists():
 phase_execution=json.loads(phase_backend.read_text())
 if phase_execution['backend']!='oxcaml':raise ValueError('Unsupported finalization backend')
 binary=archive/phase_execution['executable']
 if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest()!=phase_execution['sha256']:
  raise ValueError('Finalization backend executable hash mismatch')
 os.environ['DNG_BYTE_BACKEND']=str(binary.resolve())
subprocess.run([sys.executable,str(project/'image_categories.py'),str(archive)],check=True)
for name in ['finalize_year.py','image_categories.py','compare_dng.py','requirements.txt','database_backend.py']:
 if (project/name).resolve()!=(archive.parent.parent/'tools'/name).resolve():shutil.copy2(project/name,archive.parent.parent/'tools'/name)
provenance=json.loads((archive/'provenance.json').read_text())
if phase_execution is not None:provenance['finalization_byte_backend']=phase_execution
try:
 provenance['git_at_finalization']=dict(commit=subprocess.check_output(['git','-C',str(project),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip(),working_tree=subprocess.check_output(['git','-C',str(project),'status','--porcelain'],text=True).splitlines())
except (OSError,subprocess.CalledProcessError):provenance['git_at_finalization']=dict(available=False)
provenance['image_python_environment']=sorted(
 f"{distribution.metadata['Name']}=={distribution.version}"
 for distribution in importlib.metadata.distributions()
 if distribution.metadata.get('Name'))
provenance['image_comparison']='Original compare_dng.py encoded-block and decoded-sample comparison; whole-file SHA-256 equality reused.'
image_execution=json.loads((archive/'image-comparison/summary.json').read_text())
provenance['image_workers']=image_execution.get('workers',1)
provenance['image_rate_mib']=image_execution.get('rate_mib')
for name in ['finalize_year.py','image_categories.py','compare_dng.py','requirements.txt','database_backend.py']:
 provenance['tools'][name]=digest(project/name)
provenance['tools_directory']='tools'
versioned_tools=archive/'tools';versioned_tools.mkdir(exist_ok=True)
for name,expected in provenance['tools'].items():
 origin=archive.parent.parent/'tools'/name
 if digest(origin)!=expected:raise ValueError('Archived tool hash mismatch: '+name)
 shutil.copy2(origin,versioned_tools/name)
atomic_json(archive/'provenance.json',provenance)
root_provenance=json.loads((archive.parent.parent/'provenance.json').read_text())
root_provenance['tools'].update(provenance['tools'])
root_provenance['image_python_environment']=provenance['image_python_environment']
root_provenance['image_comparison']=provenance['image_comparison']
root_provenance['git_at_finalization']=provenance['git_at_finalization']
atomic_json(archive.parent.parent/'provenance.json',root_provenance)
metadata=json.loads((archive/'metadata/summary.json').read_text());preserved=json.loads((archive/'summary.json').read_text())
issues=[]
if not metadata['complete']:issues.append('Metadata comparison incomplete')
if not preserved['byte_preservation_complete']:issues.append('Original-file preservation incomplete or errors')
inventory=json.loads((archive/'metadata/inventory.json').read_text())
current=sorted(str(pathlib.Path(d,n).relative_to(source)) for d,ds,ns in os.walk(source) for n in ns)
if current!=sorted(r['relative_path'] for r in inventory):issues.append('Source inventory changed')
recovery_path=archive/'recovery.sqlite3'
wal=recovery_path.with_name(recovery_path.name+'-wal')
if wal.exists() and wal.stat().st_size:raise ValueError('Recovery checkpoint has uncheckpointed WAL data')
db=sqlite3.connect('file:'+str(recovery_path)+'?mode=ro&immutable=1',uri=True)
records={rel:json.loads(text) for rel,text in db.execute('SELECT path,record FROM files')}
if set(current)!=set(records):issues.append('Recovery inventory does not cover every source file')
manifest_records=[json.loads(line) for line in (archive/'manifest.jsonl').open()]
if len(manifest_records)!=len(records) or {r['relative_path']:r for r in manifest_records}!=records:issues.append('Portable recovery manifest differs from checkpoint records')
if metadata.get('files_processed')!=len(inventory):issues.append('Metadata processed count does not cover inventory')
if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':issues.append('Recovery SQLite integrity check failed')
db.close()
classification=sqlite3.connect('file:'+str(archive/'metadata/differences.sqlite3')+'?mode=ro',uri=True)
if classification.execute('PRAGMA integrity_check').fetchone()[0]!='ok':issues.append('Metadata SQLite integrity check failed')
expected_index={r['relative_path'] for r in inventory if pathlib.Path(r['apo']).name!='.DS_Store'}
actual_index={r[0] for r in classification.execute('SELECT path FROM files')}
if actual_index!=expected_index:issues.append('Metadata classification index does not cover source inventory')
classification.close()
image_rows=[json.loads(line) for line in (archive/'image-comparison/pairs.jsonl').open()]
expected_images={rel for rel in records if pathlib.Path(rel).suffix.lower()=='.dng'}
if len(image_rows)!=len(expected_images) or {r['relative_path'] for r in image_rows}!=expected_images:issues.append('DNG image report does not cover all source DNGs')
if not json.loads((archive/'image-comparison/summary.json').read_text()).get('complete'):issues.append('DNG image classification incomplete')

for r in inventory:
 p=pathlib.Path(r['apo']);base=pathlib.Path(r['bumer']) if r['bumer'] else None
 if stamp(p)!=r['apo_stamp'] or (base and stamp(base)!=r['bumer_stamp']):issues.append('Metadata input changed: '+r['relative_path'])
for rel,r in records.items():
 p=source/rel;base=pathlib.Path(r['reference']) if r['reference'] else None
 if identity(p)!=r['source_identity']:issues.append('Original changed: '+rel)
 if base and identity(base)!=r['reference_identity']:issues.append('Reference changed: '+rel)
 if r.get('original') and not (archive/r['original']).is_file():issues.append('Missing archived original: '+rel)
 if r.get('delta'):
  delta=archive/r['delta']
  if not delta.is_file() or digest(delta)!=r['delta_sha256']:issues.append('Missing or corrupted VCDIFF delta: '+rel)
 if not r.get('reconstruction_verified'):issues.append('Missing byte verification: '+rel)
errors=[];changes=0;metadata_paths=[]
with (archive/'comparison.csv').open('w',newline='') as f:
 out=csv.writer(f);out.writerow(['relative_path','Bumer_reference','whole_file_identical','preserved_original','vcdiff_delta','source_sha256','reference_sha256','embedded_metadata_changes','metadata_warnings','metadata_errors'])
 for path in sorted((archive/'metadata/batches').glob('*.jsonl.gz')):
  with gzip.open(path,'rt') as stream:
   for line in stream:
    r=json.loads(line);rec=records.get(r['relative_path'],{})
    metadata_paths.append(r['relative_path'])
    if r['bumer']!=rec.get('reference') or r['apo_stamp']!=rec.get('source_identity') or r['bumer_stamp']!=rec.get('reference_identity'):issues.append('Metadata and recovery inputs differ: '+r['relative_path'])
    embedded=sum(c['category']=='embedded' for c in r['differences']) if not r['errors'] else None;changes+=int(bool(embedded))
    # Unknown .DS_Store metadata is opaque filesystem bookkeeping; all original
    # bytes/xattrs are still preserved. Input format diagnostics remain explicit.
    if r['errors'] and pathlib.Path(r['apo']).name!='.DS_Store':errors.append(dict(path=r['relative_path'],errors=r['errors']))
    out.writerow([r['relative_path'],r['bumer'],rec.get('whole_file_equal'),rec.get('original'),rec.get('delta'),rec.get('source_sha256'),rec.get('reference_sha256'),embedded,0 if r.get('ignored_system_file') else len(r['warnings']),0 if r.get('ignored_system_file') else len(r['errors'])])
if len(metadata_paths)!=len(inventory) or set(metadata_paths)!={r['relative_path'] for r in inventory}:issues.append('Metadata batches do not cover source inventory exactly once')
format_errors=[item for item in errors if all('file format error' in str(e.get('error','')).lower() for e in item['errors'])]
other_errors=[item for item in errors if item not in format_errors]
if format_errors:issues.append('Metadata unavailable in '+str(len(format_errors))+' unreadable input files')
if other_errors:issues.append('Unresolved metadata errors in '+str(len(other_errors))+' media files')
# The historical XMP archive is portable evidence, not a replacement for the current full bytes.
xmp=archive/'xmp-original';packets=0;xmp_paths=[];xmp_input_diagnostics=[]
xmp_summary=json.loads((xmp/'summary.json').read_text())
if not xmp_summary.get('complete'):issues.append('XMP extraction inventory incomplete')
known_unreadable={item['path'] for item in errors if any(e.get('side')=='apo' and 'file format error' in str(e.get('error','')).lower() for e in item['errors'])}
for item in json.loads((archive/'metadata/checksums.json').read_text()):
 if digest(archive/item['path'])!=item['sha256']:issues.append('Metadata batch checksum mismatch: '+item['path'])
for line in (xmp/'manifest.jsonl').open():
 row=json.loads(line);xmp_paths.append(row['source_rel'])
 if row.get('status')!='ok':
  if row['source_rel'] in known_unreadable:xmp_input_diagnostics.append(row)
  else:issues.append('Unresolved XMP extraction error: '+row['source_rel'])
 for packet in row.get('packets',[]):
  b=(xmp/packet['archive_rel']).read_bytes();packets+=1
  if len(b)!=packet['bytes'] or hashlib.sha256(b).hexdigest()!=packet['sha256']:issues.append('XMP archive hash mismatch')
if len(xmp_paths)!=len(expected_images) or set(xmp_paths)!=expected_images:issues.append('XMP manifest does not cover every source DNG exactly once')
if xmp_input_diagnostics:issues.append('XMP unavailable in '+str(len(xmp_input_diagnostics))+' unreadable source files')
archive_bytes=sum(p.stat().st_size for d,ds,ns in os.walk(archive) for n in ns for p in [pathlib.Path(d,n)] if p.is_file())
original_bytes=sum(r['source_identity'][1] for r in records.values())
execution_issues=[item for item in issues if not item.startswith(('Metadata unavailable in ','XMP unavailable in '))]
readiness=dict(status='complete',comparison_complete=not execution_issues,archive_verified=not execution_issues,
               input_diagnostics=errors,xmp_input_diagnostics=xmp_input_diagnostics,execution_issues=execution_issues,verified=not issues,safe_to_delete_source=False,archive_logical_bytes=archive_bytes,
               original_bytes=original_bytes,archive_to_original_ratio=archive_bytes/original_bytes if original_bytes else None,source=str(source),reference=str(reference),total_source_files=len(current),
               preserved_files=len(records),files_with_embedded_metadata_differences=changes,files_with_unavailable_metadata=len(errors),
               verified_xmp_packets=packets,image_comparison=json.loads((archive/'image-comparison/summary.json').read_text()),issues=issues,media_metadata_errors=errors,
               condition='Keep this archive and every referenced Bumer file unchanged or hash-identical. Source has NOT been deleted.')
atomic_json(archive/'verification.json',readiness)
print(json.dumps(readiness,indent=2),flush=True)
# Input format diagnostics remain explicit and excluded from valid metadata comparisons.
# They cannot justify aborting the remaining source years after exact-byte preservation passes.
if execution_issues:sys.exit(1)

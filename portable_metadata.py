#!/usr/bin/env python3
"""Read-only, resumable Apo/Bumer metadata snapshots in gzip JSON Lines.
Every decoded value is retained in full. No metadata is merged or overwritten.
"""
import argparse, base64, collections, gzip, hashlib, json, os, pathlib, subprocess, time, contextlib
from metadata_index import open_index, ingest, read_rows, ProcessIndex
from xattr_portable import install
install()

EXCLUDE = ["PreviewImage", "JpgFromRaw", "ThumbnailImage", "OtherImage", "RawImage", "OriginalRawFileData"]


def stamp(p):
    s=p.stat()
    return [s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def properties(p):
    s=p.stat()
    return dict(mode=s.st_mode, uid=s.st_uid, gid=s.st_gid, mtime_ns=s.st_mtime_ns,
                atime_ns=s.st_atime_ns, ctime_ns=s.st_ctime_ns,
                birthtime=getattr(s,"st_birthtime",None), flags=getattr(s,"st_flags",0),
                xattrs={n:base64.b64encode(os.getxattr(p,n)).decode() for n in os.listxattr(p)})


def atomic_json(path,data):
    temp=path.with_suffix(".tmp")
    with temp.open("w") as f:
        json.dump(data,f,indent=2,ensure_ascii=True); f.flush();os.fsync(f.fileno())
    temp.replace(path)


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError("Duplicate JSON metadata key: "+key)
        result[key]=value
    return result

def compare(left,right):
    # Keep original grouped tag names and typed values; added/removed are directional.
    result=[]
    for k in sorted(set(left)|set(right)):
        if k=="SourceFile" or k.startswith("ExifTool:") or k.startswith("Composite:"):continue
        if k in left and k in right and left[k]==right[k]:continue
        r=dict(tag=k,kind="added_in_bumer" if k not in left else "missing_in_bumer" if k not in right else "changed_in_bumer")
        if k in left:r["apo"]=left[k]
        if k in right:r["bumer"]=right[k]
        result.append(r)
    return result


def known_pairs(project):
    """Optional legacy TSV hints; clean clones fall back to same-relative-path peers."""
    pairs={}
    for p in [project/"apo-raw-report/verified-from-python.tsv",project/"apo-raw-report/verified.tsv"]:
        if not p.exists(): continue
        for line in p.open():
            c=line.rstrip("\n").split("\t")
            if len(c)==4:pairs[os.fsdecode(bytes.fromhex(c[0]))]=os.fsdecode(bytes.fromhex(c[1]))
    return pairs


def extract(paths):
    cmd=["exiftool","-j","-a","-G0:1:4","-n","-struct","-b","-u","-U","-api","LargeFileSupport=1","-all","-DNGPrivateData","-MakerNotes"]
    for tag in EXCLUDE:cmd.extend(["-x",tag])
    backend=os.environ.get("DNG_METADATA_BACKEND")
    if backend:
        payload=("\0".join(paths)+"\0").encode() if backend.endswith('-oxcaml') else json.dumps(paths).encode()
        proc=subprocess.run([backend,os.environ.get("DNG_METADATA_WORKERS","18")],input=payload,capture_output=True)
        if proc.returncode:raise ValueError("Parallel metadata extraction failed: "+proc.stderr.decode(errors="replace"))
    else:
        proc=subprocess.run(cmd+paths,capture_output=True)
    rows=json.loads(proc.stdout,object_pairs_hook=unique_object)
    if not isinstance(rows,list):raise ValueError("Invalid ExifTool response")
    return {r["SourceFile"]:r for r in rows},proc.stderr.decode(errors="replace")


def snapshot(source,reference,archive,project,batch_size,prepared_inventory=None,wait_verified=None):
    from preserve_apo import validate_output, archive_lock
    source=pathlib.Path(source).absolute();reference=pathlib.Path(reference).absolute()
    archive=validate_output(source,reference,archive)
    if not 1 <= batch_size <= 64: raise ValueError('batch size must be 1..64')
    with archive_lock(archive,'.metadata-preservation.lock'):
        (archive/'metadata').mkdir(parents=True,exist_ok=True)
        index=ProcessIndex(archive) if wait_verified is not None else open_index(archive)
        with contextlib.closing(index):
            return _snapshot(source,reference,archive,pathlib.Path(project),batch_size,prepared_inventory,wait_verified,index)

def _snapshot(source,reference,archive,project,batch_size,prepared_inventory,wait_verified,classification_db):
    archive.mkdir(parents=True,exist_ok=True); batches=archive/"metadata/batches";batches.mkdir(parents=True,exist_ok=True)
    inventory_path=archive/"metadata/inventory.json"; pairs=known_pairs(project)
    if prepared_inventory is not None:
        inventory=prepared_inventory
        if inventory_path.exists():
            saved=json.loads(inventory_path.read_text())
            # Access times can change during read-only work; immutable identities cannot.
            keys=('relative_path','apo','bumer','apo_stamp','bumer_stamp')
            if [{k:r[k] for k in keys} for r in saved]!=[{k:r[k] for k in keys} for r in inventory]:
                raise ValueError('Metadata inventory changed before unified resume')
            inventory=saved
        else:atomic_json(inventory_path,inventory)
    elif inventory_path.exists():inventory=json.loads(inventory_path.read_text())
    else:
        inventory=[]
        for d,ds,ns in os.walk(source,onerror=lambda e:(_ for _ in ()).throw(e)):
            ds.sort();ns.sort()
            for n in ns:
                p=pathlib.Path(d,n)
                if not p.is_file() or p.is_symlink():raise ValueError("Unsupported source entry: "+str(p))
                rel=str(p.relative_to(source));r=pathlib.Path(pairs.get(str(p),str(reference/rel)))
                if not r.is_file():r=None
                inventory.append(dict(relative_path=rel,apo=str(p),bumer=str(r) if r else None,
                                      apo_stamp=stamp(p),bumer_stamp=stamp(r) if r else None,
                                      apo_filesystem=properties(p)))
        atomic_json(inventory_path,inventory)
    counts=collections.Counter();tag_counts=collections.Counter();start=time.monotonic()
    def add(row):
        counts["files_processed"]+=1
        counts["files_with_reference"]+=int(row["bumer"] is not None)
        counts["files_without_reference"]+=int(row["bumer"] is None)
        counts["ignored_system_files"]+=int(bool(row.get("ignored_system_file")))
        counts["metadata_files_processed"]+=int(not row.get("ignored_system_file",False))
        counts["metadata_errors"]+=int(bool(row["errors"]))
        counts["warnings"]+=len(row["warnings"])
        counts["files_with_embedded_differences"]+=int(any(c["category"]=="embedded" for c in row["differences"]))
        for c in row["differences"]:
            if c["category"]=="embedded":tag_counts[(c["kind"],c["tag"])]+=1
    def summary(complete=False):
        data=dict(schema="apo-bumer-metadata-v1",source=str(source),reference=str(reference),
                  total_files=len(inventory),complete=complete,**counts,elapsed_seconds=time.monotonic()-start,
                  safe_to_delete_source=False,
                  note="Metadata snapshots are descriptive; exact original-file preservation must finish separately.",
                  common_changes=[dict(kind=k[0],tag=k[1],count=v) for k,v in tag_counts.most_common(100)])
        atomic_json(archive/"metadata/summary.json",data);print(json.dumps({k:v for k,v in data.items() if k!="common_changes"}),flush=True)
    summary()
    prefetched_numbers=set();prefetched_tags={};prefetched_stderr=""
    prefetch_batches=int(os.environ.get('DNG_METADATA_PREFETCH_BATCHES','4')) if os.environ.get('DNG_METADATA_BACKEND') else 1
    if not 1<=prefetch_batches<=16:raise ValueError('metadata prefetch batches must be 1..16')
    shards=int(os.environ.get('DNG_METADATA_SHARDS','1'))
    if shards>1:
        from metadata_shards import schedule
        scheduled=schedule(inventory,batch_size,batches,shards,wait_verified)
    else:scheduled=enumerate(range(0,len(inventory),batch_size))
    for number,index in scheduled:
        chunk=inventory[index:index+batch_size];dest=batches/(f"{number:06}.jsonl.gz")
        if wait_verified is not None:wait_verified([r['relative_path'] for r in chunk])
        if dest.exists():
            rows=read_rows(dest)
            if len(rows)!=len(chunk) or [r["relative_path"] for r in rows]!=[r["relative_path"] for r in chunk]:raise ValueError("Invalid resume batch")
        else:
            paths=[]
            for r in chunk:
                if stamp(pathlib.Path(r["apo"]))!=r["apo_stamp"]:raise ValueError("Apo changed since inventory: "+r["apo"])
                if pathlib.Path(r["apo"]).name==".DS_Store":continue
                paths.append(r["apo"])
                if r["bumer"]:
                    if stamp(pathlib.Path(r["bumer"]))!=r["bumer_stamp"]:raise ValueError("Bumer changed since inventory")
                    paths.append(r["bumer"])
            if number not in prefetched_numbers:
                all_paths=list(paths);prefetched_numbers={number}
                for offset in range(1,prefetch_batches):
                    future_number=number+offset;future_index=index+offset*batch_size
                    if future_index>=len(inventory):break
                    if (batches/(f"{future_number:06}.jsonl.gz")).exists():continue
                    prefetched_numbers.add(future_number)
                    if wait_verified is not None:
                        wait_verified([r['relative_path'] for r in inventory[future_index:future_index+batch_size]])
                    for r in inventory[future_index:future_index+batch_size]:
                        if stamp(pathlib.Path(r['apo']))!=r['apo_stamp']:raise ValueError('Source changed before prefetch')
                        if pathlib.Path(r['apo']).name=='.DS_Store':continue
                        all_paths.append(r['apo'])
                        if r['bumer']:
                            if stamp(pathlib.Path(r['bumer']))!=r['bumer_stamp']:raise ValueError('Reference changed before prefetch')
                            all_paths.append(r['bumer'])
                prefetched_tags,prefetched_stderr=extract(all_paths) if all_paths else ({}, "")
            tags,stderr=prefetched_tags,prefetched_stderr;prefetched_numbers.remove(number);rows=[]
            for r in chunk:
                if pathlib.Path(r["apo"]).name==".DS_Store":
                    rows.append(dict(**r,ignored_system_file=True,apo_metadata={},bumer_metadata={},differences=[],errors=[],warnings=[]))
                    continue
                a=tags.get(r["apo"],{});b=tags.get(r["bumer"],{}) if r["bumer"] else {};errors=[];warnings=[]
                for side,vals in [("apo",a)]+([("bumer",b)] if r["bumer"] else []):
                    if not vals:errors.append(dict(side=side,error="ExifTool returned no record"))
                    for k,v in vals.items():
                        if k.rsplit(":",1)[-1]=="Error":errors.append(dict(side=side,error=v))
                        if k.rsplit(":",1)[-1]=="Warning":warnings.append(dict(side=side,warning=v))
                changes=compare(a,b) if r["bumer"] else []
                for c in changes:
                    c["category"]="filesystem" if c["tag"].startswith("File:") else "embedded"
                if stamp(pathlib.Path(r["apo"]))!=r["apo_stamp"]:raise ValueError("Apo changed during extraction")
                if r["bumer"] and stamp(pathlib.Path(r["bumer"]))!=r["bumer_stamp"]:raise ValueError("Bumer changed during extraction")
                rows.append(dict(**r,apo_metadata=a,bumer_metadata=b,differences=changes,errors=errors,warnings=warnings))
            temp=dest.with_suffix(".tmp")
            with gzip.open(temp,"wt",encoding="utf8",compresslevel=1) as f:
                for row in rows:f.write(json.dumps(row,ensure_ascii=True)+"\n")
            with temp.open("rb") as f:os.fsync(f.fileno())
            # Independent read-back verifies all saved, unabridged values.
            with gzip.open(temp,"rt",encoding="utf8") as f:saved=[json.loads(l) for l in f]
            if saved!=rows:raise ValueError("Metadata archive read-back failed")
            temp.replace(dest)
        normalized=False
        for row in rows:
            if pathlib.Path(row["apo"]).name==".DS_Store" and not row.get("ignored_system_file"):
                row.update(ignored_system_file=True,apo_metadata={},bumer_metadata={},differences=[],errors=[],warnings=[])
                normalized=True
        if normalized:
            temp=dest.with_suffix(".tmp")
            with gzip.open(temp,"wt",encoding="utf8",compresslevel=1) as f:
                for row in rows:f.write(json.dumps(row,ensure_ascii=True)+"\n")
            with temp.open("rb") as f:os.fsync(f.fileno())
            with gzip.open(temp,"rt",encoding="utf8") as f:
                if [json.loads(l) for l in f]!=rows:raise ValueError("Metadata archive read-back failed")
            temp.replace(dest)
        ingest(classification_db,dest,rows)
        for row in rows:add(row)
        if number%8==0:summary()
    classification_db.close()
    summary(True)
    manifest=[]
    for p in sorted(batches.glob("*.jsonl.gz")):
        manifest.append(dict(path=str(p.relative_to(archive)),bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
    atomic_json(archive/"metadata/checksums.json",manifest)
    print("Complete metadata comparison and portable snapshots saved",flush=True)

if __name__=="__main__":
    ap=argparse.ArgumentParser();ap.add_argument("source",type=pathlib.Path);ap.add_argument("reference",type=pathlib.Path)
    ap.add_argument("archive",type=pathlib.Path);ap.add_argument("--project",type=pathlib.Path,default=pathlib.Path(__file__).parent)
    ap.add_argument("--batch-size",type=int,default=16);a=ap.parse_args()
    if not 1<=a.batch_size<=64:ap.error("batch size must be 1..64")
    snapshot(a.source.resolve(strict=True),a.reference.resolve(strict=True),a.archive,a.project,a.batch_size)

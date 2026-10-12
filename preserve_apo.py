#!/usr/bin/env python3
"""Lossless Apo preservation in standard VCDIFF, original files and JSON manifests.
Identical files reference Bumer by SHA-256; differences are verified by decoding
saved VCDIFF to the exact original hash. Missing references use original files.
Neither the source nor reference photograph tree is written.
"""
import argparse, base64, contextlib, fcntl, hashlib, json, os, pathlib, stat, subprocess, time, threading
import database_backend as sqlite3
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from xattr_portable import install
install()

BLOCK = 1024 * 1024

def validate_output(source, reference, output):
    """Reject output trees that can alter or consume either protected input."""
    output = pathlib.Path(output).resolve()
    for root in (source, reference):
        root = pathlib.Path(root).resolve(strict=True)
        if not root.is_dir(): raise ValueError('Input root is not a directory: ' + str(root))
        if output == root or output in root.parents or root in output.parents:
            raise ValueError('Output must be outside and disjoint from input roots: ' + str(output))
    return output

@contextlib.contextmanager
def archive_lock(archive, name):
    """Separate phase locks permit byte/metadata overlap, but reject duplicate owners."""
    archive.mkdir(parents=True, exist_ok=True)
    with (archive / name).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another process owns this archive phase: ' + name) from error
        try: yield
        finally: fcntl.flock(lock, fcntl.LOCK_UN)

def identity(p):
    s = p.stat()
    return [s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]

def attrs(p):
    s = p.stat(follow_symlinks=False)
    return dict(mode=stat.S_IMODE(s.st_mode), uid=s.st_uid, gid=s.st_gid,
                mtime_ns=s.st_mtime_ns, atime_ns=s.st_atime_ns,
                flags=getattr(s, "st_flags", 0),
                xattrs={n: base64.b64encode(os.getxattr(p, n, follow_symlinks=False)).decode()
                        for n in os.listxattr(p, follow_symlinks=False)})

def native_call(*args):
    binary=os.environ.get("DNG_BYTE_BACKEND")
    result=subprocess.run([binary,*map(str,args)],text=True,capture_output=True)
    if result.returncode:raise ValueError("Native byte verification failed: "+result.stderr.strip())
    return result.stdout.strip()

def digest(p):
    if os.environ.get("DNG_BYTE_BACKEND"):return native_call("hash",p)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(BLOCK): h.update(b)
    return h.hexdigest()

def atomic_json(p, data):
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2); f.flush(); os.fsync(f.fileno())
    tmp.replace(p)

class Budget:
    def __init__(self, rate): self.rate, self.start, self.bytes = rate * 1024 ** 2, time.monotonic(), 0; self.lock = threading.Lock()
    def charge(self, n):
        with self.lock:
            self.bytes += n
            delay = self.bytes / self.rate - (time.monotonic() - self.start)
        if delay > 0: time.sleep(delay)

def stream_delta(record, archive, dst=None, budget=None):
    delta=archive/record["delta"]
    if digest(delta)!=record["delta_sha256"]:raise ValueError("VCDIFF archive hash mismatch")
    h=hashlib.sha256()
    proc=subprocess.Popen(["xdelta3","-d","-D","-R","-c","-s",record["reference"],str(delta)],
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        while value:=proc.stdout.read(BLOCK):
            h.update(value)
            if budget:budget.charge(len(value))
            if dst:dst.write(value)
        stderr=proc.stderr.read().decode(errors="replace");rc=proc.wait()
        if rc:raise ValueError("VCDIFF decode failed: "+stderr)
    finally:
        if proc.poll() is None:proc.kill();proc.wait()
        proc.stdout.close();proc.stderr.close()
    if h.hexdigest()!=record["source_sha256"]:raise ValueError("Reconstructed original SHA-256 mismatch")
    return h.hexdigest()

def reconstruct(record, archive, target=None, budget=None):
    if target and target.exists():raise FileExistsError(target)
    temporary=target.with_name(target.name+".recovery-partial") if target else None
    dst=temporary.open("xb") if temporary else None
    try:
        if record.get("delta"):
            if digest(pathlib.Path(record["reference"]))!=record["reference_sha256"]:
                raise ValueError("Bumer reference SHA-256 mismatch")
            result=stream_delta(record,archive,dst,budget)
        else:
            source=archive/record["original"] if record.get("original") else pathlib.Path(record["reference"])
            h=hashlib.sha256()
            with source.open("rb") as f:
                while value:=f.read(BLOCK):
                    h.update(value)
                    if budget:budget.charge(len(value))
                    if dst:dst.write(value)
            result=h.hexdigest()
            if result!=record["source_sha256"]:raise ValueError("Original file recovery hash mismatch")
        if dst:dst.flush();os.fsync(dst.fileno())
    except BaseException:
        if dst:dst.close();dst=None
        if temporary and temporary.exists():temporary.unlink()
        raise
    finally:
        if dst:dst.close()
    if temporary:
        # Atomic, no-overwrite commitment of a fully verified result.
        os.link(temporary,target);temporary.unlink()
    return result

def preserve_file(source, base, archive, rel, budget, full_copy=False, delta_mode=False):
    before = identity(source); meta = attrs(source)
    prior = identity(base) if base else None
    sh, bh = hashlib.sha256(), hashlib.sha256(); changed = False
    if os.environ.get("DNG_BYTE_BACKEND"):
        budget.charge(before[1]+(prior[1] if prior else 0))
        source_hash,reference_hash,equal=native_call("compare",source,str(base) if base else "").split("\t")
        changed=equal!="1"
    else:
        with source.open("rb") as sf:
            bf = base.open("rb") if base else None
            try:
                while a := sf.read(BLOCK):
                    b = bf.read(len(a)) if bf else b""
                    budget.charge(len(a) + len(b)); sh.update(a); bh.update(b)
                    if a != b: changed = True
                if bf:
                    while b := bf.read(BLOCK):
                        bh.update(b);budget.charge(len(b));changed=True
            finally:
                if bf: bf.close()
        source_hash=sh.hexdigest();reference_hash=bh.hexdigest() if base else None
    if identity(source)!=before or (base and identity(base)!=prior):
        raise ValueError("File changed during comparison")
    original_rel = None
    if (changed and not (base and delta_mode)) or not base or full_copy:
        original_rel = "originals/" + rel
        dest = archive / original_rel; dest.parent.mkdir(parents=True,exist_ok=True)
        temp = dest.with_name(dest.name+".partial")
        copy_hash = hashlib.sha256()
        with source.open("rb") as sf, temp.open("wb") as out:
            while data := sf.read(BLOCK):
                copy_hash.update(data);out.write(data);budget.charge(len(data)*2)
            out.flush();os.fsync(out.fileno())
        if copy_hash.hexdigest()!=source_hash:raise ValueError("Source changed while copying")
        for name,value in meta["xattrs"].items():os.setxattr(temp,name,base64.b64decode(value))
        os.chmod(temp,meta["mode"]);os.utime(temp,ns=(meta["atime_ns"],meta["mtime_ns"]))
        # Independently reread the original-format archive copy before marking success.
        if digest(temp)!=source_hash:raise ValueError("Archive copy read-back failed")
        temp.replace(dest)
    record=dict(relative_path=rel,source=str(source),reference=str(base) if base else None,
                source_identity=before,reference_identity=prior,source_attrs=meta,
                source_sha256=source_hash,reference_sha256=reference_hash if base else None,
                original=original_rel,whole_file_equal=bool(base and not changed),
                preserved_bytes=before[1] if original_rel else 0,reconstruction_verified=True)
    if changed and base and delta_mode and not full_copy:
        delta_rel="deltas/"+rel+".vcdiff"
        dest=archive/delta_rel;dest.parent.mkdir(parents=True,exist_ok=True)
        temp=dest.with_name(dest.name+".partial")
        if temp.exists():temp.unlink()
        budget.charge(before[1]+prior[1])
        if os.environ.get("DNG_BYTE_BACKEND"):
            delta_hash=native_call("delta",base,source,temp,source_hash)
            temp.replace(dest)
            record.update(delta=delta_rel,delta_sha256=delta_hash,delta_format="VCDIFF RFC 3284",preserved_bytes=dest.stat().st_size)
        else:
            subprocess.run(["xdelta3","-e","-1","-S","none","-n","-a","-D","-R","-A",
                            "-B","16777216","-W","8388608","-s",str(base),str(source),str(temp)],
                           check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            with temp.open("rb") as f:os.fsync(f.fileno())
            temp.replace(dest)
            record.update(delta=delta_rel,delta_sha256=digest(dest),delta_format="VCDIFF RFC 3284",preserved_bytes=dest.stat().st_size)
            stream_delta(record,archive,budget=budget)
        if os.environ.get("DNG_BYTE_BACKEND"):budget.charge(before[1])
    if identity(source)!=before or (base and identity(base)!=prior):raise ValueError("File changed during preservation")
    if attrs(source)["xattrs"]!=meta["xattrs"]:raise ValueError("Source attributes changed")
    return record

def load_known(project):
    """Optional legacy encoded-path TSV hints; absent ledgers mean relative-path matching."""
    pairs = {}
    for path in [project / "apo-raw-report/verified-from-python.tsv", project / "apo-raw-report/verified.tsv"]:
        if not path.exists(): continue
        for line in path.open():
            cols = line.rstrip("\n").split("\t")
            if len(cols) == 4:
                pairs[os.fsdecode(bytes.fromhex(cols[0]))] = pathlib.Path(os.fsdecode(bytes.fromhex(cols[1])))
    return pairs

def scan(source, reference, archive, project, rate, full_copy=False, delta_mode=False, workers=1, prepared_inventory=None, on_verified=None):
    # Keep the caller's spelling: prepared inventories bind these literal paths.
    # validate_output separately canonicalizes paths for the containment guard.
    source=pathlib.Path(source).absolute(); reference=pathlib.Path(reference).absolute()
    archive=validate_output(source, reference, archive)
    if not rate > 0 or not __import__('math').isfinite(rate): raise ValueError('Rate must be finite and positive')
    with archive_lock(archive, '.byte-preservation.lock'):
        return _scan(source, reference, archive, pathlib.Path(project), rate, full_copy,
                     delta_mode, workers, prepared_inventory, on_verified)

def _scan(source, reference, archive, project, rate, full_copy=False, delta_mode=False, workers=1, prepared_inventory=None, on_verified=None):
    if not 1 <= workers <= 36: raise ValueError("Preservation workers must be 1..36")
    archive.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(archive / "recovery.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, record TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS errors (path TEXT PRIMARY KEY, error TEXT NOT NULL)")
    known = load_known(project)
    metadata_inventory=archive/"metadata/inventory.json"
    if metadata_inventory.exists():
        for entry in json.loads(metadata_inventory.read_text()):
            if entry["bumer"]:known[entry["apo"]]=pathlib.Path(entry["bumer"])
    budget = Budget(rate); directories=[]; files=[]
    if prepared_inventory is not None:
        directories, entries = prepared_inventory
        files = [pathlib.Path(entry['apo']) for entry in entries]
    else:
        for root, dirs, names in os.walk(source, onerror=lambda e: (_ for _ in ()).throw(e)):
            dirs.sort(); names.sort(); d=pathlib.Path(root)
            directories.append(dict(path=str(d.relative_to(source)), attributes=attrs(d)))
            for name in names:
                p=d/name
                if not p.is_file() or p.is_symlink(): raise ValueError("Unsupported source entry: " + str(p))
                files.append(p)
    atomic_json(archive / "directories.json", directories)
    atomic_json(archive / "inventory.json", [str(p.relative_to(source)) for p in files])
    total=len(files); last=0; start=time.monotonic()
    totals=dict(stored=0,source_bytes=0,changed_files=0)
    def account(record, direction):
        totals['stored']+=direction*record['preserved_bytes']
        totals['source_bytes']+=direction*record['source_identity'][1]
        totals['changed_files']+=direction*int(not record['whole_file_equal'])
    # Seed once, before readiness notifications start concurrent metadata work.
    # Repeated SDK row iteration otherwise contends with metadata for the GIL.
    saved_records={}
    for rel,text in db.execute('SELECT path,record FROM files'):
        saved_records[rel]=text
        account(json.loads(text),1)
    error_paths={rel for rel, in db.execute('SELECT path FROM errors')}
    def summary(complete=False):
        count=db.execute("SELECT count(*) FROM files").fetchone()[0]
        errors=db.execute("SELECT count(*) FROM errors").fetchone()[0]
        data=dict(source=str(source), reference=str(reference), total_files=total,
                  verified_files=count, errors=errors, complete=complete, preservation_workers=workers,
                  safe_to_delete_source=False, byte_preservation_complete=complete and count==total and errors==0,
                  bytes_read_this_run=budget.bytes, elapsed_seconds=time.monotonic()-start,
                  matching='Optional legacy TSV hints, otherwise same relative path; every candidate is byte-verified.',
                  note="Deletion readiness also requires complete metadata comparison, final identity validation, and inventory coverage. Recovery references require hash-identical reference files.",storage_mode="independent_originals" if full_copy else "standard_vcdiff_and_sha256_references" if delta_mode else "originals_for_differing_files_and_hash_references_for_identical_files")
        stored,source_bytes,changed_files=(totals[k] for k in ('stored','source_bytes','changed_files'))
        data.update(source_bytes_verified=source_bytes,preserved_payload_bytes=stored,
                    payload_to_verified_original_ratio=stored/source_bytes if source_bytes else None,
                    byte_different_files=changed_files)
        atomic_json(archive / "summary.json", data)
        print(json.dumps(data), flush=True)
    summary()
    def process(p, saved):
        rel=str(p.relative_to(source))
        if saved:
            r=json.loads(saved); base=pathlib.Path(r["reference"]) if r["reference"] else None
            if identity(p)==r["source_identity"] and (not base or identity(base)==r["reference_identity"]):
                original=archive/r["original"] if r.get("original") else None
                delta=archive/r["delta"] if r.get("delta") else None
                valid_original=not original or (original.is_file() and digest(original)==r["source_sha256"])
                valid_delta=not delta or (delta.is_file() and digest(delta)==r["delta_sha256"])
                if valid_original and valid_delta and (not full_copy or original):return None
        base=known.get(str(p),reference/rel)
        if not base.is_file():base=None
        r=preserve_file(p,base,archive,rel,budget,full_copy,delta_mode)
        r["reference_relative_path"]=str(base.relative_to(reference)) if base else None
        return r

    # Only the coordinator touches SQLite; workers own distinct file payloads.
    # Keep the queue bounded so interruption and memory costs do not grow with inventory.
    iterator=iter(files)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="preservation") as pool:
        pending={}
        def submit():
            try:p=next(iterator)
            except StopIteration:return False
            rel=str(p.relative_to(source))
            saved=saved_records.pop(rel,None)
            pending[pool.submit(process,p,saved)]=(rel,saved)
            return True
        for _ in range(workers):submit()
        while pending:
            completed,_=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
            for future in completed:
                rel,saved=pending.pop(future)
                counted_new=None
                try:
                    r=future.result()
                    if r is not None:db.execute("INSERT OR REPLACE INTO files VALUES (?,?)",(rel,json.dumps(r)))
                    # A verified cached record with no error needs no write
                    # transaction. Retain its identity and payload hash checks.
                    if rel in error_paths:db.execute("DELETE FROM errors WHERE path=?",(rel,))
                    db.commit()
                    error_paths.discard(rel)
                    if r is not None:
                        if saved:account(json.loads(saved),-1)
                        account(r,1);counted_new=r
                    if on_verified is not None:on_verified(rel)
                except Exception as e:
                    db.rollback()
                    db.execute("DELETE FROM files WHERE path=?",(rel,))
                    db.execute("INSERT OR REPLACE INTO errors VALUES (?,?)",(rel,str(e)));db.commit()
                    if counted_new is not None:account(counted_new,-1)
                    elif saved:account(json.loads(saved),-1)
                    error_paths.add(rel)
                    print("ERROR",rel,str(e),flush=True)
                submit()
            if time.monotonic()-last>60:summary();last=time.monotonic()
    # Verify inventory and identities again before the deletion decision.
    current=sorted(str(pathlib.Path(d,n).relative_to(source)) for d,ds,ns in os.walk(source) for n in ns)
    if current!=sorted(str(p.relative_to(source)) for p in files): raise ValueError("Source inventory changed")
    for rel,text in db.execute("SELECT path,record FROM files"):
        r=json.loads(text);p=source/rel;base=pathlib.Path(r["reference"]) if r["reference"] else None
        if identity(p)!=r["source_identity"] or (base and identity(base)!=r["reference_identity"]):
            db.execute("INSERT OR REPLACE INTO errors VALUES (?,?)",(rel,"Identity changed before completion"))
    db.commit()
    with (archive/"manifest.jsonl").open("w") as f:
        for text, in db.execute("SELECT record FROM files ORDER BY path"): f.write(text+"\n")
        f.flush();os.fsync(f.fileno())
    summary(True)
    success=db.execute("SELECT count(*) FROM files").fetchone()[0]==total and db.execute("SELECT count(*) FROM errors").fetchone()[0]==0
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)");db.close()
    return success

def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest="command",required=True)
    a=sub.add_parser("scan");a.add_argument("source",type=pathlib.Path);a.add_argument("reference",type=pathlib.Path)
    a.add_argument("archive",type=pathlib.Path);a.add_argument("--project",type=pathlib.Path,default=pathlib.Path(__file__).parent)
    a.add_argument("--workers",type=int,default=1);a.add_argument("--rate-mib",type=float,default=200);a.add_argument("--full-copy",action="store_true");a.add_argument("--delta",action="store_true")
    a=sub.add_parser("restore");a.add_argument("archive",type=pathlib.Path);a.add_argument("relative_path");a.add_argument("output",type=pathlib.Path);a.add_argument("--reference-root",type=pathlib.Path)
    args=ap.parse_args()
    if args.command=="scan":
        if args.rate_mib<=0:ap.error("Rate must be positive")
        if not scan(args.source.resolve(strict=True),args.reference.resolve(strict=True),args.archive,args.project,args.rate_mib,args.full_copy,args.delta,args.workers):
            raise SystemExit("Preservation incomplete; inspect errors and rerun before deleting source")
    else:
        db=sqlite3.connect(args.archive/"recovery.sqlite3")
        row=db.execute("SELECT record FROM files WHERE path=?",(args.relative_path,)).fetchone()
        if not row:raise ValueError("File not preserved")
        r=json.loads(row[0])
        if args.reference_root and r.get("reference"):
            relative=pathlib.Path(r["reference_relative_path"])
            if relative.is_absolute() or ".." in relative.parts:raise ValueError("Unsafe reference path")
            r["reference"]=str(args.reference_root/relative)
        reconstruct(r,args.archive,args.output)
        for name,value in r["source_attrs"]["xattrs"].items(): os.setxattr(args.output,name,base64.b64decode(value))
        os.chmod(args.output,r["source_attrs"]["mode"])
        os.utime(args.output,ns=(r["source_attrs"]["atime_ns"],r["source_attrs"]["mtime_ns"]))
        print("Restored and SHA-256 verified",args.output)

if __name__=="__main__":main()

"""Where an archive keeps its sections: one rule for every dng-compare tool.

The byte section of ARCHIVE (docs/dngc.md, "Archive layout"):

- ARCHIVE itself, when it holds recovery.sqlite3 and no bytes/ (a legacy
  archive, written before the unified layout);
- else the path itself, when its last component is `bytes` (ARCHIVE/bytes was
  given directly; never a nested bytes/bytes);
- else ARCHIVE/bytes (created for a new or empty archive).

The metadata section, provenance.json, runs.jsonl, pair lists and ARCHIVE.json
stay at the archive's top; given ARCHIVE/bytes, the top is its parent. The same
rule is in ocaml-archive (Preserve.byte_section) and the Rust dng-compare-all
(archive::byte_section)."""
import atexit, fcntl, os, pathlib, shutil, sqlite3, tempfile, time


def byte_section(archive):
    a = pathlib.Path(archive)
    if (a / 'recovery.sqlite3').exists() and not (a / 'bytes').exists():
        return a
    if a.name == 'bytes':
        return a
    return a / 'bytes'


def archive_top(archive):
    a = pathlib.Path(archive)
    return a.parent if a.name == 'bytes' else a


def version_label(record):
    """The version of an archived content (a recovery record): its source's
    modification time (UTC) and the start of its SHA-256, e.g.
    20261029T171631Z-b755b147cbed. A relative path's earlier contents are kept
    under these labels in recovery.sqlite3's versions table, their payloads
    where they were; a later content's payload goes to versions/LABEL/."""
    return (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(record["source_identity"][2] // 10**9))
            + "-" + record["source_sha256"][:12])


def writer_lock(path):
    """The lock a writer of this database holds: the byte section's
    .byte-preservation.lock for recovery.sqlite3, the top's
    .metadata-preservation.lock for metadata/differences.sqlite3, skeletons/.lock
    for skeletons/skeletons.sqlite3."""
    path = pathlib.Path(path)
    if path.name == 'differences.sqlite3':
        return path.parent.parent / '.metadata-preservation.lock'
    if path.name == 'skeletons.sqlite3':
        return path.parent / '.lock'
    return path.parent / '.byte-preservation.lock'


def lock_held(lock):
    """Whether another process holds the flock; the file is never created."""
    if not os.path.isfile(lock):
        return False
    with open(lock, 'rb') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def open_readonly(path):
    """A read-only SQLite connection that leaves nothing behind. A plain
    mode=ro open of a closed WAL database creates its -wal and -shm files; with
    no writer holding the lock and no WAL content, the file is opened
    immutable=1, which creates nothing; otherwise a private copy of the
    database and its WAL is read."""
    path = pathlib.Path(path)
    wal = path.with_name(path.name + '-wal')
    if not lock_held(writer_lock(path)) and (not wal.exists() or wal.stat().st_size == 0):
        return sqlite3.connect(f'file:{path}?immutable=1', uri=True)
    d = tempfile.mkdtemp(prefix='dngc-db-')
    atexit.register(shutil.rmtree, d, True)
    copy = pathlib.Path(d) / path.name
    shutil.copyfile(path, copy)
    if wal.exists():
        shutil.copyfile(wal, copy.with_name(copy.name + '-wal'))
    return sqlite3.connect(copy)

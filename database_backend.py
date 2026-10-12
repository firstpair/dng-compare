"""Database driver selection for native-workflow checkpoints.

Turso is embedded in-process, pinned to 0.8.2, with explicit MVCC transactions.
Legacy archives remain readable; migration never opens an active SQLite WAL.
"""
import importlib.metadata
import json
import os
import pathlib
import shutil
import sqlite3 as legacy
import threading
from urllib.parse import unquote

Error = legacy.Error
OperationalError = legacy.OperationalError
_lifecycle_lock = threading.RLock()
_open_handles = {}


def connect(database, timeout=60, uri=False, **kwargs):
    with _lifecycle_lock:
        return _connect(database,timeout,uri,**kwargs)


def _connect(database, timeout=60, uri=False, **kwargs):
    name = str(database)
    readonly = uri and 'mode=ro' in name
    path = pathlib.Path(unquote(name[5:].split('?',1)[0]) if uri and name.startswith('file:') else name)
    marker = path.with_name(path.name + '.engine.json')
    saved = json.loads(marker.read_text()) if marker.exists() else None
    engine = saved['engine'] if saved else os.environ.get('DNG_DATABASE_BACKEND','sqlite')
    if engine == 'sqlite':return legacy.connect(database, timeout=timeout, uri=uri, **kwargs)
    if engine != 'turso':raise ValueError('Unknown database engine: ' + engine)
    import turso
    if importlib.metadata.version('pyturso') != '0.8.2':raise ValueError('Turso requires pyturso==0.8.2')
    wal = path.with_name(path.name + '-wal')
    if not saved and wal.exists() and wal.stat().st_size:
        raise ValueError('Cannot migrate an uncheckpointed SQLite database: ' + str(path))
    if saved and saved.get('active_pid') not in (None, os.getpid()):
        try:os.kill(saved['active_pid'],0)
        except ProcessLookupError:pass
        else:raise ValueError('Turso MVCC database is active in another process: ' + str(path))
    if not saved and path.exists():
        backup = path.with_name(path.name + '.before-turso')
        if backup.exists():raise ValueError('Existing migration backup requires inspection: ' + str(backup))
        shutil.copy2(path,backup)
    raw = turso.connect(str(path),isolation_level=None)
    try:
        mode = raw.execute('PRAGMA journal_mode' if _open_handles.get(str(marker)) else 'PRAGMA journal_mode=mvcc').fetchone()[0]
        if mode != 'mvcc':raise ValueError('Turso did not enable MVCC')
        raw.execute('PRAGMA synchronous=FULL')
    except BaseException:
        raw.close();raise
    info = dict(engine='turso',version='0.8.2',journal_mode='mvcc',active_pid=os.getpid())
    temp = marker.with_suffix('.tmp');temp.write_text(json.dumps(info)+'\n');temp.replace(marker)
    _open_handles[str(marker)] = _open_handles.get(str(marker),0)+1
    return Connection(raw, marker, readonly)


class Connection:
    def __init__(self, raw, marker, readonly):
        self.raw, self.marker, self.readonly = raw, marker, readonly
        self.transaction = False
        self.closed = False

    def execute(self, sql, parameters=()):
        command = sql.lstrip().split(None,1)[0].upper()
        if self.readonly and command not in ('SELECT','PRAGMA','WITH','EXPLAIN'):
            raise ValueError('Attempted write through read-only database handle')
        if command in ('INSERT','UPDATE','DELETE','REPLACE') and not self.transaction:
            self.raw.execute('BEGIN CONCURRENT');self.transaction = True
        # Legacy callers request WAL. Preserve their checkpoint contract with MVCC.
        if sql.strip().lower().replace(' ','') == 'pragmajournal_mode=wal':
            sql = 'PRAGMA journal_mode=mvcc'
        result=self.raw.execute(sql, parameters)
        if command=='BEGIN':self.transaction=True
        elif command in ('COMMIT','END','ROLLBACK'):self.transaction=False
        return result

    def executescript(self, script):
        if self.readonly:raise ValueError('Attempted schema write through read-only handle')
        return self.raw.executescript(script)

    def commit(self):
        if self.transaction:
            self.raw.execute('COMMIT');self.transaction = False

    def rollback(self):
        if self.transaction:
            self.raw.execute('ROLLBACK');self.transaction = False

    def close(self):
        with _lifecycle_lock:
            if self.closed:return
            self.rollback()
            last=_open_handles[str(self.marker)]==1
            if last:
                # Export only after every other connection has closed.
                self.raw.execute('PRAGMA journal_mode=wal').fetchall()
                self.raw.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
            self.raw.close();self.raw=None;self.closed=True
            _open_handles[str(self.marker)]-=1
            if last:
                del _open_handles[str(self.marker)]
                info=json.loads(self.marker.read_text());info['active_pid']=None;info['closed_checkpoint_mode']='wal'
                temp=self.marker.with_suffix('.tmp');temp.write_text(json.dumps(info)+'\n');temp.replace(self.marker)

    def __enter__(self):return self

    def __exit__(self, kind, value, traceback):
        if kind:self.rollback()
        else:self.commit()

#!/usr/bin/env python3
"""Capture develop settings from several editors into one comparable archive.

Each capture records one editor's settings for the photos of one folder:

* ``xmp``: settings embedded in the DNGs' XMP (Lightroom, Lightroom Classic
  after Save Metadata, Camera Raw), read with ExifTool, or imported from an
  existing ``compare_dng.metadata`` snapshot.
* ``lrcat``: Lightroom Classic develop settings from a catalog, read-only.
* ``capture-one``: Capture One ``.cos`` settings files.

Every capture keeps its raw source (ExifTool tags, catalog settings text or
.cos XML) unchanged, plus a ``settings`` map with typed values under a common
tag namespace: Adobe settings as ``XMP-crs:<name>``, Capture One settings as
``CaptureOne:<block>:<name>``. Captures are written once, never overwritten,
and indexed in ``settings/settings.sqlite3`` for SQL comparison.

Nothing here writes to photos, catalogs or settings files.
"""
import argparse, datetime, gzip, hashlib, json, math, os, pathlib, re, statistics, subprocess, sys
import sqlite3 as catalog_sqlite
import xml.etree.ElementTree as ET
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

import database_backend as sqlite3

SCHEMA_VERSION = 'develop-settings-v1'
NUMBER = re.compile(r'[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$')

INDEX_SCHEMA = '''
CREATE TABLE IF NOT EXISTS captures(label TEXT PRIMARY KEY, app TEXT NOT NULL, source TEXT NOT NULL,
  record_file TEXT NOT NULL, record_sha256 TEXT NOT NULL, photos INTEGER NOT NULL, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, tag TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS settings(label TEXT NOT NULL REFERENCES captures(label), photo_id INTEGER NOT NULL REFERENCES photos(id),
  tag_id INTEGER NOT NULL REFERENCES tags(id), value_json TEXT NOT NULL, number REAL, PRIMARY KEY(label,photo_id,tag_id));
CREATE INDEX IF NOT EXISTS settings_tag ON settings(tag_id,label,photo_id);
CREATE VIEW IF NOT EXISTS setting_values AS SELECT s.label,c.app,p.name photo,t.tag,s.value_json,s.number
  FROM settings s JOIN captures c ON c.label=s.label JOIN photos p ON p.id=s.photo_id JOIN tags t ON t.id=s.tag_id;
'''


def typed(value):
    """Return a comparable typed value; text that is exactly a number or boolean becomes one.

    Integers longer than 15 digits stay text (digests, serial numbers), so no
    implementation loses precision. Lists and structures are typed item by item.
    """
    if isinstance(value, list):
        return [typed(v) for v in value]
    if isinstance(value, dict):
        return {k: typed(v) for k, v in value.items()}
    if isinstance(value, str):
        text = value.strip()
        if NUMBER.match(text):
            integral = '.' not in text and 'e' not in text.lower()
            if integral and len(text.lstrip('+-')) > 15:
                return value
            number = float(text)
            return int(text) if integral else number
        if text in ('True', 'true'):
            return True
        if text in ('False', 'false'):
            return False
    return value


def value_json(value):
    """Canonical JSON shared with the Rust implementation."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def number_of(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


# --- Lua table literals (Lightroom Classic settings text) -------------------------------------

LUA_TOKEN = re.compile(r'\s*(?:(--[^\n]*)|("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')|'
                       r'([-+]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?))|([A-Za-z_]\w*)|([{}\[\]=,;]))', re.S)
LUA_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v', '\\': '\\', '"': '"', "'": "'", '\n': '\n'}


def lua_string(literal):
    # Lua strings are bytes: decimal escapes are bytes, other characters are the stored UTF-8.
    body, out, i = literal[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c != '\\':
            out += c.encode('utf-8'); i += 1; continue
        n = body[i + 1]
        if n.isdigit():
            digits = re.match(r'\d{1,3}', body[i + 1:]).group()
            out.append(int(digits)); i += 1 + len(digits)
        elif n in LUA_ESCAPES:
            out += LUA_ESCAPES[n].encode(); i += 2
        else:
            raise ValueError('Unsupported Lua string escape: \\' + n)
    return out.decode('utf-8', 'surrogateescape')


def lua_tokens(text):
    position, tokens = 0, []
    while True:
        match = LUA_TOKEN.match(text, position)
        if not match:
            if text[position:].strip():
                raise ValueError('Unparseable Lua text at offset %d' % position)
            return tokens
        position = match.end()
        comment, string, number, name, symbol = match.groups()
        if comment:
            continue
        if string:
            tokens.append(('value', lua_string(string)))
        elif number:
            tokens.append(('value', int(number, 16) if number.lower().lstrip('+-').startswith('0x') else typed(number)))
        elif name:
            tokens.append(('value', {'true': True, 'false': False, 'nil': None}[name]) if name in ('true', 'false', 'nil') else ('name', name))
        else:
            tokens.append(('symbol', symbol))


def parse_lua_assignment(text):
    """Parse Lightroom's ``s = { ... }`` settings text into Python values."""
    tokens = lua_tokens(text)
    if tokens[:2] and tokens[0][0] == 'name' and tokens[1] == ('symbol', '='):
        tokens = tokens[2:]
    value, end = _lua_value(tokens, 0)
    if end != len(tokens):
        raise ValueError('Trailing Lua tokens')
    return value


def _lua_value(tokens, i):
    kind, value = tokens[i]
    if kind == 'value':
        return value, i + 1
    if (kind, value) != ('symbol', '{'):
        raise ValueError('Unexpected Lua token: %r' % (value,))
    i += 1
    array, mapping = [], {}
    while tokens[i] != ('symbol', '}'):
        if tokens[i][0] == 'name' and tokens[i + 1] == ('symbol', '='):
            key = tokens[i][1]; item, i = _lua_value(tokens, i + 2); mapping[key] = item
        elif tokens[i] == ('symbol', '['):
            key, i = _lua_value(tokens, i + 1)
            if tokens[i] != ('symbol', ']') or tokens[i + 1] != ('symbol', '='):
                raise ValueError('Malformed Lua key')
            item, i = _lua_value(tokens, i + 2); mapping[str(key)] = item
        else:
            item, i = _lua_value(tokens, i); array.append(item)
        if tokens[i] in (('symbol', ','), ('symbol', ';')):
            i += 1
    if array and mapping:
        mapping.update({str(n + 1): v for n, v in enumerate(array)})
        return mapping, i + 1
    return (mapping if mapping or not array else array), i + 1


# --- Types --------------------------------------------------------------------------------------
# These mirror the serde types of the Rust implementation (rust-scanner/src/bin/develop-settings.rs).

App = Literal['xmp', 'lrcat', 'capture-one']
Settings = dict[str, JsonValue]


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FileIdentity(Strict):
    path: str
    size: int
    mtime_ns: int


class SnapshotSource(Strict):
    path: str
    size: int | None
    mtime_ns: int | None
    snapshot: str


class CatalogSource(Strict):
    path: str
    image_id: int | float
    folder: str


class ExifToolRaw(Strict):
    exiftool: dict[str, JsonValue]
    warnings: list[str] | None = None


class XmpPacketRaw(Strict):
    xmp_packet: str | None


class CatalogRaw(Strict):
    text: str | None
    digest: str | None
    virtual_copy: str | None
    history: list[str]


class CaptureOneRaw(Strict):
    cos_xml: str


class Record(Strict):
    photo: str
    source: SnapshotSource | CatalogSource | FileIdentity
    settings: Settings
    raw: CaptureOneRaw | CatalogRaw | ExifToolRaw | XmpPacketRaw


class CaptureSource(Strict):
    folder: str
    snapshot: str | None = None
    catalog: str | None = None
    settings_dir: str | None = None
    extensions: list[str] | None = None


class Provenance(BaseModel):
    schema_: str = Field(alias='schema')
    label: str
    app: App
    source: CaptureSource
    photos: int
    record_file: str
    record_sha256: str
    created_utc: str
    implementation: str | None = None
    tool_commit: str | None
    notes: list[str] = []
    model_config = ConfigDict(populate_by_name=True)


class Agreement(BaseModel):
    left_tag: str
    right_tag: str
    photos: int
    identical: int
    numeric: int | None = None
    median_difference: float | None = None
    median_abs_difference: float | None = None
    p90_abs_difference: float | None = None
    correlation: float | None = None


def dump(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode='json', by_alias=True, exclude_none=True), ensure_ascii=True, sort_keys=True)


def dump_record(record: Record) -> str:
    # Absent values inside sources and raw records are kept as null, as Rust writes them.
    data = record.model_dump(mode='json')
    if isinstance(record.raw, ExifToolRaw) and record.raw.warnings is None:
        del data['raw']['warnings']
    return json.dumps(data, ensure_ascii=True, sort_keys=True)


# --- Importers ----------------------------------------------------------------------------------

def stat_of(path: os.PathLike | str) -> FileIdentity:
    s = os.stat(path)
    return FileIdentity(path=str(path), size=s.st_size, mtime_ns=s.st_mtime_ns)


# ExifTool tag names that differ from the XMP property names (top level of the crs namespace).
EXIFTOOL_RENAMES = {'XMP-crs:ColorTemperature': 'XMP-crs:Temperature'}


def crs_settings(tags: dict[str, JsonValue]) -> Settings:
    """Camera Raw settings named by XMP property, as Lightroom Classic and the Rust reader name them."""
    return {EXIFTOOL_RENAMES.get(k, k): typed(v) for k, v in tags.items() if k.startswith('XMP-crs:')}


def capture_xmp(folder: pathlib.Path, snapshot: pathlib.Path | None = None, batch: int = 32,
                extensions: tuple[str, ...] = ('dng',)) -> list[Record]:
    folder = pathlib.Path(folder)
    records = []
    if snapshot:
        with gzip.open(snapshot, 'rt') as f:
            for line in f:
                row = json.loads(line)
                if 'tags' not in row:
                    raise ValueError('Snapshot row has no tags: %s' % row.get('file'))
                path = pathlib.Path(row['file'])
                source = SnapshotSource(path=str(path) if path.is_absolute() else str(folder / path.name),
                                        size=row.get('size'), mtime_ns=row.get('mtime_ns'), snapshot=str(snapshot))
                records.append(Record(photo=path.name, source=source, settings=crs_settings(row['tags']),
                                      raw=ExifToolRaw(exiftool=row['tags'])))
        return records
    import compare_dng
    wanted = {'.' + e.lower().lstrip('.') for e in extensions}
    paths = sorted(str(p) for p in folder.iterdir() if p.suffix.lower() in wanted and p.is_file())
    for start in range(0, len(paths), batch):
        chunk = paths[start:start + batch]
        before = {p: stat_of(p) for p in chunk}
        meta = compare_dng.metadata(chunk)
        for p in chunk:
            if stat_of(p) != before[p]:
                raise ValueError('File changed while its metadata was read: ' + p)
            entry = meta.get(p, {'error': 'ExifTool returned no record'})
            if 'error' in entry:
                raise ValueError('%s: %s' % (p, entry['error']))
            records.append(Record(photo=pathlib.Path(p).name, source=before[p], settings=crs_settings(entry['tags']),
                                  raw=ExifToolRaw(exiftool=entry['tags'], warnings=entry['warnings'])))
    return records


def capture_lrcat(folder: pathlib.Path, catalog: pathlib.Path) -> tuple[list[Record], list[str]]:
    folder_path = os.path.realpath(folder).rstrip('/') + '/'
    # Always the standard SQLite driver, read-only: never migrate or write a Lightroom catalog.
    db = catalog_sqlite.connect(pathlib.Path(catalog).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        db.execute('PRAGMA query_only=1')
        db.execute('BEGIN')
        rows = db.execute("""
          SELECT i.id_local, f.baseName || '.' || f.extension, i.copyName, d.text, d.digest
          FROM AgLibraryRootFolder r JOIN AgLibraryFolder fo ON fo.rootFolder = r.id_local
          JOIN AgLibraryFile f ON f.folder = fo.id_local JOIN Adobe_images i ON i.rootFile = f.id_local
          LEFT JOIN Adobe_imageDevelopSettings d ON d.image = i.id_local
          WHERE r.absolutePath || fo.pathFromRoot = ? ORDER BY f.baseName, i.id_local""", (folder_path,)).fetchall()
        records = []
        for image, name, copy, text, digest in rows:
            settings = parse_lua_assignment(text) if text else {}
            if not isinstance(settings, dict):
                raise ValueError('Image %s settings are not a table' % image)
            history = [s for (s,) in db.execute(
                'SELECT name FROM Adobe_libraryImageDevelopHistoryStep WHERE image=? ORDER BY dateCreated, id_local', (image,))]
            records.append(Record(photo=name + (' [%s]' % copy if copy else ''),
                                  source=CatalogSource(path=str(catalog), image_id=image, folder=folder_path),
                                  settings={'XMP-crs:' + k: v for k, v in settings.items()},
                                  raw=CatalogRaw(text=text, digest=digest, virtual_copy=copy, history=history)))
    finally:
        db.rollback() if db.in_transaction else None
        db.close()
    # Unrendered Auto: Classic stores -999999 until it computes the photo.
    pending = [r.photo for r in records if any(number_of(v) == -999999 for v in r.settings.values())]
    return records, pending


def capture_one(settings_dir: pathlib.Path) -> list[Record]:
    records = []
    for path in sorted(pathlib.Path(settings_dir).glob('*.cos')):
        before = stat_of(path)
        text = path.read_text(encoding='utf-8')
        if stat_of(path) != before:
            raise ValueError('Settings file changed while read: %s' % path)
        root = ET.fromstring(text)
        settings: Settings = {}
        for element in root.findall('E'):
            settings['CaptureOne:Image:%s' % element.get('K')] = typed(element.get('V', ''))
        for variant in root.findall('VAR'):
            for element in variant.findall('E'):
                settings['CaptureOne:Variant:%s' % element.get('K')] = typed(element.get('V', ''))
            for block in variant:
                if block.tag == 'E':
                    continue
                for element in block.iter('E'):
                    settings['CaptureOne:%s:%s' % (block.tag, element.get('K'))] = typed(element.get('V', ''))
        records.append(Record(photo=path.name[:-len('.cos')], source=before, settings=settings, raw=CaptureOneRaw(cos_xml=text)))
    return records


# --- Archive ------------------------------------------------------------------------------------

def git_commit() -> str | None:
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=pathlib.Path(__file__).parent,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None


def read_records(path: pathlib.Path) -> list[Record]:
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        return [Record.model_validate_json(line) for line in f]


def write_capture(archive: pathlib.Path, label: str, app: App, source: CaptureSource, records: list[Record],
                  notes: list[str] | None = None) -> Provenance:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', label):
        raise ValueError('Label may contain letters, digits, dot, dash and underscore')
    names = [r.photo for r in records]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate photo names in capture')
    folder = pathlib.Path(archive) / 'settings'
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / (label + '.jsonl.gz')
    provenance_path = folder / (label + '.provenance.json')
    if out.exists() or provenance_path.exists():
        raise FileExistsError('Capture %s exists; choose a new label' % label)
    temporary = folder / (label + '.jsonl.tmp')
    try:
        with gzip.open(temporary, 'wt', encoding='utf-8') as f:
            for r in records:
                f.write(dump_record(r) + '\n')
        with temporary.open('rb') as f:
            os.fsync(f.fileno())
        if read_records(temporary) != records:
            raise ValueError('Capture read-back failed')
        os.link(temporary, out)  # fails rather than replacing an existing capture
    finally:
        temporary.unlink(missing_ok=True)
    provenance = Provenance(schema=SCHEMA_VERSION, label=label, app=app, source=source, photos=len(records),
                            record_file=out.name, record_sha256=hashlib.sha256(out.read_bytes()).hexdigest(),
                            created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            implementation='python', tool_commit=git_commit(), notes=notes or [])
    provenance_path.write_text(json.dumps(provenance.model_dump(mode='json', by_alias=True), indent=2) + '\n')
    index(archive, provenance, records)
    return provenance


def open_index(archive: pathlib.Path):
    db = sqlite3.connect(pathlib.Path(archive) / 'settings' / 'settings.sqlite3', timeout=60)
    db.executescript(INDEX_SCHEMA)
    return db


def index(archive: pathlib.Path, provenance: Provenance, records: list[Record]) -> None:
    db = open_index(archive)
    try:
        with db:
            source = json.dumps(provenance.source.model_dump(mode='json', exclude_none=True), separators=(',', ':'))
            db.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?)', (provenance.label, provenance.app, source, provenance.record_file,
                       provenance.record_sha256, provenance.photos, provenance.created_utc))
            ids: dict[tuple[str, str], int] = {}
            def key(table: str, column: str, value: str) -> int:
                if (table, value) not in ids:
                    db.execute('INSERT OR IGNORE INTO %s(%s) VALUES (?)' % (table, column), (value,))
                    ids[table, value] = db.execute('SELECT id FROM %s WHERE %s=?' % (table, column), (value,)).fetchone()[0]
                return ids[table, value]
            for r in records:
                photo = key('photos', 'name', r.photo)
                for tag, value in r.settings.items():
                    db.execute('INSERT INTO settings VALUES (?,?,?,?,?)', (provenance.label, photo, key('tags', 'tag', tag),
                               value_json(value), number_of(value)))
    finally:
        db.close()


def rebuild_index(archive: pathlib.Path) -> None:
    path = pathlib.Path(archive) / 'settings' / 'settings.sqlite3'
    if path.exists():
        path.unlink()
    for provenance_path in sorted((pathlib.Path(archive) / 'settings').glob('*.provenance.json')):
        provenance = Provenance.model_validate_json(provenance_path.read_text())
        out = provenance_path.parent / provenance.record_file
        if hashlib.sha256(out.read_bytes()).hexdigest() != provenance.record_sha256:
            raise ValueError('Capture file changed: ' + out.name)
        index(archive, provenance, read_records(out))


def compare(archive: pathlib.Path, left: str, right: str, mapping: list[tuple[str, str]] | None = None) -> list[Agreement]:
    """Per-tag agreement between two captures, over photos present in both."""
    db = open_index(archive)
    try:
        pairs = mapping or [(t, t) for (t,) in db.execute(
            'SELECT DISTINCT t.tag FROM settings a JOIN settings b ON b.photo_id=a.photo_id AND b.tag_id=a.tag_id '
            'JOIN tags t ON t.id=a.tag_id WHERE a.label=? AND b.label=? ORDER BY t.tag', (left, right))]
        results = []
        for left_tag, right_tag in pairs:
            rows = db.execute(
                'SELECT a.value_json,b.value_json,a.number,b.number FROM settings a JOIN settings b ON b.photo_id=a.photo_id '
                'JOIN tags ta ON ta.id=a.tag_id JOIN tags tb ON tb.id=b.tag_id '
                'WHERE a.label=? AND b.label=? AND ta.tag=? AND tb.tag=?', (left, right, left_tag, right_tag)).fetchall()
            if not rows:
                continue
            result = Agreement(left_tag=left_tag, right_tag=right_tag, photos=len(rows),
                               identical=sum(a == b or (x is not None and x == y) for a, b, x, y in rows))
            numeric = [(x, y) for _, _, x, y in rows if x is not None and y is not None]
            if numeric:
                diff = sorted(y - x for x, y in numeric)
                absolute = sorted(abs(d) for d in diff)
                result.numeric = len(numeric)
                result.median_difference = statistics.median(diff)
                result.median_abs_difference = statistics.median(absolute)
                result.p90_abs_difference = absolute[min(len(absolute) - 1, int(0.9 * len(absolute)))]
                xs, ys = zip(*numeric)
                if len(numeric) > 1 and len(set(xs)) > 1 and len(set(ys)) > 1:
                    result.correlation = statistics.correlation(xs, ys)
            results.append(result)
        return results
    finally:
        db.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='command', required=True)
    c = sub.add_parser('capture', help='record one editor\'s settings for one folder')
    c.add_argument('archive', type=pathlib.Path)
    c.add_argument('label')
    c.add_argument('app', choices=['xmp', 'lrcat', 'capture-one'])
    c.add_argument('folder', type=pathlib.Path, help='folder holding the photos')
    c.add_argument('--catalog', type=pathlib.Path, help='Lightroom Classic catalog (lrcat)')
    c.add_argument('--settings-dir', type=pathlib.Path, help='Capture One settings folder (default FOLDER/CaptureOne/Settings*)')
    c.add_argument('--snapshot', type=pathlib.Path, help='xmp: import an existing compare_dng.metadata JSONL snapshot instead of reading the DNGs')
    c.add_argument('--extensions', default='dng', help='xmp: comma-separated file extensions to read (default dng; for example dng,jpg)')
    c.add_argument('--allow-pending', action='store_true', help='lrcat: record photos whose Auto values are not yet computed')
    m = sub.add_parser('compare', help='per-tag agreement between two captures')
    m.add_argument('archive', type=pathlib.Path)
    m.add_argument('left'); m.add_argument('right')
    m.add_argument('--map', action='append', default=[], metavar='LEFT_TAG=RIGHT_TAG')
    r = sub.add_parser('reindex', help='rebuild settings.sqlite3 from the capture files')
    r.add_argument('archive', type=pathlib.Path)
    a = ap.parse_args(argv)
    if a.command == 'capture':
        notes = []
        source = CaptureSource(folder=str(a.folder))
        if a.app == 'xmp':
            extensions = tuple(e.strip().lower().lstrip('.') for e in a.extensions.split(',') if e.strip())
            records = capture_xmp(a.folder, a.snapshot, extensions=extensions)
            source.snapshot = str(a.snapshot) if a.snapshot else None
            if extensions != ('dng',):
                source.extensions = list(extensions)
        elif a.app == 'lrcat':
            if not a.catalog:
                ap.error('lrcat requires --catalog')
            records, pending = capture_lrcat(a.folder, a.catalog)
            if pending and not a.allow_pending:
                sys.exit('%d photos have uncomputed Auto values (-999999); build previews first or pass --allow-pending' % len(pending))
            if pending:
                notes.append('%d photos recorded with uncomputed Auto placeholders' % len(pending))
            source.catalog = str(a.catalog)
        else:
            settings_dir = a.settings_dir
            if not settings_dir:
                candidates = sorted(p for p in (a.folder / 'CaptureOne').glob('Settings*') if p.is_dir())
                if len(candidates) != 1:
                    ap.error('pass --settings-dir; found %d CaptureOne/Settings* folders' % len(candidates))
                settings_dir = candidates[0]
            records = capture_one(settings_dir)
            source.settings_dir = str(settings_dir)
        if not records:
            sys.exit('No photos found')
        print(dump(write_capture(a.archive, a.label, a.app, source, records, notes)))
    elif a.command == 'compare':
        mapping = [tuple(x.split('=', 1)) for x in a.map] or None
        for row in compare(a.archive, a.left, a.right, mapping):
            print(dump(row))
    else:
        rebuild_index(a.archive)


if __name__ == '__main__':
    main()

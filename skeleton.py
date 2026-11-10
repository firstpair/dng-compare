"""Skeletons (docs/dngc-archive-spec.md, section 13): what makes a reference's
content rebuildable from an edited version of the file.

A metadata edit (a rating, develop settings, IPTC) rewrites a file's small
structures and carries its large blocks over unchanged, perhaps to other
offsets. A skeleton of a file is every byte outside its blocks, with a map of
where each block goes; the blocks themselves are not stored. Given a later
version of the file, the blocks are found in it by their marks and laid around
the skeleton, and the whole is checked against the content's SHA-256.

Walkers: classic TIFF (DNG and TIFF-based raws) and JPEG; they yield exactly
the blocks of the Rust library's (rust-archive/src/skeleton.rs). A file of
another format no larger than WHOLE is its own skeleton, with no blocks.

The section is ARCHIVE/skeletons/skeletons.sqlite3, one row per content. This
module reads it for restore (preserve_apo.reconstruct) and verify; build() and
encode() make a row, for tests and the conformance corpus."""
import collections, contextlib, hashlib, json, os, pathlib, shutil, sqlite3, subprocess, tempfile

BLOCK = 512      # an out-of-line TIFF value or JPEG segment this large is a block
GAP = 65536      # a run this long that no tag points at is a block
MARK = 4096      # a mark hashes this many bytes from each end of a block
PAD = 3          # strips or tiles of one image this close together are one block
WHOLE = 1 << 20  # a file without a walker is stored whole up to this size

VOLATILE = (700, 33723, 34377)         # XMP, IPTC, Photoshop: what editors rewrite
POINTERS = (330, 34665, 34853, 40965)  # SubIFDs, Exif, GPS, Interoperability
LAYOUT = (273, 279, 324, 325)          # strip and tile offsets and byte counts
TYPE_SIZE = {3: 2, 8: 2, 4: 4, 9: 4, 11: 4, 13: 4, 5: 8, 10: 8, 12: 8}
WIDTH = {1: 1, 3: 2, 4: 4, 13: 4}      # BYTE, SHORT, LONG, IFD

Skeleton = collections.namedtuple('Skeleton', 'format size blocks bytes')  # blocks: [at, len, mark, role]


# ---- zstd: the standard library's from Python 3.14, else the command-line tool ----

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None


def _zstd_tool(args, data):
    tool = shutil.which('zstd')
    if not tool:
        raise ValueError('skeletons need Python 3.14 (compression.zstd) or the zstd tool')
    r = subprocess.run([tool, '-q', '-c', *args], input=data, capture_output=True)
    if r.returncode:
        raise ValueError('zstd failed: ' + r.stderr.decode(errors='replace').strip())
    return r.stdout


def compress(data, level=19):
    return _zstd.compress(data, level) if _zstd else _zstd_tool([f'-{level}'], data)


def decompress(data):
    if not _zstd:
        return _zstd_tool(['-d'], data)
    try:
        return _zstd.decompress(data)
    except _zstd.ZstdError as e:
        raise ValueError(f'zstd: {e}') from e


# ---- walkers ----

def _read_at(f, at, length):
    """Up to length bytes at at; fewer at the end of the file."""
    f.seek(at)
    return f.read(length)


def _values(f, order, size, entry):
    """An entry's unsigned integer values (BYTE, SHORT, LONG, IFD)."""
    tag, typ, raw, total = entry
    width = WIDTH.get(typ)
    if not width:
        return []
    if total <= 4:
        buf = raw[:total]
    else:
        at = int.from_bytes(raw, order)
        if at + total > size:
            return []
        buf = _read_at(f, at, total)
    if len(buf) != total:
        return []
    return [int.from_bytes(buf[i:i + width], order) for i in range(0, total, width)]


def _tiff(f, size):
    h = _read_at(f, 0, 8)
    order = {b'II': 'little', b'MM': 'big'}.get(h[:2])
    if not order:
        raise ValueError('not a TIFF')
    if len(h) < 8 or int.from_bytes(h[2:4], order) != 42:
        raise ValueError('not a classic TIFF')
    blocks, claims, seen = [], [(0, 8)], set()
    queue = collections.deque([(int.from_bytes(h[4:8], order), '0')])
    while queue:
        off, role = queue.popleft()
        n = 0
        while off != 0 and off + 6 <= size and off not in seen:
            seen.add(off)
            head = _read_at(f, off, 2)
            if len(head) < 2:
                break
            cnt = int.from_bytes(head, order)
            data = _read_at(f, off + 2, cnt * 12 + 4)
            if len(data) < cnt * 12 + 4:
                break
            r = role if n == 0 else f'{role}+{n}'
            claims.append((off, 6 + cnt * 12))
            entries = []
            for i in range(0, cnt * 12, 12):
                c = data[i:i + 12]
                typ = int.from_bytes(c[2:4], order)
                entries.append((int.from_bytes(c[0:2], order), typ, c[8:12],
                                TYPE_SIZE.get(typ, 1) * int.from_bytes(c[4:8], order)))
            first = {}
            for e in entries:
                first.setdefault(e[0], e)
            for otag, ctag, name in ((273, 279, 'strips'), (324, 325, 'tiles')):
                if otag in first and ctag in first:
                    for o, c in zip(_values(f, order, size, first[otag]), _values(f, order, size, first[ctag])):
                        if c > 0 and o + c <= size:
                            blocks.append((o, c, f'{r}/{name}'))
            if 513 in first and 514 in first:
                o, c = _values(f, order, size, first[513])[:1], _values(f, order, size, first[514])[:1]
                if o and c and c[0] > 0 and o[0] + c[0] <= size:
                    blocks.append((o[0], c[0], f'{r}/jpeg'))
            for tag, typ, raw, total in entries:
                if total <= 4:
                    continue
                at = int.from_bytes(raw, order)
                if at + total > size:
                    continue
                claims.append((at, total))
                if total >= BLOCK and tag not in VOLATILE and tag not in POINTERS and tag not in LAYOUT:
                    blocks.append((at, total, f'{r}/tag{tag}'))
            for tag in POINTERS:
                if tag in first:
                    for k, o in enumerate(_values(f, order, size, first[tag])):
                        queue.append((o, f'{r}/{tag}.{k}'))
            off = int.from_bytes(data[cnt * 12:cnt * 12 + 4], order)
            n += 1
    # Data only a maker's own structures reach: no tag points at it, and an
    # edit carries it over as it is.
    pos = 0
    for o, l in sorted(claims + [(b[0], b[1]) for b in blocks] + [(size, 0)]):
        if o > pos and o - pos >= GAP:
            blocks.append((pos, o - pos, 'gap'))
        pos = max(pos, o + l)
    return blocks


def _jpeg(f, size):
    blocks, pos = [], 2
    while pos + 4 <= size:
        m = _read_at(f, pos, 4)
        if len(m) < 4 or m[0] != 0xFF:
            break
        marker = m[1]
        if marker == 0xFF:
            pos += 1
            continue
        # Start of scan, or a marker without a length: the image runs from here.
        if marker in (0xDA, 0x00, 0x01) or 0xD0 <= marker <= 0xD9:
            break
        seg = int.from_bytes(m[2:4], 'big') + 2
        if seg < 4 or pos + seg > size:
            break
        # Exif and XMP (APP1), IPTC (APP13) and comments stay in the skeleton.
        if marker not in (0xE1, 0xED, 0xFE) and seg >= BLOCK:
            blocks.append((pos, seg, f'seg{marker:02x}'))
        pos += seg
    if pos < size:
        blocks.append((pos, size - pos, 'scan'))
    return blocks


def _normalize(blocks):
    """Sorted, without overlaps; the strips or tiles of one image merged when
    no more than PAD bytes apart."""
    out = []
    for o, l, role in sorted(set(blocks)):
        if out:
            last = out[-1]
            end = last[0] + last[1]
            if o < end:
                continue
            if role == last[2] and role.endswith(('strips', 'tiles')) and o - end <= PAD:
                last[1] = o + l - last[0]
                continue
        out.append([o, l, role])
    return out


def walk(f, size):
    """A file's format and blocks [at, len, role], decided by its first bytes."""
    magic = _read_at(f, 0, 4)
    if magic in (b'II*\0', b'MM\0*'):
        return 'tiff', _normalize(_tiff(f, size))
    if magic[:2] == b'\xff\xd8':
        return 'jpeg', _normalize(_jpeg(f, size))
    raise ValueError('no skeleton walker for this format')


def mark(f, at, length):
    """A block's mark: the first 32 hex digits of the SHA-256 of its two ends."""
    h = hashlib.sha256()
    if length <= 2 * MARK:
        h.update(_read_at(f, at, length))
    else:
        h.update(_read_at(f, at, MARK))
        h.update(_read_at(f, at + length - MARK, MARK))
    return h.hexdigest()[:32]


def build(path):
    """The skeleton of the file at path. Reads its structures, the bytes
    outside its blocks, and each block's two ends; never a whole block."""
    with open(path, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        try:
            fmt, extents = walk(f, size)
        except (ValueError, OSError):
            if size > WHOLE:
                raise
            fmt, extents = 'whole', []
        blocks, parts, pos = [], [], 0
        for at, length, role in extents:
            parts.append(_read_at(f, pos, at - pos))
            blocks.append([at, length, mark(f, at, length), role])
            pos = at + length
        parts.append(_read_at(f, pos, size - pos))
    data = b''.join(parts)
    if len(data) != size - sum(b[1] for b in blocks):
        raise ValueError('file changed while its skeleton was read')
    return Skeleton(fmt, size, blocks, data)


def rebuild(skeleton, edited, sink):
    """Write the skeleton's content to sink, its blocks taken from edited, a
    later version of the same file (section 13.3); returns the SHA-256 of what
    was written. The caller compares it with the content's SHA-256: a mark
    covers only a block's ends."""
    if not skeleton.blocks:
        if len(skeleton.bytes) != skeleton.size:
            raise ValueError('skeleton shorter than its content')
        sink.write(skeleton.bytes)
        return hashlib.sha256(skeleton.bytes).hexdigest()
    h = hashlib.sha256()

    def put(data):
        h.update(data)
        sink.write(data)

    with open(edited, 'rb') as f:
        size = os.fstat(f.fileno()).st_size
        found = {}
        for at, length, _ in walk(f, size)[1]:
            found.setdefault((length, mark(f, at, length)), at)
        pos = skel = 0
        for at, length, block_mark, role in skeleton.blocks:
            n = at - pos
            if skel + n > len(skeleton.bytes):
                raise ValueError('skeleton shorter than its map')
            put(skeleton.bytes[skel:skel + n])
            skel += n
            where = found.get((length, block_mark))
            if where is None:
                raise ValueError(f'block {role} ({length} bytes) is not in {edited}')
            f.seek(where)
            left = length
            while left:
                data = f.read(min(left, 1 << 20))
                if not data:
                    raise ValueError(f'{edited} ended inside block {role}')
                put(data)
                left -= len(data)
            pos = at + length
    put(skeleton.bytes[skel:])
    if skel + skeleton.size - pos != len(skeleton.bytes):
        raise ValueError('skeleton longer than its map')
    return h.hexdigest()


# ---- the section ----

def store_path(top):
    return pathlib.Path(top) / 'skeletons' / 'skeletons.sqlite3'


def encode(sha256, path, identity, skeleton):
    """The record text (section 13.2) of a skeleton of the content sha256,
    found at path with identity, and its compressed bytes."""
    record = dict(sha256=sha256, size=skeleton.size, format=skeleton.format, path=str(path), identity=list(identity),
                  blocks=[list(b) for b in skeleton.blocks], skeleton_bytes=len(skeleton.bytes),
                  skeleton_sha256=hashlib.sha256(skeleton.bytes).hexdigest(), skeleton_format='zstd')
    return json.dumps(record), compress(skeleton.bytes)


def from_record(record, data):
    """A skeleton from a record (section 13.2) and its uncompressed bytes."""
    def number(v):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError('bad number in a skeleton record')
        return v
    blocks = []
    for b in record.get('blocks') or []:
        if not isinstance(b, list) or len(b) != 4:
            raise ValueError('bad block in a skeleton record')
        blocks.append([number(b[0]), number(b[1]), str(b[2]), str(b[3])])
    size, pos = number(record.get('size')), 0
    for at, length, _, _ in blocks:
        if at < pos or at + length > size:
            raise ValueError('blocks out of order in a skeleton record')
        pos = at + length
    return Skeleton(str(record.get('format') or ''), size, blocks, data)


class Store:
    """The skeleton section of an archive, opened for reading."""
    def __init__(self, db):
        self.db = db

    @classmethod
    def open(cls, top):
        """The section of the archive whose top is given, or None when it has none."""
        path = store_path(top)
        if not path.exists():
            return None
        from archive_layout import open_readonly
        return cls(open_readonly(path))

    def count(self):
        return self.db.execute('SELECT count(*) FROM skeletons').fetchone()[0]

    def get(self, sha256):
        """A content's skeleton, its bytes checked against skeleton_sha256; None without one."""
        row = self.db.execute('SELECT record, skeleton FROM skeletons WHERE sha256=?', (sha256,)).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        data = decompress(bytes(row[1]))
        if hashlib.sha256(data).hexdigest() != record.get('skeleton_sha256'):
            raise ValueError(f'skeleton of {sha256} does not match its skeleton_sha256')
        return from_record(record, data)

    def close(self):
        self.db.close()


def unchanged(reference, identity):
    """Whether the file at reference still has the size and modification time
    of the identity a record saw."""
    try:
        st = os.stat(reference)
    except OSError:
        return False
    return bool(identity) and len(identity) == 4 and (st.st_size, st.st_mtime_ns) == (identity[1], identity[2])


@contextlib.contextmanager
def rebuilt(top, reference, sha256):
    """The content sha256, which the file at reference no longer holds, as a
    temporary file rebuilt from the archive's skeleton of it and that file
    (sections 5.5 and 13.3); None when the archive has no skeleton of it."""
    store = Store.open(top)
    if store is None:
        yield None
        return
    try:
        skeleton = store.get(sha256)
    except (ValueError, sqlite3.Error) as e:
        raise ValueError(f'Reference SHA-256 mismatch, and its skeleton cannot be used: {e}') from e
    finally:
        store.close()
    if skeleton is None:
        yield None
        return
    fd, name = tempfile.mkstemp(prefix='dngc-base-')
    try:
        with os.fdopen(fd, 'wb') as sink:
            try:
                got = rebuild(skeleton, reference, sink)
            except (ValueError, OSError) as e:
                raise ValueError(f'Reference SHA-256 mismatch, and its skeleton does not rebuild it: {e}') from e
        if got != sha256:
            raise ValueError('Reference SHA-256 mismatch, and its skeleton rebuilds other bytes')
        yield pathlib.Path(name)
    finally:
        os.unlink(name)

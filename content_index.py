"""Where a content is now, by phindex indexes (docs/dngc-archive-spec.md, 5.5).

A record's reference is a content, named by its SHA-256. When the file at its
recorded path no longer holds it, a phindex index of the tree can say where it
went: the live files it lists with that SHA-256, and the live files that share
an edit-surviving identity (DNG raw blocks, image data) with a row that has
that SHA-256 or that path, from which the content is rebuilt through its
skeleton. An index is a hint and may be stale: the caller checks every file
by SHA-256 before it is used. The Rust library's `phindex::Index` and
`Reader`, and the OCaml library's `Phindex.lookup`, do the same.
"""
import hashlib
import os
import pathlib
import sqlite3
import urllib.parse

# The most files tried per record in each index-aided step, by default.
CANDIDATES = 8

# The identities that survive a metadata edit, as columns of `files`.
IDENTITIES = ('dng_blocks', 'image_data')

HOLDING = 'SELECT path, size, sha256 FROM files WHERE sha256=?1 AND gone=0 LIMIT ?2'


class Index:
    """One phindex index, opened read-only with immutable=1 (nothing is
    written beside it). Every lookup is a point query on an index of `files`;
    a column the database has no index for is not looked up (`unindexed`)."""

    def __init__(self, path):
        path = pathlib.Path(path)
        if not path.is_file():
            raise ValueError(f'no phindex index at {path}')
        name = urllib.parse.quote(os.path.realpath(path), safe='/')
        self.db = sqlite3.connect(f'file:{name}?immutable=1', uri=True)
        names = lambda q: [r[0] for r in self.db.execute(q)]
        columns = names("SELECT name FROM pragma_table_info('files')")
        if not all(c in columns for c in ('path', 'size', 'sha256', 'gone')):
            self.db.close()
            raise ValueError(f'{path} is not a phindex index')
        # The columns an index of files starts with: only those are looked up.
        leading = names("SELECT i.name FROM pragma_index_list('files') l, pragma_index_info(l.name) i WHERE i.seqno=0")
        self.by_sha256 = 'sha256' in leading
        self.unindexed = [] if self.by_sha256 else ['sha256']
        self.identities = []
        for c in IDENTITIES:
            if c in columns:
                (self.identities if c in leading else self.unindexed).append(c)

    def close(self):
        self.db.close()

    def _identity_query(self, key):
        columns = ', '.join(c if c in self.identities else 'NULL' for c in IDENTITIES)
        return f'SELECT {columns} FROM files WHERE {key}=?1 LIMIT ?2'

    @staticmethod
    def _sharing_query(column):
        return f'SELECT path, size, sha256 FROM files WHERE {column}=?1 AND gone=0 LIMIT ?2'

    def holding(self, sha256, limit):
        """Live files (not gone) listed with this SHA-256, at most limit: (path, size, sha256)."""
        if not self.by_sha256:
            return []
        return [(p, s, h or '') for p, s, h in self.db.execute(HOLDING, (sha256, limit))]

    def identity_values(self, sha256, paths, limit):
        """The edit-surviving identities, as (column, value), of the rows
        listed with this SHA-256, gone or not, and of the rows at paths,
        whatever content they list now."""
        out = []
        if not self.identities:
            return out
        def take(query, key):
            for row in self.db.execute(query, (key, limit)):
                for c, v in zip(IDENTITIES, row):
                    if v and (c, v) not in out:
                        out.append((c, v))
        if self.by_sha256:
            take(self._identity_query('sha256'), sha256)
        for p in paths:
            take(self._identity_query('path'), p)
        return out

    def sharing(self, column, value, limit):
        """Live files listed with this identity, at most limit: (path, size, sha256)."""
        if column not in self.identities:
            return []
        return [(p, s, h or '') for p, s, h in self.db.execute(self._sharing_query(column), (value, limit))]

    def plans(self):
        """Each lookup this index answers, with its EXPLAIN QUERY PLAN."""
        queries = []
        if self.by_sha256:
            queries.append(HOLDING)
        if self.identities:
            if self.by_sha256:
                queries.append(self._identity_query('sha256'))
            queries.append(self._identity_query('path'))
        queries += [self._sharing_query(c) for c in self.identities]
        return [(q, '; '.join(r[3] for r in self.db.execute('EXPLAIN QUERY PLAN ' + q, ('', 1)))) for q in queries]


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def listed(indexes, sha256, reference, candidates=CANDIDATES):
    """A file an index lists as holding sha256 that holds it now, or None. A
    row is a hint: a file of another size is skipped unread, any other is
    digested, at most candidates of them."""
    reference, tried = str(reference), []
    for index in indexes:
        for path, size, _ in index.holding(sha256, candidates * 4):
            if len(tried) >= candidates:
                return None
            if path == reference or path in tried:
                continue
            try:
                if not os.path.isfile(path) or os.path.getsize(path) != size:
                    continue
                if _digest(path) == sha256:
                    return pathlib.Path(path)
            except OSError:
                pass
            tried.append(path)
    return None


def edited(indexes, sha256, paths, reference, candidates=CANDIDATES):
    """Files that may be edited versions of the content sha256, last seen at
    one of paths: live files an index lists with an edit-surviving identity
    of a row that has that SHA-256, gone or not, or of the row at one of
    those paths. One file per listed SHA-256, since byte-identical copies
    rebuild alike; at most candidates files."""
    reference, identities = str(reference), []
    for index in indexes:
        for i in index.identity_values(sha256, paths, candidates * 4):
            if i not in identities:
                identities.append(i)
    out, seen = [], []
    for column, value in identities:
        for index in indexes:
            for path, _, listed_sha in index.sharing(column, value, candidates * 4):
                if len(out) >= candidates:
                    return out
                if path == reference or path in out or (listed_sha and listed_sha in seen):
                    continue
                if not os.path.isfile(path):
                    continue
                seen.append(listed_sha)
                out.append(path)
    return out

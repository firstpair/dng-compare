# The dngc archive format

Version `dngc-archive-1`, as written by dng-compare from 2026-11-09 (`e562832`) on.

This document specifies what a dngc archive contains and the rules every tool
that reads or writes one follows. The OCaml front end (`dngc`, `dng-archive`),
the Python reference (`dngc.py`, `preserve_apo.py`, `phindex_metadata.py`,
`metadata_index.py`, `archive_ops.py`) and the Rust comparison
(`dng-compare-all`) implement it and are tested against each other. The DNG
Compare manual explains how to use an archive; this document says what it is.

**MUST**, **MUST NOT** and **MAY** are used as in RFC 2119.

## 1. Terms

- **Source** (LEFT): the tree an archive preserves. **Reference** (RIGHT): the
  tree it is preserved against. Neither is ever written by a tool.
- **Relative path**: a source file's path below the source root, with `/`
  separators and no leading `/`. It is the key of everything the archive holds
  about that file.
- **Identity**: `[inode, size, mtime_ns, ctime_ns]` of a file, integers.
- **Stamp** (report section only): `[device, inode, size, mtime_ns, ctime_ns]`.
- **Record**: the JSON object that says how to rebuild one content of one
  source file (section 5.2).
- **Payload**: a file inside the archive that a record names: a delta or a full
  copy.
- **Version label**: `YYYYMMDDTHHMMSSZ-hhhhhhhhhhhh`, the source's modification
  time in UTC and the first 12 hex digits of its SHA-256; it names an earlier
  content of a path.
- **Drop id**: `YYYYMMDDTHHMMSSZ` in UTC, with `-2`, `-3`, … appended when taken.

## 2. Layout

```text
ARCHIVE/
  ARCHIVE.json            the archive's own record (section 4.1)
  MIGRATION.jsonl         the log of a migration from the legacy layout (4.2)
  report/                 the comparison section (6)
    pairs.jsonl  summary.json  cache.sqlite3  unmatched.json  not-confirmed.json
    .lock
  bytes/                  the byte section (5)
    recovery.sqlite3      records, earlier versions, errors, trash, tombstones
    deltas/               VCDIFF deltas: deltas/<relative path>.vcdiff
    originals/            full copies: originals/<relative path>
    versions/<label>/     payloads of contents preserved after an earlier one
    manifest.jsonl  inventory.json  directories.json  summary.json
    .byte-preservation.lock
  metadata/               the metadata section (7)
    batches/*.jsonl.gz    one row per source file, both sides' records
    differences.sqlite3   the searchable index of the rows
    summary.json  provenance.json
  .metadata-preservation.lock
  skeletons/              the skeleton section (13)
    skeletons.sqlite3     one skeleton per reference content
    skipped.tsv  .lock
  trash/<drop id>/        payloads of dropped records (8)
```

Every section is optional. An archive with only `report/` is a comparison; with
`bytes/` it can rebuild files; with `metadata/` it can be queried. Payload paths
inside records are relative to the byte section, so an archive MAY be moved or
copied as a whole directory.

## 3. Finding the sections

A tool given a path P finds the archive's top and its byte section by one rule:

1. If `P/recovery.sqlite3` exists and `P/bytes` does not, P is a **legacy**
   archive: the byte section is P itself.
2. Otherwise, if the last component of P is `bytes`, the byte section is P and
   the top is its parent.
3. Otherwise the top is P and the byte section is `P/bytes`, created when a
   writer first needs it.

`report/`, `metadata/`, `ARCHIVE.json`, `MIGRATION.jsonl` and `trash/` are
always at the top. A writer MUST print the byte section it chose
(`Byte section: PATH`) before preserving.

## 4. Top-level files

### 4.1 `ARCHIVE.json`

A JSON object, written whole (to `ARCHIVE.tmp`, then renamed), indented by two
spaces.

| Member | Contents |
| --- | --- |
| `schema` | `"dngc-archive-1"` |
| `runs` | one object per `dngc run`: `started`, `finished` (local time, `%Y-%m-%dT%H:%M:%S%z`), `left`, `right` (absolute), `pairs` (the pair file, or `"whole tree"`), `bytes`, `metadata`, `impl`, `index` (two index paths or null), `exit_statuses` (step name to exit status) |
| `migrations` | one object per migration: `migrated` (time), `moved` (`{path, from}` per entry), `report` (`{path, from}` or absent), `summary` (sources, engine and counts at that time) |
| `drops` | one object per drop (section 8): `id`, `time`, `state` (`trashed`, `deleted`, then `restored` or `emptied`), `globs`, `listed` (bool), `records`, `earlier_versions`, `payloads`, `payload_bytes`; later `restored`, `emptied`, `released` times |
| `keeps` | one object per keep (section 8.4): `time`, `globs`, `listed`, `files`, `bytes`, `errors` |

A missing `ARCHIVE.json` reads as `{"schema": "dngc-archive-1", "runs": []}`.
Members other than these MUST be preserved by writers.

### 4.2 `MIGRATION.jsonl`

Present only after `dngc migrate`. One JSON object per line, each written and
fsynced **before** the step it describes:

- `{"op": "begin", "time", "archive", "report", "byte_section"}`
- `{"op": "mkdir", "path": "bytes"}`
- `{"op": "move", "from": NAME, "to": "bytes/NAME"}`, a rename on one volume
- `{"op": "copy", "from": DIR, "to": "report"}`
- `{"op": "done", "time"}`
- for an undo: `undo-begin`, then `{"op": "unmove", "from", "to"}` per move
  reversed, `{"op": "remove", "path"}` for the copied report,
  `{"op": "rmdir", "path": "bytes"}`, and `undo-done`

The entries after the last `begin` give the migration's state: no `done` means
**migrating** (a rerun resumes), `done` means **migrated**, `undo-begin`
without `undo-done` means **undoing**, `undo-done` means **undone**. Only
`migrate` reads this file; once migrated, the same history is in
`ARCHIVE.json`'s `migrations`, and the file MAY be deleted, which leaves
nothing to resume or undo.

## 5. The byte section

### 5.1 `recovery.sqlite3`

An SQLite database. Tables:

```sql
CREATE TABLE files    (path TEXT PRIMARY KEY, record TEXT NOT NULL);   -- current content per path
CREATE TABLE versions (path TEXT NOT NULL, version TEXT NOT NULL, record TEXT NOT NULL,
                       PRIMARY KEY (path, version));                    -- earlier contents
CREATE TABLE errors   (path TEXT PRIMARY KEY, error TEXT NOT NULL);    -- files that could not be preserved
-- created by the first drop (section 8):
CREATE TABLE trash    (drop_id TEXT NOT NULL, state TEXT NOT NULL,     -- 'current' or 'earlier'
                       path TEXT NOT NULL, version TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE dropped  (path TEXT PRIMARY KEY, drop_id TEXT NOT NULL);  -- tombstones
```

`path` is a relative path; `record` is the record's JSON text (5.2), as
`json.dumps` writes it: `", "` and `": "` separators, keys in the order below,
non-ASCII as `\u` escapes.

### 5.2 Records

| Field | Type | Meaning |
| --- | --- | --- |
| `relative_path` | string | the source file's relative path |
| `source` | string | its absolute path when preserved |
| `reference` | string or null | the counterpart's absolute path; null for a full copy without counterpart |
| `source_identity` | identity | of the source when preserved |
| `reference_identity` | identity or null | of the reference when preserved |
| `source_attrs` | object | `mode`, `uid`, `gid`, `mtime_ns`, `atime_ns`, `flags`, `xattrs` (name to base64 value) |
| `source_sha256` | hex | SHA-256 of the source's bytes |
| `reference_sha256` | hex or null | SHA-256 of the reference's bytes |
| `original` | string or null | payload path of a full copy |
| `whole_file_equal` | bool | the reference holds the same bytes |
| `preserved_bytes` | integer | size of the payload; 0 for identical |
| `reconstruction_verified` | bool | the payload was decoded or read back and matched `source_sha256` |
| `delta` | string or null | payload path of a delta (delta records only) |
| `delta_sha256` | hex | SHA-256 of the delta file (delta records only) |
| `delta_format` | string | `"VCDIFF RFC 3284"` (delta records only) |
| `reference_relative_path` | string or null | the counterpart's path below the reference root |

Every record is of exactly one **kind**, decided in this order:

1. **delta**: `delta` is a non-empty string. The source is the reference with
   the delta applied.
2. **full**: `original` is a non-empty string. The source is that payload.
3. **identical**: neither. `whole_file_equal` is true and `source_sha256` equals
   `reference_sha256`; the source is the reference's bytes.

A writer MUST NOT add a record whose `reconstruction_verified` is false: a delta
counts only after it has been decoded again and the result's SHA-256 matched
`source_sha256`. An empty source is recorded as an identical or full record of
zero bytes (SHA-256 `e3b0c442…`); a damaged source is preserved as it is.

### 5.3 Payloads

- A delta for relative path R is `deltas/R.vcdiff`; a full copy is
  `originals/R`. When R already has a record of other content, that earlier
  record moves to `versions` with its payload where it was, and the new
  content's payload goes under `versions/<label>/` (for example
  `versions/<label>/deltas/R.vcdiff`, the label being the new content's), so no
  payload is ever overwritten.
- Deltas are VCDIFF (RFC 3284), encoded against the reference file as source
  window. dng-compare encodes with
  `xdelta3 -e -1 -S none -n -a -D -R -A -B 16777216 -W 8388608 -s REFERENCE SOURCE OUT`;
  any VCDIFF decoder that accepts the reference as source window rebuilds it
  (`xdelta3 -d -s REFERENCE DELTA`).
- A payload file is written to a temporary name, fsynced and renamed into
  place, and never changed afterwards.

### 5.4 Other byte-section files

- `manifest.jsonl`: every record written, one JSON object per line, in the
  order written: an append-only log of the same records `files` holds.
- `inventory.json`: a JSON list of the relative paths the last run covered.
- `directories.json`: a list of `{path, attributes}` for the source's
  directories (mode, owner, times, flags, xattrs), to restore a tree.
- `summary.json`: the last run's totals (`total_files`, `verified_files`,
  `errors`, `complete`, `byte_preservation_complete`, `preserved_payload_bytes`,
  `byte_different_files`, `missing_sources`, …). Informative; `files` is
  authoritative.

### 5.5 Restoring a content

To restore relative path R (or its earlier content `--version LABEL`, from
`versions`), a reader MUST:

1. Refuse if the output exists; write to `OUTPUT.recovery-partial`.
2. For a **delta** record: check that SHA-256 of `reference` equals
   `reference_sha256`, then decode the delta (5.3) against it. For a **full**
   record: read the byte section's `original`. For an **identical** record: read
   `reference`.
3. Check that the SHA-256 of what was written equals `source_sha256`; on a
   mismatch, remove the partial file and fail.
4. fsync, hard-link the partial file to the output, remove the partial name.
5. Restore `source_attrs`: mode, times, flags and extended attributes.

A reference tree that has moved MAY be given (`--reference-root`); the
reference is then found at `reference_relative_path` below it.

A record's reference is a **content**, named by its SHA-256
(`reference_sha256`; for an identical record, `source_sha256`); `reference` is
where that content was last seen. When the file there no longer holds it and
the archive has a skeleton of that content (section 13), a reader SHOULD rebuild
the content from the skeleton and that file, check its SHA-256, and use the
result as the reference in step 2. A reader without section 13 fails at step 2
or 3 as before. For an identical record, a reader MAY take a reference whose
size and modification time equal `reference_identity` as unchanged and read it
directly; step 3 still decides.

A reader MAY also be given indexes of file trees (phindex databases, which
list each file's path, SHA-256 and image identities) to find a content whose
file was moved or renamed. It then looks for the content in this order and
uses the first that succeeds: the file at `reference`; a file an index lists
as present with that SHA-256; the skeleton rebuilt from the file at
`reference`; the skeleton rebuilt from a file an index lists with the same
edit-surviving identity (DNG raw blocks, or image data) as a row that has
that SHA-256 or that path. An index is only a hint and may be stale: a file
it names is used only after its SHA-256, or that of the content rebuilt from
it, has been checked to be the content's. A reader tries a bounded number of
such files per record, never searches a tree, and otherwise fails as it does
without an index. The bound is the reader's own and SHOULD be settable. The
Rust library's default is 8 files in each of the two index steps (8 files
digested for the SHA-256 lookup, a listed file of another size being skipped
unread and not counted, and 8 edited files a skeleton rebuild is tried from),
reading four times as many index rows per lookup; `Reader::with_candidates`
sets another, and 0 turns the index steps off.

## 6. The comparison section (`report/`)

- `pairs.jsonl`: one canonical JSON object per pair (sorted keys, no spaces):
  `left`, `right` (absolute paths), `stamps` (two stamps; absent when the pair
  failed before its files were stamped, which `error` then explains), `raw` and
  `auxiliary` (a `status`: `equal`, `different`, `unverified` or
  `not_compared`, with a `method` such as `encoded_blocks`, `phindex_sha256`
  or `phindex_dng_blocks`, and per-image `images` entries with SHA-256s and
  shapes), `metadata_status` (`ok`, `skipped`, `error`), and, when compared,
  `differences` and `metadata_warnings`; `error` when a pair failed.
- `cache.sqlite3`: `CREATE TABLE results (key TEXT PRIMARY KEY, stamps TEXT, result TEXT)`.
  `key` is the canonical JSON `[version, left, right]`; `stamps` the canonical
  JSON of both stamps; `result` the pair's object. A cached result is reused
  only when both files' stamps are unchanged **ignoring the device number**
  (macOS renumbers a volume on every mount) and, for a run that compares
  metadata, only if the cached result is not `metadata_status: skipped`.
- `summary.json`: the comparison's totals (`counts`: `pairs_reported`,
  `raw_equal`, `auxiliary_equal`, `metadata_ok`, …), its roots and options,
  and `complete`.
- `unmatched.json`, `not-confirmed.json`: listed files without a pair, and
  pairs whose equality could not be confirmed.

## 7. The metadata section (`metadata/`)

### 7.1 Batches

`batches/<name>.jsonl.gz`: gzip, one JSON object per line (`json.dumps` with
ASCII escapes). Names are `NNNNNN.jsonl.gz` (ExifTool snapshots) or
`phindex-NNNNNN.jsonl.gz` (from phindex indexes), numbered upward; a batch file
is written whole, read back and compared before it is renamed into place, and
never changed afterwards.

The row fields `apo` and `bumer` are historical names: **`apo` is always the
source (LEFT) and `bumer` the reference (RIGHT)**, whatever the volumes are
called.

| Field | Meaning |
| --- | --- |
| `relative_path` | the source's relative path |
| `version` | present for a later content of a path already recorded: the run's UTC stamp |
| `apo`, `bumer` | absolute paths of source and reference (`bumer` null without counterpart) |
| `apo_stamp`, `bumer_stamp` | identities, or null when the file could not be read |
| `apo_filesystem` | the source's `mode`, `uid`, `gid`, `mtime_ns`, `atime_ns`, `ctime_ns`, `birthtime`, `flags`, `xattrs` |
| `apo_metadata`, `bumer_metadata` | each side's metadata record (7.2), or `{}` |
| `differences` | `{tag, kind, apo?, bumer?, category}` per differing tag; `kind` is `changed_in_bumer`, `added_in_bumer` or `missing_in_bumer`; `category` is `embedded` or `filesystem` (`File:` tags) |
| `errors`, `warnings` | `{side, error}` and `{side, warning}` lists |
| `no_metadata` | present when a side's file type is not read by the engine: the sides, e.g. `["apo", "bumer"]` |
| `same_bytes_sha256` | present when both sides have this SHA-256: the records are omitted (`{}`) and there are no differences |
| `ignored_system_file` | true for a `.DS_Store` row, which carries nothing else |

A tag named `SourceFile`, or beginning with `ExifTool:` or `Composite:`, is not
compared.

### 7.2 Metadata records and engines

A metadata record is ExifTool's JSON for one file (`-G0:1:4 -n -struct`,
duplicates kept). Perl ExifTool is the engine of record. A record from the
exiftool-rs engine carries `ExifTool:DngCompareEngine` (for example
`"exiftool-rs 0.12.0 (ExifTool 13.59 tables)"`) and is a different identity:
an archive's records MUST all come from one engine, and a writer MUST refuse to
add records of another. Records taken from phindex indexes replace every string
longer than 1 KiB by `long-sha256:<hex>:<length>`.

Only files whose extension the engine reads get a record: Perl ExifTool's
`exiftool -listf` list, or exiftool-rs's table
(`metadata-extensions-exiftool-rs.txt`). Other files are listed under
`no_metadata`, never as errors.

### 7.3 `differences.sqlite3`

The index of the batches; it can always be rebuilt from them (`dngc index`).

```sql
CREATE TABLE batches (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL, version TEXT NOT NULL DEFAULT '',
                    extension TEXT NOT NULL, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE,
                    compared INTEGER NOT NULL, errors INTEGER NOT NULL, UNIQUE (path, version),
                    -- added by ALTER TABLE, in this order:
                    apo_size INTEGER, apo_mtime_ns INTEGER, no_metadata INTEGER, same_bytes INTEGER);
CREATE TABLE fields (id INTEGER PRIMARY KEY, tag TEXT UNIQUE NOT NULL, category TEXT NOT NULL);
CREATE TABLE vals (id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, json_zlib BLOB NOT NULL, json_bytes INTEGER NOT NULL);
CREATE TABLE differences (file_id INTEGER REFERENCES files(id) ON DELETE CASCADE, field_id INTEGER REFERENCES fields(id),
                          kind TEXT NOT NULL, apo_value INTEGER REFERENCES vals(id), bumer_value INTEGER REFERENCES vals(id),
                          PRIMARY KEY (file_id, field_id));
CREATE TABLE coverage (batch TEXT REFERENCES batches(name) ON DELETE CASCADE, field_id INTEGER REFERENCES fields(id),
                       extension TEXT, apo_present INTEGER, bumer_present INTEGER, PRIMARY KEY (batch, field_id, extension));
-- created by the first drop:
CREATE TABLE trash_files (drop_id TEXT NOT NULL, path TEXT NOT NULL, row TEXT NOT NULL, differences TEXT NOT NULL);
-- views: field_coverage, change_counts, field_changes
```

- A batch is indexed once per content: `batches.sha256` is the SHA-256 of the
  batch file's bytes; a batch already indexed with that digest is skipped.
- A row yields one `files` row (skipped for `.DS_Store`); `compared` is 1 when
  it has a counterpart and no errors; `errors` counts its errors.
- `vals` holds each distinct value once, as zlib-compressed canonical JSON
  (sorted keys, no spaces) keyed by its SHA-256.
- `coverage` counts, per batch, field and extension, how many records hold the
  tag on each side; byte-identical rows add nothing to it.
- `apo_size`, `apo_mtime_ns`, `no_metadata`, `same_bytes` let a resumed run
  tell recorded pairs without parsing batches. A NULL `no_metadata` marks a row
  indexed before these columns.

### 7.4 `summary.json` and `provenance.json`

`summary.json` holds the section's totals (`files_processed`,
`metadata_errors`, `files_without_metadata`, `files_byte_identical`,
`files_with_embedded_differences`, `common_changes`, …). `provenance.json`
names the tool, the `engine`, the source and reference, and for phindex input
the two indexes and the pair list.

## 8. Drop, trash, tombstones, keep

### 8.1 Drop

A drop removes the current records of selected relative paths, and all their
earlier versions, from the byte section:

- `drop --yes` moves them: each row goes to `trash` (`state` `current` or
  `earlier`, `version` `''` for current), each payload is renamed from
  `bytes/P` to `trash/<drop id>/P`, and each path's metadata index rows go to
  `trash_files` (`row`: the `files` row as a JSON object of column to value;
  `differences`: a JSON list of `[field_id, kind, apo_value, bumer_value]`).
- `drop --force` deletes them instead.
- Both write a **tombstone** per path in `dropped`, and append the drop to
  `ARCHIVE.json`.

The database changes are committed before payloads move; a reader MUST look
for a trashed payload under `trash/<drop id>/` and, failing that, in its
original place.

### 8.2 Tombstones

A run MUST NOT preserve a source file whose relative path has a tombstone; it
reports how many listed files it skipped. `dngc index` MUST remove the metadata
rows of tombstoned paths after rebuilding the index. `restore` of a tombstoned
path MUST fail, naming the drop.

### 8.3 Trash

- `trash restore ID` (drop state `trashed`): refuses if any path has a newer
  current record; otherwise moves the payloads back, re-inserts the rows into
  `files` and `versions` and the metadata rows (with new ids), deletes the
  drop's `trash` rows and tombstones, removes `trash/ID/`, and sets the state
  to `restored`.
- `trash empty [ID] --yes`: deletes the drop's `trash` and `trash_files` rows
  and `trash/ID/`; the tombstones stay. State `emptied`.
- `trash release ID`: deletes the drop's tombstones, so later runs preserve
  those paths again.

### 8.4 Keep

`keep --yes` turns identical and delta records into full records: it restores
each file (5.5) to `bytes/originals/<relative path>`, sets `original` to that
path and `preserved_bytes` to its size, sets `delta`, `delta_sha256` and
`delta_format` to null where present, updates `files`, deletes the delta it
replaced, and appends to `keeps`. Afterwards the record no longer depends on
the reference.

## 9. Concurrency and reading

- Writers hold an exclusive `flock` on the lock file of what they write:
  `bytes/.byte-preservation.lock` (legacy: `.byte-preservation.lock` at the
  top), `.metadata-preservation.lock` (the metadata section),
  `report/.lock` (the comparison). `migrate`, `drop`, `trash` and `keep` take
  every lock that exists, without waiting, and refuse if one is held.
- A reader MUST NOT change an archive. It opens a database with SQLite's
  `immutable=1` when no writer holds its lock and its `-wal` file is absent or
  empty, and otherwise reads a private copy of the database and its WAL. A plain
  read-only open of a WAL database would create `-wal` and `-shm` files.
- Databases MAY be in WAL or rollback-journal mode.

## 10. Guarantees and verification

An archive written by these rules guarantees:

1. **Exact rebuild.** Every record rebuilds its source byte for byte, checked
   against `source_sha256`, given its payload and, for identical and delta
   records, the reference file with `reference_sha256`.
2. **No overwriting.** A changed content of a path becomes a new current record
   and the old one an entry of `versions`; payloads are never rewritten.
3. **No silent loss.** Records leave only by an explicit drop, which can be
   undone until the trash is emptied, and is logged in `ARCHIVE.json`.
4. **Reading changes nothing.**
5. **Faithfulness.** A file is preserved as it is: empty files as empty,
   damaged files damaged.

`dngc verify` checks that the byte section has no errors, that every payload a
current or earlier record names exists, that every trashed record's payload
exists, that `differences.sqlite3` passes `PRAGMA quick_check` (on a local copy
when it is larger than 256 MB), and that the comparison is complete;
`--restore-sample N` restores N records at random by 5.5.

## 11. Compatibility

- A **legacy** archive (byte section at the top, section 3) is read as it is;
  `dngc migrate` moves it into `bytes/` by renames, logged in `MIGRATION.jsonl`.
- A metadata index without the resume columns gains them on first open; their
  values are filled in when a resumed run parses those batches.
- Tables `trash`, `dropped` and `trash_files` exist only once something has been
  dropped; readers MUST treat their absence as empty.
- A future, incompatible format will change `schema`. The current tools do not
  yet check it.

## 12. Conformance

`conformance/validate_archive.py ARCHIVE` checks an archive against this
document and names the rule (section) each problem breaks. It is written from
this document alone and imports nothing from the implementations; `--rebuild`
also rebuilds every record from its reference, `--sample N` only N of them.

`conformance/corpus/` holds small archives built by `conformance/make_corpus.py`,
valid and broken, each with `expected.json`, the rules the validator must
report. Each language checks its own implementation against them, reading with
its own code and checking what it writes with the validator:

| Suite | Reads the corpus with | Writes, then validates |
| --- | --- | --- |
| `test_conformance.py` | both front ends: `ls`, `restore` of every record, `verify`, refusals; `dngc.py` on the skeleton cases: every record restored through its skeleton, an unsound skeleton or a changed block refused, the errors of 5.5 without a skeleton | `dngc run` with each implementation; the native metadata step |
| `test_skeleton.py` | `skeleton.py`'s walkers and rebuilding on synthetic TIFFs and JPEGs, against the validator's walkers | when `dngc-skeleton` is built: the rows it writes are those `skeleton.py` makes, and validate |
| OCaml `ocaml-archive/test/conformance.ml` (`dune test`) | `Preserve.byte_section`, `reconstruct`, `label_of`; `Dngc_cli` verify and restore; `Skeleton` and restores after every reference is edited (13) | `dngc run --impl ocaml`; `Phindex_metadata.run`; `dngc skeleton build`, compared with `dngc-skeleton`'s section |
| Rust library `dng-archive` (`rust-archive/`, `cargo test`) | `Reader`: `sections`, records, `restore` of every record, `version_label`, tombstones | `dng-compare-all`, built on it, on synthetic DNG pairs (`rust-scanner/tests/conformance_writer.rs`) |

`make conformance` runs the three in turn (they share
`/private/tmp/dngc-conformance`). A change to this format changes this
document, the validator and the corpus together.

## 13. The skeleton section (`skeletons/`)

Identical and delta records need their reference's exact bytes. An edit that
writes metadata into a reference file (a rating, develop settings such as
exposure or blacks, keywords, IPTC) changes those bytes, though it rewrites only
the file's small structures and carries its large blocks over unchanged, often
at other offsets. A **skeleton** keeps what such an edit destroys, so the
content before the edit can be rebuilt from the file after it.

The section is optional and holds no image data. It is written while the
reference files still hold the contents the records name.

### 13.1 Blocks and the skeleton

A file is split by its own structure into **blocks**, which are not stored, and
the **skeleton**: every byte outside the blocks, concatenated in file order.

Classic TIFF, including DNG and TIFF-based raws (format `tiff`). All IFDs are
walked breadth-first: the chain from the header, and the IFDs that SubIFDs
(330), Exif (34665), GPS (34853) and Interoperability (40965) point at. An IFD
reached twice is walked once. The first IFD's role is `0`; the n-th next IFD of
a chain adds `+n` (`0+1`); the k-th IFD a pointer tag T of IFD R points at is
`R/T.k` (`0/330.0`). Offsets and counts are read from BYTE, SHORT, LONG and IFD
values only; of a tag that appears twice in an IFD the first entry counts.
Candidate blocks of an IFD with role R:

- each strip (273/279) or tile (324/325): `R/strips`, `R/tiles`;
- the JPEG stream (513/514): `R/jpeg`;
- any other value stored out of line of 512 bytes or more: `R/tag<number>`.
  Never a block: XMP (700), IPTC (33723), Photoshop resources (34377), the four
  pointer tags, and the offset and count arrays 273, 279, 324 and 325.

A candidate of zero length, or reaching past the end of the file, is dropped.
Then:

- a run of 65,536 bytes or more that is outside the 8-byte header, every IFD
  (its count, entries and next-IFD offset), every out-of-line value that lies
  within the file, and every candidate, is a candidate too: `gap`. A run at the
  end of the file counts.

JPEG (format `jpeg`). After the SOI marker, segments are walked while each
starts with 0xFF, a marker that takes a length, and lies within the file; a
0xFF fill byte before a marker is skipped. The walk stops at a start of scan
(0xDA), at markers 0x00, 0x01 and 0xD0–0xD9, and at anything else that is not
such a segment.

- A segment whose full length, marker included, is 512 bytes or more, other
  than APP1, APP13 and COM: `seg<marker in two hex digits>` (`sege2`).
- Everything from where the walk stopped to the end of the file, when not
  empty: `scan`.

For both formats the candidates are sorted by offset, then length, then role;
duplicates are dropped, and so is a candidate that starts before the end of the
last one kept. A `…/strips` or `…/tiles` candidate that follows a kept block of
the same role by no more than 3 bytes is merged into it, the bytes between them
included. What remains are the blocks, in file order, not overlapping.

A file that is neither, or whose walk fails, and is no larger than 1 MiB is its
own skeleton, with no blocks (format `whole`). A larger one has no skeleton. A
skeleton without blocks is its content, whatever its format.

A block's **mark** is the first 32 hex digits of the SHA-256 of the block's
first and last 4,096 bytes (of the whole block when it is no longer than 8,192
bytes).

### 13.2 `skeletons.sqlite3`

```sql
CREATE TABLE skeletons(sha256 TEXT PRIMARY KEY, record TEXT NOT NULL, skeleton BLOB NOT NULL);
```

`sha256` is the content's SHA-256; `skeleton` is the skeleton's bytes as one
zstd frame, at any compression level; `record` is JSON in the form of section 5.2's records:

| Field | Type | Meaning |
| --- | --- | --- |
| `sha256` | hex | SHA-256 of the content |
| `size` | integer | its size |
| `format` | string | `tiff`, `jpeg` or `whole` |
| `path` | string | the absolute path of the file the skeleton was read from |
| `identity` | identity | of that file then |
| `blocks` | list | `[offset, length, mark, role]` per block, in file order |
| `skeleton_bytes` | integer | size of the skeleton before compression |
| `skeleton_sha256` | hex | SHA-256 of the skeleton before compression |
| `skeleton_format` | string | `"zstd"` |

A writer takes the content's SHA-256 from a record that names the file
(`reference_sha256`, or `source_sha256` of an identical record) only while the
file's size and modification time equal the record's `reference_identity`; it
reads the file's structures, the bytes outside its blocks and each block's two
ends. A file that fails this check gets no skeleton and is listed in
`skipped.tsv` (reason, path, detail). A content keeps the first skeleton
written for it. A finished section is left in rollback-journal mode.

### 13.3 Rebuilding a content from an edited file

Given a skeleton and a later version of its file, a reader MUST:

1. Find the later file's blocks by 13.1 and compute their marks.
2. Write the content in order: the skeleton's bytes between blocks, and for
   each block the bytes of the first block of the later file, in file order,
   with the same length and mark. If a block has none, fail.
3. Check that the SHA-256 of what was written equals `sha256`. A mark covers
   only a block's ends, so this check is what makes the result exact.

A `whole` skeleton is the content itself and needs no later file.

### 13.4 What a skeleton does not cover

A block the edit itself changed is gone: a regenerated embedded preview
("Update DNG Previews"), re-encoded or converted image data, a truncated file.
Rebuilding then fails at step 2 or 3 and nothing wrong is written. Such a
content can only be kept by preserving it from a copy that still holds it,
before that copy is updated.

On 1,494 DNG and 400 JPEG pairs taken at random from an archive of files edited
by Lightroom Classic and Photo Mechanic, every DNG and 399 JPEGs were rebuilt
exactly from the edited file (the other JPEG was truncated in one copy);
skeletons averaged 6.5 KB per DNG and 9.7 KB per JPEG after compression.

The Rust library implements this section (`dng_archive::skeleton`, and
`Reader` for 5.5); `dngc-skeleton build ARCHIVE` writes it. The OCaml library
implements it too (`Skeleton`, and `Preserve.reconstruct` for 5.5); the native
`dngc skeleton build ARCHIVE` writes it. The Python reference reads it and does
not write it (`dngc.py skeleton` runs the Rust tool): `skeleton.py` has the
same walkers and `preserve_apo.py` restores through it. In all three, `restore`,
`keep` and `verify --restore-sample` go to the skeleton when a delta's
reference fails its SHA-256 or an identical record's reference has another size
or modification time; `dngc verify` reports the number of skeletons and runs
`PRAGMA quick_check` on the database, decoding none.

The validator checks the section with walkers of its own, under the rules
`skeleton-tables`, `skeleton-record`, `skeleton-bytes` (13.2),
`skeleton-blocks` (13.1) and `skeleton-rebuild` (13.3): a skeleton without
blocks must be its content; with `--rebuild` (`--sample N`: for N of them)
each other content is rebuilt from the file now at `path`, when there is one,
and a record whose reference has changed is rebuilt from its skeleton, a
`reference` problem when that fails. The corpus cases `skeletons` and
`skeleton-*` hold such archives, their reference files edited after the
skeletons were written.

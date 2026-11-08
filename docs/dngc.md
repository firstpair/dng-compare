# dngc: one command

`dngc` puts dng-compare's comparison, preservation and metadata tools behind
six verbs that work on one kind of archive. It exists twice, with the same
verbs, options, defaults, archive layout, messages and exit statuses:

| Front end | Where | How it does the work |
| --- | --- | --- |
| **Native binary** (default) | `ocaml-archive/`, `bin/dngc.exe` of a dune build; installed as `dngc` next to `dng-archive` | calls the `dng_archive` OCaml library in its own process |
| `dngc.py` (reference) | the repository root | runs the tools as programs: `dng-archive`, `dng-compare-all`, the Python modules |

`./dngc` runs the native binary when the checkout has a build of it
(`DNGC_NATIVE`, else the OxCaml build `ocaml-archive/_build-5.4.0+ox/`, else any
other `ocaml-archive/_build*/`), and otherwise `dngc.py` with the checkout's uv
environment (`make setup`). `DNGC_FRONTEND=python ./dngc ...` always runs
`dngc.py`. Link `./dngc` into a directory on `PATH` to call it from anywhere.
`test_dngc.py` runs every case against both front ends and requires the same
output from `status`, `verify`, `query` and usage errors on the same archive.
See [archives-and-tools.md](archives-and-tools.md) for the underlying tools,
which keep working on their own.

```sh
./dngc run LEFT RIGHT ARCHIVE --pairs PAIRS.tsv     # compare + preserve, one pass per pair
./dngc status ARCHIVE                               # what the archive holds
./dngc restore ARCHIVE RELATIVE_PATH OUTPUT         # rebuild a file byte for byte
./dngc versions ARCHIVE [RELATIVE_PATH]             # current and earlier contents of each path
./dngc restore ARCHIVE REL OUTPUT --version LABEL   # rebuild an earlier content
./dngc query ARCHIVE                                # most frequent metadata changes
./dngc query ARCHIVE files EXIF:ExifIFD:LensModel   # files where a tag changed
./dngc query ARCHIVE values EXIF:ExifIFD:LensModel  # with old and new values
./dngc query ARCHIVE "SELECT ..."                   # any SQL on the index
./dngc index ARCHIVE                                # rebuild the metadata index from its batches
./dngc verify ARCHIVE [--restore-sample N]          # records, files and index; restore N at random
./dngc migrate ARCHIVE [--dry-run] [--report DIR]   # move a legacy archive's byte section into bytes/
./dngc migrate ARCHIVE --undo                       # move it back
```

## Building the native binary

It builds with stock OCaml (the `default` switch, OCaml 5.2.0) and with OxCaml
(the `5.4.0+ox` switch), each into its own build directory:

```sh
DUNE_BUILD_DIR=_build-5.4.0+ox make -C ocaml-archive DUNE="opam exec --switch 5.4.0+ox -- dune"
DUNE_BUILD_DIR=_build-default  make -C ocaml-archive DUNE="opam exec --switch default -- dune"
```

The binary finds the checkout (for `--impl python`, `--impl rust`, the
decoder fallback of `all`, and the sibling `phindex` checkout) as `dng-archive
all` does: `DNG_COMPARE_DIR`, else the nearest directory above the executable
that holds `compare_dng.py`. An installed copy outside the checkout needs
`DNG_COMPARE_DIR` for those.

## What each verb runs

| Verb | Native binary | `dngc.py` |
| --- | --- | --- |
| `run`, DNG pairs, `--impl ocaml` | `All.run_options` (the `dng-archive all` code) in process | `dng-archive all` |
| `run`, DNG pairs, `--impl rust` | `dng-compare-all` | `dng-compare-all` |
| `run`, DNG pairs, `--impl python` | `compare_dng.py` | `compare_dng.py` |
| `run`, other and reference-less pairs | `Preserve.scan` (the `dng-archive preserve` code) in process; `preserve_apo.py` with `--impl python` | `preserve_apo.py scan --delta --pairs` |
| `run --metadata phindex` | `Phindex_metadata` in process with `--impl ocaml`, else `phindex_metadata.py` | `dng-archive phindex-metadata` with `--impl ocaml`, else `phindex_metadata.py` |
| `run` without `--pairs` | the walk in OCaml; `phindex align` with `--index` | the walk in Python; `phindex align` with `--index` |
| `restore` | `Preserve.restore` | `preserve_apo.py restore` |
| `versions` | in OCaml | in Python |
| `index` | `Index.index_archive` | `dng-archive index`, else `metadata_index.py` |
| `query`, `status`, `verify` | in OCaml on the archive's SQLite files | in Python |
| `verify --restore-sample` | `Preserve.restore` | `preserve_apo.py restore` |
| `migrate` | in OCaml (renames) | in Python (renames) |

Every step prints a `+ ` line before it starts. For a program it is the
command line, as with `dngc.py`; for a step the native binary runs in its own
process it is the equivalent `dng-archive` command (`dng-archive preserve`
takes `--source-index`/`--reference-index` for this).

Differences of the native binary from `dngc.py`:

- **Paths given to programs are absolute.** `dngc.py` passes LEFT, RIGHT and
  the indexes as given and runs the programs in the checkout directory, so a
  relative path there resolves against the checkout; the native binary
  resolves them against the working directory.
- **Errors inside an in-process step** print `dngc: MESSAGE` and give that
  step status 1, where the program would have printed its own message or a
  Python traceback; usage errors of `all` and `phindex-metadata` keep their
  texts and status 2.
- **Other pairs, with `--impl ocaml` or `rust`,** are preserved by the OCaml
  code, not `preserve_apo.py`. Records are the same; `bytes/summary.json` says
  `Listed pairs; every candidate is byte-verified.` under `matching`, and a
  pair without a reference falls back to the same relative path under RIGHT
  only, not to `preserve_apo.py`'s legacy TSV hints.
- **Read-only SQLite leaves nothing behind**, in both front ends.
- An interrupt (Ctrl-C) between steps gives status 130.

**Reading leaves the archive as it was.** A plain `?mode=ro` open of a closed
WAL database creates its `-wal` and `-shm` files. `status`, `verify`, `query`,
`versions`, `restore` (also `dng-archive restore` and `preserve_apo.py
restore`) and `migrate`'s verify therefore open `recovery.sqlite3` and
`metadata/differences.sqlite3` with `immutable=1` when no writer holds the
archive's lock (`.byte-preservation.lock` in the byte section,
`.metadata-preservation.lock` at the top) and the `-wal` is empty or absent,
and otherwise read a private copy of the database and its WAL
(`archive_layout.open_readonly`, `Preserve.readonly_db`). An archive on the
Turso engine is read through its engine.

## The archive

One directory, with the sections a run produced:

| Path | Section | Written by |
| --- | --- | --- |
| `report/` | comparison: `pairs.jsonl`, `summary.json`, `cache.sqlite3` | the comparison step |
| `bytes/` | preservation: `recovery.sqlite3`, `deltas/`, `originals/`, manifests | the preservation step |
| `metadata/` | searchable metadata: `batches/`, `differences.sqlite3`, `provenance.json` | `--metadata phindex`, `dngc index` |
| `ARCHIVE.json` | every run's options, sources, implementation and exit statuses | `dngc run` |

An archive written before `dngc`, with `recovery.sqlite3` at the top, is read as
it is: its top level is its byte section. The rule below decides, for `dngc`
and every other tool.

## Archive layout: where the byte section is

Every tool that writes or reads preserved bytes finds the byte section of an
archive by one rule (`archive_layout.py`; `Preserve.byte_section` in
`ocaml-archive`; `archive::byte_section` in the Rust `dng-compare-all`):

1. if `ARCHIVE/recovery.sqlite3` exists and `ARCHIVE/bytes` does not, the byte
   section is `ARCHIVE` itself (a legacy archive);
2. else, if the path's last component is `bytes` (`ARCHIVE/bytes` given
   directly), it is that path, never a nested `bytes/bytes`;
3. else it is `ARCHIVE/bytes`, created for a new or empty archive.

The metadata section (`metadata/`), `provenance.json`, `runs.jsonl`, Semantic
Unison's `pairs-*.jsonl` and their logs, `report/` and `ARCHIVE.json` stay at
the archive's top; given `ARCHIVE/bytes`, the metadata tools (`index`,
`phindex-metadata`) and `dngc` take its parent as the top. Writers print the
byte section they chose (`Byte section: PATH`) before preserving.

| Tool | Uses the rule for |
| --- | --- |
| `dng-archive preserve`, `restore` | the archive argument |
| `dng-archive all --archive`, `dng-compare-all --archive`, `compare_dng.py --archive` | the archive (default `REPORT/archive`, so `REPORT/archive/bytes`) |
| `dng-archive index`, `metadata_index.py`, `dng-archive phindex-metadata`, `phindex_metadata.py` | the top (parent of `ARCHIVE/bytes`) |
| `preserve_apo.py scan`, `restore` | the archive argument; its checks and `.byte-preservation.lock` are in the byte section |
| `dngc` (both front ends) | every verb |

The Python API `preserve_apo.scan(...)` writes where it is told, as the year
workflows (`preserve_years.py`, `finalize_year.py`) expect; only the command
lines apply the rule. Existing legacy archives resolve to their top and keep
working unchanged; `dngc migrate` moves one into `bytes/`.

## `dngc run`

| Option | Values | Default | Meaning |
| --- | --- | --- | --- |
| `--pairs FILE` | `LEFT<TAB>RIGHT` lines | the whole tree | the pairs to process, e.g. `phindex align`'s pair files |
| `--bytes` | `delta`, `full`, `none` | `delta` | preserve each left file as a verified delta against its right file; `none` only compares |
| `--metadata` | `exiftool`, `phindex`, `none` | `exiftool` | compare metadata with Perl ExifTool; take it from phindex indexes (also writing the searchable section); or skip it |
| `--index L R` | phindex indexes | none | settle unchanged pairs without reading them; needed by `--metadata phindex` |
| `--impl` | `ocaml`, `rust`, `python` | `ocaml` (`dng-archive`, OxCaml build) | which implementation runs the pairs |
| `--workers N` | | a quarter of the cores | pairs at a time; also ExifTool processes |
| `--max-mib R` | MiB/s | no limit | throttle reads, e.g. on a single hard disk |

- **Without `--pairs`, the whole LEFT tree is covered:** every regular file
  (symlinks and `.DS_Store` aside) is paired with its RIGHT counterpart and kept
  as a reference or delta; a file with none is kept as a full copy. With
  `--index`, `phindex align` pairs files by content, so moved and renamed files
  find their counterpart (LEFT must be fully indexed); otherwise the same
  relative path is tried, then a file name unique under RIGHT.
- DNG pairs go through the one-pass `all` command (or `compare_dng.py`); other
  listed pairs (JPEG, TIFF, raws) are preserved by the preservation code
  (`dng-archive preserve`'s in the native binary, `preserve_apo.py` in
  `dngc.py` and with `--impl python`).
- A repeated run reuses everything still valid: cached comparisons, preserved
  records, metadata batches. It trusts a pair list it checked before.
- `--metadata phindex` runs the comparison without ExifTool and writes the
  metadata section from the indexes' exiftool-rs records (values over 1 KiB as
  `long-sha256` references). Archives never mix metadata engines.
- Not yet wired: `--bytes full`, year archives, and `--metadata exiftool-rs`. Use the underlying tools for
  those (`preserve_apo.py --full-copy`, `preserve_years.py`, `portable_metadata.py`).

## `dngc verify`

Checks that the byte section has no errors and that every delta and original
its records name exists, that the metadata index passes SQLite's `quick_check`,
and that the comparison finished. `--restore-sample N` also restores N records
at random into a scratch directory, which checks their SHA-256. Exit status 0
means no problems.

## `dngc migrate`: a legacy archive into the unified layout

    dngc migrate ARCHIVE [--dry-run] [--report DIR] [--undo] [--no-verify]

A legacy archive keeps its byte section at the top. `migrate` moves exactly
the entries the byte tools write there into `ARCHIVE/bytes/`, by renames on the
same volume; it never copies and deletes. Those entries are `recovery.sqlite3`
with its `-wal`, `-shm`, `-journal`, `.engine.json`, `.engine.tmp` and
`.before-turso` files, `deltas/`, `originals/`, `manifest.jsonl`,
`inventory.json`, `directories.json`, `summary.json` (and the `.tmp` files of
the last three), `matching.json`, `xmp-original/`, `image-comparison/`,
`finalization-backend.json` and `.byte-preservation.lock`. Everything else
stays at the top: `metadata/`, `provenance.json`, `runs.jsonl`,
`verification.json`, `deletion-readiness.json`, `comparison.csv`, `tools/`,
`README.md`, `prior-evidence/`, logs, pair lists, `.metadata-preservation.lock`.
Records name their deltas and originals relative to the byte section, so
nothing inside is rewritten: `dngc restore ARCHIVE ...` and
`dng-archive restore ARCHIVE/bytes ...` work as before.

It refuses (status 1) when:

- a tool lock is held: `.byte-preservation.lock`, `.metadata-preservation.lock`,
  `bytes/.byte-preservation.lock` or `report/.lock` (tried with a non-blocking
  `flock`, and held by `migrate` until it finishes), `preserve_years.py`'s
  `.running` exists, or a Turso marker names a live `active_pid`;
- `recovery.sqlite3-wal` is not empty and there is no
  `.byte-preservation.lock` that shows no writer is active;
- `recovery.sqlite3` is at the top and `bytes/` exists as well;
- an entry is on another device than the archive;
- with `--report`, `report/` exists or the report's own `.lock` is held.

An archive without `recovery.sqlite3` at the top has nothing to migrate
(status 0), except a **metadata-only archive**: one with `metadata/` or
`provenance.json`, no byte section and no `ARCHIVE.json` (such as a Semantic
Unison semdiff archive). `migrate` writes its `ARCHIVE.json` (the same
summary: sources, engine, counts, with `metadata_files` from the index) and
moves nothing; `--dry-run` shows `write ARCHIVE.json (new)`. The log records
`begin` (with `"byte_section": false`), `manifest` and `done`, and `--undo`
removes that `ARCHIVE.json` again, keeping `MIGRATION.jsonl` as the record.

- `--report DIR` also copies a comparison report into `ARCHIVE/report/`
  (through `report.migrating/`, renamed when complete), keeping modes and
  times; the report itself is not changed.
- `--dry-run` prints the plan (`mkdir`, `move`, `copy`, `keep` lines and
  whether `ARCHIVE.json` is new or appended to) and changes nothing.
- Every step is written to `ARCHIVE/MIGRATION.jsonl` and fsync'd before it
  happens (`begin`, `mkdir`, `move` with `from`/`to`, `copy`, `done`). Rerunning
  `migrate` after an interruption resumes it.
- `ARCHIVE.json` (schema `dngc-archive-1`, created if absent) gets an entry in
  `migrations`: `migrated` (time), `moved` (`path` and the `from` it had),
  `report`, and a `summary` with `source`, `reference`, `engine`,
  `total_files`, `verified_files`, `errors`, `byte_preservation_complete`,
  `runs_logged` and `metadata_files` (the metadata index's files), taken from `bytes/summary.json`, `provenance.json`,
  `metadata/provenance.json` and `runs.jsonl`.
- Afterwards, unless `--no-verify`, it runs `verify --restore-sample 3`; on
  problems it prints them and exits 1, keeping the migration.
- `--undo` moves back what the log records, in reverse order, removes
  `report/` only if `migrate` copied it, removes `bytes/` if `migrate` made it
  and it is empty, and records the undo in the log (`undo-begin`, `unmove`,
  `remove`, `rmdir`, `undo-done`) and in `ARCHIVE.json` (`undone`, `restored`,
  `report_removed`). Every original path is back where it was;
  `MIGRATION.jsonl` and `ARCHIVE.json` stay as the record. An interrupted undo
  is finished by running `--undo` again.

Both front ends implement `migrate` alike; `test_dngc.py` migrates and undoes
the same archive with each and requires the same output, logs and tree.

## Wiped and refilled sources

A shuttle disk is emptied after its files are archived, then filled again,
sometimes with other files under the same relative paths. Every tool keeps the
archive's history through that:

- **Reading needs no source.** `status`, `verify`, `query`, `index`,
  `versions` and `restore` read the archive and, for a delta, the reference
  only; `verify` never reads the source. An emptied or missing LEFT changes
  nothing for them, and `migrate` works on such an archive too.
- **Earlier records are never deleted or turned into errors.** The final
  identity check of a preservation run covers only this run's files (Python
  `preserve_apo.py`, OCaml `dng-archive`, Rust `dng-compare-all` alike).
  A failed re-preservation of a path keeps its earlier record and adds an
  error row beside it.
- **Missing listed sources are reported.** A listed source that is gone is
  printed as `MISSING REL`, counted as `missing_sources` in the byte section's
  `summary.json`, and not preserved; the run is incomplete (status 1).
  `compare_dng.py`, `dng-archive all` and `dng-compare-all` without
  `--trust-pairs` refuse such a list up front with `REL: missing under ROOT`
  (status 2); with `--trust-pairs` (a repeated `dngc run`) the pair is
  reported unverified and its preservation as missing. Cached comparisons whose
  files changed or vanished are not reused.
- **A whole-tree run over an emptied source** succeeds with `0 files under
  LEFT`; LEFT must still exist as a directory (a missing LEFT is a usage error
  naming it).
- **Refilled paths are versioned, not overwritten.** When a path's record is
  replaced by one of a later content, the earlier record moves to the
  `versions` table of `recovery.sqlite3` (`path`, `version`, `record`) under
  its version label: the source's modification time in UTC and the first 12
  hex digits of its SHA-256, `YYYYMMDDTHHMMSSZ-xxxxxxxxxxxx`. Its payload stays
  where it was. The later content's payload goes to `versions/LABEL/deltas/…`
  or `versions/LABEL/originals/…` (its own label), so no payload is ever
  overwritten. `files` and `manifest.jsonl` hold the current record of each
  path, as before.
- `dngc versions ARCHIVE [REL]` lists `path`, `version` and `state`
  (`current` or `earlier`); `dngc restore ARCHIVE REL OUT --version LABEL`
  (and `dng-archive restore`/`preserve_apo.py restore --version LABEL`) rebuilds
  that content. `status` counts `earlier_versions`; `verify` checks their
  payloads too.
- **Metadata** (`phindex_metadata.py`, `dng-archive phindex-metadata`): a pair
  whose source is recorded with its current size and `mtime_ns` is not read
  again; a missing source is reported and its rows stay; a new content of a
  recorded path gets the run's UTC stamp as its `version`, the field
  `metadata_index.py` already uses to tell contents of one path apart. New
  batches continue the `phindex-NNNNNN` numbering.

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
./dngc query ARCHIVE                                # most frequent metadata changes
./dngc query ARCHIVE files EXIF:ExifIFD:LensModel   # files where a tag changed
./dngc query ARCHIVE values EXIF:ExifIFD:LensModel  # with old and new values
./dngc query ARCHIVE "SELECT ..."                   # any SQL on the index
./dngc index ARCHIVE                                # rebuild the metadata index from its batches
./dngc verify ARCHIVE [--restore-sample N]          # records, files and index; restore N at random
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
| `index` | `Index.index_archive` | `dng-archive index`, else `metadata_index.py` |
| `query`, `status`, `verify` | in OCaml on the archive's SQLite files | in Python |
| `verify --restore-sample` | `Preserve.restore` | `preserve_apo.py restore` |

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
- **Read-only SQLite.** `status`, `verify` and `query` open the databases with
  `?mode=ro`, as `dngc.py` does. The macOS system SQLite (3.43) cannot open a
  WAL database read-only while its `-shm` file is missing, which the SQLite in
  Python's build (3.53) can; the native binary then opens the file read-write
  with `PRAGMA query_only=ON`, so it is still not written.
- An interrupt (Ctrl-C) between steps gives status 130.

## The archive

One directory, with the sections a run produced:

| Path | Section | Written by |
| --- | --- | --- |
| `report/` | comparison: `pairs.jsonl`, `summary.json`, `cache.sqlite3` | the comparison step |
| `bytes/` | preservation: `recovery.sqlite3`, `deltas/`, `originals/`, manifests | the preservation step |
| `metadata/` | searchable metadata: `batches/`, `differences.sqlite3`, `provenance.json` | `--metadata phindex`, `dngc index` |
| `ARCHIVE.json` | every run's options, sources, implementation and exit statuses | `dngc run` |

An archive written before `dngc`, with `recovery.sqlite3` at the top, is read as
it is: its top level is its byte section.

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

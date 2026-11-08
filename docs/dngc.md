# dngc: one command

`dngc` puts dng-compare's comparison, preservation and metadata tools behind
six verbs that work on one kind of archive. It runs the existing tools, so
their outputs, tests and parity guarantees carry over; the older commands keep
working. See [archives-and-tools.md](archives-and-tools.md) for the tools it
runs.

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

`./dngc` uses the checkout's uv environment (`make setup`); link it into a
directory on `PATH` to call it from anywhere.

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
  listed pairs (JPEG, TIFF, raws) are preserved by `preserve_apo.py`.
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

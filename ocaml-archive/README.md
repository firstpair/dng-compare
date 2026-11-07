# dng-archive

The metadata index and lossless preservation archive of dng-compare, in OCaml.
It builds with stock OCaml 5 and with OxCaml, and it reads and writes the same
archive files as the Python tools:

| Command | Python equivalent |
| --- | --- |
| `dng-archive index ARCHIVE` | `metadata_index.py ARCHIVE` |
| `dng-archive preserve SOURCE REFERENCE ARCHIVE --pairs FILE [--delta] [--full-copy] [--workers N] [--rate-mib R]` | `preserve_apo.py scan ... --pairs FILE` |
| `dng-archive restore ARCHIVE RELATIVE_PATH OUTPUT [--reference-root DIR]` | `preserve_apo.py restore ...` |
| `dng-archive all LEFT RIGHT --pairs FILE --report DIR [--archive DIR] [--left-index FILE --right-index FILE] [--workers N] [--batch-size N] [--exiftool-processes N] [--progress-secs S] [--rate-mib R] [--fresh]` | `compare_dng.py --pairs-only` and `preserve_apo.py scan --delta --pairs`, in one pass |

The index has the same schema and rows, and it keys each stored value by the
SHA-256 of the same canonical JSON (`json.dumps(sort_keys=True,
ensure_ascii=True, separators=(',', ':'))`), so either tool can add batches to
an index the other built. Preservation records in `recovery.sqlite3` have the
same fields in the same order, and each tool restores what the other preserved.
`test_dng_archive.py` in the repository root checks all of this against the
Python modules.

## `all`: one-pass comparison and preservation

`dng-archive all` is the OCaml implementation of the command specified in
[docs/native-all.md](../docs/native-all.md), with the same options, defaults
and outputs as the Rust `dng-compare-all`. Each pair of a `LEFT<TAB>RIGHT`
pairs file is read from disk once into memory; from those bytes it compares
the DNG pages by encoded blocks as `compare_dng.py` does through tifffile
(page order, raw/auxiliary split, `TiffPage.shape`, block digests, encoding
signature, error texts), and preserves the left file against the right one as
`preserve_apo.py scan --delta --pairs` does. `--report DIR` receives exactly
what `compare_dng.py --pairs-only --output DIR` writes (`pairs.jsonl`,
`summary.json`, `unmatched.json`, `not-confirmed.json`, `cache.sqlite3`,
`.lock`), `--archive DIR` exactly what the preservation writes, so either
side resumes, queries or restores what the other wrote. Without `--archive`
it only compares. `--left-index`/`--right-index` take phindex indexes, opened
immutable, and settle pairs whose rows are current without reading them.
`test_native_all.py` checks all of this against the Python commands.

- Pages whose encoded blocks or encoding signature differ are decoded by the
  Python decoder in a child process, as `decoded_status_isolated` does it. The
  interpreter is `DNG_COMPARE_PYTHON`, else `.venv/bin/python` of the
  directory holding `compare_dng.py`: `DNG_COMPARE_DIR`, else the nearest
  directory above the executable that holds `compare_dng.py` (a checkout
  build). An installed binary needs `DNG_COMPARE_DIR` for decode fallbacks.
- Metadata comes from Perl ExifTool with `compare_dng.py`'s arguments, one
  batch of `--batch-size` pairs at a time over `--exiftool-processes`
  processes, running while the workers read and compare.
- Workers are forked processes, as for `preserve`; each batch forks
  `--workers` of them, and each keeps its share of `--rate-mib` for the
  preservation reads. Only the parent writes the databases and the report.
- Exit status: 0 when every left file has a raw-equal right copy, 1 otherwise
  or when preservation is incomplete, 2 for usage errors, 130 when interrupted.
- tifffile's special cases for non-DNG vendor formats (LSM, STK, NDPI,
  Philips, Vista, ScanImage) are not reproduced; a cyclic IFD chain ends at
  the first repeated IFD. A left file listed with several rights is preserved
  once, against its last listed right, as `preserve_apo`'s reference map does.

Differences of `preserve` from the Python tools:

- **Listed pairs only.** `preserve` always takes `--pairs`, the subset that
  Semantic Unison's `unison-dngscan -semdiff -preservefull` writes. The
  whole-tree scan, the legacy TSV hints and the year workflows stay in Python.
- **Wiped sources.** The final identity check covers the files of this run,
  so records from earlier runs whose sources are gone, such as files on a
  wiped shuttle disk, do not fail the run.
- **SQLite only.** An archive moved to the Turso engine (`*.engine.json`) is
  refused rather than written behind its marker.
- **Workers.** Workers are forked processes, and each one gets an equal share
  of `--rate-mib`.

It needs `xdelta3` on `PATH`, and SQLite and zlib from the system.

## Build and install

```sh
make DUNE="opam exec --switch 5.2.0+ox -- dune"
opam pin add -y --switch 5.2.0+ox dng-archive ~/src/dng-compare/ocaml-archive
opam pin add -y --switch default dng-archive ~/src/dng-compare/ocaml-archive
```

The pin installs `dng-archive` into the switch's `bin`, where Semantic Unison
finds it. The pin follows the committed state of the repository, so commit
before `opam reinstall dng-archive`. The opam `sqlite3` and `conf-zlib`
packages were installed with `--assume-depexts`, because the system already
provides SQLite and zlib.

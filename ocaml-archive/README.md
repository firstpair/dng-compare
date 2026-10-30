# dng-archive

The metadata index and lossless preservation archive of dng-compare, in OCaml.
It builds with stock OCaml 5 and with OxCaml, and it reads and writes the same
archive files as the Python tools:

| Command | Python equivalent |
| --- | --- |
| `dng-archive index ARCHIVE` | `metadata_index.py ARCHIVE` |
| `dng-archive preserve SOURCE REFERENCE ARCHIVE --pairs FILE [--delta] [--full-copy] [--workers N] [--rate-mib R]` | `preserve_apo.py scan ... --pairs FILE` |
| `dng-archive restore ARCHIVE RELATIVE_PATH OUTPUT [--reference-root DIR]` | `preserve_apo.py restore ...` |

The index has the same schema and rows, and it keys each stored value by the
SHA-256 of the same canonical JSON (`json.dumps(sort_keys=True,
ensure_ascii=True, separators=(',', ':'))`), so either tool can add batches to
an index the other built. Preservation records in `recovery.sqlite3` have the
same fields in the same order, and each tool restores what the other preserved.
`test_dng_archive.py` in the repository root checks all of this against the
Python modules.

Differences from the Python tools:

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

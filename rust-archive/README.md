# dng-archive (Rust)

The dngc archive format, [docs/dngc-archive-spec.md](../docs/dngc-archive-spec.md),
in Rust: the counterpart of the OCaml `dng-archive` library in
[`../ocaml-archive`](../ocaml-archive). `dng-compare-all` (in `../rust-scanner`)
is built on it.

```rust
use dng_archive::{Reader, kind, Kind};

let archive = Reader::open("/Volumes/Backup/dngc/Archive")?;
for (path, _text, record) in archive.records()? {
    if kind(&record) == Kind::Delta {
        archive.restore(&record, std::path::Path::new("/tmp/out").join(&path).as_path(), None)?;
    }
}
```

- `read`: open an archive (`Reader`), list records, earlier versions and
  tombstones, rebuild or restore a content byte for byte, with its attributes.
- `skeleton`: skeletons (spec section 13), which rebuild a reference's content
  from an edited version of the file; `Reader` uses them when a reference has
  changed. `dngc-skeleton build ARCHIVE` writes an archive's skeleton section.
- `archive`: write the byte section, as dng-compare-all does for listed pairs.
- `phindex`: phindex indexes, to settle identical pairs without reading them.
  `Reader::open(archive)?.with_indexes([index, ...])?` also finds a reference that was moved, renamed or edited
  elsewhere (spec section 5.5): by SHA-256, or through its skeleton and a file of the same image identity.
- `pyjson`: JSON as Python's json module reads and writes it.
- `os`: identities, attributes, extended attributes.

`cargo test` reads the conformance corpus (`../conformance/corpus`) through
this API; `make conformance` at the top runs the Python, Rust and OCaml suites.

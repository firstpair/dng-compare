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
- `archive`: write the byte section, as dng-compare-all does for listed pairs.
- `phindex`: phindex indexes, to settle identical pairs without reading them.
- `pyjson`: JSON as Python's json module reads and writes it.
- `os`: identities, attributes, extended attributes.

`cargo test` reads the conformance corpus (`../conformance/corpus`) through
this API; `make conformance` at the top runs the Python, Rust and OCaml suites.

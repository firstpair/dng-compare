# dng-exiftool-rs (OCaml / OxCaml)

A standalone dune project binding the `dng-exiftool-ffi` C ABI, which wraps the
[`exiftool-rs`](https://crates.io/crates/exiftool-rs) crate (a pure Rust
reimplementation of ExifTool). It offers:

- `Dng_exiftool_rs.extract_json path` — one ExifTool-shaped JSON object per
  file, following `-j -a -G0:1:4 -n -struct -b -u -U` with image payload tags
  excluded.
  Failures are reported inside the record as `ExifTool:Error`, never as success.
- `Dng_exiftool_rs.extract_all ~workers paths` — a domain-parallel pool that
  returns records in input order.
- `metadata-extract-exiftool-rs-oxcaml WORKERS [--engine exiftool-rs]` — the
  dng-compare worker: NUL-delimited paths on stdin, a JSON array on stdout.
  `--engine-version` prints the engine identity.

Records are exiftool-rs metadata (release 0.12.0, which includes `-struct`).
They are a separate identity from Perl ExifTool records: tag coverage and
some value conversions differ, and `ExifTool:ExifToolVersion` reports the
crate version. Every record carries
`ExifTool:DngCompareEngine` naming the engine and the ExifTool tables it mirrors.

## Stock OCaml and OxCaml

The same sources build with both compilers; dune's `select` picks the pool:

| Compiler | Pool module | Mechanism |
|---|---|---|
| Stock OCaml ≥ 5.1 | `pool.stock.ml` | `Domain.spawn` workers pulling indexes from an `Atomic` counter |
| OxCaml (`stdlib_stable` present) | `pool.oxcaml.ml` | `Domain.Safe.spawn` with portable closures over an immutable `iarray` and an `Atomic` counter; the stub is declared `@@ portable` |

Both pools cap workers at `Domain.recommended_domain_count ()` and at the
number of paths. The C stub releases the OCaml runtime lock around each
extraction, so domains run the Rust engine concurrently. `Dng_exiftool_rs.backend`
reports which pool was compiled in (`domains-stock` or `domains-oxcaml`).

## Building

Build the Rust library first (from the repository root):

```sh
cargo build --locked --release --manifest-path rust-scanner/Cargo.toml \
  --features dng-subset/exiftool-rs -p dng-subset -p dng-exiftool-ffi
```

Then build with any opam switch that has dune:

```sh
cd ocaml-exiftool-rs
opam exec --switch 5.2.0+ox -- dune build @install   # OxCaml
opam exec --switch default  -- dune build @install   # stock OCaml 5
dune test --force
```

## Installing into an opam switch

Pin the checkout into each switch that should offer the library. The pinned
build runs from opam's copy of the sources, so name the Rust directory:

```sh
export DNG_EXIFTOOL_FFI_LIBDIR=$HOME/src/dng-compare/rust-scanner/target/release
opam pin add -y --with-test --switch 5.2.0+ox dng-exiftool-rs ./ocaml-exiftool-rs
opam pin add -y --with-test --switch default  dng-exiftool-rs ./ocaml-exiftool-rs
```

The package installs `libdng_exiftool_ffi.a` and `dng_exiftool_rs.h` beside
the OCaml library, so consumers only write `(libraries dng-exiftool-rs)`.
Their link prints a harmless `search path 'lib' not found` warning. After a
Rust rebuild, run `opam reinstall dng-exiftool-rs` in each switch with the
same variable set; `opam upgrade` skips it when no OCaml source changed.


The worker is `_build/default/bin/main.exe`; `make build-exiftool-rs` at the
repository root copies it to `metadata-extract-exiftool-rs-oxcaml`.

Environment variables read at build time:

- `DNG_EXIFTOOL_FFI_LIBDIR` — directory holding `libdng_exiftool_ffi.{a,dylib,so}`
  (default `../rust-scanner/target/release` relative to this project).
- `DNG_EXIFTOOL_FFI_LINK` — `static` (default; executables are self-contained)
  or `dynamic` (links the shared library with an rpath to that directory).

`lib/dng_exiftool_rs.h` must stay byte-identical to
`rust-scanner/exiftool-ffi/dng_exiftool_rs.h`; a repository test checks this.

## License

The binding sources are MIT like the repository. The linked engine is
GPL-3.0-or-later, so executables built from this project are distributed under
GPL terms; see `rust-scanner/exiftool-ffi/THIRD-PARTY.md`.

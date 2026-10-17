# Publishing the OCaml exiftool-rs wrapper to opam

Assessment written 2026-10-16. The wrapper is the dune project in
`ocaml-exiftool-rs/` (opam package `dng-exiftool-rs`, module
`Dng_exiftool_rs`, worker executable `metadata-extract-exiftool-rs-oxcaml`).
It links the Rust crate `rust-scanner/exiftool-ffi` (`dng-exiftool-ffi`, C ABI
`dng_exiftool_rs_*`), which wraps the dng-compare fork of `exiftool-rs`
(https://github.com/alexy/exiftool-rs, branch `dng-compare-parity`, upstream
pull request https://github.com/Le-Syl21/exiftool-rs/pull/20).

The dune build, the opam file, the stock-OCaml build without OxCaml and the
network-free test suite are already in the shape opam-repository expects.
Everything else below is work.

## Blockers for opam-repository

| Blocker | Why it fails review or CI | Resolution |
|---|---|---|
| Rust library built from the network | opam builds run sandboxed with no network, so neither crates.io nor a git fork can be fetched | Ship a vendored crate tree (`cargo vendor` of `dng-exiftool-ffi`, `exiftool-rs` and its ten dependencies) in the source archive; depend on `conf-rust-2021`; build with `cargo build --offline` before `dune build`, the way Tezos packages its Rust-backed libraries |
| Git fork pin | A published package pinning a fork revision is fragile and will be refused | Wait for upstream to merge PR #20 and release on crates.io, then vendor that release; or publish the fork to crates.io under another name if upstream stalls |
| License field says MIT | Executables that compile exiftool-rs are GPL-3.0-or-later | Set `license: "GPL-3.0-or-later"` in the opam file; the binding sources can stay MIT in their headers but the package is GPL |
| Project lives inside the dng-compare monorepo | opam needs a tagged release archive whose root is the project | Move `ocaml-exiftool-rs/`, the C header and the FFI crate into their own repository (for example `alexy/ocaml-exiftool-rs`) |
| dng-compare specifics | Every record carries `ExifTool:DngCompareEngine`; the worker is named and shaped for the dng-compare coordinator; the package name says `dng-` | Make the stamp optional, offer a plain `extract_json`, drop or generalise the worker executable, rename the package to `exiftool-rs` or `ocaml-exiftool-rs` |
| Rust edition 2024 in the FFI crate | Needs Rust 1.85 or newer; `conf-rust-2021` guarantees less | Switch the FFI crate to edition 2021, matching exiftool-rs itself (MSRV 1.82) |

Two non-blockers worth knowing: the build artifact is large (the static
library is about 97 MB on macOS) but the vendored sources are a few MB, and
the OxCaml `Domain.Safe.spawn` pool is selected only when `stdlib_stable`
exists, so opam-repository CI on stock OCaml takes the `Domain.spawn` pool.

## Plan

1. Upstream release. Wait for the exiftool-rs release that contains PR #20, or
   publish the fork to crates.io under a distinct name. Re-pin and vendor that.
2. Split the project out. New repository holding `dune-project`, `lib/`,
   `bin/` (generalised or removed), `test/`, `config/`, the FFI crate and the
   vendored crate tree under `vendor/`, with a `.cargo/config.toml` pointing
   cargo at it. Generic naming, GPL license, edition 2021, a README that states
   the GPL consequence for linked executables.
3. opam file. `depends` on `ocaml {>= "5.1"}`, `dune {>= "3.9"}`,
   `conf-rust-2021`; `build` runs cargo offline then dune; `run-test` runs
   `dune test`. Keep the `select` between the stock and OxCaml pools.
4. Tag a release and verify `opam install .` from the tarball on a clean
   switch, Linux and macOS, with `OPAMNOSANDBOX` unset.
5. Submit the opam file with the archive URL and checksum to
   https://github.com/ocaml/opam-repository. Review usually takes a few days;
   its CI builds across OCaml versions on Linux and macOS.

Effort: about a day once the upstream release exists, plus review time.

## Faster alternative

A custom opam repository on GitHub (a repository with `packages/<name>/<name>.<version>/opam`
and the `url` section pointing at the release archive) can be added by anyone
with `opam repo add`. It has none of the review constraints above except the
vendored, offline Rust build, so it can carry the fork pin and the
dng-compare naming until upstream releases. It is the right place for an
interim 0.1.0.

## Decision pending

Whether to generalise the package now or publish the interim custom-repository
version first. Both start with the split-out repository and the vendored build.

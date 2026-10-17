# exiftool-rs engine attribution

This crate links the `exiftool-rs` crate, a pure Rust reimplementation of
ExifTool, as an optional metadata engine. No exiftool-rs source is vendored.

- Upstream: https://github.com/Le-Syl21/exiftool-rs (crates.io `exiftool-rs`, 0.10.0)
- Linked build: the dng-compare fork https://github.com/alexy/exiftool-rs,
  branch `dng-compare-parity`, pinned by commit in `Cargo.toml`. The fork adds
  `-struct`, multi-family `-G` labels, `-n` JSON output and numeric raw values
  measured against Perl ExifTool 13.59; see its CHANGELOG and `XMP.md` here.
- License: GPL-3.0-or-later (the same license family as Perl ExifTool)

Our adapter sources carry the repository license. Any binary built with the
`exiftool-rs` cargo feature (`metadata-extract`, `libdng_exiftool_ffi`,
`metadata-extract-exiftool-rs-oxcaml`) links GPL code and is distributed under
GPL-3.0-or-later terms. The feature is off by default; the default workers only
invoke the separately installed Perl ExifTool executable.

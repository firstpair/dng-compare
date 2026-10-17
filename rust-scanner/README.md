# Rust tools

These Unix tools are part of [dng-compare](../README.md). The project pins Rust
1.99.0 and uses edition 2024. Build from the repository root:

```sh
rustup toolchain install 1.99.0
make build-rust
```

## Bounded raw subset scanner

```sh
./rust-scanner/target/release/dng-subset \
  /media/source/foto /media/reference/foto reports/rust --rate-mib 32
```

Every left DNG needs at least one same-named right copy. Extra right files are
ignored. Filenames are case-sensitive; `.dng` extension discovery ignores case.
An identical relative path is tried first among candidates. The scanner processes
one pair at a time in 1 MiB chunks and budgets combined image reads, default
32 MiB/s, configurable above zero through 512 MiB/s.

It parses classic TIFF and BigTIFF, traverses main/subimage IFDs, and compares the
selected primary raw image blocks and encoding parameters. Exact whole-file
equality is a fallback for some structural/encoding failures. It does not decode
changed compression, compare auxiliary previews, render photos or generate
Lightroom fingerprints. An identical damaged copy can be preserved byte-for-byte
without being a valid DNG. `UNVERIFIED` does not prove changed samples.

Options after `LEFT RIGHT REPORT`:

| Option | Meaning |
| --- | --- |
| `--rate-mib N` | Combined raw-read budget, `0 < N <= 512` |
| `--max-new N` | Check at most N new left files; retain pending files |
| `--import-cache FILE` | Import trusted four-column verified TSV records |
| `--after SUMMARY` | Wait for a prior summary containing `"complete": true` and both roots to exist |

`--after` checks processing completion, not a prior equality verdict. The command
does not install a service, inhibit sleep or restart itself.

`summary.json` has `verified_equal`, `total_left`, `complete` and
`left_subsumed_by_right`. Exit 0 confirms complete raw subsumption; exit 1 means
missing, unverified or pending left files remain. `verified.tsv` is append-only
with hex-encoded Unix paths and both file stamps. `not-confirmed.tsv` separates
`MISSING`, `UNVERIFIED` and `PENDING`. Resume checks path membership, inode, size,
mtime and ctime; it intentionally excludes device number after remounting. A
stat-based cache is not a fresh content audit. Use a new report directory if you
need every payload reread. Only one process may use a Rust report directory;
there is no automatic report lock.

## Exact embedded XMP extraction

```sh
./rust-scanner/target/release/dng-subset --archive-xmp \
  /media/source/foto archives/source-xmp
```

This stores TIFF tag 700 packets under `packets/`, with relative paths, sizes,
SHA-256 and source identity in the manifests. Parse failures can use ExifTool as
an extraction fallback; the method is recorded. `summary.json` distinguishes
files with/without XMP, errors and `all_xmp_saved`. Packet bytes and source stamps
are checked on resume. It does not include existing `.xmp` sidecars or establish
that a packet is the current develop setting requested by a cloud service.

## Metadata report over verified pairs

```sh
mkdir -p reports
touch reports/empty-import.tsv
./rust-scanner/target/release/dng-metadata \
  reports/empty-import.tsv reports/rust/verified.tsv reports/metadata 32
```

Arguments are `IMPORTED_VERIFIED.tsv RUST_VERIFIED.tsv REPORT_DIR BATCH_SIZE`,
with batch size 1–64. The tool invokes ExifTool, classifies embedded/filesystem/
layout differences and writes atomic `batches/*.jsonl`, a summary and combined
`pairs.jsonl`. It does not independently reverify the image verdict or source
identity. Existing batches are reused by their index and row count, so keep the
input pair set and batch size unchanged and files stable. Use a new directory
when those change. Large differing values are compared in full but compacted
with a prefix and length in this reporter; the portable Python metadata archive
retains full values.

## Native worker protocols

`metadata-extract WORKERS` consumes a JSON array of paths on stdin and emits the
combined ExifTool JSON in input order. `WORKERS` is 1–36. It uses threads to
schedule separate ExifTool processes and preserves their JSON bytes without
numeric reserialization.

`metadata-extract WORKERS --engine exiftool-rs` extracts in-process through
the optional `dng-exiftool-ffi` crate (workspace member `exiftool-ffi`, cargo
feature `exiftool-rs`, the dng-compare fork of exiftool-rs 0.10.0 pinned by
commit, GPL-3.0-or-later). The
default build rejects that engine with an explicit error. `--engine-version`
prints the engine identity. The crate also exports the C ABI declared in
`exiftool-ffi/dng_exiftool_rs.h` for the OCaml project in `ocaml-exiftool-rs`.
Its records are exiftool-rs metadata, not Perl ExifTool output; see the manual.

`byte-verify` implements:

```text
byte-verify hash FILE
byte-verify compare SOURCE REFERENCE
byte-verify delta REFERENCE SOURCE DELTA EXPECTED_SOURCE_SHA256
```

`compare` prints source SHA-256, reference SHA-256 and `1`/`0` equality separated
by tabs. An empty reference argument means no reference. `delta` requires
xdelta3 and verifies reconstruction before emitting the delta SHA-256. These
workers do not provide the orchestrator's inventory/identity/ownership gates;
the Python archive layer owns that contract.

The opt-in `turso-probe` is a database smoke test, not an image comparator or
production scan. Its setup and the OCaml equivalent are documented in the
[manual](../docs/dng-compare-manual.md).

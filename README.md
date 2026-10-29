# dng-compare

**Compare the image samples and metadata of DNG copies, then preserve the exact
versions you need to keep.** The project includes Python orchestration, a bounded
Rust DNG scanner, and interchangeable Rust and OCaml workers for byte verification
and ExifTool extraction.

This answers a common archive question: two DNG trees have the same photographs,
but editing software has rewritten their metadata. A whole-file checksum says
they differ. Which sensor samples changed? Which settings changed? Can the older
version be reconstructed exactly without storing another full copy?

The main comparison is read-only on both photograph trees. It writes its own
reports and checkpoints. Preservation commands additionally write an explicitly
chosen archive, and restore writes a new destination. Nothing here imports,
relinks, edits or deletes Lightroom catalog records, and nothing generates
Lightroom's native duplicate-detection fingerprints.

Read the [HOWTO](HOWTO.md) for a short workflow and the
[complete manual](docs/dng-compare-manual.md) for definitions, every command,
report interpretation, resuming, preservation and limitations.

The manual is published on the [First Pair HOWTO shelf](https://firstpair.org/books/dng-compare/),
alongside Lightroom Repair: [PDF](https://firstpair.org/dng-compare/pdf/),
[EPUB](https://firstpair.org/dng-compare/epub/),
[read online](https://firstpair.org/read/dng-compare/) and
[chapters](https://firstpair.org/read/dng-compare/chapters/).
See the [publication record](docs/PUBLICATION.md) for artifact verification and
the separately prepared blog handoff.

## What an equal result means

| Question | Tool or result | Meaning |
| --- | --- | --- |
| Are all file bytes equal? | `byte-verify`, preservation SHA-256 | Exact original-file identity, including metadata and unused bytes |
| Are the stored image samples equal? | `compare_dng.py`, `raw.status == "equal"` | Matching encoded blocks and interpretation parameters, or a successful full decoded-sample comparison |
| Are the embedded previews equal? | Python `auxiliary.status` | Separate comparison of the other image IFDs |
| Which metadata values differ? | ExifTool reports and archives | Differences in fields ExifTool exposes, retaining warnings and direction |
| Will both files render identically? | Not established | Crop, white balance, orientation, opcodes, profiles and develop settings can differ |
| Does Lightroom regard these as duplicates? | Not established | Lightroom's native original digest is a different identity |

The Rust scanner is deliberately narrower than the Python comparator: it
confirms matching encoded raw blocks without decoding, and uses exact whole-file
equality as a fallback. A Rust `UNVERIFIED` result requires investigation or a
targeted Python comparison; it is not evidence of different pixels.

## Install

The native tools target Unix, with macOS the main development platform. Python
requires 3.12 or newer. Rust is pinned to **1.99.0, edition 2024**. The OCaml
workers use ordinary OCaml; the existing `*-oxcaml` executable names are retained
for protocol compatibility. The OCaml SHA-256 C binding uses macOS CommonCrypto.

On macOS, install [uv](https://docs.astral.sh/uv/getting-started/installation/),
[rustup](https://rustup.rs/), an OCaml compiler and the external tools:

```sh
brew install exiftool xdelta ocaml
git clone https://github.com/firstpair/dng-compare.git
cd dng-compare
make setup
rustup toolchain install 1.99.0
make build
make test
```

`make setup` uses uv to install the pinned Python environment. `make build-rust`
builds only the Rust tools; `make build-oxcaml` builds the OCaml workers. See the
manual for compiler overrides and optional Turso database probes. Use `uv run`
for Python commands so they use the project environment.

The [publication validation record](BUILD-VALIDATION.md) lists the actual
toolchain, executed fixture checks and untested platform/experimental scopes.

## Compare two roots

The **left** root is the collection you want accounted for. The **right** root
may contain additional files. Matching is recursive by case-sensitive filename,
with an identical relative path preferred among duplicate-name candidates.
Renamed files are not discovered automatically.

Start with a representative sample:

```sh
uv run python compare_dng.py /media/source/foto /media/reference/foto \
  --sample 12 --workers 2 --output reports/sample
```

A sample can exit 1 because it does not establish coverage of the full left
tree. To run the complete comparison:

```sh
uv run python compare_dng.py /media/source/foto /media/reference/foto \
  --workers 2 --output reports/full
```

The Python exit code is 0 when every left DNG has at least one confirmed equal
raw copy, 1 when that has not been established, 2 for argument errors and 130 for
interruption. Metadata differences alone do not make the raw comparison fail.

Inspect `reports/full/summary.json`:

```json
{
  "complete": true,
  "left_subsumed_by_right": {
    "raw": true,
    "all_images": true,
    "metadata_preserved": false
  }
}
```

This example means all stored raw samples and auxiliary images have an equal
copy, but exposed embedded metadata is not preserved without change in the
matching copies. The summary is **not a deletion instruction**. Keep differing
settings, original file bytes and the independent backup copies your archive
requires.

## Reports and resume

| File | Purpose |
| --- | --- |
| `pairs.jsonl` | Every selected candidate pair, image verdicts and metadata differences |
| `summary.json` | Coverage, counts, changed-field frequencies and examples |
| `unmatched.json` | Missing filenames and duplicate-name candidates |
| `not-confirmed.json` | Left files lacking a confirmed raw match at normal completion |
| `cache.sqlite3` | Versioned, identity-bound checkpoint results |

Rerun the same command and output directory to resume. Python reuse checks the
absolute paths, device, inode, size, modification time, change time and cache
version. Changed inputs are reread; errors and unverified pairs are retried.
Only one Python comparator may own a report directory. Do not edit the source
files during a run. Reading can update access times.

The program reads the image payloads rather than trusting file size or a stored
`RawImageDigest`. Equal blocks with equal decoding parameters avoid decompression.
When the encoding differs, `tifffile` and `imagecodecs` decode both pages and
compare their complete arrays. Decode failures remain `unverified`.

## Rust fast path

```sh
./rust-scanner/target/release/dng-subset \
  /media/source/foto /media/reference/foto reports/rust --rate-mib 32
```

It processes one pair at a time with 1 MiB buffers and a default combined
raw-read budget of 32 MiB/s. It does not decode differently encoded images or
compare embedded previews and metadata. Exit 0 confirms complete left raw
coverage; exit 1 means coverage was not confirmed. Inspect the JSON verdict and
`not-confirmed.tsv` for the reason. See [the Rust tools](rust-scanner/README.md).

## Exact preservation

The archive layer keeps whole-file identity separate from sample equality:

- An exact copy can reference the retained reference file by SHA-256.
- A different version can be stored as its original bytes or a VCDIFF delta
  against the reference.
- A missing or ambiguous reference falls back to preserving the complete
  source file.
- A delta is decoded and its reconstructed SHA-256 checked before acceptance.

Reference-backed archives require the exact reference bytes to remain
available. `--full-copy` provides independent original payloads instead. Raw
sample equality alone cannot make a metadata-different DNG a whole-byte backup.
The manual includes restore instructions, archive verification and a synthetic
example that never touches a real photograph library.

## Develop settings across editors

`develop_settings.py` records each editor's settings for a folder's photos in
one archive, so you can keep them and compare them:

```sh
uv run python develop_settings.py capture ARCHIVE lr xmp FOLDER
uv run python develop_settings.py capture ARCHIVE lrc lrcat FOLDER --catalog CATALOG.lrcat
uv run python develop_settings.py capture ARCHIVE c1 capture-one FOLDER
uv run python develop_settings.py compare ARCHIVE lr lrc
```

It reads settings embedded in DNG XMP or exported JPEGs (`--extensions jpg`), a
Lightroom Classic catalog (read-only) and Capture One `.cos` files. Each capture keeps the raw source unchanged and
adds typed values under one namespace (`XMP-crs:*`, `CaptureOne:<block>:*`),
indexed in `ARCHIVE/settings/settings.sqlite3`.

`rust-scanner/target/release/develop-settings` takes the same arguments and
writes the same archive; it reads the XMP packet directly instead of running
ExifTool. Either implementation can add to, compare or reindex an archive. See
the manual.

## Source layout

| Area | Contents |
| --- | --- |
| `compare_dng.py` | DNG inventory, comparison, ExifTool differences and resume |
| `rust-scanner/src/` | DNG subset/XMP scanner, metadata reporter, native workers and optional database probe |
| `metadata_extract_oxcaml.ml` | OCaml process pool preserving ExifTool JSON |
| `rust-scanner/exiftool-ffi/` | Optional in-process exiftool-rs engine with a C ABI (cargo feature `exiftool-rs`) |
| `ocaml-exiftool-rs/` | Standalone dune project binding that C ABI for stock OCaml 5 and OxCaml |
| `byte_verify_oxcaml.ml`, `byte_verify_sha_stubs.c` | OCaml exact-byte and reconstruction worker |
| `preserve_apo.py`, `portable_metadata.py` | Preservation and full metadata snapshots; historic filenames retained |
| `preserve_years.py`, `unified_pass.py` | Configurable year workflow and bounded overlap |
| `develop_settings.py`, `rust-scanner/src/bin/develop-settings.rs` | Develop-settings capture from XMP, Lightroom Classic catalogs and Capture One, in Python and Rust |
| `metadata_index.py`, `metadata_shards.py` | Full-value searchable metadata index and extraction shards |
| `database_backend.py`, `turso_oxcaml*` | Checkpoint backend adapter and experimental native database bindings |
| `test_*.py`, `benchmark_*.py` | Synthetic tests and measured worker workflows |

The public repository contains source, build specifications, tests and manuals.
Private photograph trees, archive payloads, personal inventories and execution
reports are excluded. Performance depends on storage, codecs and metadata size;
there is no universal worker count or throughput guarantee.

MIT licensed. Adobe, Lightroom, DNG, ExifTool, OCaml and Rust are the respective
projects' names; this tool is independent of Adobe.

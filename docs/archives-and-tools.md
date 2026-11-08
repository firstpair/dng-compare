# DNG metadata extraction and preservation: tools and archives

The kinds of archive dng-compare and its companion tools write, which tool
writes each kind, and how they differ. See the [manual](dng-compare-manual.md)
for each command's options and [native-all.md](native-all.md) for the one-pass
native commands.
[dngc.md](dngc.md) describes `dngc`, one command over these tools and a
single archive layout; its native binary (`ocaml-archive/`) calls the OCaml
library directly, and `dngc.py` runs the tools as programs.

## 1. Kinds of archive

| Kind | Layout | Holds | Rebuilds files? | Searchable? |
|---|---|---|---|---|
| **A. Comparison report** | `pairs.jsonl`, `summary.json`, `cache.sqlite3`, `not-confirmed.json` | per pair: raw and preview equal or different (encoded-block digests), metadata differences (values over 1 KiB as hashes) | no | by reading JSON; no SQL index |
| **B. Byte preservation** (in `ARCHIVE/bytes/`; a legacy archive at its top) | `recovery.sqlite3`, `deltas/*.vcdiff`, `originals/`, `manifest.jsonl`, `inventory.json`, `directories.json` | every source file as a SHA-256 reference (identical), a verified VCDIFF delta against the reference, or a full copy (no reference); attributes and xattrs | **yes,** byte for byte (deltas need the reference unchanged) | no |
| **C. Metadata section** (inside a B archive, or on its own) | `metadata/batches/*.jsonl.gz`, `metadata/differences.sqlite3`, `metadata/summary.json`, `provenance.json` | each file's full metadata record on both sides, plus a typed difference row per changed tag | no | **yes, SQL** (dng-compare `METADATA-QUERIES.md`) |
| **D. Year archive** | `ARCHIVE/LABEL/YYYY/` = B + C + `xmp-original/` + `image-comparison/`, `scope.json`, `matching.json` | the complete source tree, year by year: bytes, metadata, exact embedded XMP packets, raw/preview classification | yes | yes |
| **E. XMP packet archive** | `packets/…`, manifests | exact embedded TIFF tag-700 XMP packets with SHA-256 | the XMP only | no |
| **F. Develop-settings archive** | `settings/LABEL.jsonl.gz`, `LABEL.provenance.json`, `settings.sqlite3` | develop settings captured from several editors | no | yes |
| **G. phindex index** | one `.sqlite` per volume, copied to `VOLUME/phindex/` | every file's identities (SHA-256, raw blocks, Lightroom fingerprint, image data, metadata digest) and a shortened exiftool-rs record | no | yes (SQL), per file, not per pair |

## 2. Tools

### Comparison (A)

| Tool | Language | Engine | Writes |
|---|---|---|---|
| `compare_dng.py` | Python | Perl ExifTool | A, then B by default (`--compare-only` opts out); phindex shortcuts |
| `dng-compare-all` | Rust | Perl ExifTool, or none with `--skip-metadata` | A + B in one pass per pair; `--trust-pairs`, phindex shortcuts |
| `dng-archive all` | OCaml / OxCaml | same | same as Rust |
| `dng-subset` | Rust | none (raw blocks only) | raw subset report (`verified.tsv`); `--archive-xmp` writes E |
| `dng-metadata` | Rust | Perl ExifTool | metadata report over `dng-subset`'s verified pairs |

### Byte preservation (B)

| Tool | Language | Notes |
|---|---|---|
| `preserve_apo.py scan` / `restore` | Python | whole tree or `--pairs`; `--delta`, `--full-copy`, phindex shortcut for identical files |
| `dng-archive preserve` / `restore` | OCaml / OxCaml | `--pairs` only; record-compatible with Python. Semantic Unison uses it. |
| `byte-verify` / `byte-verify-oxcaml` | Rust / OCaml | native byte workers for `preserve_apo.py` (`DNG_BYTE_BACKEND`) |

### Year archives (D)

| Tool | Language | Notes |
|---|---|---|
| `preserve_years.py`, `unified_pass.py` | Python | writes D, overlapping byte and metadata work |
| `finalize_year.py`, `finalize_preservation.py` | Python | verify a year; deletion-readiness report |

### Metadata sections (C)

| Tool | Language | Engine | Notes |
|---|---|---|---|
| `portable_metadata.py` | Python | Perl ExifTool, or exiftool-rs through native workers | writes batches by running ExifTool over every file |
| `metadata-extract`, `metadata-extract-oxcaml`, `metadata-extract-exiftool-rs-oxcaml` | Rust / OCaml | Perl, or exiftool-rs in process | native extraction workers for the above |
| `metadata_index.py` / `dng-archive index` | Python / OCaml | — | build the SQL index from existing batches; identical results |
| `phindex_metadata.py` | Python | exiftool-rs, from phindex | batches from two phindex indexes, no file reads; values over 1 KiB as hashes |
| `dng-archive phindex-metadata` | OCaml | same | being added (OCaml port of the above) |
| `unison-dngscan -semdiff -preservefull` | OCaml (Semantic Unison) | exiftool-rs | batches per run, plus pair lists that `dng-archive preserve` then preserves |

### Develop settings (F)

`develop_settings.py` (Python) and `develop-settings` (Rust), writing the same archive.

### Indexes (G) and engines

- **`phindex` / `phindex-ox`:** Rust and OxCaml, writing the same index from the `phidentify` library.
- **Engines:** `dng-exiftool-ffi` (Rust) and `dng-exiftool-rs` (OCaml) wrap exiftool-rs 0.12.0, used by phidentify and the exiftool-rs workers. Perl ExifTool 13.59 is the reference engine.

## The main differences

- **One rule places the byte section** for every tool: `ARCHIVE` itself when it
  has `recovery.sqlite3` and no `bytes/` (legacy), the path itself when it ends
  in `bytes`, else `ARCHIVE/bytes` (new archives). Metadata, provenance, run
  logs and `ARCHIVE.json` stay at the top. See [dngc.md](dngc.md#archive-layout-where-the-byte-section-is).

- **Only byte preservation (B) and year archives (D) can rebuild a file.**
  Comparison reports (A), metadata sections (C) and phindex indexes (G)
  describe what differs; B and D keep the bytes.
- **Engines are never mixed within one archive's metadata.** Perl ExifTool is
  the reference engine; exiftool-rs records are a separate metadata identity,
  stamped `ExifTool:DngCompareEngine`. Tools that add to an archive refuse one
  whose metadata came from the other engine.
- **Long values:** stored in full in B, D, and C made by extraction
  (`portable_metadata.py`, Semantic Unison's semdiff); shortened to
  `long-sha256:<hex>:<length>` in A, G, and C made from phindex.
- **Coverage:** an archive made from a list of pairs (`--pairs`) covers only the
  listed files. Files with no counterpart must be listed without a reference to
  be kept as full copies.

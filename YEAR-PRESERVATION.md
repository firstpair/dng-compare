# Configurable year preservation

This workflow preserves an exact byte-recoverable source tree against a reference,
then records metadata, embedded XMP and DNG image comparisons. It requires explicit
roots; it never infers mounted volume names. The public code has no running-job
state or completion receipts. See [the manual](docs/dng-compare-manual.md) for the comparison tools
and installation instructions.

## Scope and invocation

Source roots contain direct `YYYY` children. Give each source a simple unique
label; the output becomes `ARCHIVE/LABEL/YYYY`. Repeat `--source-root` for another
source tree. Only year directories are included: other siblings, including an
`lrmobile` directory, are excluded by this workflow. Each selected reference year
must exist. The archive must be disjoint from all source/reference trees, including
symlink aliases. The exact source labels, canonical roots and year list are saved
in `scope.json`; an incompatible existing scope stops rather than silently changing
its meaning.

```sh
uv run --no-project --with-requirements requirements.txt python preserve_years.py /archives/preservation \
  --source-root ArchiveA=/photos/archive-a \
  --source-root ArchiveB=/photos/archive-b \
  --reference-root /photos/master \
  --database-backend sqlite --preservation-workers 2 --rate-mib 200
```

Build `dng-subset` first with `make build-rust`; the year workflow uses it to archive
embedded TIFF XMP. Source and reference originals remain read-only. All formats
can be preserved as exact bytes, but the pixel classifier handles DNG only. Invalid
or undecodable files are preserved with diagnostics; their pixel result remains
unverified.

The default database is SQLite. `--database-backend turso` opts into the pinned
embedded Turso backend. It requires `pyturso==0.8.2`; it is not a cloud database
connection. Engine markers and migration backups are archive bookkeeping. A closed
Turso checkpoint is exported to SQLite-readable WAL mode. The Turso wrapper's
read-only SQL checks do not mean physically read-only access: opening/closing may
update engine markers, journal modes and checkpoint files. Use ordinary SQLite
read-only access only after the owning process has closed and checkpointed.

## Matching and preservation

A same-relative-path reference is preferred, then a unique case-sensitive filename
within that year. All candidate lists and selections are retained in `matching.json`.
Ambiguous or absent references get complete original copies. Exact source bytes are
preserved with whole-file SHA-256, independently decoded and hashed VCDIFF deltas,
or complete originals. A delta archive depends on unchanged or hash-identical
reference bytes; it is not an independent second backup.

Typed full ExifTool JSON values, binary metadata and recorded filesystem attributes
are exported to gzip JSON Lines and indexed in `metadata/differences.sqlite3`.
Embedded metadata, filesystem facts and layout fields are distinct from sensor
samples. In the historical portable schema, `apo`, `apo_metadata`, `apo_stamp`,
`apo_filesystem` and `apo_value` mean the selected source; `bumer` means the selected
reference. Directional `added_in_bumer`, `missing_in_bumer` and `changed_in_bumer`
labels are retained for schema compatibility, not literal disk names. Missing,
null and empty values stay distinct. Consult [METADATA-QUERIES.md](METADATA-QUERIES.md).

`xmp-original` contains exact embedded TIFF tag-700 packets, with per-packet SHA-256
and source identity. Existing XMP sidecars are preserved as files by the byte and
metadata passes, rather than by the embedded-XMP extractor. Raw DNG samples and
auxiliary previews are classified separately in `image-comparison`. Whole-file
SHA equality is reused; differing encodings are decoded by the Python comparison
where supported. Decoder errors remain unverified.

## Parallelism and resuming

Byte preservation uses `--preservation-workers 1..36`; the coordinator owns SQLite
and workers own distinct payloads. `--preservation-backend python|rust|oxcaml` selects
the whole-file verification implementation. OCaml's SHA binding uses macOS
CommonCrypto. Native metadata extraction requires `--metadata-workers 1..36` and
`--metadata-backend rust|oxcaml`; these tools launch ExifTool and preserve its JSON,
rather than implementing their own metadata parser. Build the selected executables
before invocation.

Unified byte/metadata processing is the default. A shared inventory and bounded
readiness notifications permit verified byte work to overlap metadata extraction.
`--sequential-pass` keeps the phases separate. Metadata shards, selected through
`--metadata-shards`, use separate extraction/compression processes with one index
owner. ExifTool reads are outside the byte-pass rate budget. Final image
classification has separate `DNG_IMAGE_WORKERS` and `DNG_IMAGE_RATE_MIB` settings.
Worker counts are limits, not promises of throughput: spinning disks may slow down
with extra seeks and decoding may exhaust memory.

The `.running` directory rejects two year coordinators for one archive. Separate
byte and metadata flock locks protect standalone phase ownership while allowing
the intended overlap. If interrupted, verify the old process is gone before
removing the year coordinator's stale `.running` directory. Completed byte records
and metadata/XMP batches are reused only through their retained identity and
payload-checksum gates. `--start-at LABEL/YYYY` validates prior year reports before
skipping them. An archive from the earlier operator-specific workflow has a
different scope schema: retain it and use its archived tools, or choose a fresh
public output directory. Do not rewrite old provenance to make it fit.

## Completion and recovery

`verification.json` distinguishes `archive_verified`, `comparison_complete`,
`verified`, `input_diagnostics` and `execution_issues`. Format diagnostics may leave
`verified` false while exact recovery and coverage pass; execution/coverage/checksum
errors block completion. Root `summary.json` appears complete only after every
selected year finishes. These reports never delete sources or replace a second
independent copy.

Restore one archived file to a new, absent destination:

```sh
uv run --no-project --with-requirements requirements.txt python preserve_apo.py restore \
  /archives/preservation/ArchiveA/2020 relative/path/photo.DNG /restored/photo.DNG \
  --reference-root /photos/master/2020
```

Restore validates reference and reconstructed source SHA-256, refuses an existing
output and reapplies recorded xattrs, modes and timestamps where supported.
Ownership, filesystem flags and directory attributes are recorded for explicit
platform-specific handling; they are not all automatically restored. Preserve
integer nanosecond timestamps with integer-aware readers. Keep source trees and
a second copy until an independent restore has passed.

## Optional controlled benchmarks

Benchmarks are advanced, explicit jobs on selected archive years, not background
services. `benchmark_metadata_workers.py` requires `--year LABEL/YYYY` and selects
18 DNG pairs; `--path-contains` can narrow the dataset. `benchmark_byte_workers.py`
requires `--year LABEL/YYYY` and 12 byte-equal plus 12 byte-different pairs. The
Turso metadata benchmark requires `--year LABEL/YYYY`, eight DNG and eight JPEG
pairs and optional `--path-contains`; `--dataset leica|fuji` chooses a report label,
not automatic camera detection. The memory benchmark consumes the worker timing
report's selected paths.

By default benchmarks signal no process. Pausing an existing job requires both
`--live-pid PID` and `--pause-live-job`; the PID must belong to the current user and
invoke this project's preservation or metadata script. Its same-user descendants
are stopped only during the trial and resumed in cleanup, including freeze failure.
Only use this option for a job you own. Trials preserve complete typed-value parity,
excluding read-dependent access dates from timing equality. Results include input
and executable provenance; warm-cache sampled timings are not production disk
throughput or universal worker-count recommendations.

# Publication build and fixture validation

Validated on 2026-10-12 on macOS 26.2, Apple Silicon. These results apply to
generated temporary fixtures and this source release. No photographs, mounted
photo trees, Lightroom catalog, live archive or remote preservation job were
read or changed during publication validation.

## Toolchain actually used

| Component | Observed version |
| --- | --- |
| Rust | `rustc 1.99.0 (b940084d7 2026-09-28)` |
| Cargo | `cargo 1.99.0 (5f94df478 2026-08-27)` |
| Rust edition | 2024 |
| OCaml native compiler | Stock OCaml 5.5.0 |
| Python, managed by uv | CPython 3.14.6 |
| uv | 0.11.32 |
| ExifTool | 13.55 |
| xdelta3 | 3.2.2 |
| NumPy / tifffile / imagecodecs | 2.5.3 / 2026.9.20 / 2026.8.16 |
| Embedded Python Turso | pyturso 0.8.2 |

The Rust toolchain is pinned to **1.99.0**, rather than relying on the host's
default compiler. `rust-scanner/Cargo.toml` uses edition 2024. `uv.lock` and the
Cargo lockfile are included. No compiler version was substituted silently.

## Executed checks

```sh
uv sync
make build
make test
make test-turso
cargo clippy --locked --manifest-path rust-scanner/Cargo.toml --all-targets -j 4
ocamlopt -O3 -I +unix -c turso_oxcaml_stubs.c turso_oxcaml.ml turso_probe_oxcaml.ml
```

`make build` succeeded for all four default Rust executables and both native
OCaml workers. The workers retain their historical `-oxcaml` filenames for the
existing wire-format dispatcher; the tested source builds with standard OCaml.
The byte worker uses macOS CommonCrypto and has **not** been verified on Linux.
The Rust sources use Unix APIs and have **not** been verified on Windows.

The final default fixture suite passed **36 tests** in 7.672 seconds. The same
suite also passed all 36 with `DNG_DATABASE_BACKEND=turso` and
`DNG_METADATA_SHARDS=2` in 6.852 seconds. Its tests cover:

- Metadata changes and moved TIFF offsets without changed raw samples;
  compression and byte-order differences; changed and truncated image data.
- Same-name candidates, partial verdicts, successful resuming and cache
  invalidation after a source change.
- Whole-byte SHA-256, VCDIFF reconstruction, archive corruption and preserved
  originals when a reference is missing.
- Rust/OCaml extraction ordering and typed-value parity, native byte-worker
  parity, overlapped processing, interruption and failure propagation.
- Embedded Turso concurrent transactions, typed values, legacy checkpoint
  migration and a closed checkpoint readable by ordinary SQLite.
- A generic year pipeline with new root labels, a clean-clone preservation and
  restoration without historical ledgers, disjoint output and exclusive owner
  guards, literal aliases, scoped export and foreign-partial preservation.
- Explicit benchmark pause admission, refusal for unrelated or foreign-user
  processes, and restoration when a controlled mocked freeze fails. No live
  process was signaled by these tests.

Four of these tests specifically execute the release `dng-subset` binary on
synthetic TIFF/DNG fixtures. They verify confirmed equality plus resume,
changed-image rejection, missing-candidate rejection and an incomplete pilot.
The Rust binary now exits 1 if complete raw subsumption is unconfirmed; it
retains its reports. `cargo test` succeeded, but the four binary targets contain
**zero Rust unit tests**: it must not be counted as another body of assertions.
The meaningful native coverage is the fixture suite described above.

Clippy finished successfully with nine inherited style/complexity warnings.
No `-D warnings` run or warning-free claim is made. The Python suite emitted
nonfatal `ResourceWarning` messages for SQLite handles during fixture tests.
That cleanup scope remains a limitation; a passing verdict
does not mean every exceptional resource path has been proven leak-free.

The Makefile benchmark targets were also checked without opening an archive:
five argument admission/refusal cases and three command dry runs passed.
Metadata timing and Turso benchmarks require an explicit `YEAR=LABEL/YYYY`;
memory measurement reuses its existing timing report. Default commands do not
signal a live job. Supplying `LIVE_PID` requires `PAUSE_LIVE_JOB=1`, and a pause
requires its PID; the Python owner checks still apply before any actual signal.

## Optional and unverified scopes

The optional Turso Rust dependency graph was resolved with its pinned 0.8.2
packages. All OCaml Turso binding/probe sources compiled to objects. The full
Rust Turso SDK release build, final OCaml SDK link and both native Turso probe
executions were **not** run during this publication check. They remain an
explicit experimental path; the default build does not compile them.

The fixture tests do not prove every camera/container combination, all DNG
opcodes, every auxiliary image or arbitrary malformed inputs. They are not
Lightroom fingerprint-oracle tests. No throughput claim comes from this run.
Existing report concurrency, Rust metadata-resume and completion-only `--after`
limitations are described in the manual; the fixture results do not remove
them or authorize deleting original files.

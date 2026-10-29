# DNG Compare publication record

## Public source and manual

Published on 2026-10-12 under the MIT license at
[firstpair/dng-compare](https://github.com/firstpair/dng-compare).
All authored Python, Rust, OCaml and C sources are included. The complete Turso
MIT notice accompanies the retained upstream header. Private photograph trees,
execution reports, inventories, environments and generated native binaries are
excluded; the original project history remains preserved privately.

The [library entry](https://firstpair.org/books/dng-compare/) is on the HOWTO shelf
alongside Lightroom Repair. The full manual is available as:

- [PDF](https://firstpair.org/dng-compare/pdf/)
- [EPUB](https://firstpair.org/dng-compare/epub/)
- [HTML](https://firstpair.org/read/dng-compare/)
- [Chapter reader](https://firstpair.org/read/dng-compare/chapters/)

The current edition is `1.1.0-bccb446c`, built from source commit
`bccb446cf6aef2e249534b78f160066ddcb486e2` and committed in release `5de436a`
on 2026-10-29. It adds the "Develop settings across editors" section: capturing
develop settings from DNG XMP, exported JPEGs, Lightroom Classic catalogs and
Capture One `.cos` files with the Python `develop_settings.py` and the Rust
`develop-settings`, which write the same archive. The HOWTO gains a matching
step. The publisher uploaded only this title; the local catalog, production
build and complete site smoke passed. FirstPair catalog/manifests were
committed and pushed as `51f0eaf`, and the Vercel Git integration deployed them
to firstpair.org.

Independent [live verification](live-publication-verification.json) on
2026-10-29 confirmed that the hosted catalog is byte-identical to the committed
catalog and the title stays on HOWTO. The live PDF and EPUB match the local
SHA-256 and sizes below, and the single HTML reader and chapter 17 serve the
new sections. Hosted readers include the publisher's navigation wrapper, so
their complete bytes differ from the standalone HTML artifacts.

The previous edition, `1.0.0-27449a8f` (release `3623b62`, FirstPair `51a0ebb`,
deployment `dpl_14UhEji7Ce7jAcZWYS6GZLKFAsPX`), added the optional exiftool-rs
engine section. The first edition, `1.0.0-3553ec2f`, was published on
2026-10-12 (release `57099b2`, FirstPair `2688a08`, deployment
`dpl_DJBf8qfv8kc43znYRgwf7R59oRdo`).

## Executed validation

For this edition, `make test` passed 49 Python tests, with the 2 optional
exiftool-rs worker tests skipped because those workers were not built, and the
Rust tests passed, including four `develop-settings` unit tests. The develop
settings tests use synthetic DNG, JPEG, catalog and `.cos` fixtures and check
that the Python and Rust implementations write equal records and read each
other's archives. The previous edition's exiftool-rs engine results (eight Rust
tests, OCaml wrapper tests on stock OCaml 5.2.0 and OxCaml 5.2.0+ox) and the
first edition's 36 Turso fixture tests were not repeated.
The [code validation record](../BUILD-VALIDATION.md) lists exact compiler and
dependency versions, platform limits, inherited warnings and experimental
native Turso scopes. No real photographs or Lightroom catalogs were read or
changed during publication validation.

The FirstPair builder verified the PDF layout and the library book contract
([artifact validation record](../dist/BUILD-VALIDATION.json)). All 19 PDF pages
were rendered and visually reviewed; three long commands that wrapped mid-option
were split with line continuations before the final build. The EPUB imprint page
carries the version and firstpair.press.

| Artifact | SHA-256 | Bytes |
| --- | --- | ---: |
| PDF | `ae29217b8d39b50f4ce89e988dc37ae660351895ea88df1f87c23b06d36058ec` | 4,242,813 |
| EPUB | `7b19192febb0fb193ba7424a265df9fbf834aa8de2c09eedbd50b2e8e7320e67` | 6,029,979 |
| HTML | `992dd4ae48217b66f9afe538afcfaba6ff431254d557431f51516a42736a8b21` | 4,134,000 |
| Supplied cover | `71c328c81eeb6f4141a537f3b9707b252ddaa2434773b2b5674d85c40ce6f4af` | 3,038,273 |

The versioned PDF and EPUB were delivered as regular files through the normal
FirstPair iCloud Books workflow. Both copies compare exactly with their source
artifacts. This record does not certify a real-photo archive comparison or
authorize deleting any original tree.

## Blog handoff

Canonical source: [the prepared article](blog/dng-compare/post.md).
Destination: `firstpair.press`, slug `dng-compare`.

The stamped [TextPack](blog/dng-compare/dist/dng-compare.textpack) and versioned
`dng-compare (1.0.0-842c86).textpack` handoff are committed and pushed. ZIP
integrity, routing, exact cover bytes and full source provenance were checked.
The normal FirstPair delivery wrapper copied the already committed pack to
iCloud Blogs, and an exact comparison passed.

- Provenance schema: `omnighost-textpack-v1`.
- Source commit: `842c861eb4aaf3db97e1243edd28e81387f48e00`.
- Payload SHA-256: `1d403f5f751cfc504c25bf085b45d300a004e45beac36724a6c666be5291d551`.
- Pack size: 3,005,417 bytes.

File delivery is complete. The article is prepared for the operator's normal
Obsidian/Omnighost publication; no Ghost post was submitted in this release.
Add a library Story link after the canonical article is live and verified.
The canonical blog URL returned HTTP 404 in the independent live check;
this is a completed file handoff, not an already published article.

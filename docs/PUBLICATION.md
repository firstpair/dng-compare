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

The current edition is `1.0.0-27449a8f`, built from source commit
`27449a8f3ee0565455aaa15619bd5ffb3c192492` and committed in release `3623b62`.
It carries the exiftool-rs 0.12.0 wording and the optional engine section, and
was built with the latest FirstPair toolchain (Typst 0.15.1, Pandoc 3.12.1,
Poppler 26.10.0, calibre 9.16.0). The publisher uploaded only this title.
FirstPair catalog/manifests were committed and pushed as `51a0ebb`, and
production deployment `dpl_14UhEji7Ce7jAcZWYS6GZLKFAsPX` became READY and was
aliased to firstpair.org. The local catalog, production build and complete
site smoke passed.

Independent [live verification](live-publication-verification.json) on
2026-10-18 confirmed that the hosted catalog is byte-identical to the committed
catalog and the title stays on HOWTO. The live PDF and EPUB match the local
SHA-256 and sizes below, and the single HTML reader and chapter 17 serve the
new text. Hosted readers include the publisher's navigation wrapper, so their
complete bytes differ from the standalone HTML artifacts.

The first edition, `1.0.0-3553ec2f`, was published on 2026-10-12 (release
`57099b2`, FirstPair `2688a08`, deployment `dpl_DJBf8qfv8kc43znYRgwf7R59oRdo`).

## Executed validation

For this edition, `make test` passed all 42 Python tests, and the optional
exiftool-rs engine passed its eight Rust tests and the OCaml wrapper tests on
stock OCaml 5.2.0 and OxCaml 5.2.0+ox. The first edition's default and
embedded Python Turso fixture suites passed 36 tests.
The [code validation record](../BUILD-VALIDATION.md) lists exact compiler and
dependency versions, platform limits, inherited warnings and experimental
native Turso scopes. No real photographs or Lightroom catalogs were read or
changed during publication validation.

The [artifact validation record](../dist/BUILD-VALIDATION.json) records all 22
manual sections across PDF, EPUB, single HTML and split chapters; 458 local
EPUB/HTML references with none broken; exact source links; and unchanged
supplied cover bytes. All 16 final PDF pages were rendered and visually
reviewed. All 23 named and outline destinations resolve into the active page
tree, and all 31 link annotations are present. The added destination is the
"Optional exiftool-rs engine" subsection.

| Artifact | SHA-256 | Bytes |
| --- | --- | ---: |
| PDF | `86943369c553de2c6a430e6ba1bd36f682ac38c15a1b2b92a8a1fa222e5df89a` | 4,230,388 |
| EPUB | `669cc7d5274e7eb28de5a5bf1257815dc68c9871b58c08e82a3cb2f76b8cb681` | 6,027,617 |
| HTML | `331f2d97b52f8e784ce00f549e9268fd6040a9b3823d6c1a8eda2295cac2b736` | 4,125,548 |
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

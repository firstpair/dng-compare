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

The edition is `1.0.0-3553ec2f`, built from source commit
`3553ec2ff5559d64dbfeec1b7ae7d328bb24b0d9` and committed in release `57099b2`.
The publisher uploaded only this title. FirstPair catalog/manifests were committed
and pushed as `2688a08`, and production deployment
`dpl_DJBf8qfv8kc43znYRgwf7R59oRdo` became READY and was aliased to firstpair.org.
The local catalog, production build and complete 37-entry site smoke passed;
all 36 prior catalog entries were preserved by this publication.

Independent [live verification](live-publication-verification.json) on
2026-10-12 at 18:53 UTC confirmed that the hosted catalog is byte-identical to
the committed catalog, all 36 earlier entries are unchanged, and both titles
are on HOWTO. The live PDF, EPUB and cover match the local SHA-256 and sizes
below. The single HTML reader, chapter index and a concrete chapter returned
HTTP 200 with the standard library navigation. Hosted readers include the
publisher's navigation wrapper, so their complete bytes differ from the
standalone HTML artifacts.

## Executed validation

Both default and embedded Python Turso fixture suites passed 36 tests.
The [code validation record](../BUILD-VALIDATION.md) lists exact compiler and
dependency versions, platform limits, inherited warnings and experimental
native Turso scopes. No real photographs or Lightroom catalogs were read or
changed during publication validation.

The [artifact validation record](../dist/BUILD-VALIDATION.json) records all 22
manual sections across PDF, EPUB, single HTML and split chapters; 476 local
EPUB/HTML references; exact source links; and unchanged supplied cover bytes.
All 16 final PDF pages were rendered and visually reviewed. All 22 named
destinations and outline destinations resolve into the active page tree, and
all 30 link annotations remain present.

An initial cover merge with `pdfunite` rendered correctly but left table-of-
contents destinations pointing to unreachable duplicate pages. Those builds
were retained privately and never uploaded. The shared FirstPair builder was
corrected in `a232ebe` to use validated pypdf append operations, with seven
generated controls covering native Typst navigation and refusal of invalid
destinations. The corrected book's rendered pages remained pixel-identical to
the table-corrected earlier build. A nonfatal pypdf annotation-size warning is
recorded; complete actual target/count comparisons passed.

| Artifact | SHA-256 | Bytes |
| --- | --- | ---: |
| PDF | `f7155f4d50fe219306c7d23d9ef0a855c3da004600c750acbd4653b2bb2dafa4` | 4,225,267 |
| EPUB | `a0afc5e033fb3b782549da434e8e20fb6bd12bac149a1d4a58fcba70389fd053` | 6,026,403 |
| HTML | `bded2bc2e366b06bb949079b94031b51f9c1bdf35560e20ca873366c7c53a69a` | 4,121,489 |
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

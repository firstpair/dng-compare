# XMP structure and the exiftool-rs engine

Design review of what the optional `exiftool-rs` metadata engine loses
relative to Perl ExifTool, whether dng-compare needs it, and how the fork at
https://github.com/alexy/exiftool-rs (upstream https://github.com/Le-Syl21/exiftool-rs)
closed the gap. Upstream merged that work on 2026-10-17 and released it in
0.11.0; dng-compare now links the crates.io release 0.12.0 (see the last
section). Measurements below were taken on 2026-10-16 with
exiftool-rs 0.10.0, Perl ExifTool 13.59 (the release the crate mirrors) and
synthetic fixtures only.

## Identities at stake

dng-compare keeps several identities apart: encoded-block equality, decoded
samples, rendered appearance, whole-file SHA-256, native Lightroom
fingerprints and metadata. The metadata snapshot is descriptive. The
authoritative XMP is preserved twice regardless of engine:

- the byte pass keeps every original file exactly;
- `dng-subset --archive-xmp` stores each embedded TIFF tag-700 packet with its
  own SHA-256 under `xmp-original`.

So a weaker metadata engine cannot lose develop settings. It can only make the
metadata *comparison* coarser and make archives from different engines
incomparable with each other. Every exiftool-rs record carries
`ExifTool:DngCompareEngine` for that reason.

## What flattening loses

Perl ExifTool with `-struct` keeps Lightroom's nested XMP as nested JSON:
`Look`, `RetouchAreas`, `PaintBasedCorrections`, `GradientBasedCorrections`,
`MaskGroupBasedCorrections` and their inner mask lists. Without `-struct` the
same data becomes one flat tag per leaf field. Two things disappear:

- Association between sibling fields. A list of three masks becomes parallel
  arrays of `What`, `MaskValue`, `Dabs`; editing one field of the second mask
  shows up as several unrelated changed tags.
- Nesting depth. Lists inside list elements are joined into one flat list, so
  element boundaries are ambiguous. On the fixture, two corrections holding
  two and one masks became `CorrectionMasksMaskValue: [1, 0.5, 0.7]`.

exiftool-rs 0.10.0 has no struct value type at all (`Value` has String,
integers, rationals, floats, Binary, List, Undefined), and its `-struct`
command-line flag was parsed as a synonym for short tag names.

## Discrepancies found under dng-compare's flags

dng-compare extracts with `-j -a -G0:1:4 -n -struct -b -u -U`. The crate's own
parity harness compares Perl `-s -G1` default print output over 195 corpus
files, so none of those flags were ever measured upstream. On a 16x16 DNG
carrying Lightroom-style XMP, 24 of 41 tags matched. The rest:

| Discrepancy | Cause | Fix location |
|---|---|---|
| `DNGVersion` printed as a JSON array instead of `"1 4 0 0"` | dng-compare adapter emitted every list as an array; ExifTool only does so for list-typed tags | `rust-scanner/exiftool-ffi` (fixed) |
| `FileType` `Dng`, raw value was the enum variant name | crate pseudo tag | fork (fixed) |
| `FileTypeExtension` casing: Perl raw `DNG`, print `dng` | crate pseudo tag | fork |
| `Directory` empty for a bare file name, Perl prints `.` | crate pseudo tag | fork (fixed) |
| `ExifByteOrder` long form under `-n`, Perl prints `II`/`MM` | crate stores PrintConv as raw, in two places | fork |
| `ImageSize` `16x16` under `-n`, Perl prints `16 16` | composite raw value was the print form | fork (fixed) |
| Equal repeated list items collapsed (`What: "Correction"` for two masks) | XMP aggregation dropped repeats of equal print values | fork (fixed) |
| `ExifToolVersion` reports the crate version | by design upstream; left as is, `DngCompareEngine` carries both versions | none |
| `Look`, `MaskGroupBasedCorrections` flattened | no `-struct` | fork, see below |

## Perl `-struct` rules, measured on the corpus

Comparing Perl 13.59 structured and flattened JSON over the ExifTool test
XMP files gave these rules, which the fork implements:

1. A top-level property whose value is a structure, or a list containing
   structures, becomes one tag named after the property. All flattened tags
   derived from it disappear (`DerivedFromDocumentID` and
   `DerivedFromInstanceID` are replaced by `DerivedFrom`).
2. Flattened names do not always start with the property name
   (`RegionInfo` flattens to `RegionName`, `RegionAreaX`; `ArtworkOrObject`
   to `ArtworkTitle`; `KeywordInfo` to `HierarchicalKeywords1`). Suppression
   therefore cannot be a name-prefix rule.
3. Every list-typed XMP tag is emitted as a JSON array under `-struct`, even
   with one item (`Creator: ["one"]`, `ISO: [400]`). Language alternatives
   are not lists (`Title: "T"`).
4. Struct field keys are the capitalised local names, sorted alphabetically.
   A language alternative inside a struct expands to `Field` (x-default) and
   `Field-lang`. A field from a foreign namespace in an open struct is
   qualified (`Extensions: {"XMP-exif:Flash": ...}`).
5. Leaf values keep ExifTool's ValueConv: dates become
   `YYYY:MM:DD HH:MM:SS+TZ`; the JSON typing rule still applies, so `1.000000`
   is a number and `False` becomes `false`.
6. Copy numbering is global by tag name, so a struct tag named `Flash`
   shifts the `CopyN` of a later EXIF `Flash`.

## Fork design for `-struct`

The streaming XMP reader (3,900 lines) flattens as it parses and carries the
flat-name overrides of rule 2. Rather than teach every emission site about
structures, the fork keeps that parser unchanged and adds a tree pass:

- `Options.struct_output` (CLI `-struct`), threaded to the XMP reader through
  the same thread-local pattern the crate uses for `-a`.
- `Value::Struct(Vec<(String, Value)>)` with ExifTool's text serialisation
  (`{Key=Value,List=[a,b]}`) for non-JSON output.
- A second pass builds a tree of each top-level property, classifies struct
  roots, and re-serialises the packet *without* those roots. Reading the
  pruned packet with the unchanged streaming parser yields exactly the
  non-struct tags, overrides included, which sidesteps rule 2 without
  replicating it. Struct tags are then inserted at the position of the next
  non-struct property in document order.
- Single-item Bag/Seq values stay lists when `struct_output` is on (rule 3).
- JSON output renders struct values recursively with sorted keys, scalars
  typed by the existing number/boolean rule, dates converted with the
  reader's `convert_xmp_date`.

Known limits of the first version: struct leaves receive ValueConv (dates)
but not PrintConv, so `-struct` without `-n` shows raw leaf text where Perl
would show converted labels; dng-compare always uses `-n`. Insertion order
is a heuristic that only affects `CopyN` numbering of same-named tags.

## Effort and maintenance

| Tier | Work | Status |
|---|---|---|
| Adapter `-n` list rule | hours | done in dng-compare |
| Pseudo-tag and ImageSize raw values, equal list items | days, small self-contained patches | in the fork, candidates for upstream pull requests |
| `-struct` reader and JSON output | weeks | in the fork |
| Parity under dng-compare's flags over a real archive | open-ended; run the crate's parity harness with `-n -a -G0:1:4 -struct` on an archive | not started |

Upstream is one maintainer with two-week releases and only dependabot pull
requests merged so far. A fork carrying a deep parser change must rebase
across those releases; the tree-pass design keeps the diff to the streaming
parser near zero to make that cheap. The crate mirrors ExifTool 13.59 tables
while the Homebrew Perl ExifTool is 13.10, so some value differences in a live
comparison are version drift, not engine bugs.

## Results of the fork work (2026-10-16)

Branch `dng-compare-parity` of https://github.com/alexy/exiftool-rs, commit
`3cd699a`, pinned in `rust-scanner/exiftool-ffi/Cargo.toml`. Under
`-j -a -G0:1:4 -n -struct -b -u -U` against Perl 13.59, ignoring file dates:

| File | Before | After | What remains |
|---|---|---|---|
| Lightroom-style DNG fixture (`x.DNG`) | 24 of 41 | 28 of 28 | nothing |
| Lightroom-style XMP fixture (`h.xmp`) | — | 11 of 11 | nothing |
| ExifTool corpus XMP4, XMP7, XMP8 | partial | all equal | nothing |
| XMP5 (regions, keyword hierarchy) | 6 of 17 | 12 of 16 | `CopyN` order of `Flash` |
| XMP.xmp | 42 of 73 | 59 of 74 | composite `-n` values, APEX conversions |
| DNG.dng (Canon) | 151 of 1942 | 213 of 1943 | 1,562 unknown Canon MakerNotes under `-U`, composite and MakerNotes `-n` values |

Fixed in the fork: `-struct` (new tree pass, `Value::Struct`), `-G0:1:4`
labels with `CopyN`, `-j` honouring `-n` (JSON printed PrintConv values
before), `-b` with `-j` as `base64:`, pseudo-tag raw values, equal repeated
list items, ValueConv for undefined-typed EXIF tags, XMP dates, PLUS codes and
rational lists. Fixed in dng-compare: the adapter's list rule. The fork's
`tests/dng_compare_parity.rs` pins these shapes; upstream's own baselines and
clippy gate still pass.

Not done: ValueConv for most composites and MakerNotes under `-n` (raw holds
the print text), `-U` unknown MakerNote coverage, ExifTool's priority-based
`CopyN` displacement, `-struct` without `-n` (leaf PrintConv). Blank-node
(`rdf:nodeID`) Descriptions were fixed upstream in 0.11.0, see below. None of these change what dng-compare compares
when both sides use the same engine; they matter only for reading archives
side by side with Perl output.

## Recommendation

Keep Perl ExifTool as the engine of record for the metadata snapshot until
the engine's `-struct` output is verified on a real Lightroom archive with the
parity harness. Treat exiftool-rs records as a separate identity either way.

## Upstream release (2026-10-17)

Upstream merged pull request #20 on 2026-10-17 and released 0.11.0 and
0.12.0 the same day. `rust-scanner/exiftool-ffi/Cargo.toml` now pins the
crates.io release `=0.12.0`; the fork branch is no longer linked. The PR
branch was rebased before the merge, so the former pin `3cd699a` is not on
any branch of the fork any more, which is a second reason to depend on the
release. Upstream's own changes in the two releases:

| Change | Effect on dng-compare |
|---|---|
| An unreferenced `rdf:nodeID` Description read its lists twice (5143790) | Fixed a real bug: a sidecar `dc:creator` Seq of Ann, Bob read as `["Ann", "Ann, Bob"]` and a one-item Bag as `["x", "x"]`; our `-struct` pass keeps equal repeats, so it reached our records. Now equal to Perl 13.59; pinned by the FFI test `unreferenced_node_id_description_reads_lists_once` |
| `-struct` text escaping follows XMPStruct.pl (226fe30) | None: the FFI emits nested JSON, not the serialised text form |
| `Value`, `Error`, `FileType` marked `non_exhaustive` | None: the adapter already matches with a wildcard arm |
| Write path: exclusive temporary file; `write_info` counts only changes | None: dng-compare only reads |

Measured with the same flags against Perl 13.59 on the 13 files above
(`x.DNG`, `h.xmp`, XMP through XMP9, DNG.dng): apart from the two version
stamps, 0.12.0 output is identical to the former fork pin, and the remaining
gaps listed above are unchanged. The nodeID shape is absent from that corpus
(XMP2.xmp only looked right because equal duplicates were merged), which is
why it is now a dedicated test. Upstream's gate at v0.12.0 passes: `cargo fmt
--check`, `cargo clippy --release --all-targets -- -D warnings` and
`cargo test --release`, including the parity tests from PR #20.

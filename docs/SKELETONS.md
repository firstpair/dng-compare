# Skeletons: two designs for an archive whose reference is still in use

This note records how the skeleton section of the DNGC archive came about:
the problem, a first design that was not built, and the second design that
was. The exact format is in
[dngc-archive-spec.md](dngc-archive-spec.md), sections 5.5 and 13;
the manual's Parts VI and VII describe it for readers.

## The problem

A DNGC archive preserves one tree of files by reference to another. Most of
its records are small because the reference holds the bytes:

- a **delta** record decodes against a whole reference file and is checked
  against `reference_sha256`;
- an **identical** record only points at the reference file.

The reference is usually the working library, the tree a photo editor has
open. When Lightroom writes a rating or develop settings into a DNG, or Photo
Mechanic writes a caption into a JPEG, the file's bytes change, and every
record that names it stops restoring. In the archive that prompted this work
that meant about 1.7 million records, all depending on a library in daily use.

The archive should be able to look at a file that has been written into and
still reproduce it as it was before the edits, and from that any copy
recorded against it.

Both designs rest on one observation: these tools rewrite metadata and leave
the image data, which is most of each file, alone.

## First design: essence and envelope recipes (not built)

### Idea

Split every file along its own structure into two parts.

- **Essence:** the image data editors never rewrite.
  - TIFF-based raws (DNG, NEF, CR2, ARW): the raw image's strips or tiles.
  - CR3 and HEIC: the raw or image `mdat` data.
  - RAF: its raw block.
  - JPEG: the entropy-coded scan.
  - Unknown formats: content-defined chunks.
- **Envelope:** everything else: headers, directories, XMP, IPTC, maker notes
  and embedded previews.

An original would be stored as a **recipe**, a new record kind:

```json
{"kind": "recipe", "source_sha256": "…", "size": 51234567,
 "segments": [
   {"pack":    "9f…", "len": 65536},
   {"essence": "c2…", "len": 48900000},
   {"pack":    "4a…", "len": 2280000}]}
```

- **Essence segments** are found by SHA-256 in a chunk index
  (`hash → path, offset, length, file identity`) built by scanning every
  available copy. An essence that moved inside its file, or a file that was
  renamed, is found again by hash.
- **Envelopes** are stored in the archive in pack files, compressed with zstd
  and a dictionary trained per camera model.
- **The final check is unchanged:** the rebuilt file must match
  `source_sha256`.

### Costs

- **Embedded previews:** an editor can regenerate them, so they are not
  stable essence. Packing them is safe but costs 1–3 MB per DNG; referencing
  them by hash is cheap but they can be lost.
- **A conversion of the whole archive:** every preserved file rebuilt and
  re-recorded as a recipe, which meant reading about 18 TB, and finishing
  before the reference was edited further.
- **New machinery:** a chunk index, pack files, per-camera dictionaries, a
  format-version bump, and a full hash of all image data in both trees.

### Why it was dropped

Measured against the archive itself, it was far heavier than the problem.
The archive held 498,128 deltas between an older copy of a library and the
same library after years of use in Lightroom Classic and Photo Mechanic:

| Delta size | Files |
| --- | --- |
| under 4 KB | 497,790 |
| 4 KB – 16 MB | 94 |
| 16 MB and over | 244 (about 10 GB of the 10.17 GB total) |

So in 99.9% of real edits, everything but a few kilobytes of the old file
survives inside the edited file, previews included. The first design would
have stored whole envelopes for 1.7 million files and re-read 18 TB to
protect against edits that cost about 1 KB each. It also duplicated what
phindex already keeps for every indexed file: identities that survive
metadata edits (`dng_blocks`, `image_data`).

## Second design: skeletons (built)

### Idea

Two changes, and no conversion of an existing archive.

**1. A reference is a content, not a path.** A record already holds the
reference's SHA-256. That hash is the reference; the path is where it was
last seen. To restore, the reader looks for the content:

1. at the recorded path;
2. anywhere else, through a phindex index;
3. failing that, by rebuilding it from a skeleton.

**2. A skeleton makes a file rebuildable from its own edited version.** A
skeleton holds the small bytes an edit destroys (header, directories, small
tag values, the XMP packet), compressed, plus a map of where each large block
goes: raw strips and tiles, previews, maker notes, profiles.

To rebuild a file as it was before edits:

1. find the live file with the same image identity;
2. walk its structure and take each block, matched by length and a sampled
   fingerprint (the mark);
3. lay the blocks around the stored skeleton;
4. check the whole-file SHA-256.

### What happens on an edit

Lightroom writes ratings into a reference file, changing F0 to F1.

- **The reference before edits:** rebuilt from F1 plus its skeleton.
- **The preserved copy:** its existing delta applies to the rebuilt F0. Every
  existing record stays as it is.

Develop adjustments (exposure, blacks, shadows, tone curve, crop) are covered
the same way, because they live in the XMP packet, which is in the skeleton.
In the archive above, about 13,000 pairs differed in develop settings and
about 48,000 in crops.

### As proposed and as built

| | Proposed | Built |
| --- | --- | --- |
| Storage | a `skeleton` record kind, one compressed file per skeleton | a section, `skeletons/skeletons.sqlite3`, one row per content |
| Block identity | a role and a class (essence, preview, shared) | length and a mark: SHA-256 over the block's first and last 4,096 bytes |
| Blocks shared across files (profiles, opcode lists) | stored once | not built; blocks always come from the edited file |
| TIFF walker | an existing structure library | a small walker of its own, to keep dependencies down |
| Data no tag points at | not foreseen | runs of 64 KB or more are blocks; one maker's files carry 620 KB of it |
| Tiles | one block each | merged when 3 bytes or less apart; the median fell from 107 blocks per file to 7 |
| Size | estimated at a few KB per file | about 10 KB per file measured (below) |

TIFF-based files and JPEG have walkers. A file of any other format up to
1 MiB, such as an XMP sidecar, is stored whole. Larger files of other formats
have no skeleton.

### Measured

- **Real edit pairs:** 1,494 of 1,494 sampled DNGs and 399 of 400 JPEGs were
  rebuilt byte for byte from the edited file and the skeleton. The one miss
  was a truncated copy, not an edit.
- **Stored size on those pairs:** 6.5 KB per DNG, 9.7 KB per JPEG.
- **A 20,000-record trial:** 19,931 skeletons in 611 seconds with 4 workers,
  164 MB; a sample of 300 all rebuilt.
- **The full archive, in progress when this was written:** 662,135 of
  1,710,150 records after 4.6 hours with 8 workers (about 40 records per
  second), 522,502 skeletons in 5.5 GB, or 10.6 KB each.
- **Skipped so far:** 1,963 files with no walker: video (1,736), light-field
  raws (217), and ten others.

### Limits

A skeleton cannot recover a block that the edit destroyed:

- regenerating a DNG's embedded previews replaces them;
- a lossy DNG conversion or a pixel edit replaces the image data.

Of the 298 existing deltas of 4 KB or more, 52 rebuilt and 242 were reported
as missing blocks, because the raw data itself differed. Four matched every
mark and were caught by the final SHA-256, so nothing wrong was accepted.

For those cases the old bytes have to be captured while some copy still has
them, for example as a reverse delta taken before a mirror is updated. That
is not built.

Also not built: splitting the Exif thumbnail out of a JPEG's skeleton.

The index lookup was at first in the Rust library only. It is now in all
three libraries and behind `--index FILE` on `dngc restore`, `verify
--restore-sample` and `keep` in both front ends.

### Ordering

Nothing waits on a conversion. The skeleton pass should finish before the
reference is edited further; a file edited before its skeleton exists is
still covered for as long as another copy holds the old bytes.

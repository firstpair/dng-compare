# dngc speedups: what one large run taught

This note records the fixes made while preserving one real source volume
against its backup with `dngc run`, in November 2026, and what to expect from
the next run of the same kind. The source held 1,709,944 files (713,261 DNGs);
1,704,319 had a counterpart on the backup. Both volumes are USB-attached
spinning-disk arrays; the archive was written to a local SSD and then copied
to the backup volume.

The first attempt took 26 hours and stopped on an error before it finished.
With the fixes below, a repeat run on idle disks should take about an hour and a
quarter, end to end; the table at the end gives the estimate per step.

## The shape of a whole-tree run

```sh
phindex --db source.sqlite --root /Volumes/Archive/Photos      # index both sides
phindex --db backup.sqlite --root /Volumes/Backup/Photos
dngc run /Volumes/Archive/Photos /Volumes/Backup/Photos ARCHIVE \
     --index source.sqlite backup.sqlite --metadata phindex --workers 8
dngc verify ARCHIVE --restore-sample 5
```

`dngc run` without `--pairs` walks the whole source, pairs each file with the
same relative path on the backup, and then:

1. **DNG pass**: compares each DNG pair's raw data and preserves the source as
   a delta (or a SHA-256 reference) against the backup;
2. **other files**: preserves every other file the same way;
3. **metadata**: builds the searchable metadata section from the two phindex
   indexes (`--metadata phindex`), so ExifTool reads nothing;
4. **final identity check**: re-stats both files of every pair, so a file
   changed during the run is caught.

## Fixes, in the order the run met them

### Progress where there was none

| Commit | Change |
| --- | --- |
| `21fb58c` | The walk prints progress every 30 s and a total, in OCaml and Python. 1.7 M files took 44 s. |
| phindex `af61c67` | `phindex align` reports its progress on stderr while it works. |
| `7c31122` | The final identity check reports progress every 30 s. |

Long silent phases were the main complaint before any of them were slow: a
phase with no output cannot be told apart from a hung one.

### An ETA that kept growing

`7db7647`: the ETA came from the rate of the pairs actually compared, while most
pairs were settled from the cache or the indexes in a fraction of the time; as
the cheap pairs ran out, the estimate grew. It now comes from the recent pace of
**all** pairs, over a window of the last five progress intervals, so it tracks
what the run is doing now.

### A remount threw the cache away

`3790f0a`: cached comparisons are reused when both files are unchanged, and
"unchanged" compared stamps that included `st_dev`. macOS numbers a volume each
time it is mounted, so after a remount every cached result was discarded and
the run started over. The test now leaves the device out, in the OCaml, Rust and
Python implementations. Results of `--skip-metadata` runs are cached as well,
and reused only by runs that also skip metadata.

### A `realpath` per file

`86151df`: the native `all` resolved every preserved path with `realpath`, which
looks up each path component; on a USB-bridged array that ran at 120 files a
second, and 713,000 pairs would have taken hours. Paths now resolve through a
cache of directories.

### Identical files, one at a time

`3a7a54c`: files whose SHA-256 is the same on both sides (the phindex indexes
already hold it) need no reading: their record is a reference to the backup's
copy. They went through the per-pair path one by one. They are now recorded in
bulk, in-process, 5,000 to a transaction: 226,005 identical DNGs in 127 s, and
973,334 of 979,356 other files in 12 minutes.

`22f514d`: records saved by an earlier, interrupted run were also rechecked one
pair at a time. They are now rechecked in bulk too: about 720,000 in about 10
minutes. Together these two saved about 30 minutes on a restart.

### A summary written after every batch

`24caa49`: the running `summary.json` aggregates every metadata difference with
examples. On this run it grew to 5–7 MB, and it was rewritten after every batch
of 128 pairs, so the run grew slower the further it went: the rate fell from 390
to 230 pairs a second, with the CPU busy serializing JSON. It is now written at
most every 30 s, and once at the end; the rate then held at about 450 pairs a
second.

### `--trust-files`

`4bf9c11`: the final identity check re-stats both files of every pair. On 1.7 M
pairs on spinning disks that is a long phase, and it buys nothing when nothing
else touches the volumes during the run. `--trust-files` skips it and says so in
the output ("final identity check skipped (--trust-files)"). Use it only when no
other process writes to either volume for the length of the run.

### The metadata step, part 1: 2.7 times faster

`6b339f7`: `phindex-metadata` ran at 115 pairs a second. Two causes:

- comparing two records looked members up with `List.assoc`, quadratic in the
  number of tags, and records hold hundreds; it now uses hash tables;
- each record was parsed three times; it is now parsed once, and `write_batch`
  checks a batch by comparing the gunzipped text with the text it wrote, not by
  parsing every row again.

On 3,001 real pairs: 13.2 s before, 4.9 s after, with byte-identical batches and
the same 18,856 difference rows.

### Files the engine does not read

`e6f3101`, phidentify `cbcb577`, phindex `5c45697`, `6defedf`: the metadata
step reported 59,700 errors. 59,506 of them were Capture One `.cop` cache
proxies, files no metadata engine should be given: phidentify ran the engine on
every file, and stored its refusal as a failure. Another 9,861 newer `.cop`
proxies hold a JPEG XL stream, which the engine recognized by its first bytes,
so they were given full metadata records and diffed.

The rule now: the metadata identity is attempted only for a file whose
**extension** the engine reads. What the file is for decides, not what its bytes
resemble.

- `dng-exiftool-ffi` has `reads_extension(path)` (exiftool-rs's own table, no
  I/O) and `extensions()`. The table is committed as
  `metadata-extensions-exiftool-rs.txt`, which a Rust test keeps equal to the
  crate.
- phidentify skips the others: their outcome is "not attempted", not a failure.
  The same check is in its C ABI (`phidentify_reads_metadata`) and its OCaml
  binding (`Phidentify.reads_metadata`).
- phindex and phindex-ox clear the stored metadata of such rows at the start of
  an update, without reading any file again and without a new revision stamp.
- `phindex-metadata` lists such a side under `no_metadata` and counts it as
  `files_without_metadata`, not as an error.
- `portable_metadata.snapshot`, the whole-tree ExifTool path of the unified pass
  and `preserve_years.py`, gives Perl ExifTool only the extensions in its own
  `-listf` list, and exiftool-rs only those in its table.

`dngc run --metadata exiftool` was never affected: it gives ExifTool only `.dng`
pairs.

### The metadata step, part 2: identical pairs keep the hash

`e2137e8`: the step read, parsed and stored both full records of every pair,
including the pairs whose bytes are identical, whose metadata cannot differ. On
this volume that is about three pairs in four. A pair whose two sides have the
same SHA-256 in current index rows now keeps its stamps and `same_bytes_sha256`
instead of the two records; it counts as compared with no differences
(`files_byte_identical`), and it is left out of the per-tag coverage counts,
which describe the records kept.

On 5,000 real pairs (3,673 identical), with warm caches: 19.3 s before, 3.9 s
after (259 to 1,280 pairs a second), and 44% less written. The full step took
1 h 50 min before this change; the estimate for 1.7 M pairs is now 25–35 min.

### Correctness fixes met on the way

- `e8fc8bc`: `phindex align` leaves out names beginning with a dot. A whole-tree
  run stopped when it met such files (interrupted-transfer temporaries, for
  example). dngc now keeps them as full copies.
- The run's wrapper script must build the native binaries before the run.
  Without them, `dngc` falls back to the Python implementation, many times
  slower, without failing. Check that the native `dngc` prints the expected
  usage before starting a long run.

## What was measured and did not help

**Stamps read in path order before the DNG pass.** The DNG pass checks each
cached pair by statting both files; pairs come in basename order, so the stats
look scattered. In directory order a cold `stat` ran at 34,000 files a second on
one array and 6,000 on the other, so reading every stamp up front in path order
looked promising. Measured on 20,000 cold cached pairs it was slower (17.5 s
against 12–13 s), and on warm caches the same. The change was not kept.

With idle disks the DNG pass runs at about 1,560 cached pairs a second cold and
4,000 warm, so 713,000 DNG pairs take about 8 minutes. The 450 a second seen in
the real run came mostly from other load on the same disks.

**Profiling with a random sample.** A random sample of pairs makes every stat a
seek and runs at a few dozen pairs a second. It is not representative; sample
contiguous ranges of the pair list.

## Operating notes for the next run

- **Keep other I/O off both volumes.** A second job on the same disks slows both
  several times. The final `dngc verify` of the copy reading one disk made a cold
  `stat` on that disk run eight times slower (775 against 6,000 files a second).
- **Read the metadata index into memory before verifying on spinning disks.**
  `dngc verify` runs `PRAGMA quick_check` on `metadata/differences.sqlite3`.
  That is a page-by-page walk of the B-trees, which on a 3 GB index on spinning
  disks is random reads for most of an hour. One sequential read first puts the
  file in the page cache, and the check then takes minutes:

  ```sh
  cat ARCHIVE/metadata/differences.sqlite3 > /dev/null
  dngc verify ARCHIVE --restore-sample 5
  ```

- **Use `--trust-files`** when nothing else writes to either volume during the
  run.
- **Restart rather than wait** after a fix: saved records and identical files are
  settled in bulk, so a restart repeats only minutes of work.
- **Leave dot-files and cache folders to the rules above.** They are kept as
  full copies and are not given to the metadata engine.

## Expected time of a repeat run

For the same two volumes, with both indexes current and the disks otherwise
idle:

| Step | Before | Now (measured or estimated) |
| --- | --- | --- |
| index both volumes | minutes | 3–5 min (unchanged folders skipped) |
| walk | silent | 44 s, with progress |
| saved records and identical DNGs, in bulk | per pair | about 10 min |
| DNG pass, 713 k pairs | 27 min at 450/s under load | about 8 min cold on idle disks |
| other files | per pair | about 20 min (12 of them recording identical files in bulk) |
| metadata, 1.7 M pairs | 1 h 50 min | 25–35 min |
| final identity check | long, silent | progress every 30 s, or skipped with `--trust-files` |
| verify on spinning disks | most of an hour | minutes after the read-ahead |

## What could still be faster

- **Cached DNG pairs under `--trust-files`.** The remaining cost of the DNG pass
  is a cold `stat` of both files per cached pair. A run that trusts the files
  could take those stamps from the phindex rows the index step has just
  refreshed. That would save the ~8 minutes, at the cost of missing a file
  changed after indexing.
- **Parallel metadata.** `phindex-metadata` compares pairs on one core; the pairs
  that are not byte-identical could be spread across domains.
- **Finding the remaining DNG-pass gap.** The measurements above are in
  compare-only mode. The real run also kept a preservation session open, and
  measuring it against a scratch copy of the byte section would show whether
  that session adds cost of its own.

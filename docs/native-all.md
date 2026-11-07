# One-pass native comparison and preservation (`all`)

`compare_dng.py --pairs-only` and `preserve_apo.py scan --delta --pairs` each read
every file of a pair list in full. The `all` command does both jobs in one pass:
each pair is read once from disk, then compared, its metadata extracted, and the
left file preserved against the right one. Its outputs are exactly the outputs of
the two Python commands, so either side can resume, query or restore what the
other wrote. It exists twice, with the same options and the same outputs:

| Implementation | Binary |
| --- | --- |
| Rust | `rust-scanner/target/release/dng-compare-all` |
| OCaml (stock OCaml 5 and OxCaml) | `dng-archive all` (`ocaml-archive/`) |

`test_native_all.py` runs both Python commands and each binary on the same
synthetic pairs and requires equal outputs.

## Command

    dng-compare-all LEFT RIGHT --pairs FILE --report DIR [--archive DIR]
        [--left-index FILE --right-index FILE] [--workers N] [--batch-size N] [--exiftool-processes N]
        [--progress-secs S] [--rate-mib R] [--fresh]
    dng-archive all LEFT RIGHT --pairs FILE --report DIR [--archive DIR] [same options]

- `LEFT`, `RIGHT`: roots, as for `compare_dng.py`. LEFT is the source preserved;
  RIGHT is the reference it is preserved against.
- `--pairs FILE`: `LEFT<TAB>RIGHT` lines, absolute or relative to the roots, the
  format of `compare_dng.py --pairs` and of `phindex align`'s `dng-pairs.tsv`.
  Only listed pairs are processed (the `--pairs-only` behaviour).
- `--report DIR`: what `compare_dng.py --pairs-only --pairs FILE --output DIR`
  writes: `pairs.jsonl`, `summary.json`, `unmatched.json`, `not-confirmed.json`,
  `cache.sqlite3` (same schema, keys and stamps, so either tool reuses the
  other's results), and the `.lock` file.
- `--archive DIR`: what `preserve_apo.py scan LEFT RIGHT DIR --delta --pairs P`
  writes for the same pairs (P as JSON lines `{source, reference}`):
  `recovery.sqlite3` (tables `files`, `errors`, WAL), `deltas/…vcdiff`,
  `originals/…` for files without a reference, `inventory.json`,
  `directories.json`, `summary.json`, `manifest.jsonl` and the
  `.byte-preservation.lock` file. A record already in `files` is not redone.
- Defaults: `--workers 2`, `--batch-size 32`, `--exiftool-processes 1`,
  `--progress-secs 0` (a progress line per batch), `--rate-mib 200`.
  Validation and error messages follow the Python commands.
- Exit status: as `compare_dng.py` (0 when every left file has a raw-equal right
  copy, 1 otherwise, 2 for usage errors, 130 when interrupted).

## Per pair

1. Stat both files (`[st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns]`, the
   `stamp` of `compare_dng.py`) and read both whole into memory. Files over
   1 GiB may be streamed instead; results must not differ.
2. From those bytes: whole-file SHA-256 of each, and equality.
3. Pixel comparison, as `compare_pixels`:
   - `Missing DNGVersion tag` unless tag 50706 is in the first IFD of both.
   - Pages are every IFD of the main chain with its SubIFDs, depth first, in
     tifffile's order. A page is *raw* when NewSubfileType is 0 and
     PhotometricInterpretation is 0, 1, 2, 32803 or 34892, else *auxiliary*.
   - Per group, a differing page count gives
     `{"status":"unverified","error":"Image count differs","left_count":…,"right_count":…}`.
   - Per page pair: `left_shape`/`right_shape` as tifffile's `TiffPage.shape`
     (`[length, width]`, or `[length, width, samples]` for contiguous samples,
     `[samples, length, width]` for planar), the payload digest (SHA-256 over
     each strip/tile: 8-byte little-endian length, then the bytes; the error
     text `Invalid image block: offset=O, length=L` when a block lies outside the
     file; `Image has no complete strip/tile directory` when offsets and counts
     are missing or differ in number), `bytes_read` (sum of both pages' block
     lengths), and status `equal`/method `encoded_blocks` when the digests and
     the encoding signature agree. The signature is the byte order and the
     values of the tags `ENCODING` lists in `compare_dng.py`.
   - Otherwise the samples are decoded by the Python decoder in a child
     process, exactly as `decoded_status_isolated` does it (same command, same
     timeout, same `decoder crashed (signal N)` / `decoder timed out` /
     `decoder failed: …` texts), so that a crashing decoder never ends the run.
     The Python interpreter is `DNG_COMPARE_PYTHON` or the checkout's
     `.venv/bin/python`, run in the directory holding `compare_dng.py`.
   - Group status as `group_status`; an empty auxiliary group is `equal`.
   - Exceptions set `raw` to `{"status":"unverified","error":…}` and `error`.
4. Metadata: Perl ExifTool with `compare_dng.py`'s exact arguments, a batch per
   `--batch-size` pairs split over `--exiftool-processes` processes; then
   `normalize_tags`, `flatten`, `compare_metadata` and `compact` with identical
   results. Canonical JSON is Python's `json.dumps(sort_keys=True,
   ensure_ascii=True, separators=(',', ':'))`, including Python's float repr
   (`1e-05`, `1e+16`, `0.1`) and integer/float distinction; `compact` hashes that
   text.
5. Stamps re-checked; a change gives `File changed or was unreadable during
   comparison` exactly as `compare_dng.py` does. Cache rows are written under
   the same conditions.
6. Preservation of the left file against the right one from the same bytes:
   the record of `preserve_file(…, delta_mode=True)` field for field (identity,
   `attrs` with xattrs, SHA-256s, `whole_file_equal`, `original`, `delta`,
   `delta_sha256`, `delta_format`, `preserved_bytes`, `reconstruction_verified`),
   the delta made by `xdelta3` with the same arguments and verified as
   `stream_delta` verifies it. Equal files store only the reference hash.

`pairs.jsonl` lines are canonical JSON of the result, in pair order;
`summary.json` is `Summary.write` with the same context (`version`, `roots`,
`matched_pairs`, `explicit_pairs`, `pairs_only: true`, `left_files`,
`right_files`, `duplicate_names`, `left_only_names`, `direction`,
`selected_pairs`, `cached_pairs_reused`) and `json.dumps(indent=2)` layout.

## Using phindex indexes (optional)

    --left-index LEFT.sqlite --right-index RIGHT.sqlite

phindex (github.com/firstpair/phindex) indexes of the two trees let the command
skip reads it does not need. A file's index row is *current* when its path is in
`files` with `gone=0`, a non-null `sha256`, and the `size` and `mtime_ns` of the
file's stat now. The indexes are opened read-only (`immutable=1`) and never written.

- Both rows current and `sha256` equal: the pair is byte-identical and is not
  read. The report records `raw` and `auxiliary` as
  `{"status":"equal","method":"phindex_sha256"}` (no `images`), metadata as
  usual (ExifTool reads only the metadata, and filesystem tags can still differ),
  and the archive a `whole_file_equal` record with both hashes from the index.
- Both rows current, `dng_blocks` non-null and equal, and no `--archive`:
  `raw` is `{"status":"equal","method":"phindex_dng_blocks"}` and `auxiliary` is
  `{"status":"not_compared"}`, without reading the files. phindex's `dng_blocks`
  is a digest of the same encoded raw blocks (phidentify's definition), so
  equality is the same `encoded_blocks` evidence; previews are not covered, so
  `all_images` coverage stays false for such pairs.
- Otherwise the pair is read and compared as above. With `--archive` every pair
  whose bytes differ is read in full, since its delta needs both files.

The summary counts pairs settled from the indexes under
`counts.phindex_sha256` and `counts.phindex_dng_blocks`. Without the options,
outputs are unchanged. `--archive` is optional too: without it the command only
compares, like `compare_dng.py --pairs-only`.

Reading each file once is the point: the only further disk reads are ExifTool's
metadata reads and the delta encoder's, which normally come from the file cache.

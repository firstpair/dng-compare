#!/usr/bin/env python3
"""Metadata batches for a preservation archive from two phindex indexes.

phindex (github.com/firstpair/phindex) stores, for every file it indexes, the
metadata record of dng-compare's exiftool-rs engine (via phidentify). For listed
pairs whose index rows are current, this writes the archive's metadata section
(metadata/batches/*.jsonl.gz rows in portable_metadata's format, then
metadata/differences.sqlite3 via metadata_index) without reading the files:

    phindex_metadata.py LEFT RIGHT ARCHIVE --pairs PAIRS.tsv \\
        --left-index LEFT.sqlite --right-index RIGHT.sqlite

PAIRS.tsv has LEFT<TAB>RIGHT lines (absolute, or relative to the roots), as for
compare_dng.py --pairs and phindex align's pair files. LEFT is the archive's
source side (`apo`), RIGHT its reference (`bumer`).

- The records are exiftool-rs records, stamped ExifTool:DngCompareEngine. An
  archive whose metadata comes from another engine (Perl ExifTool) is refused:
  records of different engines are separate identities and never mixed.
- phindex keeps string values over 1 KiB as `long-sha256:<hex>:<length>`, so such
  values (embedded thumbnails, camera-profile tables) show that they differ, not
  their content. The full bytes are in the archive's preserved files.
- A row is current when its size and modification time match the file's stat
  now. A pair whose rows are not current, or that has no record, is written with
  an error naming the reason, and is not compared.
- Resumable: existing batches are kept when they list the same paths.
"""
import argparse, collections, gzip, json, os, pathlib, re, sqlite3, sys, time, zlib

from metadata_index import open_index, ingest, read_rows
from portable_metadata import compare, stamp, properties, atomic_json
from preserve_apo import validate_output, archive_lock
from archive_layout import archive_top

ENGINE_TAG = "ExifTool:DngCompareEngine"
PREFIX = "phindex-"


def read_pairs(path, roots):
    pairs = []
    for number, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not all(fields):
            raise ValueError(f"{path}:{number}: expected LEFT<TAB>RIGHT")
        pairs.append(tuple(os.path.join(root, f) for root, f in zip(roots, fields)))
    return pairs


# Index.record's reason for a file whose type phindex does not read metadata from:
# not an error; the row lists the side under "no_metadata".
NOT_READ = "metadata not read for this file type"


def count_summary(r):
    """What counting a recorded row reads, and nothing else: the error,
    no_metadata and byte-identical flags, the kind and tag of each embedded
    difference, and each side's engine stamp, with strings interned. A resumed
    run keeps one of these per recorded row instead of the row."""
    stamp = lambda side: {ENGINE_TAG: r[side][ENGINE_TAG]} if ENGINE_TAG in (r.get(side) or {}) else {}
    return dict(differences=[dict(kind=sys.intern(d["kind"]), tag=sys.intern(d["tag"]), category="embedded")
                             for d in r.get("differences") or () if d.get("category") == "embedded"],
                errors=[None] if r.get("errors") else [], no_metadata=bool(r.get("no_metadata")),
                same_bytes_sha256=bool(r.get("same_bytes_sha256")),
                apo_metadata=stamp("apo_metadata"), bumer_metadata=stamp("bumer_metadata"))


class Index:
    def __init__(self, path):
        self.db = sqlite3.connect(f"file:{pathlib.Path(path).resolve()}?immutable=1", uri=True)

    def record(self, path, st):
        """The record of `path` if its row is current, else (None, reason)."""
        row = self.db.execute("SELECT size, mtime_ns FROM files WHERE path=? AND gone=0", (path,)).fetchone()
        if not row:
            return None, "not in the phindex index"
        if st is not None and (row[0] != st.st_size or row[1] != st.st_mtime_ns):
            return None, "phindex row is out of date"
        meta = self.db.execute("SELECT record_deflate FROM metadata WHERE path=?", (path,)).fetchone()
        if not meta:
            if self.not_read(path):
                return None, NOT_READ
            return None, "phindex has no metadata record"
        return json.loads(zlib.decompress(meta[0], -15)), None

    def sha256(self, path, st):
        """The SHA-256 phindex holds for `path` when its row is current, else None."""
        try:
            row = self.db.execute("SELECT size, mtime_ns, sha256 FROM files WHERE path=? AND gone=0", (path,)).fetchone()
        except sqlite3.OperationalError:  # an index without the identity columns
            return None
        return row[2] if row and row[0] == st.st_size and row[1] == st.st_mtime_ns else None

    def not_read(self, path):
        """Whether phindex identified `path` and did not attempt its metadata, as
        it does for a file type the engine does not read (a `.cop` cache proxy)."""
        try:
            row = self.db.execute("SELECT sha256 IS NOT NULL AND md_digest IS NULL AND md_err IS NULL "
                                  "FROM files WHERE path=? AND gone=0", (path,)).fetchone()
        except sqlite3.OperationalError:  # an index from before the identity columns
            return False
        return bool(row and row[0])


def existing_engine(archive):
    """The engine of an archive's existing metadata batches: a stamp, 'Perl ExifTool', or None."""
    for path in sorted((archive / "metadata/batches").glob("*.jsonl.gz")):
        for row in read_rows(path):
            for side in ("apo_metadata", "bumer_metadata"):
                if row.get(side):
                    return row[side].get(ENGINE_TAG, "Perl ExifTool")
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("left", type=pathlib.Path)
    ap.add_argument("right", type=pathlib.Path)
    ap.add_argument("archive", type=pathlib.Path)
    ap.add_argument("--pairs", type=pathlib.Path, required=True)
    ap.add_argument("--left-index", type=pathlib.Path, required=True)
    ap.add_argument("--right-index", type=pathlib.Path, required=True)
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--progress-secs", type=float, default=60)
    # The native tool builds rows in this many forked workers; this reference runs serially.
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if not 1 <= a.batch_size <= 100000:
        ap.error("batch-size must be 1..100000")
    if not 1 <= a.workers <= 256:
        ap.error("workers must be 1..256")
    roots = [str(p.resolve()) for p in (a.left, a.right)]
    if not all(os.path.isdir(r) for r in roots):
        ap.error("Both roots must be readable directories")
    for p in (a.left_index, a.right_index):
        if not p.is_file():
            ap.error(f"phindex index not found: {p}")
    try:
        pairs = read_pairs(a.pairs, roots)
        # Given ARCHIVE/bytes, the metadata section is at the archive's top (archive_layout.py).
        archive = validate_output(pathlib.Path(roots[0]), pathlib.Path(roots[1]), archive_top(a.archive))
    except (OSError, ValueError) as e:
        ap.error(str(e))
    for left, _ in pairs:
        if os.path.commonpath([left, roots[0]]) != roots[0]:
            ap.error(f"{left}: not under {roots[0]}")
    indexes = Index(a.left_index), Index(a.right_index)
    with archive_lock(archive, ".metadata-preservation.lock"):
        batches = archive / "metadata/batches"
        batches.mkdir(parents=True, exist_ok=True)
        prior = existing_engine(archive)
        engine = None
        db = open_index(archive)
        counts, tag_counts = collections.Counter(), collections.Counter()
        start = last = time.monotonic()
        print(f"{len(pairs):,} pairs; archive {archive}", flush=True)
        # What the archive's batches already hold. A pair whose source is recorded
        # with its current size and mtime_ns is not read again (a rerun, or a
        # resumed run); a new content of a recorded relative path (a refilled
        # shuttle) gets this run's UTC stamp as its version, as metadata_index
        # tells contents of one path apart. New batches continue the numbering.
        recorded, known, numbers = {}, set(), []
        # Reading back a large archive's batches takes minutes: a line every
        # 30 s while it lasts, and one when it is done.
        files = sorted(batches.glob("*.jsonl.gz"))
        t_read = last_read = time.monotonic(); read_files = read_rows_n = 0
        for path in files:
            m = re.fullmatch(re.escape(PREFIX) + r"(\d{6})\.jsonl\.gz", path.name)
            if m:
                numbers.append(int(m.group(1)))
            rows = read_rows(path)
            read_rows_n += len(rows); read_files += 1
            for r in rows:
                known.add(r.get("relative_path"))
                if r.get("apo_stamp"):
                    recorded[(r.get("relative_path"), r["apo_stamp"][1], r["apo_stamp"][2])] = count_summary(r)
            now = time.monotonic()
            if now - last_read >= 30:
                last_read = now
                print(f"reading recorded batches: {read_files:,} of {len(files):,}, {read_rows_n:,} rows | {now - t_read:.0f}s", flush=True)
        if files:
            print(f"{read_rows_n:,} recorded rows read from {len(files):,} batches | {time.monotonic() - t_read:.0f}s", flush=True)
        number = max(numbers) + 1 if numbers else 0
        version = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        reused, todo, missing = [], [], []
        for left, right in pairs:
            rel = os.path.relpath(left, roots[0])
            try:
                st = os.stat(left)
            except FileNotFoundError:
                missing.append(rel)  # a wiped source: reported, its earlier rows kept
                continue
            row = recorded.get((rel, st.st_size, st.st_mtime_ns))
            if row is not None:
                reused.append(row)
            else:
                todo.append((rel, left, right))
        for rel in missing:
            print(f"MISSING {rel}", flush=True)

        def pair_row(rel, left, right):
            nonlocal engine
            row = dict(relative_path=rel, **({"version": version} if rel in known else {}), apo=left, bumer=right)
            errors, warnings, records, no_metadata = [], [], {}, []
            # Byte-identical sides (the same SHA-256 in both current phindex rows)
            # have the same metadata by construction: the row keeps their stamps
            # and the hash, and neither record is read, parsed or stored.
            try:
                st_a, st_b = os.stat(left), os.stat(right)
            except OSError:
                st_a = st_b = None
            if st_a is not None:
                sha = indexes[0].sha256(left, st_a)
                try:
                    props = properties(pathlib.Path(left)) if sha is not None and sha == indexes[1].sha256(right, st_b) else None
                except OSError:  # the full path below reports it
                    props = None
                if props is not None:
                    row.update(apo_stamp=[st_a.st_ino, st_a.st_size, st_a.st_mtime_ns, st_a.st_ctime_ns],
                               apo_filesystem=props,
                               bumer_stamp=[st_b.st_ino, st_b.st_size, st_b.st_mtime_ns, st_b.st_ctime_ns],
                               apo_metadata={}, bumer_metadata={}, differences=[], errors=[], warnings=[],
                               same_bytes_sha256=sha)
                    return row
            for side, path, index in (("apo", left, indexes[0]), ("bumer", right, indexes[1])):
                try:
                    st = os.stat(path)
                    row[side + "_stamp"] = [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
                    if side == "apo":
                        row["apo_filesystem"] = properties(pathlib.Path(path))
                except OSError as e:
                    row[side + "_stamp"] = None
                    errors.append(dict(side=side, error=str(e)))
                    continue
                record, reason = index.record(path, st)
                if reason == NOT_READ:
                    no_metadata.append(side)
                    continue
                if record is None:
                    errors.append(dict(side=side, error=reason))
                    continue
                stamp_ = record.get(ENGINE_TAG)
                if stamp_ is None:
                    raise SystemExit(f"{path}: phindex record has no {ENGINE_TAG}")
                engine = engine or stamp_
                if stamp_ != engine:
                    raise SystemExit(f"{path}: engine {stamp_!r} differs from {engine!r}; not mixing engines")
                if prior is not None and stamp_ != prior:
                    raise SystemExit(f"{archive}: its metadata comes from {prior!r}, these records from "
                                     f"{stamp_!r}; not mixing engines")
                records[side] = record
                for k, v in record.items():
                    if k.rsplit(":", 1)[-1] == "Error":
                        errors.append(dict(side=side, error=v))
                    if k.rsplit(":", 1)[-1] == "Warning":
                        warnings.append(dict(side=side, warning=v))
            a_meta, b_meta = records.get("apo", {}), records.get("bumer", {})
            changes = compare(a_meta, b_meta) if not errors and not no_metadata else []
            for c in changes:
                c["category"] = "filesystem" if c["tag"].startswith("File:") else "embedded"
            row.update(apo_metadata=a_meta, bumer_metadata=b_meta, differences=changes,
                       errors=errors, warnings=warnings)
            if no_metadata:
                row["no_metadata"] = no_metadata
            return row

        def count(rows):
            nonlocal engine
            for row in rows:
                counts["files_processed"] += 1
                counts["metadata_errors"] += int(bool(row["errors"]))
                counts["files_without_metadata"] += int(bool(row.get("no_metadata")))
                counts["files_byte_identical"] += int(bool(row.get("same_bytes_sha256")))
                counts["files_with_embedded_differences"] += int(any(c["category"] == "embedded" for c in row["differences"]))
                for c in row["differences"]:
                    if c["category"] == "embedded":
                        tag_counts[(c["kind"], c["tag"])] += 1
                engine = engine or next((r[ENGINE_TAG] for r in (row["apo_metadata"], row["bumer_metadata"]) if ENGINE_TAG in r), None)

        count(reused)
        for first in range(0, len(todo), a.batch_size):
            dest = batches / f"{PREFIX}{number:06}.jsonl.gz"
            number += 1
            rows = [pair_row(*t) for t in todo[first:first + a.batch_size]]
            # A root that is no longer a directory (an array that dropped off
            # during the run) would turn every remaining pair into an error row:
            # stop before writing the batch, so a rerun picks up from here.
            for side, root in (("LEFT", roots[0]), ("RIGHT", roots[1])):
                if not os.path.isdir(root):
                    raise SystemExit(f"{side} root {root} is no longer available; stopped before writing batch {number - 1}")
            temp = dest.with_suffix(".tmp")
            with gzip.open(temp, "wt", encoding="utf8", compresslevel=1) as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")
            with temp.open("rb") as f:
                os.fsync(f.fileno())
            with gzip.open(temp, "rt", encoding="utf8") as f:
                if [json.loads(l) for l in f] != rows:
                    raise SystemExit("Metadata archive read-back failed")
            temp.replace(dest)
            ingest(db, dest, rows)
            count(rows)
            now = time.monotonic()
            if now - last >= a.progress_secs or first + a.batch_size >= len(todo):
                last = now
                done = min(first + a.batch_size, len(todo))
                print(f"{done:,}/{len(todo):,} new pairs | errors={counts['metadata_errors']:,} | {now - start:.0f}s", flush=True)
        # Batches an interrupted run wrote but did not index (ingest is idempotent).
        for path in sorted(batches.glob(f"{PREFIX}*.jsonl.gz")):
            ingest(db, path)
        atomic_json(archive / "metadata/summary.json", dict(
            schema="apo-bumer-metadata-v1", source=roots[0], reference=roots[1], total_files=len(pairs), complete=True,
            **counts, rows_added=len(todo), missing_sources=len(missing), elapsed_seconds=time.monotonic() - start, safe_to_delete_source=False,
            note="From phindex indexes (exiftool-rs records; values over 1 KiB kept as long-sha256 references). "
                 "Exact original files are preserved separately.",
            common_changes=[dict(kind=k[0], tag=k[1], count=v) for k, v in tag_counts.most_common(100)]))
        atomic_json(archive / "metadata/provenance.json", dict(
            tool="phindex_metadata.py", engine=engine, source=roots[0], reference=roots[1],
            left_index=str(a.left_index.resolve()), right_index=str(a.right_index.resolve()),
            pairs=str(a.pairs.resolve()), long_values="long-sha256:<hex>:<length> for strings over 1 KiB"))
        print(json.dumps(dict(indexed_files=db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                              differences=db.execute("SELECT COUNT(*) FROM differences").fetchone()[0],
                              errors=counts["metadata_errors"],
                              without_metadata=counts["files_without_metadata"], engine=engine)), flush=True)
        db.close()
    return 0 if not counts["metadata_errors"] and not missing else 1


if __name__ == "__main__":
    sys.exit(main())

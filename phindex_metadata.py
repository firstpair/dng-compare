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
import argparse, collections, gzip, json, os, pathlib, sqlite3, sys, time, zlib

from metadata_index import open_index, ingest, read_rows
from portable_metadata import compare, stamp, properties, atomic_json
from preserve_apo import validate_output, archive_lock

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
            return None, "phindex has no metadata record"
        return json.loads(zlib.decompress(meta[0], -15)), None


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
    a = ap.parse_args()
    if not 1 <= a.batch_size <= 100000:
        ap.error("batch-size must be 1..100000")
    roots = [str(p.resolve()) for p in (a.left, a.right)]
    if not all(os.path.isdir(r) for r in roots):
        ap.error("Both roots must be readable directories")
    for p in (a.left_index, a.right_index):
        if not p.is_file():
            ap.error(f"phindex index not found: {p}")
    try:
        pairs = read_pairs(a.pairs, roots)
        archive = validate_output(pathlib.Path(roots[0]), pathlib.Path(roots[1]), a.archive)
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
        for number, first in enumerate(range(0, len(pairs), a.batch_size)):
            chunk = pairs[first:first + a.batch_size]
            dest = batches / f"{PREFIX}{number:06}.jsonl.gz"
            rels = [os.path.relpath(l, roots[0]) for l, _ in chunk]
            if dest.exists():
                rows = read_rows(dest)
                if [r["relative_path"] for r in rows] != rels:
                    raise SystemExit(f"{dest}: lists other files; remove it or use another archive")
            else:
                rows = []
                for rel, (left, right) in zip(rels, chunk):
                    row = dict(relative_path=rel, apo=left, bumer=right)
                    errors, warnings, records = [], [], {}
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
                    changes = compare(a_meta, b_meta) if not errors else []
                    for c in changes:
                        c["category"] = "filesystem" if c["tag"].startswith("File:") else "embedded"
                    row.update(apo_metadata=a_meta, bumer_metadata=b_meta, differences=changes,
                               errors=errors, warnings=warnings)
                    rows.append(row)
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
            for row in rows:
                counts["files_processed"] += 1
                counts["metadata_errors"] += int(bool(row["errors"]))
                counts["files_with_embedded_differences"] += int(any(c["category"] == "embedded" for c in row["differences"]))
                for c in row["differences"]:
                    if c["category"] == "embedded":
                        tag_counts[(c["kind"], c["tag"])] += 1
                engine = engine or next((r[ENGINE_TAG] for r in (row["apo_metadata"], row["bumer_metadata"]) if ENGINE_TAG in r), None)
            now = time.monotonic()
            if now - last >= a.progress_secs or first + a.batch_size >= len(pairs):
                last = now
                done = min(first + a.batch_size, len(pairs))
                print(f"{done:,}/{len(pairs):,} pairs | errors={counts['metadata_errors']:,} | {now - start:.0f}s", flush=True)
        atomic_json(archive / "metadata/summary.json", dict(
            schema="apo-bumer-metadata-v1", source=roots[0], reference=roots[1], total_files=len(pairs), complete=True,
            **counts, elapsed_seconds=time.monotonic() - start, safe_to_delete_source=False,
            note="From phindex indexes (exiftool-rs records; values over 1 KiB kept as long-sha256 references). "
                 "Exact original files are preserved separately.",
            common_changes=[dict(kind=k[0], tag=k[1], count=v) for k, v in tag_counts.most_common(100)]))
        atomic_json(archive / "metadata/provenance.json", dict(
            tool="phindex_metadata.py", engine=engine, source=roots[0], reference=roots[1],
            left_index=str(a.left_index.resolve()), right_index=str(a.right_index.resolve()),
            pairs=str(a.pairs.resolve()), long_values="long-sha256:<hex>:<length> for strings over 1 KiB"))
        print(json.dumps(dict(indexed_files=db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                              differences=db.execute("SELECT COUNT(*) FROM differences").fetchone()[0],
                              errors=counts["metadata_errors"], engine=engine)), flush=True)
        db.close()
    return 0 if not counts["metadata_errors"] else 1


if __name__ == "__main__":
    sys.exit(main())

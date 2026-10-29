# Querying preserved metadata differences

Database: `metadata/differences.sqlite3` in the preservation archive. For year-scoped runs, use `VOLUME/YEAR/metadata/differences.sqlite3`, for example `ArchiveA/2017/metadata/differences.sqlite3`. A closed, fully checkpointed index is readable by standard SQLite; no Python package is required for SQL queries. Do not open an active Turso MVCC index with another engine. The index updates transactionally after each saved metadata batch and resumes by batch SHA-256. Results cover only indexed batches until the metadata scan finishes. `.DS_Store` is excluded.

Direction is always **selected source → selected reference**: `added_in_bumer`, `missing_in_bumer`, or `changed_in_bumer`. In the legacy schema, `apo` columns mean the selected source, identified by provenance.json; they do not name a particular disk. Grouped ExifTool field names are retained. Embedded metadata and filesystem fields are separate categories. ExifTool diagnostics, synthesized Composite fields and SourceFile paths are excluded. Valid paired files form the comparison denominator; absent references and extraction errors remain listed in `files` but do not count as successful comparisons.

## Most frequent field differences

```sql
SELECT * FROM field_changes ORDER BY affected_files DESC;
```

## Fields added to every successfully compared file

```sql
SELECT * FROM field_changes
WHERE kind='added_in_bumer' AND affected_files=compared_files;
```

## Counts by media format and field presence on both sides

```sql
SELECT * FROM change_counts ORDER BY extension,affected_files DESC;
SELECT * FROM field_coverage ORDER BY tag,extension;
SELECT extension,COUNT(*) compared_files FROM files WHERE compared=1 GROUP BY extension;
```

Presence counts distinguish a field added to all files from one added to all files of a particular camera/format or only those that originally lacked it. Format percentages can be calculated using the corresponding compared-files count above. Missing tags differ from present tags with null values.

## Find affected files

```sql
SELECT f.path,d.kind FROM differences d
JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id
WHERE t.tag='EXIF:IFD0:Artist' ORDER BY f.path;
```

Use an actual grouped tag from `field_changes` in place of the example name.

## Common exact before/after value patterns

```sql
SELECT t.tag,d.kind,a.sha256 apo_value_sha256,b.sha256 bumer_value_sha256,COUNT(*) files
FROM differences d JOIN fields t ON t.id=d.field_id
LEFT JOIN vals a ON a.id=d.apo_value LEFT JOIN vals b ON b.id=d.bumer_value
GROUP BY d.field_id,d.kind,d.apo_value,d.bumer_value ORDER BY files DESC;
```

Values are stored once by SHA-256 of canonical typed JSON, compressed with standard zlib. Decode `vals.json_zlib` with Python `json.loads(zlib.decompress(blob))`. An absent value is an SQL NULL foreign key; a present JSON null has its own value row. Arrays and structured values remain typed and lossless. This makes repeated additions/removals compact and queryable without duplicating tag names, paths or large repeated values in every difference. Full gzip JSONL snapshots remain the independent preservation record; the index can be rebuilt with `uv run --no-project --with-requirements requirements.txt python metadata_index.py ARCHIVE`.

The index classifies extracted fields; it does not claim semantic equivalence of changed values or flatten structured fields. Exact byte restoration is verified separately.

## Shared tag rules

`metadata_rules.json` names the tags that are never compared, the volatile
filesystem tags, and the tags whose change cannot affect rendering (anything
not listed may). `metadata_rules.py` reads it for Python. Semantic Unison's
`unison-dngscan -semdiff` reads the same file, and with `-preserve` writes
batch rows that `metadata_index.py` indexes unchanged. Those archives record
`unison-dngscan -semdiff` in `provenance.json` and list each run in
`runs.jsonl`.

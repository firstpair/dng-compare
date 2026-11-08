"""Where an archive keeps its sections: one rule for every dng-compare tool.

The byte section of ARCHIVE (docs/dngc.md, "Archive layout"):

- ARCHIVE itself, when it holds recovery.sqlite3 and no bytes/ (a legacy
  archive, written before the unified layout);
- else the path itself, when its last component is `bytes` (ARCHIVE/bytes was
  given directly; never a nested bytes/bytes);
- else ARCHIVE/bytes (created for a new or empty archive).

The metadata section, provenance.json, runs.jsonl, pair lists and ARCHIVE.json
stay at the archive's top; given ARCHIVE/bytes, the top is its parent. The same
rule is in ocaml-archive (Preserve.byte_section) and the Rust dng-compare-all
(archive::byte_section)."""
import pathlib


def byte_section(archive):
    a = pathlib.Path(archive)
    if (a / 'recovery.sqlite3').exists() and not (a / 'bytes').exists():
        return a
    if a.name == 'bytes':
        return a
    return a / 'bytes'


def archive_top(archive):
    a = pathlib.Path(archive)
    return a.parent if a.name == 'bytes' else a

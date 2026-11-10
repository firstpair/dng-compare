//! The conformance corpus (../conformance/corpus, docs/dngc-archive-spec.md)
//! read with this binary's own archive code: the byte-section rule, the
//! record text (json.dumps form), every current record rebuilt (deltas with
//! stream_delta, full copies and references with digest_file), and earlier
//! versions under their labels. A valid case must read cleanly; a case broken
//! in its payloads or references must fail to rebuild.
//!
//! Records hold absolute paths, so each case is materialized at
//! /private/tmp/dngc-conformance/<case>, where make_corpus.py built it.

use super::{Budget, Ctx, byte_section, digest_file, label_of, stream_delta};
use crate::pyjson::{self, J};
use rusqlite::{Connection, OpenFlags};
use std::fs;
use std::path::{Path, PathBuf};

const BUILD: &str = "/private/tmp/dngc-conformance";
const REBUILD_RULES: [&str; 4] = ["payload-hash", "payload-missing", "rebuild", "reference"];

fn corpus() -> PathBuf { Path::new(env!("CARGO_MANIFEST_DIR")).join("../conformance/corpus") }

fn copy_tree(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for e in fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        let (from, to) = (e.path(), dst.join(e.file_name()));
        if e.file_type().unwrap().is_dir() { copy_tree(&from, &to) } else { fs::copy(&from, &to).unwrap(); }
    }
}

fn materialize(name: &str) -> PathBuf {
    let dst = Path::new(BUILD).join(name);
    let _ = fs::remove_dir_all(&dst);
    copy_tree(&corpus().join(name), &dst);
    dst
}

fn ro(path: &Path) -> Connection {
    Connection::open_with_flags(format!("file:{}?immutable=1", path.display()),
                                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI).unwrap()
}

fn has_table(db: &Connection, name: &str) -> bool {
    db.query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?1", [name], |r| r.get::<_, i64>(0)).unwrap() > 0
}

/// What reading this case found wrong: one line per record that does not read.
fn read_case(case: &Path) -> Vec<String> {
    let top = case.join("archive");
    let top_s = top.to_string_lossy().into_owned();
    // [3] the byte-section rule, derived here independently of byte_section.
    let legacy = top.join("recovery.sqlite3").exists() && !top.join("bytes").exists();
    let want = if legacy { top.clone() } else { top.join("bytes") };
    assert_eq!(Path::new(&byte_section(&top_s)), want.as_path(), "{}", case.display());
    let bytes = want;
    let ctx = Ctx { dir: bytes.clone(), reference: String::new(), budget: Budget::new(1.0e9) };
    let db = ro(&bytes.join("recovery.sqlite3"));
    let mut failures = Vec::new();
    let mut st = db.prepare("SELECT path, record FROM files ORDER BY path").unwrap();
    let rows: Vec<(String, String)> = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).unwrap().map(Result::unwrap).collect();
    for (path, text) in rows {
        let r = pyjson::parse(&text).unwrap();
        assert_eq!(pyjson::default(&r), text, "{path}: a record is written as json.dumps writes it");
        assert_eq!(r.get("relative_path").and_then(J::as_str), Some(path.as_str()));
        let get = |k: &str| r.get(k).and_then(J::as_str).unwrap_or("").to_string();
        let source_sha = get("source_sha256");
        let rebuilt = if r.get("delta").is_some_and(J::truthy) {
            stream_delta(&r, &ctx)
        } else if r.get("original").is_some_and(J::truthy) {
            digest_file(&bytes.join(get("original")))
        } else {
            digest_file(Path::new(&get("reference")))
        };
        match rebuilt {
            Ok(h) if h == source_sha => {}
            Ok(_) => failures.push(format!("{path}: SHA-256 differs from source_sha256")),
            Err(e) => failures.push(format!("{path}: {e}")),
        }
    }
    if has_table(&db, "versions") {
        let mut st = db.prepare("SELECT path, version, record FROM versions").unwrap();
        for row in st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?))).unwrap() {
            let (path, version, text) = row.unwrap();
            assert_eq!(label_of(&text).unwrap(), version, "{path}: an earlier version is stored under its label");
        }
    }
    failures
}

#[test]
fn corpus_reads_as_the_spec_says() {
    let dir = corpus();
    if !dir.is_dir() || std::process::Command::new("xdelta3").arg("-V").output().is_err() {
        eprintln!("skipped: no corpus or no xdelta3");
        return;
    }
    let mut names: Vec<String> = fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| dir.join(n).join("expected.json").exists()).collect();
    names.sort();
    assert!(!names.is_empty());
    for name in names {
        let case = materialize(&name);
        let expected = pyjson::parse(&fs::read_to_string(case.join("expected.json")).unwrap()).unwrap();
        let rules: Vec<&str> = expected.get("rules").map(J::as_arr).unwrap_or(&[]).iter().filter_map(J::as_str).collect();
        let breaks_rebuild = rules.iter().any(|r| REBUILD_RULES.contains(r));
        let failures = read_case(&case);
        if breaks_rebuild {
            assert!(!failures.is_empty(), "{name}: broken ({rules:?}) yet every record rebuilt");
        } else {
            assert!(failures.is_empty(), "{name}: {failures:?}");
        }
    }
}

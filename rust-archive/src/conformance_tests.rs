//! The conformance corpus (../conformance/corpus, docs/dngc-archive-spec.md)
//! read through this library's public API: the byte-section rule, the record
//! text (json.dumps form), every current record restored to a file and
//! compared with its source, earlier versions under their labels, and dropped
//! paths' tombstones. A valid case must read cleanly; a case broken in its
//! payloads or references must fail to restore.
//!
//! Records hold absolute paths, so each case is materialized at
//! /private/tmp/dngc-conformance/<case>, where make_corpus.py built it. The
//! tests run one at a time (a mutex), since they share that folder.

use crate::pyjson::{self, J};
use crate::{Reader, sections, version_label};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const BUILD: &str = "/private/tmp/dngc-conformance";
const REBUILD_RULES: [&str; 4] = ["payload-hash", "payload-missing", "rebuild", "reference"];
static SERIAL: Mutex<()> = Mutex::new(());

fn corpus() -> PathBuf { Path::new(env!("CARGO_MANIFEST_DIR")).join("../conformance/corpus") }

fn ready() -> bool {
    corpus().is_dir() && std::process::Command::new("xdelta3").arg("-V").output().is_ok()
}

/// Copy a tree, keeping modification times (the cases' phindex indexes match files by them).
fn copy_tree(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for e in fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        let (from, to) = (e.path(), dst.join(e.file_name()));
        if e.file_type().unwrap().is_dir() { copy_tree(&from, &to); continue; }
        fs::copy(&from, &to).unwrap();
        let modified = fs::metadata(&from).unwrap().modified().unwrap();
        fs::File::options().write(true).open(&to).unwrap().set_modified(modified).unwrap();
    }
}

fn materialize(name: &str) -> PathBuf {
    let dst = Path::new(BUILD).join(name);
    let _ = fs::remove_dir_all(&dst);
    copy_tree(&corpus().join(name), &dst);
    dst
}

/// What reading this case found wrong: one entry per record that does not restore.
fn read_case(name: &str, case: &Path) -> Vec<String> {
    let top = case.join("archive");
    // [3] the byte-section rule, derived here independently of sections().
    let legacy = top.join("recovery.sqlite3").exists() && !top.join("bytes").exists();
    let (t, b, l) = sections(&top);
    assert_eq!((t, b, l), (top.clone(), if legacy { top.clone() } else { top.join("bytes") }, legacy), "{name}");
    let reader = Reader::open(&top).unwrap();
    let out = case.join("rust-restored");
    let mut failures = Vec::new();
    for (path, text, record) in reader.records().unwrap() {
        assert_eq!(pyjson::default(&record), text, "{name} {path}: a record is written as json.dumps writes it");
        assert_eq!(record.get("relative_path").and_then(J::as_str), Some(path.as_str()), "{name}");
        let target = out.join(&path);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        match reader.restore(&record, &target, None) {
            Ok(()) => {
                if fs::read(&target).unwrap() != fs::read(case.join("L").join(&path)).unwrap() {
                    failures.push(format!("{path}: restored bytes differ from the source"));
                }
            }
            Err(e) => failures.push(format!("{path}: {e}")),
        }
    }
    for (path, label, record) in reader.versions().unwrap() {
        assert_eq!(version_label(&record), label, "{name} {path}: an earlier version is stored under its label");
    }
    failures
}

#[test]
fn corpus_reads_as_the_spec_says() {
    if !ready() { eprintln!("skipped: no corpus or no xdelta3"); return; }
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let dir = corpus();
    let mut names: Vec<String> = fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| dir.join(n).join("expected.json").exists()).collect();
    names.sort();
    assert!(!names.is_empty());
    for name in names {
        let case = materialize(&name);
        let expected = pyjson::parse(&fs::read_to_string(case.join("expected.json")).unwrap()).unwrap();
        let rules: Vec<&str> = expected.get("rules").map(J::as_arr).unwrap_or(&[]).iter().filter_map(J::as_str).collect();
        let failures = read_case(&name, &case);
        if rules.iter().any(|r| REBUILD_RULES.contains(r)) {
            assert!(!failures.is_empty(), "{name}: broken ({rules:?}) yet every record restored");
        } else {
            assert!(failures.is_empty(), "{name}: {failures:?}");
        }
    }
}

#[test]
fn dropped_paths_have_tombstones() {
    if !ready() { return; }
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let case = materialize("dropped");
    let reader = Reader::open(case.join("archive")).unwrap();
    assert!(reader.dropped("b/big.bin").unwrap().is_some(), "the trashed path has a tombstone");
    assert!(reader.dropped("b/only.dat").unwrap().is_some(), "the forced drop has a tombstone");
    assert!(reader.record("b/big.bin").unwrap().is_none());
    assert!(reader.dropped("a/same.txt").unwrap().is_none());
    assert!(reader.record("a/same.txt").unwrap().is_some());
}

/// Section 13: after every reference file is edited, each record still
/// restores, its base content rebuilt from the skeleton section.
#[test]
fn edited_references_restore_through_skeletons() {
    use crate::skeleton::{self, Store};
    if !ready() { return; }
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let case = materialize("current");
    let top = case.join("archive");
    let records = Reader::open(&top).unwrap().records().unwrap();
    let store = Store::create(&top).unwrap();
    let mut references = std::collections::BTreeSet::new();
    for (_, _, record) in &records {
        let Some(reference) = record.get("reference").and_then(J::as_str) else { continue };
        let sha = match crate::kind(record) { crate::Kind::Full => continue, crate::Kind::Delta => "reference_sha256", crate::Kind::Identical => "source_sha256" };
        let sha = record.get(sha).and_then(J::as_str).unwrap();
        let sk = skeleton::build(Path::new(reference)).unwrap();
        let (text, packed) = Store::encode(sha, reference, crate::os::stat(reference).unwrap().identity(), &sk).unwrap();
        store.put(sha, &text, &packed).unwrap();
        references.insert(reference.to_string());
    }
    drop(store);
    assert!(!references.is_empty());
    for reference in &references {
        let mut bytes = fs::read(reference).unwrap();
        bytes.extend(b" edited after the archive was written");
        fs::write(reference, bytes).unwrap();
    }
    let reader = Reader::open(&top).unwrap();
    let out = case.join("rust-restored-after-edits");
    for (path, _, record) in &records {
        let target = out.join(path);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        reader.restore(record, &target, None).unwrap_or_else(|e| panic!("{path}: {e}"));
        assert_eq!(fs::read(&target).unwrap(), fs::read(case.join("L").join(path)).unwrap(), "{path}");
    }
}

//! Finding a reference's content through phindex indexes (section 5.5) after
//! its file was moved, renamed or edited: small synthetic trees, an archive
//! with identical and delta records, and an index of the tree.
//!
//! The index is made here with phindex's own `files` table and indexes (its
//! SCHEMA and migrate), filled as phindex fills it: a vanished path's row is
//! marked gone and keeps its digests, a file edited in place gets a new
//! SHA-256 under the same image identity. With PHINDEX set to a phindex
//! binary, one test indexes the tree with it instead.

use crate::phindex::Index;
use crate::pyjson::{self, J, s};
use crate::skeleton::tests::{dir, jpeg, noise, tiff};
use crate::skeleton::{self, Store};
use crate::{Reader, hex, os};
use rusqlite::Connection;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

const FILES: &str = "
CREATE TABLE files (
  path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ino INTEGER NOT NULL,
  dev INTEGER NOT NULL, sha256 TEXT, hashed_unix INTEGER, seen_unix INTEGER NOT NULL, gone INTEGER NOT NULL DEFAULT 0,
  dng_blocks TEXT, dng_blocks_err TEXT, lr_digest TEXT, lr_err TEXT, md_digest TEXT, md_err TEXT, ids_rev TEXT,
  dngsem_profile TEXT, dngsem_err TEXT, preserved TEXT, preserved_err TEXT, image_data TEXT, image_err TEXT);
CREATE INDEX files_sha ON files(sha256);
CREATE INDEX files_size ON files(size);
CREATE INDEX files_dng_blocks ON files(dng_blocks);
CREATE INDEX files_lr ON files(lr_digest);
CREATE INDEX files_md ON files(md_digest);
CREATE INDEX files_preserved ON files(preserved);
CREATE INDEX files_image ON files(image_data);";

fn sha(bytes: &[u8]) -> String { hex(&Sha256::digest(bytes)) }

fn put(path: &Path, bytes: &[u8]) -> String {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, bytes).unwrap();
    path.to_string_lossy().into_owned()
}

/// A hand-made index. `row` lists a path with a SHA-256 and identities.
struct Made { db: Connection, path: PathBuf }

impl Made {
    fn new(dir: &Path, name: &str) -> Made {
        let path = dir.join(name);
        let db = Connection::open(&path).unwrap();
        db.execute_batch(FILES).unwrap();
        Made { db, path }
    }

    fn row(&self, path: &str, size: usize, sha256: &str, gone: bool, dng_blocks: Option<&str>, image_data: Option<&str>) {
        self.db.execute("INSERT OR REPLACE INTO files(path, size, mtime_ns, ino, dev, sha256, seen_unix, gone, dng_blocks, image_data)
                         VALUES(?1, ?2, 0, 0, 0, ?3, 0, ?4, ?5, ?6)",
                        rusqlite::params![path, size as i64, sha256, gone, dng_blocks, image_data]).unwrap();
    }

    /// Lists the file at `path` as it is now.
    fn live(&self, path: &str, dng_blocks: Option<&str>, image_data: Option<&str>) {
        let bytes = fs::read(path).unwrap();
        self.row(path, bytes.len(), &sha(&bytes), false, dng_blocks, image_data);
    }
}

/// An archive of identical records (and deltas, given a payload), with a
/// skeleton section when `skeletons` names contents.
struct Archive { top: PathBuf, db: Connection }

impl Archive {
    fn new(dir: &Path) -> Archive {
        let top = dir.join("archive");
        fs::create_dir_all(top.join("bytes")).unwrap();
        let db = Connection::open(top.join("bytes/recovery.sqlite3")).unwrap();
        db.execute_batch("CREATE TABLE files (path TEXT PRIMARY KEY, record TEXT NOT NULL);").unwrap();
        Archive { top, db }
    }

    fn add(&self, rel: &str, record: J) {
        self.db.execute("INSERT INTO files(path, record) VALUES(?1, ?2)", [rel, &pyjson::default(&record.with("relative_path", s(rel)))]).unwrap();
    }

    /// The source was byte-identical to the reference file, as it is now.
    fn identical(&self, rel: &str, reference: &str) {
        let st = os::stat(reference).unwrap();
        self.add(rel, J::obj().with("reference", s(reference)).with("reference_identity", st.identity())
            .with("source_sha256", s(sha(&fs::read(reference).unwrap()))));
    }

    /// `source` as a delta against the reference file, as it is now.
    fn delta(&self, rel: &str, reference: &str, source: &[u8]) {
        let from = self.top.join("source.tmp");
        fs::write(&from, source).unwrap();
        let payload = format!("{rel}.vcdiff");
        let to = self.top.join("bytes").join(&payload);
        let ok = std::process::Command::new("xdelta3").args(["-e", "-f", "-s", reference]).arg(&from).arg(&to).status().unwrap().success();
        assert!(ok, "xdelta3 -e");
        fs::remove_file(&from).unwrap();
        let st = os::stat(reference).unwrap();
        self.add(rel, J::obj().with("reference", s(reference)).with("reference_identity", st.identity())
            .with("reference_sha256", s(sha(&fs::read(reference).unwrap()))).with("source_sha256", s(sha(source)))
            .with("delta", s(payload)).with("delta_sha256", s(sha(&fs::read(&to).unwrap()))));
    }

    /// A skeleton of the file at `path`, as it is now.
    fn skeleton(&self, path: &str) {
        let store = Store::create(&self.top).unwrap();
        let sk = skeleton::build(Path::new(path)).unwrap();
        let (record, packed) = Store::encode(&sha(&fs::read(path).unwrap()), path, os::stat(path).unwrap().identity(), &sk).unwrap();
        store.put(&sha(&fs::read(path).unwrap()), &record, &packed).unwrap();
    }

    fn rebuild(&self, rel: &str, indexes: &[&Path]) -> Result<Vec<u8>, String> {
        let reader = Reader::open(&self.top)?.with_indexes(indexes)?;
        let record = reader.record(rel)?.unwrap();
        let mut out = Vec::new();
        reader.rebuild(&record, None, &mut out).map(|_| out)
    }
}

fn xdelta3() -> bool { std::process::Command::new("xdelta3").arg("-V").output().is_ok() }

fn xmp(text: &str, pad: usize) -> Vec<u8> { format!("<x:xmpmeta>{text}{}</x:xmpmeta>", " ".repeat(pad)).into_bytes() }

/// No file beside an index: an immutable read leaves no -wal, -shm or journal.
fn alone(index: &Path) {
    let name = index.file_name().unwrap().to_string_lossy().into_owned();
    let beside: Vec<String> = fs::read_dir(index.parent().unwrap()).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n != &name && n.starts_with(&name)).collect();
    assert!(beside.is_empty(), "files left beside the index: {beside:?}");
}

#[test]
fn a_moved_reference_is_found_by_its_sha256() {
    let d = dir("index-moved");
    let old = tiff(&xmp("rating 0", 0));
    let was = put(&d.join("tree/a/one.tif"), &old);
    let archive = Archive::new(&d);
    archive.identical("one.tif", &was);
    let source = [&old[..1000], b"the source differs here", &old[1000..]].concat();
    if xdelta3() { archive.delta("two.tif", &was, &source); }
    // Moved and renamed, unchanged; the index saw both states.
    let now = put(&d.join("tree/b/renamed.tif"), &old);
    fs::remove_file(&was).unwrap();
    let made = Made::new(&d, "index.sqlite");
    made.row(&was, old.len(), &sha(&old), true, None, Some("image-1"));
    made.live(&now, None, Some("image-1"));
    drop(made.db);

    assert!(archive.rebuild("one.tif", &[]).is_err(), "without an index the moved reference is not found");
    assert_eq!(archive.rebuild("one.tif", &[&made.path]).unwrap(), old);
    if xdelta3() {
        assert!(archive.rebuild("two.tif", &[]).is_err());
        assert_eq!(archive.rebuild("two.tif", &[&made.path]).unwrap(), source);
    }
    // And through restore, to a file.
    let reader = Reader::open(&archive.top).unwrap().with_indexes([&made.path]).unwrap();
    reader.restore(&reader.record("one.tif").unwrap().unwrap(), &d.join("restored.tif"), None).unwrap();
    assert_eq!(fs::read(d.join("restored.tif")).unwrap(), old);
    alone(&made.path);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn a_moved_and_edited_reference_is_rebuilt_from_its_skeleton() {
    let d = dir("index-edited");
    let old = tiff(&xmp("rating 0", 0));
    let edited = tiff(&xmp("rating 5, exposure +0.35", 3001));
    let was = put(&d.join("tree/a/one.tif"), &old);
    let archive = Archive::new(&d);
    archive.identical("one.tif", &was);
    let source = [&old[..1000], b"the source differs here", &old[1000..]].concat();
    if xdelta3() { archive.delta("two.tif", &was, &source); }
    archive.skeleton(&was);
    // Moved, renamed and edited. Beside it, another picture, and a file with
    // the same image identity whose strip differs (a wrong candidate).
    fs::remove_file(&was).unwrap();
    let now = put(&d.join("tree/b/renamed.tif"), &edited);
    let mut other = edited.clone();
    let n = other.len();
    other[n / 2] ^= 1;
    let wrong = put(&d.join("tree/b/damaged.tif"), &other);
    let unrelated = put(&d.join("tree/b/unrelated.jpg"), &jpeg(&[(0xe0, b"JFIF\0".to_vec())], &noise(5000, 9)));
    let made = Made::new(&d, "index.sqlite");
    made.row(&was, old.len(), &sha(&old), true, None, Some("image-1"));
    made.live(&wrong, None, Some("image-1"));
    made.live(&now, None, Some("image-1"));
    made.live(&unrelated, None, Some("image-2"));
    drop(made.db);

    let err = archive.rebuild("one.tif", &[]).unwrap_err();
    assert!(err.starts_with("reference SHA-256 mismatch, and its skeleton does not rebuild it"), "{err}");
    assert_eq!(archive.rebuild("one.tif", &[&made.path]).unwrap(), old);
    if xdelta3() { assert_eq!(archive.rebuild("two.tif", &[&made.path]).unwrap(), source); }
    alone(&made.path);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn an_identity_comes_from_the_old_paths_row_when_no_row_has_the_sha256() {
    // Edited in place, indexed (the row's SHA-256 replaced), then moved: the
    // row at the recorded path is gone, lists the edited bytes, and still
    // carries the image identity.
    let d = dir("index-path");
    let old = tiff(&xmp("rating 0", 0));
    let edited = tiff(&xmp("rating 3", 2000));
    let was = put(&d.join("tree/a/one.dng"), &old);
    let archive = Archive::new(&d);
    archive.identical("one.dng", &was);
    archive.skeleton(&was);
    fs::remove_file(&was).unwrap();
    let now = put(&d.join("tree/c/one-edited.dng"), &edited);
    let made = Made::new(&d, "index.sqlite");
    made.row(&was, edited.len(), &sha(&edited), true, Some("raw-1"), None);
    made.live(&now, Some("raw-1"), None);
    drop(made.db);
    assert_eq!(archive.rebuild("one.dng", &[&made.path]).unwrap(), old);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn a_stale_row_is_skipped() {
    let d = dir("index-stale");
    let old = tiff(&xmp("rating 0", 0));
    let was = put(&d.join("tree/a/one.tif"), &old);
    let archive = Archive::new(&d);
    archive.identical("one.tif", &was);
    fs::remove_file(&was).unwrap();
    // Three rows list the content: a file since overwritten at the same size,
    // one since truncated, one since deleted. A fourth still holds it.
    let mut changed = old.clone();
    changed[old.len() / 2] ^= 1;
    let same_size = put(&d.join("tree/b/1-same-size.tif"), &changed);
    let truncated = put(&d.join("tree/b/2-truncated.tif"), &old[..old.len() - 1]);
    let deleted = d.join("tree/b/3-deleted.tif").to_string_lossy().into_owned();
    let good = put(&d.join("tree/b/4-good.tif"), &old);
    let stale = Made::new(&d, "stale.sqlite");
    for p in [&same_size, &truncated, &deleted] { stale.row(p, old.len(), &sha(&old), false, None, Some("image-1")); }
    drop(stale.db);
    let fresh = Made::new(&d, "fresh.sqlite");
    fresh.live(&good, None, Some("image-1"));
    drop(fresh.db);

    // Only stale rows: the reader fails as it does without an index.
    let without = archive.rebuild("one.tif", &[]).unwrap_err();
    assert_eq!(archive.rebuild("one.tif", &[&stale.path]).unwrap_err(), without);
    // A second index lists a file that holds it.
    assert_eq!(archive.rebuild("one.tif", &[&stale.path, &fresh.path]).unwrap(), old);
    // With a skeleton section, the stale files are tried as edited versions
    // (same identity) and none passes the SHA-256 check.
    fs::write(&was, &old).unwrap();
    archive.skeleton(&was);
    fs::remove_file(&was).unwrap();
    let err = archive.rebuild("one.tif", &[&stale.path]).unwrap_err();
    assert!(err.starts_with("reference SHA-256 mismatch, and its skeleton does not rebuild it"), "{err}");
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn without_an_index_the_reader_is_as_before() {
    let d = dir("index-none");
    let old = tiff(&xmp("rating 0", 0));
    let edited = tiff(&xmp("rating 5", 3001));
    let was = put(&d.join("tree/a/one.tif"), &old);
    let archive = Archive::new(&d);
    archive.identical("one.tif", &was);
    let source = [&old[..1000], b"the source differs here", &old[1000..]].concat();
    if xdelta3() { archive.delta("two.tif", &was, &source); }
    // In place, unchanged.
    assert_eq!(archive.rebuild("one.tif", &[]).unwrap(), old);
    // No skeleton section: an edited reference fails with the texts of 5.5.
    fs::write(&was, &edited).unwrap();
    assert_eq!(archive.rebuild("one.tif", &[]).unwrap_err(), "rebuilt content SHA-256 differs from source_sha256");
    if xdelta3() { assert_eq!(archive.rebuild("two.tif", &[]).unwrap_err(), "reference SHA-256 mismatch"); }
    // The same texts with an index that knows nothing of it.
    let empty = Made::new(&d, "empty.sqlite");
    drop(empty.db);
    assert_eq!(archive.rebuild("one.tif", &[&empty.path]).unwrap_err(), "rebuilt content SHA-256 differs from source_sha256");
    if xdelta3() { assert_eq!(archive.rebuild("two.tif", &[&empty.path]).unwrap_err(), "reference SHA-256 mismatch"); }
    // A missing reference: the same error with and without the index.
    fs::remove_file(&was).unwrap();
    let missing = archive.rebuild("one.tif", &[]).unwrap_err();
    assert!(missing.contains("No such file"), "{missing}");
    assert_eq!(archive.rebuild("one.tif", &[&empty.path]).unwrap_err(), missing);
    // With a skeleton section, the edited file in place rebuilds, as before.
    fs::write(&was, &old).unwrap();
    archive.skeleton(&was);
    fs::write(&was, &edited).unwrap();
    assert_eq!(archive.rebuild("one.tif", &[]).unwrap(), old);
    assert_eq!(archive.rebuild("one.tif", &[&empty.path]).unwrap(), old);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn at_most_candidates_files_are_tried() {
    let d = dir("index-cap");
    let old = tiff(&xmp("rating 0", 0));
    let edited = tiff(&xmp("rating 1", 100));
    let was = put(&d.join("tree/a/one.tif"), &old);
    let archive = Archive::new(&d);
    archive.identical("one.tif", &was);
    archive.skeleton(&was);
    fs::remove_file(&was).unwrap();
    let made = Made::new(&d, "index.sqlite");
    made.row(&was, old.len(), &sha(&old), true, None, Some("image-1"));
    // More wrong candidates than the cap, each with its own SHA-256, listed
    // before the right one.
    for i in 0..crate::read::CANDIDATES {
        let mut wrong = edited.clone();
        let n = wrong.len();
        wrong[n / 2] = i as u8;
        made.live(&put(&d.join(format!("tree/b/wrong-{i}.tif")), &wrong), None, Some("image-1"));
    }
    made.live(&put(&d.join("tree/b/right.tif"), &edited), None, Some("image-1"));
    drop(made.db);
    let err = archive.rebuild("one.tif", &[&made.path]).unwrap_err();
    assert!(err.starts_with("reference SHA-256 mismatch, and its skeleton does not rebuild it"), "{err}");
    // The limit is the reader's: one more candidate reaches the right file, and none uses no index.
    let with = |n: usize| {
        let reader = Reader::open(&archive.top).unwrap().with_indexes([&made.path]).unwrap().with_candidates(n);
        assert_eq!(reader.candidates(), n);
        let record = reader.record("one.tif").unwrap().unwrap();
        let mut out = Vec::new();
        reader.rebuild(&record, None, &mut out).map(|_| out)
    };
    assert_eq!(with(crate::read::CANDIDATES + 1).unwrap(), old);
    assert!(with(0).is_err());
    // Byte-identical copies count once: with the wrong ones all alike, the right one is reached.
    for i in 0..crate::read::CANDIDATES { fs::copy(d.join("tree/b/wrong-0.tif"), d.join(format!("tree/b/wrong-{i}.tif"))).unwrap(); }
    let made = Made::new(&d, "index-2.sqlite");
    made.row(&was, old.len(), &sha(&old), true, None, Some("image-1"));
    for i in 0..crate::read::CANDIDATES { made.live(&d.join(format!("tree/b/wrong-{i}.tif")).to_string_lossy(), None, Some("image-1")); }
    made.live(&d.join("tree/b/right.tif").to_string_lossy(), None, Some("image-1"));
    drop(made.db);
    assert_eq!(archive.rebuild("one.tif", &[&made.path]).unwrap(), old);
    let _ = fs::remove_dir_all(&d);
}

#[test]
fn every_lookup_uses_an_index_and_an_unindexed_column_is_not_looked_up() {
    let d = dir("index-plans");
    let made = Made::new(&d, "index.sqlite");
    drop(made.db);
    let index = Index::open(&made.path).unwrap();
    assert!(index.unindexed().is_empty());
    let plans = index.plans().unwrap();
    assert_eq!(plans.len(), 5);
    for (query, plan) in &plans {
        assert!(plan.starts_with("SEARCH files USING INDEX ") && !plan.contains("SCAN"), "{query}: {plan}");
    }
    // An index without files_image: image_data is reported, and not asked.
    let bare = Made::new(&d, "bare.sqlite");
    bare.db.execute_batch("DROP INDEX files_image").unwrap();
    bare.row("/nowhere/a.tif", 1, "aa", true, None, Some("image-1"));
    bare.row("/nowhere/b.tif", 1, "bb", false, None, Some("image-1"));
    drop(bare.db);
    let index = Index::open(&bare.path).unwrap();
    assert_eq!(index.unindexed(), ["image_data"]);
    assert!(index.identities("aa", &[], 8).unwrap().is_empty());
    assert!(index.sharing("image_data", "image-1", 8).unwrap().is_empty());
    assert_eq!(index.plans().unwrap().len(), 4);
    // Not an index at all.
    fs::write(d.join("not.sqlite"), b"").unwrap();
    assert!(Index::open(d.join("not.sqlite")).is_err());
    assert!(Reader::open(d.join("nothing")).is_err());
    let _ = fs::remove_dir_all(&d);
}

/// The same moves, indexed by phindex itself (PHINDEX names the binary):
/// index, move and edit, index again, restore.
#[test]
fn with_the_phindex_binary() {
    let Some(phindex) = std::env::var_os("PHINDEX") else { eprintln!("PHINDEX not set: skipped"); return };
    let d = dir("index-real");
    let tree = d.join("tree");
    let scan: Vec<u8> = noise(150_000, 5).into_iter().filter(|b| *b != 0xff).collect();
    let jfif = b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0".to_vec();
    let (tif, jpg) = (tiff(&xmp("rating 0", 0)), jpeg(&[(0xe0, jfif.clone()), (0xe1, b"Exif\0\0old".to_vec()), (0xdb, noise(130, 6))], &scan));
    let (tif_was, jpg_was) = (put(&tree.join("a/one.tif"), &tif), put(&tree.join("a/one.jpg"), &jpg));
    let kept = put(&tree.join("a/kept.tif"), &tiff(&xmp("another picture", 7)));
    let archive = Archive::new(&d);
    for (rel, p) in [("one.tif", &tif_was), ("one.jpg", &jpg_was), ("kept.tif", &kept)] { archive.identical(rel, p); archive.skeleton(p); }
    let index = d.join("index.sqlite");
    let run = || {
        let out = std::process::Command::new(&phindex).arg("--db").arg(&index).arg("--root").arg(&tree).args(["--workers", "2"]).output().unwrap();
        assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    };
    run();
    // The TIFF moved and renamed, unchanged; the JPEG moved, renamed and edited.
    fs::create_dir_all(tree.join("b")).unwrap();
    fs::rename(&tif_was, tree.join("b/moved.tif")).unwrap();
    fs::remove_file(&jpg_was).unwrap();
    put(&tree.join("b/edited.jpg"), &jpeg(&[(0xe0, jfif), (0xe1, b"Exif\0\0new and longer".to_vec()), (0xed, b"Photoshop 3.0\0caption".to_vec()), (0xdb, noise(130, 6))], &scan));
    run();
    assert!(archive.rebuild("one.tif", &[]).is_err() && archive.rebuild("one.jpg", &[]).is_err());
    assert_eq!(archive.rebuild("one.tif", &[&index]).unwrap(), tif);
    assert_eq!(archive.rebuild("one.jpg", &[&index]).unwrap(), jpg);
    assert_eq!(archive.rebuild("kept.tif", &[&index]).unwrap(), fs::read(&kept).unwrap());
    // Then the TIFF edited where it now is.
    put(&tree.join("b/moved.tif"), &tiff(&xmp("rating 4", 900)));
    run();
    assert_eq!(archive.rebuild("one.tif", &[&index]).unwrap(), tif);
    let plans = Index::open(&index).unwrap().plans().unwrap();
    for (query, plan) in &plans { eprintln!("{query}\n    {plan}"); assert!(plan.starts_with("SEARCH files USING INDEX "), "{query}: {plan}"); }
    alone(&index);
    let _ = fs::remove_dir_all(&d);
}

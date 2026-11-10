//! Reading an archive (docs/dngc-archive-spec.md, sections 3, 5 and 8): its
//! sections, its records and earlier versions, each record's kind, and
//! rebuilding or restoring one content byte for byte.
//!
//! The archive must be idle: the byte section's database is opened read-only
//! with immutable=1 (section 9), which writes nothing.

use crate::hex;
use crate::os;
use crate::pyjson::{self, J};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use sha2::{Digest, Sha256};
use std::fs::{self, File, FileTimes, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, UNIX_EPOCH};

/// A record's kind (section 5.2), decided in this order: delta, full, identical.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind { Delta, Full, Identical }

pub fn kind(record: &J) -> Kind {
    if record.get("delta").is_some_and(J::truthy) { Kind::Delta }
    else if record.get("original").is_some_and(J::truthy) { Kind::Full }
    else { Kind::Identical }
}

fn field<'a>(record: &'a J, key: &str) -> &'a str { record.get(key).and_then(J::as_str).unwrap_or("") }

/// The archive's top and byte section for a given path (section 3): a legacy
/// archive keeps its byte section at the top; a path named bytes is the byte
/// section of its parent; otherwise the byte section is PATH/bytes.
pub fn sections(path: &Path) -> (PathBuf, PathBuf, bool) {
    if path.join("recovery.sqlite3").exists() && !path.join("bytes").exists() {
        return (path.to_path_buf(), path.to_path_buf(), true);
    }
    if path.file_name().is_some_and(|n| n == "bytes") {
        return (path.parent().unwrap_or(path).to_path_buf(), path.to_path_buf(), false);
    }
    (path.to_path_buf(), path.join("bytes"), false)
}

/// The label an earlier content is stored under (section 1): the source's
/// modification time in UTC and the first 12 hex digits of its SHA-256.
pub fn version_label(record: &J) -> String {
    let mtime = record.get("source_identity").and_then(|i| i.as_arr().get(2)).and_then(J::as_i128).unwrap_or(0);
    crate::archive::version_label(mtime, field(record, "source_sha256"))
}

/// An archive opened for reading.
pub struct Reader {
    top: PathBuf, bytes: PathBuf, legacy: bool, db: Connection, skeletons: Option<crate::skeleton::Store>,
    indexes: Vec<crate::phindex::Index>,
}

/// The most files tried per record in each index-aided step of finding a
/// base: files listed with the content's SHA-256 that are digested, and
/// edited versions a skeleton is rebuilt from.
pub const CANDIDATES: usize = 8;
/// The most index rows read per lookup while choosing those files.
const ROWS: usize = 4 * CANDIDATES;

/// A record's base content as a file: the reference itself, or a temporary
/// file rebuilt from the reference's skeleton, removed when dropped.
struct Base { path: PathBuf, temporary: bool }

impl Drop for Base {
    fn drop(&mut self) { if self.temporary { let _ = fs::remove_file(&self.path); } }
}

static BASES: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

impl Reader {
    pub fn open(path: impl AsRef<Path>) -> Result<Reader, String> {
        let (top, bytes, legacy) = sections(path.as_ref());
        let db_path = bytes.join("recovery.sqlite3");
        if !db_path.exists() { return Err(format!("no byte section in {}", top.display())); }
        let db = Connection::open_with_flags(format!("file:{}?immutable=1", db_path.display()),
                                             OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI)
            .map_err(|e| e.to_string())?;
        let skeletons = crate::skeleton::Store::open(&top)?;
        Ok(Reader { top, bytes, legacy, db, skeletons, indexes: Vec::new() })
    }

    /// This reader, also looking in these phindex indexes for a reference's
    /// content when the file at its recorded path no longer holds it
    /// (section 5.5): a file listed with the same SHA-256, or, with a
    /// skeleton, an edited version listed with the same image identity. An
    /// index is a hint; every file it names is checked by SHA-256 before use.
    /// The indexes are opened read-only with immutable=1 and must be idle.
    pub fn with_indexes<P: AsRef<Path>>(mut self, indexes: impl IntoIterator<Item = P>) -> Result<Reader, String> {
        for p in indexes { self.indexes.push(crate::phindex::Index::open(p)?); }
        Ok(self)
    }

    pub fn top(&self) -> &Path { &self.top }
    pub fn byte_section(&self) -> &Path { &self.bytes }
    pub fn legacy(&self) -> bool { self.legacy }

    fn has_table(&self, name: &str) -> bool {
        self.db.query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?1", [name], |r| r.get::<_, i64>(0))
            .is_ok_and(|n| n > 0)
    }

    fn rows(&self, sql: &str) -> Result<Vec<Vec<String>>, String> {
        let mut st = self.db.prepare(sql).map_err(|e| e.to_string())?;
        let n = st.column_count();
        let rows = st.query_map([], |r| (0..n).map(|i| r.get::<_, String>(i)).collect::<Result<Vec<_>, _>>())
            .map_err(|e| e.to_string())?;
        rows.collect::<Result<_, _>>().map_err(|e| e.to_string())
    }

    /// The current record of each path, by path, with its JSON text.
    pub fn records(&self) -> Result<Vec<(String, String, J)>, String> {
        self.rows("SELECT path, record FROM files ORDER BY path")?.into_iter()
            .map(|r| { let j = pyjson::parse(&r[1])?; Ok((r[0].clone(), r[1].clone(), j)) }).collect()
    }

    /// One path's current record.
    pub fn record(&self, path: &str) -> Result<Option<J>, String> {
        let text: Option<String> = self.db.query_row("SELECT record FROM files WHERE path=?1", [path], |r| r.get(0))
            .optional().map_err(|e| e.to_string())?;
        text.map(|t| pyjson::parse(&t)).transpose()
    }

    /// Earlier contents: (path, version label, record), by path and label.
    pub fn versions(&self) -> Result<Vec<(String, String, J)>, String> {
        if !self.has_table("versions") { return Ok(Vec::new()); }
        self.rows("SELECT path, version, record FROM versions ORDER BY path, version")?.into_iter()
            .map(|r| Ok((r[0].clone(), r[1].clone(), pyjson::parse(&r[2])?))).collect()
    }

    /// The drop that left a tombstone for this path (section 8.2), if any.
    pub fn dropped(&self, path: &str) -> Result<Option<String>, String> {
        if !self.has_table("dropped") { return Ok(None); }
        self.db.query_row("SELECT drop_id FROM dropped WHERE path=?1", [path], |r| r.get(0)).optional().map_err(|e| e.to_string())
    }

    /// Where a record's reference is: its own path, or below reference_root by
    /// reference_relative_path when the reference tree has moved.
    pub fn reference(&self, record: &J, reference_root: Option<&Path>) -> PathBuf {
        match (reference_root, record.get("reference_relative_path").and_then(J::as_str)) {
            (Some(root), Some(rel)) => root.join(rel),
            _ => PathBuf::from(field(record, "reference")),
        }
    }

    /// A skeleton's content rebuilt from `edited` into a temporary file, if
    /// that gives the content `sha256`.
    fn rebuilt(skeleton: &crate::skeleton::Skeleton, edited: &Path, sha256: &str) -> Result<Base, String> {
        let n = BASES.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let base = Base { path: std::env::temp_dir().join(format!("dngc-base-{}-{n}", std::process::id())), temporary: true };
        let mut f = OpenOptions::new().write(true).create_new(true).open(&base.path).map_err(|e| e.to_string())?;
        let got = crate::skeleton::rebuild(skeleton, edited, &mut f)
            .map_err(|e| format!("reference SHA-256 mismatch, and its skeleton does not rebuild it: {e}"))?;
        if got != sha256 { return Err("reference SHA-256 mismatch, and its skeleton rebuilds other bytes".into()); }
        Ok(base)
    }

    /// A file an index lists as holding `sha256` that holds it now. A row is a
    /// hint: a file of another size is skipped unread, any other is digested.
    fn listed(&self, sha256: &str, reference: &Path) -> Option<PathBuf> {
        let mut tried: Vec<String> = Vec::new();
        for index in &self.indexes {
            for row in index.holding(sha256, ROWS).unwrap_or_default() {
                if tried.len() >= CANDIDATES { return None; }
                let path = Path::new(&row.path);
                if path == reference || tried.contains(&row.path) { continue; }
                if !fs::metadata(path).is_ok_and(|m| m.is_file() && m.len() == row.size) { continue; }
                if crate::archive::digest_file(path).as_deref() == Ok(sha256) { return Some(path.to_path_buf()); }
                tried.push(row.path);
            }
        }
        None
    }

    /// Files that may be edited versions of the content `sha256`, last seen at
    /// one of `paths`: live files an index lists with an edit-surviving
    /// identity (DNG raw blocks, image data) of a row that has that SHA-256,
    /// gone or not, or of the row at one of those paths. One file per listed
    /// SHA-256, since byte-identical copies rebuild alike.
    fn edited(&self, sha256: &str, paths: &[&str], reference: &Path) -> Vec<PathBuf> {
        let mut identities = Vec::new();
        for index in &self.indexes {
            for i in index.identities(sha256, paths, ROWS).unwrap_or_default() { if !identities.contains(&i) { identities.push(i); } }
        }
        let (mut out, mut seen): (Vec<PathBuf>, Vec<String>) = (Vec::new(), Vec::new());
        for (column, value) in &identities {
            for index in &self.indexes {
                for row in index.sharing(column, value, ROWS).unwrap_or_default() {
                    if out.len() >= CANDIDATES { return out; }
                    let path = PathBuf::from(&row.path);
                    if path == reference || out.contains(&path) || (!row.sha256.is_empty() && seen.contains(&row.sha256)) { continue; }
                    if !path.is_file() { continue; }
                    seen.push(row.sha256);
                    out.push(path);
                }
            }
        }
        out
    }

    /// The content `sha256` that a record names as its base, expected at
    /// `reference` (section 5.5, step 2), in this order: the file there; a
    /// file an index lists with that SHA-256 and that holds it now; the
    /// content rebuilt from its skeleton (section 13) and the file there, or
    /// another file an index lists as an edited version of it. Whatever is
    /// used has been checked to hold `sha256`.
    fn base(&self, record: &J, reference: &Path, sha256: &str) -> Result<Base, String> {
        let found = crate::archive::digest_file(reference);
        if found.as_deref() == Ok(sha256) { return Ok(Base { path: reference.to_path_buf(), temporary: false }); }
        if let Some(path) = self.listed(sha256, reference) { return Ok(Base { path, temporary: false }); }
        let Some((skeleton_record, skeleton)) = self.skeletons.as_ref().map(|s| s.get(sha256)).transpose()?.flatten() else {
            found?;
            return Err("reference SHA-256 mismatch".into());
        };
        let failed = match Reader::rebuilt(&skeleton, reference, sha256) { Ok(base) => return Ok(base), Err(e) => e };
        if !self.indexes.is_empty() {
            let here = reference.to_string_lossy();
            let paths = [field(record, "reference"), &here, field(&skeleton_record, "path")];
            let mut asked: Vec<&str> = Vec::new();
            for p in paths { if !p.is_empty() && !asked.contains(&p) { asked.push(p); } }
            for candidate in self.edited(sha256, &asked, reference) {
                if let Ok(base) = Reader::rebuilt(&skeleton, &candidate, sha256) { return Ok(base); }
            }
        }
        Err(failed)
    }

    /// Whether the file at `reference` still has the size and modification
    /// time the record saw.
    fn unchanged(reference: &Path, record: &J) -> bool {
        let identity = record.get("reference_identity").map(J::as_arr).unwrap_or(&[]);
        match (os::stat(&reference.to_string_lossy()), identity.get(1).and_then(J::as_i128), identity.get(2).and_then(J::as_i128)) {
            (Ok(st), Some(size), Some(mtime)) => st.size as i128 == size && st.mtime_ns == mtime,
            _ => false,
        }
    }

    /// Rebuild a content into `sink`, returning its SHA-256 (section 5.5,
    /// steps 2 and 3). A delta's reference is checked against
    /// reference_sha256 before it is decoded.
    pub fn rebuild(&self, record: &J, reference_root: Option<&Path>, sink: &mut dyn Write) -> Result<String, String> {
        let mut hasher = Sha256::new();
        let mut copy = |reader: &mut dyn Read| -> Result<(), String> {
            let mut buf = vec![0u8; 1 << 20];
            loop {
                let n = reader.read(&mut buf).map_err(|e| e.to_string())?;
                if n == 0 { return Ok(()); }
                hasher.update(&buf[..n]);
                sink.write_all(&buf[..n]).map_err(|e| e.to_string())?;
            }
        };
        match kind(record) {
            Kind::Full => {
                let p = self.bytes.join(field(record, "original"));
                copy(&mut File::open(&p).map_err(|e| os::os_error(&e, Some(&p.to_string_lossy())))?)?;
            }
            Kind::Identical => {
                // Read as it is while it looks unchanged; the check of what was
                // written decides. A changed one goes through its skeleton.
                let p = self.reference(record, reference_root);
                // Without a skeleton section, a reference no index finds is
                // read as it is, to fail as it always has.
                let aided = self.skeletons.is_some() || !self.indexes.is_empty();
                let base = if !aided || Reader::unchanged(&p, record) { Base { path: p, temporary: false } }
                           else {
                               match self.base(record, &p, field(record, "source_sha256")) {
                                   Err(_) if self.skeletons.is_none() => Base { path: p, temporary: false },
                                   found => found?,
                               }
                           };
                copy(&mut File::open(&base.path).map_err(|e| os::os_error(&e, Some(&base.path.to_string_lossy())))?)?;
            }
            Kind::Delta => {
                let base = self.base(record, &self.reference(record, reference_root), field(record, "reference_sha256"))?;
                let reference = &base.path;
                let delta = self.bytes.join(field(record, "delta"));
                if crate::archive::digest_file(&delta)? != field(record, "delta_sha256") {
                    return Err("VCDIFF archive hash mismatch".into());
                }
                let mut child = Command::new("xdelta3").args(["-d", "-D", "-R", "-c", "-s"]).arg(reference).arg(&delta)
                    .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()
                    .map_err(|e| os::os_error(&e, Some("xdelta3")))?;
                let mut out = child.stdout.take().unwrap();
                let copied = copy(&mut out);
                let output = child.wait_with_output().map_err(|e| e.to_string())?;
                copied?;
                if !output.status.success() {
                    return Err(format!("VCDIFF decode failed: {}", String::from_utf8_lossy(&output.stderr)));
                }
            }
        }
        let got = hex(&hasher.finalize());
        if got != field(record, "source_sha256") { return Err("rebuilt content SHA-256 differs from source_sha256".into()); }
        Ok(got)
    }

    /// Restore a content at `target` (section 5.5): refuse an existing target,
    /// write OUTPUT.recovery-partial while checking the SHA-256, fsync, link it
    /// into place, then restore the source's extended attributes, mode, times
    /// and flags.
    pub fn restore(&self, record: &J, target: &Path, reference_root: Option<&Path>) -> Result<(), String> {
        if target.exists() { return Err(format!("{} exists", target.display())); }
        let mut partial = target.as_os_str().to_owned();
        partial.push(".recovery-partial");
        let partial = PathBuf::from(partial);
        let mut f = OpenOptions::new().write(true).create_new(true).open(&partial).map_err(|e| e.to_string())?;
        let written = self.rebuild(record, reference_root, &mut f).and_then(|_| f.sync_all().map_err(|e| e.to_string()));
        drop(f);
        if let Err(e) = written { let _ = fs::remove_file(&partial); return Err(e); }
        fs::hard_link(&partial, target).map_err(|e| e.to_string())?;
        fs::remove_file(&partial).map_err(|e| e.to_string())?;
        set_attributes(target, record.get("source_attrs").unwrap_or(&J::Null))
    }
}

fn ns_time(ns: i128) -> std::time::SystemTime {
    if ns >= 0 { UNIX_EPOCH + Duration::from_nanos(ns as u64) } else { UNIX_EPOCH - Duration::from_nanos((-ns) as u64) }
}

#[cfg(target_os = "macos")]
unsafe extern "C" { fn chflags(path: *const std::ffi::c_char, flags: u32) -> std::ffi::c_int; }

/// source_attrs onto a restored file: extended attributes, mode, times, flags.
fn set_attributes(target: &Path, attrs: &J) -> Result<(), String> {
    let path = target.to_string_lossy().into_owned();
    if let Some(J::Obj(xattrs)) = attrs.get("xattrs") {
        for (name, value) in xattrs {
            os::set_xattr(&path, name, &os::unbase64(value.as_str().unwrap_or("")))?;
        }
    }
    if let Some(mode) = attrs.get("mode").and_then(J::as_i128) {
        fs::set_permissions(target, fs::Permissions::from_mode((mode as u32) & 0o7777)).map_err(|e| e.to_string())?;
    }
    if let (Some(a), Some(m)) = (attrs.get("atime_ns").and_then(J::as_i128), attrs.get("mtime_ns").and_then(J::as_i128)) {
        let f = OpenOptions::new().write(true).open(target).or_else(|_| File::open(target)).map_err(|e| e.to_string())?;
        f.set_times(FileTimes::new().set_accessed(ns_time(a)).set_modified(ns_time(m)))
            .map_err(|e: io::Error| e.to_string())?;
    }
    #[cfg(target_os = "macos")]
    if let Some(flags) = attrs.get("flags").and_then(J::as_i128).filter(|f| *f != 0) {
        let c = std::ffi::CString::new(path.clone()).map_err(|e| e.to_string())?;
        if unsafe { chflags(c.as_ptr(), flags as u32) } != 0 { return Err(io::Error::last_os_error().to_string()); }
    }
    Ok(())
}

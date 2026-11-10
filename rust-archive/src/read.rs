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
pub struct Reader { top: PathBuf, bytes: PathBuf, legacy: bool, db: Connection }

impl Reader {
    pub fn open(path: impl AsRef<Path>) -> Result<Reader, String> {
        let (top, bytes, legacy) = sections(path.as_ref());
        let db_path = bytes.join("recovery.sqlite3");
        if !db_path.exists() { return Err(format!("no byte section in {}", top.display())); }
        let db = Connection::open_with_flags(format!("file:{}?immutable=1", db_path.display()),
                                             OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI)
            .map_err(|e| e.to_string())?;
        Ok(Reader { top, bytes, legacy, db })
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
                let p = self.reference(record, reference_root);
                copy(&mut File::open(&p).map_err(|e| os::os_error(&e, Some(&p.to_string_lossy())))?)?;
            }
            Kind::Delta => {
                let reference = self.reference(record, reference_root);
                if crate::archive::digest_file(&reference)? != field(record, "reference_sha256") {
                    return Err("reference SHA-256 mismatch".into());
                }
                let delta = self.bytes.join(field(record, "delta"));
                if crate::archive::digest_file(&delta)? != field(record, "delta_sha256") {
                    return Err("VCDIFF archive hash mismatch".into());
                }
                let mut child = Command::new("xdelta3").args(["-d", "-D", "-R", "-c", "-s"]).arg(&reference).arg(&delta)
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

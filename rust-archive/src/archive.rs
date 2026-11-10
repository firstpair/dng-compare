//! preserve_apo.py `scan --delta --pairs`: recovery.sqlite3, deltas/, originals/,
//! inventory.json, directories.json, summary.json and manifest.jsonl, written
//! with the records preserve_file(..., delta_mode=True) makes. Files arrive
//! already read; xdelta3 encodes from disk with preserve_apo's arguments and the
//! delta is decoded again to the source SHA-256 before it counts.

use crate::os::{self, Stat, os_error};
use crate::pyjson::{self, J, int, opt_s, py_eq, py_repr_str, s};
use crate::{hex, phindex::Indexes};
use rusqlite::{Connection, params};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const BLOCK: usize = 1024 * 1024;
const XDELTA_ENCODE: [&str; 13] = ["-e", "-1", "-S", "none", "-n", "-a", "-D", "-R", "-A", "-B", "16777216", "-W", "8388608"];

pub struct Budget { rate: f64, start: Instant, bytes: Mutex<u64> }

impl Budget {
    pub fn new(mib: f64) -> Self { Budget { rate: mib * 1024.0 * 1024.0, start: Instant::now(), bytes: Mutex::new(0) } }
    pub fn charge(&self, n: u64) {
        let delay = { let mut b = self.bytes.lock().unwrap(); *b += n; *b as f64 / self.rate - self.start.elapsed().as_secs_f64() };
        if delay > 0.0 { std::thread::sleep(Duration::from_secs_f64(delay)); }
    }
    fn bytes(&self) -> u64 { *self.bytes.lock().unwrap() }
}

/// What preservation workers share.
pub struct Ctx { pub dir: PathBuf, pub reference: String, pub budget: Budget }

pub fn digest_file(path: &Path) -> Result<String, String> {
    let mut f = fs::File::open(path).map_err(|e| os_error(&e, Some(&path.to_string_lossy())))?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; BLOCK];
    loop {
        let n = f.read(&mut buf).map_err(|e| os_error(&e, None))?;
        if n == 0 { break; }
        h.update(&buf[..n]);
    }
    Ok(hex(&h.finalize()))
}

fn atomic_json(path: &Path, v: &J) -> Result<(), String> {
    let tmp = PathBuf::from(format!("{}.tmp", path.display()));
    let mut f = fs::File::create(&tmp).map_err(|e| os_error(&e, Some(&tmp.to_string_lossy())))?;
    f.write_all(pyjson::indent(v).as_bytes()).and_then(|_| f.sync_all()).map_err(|e| os_error(&e, None))?;
    fs::rename(&tmp, path).map_err(|e| os_error(&e, None))
}

fn relative_to(path: &str, root: &str) -> Result<String, String> {
    if path == root { return Ok(".".into()); }
    match path.strip_prefix(root).and_then(|r| r.strip_prefix('/')) {
        Some(r) if !root.is_empty() => Ok(r.to_string()),
        _ if root == "/" && path.starts_with('/') => Ok(path[1..].to_string()),
        _ => Err(format!("{} is not in the subpath of {}", py_repr_str(path), py_repr_str(root))),
    }
}

fn parent(path: &str) -> &str { match path.rfind('/') { Some(0) => "/", Some(i) => &path[..i], None => "." } }

fn fsync_path(p: &Path) -> Result<(), String> {
    fs::File::open(p).and_then(|f| f.sync_all()).map_err(|e| os_error(&e, Some(&p.to_string_lossy())))
}

fn command_error(args: &[String], status: std::process::ExitStatus) -> String {
    use std::os::unix::process::ExitStatusExt;
    let cmd = format!("[{}]", args.iter().map(|a| py_repr_str(a)).collect::<Vec<_>>().join(", "));
    match (status.code(), status.signal()) {
        (Some(c), _) => format!("Command '{cmd}' returned non-zero exit status {c}."),
        (_, Some(sig)) => format!("Command '{cmd}' died with signal {sig}."),
        _ => format!("Command '{cmd}' failed."),
    }
}

/// preserve_apo.stream_delta(record, archive, budget=budget)
fn stream_delta(record: &J, ctx: &Ctx) -> Result<String, String> {
    let get = |k: &str| record.get(k).and_then(J::as_str).unwrap_or("").to_string();
    let delta = ctx.dir.join(get("delta"));
    if digest_file(&delta)? != get("delta_sha256") { return Err("VCDIFF archive hash mismatch".into()); }
    let mut child = Command::new("xdelta3").args(["-d", "-D", "-R", "-c", "-s"]).arg(get("reference")).arg(&delta)
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().map_err(|e| os_error(&e, Some("xdelta3")))?;
    let mut err = child.stderr.take().unwrap();
    let errors = std::thread::spawn(move || { let mut v = Vec::new(); let _ = err.read_to_end(&mut v); v });
    let mut out = child.stdout.take().unwrap();
    let mut h = Sha256::new();
    let mut buf = vec![0u8; BLOCK];
    let read = loop {
        match out.read(&mut buf) {
            Ok(0) => break Ok(()),
            Ok(n) => { h.update(&buf[..n]); ctx.budget.charge(n as u64); }
            Err(e) => break Err(os_error(&e, None)),
        }
    };
    if read.is_err() { let _ = child.kill(); }
    let status = child.wait().map_err(|e| os_error(&e, None))?;
    let stderr = String::from_utf8_lossy(&errors.join().unwrap_or_default()).into_owned();
    read?;
    if !status.success() { return Err(format!("VCDIFF decode failed: {stderr}")); }
    let got = hex(&h.finalize());
    if got != get("source_sha256") { return Err("Reconstructed original SHA-256 mismatch".into()); }
    Ok(got)
}

/// The part of preserve_apo.process that accepts a saved record without redoing it.
pub fn saved_valid(ctx: &Ctx, text: &str, source: &str) -> Result<bool, String> {
    let r = pyjson::parse(text)?;
    let base = r.get("reference").and_then(J::as_str);
    if !py_eq(&os::stat(source)?.identity(), r.get("source_identity").unwrap_or(&J::Null)) { return Ok(false); }
    if let Some(b) = base && !py_eq(&os::stat(b)?.identity(), r.get("reference_identity").unwrap_or(&J::Null)) { return Ok(false); }
    let check = |k: &str, hash: &str| -> Result<bool, String> {
        match r.get(k) {
            Some(v) if v.truthy() => {
                let p = ctx.dir.join(v.as_str().unwrap_or(""));
                Ok(p.is_file() && digest_file(&p)? == r.get(hash).and_then(J::as_str).unwrap_or(""))
            }
            _ => Ok(true),
        }
    };
    Ok(check("original", "source_sha256")? && check("delta", "delta_sha256")?)
}

pub struct Pre { pub before: Stat, pub meta: J, pub base: Option<String>, pub prior: Option<Stat> }

/// preserve_file's opening: identities and attributes before reading.
pub fn pre(source: &str, reference: &str) -> Result<Pre, String> {
    let before = os::stat(source)?;
    let meta = os::attrs(source)?;
    let base = os::is_file(reference).then(|| reference.to_string());
    let prior = match &base { Some(b) => Some(os::stat(b)?), None => None };
    Ok(Pre { before, meta, base, prior })
}

/// An archived content's version: its source's modification time (UTC) and
/// the start of its SHA-256, e.g. 20261029T171631Z-b755b147cbed, as
/// preserve_apo.version_label.
pub fn version_label(mtime_ns: i128, sha256: &str) -> String {
    let secs = mtime_ns.div_euclid(1_000_000_000) as i64;
    let (days, rem) = (secs.div_euclid(86400), secs.rem_euclid(86400));
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    format!("{y:04}{m:02}{d:02}T{:02}{:02}{:02}Z-{}", rem / 3600, rem % 3600 / 60, rem % 60, &sha256[..sha256.len().min(12)])
}

/// The version label of a saved record's text.
fn label_of(text: &str) -> Result<String, String> {
    let r = pyjson::parse(text)?;
    let mtime = r.get("source_identity").and_then(|i| match i { J::Arr(v) => v.get(2).and_then(J::as_i128), _ => None }).unwrap_or(0);
    Ok(version_label(mtime, r.get("source_sha256").and_then(J::as_str).unwrap_or("")))
}

fn record(rel: &str, source: &str, p: &Pre, source_hash: &str, reference_hash: Option<&str>, original: Option<&str>,
          equal: bool, preserved: u64) -> J {
    J::obj().with("relative_path", s(rel)).with("source", s(source)).with("reference", opt_s(p.base.as_deref()))
        .with("source_identity", p.before.identity()).with("reference_identity", p.prior.map_or(J::Null, |x| x.identity()))
        .with("source_attrs", p.meta.clone()).with("source_sha256", s(source_hash))
        .with("reference_sha256", opt_s(reference_hash)).with("original", opt_s(original))
        .with("whole_file_equal", J::Bool(equal)).with("preserved_bytes", int(preserved))
        .with("reconstruction_verified", J::Bool(true))
}

impl Ctx {
    fn finish(&self, mut r: J, p: &Pre) -> Result<J, String> {
        let rel = p.base.as_deref().map(|b| relative_to(b, &self.reference)).transpose()?;
        r.set("reference_relative_path", opt_s(rel));
        Ok(r)
    }

    /// The phindex shortcut of preserve_file: equal current hashes, nothing read.
    pub fn indexed(&self, indexes: &Indexes, rel: &str, source: &str, p: &Pre) -> Result<Option<J>, String> {
        let (Some(base), Some(prior)) = (&p.base, &p.prior) else { return Ok(None) };
        let a = indexes.sha256(0, source, &p.before);
        let b = indexes.sha256(1, base, prior);
        match (a, b) {
            (Some(a), Some(b)) if a == b => self.finish(record(rel, source, p, &a, Some(&b), None, true, 0), p).map(Some),
            _ => Ok(None),
        }
    }

    /// preserve_file(source, base, ...) after both files were read and hashed.
    /// `versioned`: the relative path already has a record of other content,
    /// whose payload stays where it is; this content's payload goes to versions/LABEL/.
    pub fn preserve(&self, rel: &str, source: &str, p: &Pre, a: &[u8], b: Option<&[u8]>, source_hash: &str,
                    reference_hash: Option<&str>, versioned: bool) -> Result<J, String> {
        let changed = b.map_or(!a.is_empty(), |b| a != b);
        let unchanged = || -> Result<bool, String> {
            Ok(os::stat(source)?.identity().eq_ident(&p.before.identity())
               && match (&p.base, &p.prior) { (Some(b), Some(pr)) => os::stat(b)?.identity().eq_ident(&pr.identity()), _ => true })
        };
        if !unchanged()? { return Err("File changed during comparison".into()); }
        let prefix = if versioned { format!("versions/{}/", version_label(p.before.mtime_ns, source_hash)) } else { String::new() };
        let mut original = None;
        if p.base.is_none() {
            let original_rel = format!("{prefix}originals/{rel}");
            let dest = self.dir.join(&original_rel);
            fs::create_dir_all(dest.parent().unwrap()).map_err(|e| os_error(&e, None))?;
            let temp = PathBuf::from(format!("{}.partial", dest.display()));
            let tname = temp.to_string_lossy().into_owned();
            self.budget.charge(a.len() as u64 * 2);
            let mut f = fs::File::create(&temp).map_err(|e| os_error(&e, Some(&tname)))?;
            f.write_all(a).and_then(|_| f.sync_all()).map_err(|e| os_error(&e, None))?;
            if let J::Obj(x) = p.meta.get("xattrs").unwrap() {
                for (name, value) in x { os::set_xattr(&tname, name, &os::unbase64(value.as_str().unwrap_or("")))?; }
            }
            let num = |k: &str| p.meta.get(k).and_then(J::as_i128).unwrap_or(0);
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&temp, fs::Permissions::from_mode(num("mode") as u32)).map_err(|e| os_error(&e, Some(&tname)))?;
            let time = |n: i128| std::time::UNIX_EPOCH + Duration::from_nanos(n.max(0) as u64);
            f.set_times(fs::FileTimes::new().set_accessed(time(num("atime_ns"))).set_modified(time(num("mtime_ns"))))
                .map_err(|e| os_error(&e, Some(&tname)))?;
            drop(f);
            if digest_file(&temp)? != source_hash { return Err("Archive copy read-back failed".into()); }
            fs::rename(&temp, &dest).map_err(|e| os_error(&e, None))?;
            original = Some(original_rel);
        }
        let mut r = record(rel, source, p, source_hash, reference_hash, original.as_deref(),
                           p.base.is_some() && !changed, if original.is_some() { p.before.size } else { 0 });
        if let (true, Some(base), Some(prior)) = (changed, &p.base, &p.prior) {
            let delta_rel = format!("{prefix}deltas/{rel}.vcdiff");
            let dest = self.dir.join(&delta_rel);
            fs::create_dir_all(dest.parent().unwrap()).map_err(|e| os_error(&e, None))?;
            let temp = PathBuf::from(format!("{}.partial", dest.display()));
            if temp.exists() { fs::remove_file(&temp).map_err(|e| os_error(&e, None))?; }
            self.budget.charge(p.before.size + prior.size);
            let mut args: Vec<String> = std::iter::once("xdelta3").chain(XDELTA_ENCODE).chain(["-s"]).map(String::from).collect();
            args.extend([base.clone(), source.to_string(), temp.to_string_lossy().into_owned()]);
            let out = Command::new(&args[0]).args(&args[1..]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped())
                .output().map_err(|e| os_error(&e, Some("xdelta3")))?;
            if !out.status.success() { return Err(command_error(&args, out.status)); }
            fsync_path(&temp)?;
            fs::rename(&temp, &dest).map_err(|e| os_error(&e, None))?;
            r.set("delta", s(delta_rel));
            r.set("delta_sha256", s(digest_file(&dest)?));
            r.set("delta_format", s("VCDIFF RFC 3284"));
            r.set("preserved_bytes", int(fs::metadata(&dest).map_err(|e| os_error(&e, None))?.len()));
            stream_delta(&r, self)?;
        }
        if !unchanged()? { return Err("File changed during preservation".into()); }
        let now = os::attrs(source)?;
        if !py_eq(now.get("xattrs").unwrap(), p.meta.get("xattrs").unwrap()) { return Err("Source attributes changed".into()); }
        self.finish(r, p)
    }
}

trait EqIdent { fn eq_ident(&self, other: &J) -> bool; }
impl EqIdent for J { fn eq_ident(&self, other: &J) -> bool { py_eq(self, other) } }

pub struct Archive {
    pub ctx: Ctx,
    db: Connection,
    _lock: fs::File,
    source: String,
    total: usize,
    workers: usize,
    saved: HashMap<String, String>,
    versioned: HashSet<String>,
    missing: Vec<String>,
    seen_missing: HashSet<String>,
    error_paths: HashSet<String>,
    totals: [i128; 3],
    files: Vec<String>,
    start: Instant,
    last: Instant,
}

fn sql(e: rusqlite::Error) -> String { e.to_string() }

fn account(totals: &mut [i128; 3], text: &str, direction: i128) {
    if let Ok(r) = pyjson::parse(text) { account_j(totals, &r, direction); }
}
fn account_j(totals: &mut [i128; 3], r: &J, direction: i128) {
    totals[0] += direction * r.get("preserved_bytes").and_then(J::as_i128).unwrap_or(0);
    totals[1] += direction * r.get("source_identity").and_then(|i| i.as_arr().get(1)).and_then(J::as_i128).unwrap_or(0);
    totals[2] += direction * (!r.get("whole_file_equal").is_some_and(J::truthy)) as i128;
}

/// The byte section of `archive`, the rule every dng-compare tool shares
/// (docs/dngc.md): the archive itself when it holds `recovery.sqlite3` and no
/// `bytes/` (a legacy archive); the path itself when its last component is
/// `bytes` (given directly, never nested); otherwise `ARCHIVE/bytes`.
pub fn byte_section(archive: &str) -> String {
    let a = Path::new(archive);
    if a.join("recovery.sqlite3").exists() && !a.join("bytes").exists() { return archive.to_string(); }
    if a.file_name().is_some_and(|n| n == "bytes") { return archive.to_string(); }
    a.join("bytes").to_string_lossy().into_owned()
}

#[cfg(test)]
mod layout_tests {
    use super::{byte_section, version_label};

    #[test]
    fn version_labels_match_python() {
        // archive_layout.version_label on the same identities.
        for (m, want) in [(0i128, "19700101T000000Z-abcdef012345"), (1793215891123456789, "20261028T193131Z-abcdef012345"),
                          (951782400000000000, "20000229T000000Z-abcdef012345"), (4102444799999999999, "20991231T235959Z-abcdef012345")] {
            assert_eq!(version_label(m, "abcdef0123456789ff"), want);
        }
    }
    use std::fs;

    #[test]
    fn byte_section_rule() {
        let t = std::env::temp_dir().join(format!("dng-layout-{}", std::process::id()));
        let (new, legacy, unified) = (t.join("new"), t.join("legacy"), t.join("unified"));
        fs::create_dir_all(&new).unwrap();
        fs::create_dir_all(&legacy).unwrap();
        fs::write(legacy.join("recovery.sqlite3"), b"").unwrap();
        fs::create_dir_all(unified.join("bytes")).unwrap();
        fs::write(unified.join("bytes/recovery.sqlite3"), b"").unwrap();
        let s = |p: &std::path::Path| p.to_string_lossy().into_owned();
        assert_eq!(byte_section(&s(&new)), s(&new.join("bytes")));
        assert_eq!(byte_section(&s(&t.join("missing"))), s(&t.join("missing/bytes")));
        assert_eq!(byte_section(&s(&legacy)), s(&legacy));
        assert_eq!(byte_section(&s(&unified)), s(&unified.join("bytes")));
        assert_eq!(byte_section(&s(&unified.join("bytes"))), s(&unified.join("bytes")));
        assert_eq!(byte_section(&s(&new.join("bytes"))), s(&new.join("bytes")));
        fs::remove_dir_all(&t).unwrap();
    }
}

/// Outcome of a listed file: a new record, a saved record kept, or an error.
pub enum Outcome { Kept(J), Unchanged, Failed(String) }

impl Archive {
    /// validate_output, the phase lock and _scan's setup for `pairs` (resolved LEFT, RIGHT paths in listed order).
    pub fn open(source: &str, reference: &str, dir: &str, rate: f64, workers: usize, pairs: &[(String, String)]) -> Result<Archive, String> {
        let output = os::resolve(dir);
        for root in [source, reference] {
            let root = fs::canonicalize(root).map_err(|e| os_error(&e, Some(root)))?.to_string_lossy().into_owned();
            if !Path::new(&root).is_dir() { return Err(format!("Input root is not a directory: {root}")); }
            let inside = |a: &str, b: &str| a == b || a.starts_with(&format!("{}/", b.trim_end_matches('/')));
            if inside(&output, &root) || inside(&root, &output) {
                return Err(format!("Output must be outside and disjoint from input roots: {output}"));
            }
        }
        if !(rate > 0.0 && rate.is_finite()) { return Err("Rate must be finite and positive".into()); }
        let out = PathBuf::from(&output);
        fs::create_dir_all(&out).map_err(|e| os_error(&e, Some(&output)))?;
        let lock = os::try_lock(&out.join(".byte-preservation.lock"), false).map_err(|e| os_error(&e, None))?
            .ok_or("Another process owns this archive phase: .byte-preservation.lock")?;
        if !(1..=36).contains(&workers) { return Err("Preservation workers must be 1..36".into()); }
        let db = Connection::open(out.join("recovery.sqlite3")).map_err(sql)?;
        db.busy_timeout(Duration::from_secs(60)).map_err(sql)?;
        db.query_row("PRAGMA journal_mode=WAL", [], |_| Ok(())).map_err(sql)?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, record TEXT NOT NULL);
                          CREATE TABLE IF NOT EXISTS errors (path TEXT PRIMARY KEY, error TEXT NOT NULL);
                          CREATE TABLE IF NOT EXISTS versions (path TEXT NOT NULL, version TEXT NOT NULL, record TEXT NOT NULL,
                                                               PRIMARY KEY(path, version));").map_err(sql)?;
        // A listed source that is gone (a wiped shuttle) is reported, not
        // preserved; its earlier record, if any, stays as it is.
        let mut files = Vec::new();
        let mut missing = Vec::new();
        let mut seen = HashSet::new();
        for (l, r) in pairs {
            let rel = relative_to(l, source)?;
            relative_to(r, reference)?;
            match fs::symlink_metadata(l) {
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => { if seen.insert(rel.clone()) { missing.push(rel); } continue; }
                m => if !m.is_ok_and(|m| m.is_file()) { return Err(format!("Unsupported source entry: {l}")); },
            }
            files.push(l.clone());
        }
        let mut dirs: Vec<&str> = files.iter().map(|f| parent(f)).collect();
        dirs.sort_by(|a, b| a.split('/').cmp(b.split('/')));
        dirs.dedup();
        let mut listing = Vec::new();
        for d in dirs { listing.push(J::obj().with("path", s(relative_to(d, source)?)).with("attributes", os::attrs(d)?)); }
        atomic_json(&out.join("directories.json"), &J::Arr(listing))?;
        let rels: Vec<J> = files.iter().map(|f| relative_to(f, source).map(s)).collect::<Result<_, _>>()?;
        atomic_json(&out.join("inventory.json"), &J::Arr(rels))?;
        let mut totals = [0; 3];
        let mut saved = HashMap::new();
        {
            let mut q = db.prepare("SELECT path,record FROM files").map_err(sql)?;
            let rows = q.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).map_err(sql)?;
            for row in rows { let (rel, text) = row.map_err(sql)?; account(&mut totals, &text, 1); saved.insert(rel, text); }
        }
        let error_paths = {
            let mut q = db.prepare("SELECT path FROM errors").map_err(sql)?;
            q.query_map([], |r| r.get::<_, String>(0)).map_err(sql)?.collect::<Result<HashSet<_>, _>>().map_err(sql)?
        };
        let mut versioned: HashSet<String> = {
            let mut q = db.prepare("SELECT DISTINCT path FROM versions").map_err(sql)?;
            q.query_map([], |r| r.get::<_, String>(0)).map_err(sql)?.collect::<Result<HashSet<_>, _>>().map_err(sql)?
        };
        versioned.extend(saved.keys().cloned());
        for rel in &missing { println!("MISSING {rel}"); }
        let a = Archive { ctx: Ctx { dir: out, reference: reference.to_string(), budget: Budget::new(rate) }, db, _lock: lock,
                          source: source.to_string(), total: files.len(), workers, saved, versioned, missing, seen_missing: seen, error_paths, totals, files,
                          start: Instant::now(), last: Instant::now() };
        a.summary(false)?;
        Ok(a)
    }

    pub fn rel(&self, left: &str) -> String { relative_to(left, &self.source).unwrap_or_default() }
    /// The saved record of `rel`, taken for this run (later records replace it).
    pub fn take_saved(&mut self, rel: &str) -> Option<String> { self.saved.remove(rel) }
    /// Whether `rel` is a listed source that is gone (reported, not preserved).
    pub fn is_missing(&self, rel: &str) -> bool { !self.missing.is_empty() && self.seen_missing.contains(rel) }
    /// Whether `rel` has, or had, a record of other content.
    pub fn versioned(&self, rel: &str) -> bool { self.versioned.contains(rel) }

    fn count(&self, table: &str) -> Result<i128, String> {
        self.db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get::<_, i64>(0)).map(|n| n as i128).map_err(sql)
    }

    pub fn summary(&self, complete: bool) -> Result<(), String> {
        let (count, errors) = (self.count("files")?, self.count("errors")?);
        let [stored, source_bytes, changed] = self.totals;
        let data = J::obj().with("source", s(self.source.as_str())).with("reference", s(self.ctx.reference.as_str()))
            .with("total_files", int(self.total as u64)).with("verified_files", J::Int(count)).with("errors", J::Int(errors))
            .with("complete", J::Bool(complete)).with("preservation_workers", int(self.workers as u64))
            .with("safe_to_delete_source", J::Bool(false))
            .with("byte_preservation_complete", J::Bool(complete && count == self.total as i128 && errors == 0))
            .with("bytes_read_this_run", int(self.ctx.budget.bytes())).with("elapsed_seconds", J::Float(self.start.elapsed().as_secs_f64()))
            .with("matching", s("Optional legacy TSV hints, otherwise same relative path; every candidate is byte-verified."))
            .with("note", s("Deletion readiness also requires complete metadata comparison, final identity validation, and inventory coverage. Recovery references require hash-identical reference files."))
            .with("storage_mode", s("standard_vcdiff_and_sha256_references"))
            .with("source_bytes_verified", J::Int(source_bytes)).with("preserved_payload_bytes", J::Int(stored))
            .with("payload_to_verified_original_ratio", if source_bytes != 0 { J::Float(stored as f64 / source_bytes as f64) } else { J::Null })
            .with("byte_different_files", J::Int(changed)).with("missing_sources", int(self.missing.len() as u64));
        atomic_json(&self.ctx.dir.join("summary.json"), &data)?;
        if complete { println!("{}", pyjson::default(&data)); }
        Ok(())
    }

    pub fn tick(&mut self) -> Result<(), String> {
        if self.last.elapsed() > Duration::from_secs(60) { self.summary(false)?; self.last = Instant::now(); }
        Ok(())
    }

    /// The coordinator's handling of one finished file.
    pub fn commit(&mut self, rel: &str, saved: Option<String>, outcome: Outcome) -> Result<(), String> {
        match outcome {
            Outcome::Failed(e) => {
                // An earlier record stays: this run's error is recorded beside it.
                let tx = self.db.transaction().map_err(sql)?;
                if saved.is_none() { tx.execute("DELETE FROM files WHERE path=?", [rel]).map_err(sql)?; }
                tx.execute("INSERT OR REPLACE INTO errors VALUES (?,?)", [rel, e.as_str()]).map_err(sql)?;
                tx.commit().map_err(sql)?;
                self.error_paths.insert(rel.to_string());
                println!("ERROR {rel} {e}");
            }
            outcome => {
                let tx = self.db.transaction().map_err(sql)?;
                if let Outcome::Kept(r) = &outcome {
                    // The earlier content's record is kept as a version, never replaced.
                    if let Some(t) = &saved {
                        tx.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?)", params![rel, label_of(t)?, t]).map_err(sql)?;
                    }
                    tx.execute("INSERT OR REPLACE INTO files VALUES (?,?)", params![rel, pyjson::default(r)]).map_err(sql)?;
                }
                if self.error_paths.contains(rel) { tx.execute("DELETE FROM errors WHERE path=?", [rel]).map_err(sql)?; }
                tx.commit().map_err(sql)?;
                self.error_paths.remove(rel);
                if let Outcome::Kept(r) = &outcome {
                    if let Some(t) = saved { account(&mut self.totals, &t, -1); }
                    account_j(&mut self.totals, r, 1);
                }
            }
        }
        Ok(())
    }

    /// _scan's closing checks, manifest and final summary; true when every listed file is preserved.
    /// `trust_files`: the caller vouches that nothing changes the trees during
    /// the run, so the identities are not taken again.
    pub fn finish(&mut self, trust_files: bool) -> Result<bool, String> {
        if trust_files {
            println!("  final identity check skipped (--trust-files)");
        } else if !self.files.iter().all(|f| os::is_file(f)) { return Err("Source inventory changed".into()); }
        // Identities again for this run's files only: records of earlier runs
        // may name sources a wiped shuttle no longer holds.
        let mine: HashSet<String> = if trust_files { HashSet::new() } else { self.files.iter().map(|f| self.rel(f)).collect() };
        let rows: Vec<(String, String)> = {
            let mut q = self.db.prepare("SELECT path,record FROM files").map_err(sql)?;
            q.query_map([], |r| Ok((r.get(0)?, r.get(1)?))).map_err(sql)?.collect::<Result<Vec<(String, String)>, _>>().map_err(sql)?
                .into_iter().filter(|(rel, _)| mine.contains(rel)).collect()
        };
        let tx = self.db.transaction().map_err(sql)?;
        for (rel, text) in rows {
            let r = pyjson::parse(&text)?;
            let same = |path: &str, key: &str| os::stat(path).is_ok_and(|st| py_eq(&st.identity(), r.get(key).unwrap_or(&J::Null)));
            let source = format!("{}/{rel}", self.source.trim_end_matches('/'));
            let ok = same(&source, "source_identity") && r.get("reference").and_then(J::as_str).is_none_or(|b| same(b, "reference_identity"));
            if !ok { tx.execute("INSERT OR REPLACE INTO errors VALUES (?,?)", [rel.as_str(), "Identity changed before completion"]).map_err(sql)?; }
        }
        tx.commit().map_err(sql)?;
        {
            let path = self.ctx.dir.join("manifest.jsonl");
            let mut f = fs::File::create(&path).map_err(|e| os_error(&e, None))?;
            let mut q = self.db.prepare("SELECT record FROM files ORDER BY path").map_err(sql)?;
            let mut text = String::new();
            for r in q.query_map([], |r| r.get::<_, String>(0)).map_err(sql)? { text.push_str(&r.map_err(sql)?); text.push('\n'); }
            f.write_all(text.as_bytes()).and_then(|_| f.sync_all()).map_err(|e| os_error(&e, None))?;
        }
        self.summary(true)?;
        let stored: HashSet<String> = {
            let mut q = self.db.prepare("SELECT path FROM files").map_err(sql)?;
            q.query_map([], |r| r.get(0)).map_err(sql)?.collect::<Result<_, _>>().map_err(sql)?
        };
        let failed: HashSet<String> = {
            let mut q = self.db.prepare("SELECT path FROM errors").map_err(sql)?;
            q.query_map([], |r| r.get(0)).map_err(sql)?.collect::<Result<_, _>>().map_err(sql)?
        };
        let success = self.files.iter().map(|f| self.rel(f)).all(|rel| stored.contains(&rel) && !failed.contains(&rel))
            && self.missing.is_empty();
        self.db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(())).map_err(sql)?;
        Ok(success)
    }
}

//! dng-compare-all: compare_dng.py --pairs-only and preserve_apo.py scan --delta
//! --pairs in one pass, each pair read from disk once. See docs/native-all.md.

#[path = "dng-compare-all/archive.rs"]
mod archive;
#[path = "dng-compare-all/meta.rs"]
mod meta;
#[path = "dng-compare-all/os.rs"]
mod os;
#[path = "dng-compare-all/pyjson.rs"]
mod pyjson;
#[path = "dng-compare-all/summary.rs"]
mod summary;
#[path = "dng-compare-all/tiff.rs"]
mod tiff;

use archive::{Archive, Outcome, Pre};
use pyjson::{J, canonical, s, thousands};
use rusqlite::{Connection, OpenFlags, types::Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

const VERSION: i128 = 4;
const USAGE: &str = "usage: dng-compare-all LEFT RIGHT --pairs FILE --report DIR [--archive DIR | --compare-only] [--workers N] \
[--batch-size N] [--exiftool-processes N] [--progress-secs S] [--rate-mib R] [--fresh] [--trust-pairs] [--skip-metadata] \
[--left-index LEFT.sqlite --right-index RIGHT.sqlite]";

pub fn hex(bytes: &[u8]) -> String {
    const D: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes { out.push(D[(b >> 4) as usize] as char); out.push(D[(b & 15) as usize] as char); }
    out
}

fn usage_error(msg: &str) -> ! {
    eprintln!("{USAGE}\ndng-compare-all: error: {msg}");
    std::process::exit(2)
}

struct Args {
    left: String, right: String, pairs: String, report: String, archive: Option<String>,
    workers: usize, batch_size: usize, exiftool_processes: usize, progress_secs: f64, rate_mib: f64, fresh: bool, trust_pairs: bool, skip_metadata: bool,
    left_index: Option<String>, right_index: Option<String>,
}

/// Two serialized stamp lists describe unchanged files. The device number is
/// left out: macOS numbers a volume when it is mounted, so a remount would
/// otherwise discard every cached result.
fn same_stamps(a: &str, b: &str) -> bool {
    let tails = |t: &str| match pyjson::parse(t) {
        Ok(J::Arr(sides)) => Some(sides.iter().map(|x| match x {
            J::Arr(v) if !v.is_empty() => v[1..].iter().map(canonical).collect::<Vec<_>>(),
            _ => Vec::new(),
        }).collect::<Vec<_>>()),
        _ => None,
    };
    matches!((tails(a), tails(b)), (Some(x), Some(y)) if x == y)
}

/// A result cached by a --skip-metadata run has no metadata comparison, so a run
/// that compares metadata does not reuse it.
fn reusable(result: &J, skip_metadata: bool) -> bool {
    skip_metadata || result.get("metadata_status").and_then(J::as_str) != Some("skipped")
}

fn parse_args() -> Args {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    let mut positional = Vec::new();
    let mut opts: HashMap<&str, String> = HashMap::new();
    let mut fresh = false;
    let mut compare_only = false;
    let (mut trust_pairs, mut skip_metadata) = (false, false);
    let valued = ["--pairs", "--report", "--archive", "--workers", "--batch-size", "--exiftool-processes", "--progress-secs",
                  "--rate-mib", "--left-index", "--right-index"];
    let mut i = 0;
    while i < raw.len() {
        let a = &raw[i];
        if a == "-h" || a == "--help" { println!("{USAGE}\n\nSee docs/native-all.md."); std::process::exit(0); }
        if a == "--fresh" { fresh = true; i += 1; continue; }
        if a == "--compare-only" { compare_only = true; i += 1; continue; }
        if a == "--trust-pairs" { trust_pairs = true; i += 1; continue; }
        if a == "--skip-metadata" { skip_metadata = true; i += 1; continue; }
        let (name, inline) = match a.split_once('=') { Some((n, v)) if n.starts_with("--") => (n.to_string(), Some(v.to_string())), _ => (a.clone(), None) };
        if let Some(&key) = valued.iter().find(|k| **k == name) {
            let value = match inline { Some(v) => v, None => { i += 1; raw.get(i).cloned().unwrap_or_else(|| usage_error(&format!("argument {key}: expected one argument"))) } };
            opts.insert(key, value);
        } else if a.starts_with("--") {
            usage_error(&format!("unrecognized arguments: {a}"));
        } else {
            positional.push(a.clone());
        }
        i += 1;
    }
    let mut missing: Vec<&str> = Vec::new();
    if positional.is_empty() { missing.push("left"); }
    if positional.len() < 2 { missing.push("right"); }
    for k in ["--pairs", "--report"] { if !opts.contains_key(k) { missing.push(k); } }
    if !missing.is_empty() { usage_error(&format!("the following arguments are required: {}", missing.join(", "))); }
    if positional.len() > 2 { usage_error(&format!("unrecognized arguments: {}", positional[2..].join(" "))); }
    let int = |k: &str, d: i64| -> i64 { opts.get(k).map_or(d, |v| v.trim().parse().unwrap_or_else(|_| usage_error(&format!("argument {k}: invalid int value: '{v}'")))) };
    let float = |k: &str, d: f64| -> f64 { opts.get(k).map_or(d, |v| v.trim().parse().unwrap_or_else(|_| usage_error(&format!("argument {k}: invalid float value: '{v}'")))) };
    let (workers, batch, procs) = (int("--workers", 2), int("--batch-size", 32), int("--exiftool-processes", 1));
    let (progress, rate) = (float("--progress-secs", 0.0), float("--rate-mib", 200.0));
    if workers < 1 || batch < 1 { usage_error("workers, batch-size, limit and sample must be positive"); }
    if procs < 1 { usage_error("exiftool-processes must be positive"); }
    if progress < 0.0 { usage_error("progress-secs must not be negative"); }
    let (li, ri) = (opts.get("--left-index").cloned(), opts.get("--right-index").cloned());
    if li.is_some() != ri.is_some() { usage_error("--left-index and --right-index go together"); }
    if let (Some(l), Some(r)) = (&li, &ri) && !(os::is_file(l) && os::is_file(r)) { usage_error("phindex index not found"); }
    if compare_only && opts.contains_key("--archive") { usage_error("--archive and --compare-only exclude each other"); }
    // Preservation is the default: without --archive the archive goes in REPORT/archive.
    // The byte section by the shared rule (archive::byte_section): ARCHIVE/bytes,
    // a legacy archive's top, or ARCHIVE/bytes given directly.
    let archive = (!compare_only).then(|| archive::byte_section(&opts.get("--archive").cloned()
        .unwrap_or_else(|| std::path::Path::new(&opts["--report"]).join("archive").to_string_lossy().into_owned())));
    if archive.is_some() && rate <= 0.0 { usage_error("Rate must be positive"); }
    Args { left: positional[0].clone(), right: positional[1].clone(), pairs: opts["--pairs"].clone(), report: opts["--report"].clone(),
           archive, workers: workers as usize, batch_size: batch as usize,
           exiftool_processes: procs as usize, progress_secs: progress, rate_mib: rate, fresh, trust_pairs, skip_metadata, left_index: li, right_index: ri }
}

fn on_path(name: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|p| std::env::split_paths(&p).any(|d| {
        use std::os::unix::fs::PermissionsExt;
        fs::metadata(d.join(name)).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    }))
}

// ---------------------------------------------------------------- pairs

fn read_pairs(path: &str) -> Result<Vec<(String, String)>, String> {
    let bytes = fs::read(path).map_err(|e| os::os_error(&e, Some(path)))?;
    let text = String::from_utf8(bytes).map_err(|e| format!("'utf-8' codec can't decode byte in position {}", e.utf8_error().valid_up_to()))?;
    let mut pairs = Vec::new();
    for (number, line) in text.replace("\r\n", "\n").replace('\r', "\n").split('\n').enumerate() {
        if line.trim().is_empty() || line.trim_start().starts_with('#') { continue; }
        let fields: Vec<&str> = line.split('\t').collect();
        if fields.len() != 2 || fields.iter().any(|f| f.is_empty()) { return Err(format!("{path}:{}: expected LEFT<TAB>RIGHT", number + 1)); }
        pairs.push((fields[0].to_string(), fields[1].to_string()));
    }
    Ok(pairs)
}

fn join(root: &str, name: &str) -> String {
    let joined = if name.starts_with('/') { name.to_string() } else if root.ends_with('/') { format!("{root}{name}") } else { format!("{root}/{name}") };
    let (dir, base) = joined.rsplit_once('/').unwrap();
    let dir = os::realpath(if dir.is_empty() { "/" } else { dir });
    if dir == "/" { format!("/{base}") } else { format!("{dir}/{base}") }
}

/// An insertion-ordered map of name -> paths, as compare_dng's inventories.
#[derive(Default)]
struct Found { names: Vec<String>, paths: HashMap<String, Vec<String>> }
impl Found {
    fn add(&mut self, name: &str, path: &str) {
        let v = self.paths.entry(name.to_string()).or_insert_with(|| { self.names.push(name.to_string()); Vec::new() });
        if !v.iter().any(|p| p == path) { v.push(path.to_string()); }
    }
    fn count(&self) -> usize { self.paths.values().map(Vec::len).sum() }
}

/// listed_inventory + explicit_pairs + match for --pairs-only.
fn resolve_pairs(listed: &[(String, String)], roots: &[String; 2], trust: bool) -> Result<(Vec<(String, String)>, Found, Found, usize), String> {
    let paths: Vec<[String; 2]> = listed.iter().map(|(a, b)| [join(&roots[0], a), join(&roots[1], b)]).collect();
    let all: Vec<&String> = paths.iter().flatten().collect();
    // --trust-pairs: the list was checked before; a file gone since fails when it is read.
    let exists: Vec<bool> = if trust { vec![true; all.len()] } else {
        let next = AtomicUsize::new(0);
        let out = Mutex::new(vec![false; all.len()]);
        std::thread::scope(|scope| {
            for _ in 0..16 {
                scope.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= all.len() { break; }
                    let ok = os::is_file(all[i]);
                    out.lock().unwrap()[i] = ok;
                });
            }
        });
        out.into_inner().unwrap()
    };
    let (mut left, mut right) = (Found::default(), Found::default());
    for (k, (names, pair)) in listed.iter().zip(&paths).enumerate() {
        for (side, (name, path)) in [&names.0, &names.1].into_iter().zip(pair).enumerate() {
            if !(path.to_ascii_lowercase().ends_with(".dng") && exists[2 * k + side]) {
                if path.to_ascii_lowercase().ends_with(".dng") && fs::symlink_metadata(path).is_err() {
                    return Err(format!("{name}: missing under {}", roots[side]));
                }
                return Err(format!("{name}: not a DNG under {}", roots[side]));
            }
            let base = path.rsplit('/').next().unwrap();
            if side == 0 { left.add(base, path) } else { right.add(base, path) }
        }
    }
    let mut explicit: HashMap<&str, Vec<&str>> = HashMap::new();
    for [a, b] in &paths {
        let v = explicit.entry(a.as_str()).or_default();
        if !v.contains(&b.as_str()) { v.push(b.as_str()); }
    }
    let explicit_count = explicit.values().map(Vec::len).sum();
    let mut names = left.names.clone();
    names.sort();
    let mut pairs = Vec::new();
    for name in names {
        for source in &left.paths[&name] {
            for target in &explicit[source.as_str()] { pairs.push((source.clone(), target.to_string())); }
        }
    }
    Ok((pairs, left, right, explicit_count))
}

// ---------------------------------------------------------------- phindex

pub struct Indexes { dbs: [Mutex<Connection>; 2] }

impl Indexes {
    fn open(left: &str, right: &str) -> Result<Indexes, String> {
        let one = |p: &str| -> Result<Mutex<Connection>, String> {
            let uri = format!("file:{}?immutable=1", os::resolve(p));
            Connection::open_with_flags(uri, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_NO_MUTEX)
                .map(Mutex::new).map_err(|e| e.to_string())
        };
        Ok(Indexes { dbs: [one(left)?, one(right)?] })
    }

    fn row(&self, side: usize, sql: &str, path: &str, size: u64, mtime_ns: i128) -> Option<Vec<Value>> {
        let db = self.dbs[side].lock().unwrap();
        let row: Vec<Value> = db.query_row(sql, [path], |r| (0..r.as_ref().column_count()).map(|i| r.get::<_, Value>(i)).collect()).ok()?;
        let num = |v: &Value, want: i128| match v { Value::Integer(i) => *i as i128 == want, Value::Real(f) => *f == want as f64, _ => false };
        (truthy(&row[2]) && num(&row[0], size as i128) && num(&row[1], mtime_ns)).then_some(row)
    }

    /// preserve_apo.indexed_sha256
    pub fn sha256(&self, side: usize, path: &str, st: &os::Stat) -> Option<String> {
        let row = self.row(side, "SELECT size, mtime_ns, sha256 FROM files WHERE path=? AND gone=0", path, st.size, st.mtime_ns)?;
        match &row[2] { Value::Text(t) => Some(t.clone()), _ => None }
    }

    /// compare_dng.PhindexIndexes.settle
    fn settle(&self, pair: &(String, String), stamps: &J, compare_only: bool) -> Option<J> {
        let st = stamps.as_arr();
        let rows: Vec<Option<Vec<Value>>> = [&pair.0, &pair.1].iter().enumerate().map(|(i, p)| {
            let s = st.get(i)?.as_arr();
            self.row(i, "SELECT size, mtime_ns, sha256, dng_blocks FROM files WHERE path=? AND gone=0", p,
                     s.get(2)?.as_i128()? as u64, s.get(3)?.as_i128()?)
        }).collect();
        let (Some(a), Some(b)) = (&rows[0], &rows[1]) else { return None };
        let result = J::obj().with("left", s(pair.0.as_str())).with("right", s(pair.1.as_str())).with("stamps", stamps.clone());
        if a[2] == b[2] {
            let settled = J::obj().with("status", s("equal")).with("method", s("phindex_sha256"));
            return Some(result.with("raw", settled.clone()).with("auxiliary", settled));
        }
        if compare_only && truthy(&a[3]) && a[3] == b[3] {
            return Some(result.with("raw", J::obj().with("status", s("equal")).with("method", s("phindex_dng_blocks")))
                .with("auxiliary", J::obj().with("status", s("not_compared"))));
        }
        None
    }
}

fn truthy(v: &Value) -> bool {
    match v { Value::Null => false, Value::Integer(i) => *i != 0, Value::Real(f) => *f != 0.0, Value::Text(t) => !t.is_empty(), Value::Blob(b) => !b.is_empty() }
}

// ---------------------------------------------------------------- pixel comparison

struct Decoder { python: PathBuf, home: PathBuf }

impl Decoder {
    fn locate() -> Decoder {
        let exe = std::env::current_exe().ok().and_then(|p| fs::canonicalize(p).ok());
        let mut home = exe.as_deref().and_then(|p| p.ancestors().find(|d| d.join("compare_dng.py").is_file()).map(Path::to_path_buf))
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join(".."));
        if let Ok(h) = fs::canonicalize(&home) { home = h; }
        let python = std::env::var_os("DNG_COMPARE_PYTHON").map(PathBuf::from).unwrap_or_else(|| home.join(".venv/bin/python"));
        Decoder { python, home }
    }

    /// compare_dng.decoded_status_isolated
    fn status(&self, left: &str, right: &str, group: &str, index: usize) -> (J, J) {
        use std::os::unix::process::ExitStatusExt;
        let unverified = |e: String| (s("unverified"), J::Str(e));
        let code = "import sys, compare_dng; compare_dng.decode_child(*sys.argv[1:4], int(sys.argv[4]))";
        let child = Command::new(&self.python).args(["-c", code, left, right, group, &index.to_string()]).current_dir(&self.home)
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn();
        let mut child = match child { Ok(c) => c, Err(e) => return unverified(format!("decoder failed: {}", os::os_error(&e, None))) };
        let take = |r: Option<Box<dyn Read + Send>>| std::thread::spawn(move || { let mut v = Vec::new(); if let Some(mut r) = r { let _ = r.read_to_end(&mut v); } v });
        let out = take(child.stdout.take().map(|x| Box::new(x) as Box<dyn Read + Send>));
        let err = take(child.stderr.take().map(|x| Box::new(x) as Box<dyn Read + Send>));
        let started = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(st)) => break st,
                Ok(None) if started.elapsed() > Duration::from_secs(1800) => {
                    let _ = child.kill(); let _ = child.wait(); let _ = out.join(); let _ = err.join();
                    return unverified("decoder timed out".into());
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(5)),
                Err(e) => return unverified(format!("decoder failed: {e}")),
            }
        };
        let (stdout, stderr) = (out.join().unwrap_or_default(), err.join().unwrap_or_default());
        if let Some(sig) = status.signal() { return unverified(format!("decoder crashed (signal {sig})")); }
        let stdout = String::from_utf8_lossy(&stdout);
        if let Some(line) = stdout.trim().lines().last()
            && let Ok(J::Arr(v)) = pyjson::parse(line) && v.len() == 2 {
            return (v[0].clone(), v[1].clone());
        }
        let stderr = String::from_utf8_lossy(&stderr);
        let tail: Vec<char> = stderr.trim().chars().collect();
        unverified(format!("decoder failed: {}", tail[tail.len().saturating_sub(500)..].iter().collect::<String>()))
    }
}

fn shape(v: &[i128]) -> J { J::Arr(v.iter().map(|x| J::Int(*x)).collect()) }

fn group_status(images: &[J]) -> &'static str {
    let st: Vec<&J> = images.iter().filter_map(|r| r.get("status")).collect();
    if images.is_empty() || st.iter().any(|x| x.as_str() == Some("unverified")) { "unverified" }
    else if st.iter().any(|x| x.as_str() == Some("different")) { "different" } else { "equal" }
}

struct Side<'a> { path: &'a str, stat: os::Stat, data: &'a [u8] }

/// compare_dng.compare_pixels from bytes already read; `before` and the data
/// carry the stat and read errors of each file.
fn compare_pixels(left: &str, right: &str, before: Result<(os::Stat, os::Stat), String>,
                  data: [Result<&[u8], String>; 2], decoder: &Decoder) -> J {
    let mut result = J::obj().with("left", s(left)).with("right", s(right));
    let run = |result: &mut J| -> Result<(), String> {
        let (sl, sr) = before?;
        let [dl, dr] = data;
        let (a, b) = (Side { path: left, stat: sl, data: dl? }, Side { path: right, stat: sr, data: dr? });
        let ta = tiff::open(a.data)?;
        let tb = tiff::open(b.data)?;
        let page0 = |t: &tiff::Tiff| -> Result<bool, String> { t.page0.as_ref().map(|p| p.has(50706)).ok_or_else(|| "index 0 out of range(0)".to_string()) };
        if !page0(&ta)? || !page0(&tb)? { return Err("Missing DNGVersion tag".into()); }
        let ga = ta.page_groups()?;
        let gb = tb.page_groups()?;
        let same = a.data == b.data && a.stat.size == b.stat.size;
        for (name, pa, pb) in [("raw", &ga.0, &gb.0), ("auxiliary", &ga.1, &gb.1)] {
            if pa.len() != pb.len() {
                result.set(name, J::obj().with("status", s("unverified")).with("error", s("Image count differs"))
                    .with("left_count", J::Int(pa.len() as i128)).with("right_count", J::Int(pb.len() as i128)));
                continue;
            }
            let mut images = Vec::new();
            for (i, (x, y)) in pa.iter().zip(pb).enumerate() {
                let mut r = J::obj().with("left_shape", shape(&x.shape)).with("right_shape", shape(&y.shape));
                // Both digests at once; the left one's error comes first, as in Python.
                let (da, db) = std::thread::scope(|scope| {
                    let right = (!same).then(|| scope.spawn(|| tiff::payload_digest(b.data, y, b.stat.size)));
                    let da = tiff::payload_digest(a.data, x, a.stat.size);
                    (da, right.map(|h| h.join().unwrap()))
                });
                let (da, na) = da?;
                let (db, nb) = match db { Some(r) => r?, None => (da.clone(), na) };
                let equal = da == db && (same || x.signature(ta.format) == y.signature(tb.format));
                r.set("left_sha256", s(da)); r.set("right_sha256", s(db)); r.set("bytes_read", J::Int((na + nb) as i128));
                if equal {
                    r.set("status", s("equal")); r.set("method", s("encoded_blocks"));
                } else {
                    let (status, error) = decoder.status(a.path, b.path, name, i);
                    let failed = !matches!(error, J::Null);
                    r.set("status", status);
                    r.set("method", s(if failed { "decode_failed" } else { "decoded_samples" }));
                    if failed { r.set("error", error); }
                }
                images.push(r);
            }
            let status = if !images.is_empty() || name == "raw" { group_status(&images) } else { "equal" };
            result.set(name, J::obj().with("status", s(status)).with("images", J::Arr(images)));
        }
        if [os::stat(left)?, os::stat(right)?] != [sl, sr] { return Err("File changed during pixel comparison".into()); }
        result.set("stamps", J::Arr(vec![sl.stamp(), sr.stamp()]));
        Ok(())
    };
    if let Err(e) = run(&mut result) {
        result.set("raw", J::obj().with("status", s("unverified")).with("error", s(e.as_str())));
        result.set("error", s(e));
    }
    result
}

// ---------------------------------------------------------------- one pair

struct PresTask { rel: String, saved: Option<String>, versioned: bool }

struct Task { left: String, right: String, compare: bool, pres: Option<PresTask> }

struct Done { compare: Option<J>, pres: Option<(String, Option<String>, Outcome)> }

fn read_file(path: &str) -> Result<Vec<u8>, String> {
    let mut f = fs::File::open(path).map_err(|e| os::os_error(&e, Some(path)))?;
    let mut v = Vec::with_capacity(f.metadata().map(|m| m.len() as usize).unwrap_or(0));
    f.read_to_end(&mut v).map_err(|e| os::os_error(&e, None))?;
    Ok(v)
}

fn sha256_hex(data: &[u8]) -> String { hex(&Sha256::digest(data)) }

fn job(t: Task, ctx: Option<&archive::Ctx>, indexes: Option<&Indexes>, decoder: &Decoder) -> Done {
    let mut pres_out = None;
    let mut pres = None;
    if let (Some(p), Some(ctx)) = (t.pres, ctx) {
        let valid = match &p.saved { Some(text) => archive::saved_valid(ctx, text, &t.left), None => Ok(false) };
        match valid {
            Ok(true) => pres_out = Some((p.rel, p.saved, Outcome::Unchanged)),
            Ok(false) => match archive::pre(&t.left, &t.right) {
                Ok(pre) => {
                    match indexes.map(|ix| ctx.unwrap_indexed(ix, &p.rel, &t.left, &pre)) {
                        Some(Ok(Some(r))) => pres_out = Some((p.rel, p.saved, Outcome::Kept(r))),
                        Some(Err(e)) => pres_out = Some((p.rel, p.saved, Outcome::Failed(e))),
                        _ => pres = Some((p, pre)),
                    }
                }
                Err(e) => pres_out = Some((p.rel, p.saved, Outcome::Failed(e))),
            },
            Err(e) => pres_out = Some((p.rel, p.saved, Outcome::Failed(e))),
        }
    }
    if !t.compare && pres.is_none() { return Done { compare: None, pres: pres_out }; }
    let before = if t.compare { Some(os::stat(&t.left).and_then(|a| Ok((a, os::stat(&t.right)?)))) } else { None };
    if let (Some((_, pre)), Some(ctx)) = (&pres, ctx) {
        ctx.budget.charge(pre.before.size + pre.prior.map_or(0, |p| p.size));
    }
    let need_right = t.compare || pres.as_ref().is_some_and(|(_, pre)| pre.base.is_some());
    let dl = read_file(&t.left);
    let dr = if need_right { read_file(&t.right) } else { Err(String::new()) };
    let mut compare = None;
    std::thread::scope(|scope| {
        let hashes = pres.as_ref().map(|(_, pre)| {
            let (dl, dr) = (&dl, &dr);
            let base = pre.base.is_some();
            let identical = matches!((dl, dr), (Ok(x), Ok(y)) if x == y);
            let a = scope.spawn(move || dl.as_ref().ok().map(|d| sha256_hex(d)));
            let b = (base && !identical).then(|| scope.spawn(move || dr.as_ref().ok().map(|d| sha256_hex(d))));
            (a, b, base)
        });
        if let Some(before) = before {
            let data = [dl.as_deref().map_err(Clone::clone), dr.as_deref().map_err(Clone::clone)];
            compare = Some(compare_pixels(&t.left, &t.right, before, data, decoder));
        }
        if let (Some((p, pre)), Some(h), Some(ctx)) = (&pres, hashes, ctx) {
            let (a, b, base) = h;
            let ha = a.join().unwrap();
            let hb = match b { Some(b) => b.join().unwrap(), None if base => ha.clone(), None => None };
            let outcome = (|| -> Result<J, String> {
                let a = dl.as_deref().map_err(Clone::clone)?;
                let b = if pre.base.is_some() { Some(dr.as_deref().map_err(Clone::clone)?) } else { None };
                ctx.preserve(&p.rel, &t.left, pre, a, b, ha.as_deref().unwrap_or(""), hb.as_deref(), p.versioned)
            })();
            pres_out = Some((p.rel.clone(), p.saved.clone(), match outcome { Ok(r) => Outcome::Kept(r), Err(e) => Outcome::Failed(e) }));
        }
    });
    Done { compare, pres: pres_out }
}

impl archive::Ctx {
    fn unwrap_indexed(&self, ix: &Indexes, rel: &str, source: &str, pre: &Pre) -> Result<Option<J>, String> { self.indexed(ix, rel, source, pre) }
}

// ---------------------------------------------------------------- main

fn main() {
    std::process::exit(run());
}

fn run() -> i32 {
    let args = parse_args();
    if !args.skip_metadata && !on_path("exiftool") { usage_error("ExifTool is required: brew install exiftool"); }
    let roots = [os::resolve(&args.left), os::resolve(&args.right)];
    if !roots.iter().all(|r| Path::new(r).is_dir()) { usage_error("Both roots must be readable directories"); }
    let report = PathBuf::from(&args.report);
    if let Err(e) = fs::create_dir_all(&report) { usage_error(&os::os_error(&e, Some(&args.report))); }
    let _lock = match os::try_lock(&report.join(".lock"), true) {
        Ok(Some(l)) => l,
        Ok(None) => usage_error("Another comparison is already using this output directory"),
        Err(e) => usage_error(&os::os_error(&e, None)),
    };
    os::catch_sigint();
    let started = Instant::now();
    let listed = read_pairs(&args.pairs).unwrap_or_else(|e| usage_error(&e));
    if listed.is_empty() { usage_error("--pairs-only needs --pair or --pairs"); }
    println!("dng-compare-all {}\n{} the {} listed pairs...", env!("DNG_COMPARE_COMMIT"),
             if args.trust_pairs { "Trusting" } else { "Checking" }, thousands(listed.len() as i128));
    let (pairs, left, right, explicit) = resolve_pairs(&listed, &roots, args.trust_pairs).unwrap_or_else(|e| usage_error(&e));
    let total = pairs.len();
    let mut context = J::obj().with("version", J::Int(VERSION))
        .with("roots", J::Arr(roots.iter().map(|r| s(r.as_str())).collect())).with("matched_pairs", J::Int(total as i128))
        .with("explicit_pairs", J::Int(explicit as i128)).with("pairs_only", J::Bool(true))
        .with("left_files", J::Int(left.count() as i128)).with("right_files", J::Int(right.count() as i128))
        .with("duplicate_names", J::Int(0)).with("left_only_names", J::Int(0))
        .with("direction", s("left must be contained in right; extra right files are ignored"));
    let write = |name: &str, text: String| fs::write(report.join(name), text).unwrap_or_else(|e| usage_error(&os::os_error(&e, None)));
    write("unmatched.json", r#"{"duplicate_candidates": [], "left_only": {}}"#.into());
    context.set("selected_pairs", J::Int(total as i128));
    println!("{} candidate pairs; processing {}; duplicate names: 0", thousands(total as i128), thousands(total as i128));
    let indexes = match (&args.left_index, &args.right_index) {
        (Some(l), Some(r)) => Some(Indexes::open(l, r).unwrap_or_else(|e| { eprintln!("dng-compare-all: {e}"); std::process::exit(1) })),
        _ => None,
    };
    // The archive, for the listed pairs in the order given; each left file is
    // preserved once, against the last reference listed for it.
    let mut archive = args.archive.as_ref().map(|dir| {
        println!("Byte section: {dir}");
        let resolved: Vec<(String, String)> = listed.iter().map(|(a, b)| (join(&roots[0], a), join(&roots[1], b))).collect();
        Archive::open(&roots[0], &roots[1], dir, args.rate_mib, args.workers, &resolved).unwrap_or_else(|e| usage_error(&e))
    });
    let mut reference_of: HashMap<String, String> = HashMap::new();
    for (a, b) in &listed { reference_of.insert(join(&roots[0], a), join(&roots[1], b)); }
    let mut preserved: HashSet<String> = HashSet::new();
    let decoder = Decoder::locate();

    let db = Connection::open(report.join("cache.sqlite3")).unwrap_or_else(|e| { eprintln!("dng-compare-all: {e}"); std::process::exit(1) });
    let _ = db.busy_timeout(Duration::from_secs(60));
    let fail = |e: String| -> ! { eprintln!("dng-compare-all: {e}"); std::process::exit(1) };
    db.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, stamps TEXT, result TEXT)", []).unwrap_or_else(|e| fail(e.to_string()));
    let mut summary = summary::Summary::default();
    let mut reused: usize = 0;
    let mut last_progress = 0.0;
    let comparing = Instant::now();
    let saved: usize = if args.fresh { 0 } else { db.query_row("SELECT count(*) FROM results", [], |r| r.get::<_, i64>(0)).unwrap_or(0) as usize };
    let mut out = fs::File::create(report.join("pairs.jsonl")).unwrap_or_else(|e| fail(os::os_error(&e, None)));
    let stamps_of = |pair: &(String, String)| -> Option<J> { Some(J::Arr(vec![os::stat(&pair.0).ok()?.stamp(), os::stat(&pair.1).ok()?.stamp()])) };

    for batch in pairs.chunks(args.batch_size) {
        if os::INTERRUPTED.load(Ordering::SeqCst) { break; }
        let mut results: HashMap<usize, J> = HashMap::new();
        let mut pending: Vec<usize> = Vec::new();
        let mut initial: HashMap<usize, Option<String>> = HashMap::new();
        for (k, pair) in batch.iter().enumerate() {
            let key = canonical(&J::Arr(vec![J::Int(VERSION), s(pair.0.as_str()), s(pair.1.as_str())]));
            let row: Option<(Option<String>, String)> = if args.fresh { None } else {
                db.query_row("SELECT stamps,result FROM results WHERE key=?", [&key], |r| Ok((r.get(0)?, r.get(1)?))).ok()
            };
            let current = stamps_of(pair).map(|j| canonical(&j));
            match row {
                Some((Some(st), text)) if current.as_deref().is_some_and(|c| same_stamps(&st, c))
                    && pyjson::parse(&text).is_ok_and(|r| reusable(&r, args.skip_metadata)) => {
                    results.insert(k, pyjson::parse(&text).unwrap());
                    reused += 1;
                }
                _ => { pending.push(k); initial.insert(k, current); }
            }
        }
        let mut settled: HashMap<usize, J> = HashMap::new();
        if let Some(ix) = &indexes {
            for &k in &pending {
                if let Some(Some(st)) = initial.get(&k)
                    && let Some(r) = ix.settle(&batch[k], &pyjson::parse(st).unwrap(), archive.is_none()) {
                    summary.counts.add(r.get("raw").and_then(|x| x.get("method")).and_then(J::as_str).unwrap(), 1);
                    settled.insert(k, r);
                }
            }
        }
        // Tasks: comparison of pending pairs and preservation of each left file once.
        let mut tasks: Vec<(usize, Task)> = Vec::new();
        for (k, pair) in batch.iter().enumerate() {
            let compare = pending.contains(&k) && !settled.contains_key(&k);
            let pres = match &mut archive {
                Some(a) if reference_of.get(&pair.0) == Some(&pair.1) && !a.is_missing(&a.rel(&pair.0))
                            && preserved.insert(pair.0.clone()) => {
                    let rel = a.rel(&pair.0);
                    let saved = a.take_saved(&rel);
                    let versioned = a.versioned(&rel);
                    Some(PresTask { rel, saved, versioned })
                }
                _ => None,
            };
            if compare || pres.is_some() { tasks.push((k, Task { left: pair.0.clone(), right: pair.1.clone(), compare, pres })); }
        }
        let meta_paths: Vec<String> = pending.iter().flat_map(|&k| [batch[k].0.clone(), batch[k].1.clone()]).collect();
        let ctx = archive.as_ref().map(|a| &a.ctx);
        let next = AtomicUsize::new(0);
        let slots: Vec<Mutex<Option<Task>>> = tasks.iter_mut().map(|(_, t)| Mutex::new(Some(std::mem::replace(t,
            Task { left: String::new(), right: String::new(), compare: false, pres: None })))).collect();
        let done: Vec<Mutex<Option<Done>>> = slots.iter().map(|_| Mutex::new(None)).collect();
        let meta = std::thread::scope(|scope| {
            for _ in 0..args.workers.min(slots.len()) {
                scope.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= slots.len() { break; }
                    let t = slots[i].lock().unwrap().take().unwrap();
                    *done[i].lock().unwrap() = Some(job(t, ctx, indexes.as_ref(), &decoder));
                });
            }
            if meta_paths.is_empty() || args.skip_metadata { Ok(HashMap::new()) } else { meta::metadata_parallel(&meta_paths, args.exiftool_processes) }
        });
        if os::INTERRUPTED.load(Ordering::SeqCst) { break; }
        let (meta, metadata_error) = match meta { Ok(m) => (m, None), Err(e) => (HashMap::new(), Some(e)) };
        let mut compared: HashMap<usize, J> = settled;
        for ((k, _), d) in tasks.iter().zip(done) {
            let d = d.into_inner().unwrap().unwrap();
            if let Some(r) = d.compare { compared.insert(*k, r); }
            if let (Some((rel, saved, outcome)), Some(a)) = (d.pres, archive.as_mut()) {
                a.commit(&rel, saved, outcome).unwrap_or_else(|e| fail(e));
            }
        }
        db.execute_batch("BEGIN").unwrap_or_else(|e| fail(e.to_string()));
        for &k in &pending {
            let pair = &batch[k];
            let mut result = compared.remove(&k).unwrap();
            let none = metadata_error.clone().unwrap_or_else(|| "ExifTool returned no record".into());
            let (a, b) = (meta.get(&pair.0), meta.get(&pair.1));
            match (a, b) {
                _ if args.skip_metadata => result.set("metadata_status", s("skipped")),
                (Some(meta::Meta::Tags { tags: ta, warnings: wa }), Some(meta::Meta::Tags { tags: tb, warnings: wb })) => {
                    result.set("metadata_status", s("ok"));
                    result.set("differences", meta::compare_metadata(ta, tb));
                    result.set("metadata_warnings", J::Arr(wa.iter().chain(wb).map(|w| s(w.as_str())).collect()));
                }
                _ => {
                    let err = |m: Option<&meta::Meta>| match m { Some(meta::Meta::Error(e)) => s(e.as_str()), Some(_) => J::Null, None => s(none.as_str()) };
                    result.set("metadata_status", s("error"));
                    result.set("metadata_error", J::Arr(vec![err(a), err(b)]));
                }
            }
            if let Some(st) = result.get("stamps").cloned() {
                let changed = match (os::stat(&pair.0), os::stat(&pair.1)) {
                    (Ok(x), Ok(y)) => {
                        let now = J::Arr(vec![x.stamp(), y.stamp()]);
                        (!pyjson::py_eq(&st, &now) || Some(canonical(&st)) != initial[&k]).then(|| "File changed or was unreadable during comparison".to_string())
                    }
                    (Err(e), _) | (_, Err(e)) => Some(e),
                };
                if let Some(e) = changed {
                    result.set("error", s(e));
                    if let Some(raw) = result.get_mut("raw") { raw.set("status", s("unverified")); }
                    result.set("metadata_status", s("error"));
                }
            }
            let status = |g: &str| result.get(g).and_then(|x| x.get("status")).and_then(J::as_str).map(str::to_string);
            let metadata_done = match result.get("metadata_status").and_then(J::as_str) {
                Some("ok") => true,
                Some("skipped") => args.skip_metadata,
                _ => false,
            };
            if !result.get("error").is_some_and(J::truthy) && metadata_done
                && status("raw").as_deref() != Some("unverified") && status("auxiliary").as_deref() != Some("unverified") {
                let key = canonical(&J::Arr(vec![J::Int(VERSION), s(pair.0.as_str()), s(pair.1.as_str())]));
                db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?)",
                           [key, canonical(result.get("stamps").unwrap_or(&J::Null)), canonical(&result)]).unwrap_or_else(|e| fail(e.to_string()));
            }
            results.insert(k, result);
        }
        let mut lines = String::new();
        for k in 0..batch.len() {
            let r = &results[&k];
            lines.push_str(&canonical(r)); lines.push('\n');
            summary.add(r);
        }
        out.write_all(lines.as_bytes()).and_then(|_| out.flush()).unwrap_or_else(|e| fail(os::os_error(&e, None)));
        db.execute_batch("COMMIT").unwrap_or_else(|e| fail(e.to_string()));
        let elapsed = started.elapsed().as_secs_f64();
        context.set("cached_pairs_reused", J::Int(reused as i128));
        summary.write(&report.join("summary.json"), &context, left.count(), false, elapsed).unwrap_or_else(|e| fail(os::os_error(&e, None)));
        if let Some(a) = archive.as_mut() { a.tick().unwrap_or_else(|e| fail(e)); }
        let done = summary.counts.get("pairs_reported") as usize;
        if done < pairs.len() && elapsed - last_progress < args.progress_secs { continue; }
        last_progress = elapsed;
        let spent = comparing.elapsed().as_secs_f64();
        let rate = if spent > 0.0 { (done - reused) as f64 / spent } else { 0.0 };
        let to_read = pairs.len().saturating_sub(done).saturating_sub(saved.saturating_sub(reused));
        let eta = if rate > 0.0 { to_read as f64 / rate } else { 0.0 };
        let tail = if args.progress_secs == 0.0 { String::new() } else if rate > 0.0 {
            format!(" | {rate:.1} pairs/s, ETA {}h{:02}m", (eta / 3600.0).floor() as i64, (eta % 3600.0 / 60.0).floor() as i64)
        } else { " | reusing saved results".into() };
        println!("{}/{} | raw equal={} different={} unverified={} | {elapsed:.1}s | cached={}{tail}", thousands(done as i128),
                 thousands(pairs.len() as i128), thousands(summary.counts.get("raw_equal")), thousands(summary.counts.get("raw_different")),
                 thousands(summary.counts.get("raw_unverified")), thousands(reused as i128));
    }
    drop(db);
    if os::INTERRUPTED.load(Ordering::SeqCst) {
        eprintln!("Interrupted; rerun the same command to resume from the cache.");
        return 130;
    }
    summary.write(&report.join("summary.json"), &context, left.count(), true, started.elapsed().as_secs_f64())
        .unwrap_or_else(|e| fail(os::os_error(&e, None)));
    let mut uncovered = Vec::new();
    for name in &left.names { for p in &left.paths[name] { if !summary.covered_raw(p) { uncovered.push(s(p.as_str())); } } }
    let n_uncovered = uncovered.len();
    write("not-confirmed.json", pyjson::indent(&J::Arr(uncovered)) + "\n");
    println!("Raw pixels preserved for {}/{} left files.", thousands((left.count() - n_uncovered) as i128), thousands(left.count() as i128));
    if let Some(a) = archive.as_mut() {
        match a.finish() {
            Ok(true) => {}
            Ok(false) => eprintln!("Preservation incomplete; inspect errors and rerun before deleting source"),
            Err(e) => { eprintln!("dng-compare-all: {e}"); return 1; }
        }
    }
    if n_uncovered == 0 { 0 } else { 1 }
}

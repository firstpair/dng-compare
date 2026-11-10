//! dngc-skeleton: the skeleton section of a dngc archive (docs/dngc-archive-spec.md, section 13).
//!
//!   dngc-skeleton build ARCHIVE [--workers N] [--limit N]
//!       a skeleton for every reference file the archive's records name, so
//!       the records still rebuild after those files are edited
//!   dngc-skeleton pair OLD NEW
//!       skeleton of OLD, rebuilt from NEW's blocks, compared with OLD's SHA-256
//!   dngc-skeleton info ARCHIVE

use dng_archive::pyjson::{self, J};
use dng_archive::skeleton::{self, Store};
use dng_archive::{archive, os, sections};
use rusqlite::{Connection, OpenFlags};
use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

struct Job { path: String, sha256: String, size: i128, mtime_ns: i128 }
enum Done { Built { sha256: String, record: String, packed: Vec<u8>, raw: u64 }, Skipped { reason: &'static str, path: String, detail: String } }

fn work(job: Job) -> Done {
    let skip = |reason, detail: String| Done::Skipped { reason, path: job.path.clone(), detail };
    let st = match os::stat(&job.path) { Ok(st) => st, Err(e) => return skip("missing", e) };
    if st.size as i128 != job.size || st.mtime_ns != job.mtime_ns { return skip("changed", "size or modification time differs from the record".into()); }
    let sk = match skeleton::build(Path::new(&job.path)) {
        Ok(sk) => sk,
        Err(e) if e.starts_with("no skeleton walker") => return skip("no-walker", e),
        Err(e) => return skip("error", e),
    };
    // A whole-file skeleton was read in full: check it is the content the record names.
    if sk.blocks.is_empty() && dng_archive::hex(&<sha2::Sha256 as sha2::Digest>::digest(&sk.bytes)) != job.sha256 {
        return skip("changed", "bytes differ from the record".into());
    }
    match Store::encode(&job.sha256, &job.path, st.identity(), &sk) {
        Ok((record, packed)) => Done::Built { sha256: job.sha256, record, packed, raw: sk.bytes.len() as u64 },
        Err(e) => skip("error", e),
    }
}

fn build(top: &Path, workers: usize, limit: Option<u64>) -> Result<i32, String> {
    let (top, bytes, _) = sections(top);
    let recovery = bytes.join("recovery.sqlite3");
    if !recovery.exists() { return Err(format!("no byte section in {}", top.display())); }
    let store = Store::create(&top)?;
    let _lock = os::try_lock(&top.join("skeletons/.lock"), true).map_err(|e| e.to_string())?
        .ok_or("another dngc-skeleton is building this archive's skeletons")?;
    let total: u64 = {
        let db = Connection::open_with_flags(format!("file:{}?immutable=1", recovery.display()), OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI).map_err(|e| e.to_string())?;
        db.query_row("SELECT count(*) FROM files", [], |r| r.get::<_, i64>(0)).map_err(|e| e.to_string())? as u64
    };
    println!("{} records; skeletons in {}", pyjson::thousands(total as i128), Store::path(&top).display());
    let (job_tx, job_rx) = mpsc::sync_channel::<Job>(256);
    let (done_tx, done_rx) = mpsc::sync_channel::<Option<Done>>(256);
    let job_rx = Arc::new(Mutex::new(job_rx));
    // The lister: each record's base content, unless it already has a skeleton.
    let store_path = Store::path(&top);
    let lister_tx = done_tx.clone();
    let lister = std::thread::spawn(move || -> Result<(), String> {
        let e = |e: rusqlite::Error| e.to_string();
        let db = Connection::open_with_flags(format!("file:{}?immutable=1", recovery.display()), OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI).map_err(e)?;
        let have = Connection::open_with_flags(&store_path, OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(e)?;
        let mut known = have.prepare("SELECT 1 FROM skeletons WHERE sha256=?1").map_err(e)?;
        let mut st = db.prepare("SELECT json_extract(record,'$.reference'),
                                        CASE WHEN json_extract(record,'$.delta') IS NOT NULL THEN json_extract(record,'$.reference_sha256') ELSE json_extract(record,'$.source_sha256') END,
                                        json_extract(record,'$.reference_identity[1]'), json_extract(record,'$.reference_identity[2]'),
                                        json_extract(record,'$.delta') IS NULL AND json_extract(record,'$.original') IS NOT NULL
                                 FROM files ORDER BY path").map_err(e)?;
        let mut rows = st.query([]).map_err(e)?;
        let mut sent = 0u64;
        while let Some(r) = rows.next().map_err(e)? {
            if os::INTERRUPTED.load(std::sync::atomic::Ordering::SeqCst) { break; }
            let full: bool = r.get(4).map_err(e)?;
            let (path, sha256): (Option<String>, Option<String>) = (r.get(0).map_err(e)?, r.get(1).map_err(e)?);
            let (Some(path), Some(sha256), false) = (path, sha256, full) else { let _ = lister_tx.send(None); continue };
            if known.exists([&sha256]).map_err(e)? { let _ = lister_tx.send(None); continue; }
            let size: i64 = r.get(2).unwrap_or(-1);
            let mtime_ns: i64 = r.get(3).unwrap_or(-1);
            if job_tx.send(Job { path, sha256, size: size as i128, mtime_ns: mtime_ns as i128 }).is_err() { break; }
            sent += 1;
            if limit.is_some_and(|l| sent >= l) { break; }
        }
        Ok(())
    });
    let pool: Vec<_> = (0..workers.max(1)).map(|_| {
        let (rx, tx) = (job_rx.clone(), done_tx.clone());
        std::thread::spawn(move || loop {
            let job = rx.lock().unwrap().recv();
            match job { Ok(job) => { if tx.send(Some(work(job))).is_err() { break; } } Err(_) => break }
        })
    }).collect();
    drop(done_tx);
    let start = Instant::now();
    let mut last = Instant::now();
    let (mut seen, mut built, mut raw, mut packed_bytes, mut pending) = (0u64, 0u64, 0u64, 0u64, 0u32);
    let mut skipped: BTreeMap<&'static str, u64> = BTreeMap::new();
    let mut log = std::fs::OpenOptions::new().create(true).append(true).open(top.join("skeletons/skipped.tsv")).map_err(|e| e.to_string())?;
    let db = store.connection();
    db.execute_batch("BEGIN").map_err(|e| e.to_string())?;
    for done in done_rx {
        seen += 1;
        match done {
            None => {}
            Some(Done::Built { sha256, record, packed, raw: n }) => {
                store.put(&sha256, &record, &packed)?;
                built += 1; raw += n; packed_bytes += packed.len() as u64; pending += 1;
            }
            Some(Done::Skipped { reason, path, detail }) => {
                *skipped.entry(reason).or_default() += 1;
                writeln!(log, "{reason}\t{path}\t{}", detail.replace(['\t', '\n'], " ")).map_err(|e| e.to_string())?;
            }
        }
        if pending >= 2000 { db.execute_batch("COMMIT; BEGIN").map_err(|e| e.to_string())?; pending = 0; }
        if last.elapsed() >= Duration::from_secs(5) {
            last = Instant::now();
            println!("  {} of {} records, {} skeletons built ({:.1} MB), {} skipped, {:.0} s",
                     pyjson::thousands(seen as i128), pyjson::thousands(total as i128), pyjson::thousands(built as i128),
                     packed_bytes as f64 / 1e6, skipped.values().sum::<u64>(), start.elapsed().as_secs_f64());
        }
    }
    db.execute_batch("COMMIT").map_err(|e| e.to_string())?;
    for t in pool { let _ = t.join(); }
    lister.join().map_err(|_| "the lister failed".to_string())??;
    let interrupted = os::INTERRUPTED.load(std::sync::atomic::Ordering::SeqCst);
    // A finished section is one plain file, readable from read-only media.
    db.execute_batch("PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;").map_err(|e| e.to_string())?;
    println!("{} skeletons built in {:.0} s: {:.1} MB stored for {:.1} MB of skeleton bytes{}",
             pyjson::thousands(built as i128), start.elapsed().as_secs_f64(), packed_bytes as f64 / 1e6, raw as f64 / 1e6,
             if interrupted { " (interrupted; run again to continue)" } else { "" });
    for (reason, n) in &skipped { println!("  skipped, {reason}: {}", pyjson::thousands(*n as i128)); }
    if !skipped.is_empty() { println!("  listed in {}", top.join("skeletons/skipped.tsv").display()); }
    Ok(if interrupted { 130 } else { 0 })
}

fn pair(old: &Path, new: &Path) -> Result<i32, String> {
    let sk = skeleton::build(old)?;
    let packed = zstd::encode_all(sk.bytes.as_slice(), 19).map_err(|e| e.to_string())?;
    let want = archive::digest_file(old)?;
    let mut out = J::obj().with("format", pyjson::s(sk.format.clone())).with("size", pyjson::int(sk.size))
        .with("blocks", pyjson::int(sk.blocks.len() as u64)).with("skeleton", pyjson::int(sk.bytes.len() as u64))
        .with("stored", pyjson::int(packed.len() as u64));
    let status = match skeleton::rebuild(&sk, new, &mut std::io::sink()) {
        Ok(got) if got == want => "ok".to_string(),
        Ok(_) => "rebuilds other bytes".to_string(),
        Err(e) => e,
    };
    let ok = status == "ok";
    out.set("status", pyjson::s(status));
    println!("{}", pyjson::default(&out));
    Ok(if ok { 0 } else { 1 })
}

fn info(top: &Path) -> Result<i32, String> {
    let (top, _, _) = sections(top);
    let Some(store) = Store::open(&top)? else { println!("no skeleton section in {}", top.display()); return Ok(1) };
    let (n, stored): (i64, i64) = store.connection().query_row("SELECT count(*), coalesce(sum(length(skeleton)),0) FROM skeletons", [], |r| Ok((r.get(0)?, r.get(1)?)))
        .map_err(|e| e.to_string())?;
    println!("{} skeletons, {:.1} MB stored", pyjson::thousands(n as i128), stored as f64 / 1e6);
    let mut st = store.connection().prepare("SELECT json_extract(record,'$.format'), count(*) FROM skeletons GROUP BY 1 ORDER BY 2 DESC").map_err(|e| e.to_string())?;
    let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))).map_err(|e| e.to_string())?;
    for row in rows { let (f, c) = row.map_err(|e| e.to_string())?; println!("  {f}: {}", pyjson::thousands(c as i128)); }
    Ok(0)
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let flag = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).and_then(|v| v.parse::<u64>().ok());
    let path = |i: usize| args.get(i).map(PathBuf::from);
    os::catch_sigint();
    let result = match (args.first().map(String::as_str), path(1), path(2)) {
        (Some("build"), Some(a), _) => build(&a, flag("--workers").unwrap_or(4) as usize, flag("--limit")),
        (Some("pair"), Some(old), Some(new)) => pair(&old, &new),
        (Some("info"), Some(a), _) => info(&a),
        _ => { eprintln!("usage: dngc-skeleton build ARCHIVE [--workers N] [--limit N] | pair OLD NEW | info ARCHIVE"); std::process::exit(2) }
    };
    match result { Ok(code) => std::process::exit(code), Err(e) => { eprintln!("dngc-skeleton: {e}"); std::process::exit(1) } }
}

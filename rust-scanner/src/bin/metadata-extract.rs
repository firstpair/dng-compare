//! Parallel metadata extraction. Preserve original JSON bytes and input ordering.
//!
//! `metadata-extract WORKERS [--engine exiftool|exiftool-rs]`. The default engine
//! runs one Perl ExifTool process per path. `exiftool-rs` extracts in-process
//! through the optional `dng-exiftool-ffi` crate and is only available when this
//! binary was built with `--features exiftool-rs`; its records are a separate
//! metadata identity from Perl ExifTool records.
use std::io::{Read, Write};
use std::process::Command;
use std::sync::{Arc, Mutex, atomic::{AtomicUsize, Ordering}};

const USAGE: &str = "usage: metadata-extract WORKERS [--engine exiftool|exiftool-rs] | --engine-version [exiftool|exiftool-rs]";
const EXIFTOOL_ARGS: [&str; 25] = [
    "-j","-a","-G0:1:4","-n","-struct","-b","-u","-U","-api","LargeFileSupport=1",
    "-all","-DNGPrivateData","-MakerNotes","-x","PreviewImage","-x","JpgFromRaw",
    "-x","ThumbnailImage","-x","OtherImage","-x","RawImage","-x","OriginalRawFileData"];

#[derive(Clone, Copy, PartialEq)]
enum Engine { ExifTool, ExifToolRs }

/// One record's inner JSON object text (without the surrounding array) plus stderr.
type Extracted = Result<(Vec<u8>, Vec<u8>), String>;

fn parse_args() -> Result<(usize, Engine), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (workers, engine) = match args.as_slice() {
        [flag] if flag == "--engine-version" => (None, "exiftool"),
        [flag, engine] if flag == "--engine-version" => (None, engine.as_str()),
        [workers] => (Some(workers), "exiftool"),
        [workers, flag, engine] if flag == "--engine" => (Some(workers), engine.as_str()),
        _ => return Err(USAGE.into()),
    };
    let engine = match engine {
        "exiftool" => Engine::ExifTool,
        "exiftool-rs" if cfg!(feature = "exiftool-rs") => Engine::ExifToolRs,
        "exiftool-rs" => return Err("this metadata-extract was built without the exiftool-rs feature".into()),
        other => return Err(format!("unknown metadata engine: {other}")),
    };
    let Some(workers) = workers else { println!("{}", engine_version(engine)?); std::process::exit(0) };
    let workers: usize = workers.parse().map_err(|_| USAGE.to_string())?;
    if !(1..=36).contains(&workers) { return Err("workers must be 1..36".into()); }
    Ok((workers, engine))
}

fn engine_version(engine: Engine) -> Result<String, String> {
    match engine {
        Engine::ExifTool => {
            let output = Command::new("exiftool").arg("-ver").output().map_err(|e| format!("exiftool -ver failed: {e}"))?;
            if !output.status.success() { return Err(format!("exiftool -ver failed: {}", output.status)); }
            Ok(format!("exiftool {}", String::from_utf8_lossy(&output.stdout).trim()))
        }
        Engine::ExifToolRs => rs_engine_version(),
    }
}

#[cfg(feature = "exiftool-rs")]
fn rs_engine_version() -> Result<String, String> { Ok(dng_exiftool_ffi::engine_version()) }
#[cfg(not(feature = "exiftool-rs"))]
fn rs_engine_version() -> Result<String, String> { Err("exiftool-rs engine not compiled in".into()) }

fn extract_exiftool(path: &str) -> Extracted {
    let output = Command::new("exiftool").args(EXIFTOOL_ARGS).arg(path).output().map_err(|e| e.to_string())?;
    let trimmed = output.stdout.trim_ascii();
    if trimmed.first() != Some(&b'[') || trimmed.last() != Some(&b']') {
        return Err(format!("ExifTool returned invalid JSON array (exit {})", output.status));
    }
    // Do not deserialize/reserialize metadata: retain numbers, strings, keys and full values.
    Ok((trimmed[1..trimmed.len()-1].trim_ascii().to_vec(), output.stderr))
}

#[cfg(feature = "exiftool-rs")]
fn extract_exiftool_rs(path: &str) -> Extracted {
    Ok((dng_exiftool_ffi::record_json(std::path::Path::new(path)).into_bytes(), Vec::new()))
}
#[cfg(not(feature = "exiftool-rs"))]
fn extract_exiftool_rs(_path: &str) -> Extracted { Err("exiftool-rs engine not compiled in".into()) }

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let (workers, engine) = parse_args()?;
    let mut input = String::new(); std::io::stdin().read_to_string(&mut input)?;
    let paths: Vec<String> = serde_json::from_str(&input)?;
    let paths = Arc::new(paths); let next = Arc::new(AtomicUsize::new(0));
    let results: Arc<Mutex<Vec<Option<Extracted>>>> = Arc::new(Mutex::new((0..paths.len()).map(|_| None).collect()));
    std::thread::scope(|scope| {
        for _ in 0..workers.min(paths.len()) {
            let paths=Arc::clone(&paths); let next=Arc::clone(&next); let results=Arc::clone(&results);
            scope.spawn(move || loop {
                let i=next.fetch_add(1,Ordering::Relaxed); if i>=paths.len() { break; }
                let extracted = match engine { Engine::ExifTool => extract_exiftool(&paths[i]), Engine::ExifToolRs => extract_exiftool_rs(&paths[i]) };
                results.lock().unwrap()[i]=Some(extracted);
            });
        }
    });
    let mut stdout=std::io::BufWriter::new(std::io::stdout().lock());
    let mut stderr=std::io::stderr().lock(); stdout.write_all(b"[")?;
    let mut first=true;
    for slot in results.lock().unwrap().iter_mut() {
        let (inner, errors)=slot.take().ok_or("missing worker result")??;
        stderr.write_all(&errors)?;
        if !inner.is_empty() { if !first { stdout.write_all(b",")?; } stdout.write_all(&inner)?; first=false; }
    }
    stdout.write_all(b"]\n")?; stdout.flush()?; Ok(())
}

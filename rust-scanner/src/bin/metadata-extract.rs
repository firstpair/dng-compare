//! Parallel ExifTool extraction. Preserve original JSON bytes and input ordering.
use std::io::{Read, Write};
use std::process::{Command, Output};
use std::sync::{Arc, Mutex, atomic::{AtomicUsize, Ordering}};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let workers: usize = std::env::args().nth(1).ok_or("usage: metadata-extract WORKERS")?.parse()?;
    if !(1..=36).contains(&workers) { return Err("workers must be 1..36".into()); }
    let mut input = String::new(); std::io::stdin().read_to_string(&mut input)?;
    let paths: Vec<String> = serde_json::from_str(&input)?;
    let paths = Arc::new(paths); let next = Arc::new(AtomicUsize::new(0));
    let results: Arc<Mutex<Vec<Option<std::io::Result<Output>>>>> = Arc::new(Mutex::new((0..paths.len()).map(|_| None).collect()));
    std::thread::scope(|scope| {
        for _ in 0..workers.min(paths.len()) {
            let paths=Arc::clone(&paths); let next=Arc::clone(&next); let results=Arc::clone(&results);
            scope.spawn(move || loop {
                let i=next.fetch_add(1,Ordering::Relaxed); if i>=paths.len() { break; }
                let output=Command::new("exiftool").args([
                    "-j","-a","-G0:1:4","-n","-struct","-b","-u","-U","-api","LargeFileSupport=1",
                    "-all","-DNGPrivateData","-MakerNotes","-x","PreviewImage","-x","JpgFromRaw",
                    "-x","ThumbnailImage","-x","OtherImage","-x","RawImage","-x","OriginalRawFileData"
                ]).arg(&paths[i]).output();
                results.lock().unwrap()[i]=Some(output);
            });
        }
    });
    let mut stdout=std::io::BufWriter::new(std::io::stdout().lock());
    let mut stderr=std::io::stderr().lock(); stdout.write_all(b"[")?;
    let mut first=true;
    for slot in results.lock().unwrap().iter_mut() {
        let output=slot.take().ok_or("missing worker result")??;
        stderr.write_all(&output.stderr)?;
        let bytes=output.stdout; let trimmed=bytes.trim_ascii();
        if trimmed.first()!=Some(&b'[') || trimmed.last()!=Some(&b']') {
            return Err(format!("ExifTool returned invalid JSON array (exit {})",output.status).into());
        }
        // Do not deserialize/reserialize metadata: retain numbers, strings, keys and full values.
        let inner=trimmed[1..trimmed.len()-1].trim_ascii();
        if !inner.is_empty() { if !first { stdout.write_all(b",")?; } stdout.write_all(inner)?; first=false; }
    }
    stdout.write_all(b"]\n")?; stdout.flush()?; Ok(())
}

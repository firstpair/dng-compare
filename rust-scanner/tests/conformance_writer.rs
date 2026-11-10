//! What dng-compare-all writes passes the archive-format validator
//! (../conformance/validate_archive.py, docs/dngc-archive-spec.md).
//!
//! Three small DNG pairs: identical bytes, a different description (same raw
//! data, so a delta), and one changed sample (a delta too). The binary compares
//! and preserves them into an archive; the validator, which is written from the
//! specification alone, must find nothing wrong, rebuilding every record.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

/// A minimal little-endian TIFF with the DNGVersion tag: 64x64 16-bit samples
/// in one uncompressed strip, and an ImageDescription.
fn dng(description: &str, change: Option<(usize, u16)>) -> Vec<u8> {
    let (w, h) = (64u32, 64u32);
    let mut pixels: Vec<u16> = (0..w * h).map(|i| i as u16).collect();
    if let Some((i, v)) = change { pixels[i] = v; }
    let mut desc = description.as_bytes().to_vec();
    desc.push(0);
    let entries: u16 = 11;
    let ifd_off = 8u32;
    let ifd_len = 2 + entries as u32 * 12 + 4;
    let desc_off = ifd_off + ifd_len;
    let strip_off = desc_off + desc.len() as u32 + (desc.len() as u32 & 1);
    let strip_len = w * h * 2;
    let mut out = vec![b'I', b'I', 42, 0];
    out.extend(ifd_off.to_le_bytes());
    out.extend(entries.to_le_bytes());
    let mut tag = |out: &mut Vec<u8>, t: u16, ty: u16, n: u32, v: [u8; 4]| {
        out.extend(t.to_le_bytes()); out.extend(ty.to_le_bytes()); out.extend(n.to_le_bytes()); out.extend(v);
    };
    let short = |x: u16| { let b = x.to_le_bytes(); [b[0], b[1], 0, 0] };
    let long = |x: u32| x.to_le_bytes();
    tag(&mut out, 256, 3, 1, short(w as u16));       // ImageWidth
    tag(&mut out, 257, 3, 1, short(h as u16));       // ImageLength
    tag(&mut out, 258, 3, 1, short(16));             // BitsPerSample
    tag(&mut out, 259, 3, 1, short(1));              // Compression: none
    tag(&mut out, 262, 3, 1, short(1));              // Photometric: BlackIsZero
    tag(&mut out, 270, 2, desc.len() as u32, long(desc_off)); // ImageDescription
    tag(&mut out, 273, 4, 1, long(strip_off));       // StripOffsets
    tag(&mut out, 277, 3, 1, short(1));              // SamplesPerPixel
    tag(&mut out, 278, 3, 1, short(h as u16));       // RowsPerStrip
    tag(&mut out, 279, 4, 1, long(strip_len));       // StripByteCounts
    tag(&mut out, 50706, 1, 4, [1, 4, 0, 0]);        // DNGVersion
    out.extend(0u32.to_le_bytes());
    out.extend(&desc);
    if desc.len() & 1 == 1 { out.push(0); }
    for p in pixels { out.extend(p.to_le_bytes()); }
    out
}

fn write(p: &Path, bytes: &[u8]) { fs::create_dir_all(p.parent().unwrap()).unwrap(); fs::write(p, bytes).unwrap(); }

#[test]
fn written_archive_passes_the_validator() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let validator = repo.join("conformance/validate_archive.py");
    let python = std::env::var("PYTHON").unwrap_or_else(|_| "python3".into());
    if !validator.exists() || Command::new("xdelta3").arg("-V").output().is_err() || Command::new(&python).arg("-V").output().is_err() {
        eprintln!("skipped: needs the validator, xdelta3 and {python}");
        return;
    }
    let tmp: PathBuf = std::env::temp_dir().join(format!("dngc-rust-writer-{}", std::process::id()));
    let _ = fs::remove_dir_all(&tmp);
    let tmp = { fs::create_dir_all(&tmp).unwrap(); fs::canonicalize(&tmp).unwrap() };
    let (left, right) = (tmp.join("left"), tmp.join("right"));
    let cases: [(&str, Vec<u8>, Vec<u8>); 3] = [
        ("same.dng", dng("first", None), dng("first", None)),
        ("sub/meta.dng", dng("rated 5", None), dng("first", None)),
        ("pixel.dng", dng("first", Some((9 * 64 + 4, 7))), dng("first", None)),
    ];
    let mut pairs = String::new();
    for (rel, l, r) in &cases {
        write(&left.join(rel), l);
        write(&right.join(rel), r);
        pairs.push_str(&format!("{}\t{}\n", left.join(rel).display(), right.join(rel).display()));
    }
    fs::write(tmp.join("pairs.tsv"), pairs).unwrap();
    let archive = tmp.join("archive");
    let run = Command::new(env!("CARGO_BIN_EXE_dng-compare-all"))
        .arg(&left).arg(&right).arg("--pairs").arg(tmp.join("pairs.tsv"))
        .arg("--report").arg(archive.join("report")).arg("--archive").arg(&archive)
        .args(["--skip-metadata", "--workers", "2"]).output().unwrap();
    assert!(matches!(run.status.code(), Some(0 | 1)), "{}{}", String::from_utf8_lossy(&run.stdout), String::from_utf8_lossy(&run.stderr));
    assert!(archive.join("bytes/recovery.sqlite3").exists(), "no byte section written");
    let db = rusqlite::Connection::open(archive.join("bytes/recovery.sqlite3")).unwrap();
    let deltas: i64 = db.query_row("SELECT count(*) FROM files WHERE json_extract(record, '$.delta') IS NOT NULL", [], |r| r.get(0)).unwrap();
    let identical: i64 = db.query_row("SELECT count(*) FROM files WHERE json_extract(record, '$.whole_file_equal')", [], |r| r.get(0)).unwrap();
    assert_eq!((identical, deltas), (1, 2), "one identical record and two deltas");
    drop(db);
    let check = Command::new(&python).arg(&validator).arg(&archive).arg("--rebuild").output().unwrap();
    let report = String::from_utf8_lossy(&check.stdout);
    assert!(check.status.success(), "{report}{}", String::from_utf8_lossy(&check.stderr));
    assert!(report.contains("no problems"), "{report}");
    let _ = fs::remove_dir_all(&tmp);
}

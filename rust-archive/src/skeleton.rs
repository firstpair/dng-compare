//! Skeletons (docs/dngc-archive-spec.md, section 13): what makes a content
//! rebuildable from an edited version of itself.
//!
//! A metadata edit (a rating, develop settings, IPTC) rewrites a file's small
//! structures and carries its large blocks over unchanged, perhaps to other
//! offsets. A skeleton of a file is every byte outside its blocks, with a map of
//! where each block goes; the blocks themselves are not stored. Given a later
//! version of the file, the blocks are found in it by their marks and laid
//! around the skeleton, and the whole is checked against the content's SHA-256.
//!
//! Walkers: classic TIFF (DNG and TIFF-based raws) and JPEG. A file of another
//! format no larger than WHOLE is its own skeleton, with no blocks (a sidecar);
//! a larger one has no skeleton.

use crate::hex;
use crate::pyjson::{J, int, s};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use rusqlite::{Connection, OpenFlags, OptionalExtension};

/// An out-of-line TIFF value or JPEG segment this large is a block.
pub const BLOCK: u64 = 512;
/// A run this long that no tag points at is a block.
pub const GAP: u64 = 65536;
/// A block's mark hashes this many bytes from each end (the whole block when
/// it is no longer than twice this).
pub const MARK: u64 = 4096;
/// Strips or tiles of one image this close together are one block.
pub const PAD: u64 = 3;

/// A file without a walker is stored whole up to this size.
pub const WHOLE: u64 = 1 << 20;

/// XMP, IPTC and Photoshop resources: what editors rewrite, kept in the skeleton.
const VOLATILE: [u16; 3] = [700, 33723, 34377];
/// SubIFDs, Exif, GPS and Interoperability.
const POINTERS: [u16; 4] = [330, 34665, 34853, 40965];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block { pub at: u64, pub len: u64, pub mark: String, pub role: String }

/// A file's skeleton: its format, size, blocks in file order, and every byte
/// outside them, concatenated in file order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Skeleton { pub format: String, pub size: u64, pub blocks: Vec<Block>, pub bytes: Vec<u8> }

fn io(e: std::io::Error) -> String { e.to_string() }

/// Up to `len` bytes at `at`; fewer at the end of the file.
fn read_at(f: &mut File, at: u64, len: u64) -> Result<Vec<u8>, String> {
    f.seek(SeekFrom::Start(at)).map_err(io)?;
    let mut out = Vec::new();
    f.take(len).read_to_end(&mut out).map_err(io)?;
    Ok(out)
}

fn rd16(le: bool, b: &[u8]) -> u64 { (if le { u16::from_le_bytes([b[0], b[1]]) } else { u16::from_be_bytes([b[0], b[1]]) }) as u64 }
fn rd32(le: bool, b: &[u8]) -> u64 { (if le { u32::from_le_bytes([b[0], b[1], b[2], b[3]]) } else { u32::from_be_bytes([b[0], b[1], b[2], b[3]]) }) as u64 }

struct Entry { tag: u16, typ: u16, raw: [u8; 4], total: u64 }

fn type_size(typ: u16) -> u64 { match typ { 3 | 8 => 2, 4 | 9 | 11 | 13 => 4, 5 | 10 | 12 => 8, _ => 1 } }

/// An entry's unsigned integer values (BYTE, SHORT, LONG, IFD).
fn values(f: &mut File, le: bool, size: u64, e: &Entry) -> Vec<u64> {
    let width = match e.typ { 1 => 1, 3 => 2, 4 | 13 => 4, _ => return Vec::new() };
    let buf = if e.total <= 4 { e.raw[..e.total as usize].to_vec() } else {
        let at = rd32(le, &e.raw);
        if at + e.total > size { return Vec::new(); }
        read_at(f, at, e.total).unwrap_or_default()
    };
    if buf.len() as u64 != e.total { return Vec::new(); }
    buf.chunks(width).map(|c| match width { 1 => c[0] as u64, 2 => rd16(le, c), _ => rd32(le, c) }).collect()
}

type Extent = (u64, u64, String);

fn tiff(f: &mut File, size: u64) -> Result<Vec<Extent>, String> {
    let h = read_at(f, 0, 8)?;
    let le = match h.get(..2) { Some(b"II") => true, Some(b"MM") => false, _ => return Err("not a TIFF".into()) };
    if h.len() < 8 || rd16(le, &h[2..4]) != 42 { return Err("not a classic TIFF".into()); }
    let mut blocks: Vec<Extent> = Vec::new();
    let mut claims: Vec<(u64, u64)> = vec![(0, 8)];
    let mut seen = HashSet::new();
    let mut queue = VecDeque::from([(rd32(le, &h[4..8]), "0".to_string())]);
    while let Some((mut off, role)) = queue.pop_front() {
        let mut n = 0;
        while off != 0 && off + 6 <= size && seen.insert(off) {
            let head = read_at(f, off, 2)?;
            if head.len() < 2 { break; }
            let cnt = rd16(le, &head);
            let data = read_at(f, off + 2, cnt * 12 + 4)?;
            if (data.len() as u64) < cnt * 12 + 4 { break; }
            let r = if n == 0 { role.clone() } else { format!("{role}+{n}") };
            claims.push((off, 6 + cnt * 12));
            let entries: Vec<Entry> = data[..(cnt * 12) as usize].chunks(12).map(|c| {
                let typ = rd16(le, &c[2..4]) as u16;
                Entry { tag: rd16(le, &c[0..2]) as u16, typ, raw: [c[8], c[9], c[10], c[11]], total: type_size(typ) * rd32(le, &c[4..8]) }
            }).collect();
            let find = |tag: u16| entries.iter().find(|e| e.tag == tag);
            for (otag, ctag, name) in [(273, 279, "strips"), (324, 325, "tiles")] {
                if let (Some(o), Some(c)) = (find(otag), find(ctag)) {
                    let (os, cs) = (values(f, le, size, o), values(f, le, size, c));
                    for (o, c) in os.into_iter().zip(cs) {
                        if c > 0 && o + c <= size { blocks.push((o, c, format!("{r}/{name}"))); }
                    }
                }
            }
            if let (Some(o), Some(c)) = (find(513), find(514)) {
                if let (Some(&o), Some(&c)) = (values(f, le, size, o).first(), values(f, le, size, c).first()) {
                    if c > 0 && o + c <= size { blocks.push((o, c, format!("{r}/jpeg"))); }
                }
            }
            for e in &entries {
                if e.total <= 4 { continue; }
                let at = rd32(le, &e.raw);
                if at + e.total > size { continue; }
                claims.push((at, e.total));
                if e.total >= BLOCK && !VOLATILE.contains(&e.tag) && !POINTERS.contains(&e.tag) && ![273, 279, 324, 325].contains(&e.tag) {
                    blocks.push((at, e.total, format!("{r}/tag{}", e.tag)));
                }
            }
            for tag in POINTERS {
                if let Some(e) = find(tag) {
                    for (k, o) in values(f, le, size, e).into_iter().enumerate() { queue.push_back((o, format!("{r}/{tag}.{k}"))); }
                }
            }
            off = rd32(le, &data[(cnt * 12) as usize..]);
            n += 1;
        }
    }
    // Data only a maker's own structures reach: no tag points at it, and an
    // edit carries it over as it is.
    let mut all: Vec<(u64, u64)> = claims.into_iter().chain(blocks.iter().map(|b| (b.0, b.1))).collect();
    all.push((size, 0));
    all.sort();
    let mut pos = 0;
    for (o, l) in all {
        if o > pos && o - pos >= GAP { blocks.push((pos, o - pos, "gap".into())); }
        pos = pos.max(o + l);
    }
    Ok(blocks)
}

fn jpeg(f: &mut File, size: u64) -> Result<Vec<Extent>, String> {
    let mut blocks = Vec::new();
    let mut pos = 2;
    while pos + 4 <= size {
        let m = read_at(f, pos, 4)?;
        if m.len() < 4 || m[0] != 0xff { break; }
        let marker = m[1];
        if marker == 0xff { pos += 1; continue; }
        // Start of scan, or a marker without a length: the image runs from here.
        if marker == 0xda || marker == 0x00 || marker == 0x01 || (0xd0..=0xd9).contains(&marker) { break; }
        let seg = rd16(false, &m[2..4]) + 2;
        if seg < 4 || pos + seg > size { break; }
        // Exif and XMP (APP1), IPTC (APP13) and comments stay in the skeleton.
        if ![0xe1, 0xed, 0xfe].contains(&marker) && seg >= BLOCK { blocks.push((pos, seg, format!("seg{marker:02x}"))); }
        pos += seg;
    }
    if pos < size { blocks.push((pos, size - pos, "scan".into())); }
    Ok(blocks)
}

/// Sorted, without overlaps; the strips or tiles of one image merged when no
/// more than PAD bytes apart.
fn normalize(mut blocks: Vec<Extent>) -> Vec<Extent> {
    blocks.sort();
    blocks.dedup();
    let mut out: Vec<Extent> = Vec::new();
    for (o, l, role) in blocks {
        if let Some(last) = out.last_mut() {
            let end = last.0 + last.1;
            if o < end { continue; }
            if role == last.2 && (role.ends_with("strips") || role.ends_with("tiles")) && o - end <= PAD {
                last.1 = o + l - last.0;
                continue;
            }
        }
        out.push((o, l, role));
    }
    out
}

/// A file's format and blocks, decided by its first bytes.
fn walk(f: &mut File, size: u64) -> Result<(&'static str, Vec<Extent>), String> {
    let magic = read_at(f, 0, 4)?;
    match magic.as_slice() {
        [b'I', b'I', 42, 0] | [b'M', b'M', 0, 42] => Ok(("tiff", normalize(tiff(f, size)?))),
        [0xff, 0xd8, ..] => Ok(("jpeg", normalize(jpeg(f, size)?))),
        _ => Err("no skeleton walker for this format".into()),
    }
}

fn mark(f: &mut File, at: u64, len: u64) -> Result<String, String> {
    let mut h = Sha256::new();
    if len <= 2 * MARK { h.update(read_at(f, at, len)?); } else {
        h.update(read_at(f, at, MARK)?);
        h.update(read_at(f, at + len - MARK, MARK)?);
    }
    Ok(hex(&h.finalize()[..16]))
}

/// The skeleton of the file at `path`. Reads its structures, the bytes outside
/// its blocks, and each block's two ends; never a whole block.
pub fn build(path: &Path) -> Result<Skeleton, String> {
    let mut f = File::open(path).map_err(io)?;
    let size = f.metadata().map_err(io)?.len();
    let (format, extents) = match walk(&mut f, size) {
        Ok(w) => w,
        Err(_) if size <= WHOLE => ("whole", Vec::new()),
        Err(e) => return Err(e),
    };
    let (mut blocks, mut bytes, mut pos) = (Vec::new(), Vec::new(), 0);
    for (at, len, role) in extents {
        bytes.extend(read_at(&mut f, pos, at - pos)?);
        blocks.push(Block { at, len, mark: mark(&mut f, at, len)?, role });
        pos = at + len;
    }
    bytes.extend(read_at(&mut f, pos, size - pos)?);
    let outside = size - blocks.iter().map(|b| b.len).sum::<u64>();
    if bytes.len() as u64 != outside { return Err("file changed while its skeleton was read".into()); }
    Ok(Skeleton { format: format.into(), size, blocks, bytes })
}

/// Rebuild the skeleton's content into `sink` with blocks taken from `edited`,
/// a later version of the same file; returns the SHA-256 of what was written.
/// The caller compares it with the content's SHA-256: a mark covers only a
/// block's ends.
pub fn rebuild(skeleton: &Skeleton, edited: &Path, sink: &mut dyn Write) -> Result<String, String> {
    if skeleton.blocks.is_empty() {
        if skeleton.bytes.len() as u64 != skeleton.size { return Err("skeleton shorter than its content".into()); }
        sink.write_all(&skeleton.bytes).map_err(io)?;
        return Ok(hex(&Sha256::digest(&skeleton.bytes)));
    }
    let mut f = File::open(edited).map_err(io)?;
    let size = f.metadata().map_err(io)?.len();
    let (_, extents) = walk(&mut f, size)?;
    let mut found: HashMap<(u64, String), u64> = HashMap::new();
    for (at, len, _) in extents { found.entry((len, mark(&mut f, at, len)?)).or_insert(at); }
    let mut h = Sha256::new();
    let mut put = |d: &[u8]| -> Result<(), String> { h.update(d); sink.write_all(d).map_err(io) };
    let (mut pos, mut skel) = (0u64, 0usize);
    let mut buf = vec![0u8; 1 << 20];
    for b in &skeleton.blocks {
        let n = (b.at - pos) as usize;
        put(skeleton.bytes.get(skel..skel + n).ok_or("skeleton shorter than its map")?)?;
        skel += n;
        let at = *found.get(&(b.len, b.mark.clone()))
            .ok_or_else(|| format!("block {} ({} bytes) is not in {}", b.role, b.len, edited.display()))?;
        f.seek(SeekFrom::Start(at)).map_err(io)?;
        let mut left = b.len;
        while left > 0 {
            let want = left.min(buf.len() as u64) as usize;
            f.read_exact(&mut buf[..want]).map_err(io)?;
            put(&buf[..want])?;
            left -= want as u64;
        }
        pos = b.at + b.len;
    }
    put(&skeleton.bytes[skel..])?;
    if skel + (skeleton.size - pos) as usize != skeleton.bytes.len() { return Err("skeleton longer than its map".into()); }
    Ok(hex(&h.finalize()))
}

impl Skeleton {
    /// The record's `blocks` member: [at, len, mark, role] per block.
    pub fn blocks_json(&self) -> J {
        J::Arr(self.blocks.iter().map(|b| J::Arr(vec![int(b.at), int(b.len), s(b.mark.clone()), s(b.role.clone())])).collect())
    }

    /// A skeleton from a record (section 13.2) and its uncompressed bytes.
    pub fn from_record(record: &J, bytes: Vec<u8>) -> Result<Skeleton, String> {
        let n = |j: &J| j.as_i128().and_then(|v| u64::try_from(v).ok()).ok_or("bad number in a skeleton record");
        let mut blocks = Vec::new();
        for b in record.get("blocks").map(J::as_arr).unwrap_or(&[]) {
            let a = b.as_arr();
            if a.len() != 4 { return Err("bad block in a skeleton record".into()); }
            blocks.push(Block { at: n(&a[0])?, len: n(&a[1])?, mark: a[2].as_str().unwrap_or("").into(), role: a[3].as_str().unwrap_or("").into() });
        }
        let size = n(record.get("size").unwrap_or(&J::Null))?;
        let mut pos = 0;
        for b in &blocks {
            if b.at < pos || b.at + b.len > size { return Err("blocks out of order in a skeleton record".into()); }
            pos = b.at + b.len;
        }
        Ok(Skeleton { format: record.get("format").and_then(J::as_str).unwrap_or("").into(), size, blocks, bytes })
    }
}

/// The skeleton section of an archive: ARCHIVE/skeletons/skeletons.sqlite3,
/// one row per content, by its SHA-256.
pub struct Store { db: Connection }

fn sql(e: rusqlite::Error) -> String { e.to_string() }

impl Store {
    pub fn path(top: &Path) -> PathBuf { top.join("skeletons").join("skeletons.sqlite3") }

    /// The section opened for reading, or None when the archive has none.
    pub fn open(top: &Path) -> Result<Option<Store>, String> {
        let p = Store::path(top);
        if !p.exists() { return Ok(None); }
        let db = Connection::open_with_flags(&p, OpenFlags::SQLITE_OPEN_READ_ONLY).map_err(sql)?;
        Ok(Some(Store { db }))
    }

    /// The section opened for writing, created when missing.
    pub fn create(top: &Path) -> Result<Store, String> {
        std::fs::create_dir_all(top.join("skeletons")).map_err(io)?;
        let db = Connection::open(Store::path(top)).map_err(sql)?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
                          CREATE TABLE IF NOT EXISTS skeletons(sha256 TEXT PRIMARY KEY, record TEXT NOT NULL, skeleton BLOB NOT NULL);").map_err(sql)?;
        Ok(Store { db })
    }

    pub fn connection(&self) -> &Connection { &self.db }

    pub fn has(&self, sha256: &str) -> Result<bool, String> {
        self.db.query_row("SELECT 1 FROM skeletons WHERE sha256=?1", [sha256], |_| Ok(())).optional().map(|r| r.is_some()).map_err(sql)
    }

    /// The record (section 13.2) of a skeleton of the content `sha256`, found
    /// at `path` with `identity`, and its compressed bytes.
    pub fn encode(sha256: &str, path: &str, identity: J, skeleton: &Skeleton) -> Result<(String, Vec<u8>), String> {
        let packed = zstd::encode_all(skeleton.bytes.as_slice(), 19).map_err(io)?;
        let record = J::obj().with("sha256", s(sha256)).with("size", int(skeleton.size)).with("format", s(skeleton.format.clone()))
            .with("path", s(path)).with("identity", identity).with("blocks", skeleton.blocks_json())
            .with("skeleton_bytes", int(skeleton.bytes.len() as u64)).with("skeleton_sha256", s(hex(&Sha256::digest(&skeleton.bytes))))
            .with("skeleton_format", s("zstd"));
        Ok((crate::pyjson::default(&record), packed))
    }

    /// Add a row; a content that already has a skeleton keeps it.
    pub fn put(&self, sha256: &str, record: &str, packed: &[u8]) -> Result<(), String> {
        self.db.execute("INSERT OR IGNORE INTO skeletons(sha256, record, skeleton) VALUES(?1, ?2, ?3)", rusqlite::params![sha256, record, packed])
            .map(|_| ()).map_err(sql)
    }

    /// A content's record and skeleton, its bytes checked against skeleton_sha256.
    pub fn get(&self, sha256: &str) -> Result<Option<(J, Skeleton)>, String> {
        let row: Option<(String, Vec<u8>)> = self.db.query_row("SELECT record, skeleton FROM skeletons WHERE sha256=?1", [sha256], |r| Ok((r.get(0)?, r.get(1)?)))
            .optional().map_err(sql)?;
        let Some((text, packed)) = row else { return Ok(None) };
        let record = crate::pyjson::parse(&text)?;
        let bytes = zstd::decode_all(packed.as_slice()).map_err(io)?;
        if hex(&Sha256::digest(&bytes)) != record.get("skeleton_sha256").and_then(J::as_str).unwrap_or("") {
            return Err(format!("skeleton of {sha256} does not match its skeleton_sha256"));
        }
        let skeleton = Skeleton::from_record(&record, bytes)?;
        Ok(Some((record, skeleton)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn noise(n: usize, seed: u32) -> Vec<u8> {
        let mut x = seed.wrapping_mul(2654435761).max(1);
        (0..n).map(|_| { x ^= x << 13; x ^= x >> 17; x ^= x << 5; x as u8 }).collect()
    }

    /// A little-endian TIFF: XMP, a large private tag, one strip and a JPEG
    /// preview, then IFD0. A longer XMP packet moves everything after it.
    fn tiff(xmp: &[u8]) -> Vec<u8> {
        let (private, strip, preview) = (noise(800, 1), noise(200_000, 2), noise(30_000, 3));
        let mut out = vec![b'I', b'I', 42, 0, 0, 0, 0, 0];
        let place = |out: &mut Vec<u8>, d: &[u8]| { let at = out.len() as u32; out.extend(d); if out.len() % 2 == 1 { out.push(0); } at };
        let (x, p, s, j) = (place(&mut out, xmp), place(&mut out, &private), place(&mut out, &strip), place(&mut out, &preview));
        let ifd = out.len() as u32;
        out[4..8].copy_from_slice(&ifd.to_le_bytes());
        let entries: [(u16, u16, u32, u32); 6] = [(273, 4, 1, s), (279, 4, 1, strip.len() as u32), (513, 4, 1, j), (514, 4, 1, preview.len() as u32),
                                                  (700, 1, xmp.len() as u32, x), (50740, 1, private.len() as u32, p)];
        out.extend((entries.len() as u16).to_le_bytes());
        for (tag, typ, count, value) in entries {
            out.extend(tag.to_le_bytes()); out.extend(typ.to_le_bytes()); out.extend(count.to_le_bytes()); out.extend(value.to_le_bytes());
        }
        out.extend(0u32.to_le_bytes());
        out
    }

    fn jpeg(segments: &[(u8, Vec<u8>)], scan: &[u8]) -> Vec<u8> {
        let mut out = vec![0xff, 0xd8];
        for (marker, data) in segments {
            out.extend([0xff, *marker]); out.extend(((data.len() + 2) as u16).to_be_bytes()); out.extend(data);
        }
        out.extend([0xff, 0xda, 0, 2]); out.extend(scan); out.extend([0xff, 0xd9]);
        out
    }

    fn dir(name: &str) -> PathBuf {
        static N: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
        let n = N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let d = std::env::temp_dir().join(format!("dngc-skeleton-{name}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// Skeleton of `old`, rebuilt from `new`: the bytes rebuilt, or the error.
    fn round(name: &str, old: &[u8], new: &[u8]) -> (Skeleton, Result<Vec<u8>, String>) {
        let d = dir(name);
        std::fs::write(d.join("old"), old).unwrap();
        std::fs::write(d.join("new"), new).unwrap();
        let sk = build(&d.join("old")).unwrap();
        let mut out = Vec::new();
        let r = rebuild(&sk, &d.join("new"), &mut out).map(|sha| { assert_eq!(sha, hex(&Sha256::digest(&out))); out });
        let _ = std::fs::remove_dir_all(&d);
        (sk, r)
    }

    #[test]
    fn a_tiff_comes_back_from_an_edited_one_with_moved_blocks() {
        let old = tiff(b"<x:xmpmeta>rating 0</x:xmpmeta>");
        let new = tiff(&[b"<x:xmpmeta>rating 5, exposure +0.35, blacks +12 ".as_slice(), &[b' '; 3001], b"</x:xmpmeta>"].concat());
        let (sk, got) = round("tiff", &old, &new);
        assert_eq!(got.unwrap(), old);
        assert_eq!(sk.format, "tiff");
        assert_eq!(sk.blocks.iter().map(|b| b.role.as_str()).collect::<Vec<_>>(), ["0/tag50740", "0/strips", "0/jpeg"]);
        assert!(sk.bytes.len() < 300, "the skeleton holds the header, the XMP and the IFD only: {}", sk.bytes.len());
    }

    #[test]
    fn a_changed_block_is_reported_not_rebuilt() {
        let old = tiff(b"<x:xmpmeta/>");
        let mut new = old.clone();
        let strip = build_blocks(&old).into_iter().find(|b| b.role == "0/strips").unwrap();
        new[strip.at as usize] ^= 1;
        let err = round("tiff-changed", &old, &new).1.unwrap_err();
        assert!(err.contains("0/strips"), "{err}");
    }

    fn build_blocks(bytes: &[u8]) -> Vec<Block> {
        let d = dir("blocks");
        std::fs::write(d.join("f"), bytes).unwrap();
        let sk = build(&d.join("f")).unwrap();
        let _ = std::fs::remove_dir_all(&d);
        sk.blocks
    }

    #[test]
    fn a_change_between_a_blocks_marked_ends_fails_the_sha256() {
        let old = tiff(b"<x:xmpmeta/>");
        let mut new = old.clone();
        let strip = build_blocks(&old).into_iter().find(|b| b.role == "0/strips").unwrap();
        new[(strip.at + strip.len / 2) as usize] ^= 1;
        let (_, got) = round("tiff-middle", &old, &new);
        assert_ne!(got.unwrap(), old, "marks cover a block's ends; the caller's SHA-256 check catches the rest");
    }

    #[test]
    fn a_jpeg_comes_back_after_its_metadata_segments_change() {
        let (icc, scan) = (noise(3000, 4), noise(150_000, 5).into_iter().filter(|b| *b != 0xff).collect::<Vec<_>>());
        let old = jpeg(&[(0xe0, b"JFIF\0".to_vec()), (0xe1, b"Exif\0\0old".to_vec()), (0xe2, icc.clone()), (0xdb, noise(130, 6))], &scan);
        let new = jpeg(&[(0xe0, b"JFIF\0".to_vec()), (0xe1, b"Exif\0\0new and longer".to_vec()), (0xed, b"Photoshop 3.0\0caption".to_vec()),
                         (0xe1, noise(900, 7)), (0xe2, icc), (0xdb, noise(130, 6))], &scan);
        let (sk, got) = round("jpeg", &old, &new);
        assert_eq!(got.unwrap(), old);
        assert_eq!(sk.blocks.iter().map(|b| b.role.as_str()).collect::<Vec<_>>(), ["sege2", "scan"]);
    }

    #[test]
    fn a_small_file_without_a_walker_is_its_own_skeleton() {
        let old = b"<x:xmpmeta>sidecar</x:xmpmeta>".to_vec();
        let (sk, got) = round("whole", &old, b"anything else");
        assert_eq!((sk.format.as_str(), sk.blocks.len()), ("whole", 0));
        assert_eq!(got.unwrap(), old);
    }

    #[test]
    fn the_store_gives_back_what_was_put() {
        let d = dir("store");
        let old = tiff(b"<x:xmpmeta/>");
        std::fs::write(d.join("f.dng"), &old).unwrap();
        let sk = build(&d.join("f.dng")).unwrap();
        let sha = hex(&Sha256::digest(&old));
        let store = Store::create(&d).unwrap();
        let (record, packed) = Store::encode(&sha, "/somewhere/f.dng", J::Arr(vec![int(1u64), int(old.len() as u64), int(2u64), int(3u64)]), &sk).unwrap();
        assert_eq!(crate::pyjson::default(&crate::pyjson::parse(&record).unwrap()), record, "a record is written as json.dumps writes it");
        store.put(&sha, &record, &packed).unwrap();
        assert!(store.has(&sha).unwrap() && !store.has("00").unwrap());
        let (_, back) = store.get(&sha).unwrap().unwrap();
        assert_eq!(back, sk);
        let _ = std::fs::remove_dir_all(&d);
    }
}

//! The parts of tifffile (2026.9.20) that compare_dng.py's page comparison
//! observes, read from a file held in memory: the IFD chain and SubIFDs in
//! tifffile's order, the tag values it keeps (and drops), TiffPage's
//! subfiletype, photometric, dataoffsets, databytecounts and shape, and the
//! repr-equality of the ENCODING tag values. Error texts are tifffile's.

use crate::pyjson::{float_repr, py_repr_bytes};
use sha2::{Digest, Sha256};

pub const ENCODING: [u16; 30] = [256, 257, 258, 259, 262, 266, 277, 278, 284, 317, 322, 323, 338, 339, 347, 512, 515, 517,
    518, 519, 520, 521, 529, 530, 531, 532, 50871, 50974, 52543, 52544];
const TAG_READERS: [u16; 33] = [301, 320, 33471, 33560, 33628, 33629, 33630, 33631, 33723, 33923, 34118, 34361, 34362,
    34363, 34386, 34412, 34665, 34680, 34682, 34853, 37706, 37724, 40100, 40965, 43314, 50288, 50296, 50839, 51123, 51192,
    65426, 65432, 65439];
const TAG_TUPLE: [u16; 14] = [273, 279, 282, 283, 324, 325, 330, 338, 513, 514, 530, 531, 34736, 50838];
/// Tags a page's attributes or signature read; the values of others are not needed.
const KEPT: [u16; 13] = [254, 255, 273, 279, 324, 325, 330, 513, 514, 32997, 50706, 270, 52545];

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Num { Int(i128), Float(f64) }

impl Num {
    fn int(self) -> i128 { match self { Num::Int(i) => i, Num::Float(f) => f as i128 } }
    fn repr(self) -> String { match self { Num::Int(i) => i.to_string(), Num::Float(f) => float_repr(f) } }
}

#[derive(Clone, Debug)]
pub enum Val {
    Bytes(Vec<u8>),
    Str(String),
    /// A struct-unpacked tuple; `scalar` when tifffile unwraps a 1-tuple.
    Nums { items: Vec<Num>, scalar: bool },
    /// A NumPy array (count > 1024 outside TAG_TUPLE): its repr shows the
    /// dtype, the shape and only the first and last three items.
    Array { dtype: String, count: usize, head: Vec<Num>, tail: Vec<Num> },
}

impl Val {
    /// A text equal for two values exactly when their Python reprs are equal.
    fn repr_key(&self) -> String {
        let list = |v: &[Num]| v.iter().map(|n| n.repr()).collect::<Vec<_>>().join(", ");
        match self {
            Val::Bytes(b) => py_repr_bytes(b),
            Val::Str(s) => format!("s{s:?}"),
            Val::Nums { items, scalar: true } => items[0].repr(),
            Val::Nums { items, .. } => format!("({})", list(items)),
            Val::Array { dtype, count, head, tail } => format!("array({dtype}, {count}, [{}], [{}])", list(head), list(tail)),
        }
    }
    fn nums(&self) -> Vec<i128> {
        match self {
            Val::Bytes(b) => b.iter().map(|x| *x as i128).collect(),
            Val::Str(_) => Vec::new(),
            Val::Nums { items, .. } => items.iter().map(|n| n.int()).collect(),
            Val::Array { head, tail, .. } => head.iter().chain(tail).map(|n| n.int()).collect(),
        }
    }
    fn first(&self) -> Option<i128> { self.nums().first().copied() }
}

#[derive(Clone, Copy)]
pub struct Format { pub big_endian: bool, pub bigtiff: bool }

impl Format {
    fn u(&self, b: &[u8]) -> u64 {
        let mut v = 0u64;
        if self.big_endian { for x in b { v = v << 8 | *x as u64; } } else { for x in b.iter().rev() { v = v << 8 | *x as u64; } }
        v
    }
    fn tagnosize(&self) -> usize { if self.bigtiff { 8 } else { 2 } }
    fn tagsize(&self) -> usize { if self.bigtiff { 20 } else { 12 } }
    fn offsetsize(&self) -> usize { if self.bigtiff { 8 } else { 4 } }
    pub fn byteorder(&self) -> char { if self.big_endian { '>' } else { '<' } }
}

pub struct Page {
    tags: Vec<(u16, u64, Val)>, // code, count, value; first of a code wins
    pub subfiletype: i128,
    pub photometric: i128,
    pub shape: Vec<i128>,
    pub offsets: Vec<i128>,
    pub counts: Vec<i128>,
    pub subifds: Option<Vec<i128>>,
}

impl Page {
    fn tag(&self, code: u16) -> Option<&(u16, u64, Val)> { self.tags.iter().find(|t| t.0 == code) }
    pub fn has(&self, code: u16) -> bool { self.tag(code).is_some() }
    fn value(&self, code: u16) -> Option<&Val> { self.tag(code).map(|t| &t.2) }
    fn attr(&self, code: u16) -> Option<i128> { self.value(code).and_then(Val::first) }
    pub fn signature(&self, f: Format) -> String {
        let mut s = String::from(f.byteorder());
        for code in ENCODING {
            if let Some(v) = self.value(code) { s.push_str(&format!("|{code}={}", v.repr_key())); }
        }
        s
    }
}

pub struct Tiff<'a> { pub data: &'a [u8], pub format: Format, first: Option<u64>, pub page0: Option<Page> }

fn read<'a>(data: &'a [u8], at: u64, n: usize) -> &'a [u8] {
    let start = (at as usize).min(data.len());
    &data[start..(start + n).min(data.len())]
}

fn struct_error(n: usize) -> String { format!("unpack requires a buffer of {n} bytes") }

/// tifffile.TiffFile(...): the header and the first page.
pub fn open(data: &[u8]) -> Result<Tiff<'_>, String> {
    let header = read(data, 0, 4);
    let big_endian = match header.get(..2) {
        Some(b"II") | Some(b"EP") => false,
        Some(b"MM") => true,
        _ => return Err(format!("not a TIFF file: header={}", py_repr_bytes(header))),
    };
    if header.len() < 4 { return Err(struct_error(2)); }
    let mut f = Format { big_endian, bigtiff: false };
    let version = f.u(&header[2..4]);
    match version {
        43 => {
            let more = read(data, 4, 4);
            if more.len() < 4 { return Err(struct_error(4)); }
            let (size, zero) = (f.u(&more[..2]), f.u(&more[2..]));
            if zero != 0 || size != 8 { return Err(format!("invalid BigTIFF offset size ({size}, {zero})")); }
            f.bigtiff = true;
        }
        42 | 0x4352 | 0x55 | 0x4F52 | 0x5352 => {}
        0x4E31 => if big_endian { return Err("invalid NIFF file".into()); },
        v => return Err(format!("invalid TIFF version={v}")),
    }
    let at = if f.bigtiff { 8 } else { 4 };
    let raw = read(data, at, f.offsetsize());
    if raw.len() < f.offsetsize() { return Err(struct_error(f.offsetsize())); }
    let first = f.u(raw);
    let mut t = Tiff { data, format: f, first: None, page0: None };
    if first != 0 && (first as usize) < data.len() {
        t.first = Some(first);
        t.page0 = Some(t.page(first)?);
    }
    Ok(t)
}

fn type_format(dtype: u16) -> Option<(usize, usize)> {
    // (items per value, item size) as TIFF.DATA_FORMATS
    Some(match dtype {
        1 | 2 | 6 | 7 => (1, 1), 3 | 8 => (1, 2), 4 | 9 | 11 | 13 => (1, 4), 5 | 10 => (2, 4), 12 | 16 | 17 | 18 => (1, 8),
        _ => return None,
    })
}

fn cp1252(b: &[u8]) -> Option<String> {
    const HIGH: [u32; 32] = [0x20AC, 0, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021, 0x02C6, 0x2030, 0x0160, 0x2039, 0x0152,
        0, 0x017D, 0, 0, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014, 0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, 0,
        0x017E, 0x0178];
    b.iter().map(|&c| match c { 0x80..=0x9f => char::from_u32(HIGH[(c - 0x80) as usize]).filter(|_| HIGH[(c - 0x80) as usize] != 0),
                                c => Some(c as char) }).collect()
}

fn py_strip(s: &str) -> String {
    let ws = |c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c);
    s.trim_matches(ws).to_string()
}

impl Tiff<'_> {
    fn nums(&self, dtype: u16, b: &[u8]) -> Vec<Num> {
        let f = self.format;
        let (_, size) = type_format(dtype).unwrap();
        b.chunks_exact(size).map(|c| {
            let u = f.u(c);
            match dtype {
                6 => Num::Int(u as u8 as i8 as i128), 8 => Num::Int(u as u16 as i16 as i128),
                9 | 10 => Num::Int(u as u32 as i32 as i128), 17 => Num::Int(u as i64 as i128),
                11 => Num::Float(f32::from_bits(u as u32) as f64), 12 => Num::Float(f64::from_bits(u)),
                _ => Num::Int(u as i128),
            }
        }).collect()
    }

    /// TiffPage(parent) at `offset`.
    pub fn page(&self, offset: u64) -> Result<Page, String> {
        let f = self.format;
        let size = self.data.len() as u128;
        let head = read(self.data, offset, f.tagnosize());
        if head.len() < f.tagnosize() { return Err(format!("corrupted tag list @{offset}")); }
        let tagno = f.u(head);
        if tagno > 4096 { return Err(format!("suspicious number of tags {tagno}")); }
        let entries = read(self.data, offset + f.tagnosize() as u64, tagno as usize * f.tagsize());
        if entries.len() != tagno as usize * f.tagsize() { return Err("corrupted IFD structure".into()); }
        let threshold = f.offsetsize() as u128;
        let mut tags: Vec<(u16, u64, Val)> = Vec::new();
        let mut ntags = 0;
        for e in entries.chunks_exact(f.tagsize()) {
            let code = f.u(&e[0..2]) as u16;
            let dtype = f.u(&e[2..4]) as u16;
            let (count, slot) = if f.bigtiff { (f.u(&e[4..12]), &e[12..20]) } else { (f.u(&e[4..8]), &e[8..12]) };
            let Some((per, item)) = type_format(dtype) else { continue };
            let valuesize = count as u128 * (per * item) as u128;
            let bytes: &[u8] = if valuesize > threshold || TAG_READERS.contains(&code) {
                let at = f.u(slot) as u128;
                if at < 8 || at + valuesize > size { continue; }
                &self.data[at as usize..(at + valuesize) as usize]
            } else {
                &slot[..valuesize as usize]
            };
            ntags += 1;
            if tags.iter().any(|t| t.0 == code) || !(ENCODING.contains(&code) || KEPT.contains(&code)) {
                if !tags.iter().any(|t| t.0 == code) { tags.push((code, count, Val::Bytes(Vec::new()))); }
                continue;
            }
            let value = match dtype {
                1 | 7 => Val::Bytes(bytes.to_vec()),
                2 => {
                    let mut b = bytes;
                    while let [rest @ .., 0] = b { b = rest; }
                    match std::str::from_utf8(b) {
                        Ok(s) => Val::Str(py_strip(s)),
                        Err(_) => cp1252(b).map_or_else(|| Val::Bytes(b.to_vec()), |s| Val::Str(py_strip(&s))),
                    }
                }
                _ if count > 1024 && !TAG_TUPLE.contains(&code) && valuesize > threshold => {
                    // read_array(byteorder + format char, count): count items, also for rationals
                    let all = self.nums(dtype, &bytes[..count as usize * item]);
                    let ch = match dtype { 3 => "u2", 8 => "i2", 4 | 13 | 5 => "u4", 9 | 10 => "i4", 16 | 18 => "u8", 17 => "i8",
                                           11 => "f4", 12 => "f8", 6 => "i1", _ => "u1" };
                    let native = !(f.big_endian && item > 1);
                    let dtype = if native { ch.to_string() } else { format!(">{ch}") };
                    Val::Array { dtype, count: all.len(), head: all[..3].to_vec(), tail: all[all.len() - 3..].to_vec() }
                }
                _ => {
                    let items = self.nums(dtype, bytes);
                    let scalar = items.len() == 1 && !TAG_TUPLE.contains(&code);
                    Val::Nums { items, scalar }
                }
            };
            tags.push((code, count, value));
        }
        let mut p = Page { tags, subfiletype: 0, photometric: 0, shape: Vec::new(), offsets: Vec::new(),
                           counts: Vec::new(), subifds: None };
        if ntags == 0 || (ntags == 1 && p.has(52545)) { return Ok(p); }
        p.subfiletype = p.attr(254).unwrap_or(0);
        let width = p.attr(256).unwrap_or(0);
        let length = p.attr(257).unwrap_or(0);
        let compression = p.attr(259).unwrap_or(1);
        p.photometric = p.attr(262).unwrap_or(0);
        let mut samples = p.attr(277).unwrap_or(1);
        let mut rowsperstrip = p.attr(278).unwrap_or((1 << 32) - 1);
        let planar = p.attr(284).unwrap_or(1);
        let depth = p.attr(32997).unwrap_or(1);
        let mut bitspersample: i128 = 1;
        p.subifds = p.value(330).map(Val::nums);
        if p.subfiletype == 0 {
            match p.attr(255) { Some(2) => p.subfiletype = 1, Some(3) => p.subfiletype = 2, _ => {} }
        }
        let list = |p: &Page, codes: [u16; 3]| codes.iter().find_map(|c| p.value(*c)).map(Val::nums).unwrap_or_default();
        p.offsets = list(&p, [324, 273, 513]);
        p.counts = list(&p, [325, 279, 514]);
        if width == 0 && length == 0 && !p.offsets.is_empty() && !p.counts.is_empty() {
            // tifffile reads the dimensions of an assumed JPEG segment here; not modelled.
        } else if compression == 6 {
            if !p.has(262) || p.photometric == 2 { p.photometric = 6; }
            if !p.has(258) { bitspersample = 8; }
            if !p.has(277) && [0, 1, 2, 6].contains(&p.photometric) { samples = 3; }
        }
        if let Some((_, count, v)) = p.tag(258)
            && bitspersample == 1 {
            // count > 1: tifffile keeps a tuple when the samples differ; only its first is modelled.
            let _ = count;
            bitspersample = v.nums().first().copied().unwrap_or(1);
        }
        if p.has(322) { rowsperstrip = 0; }
        else if p.has(257) {
            if p.tag(278).is_none_or(|t| t.1 > 1) { rowsperstrip = length; }
            rowsperstrip = rowsperstrip.min(length);
        }
        p.shape = if p.photometric == 2 || samples > 1 {
            if planar == 1 { if depth == 1 { vec![length, width, samples] } else { vec![depth, length, width, samples] } }
            else if depth == 1 { vec![samples, length, width] } else { vec![samples, depth, length, width] }
        } else if depth == 1 { vec![length, width] } else { vec![depth, length, width] };
        if p.counts.is_empty() {
            p.counts = vec![p.shape.iter().product::<i128>() * bitspersample.div_euclid(8)];
        }
        if length != 0 && rowsperstrip != 0 {
            let mut max = (length as f64 / rowsperstrip as f64).ceil() as i128 * depth;
            if planar == 2 { max *= samples; }
            let cut = |v: &mut Vec<i128>| {
                let n = v.len() as i128;
                if max != n { v.truncate((if max < 0 { (n + max).max(0) } else { max.min(n) }) as usize); }
            };
            cut(&mut p.counts);
            cut(&mut p.offsets);
        }
        Ok(p)
    }

    /// compare_dng.pages(tiff.pages) with page_groups: (raw, auxiliary), depth first.
    pub fn page_groups(&self) -> Result<(Vec<Page>, Vec<Page>), String> {
        let mut raw = Vec::new();
        let mut aux = Vec::new();
        let mut put = |p: Page| {
            if p.subfiletype == 0 && [0, 1, 2, 32803, 34892].contains(&p.photometric) { raw.push(p) } else { aux.push(p) }
        };
        let Some(first) = self.first else { return Ok((raw, aux)) };
        // The main chain, discovered one offset at a time as TiffPages._seek
        // walks it while iterating (its circular-reference check is not reached
        // then; a looping chain is cut off here instead of running on).
        let f = self.format;
        let mut offsets = vec![first];

        let mut index = 0usize;
        loop {
            if index >= offsets.len() {

                let at = *offsets.last().unwrap();
                let head = read(self.data, at, f.tagnosize());
                let tagno = if head.len() == f.tagnosize() { f.u(head) } else { u64::MAX };
                if tagno > 4096 { offsets.pop(); break; }
                let next = read(self.data, at + f.tagnosize() as u64, tagno as usize * f.tagsize() + f.offsetsize());
                if next.len() < f.offsetsize() { break; }
                let next = f.u(&next[next.len() - f.offsetsize()..]);
                if next == 0 || next as usize >= self.data.len() { break; }
                offsets.push(next);
                if offsets.len() > 100_000 { return Err("too many pages in IFD chain".into()); }

            }
            let page = self.page(offsets[index])?;
            let subs = page.subifds.clone();
            put(page);
            if let Some(subs) = subs { self.sub_pages(&subs, &mut put, 1)?; }
            index += 1;
        }
        Ok((raw, aux))
    }

    fn sub_pages(&self, offsets: &[i128], put: &mut impl FnMut(Page), depth: usize) -> Result<(), String> {
        if depth > 900 { return Err("maximum recursion depth exceeded".into()); }
        if offsets.is_empty() || offsets[0] == 0 || offsets[0] >= self.data.len() as i128 { return Ok(()); }
        for &o in offsets {
            let page = self.page(o.max(0) as u64)?;
            let subs = page.subifds.clone();
            put(page);
            if let Some(subs) = subs { self.sub_pages(&subs, put, depth + 1)?; }
        }
        Ok(())
    }
}

/// compare_dng.payload_digest over the bytes in memory; `size` is the stat size.
pub fn payload_digest(data: &[u8], page: &Page, size: u64) -> Result<(String, u64), String> {
    if page.offsets.is_empty() || page.offsets.len() != page.counts.len() {
        return Err("Image has no complete strip/tile directory".into());
    }
    let mut h = Sha256::new();
    let mut total = 0u64;
    for (&o, &c) in page.offsets.iter().zip(&page.counts) {
        if o <= 0 || c <= 0 || o + c > size as i128 { return Err(format!("Invalid image block: offset={o}, length={c}")); }
        let (o, c) = (o as usize, c as usize);
        if o + c > data.len() { return Err("Truncated image block".into()); }
        h.update((c as u64).to_le_bytes());
        h.update(&data[o..o + c]);
        total += c as u64;
    }
    Ok((crate::hex(&h.finalize()), total))
}

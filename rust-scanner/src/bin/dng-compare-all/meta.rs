//! compare_dng.py's metadata path: Perl ExifTool with its exact arguments,
//! normalize_tags, flatten, compare_metadata and compact.

use crate::pyjson::{self, J, canonical, py_str, s};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::process::Command;

const IMAGE_TAGS: [&str; 6] = ["PreviewImage", "JpgFromRaw", "ThumbnailImage", "OtherImage", "RawImage", "OriginalRawFileData"];
const LAYOUT: [&str; 12] = ["StripOffsets", "StripByteCounts", "TileOffsets", "TileByteCounts", "PreviewImageStart",
    "PreviewImageLength", "JpgFromRawStart", "JpgFromRawLength", "ThumbnailOffset", "ThumbnailLength", "FreeOffsets",
    "FreeByteCounts"];

pub enum Meta { Error(String), Tags { tags: BTreeMap<String, J>, warnings: Vec<String> } }

fn last(key: &str) -> &str { key.rsplit(':').next().unwrap_or(key) }

/// re.sub(r':Copy\d+(?=:)', '', key)
fn strip_copy(key: &str) -> String {
    let b = key.as_bytes();
    let mut out = String::new();
    let mut i = 0;
    while i < b.len() {
        if b[i..].starts_with(b":Copy") {
            let mut j = i + 5;
            while j < b.len() && b[j].is_ascii_digit() { j += 1; }
            if j > i + 5 && j < b.len() && b[j] == b':' { i = j; continue; }
        }
        let c = key[i..].chars().next().unwrap();
        out.push(c);
        i += c.len_utf8();
    }
    out
}

pub fn normalize_tags(entry: &[(String, J)]) -> Vec<(String, J)> {
    let mut groups: Vec<(String, Vec<J>)> = Vec::new();
    let mut at: HashMap<String, usize> = HashMap::new();
    for (key, value) in entry {
        let key = strip_copy(key);
        match at.get(&key) {
            Some(&i) => groups[i].1.push(value.clone()),
            None => { at.insert(key.clone(), groups.len()); groups.push((key, vec![value.clone()])); }
        }
    }
    groups.into_iter().map(|(k, mut values)| {
        if values.len() == 1 { return (k, values.pop().unwrap()); }
        let mut keyed: Vec<(String, J)> = values.into_iter().map(|v| (canonical(&v), v)).collect();
        keyed.sort_by(|a, b| a.0.cmp(&b.0));
        (k, J::obj().with("duplicate_values", J::Arr(keyed.into_iter().map(|x| x.1).collect())))
    }).collect()
}

pub fn flatten(value: &[(String, J)], prefix: &str, out: &mut BTreeMap<String, J>) {
    for (key, child) in value {
        let name = if prefix.is_empty() { key.clone() } else { format!("{prefix}/{key}") };
        match child { J::Obj(m) => flatten(m, &name, out), v => { out.insert(name, v.clone()); } }
    }
}

pub fn compact(value: &J) -> J {
    let encoded = canonical(value);
    if encoded.len() > 1024 {
        return J::obj().with("sha256", s(crate::hex(&Sha256::digest(encoded.as_bytes()))))
            .with("json_length", J::Int(encoded.len() as i128)).with("prefix", s(&encoded[..120]));
    }
    value.clone()
}

pub fn compare_metadata(a: &BTreeMap<String, J>, b: &BTreeMap<String, J>) -> J {
    let mut cats: [(&str, Vec<J>); 3] = [("embedded", vec![]), ("filesystem", vec![]), ("layout", vec![])];
    let mut keys: Vec<&String> = a.keys().chain(b.keys()).collect();
    keys.sort();
    keys.dedup();
    for key in keys {
        if key.starts_with("ExifTool:") || key.starts_with("Composite:") { continue; }
        let (x, y) = (a.get(key), b.get(key));
        if let (Some(x), Some(y)) = (x, y) && pyjson::py_eq(x, y) { continue; }
        let tag = last(key);
        if ["Directory", "FileName", "FileAccessDate"].contains(&tag) && key.starts_with("System:") { continue; }
        let cat = if key.starts_with("System:") { 1 } else if LAYOUT.contains(&tag) { 2 } else { 0 };
        let kind = if x.is_none() { "added" } else if y.is_none() { "removed" } else { "changed" };
        let mut change = J::obj().with("tag", s(key.as_str())).with("kind", s(kind));
        if let Some(x) = x { change.set("left", compact(x)); }
        if let Some(y) = y { change.set("right", compact(y)); }
        cats[cat].1.push(change);
    }
    J::Obj(cats.into_iter().map(|(k, v)| (k.to_string(), J::Arr(v))).collect())
}

/// Python's text=True decoding of a child's output (UTF-8, universal newlines).
fn text(bytes: &[u8]) -> Result<String, String> {
    let t = std::str::from_utf8(bytes).map_err(|e| {
        let pos = e.valid_up_to();
        let why = if e.error_len().is_none() { "unexpected end of data" } else if bytes[pos] >= 0x80 && bytes[pos] < 0xc2 || bytes[pos] > 0xf4 { "invalid start byte" } else { "invalid continuation byte" };
        format!("'utf-8' codec can't decode byte 0x{:02x} in position {pos}: {why}", bytes[pos])
    })?;
    Ok(t.replace("\r\n", "\n").replace('\r', "\n"))
}

/// compare_dng.metadata(paths)
pub fn metadata(paths: &[String]) -> Result<HashMap<String, Meta>, String> {
    let mut cmd = Command::new("exiftool");
    cmd.args(["-j", "-G1:4", "-n", "-struct", "-b", "-u", "-api", "LargeFileSupport=1", "-all", "-DNGPrivateData", "-MakerNotes"]);
    for tag in IMAGE_TAGS { cmd.args(["-x", tag]); }
    cmd.args(paths);
    let out = cmd.output().map_err(|e| crate::os::os_error(&e, Some("exiftool")))?;
    let stdout = text(&out.stdout)?;
    let stderr = text(&out.stderr)?;
    let entries = match pyjson::parse(&stdout) {
        Ok(J::Arr(a)) => a,
        _ => return Err(format!("ExifTool failed: {}", stderr.chars().take(1000).collect::<String>())),
    };
    let mut result = HashMap::new();
    for entry in entries {
        let J::Obj(mut entry) = entry else { return Err("ExifTool returned a non-object record".into()) };
        let Some(at) = entry.iter().position(|(k, _)| k == "SourceFile") else { return Err("'SourceFile'".into()) };
        let path = py_str(&entry.remove(at).1);
        let errors: Vec<String> = entry.iter().filter(|(k, _)| last(k) == "Error").map(|(_, v)| py_str(v)).collect();
        let meta = if !errors.is_empty() { Meta::Error(errors.join("; ")) } else {
            let mut tags = BTreeMap::new();
            flatten(&normalize_tags(&entry), "", &mut tags);
            Meta::Tags { tags, warnings: entry.iter().filter(|(k, _)| last(k) == "Warning").map(|(_, v)| py_str(v)).collect() }
        };
        result.insert(path, meta);
    }
    Ok(result)
}

/// compare_dng.metadata_parallel(paths, processes)
pub fn metadata_parallel(paths: &[String], processes: usize) -> Result<HashMap<String, Meta>, String> {
    if processes <= 1 || paths.len() < 2 { return metadata(paths); }
    let size = paths.len().div_ceil(processes);
    let parts: Vec<_> = std::thread::scope(|scope| {
        let handles: Vec<_> = paths.chunks(size).map(|c| scope.spawn(move || metadata(c))).collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let mut result = HashMap::new();
    for part in parts { result.extend(part?); }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn copy_numbers_and_duplicates_normalize_as_python() {
        assert_eq!(strip_copy("IFD0:Copy12:Make"), "IFD0:Make");
        assert_eq!(strip_copy("IFD0:Copy:Make"), "IFD0:Copy:Make");
        assert_eq!(strip_copy("IFD0:Copy3"), "IFD0:Copy3");
        let entry = vec![("A:Copy1:T".to_string(), s("b")), ("A:T".to_string(), s("a")), ("X".to_string(), J::Int(1))];
        let n = normalize_tags(&entry);
        assert_eq!(canonical(&J::Obj(n)), r#"{"A:T":{"duplicate_values":["a","b"]},"X":1}"#);
    }
}

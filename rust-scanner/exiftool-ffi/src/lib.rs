//! Optional in-process metadata engine built on the `exiftool-rs` crate.
//!
//! One ExifTool-shaped JSON record per file, following the extraction flags the
//! Perl workers use (`-j -a -G0:1:4 -n -struct -b -u -U`, image tags excluded).
//! The engine is the dng-compare fork of exiftool-rs, which adds `-struct`
//! (nested XMP structures), numeric raw values for the file-level pseudo tags
//! and ExifTool's list rules. Values are still the engine's own: records from
//! this engine are a distinct metadata identity and must not be compared
//! against Perl ExifTool records as if equal.
use exiftool_rs::{ExifTool, Options, Value};
use std::collections::HashMap;
use std::path::Path;

pub const ENGINE: &str = "exiftool-rs";
/// Image payload tags omitted from records, matching the `-x` list of the Perl workers.
pub const EXCLUDED_TAGS: [&str; 6] = ["PreviewImage", "JpgFromRaw", "ThumbnailImage",
                                      "OtherImage", "RawImage", "OriginalRawFileData"];
/// Marks every record with the engine and its table version; the coordinator ignores `ExifTool:` keys.
pub const ENGINE_TAG: &str = "ExifTool:DngCompareEngine";
pub const ERROR_TAG: &str = "ExifTool:Error";

pub fn engine_version() -> String {
    format!("{ENGINE} {} (ExifTool {} tables)", exiftool_rs::VERSION, exiftool_rs::EXIFTOOL_VERSION)
}

fn options() -> Options {
    Options { duplicates: true, print_conv: false, show_unknown: 2, struct_output: true, ..Options::default() }
}

/// Serialize one file's metadata as a JSON object. Extraction failures become an
/// `ExifTool:ExifTool:Error` member so the coordinator keeps a failed receipt.
pub fn record_json(path: &Path) -> String {
    let mut out = String::with_capacity(8192);
    out.push('{');
    push_key(&mut out, "SourceFile"); push_string(&mut out, &path.to_string_lossy());
    out.push(','); push_key(&mut out, ENGINE_TAG); push_string(&mut out, &engine_version());
    match ExifTool::with_options(options()).extract_info(path) {
        Err(error) => { out.push(','); push_key(&mut out, ERROR_TAG); push_string(&mut out, &error.to_string()); }
        Ok(tags) => {
            let mut copies: HashMap<&str, usize> = HashMap::new();
            for tag in &tags {
                if EXCLUDED_TAGS.contains(&tag.name.as_str()) { continue; }
                let copy = copies.entry(tag.name.as_str()).or_insert(0); *copy += 1;
                out.push(','); push_key(&mut out, &group_key(&tag.group, &tag.name, *copy));
                push_value(&mut out, tag);
            }
        }
    }
    out.push('}');
    out
}

/// `-G0:1:4` key: ExifTool prints family 1 only when it differs from family 0
/// (`File:FileType` but `File:System:FileName`), and numbers later same-name
/// tags `Copy1`, `Copy2`, ... in family 4 so duplicate keys never collide.
fn group_key(group: &exiftool_rs::TagGroup, name: &str, copy: usize) -> String {
    let mut key = group.family0.clone();
    if !group.family1.is_empty() && group.family1 != group.family0 { key.push(':'); key.push_str(&group.family1); }
    if copy > 1 { key.push_str(&format!(":Copy{}", copy - 1)); }
    key.push(':'); key.push_str(name);
    key
}

fn push_key(out: &mut String, key: &str) { push_string(out, key); out.push(':'); }
fn push_string(out: &mut String, text: &str) { out.push_str(&serde_json::to_string(text).expect("string serializes")); }

/// ExifTool prints a JSON array only for list-valued tags (XMP bags, List=>1
/// fields). Plain multi-valued EXIF tags such as `DNGVersion` or
/// `BitsPerSample` are one space-joined string under `-n`. The crate marks the
/// first kind by a `print_value` equal to the ", "-join of its elements.
fn push_value(out: &mut String, tag: &exiftool_rs::Tag) {
    match &tag.raw_value {
        Value::Binary(bytes) | Value::Undefined(bytes) => push_string(out, &format!("base64:{}", base64(bytes))),
        value if value.has_struct() => push_nested(out, value),
        Value::List(items) if is_list_tag(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 { out.push(','); }
                match item {
                    Value::Binary(bytes) | Value::Undefined(bytes) => push_string(out, &format!("base64:{}", base64(bytes))),
                    scalar => push_scalar(out, &scalar.to_display_string()),
                }
            }
            out.push(']');
        }
        scalar => push_scalar(out, &scalar.to_display_string()),
    }
}

/// Under `-n` a list of strings is a List=>1 tag (XMP bags and sequences,
/// IPTC repeats) and prints as a JSON array; a list of numbers is a
/// multi-count EXIF value that ExifTool prints space-joined (`"1 4 0 0"`).
fn is_list_tag(items: &[Value]) -> bool {
    !items.is_empty() && items.iter().all(|v| matches!(v, Value::String(_)))
}

/// `-struct` values: objects keep the engine's sorted field order, lists are
/// arrays, scalars follow the same typing as top-level values.
fn push_nested(out: &mut String, value: &Value) {
    match value {
        Value::Struct(fields) => {
            out.push('{');
            for (i, (key, field)) in fields.iter().enumerate() {
                if i > 0 { out.push(','); }
                push_key(out, key); push_nested(out, field);
            }
            out.push('}');
        }
        Value::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 { out.push(','); }
                push_nested(out, item);
            }
            out.push(']');
        }
        Value::Binary(bytes) | Value::Undefined(bytes) => push_string(out, &format!("base64:{}", base64(bytes))),
        scalar => push_scalar(out, &scalar.to_display_string()),
    }
}

/// ExifTool JSON typing: values matching its number pattern are unquoted, true/false
/// become booleans, everything else is a quoted string.
fn push_scalar(out: &mut String, text: &str) {
    if is_exiftool_json_number(text) { out.push_str(text); }
    else if text.eq_ignore_ascii_case("true") || text.eq_ignore_ascii_case("false") { out.push_str(&text.to_ascii_lowercase()); }
    else { push_string(out, text); }
}

/// `^-?(\d|[1-9]\d{1,14})(\.\d{1,16})?(e[-+]?\d{1,3})?$`, ExifTool's EscapeJSON rule.
pub fn is_exiftool_json_number(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut rest: &[u8] = bytes.strip_prefix(b"-").unwrap_or(bytes);
    let digits = |s: &[u8]| s.iter().take_while(|b| b.is_ascii_digit()).count();
    let int_len = digits(rest);
    let int_ok = int_len == 1 || (2..=15).contains(&int_len) && rest[0] != b'0';
    if !int_ok { return false; }
    rest = &rest[int_len..];
    if let Some(after) = rest.strip_prefix(b".") {
        let frac_len = digits(after);
        if !(1..=16).contains(&frac_len) { return false; }
        rest = &after[frac_len..];
    }
    if let Some(after) = rest.strip_prefix(b"e").or_else(|| rest.strip_prefix(b"E")) {
        let after = after.strip_prefix(b"+").or_else(|| after.strip_prefix(b"-")).unwrap_or(after);
        let exp_len = digits(after);
        if !(1..=3).contains(&exp_len) { return false; }
        rest = &after[exp_len..];
    }
    rest.is_empty()
}

fn base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk.iter().fold(0u32, |acc, b| acc << 8 | u32::from(*b)) << (8 * (3 - chunk.len()));
        for i in 0..4 {
            if i <= chunk.len() { out.push(TABLE[(n >> (18 - 6 * i)) as usize & 63] as char); } else { out.push('='); }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// C ABI for the OCaml worker. Buffers are Box<[u8]> handed to the caller.
// ---------------------------------------------------------------------------

/// # Safety
/// `out` and `out_len` must be valid for writes; on 0 or 3 the buffer written to `out`
/// must be released with [`dng_exiftool_rs_free`] using the written length.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn dng_exiftool_rs_extract_json(path: *const u8, path_len: usize, out: *mut *mut u8, out_len: *mut usize) -> i32 {
    if path.is_null() || out.is_null() || out_len.is_null() { return 2; }
    let bytes = unsafe { std::slice::from_raw_parts(path, path_len) }.to_vec();
    let result = std::panic::catch_unwind(move || {
        #[cfg(unix)] let path = { use std::os::unix::ffi::OsStringExt; std::ffi::OsString::from_vec(bytes) };
        #[cfg(not(unix))] let path = std::ffi::OsString::from(String::from_utf8_lossy(&bytes).into_owned());
        record_json(Path::new(&path)).into_bytes()
    });
    let (code, bytes) = match result {
        Ok(bytes) => (0, bytes),
        Err(payload) => (3, format!("exiftool-rs engine panicked: {}", panic_message(&payload)).into_bytes()),
    };
    unsafe { hand_out(bytes, out, out_len) };
    code
}

/// # Safety
/// `out` and `out_len` must be valid for writes; release the buffer with [`dng_exiftool_rs_free`].
#[unsafe(no_mangle)]
pub unsafe extern "C" fn dng_exiftool_rs_engine_version(out: *mut *mut u8, out_len: *mut usize) -> i32 {
    if out.is_null() || out_len.is_null() { return 2; }
    unsafe { hand_out(engine_version().into_bytes(), out, out_len) };
    0
}

/// # Safety
/// `ptr`/`len` must come from one earlier `dng_exiftool_rs_*` call and be freed once.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn dng_exiftool_rs_free(ptr: *mut u8, len: usize) {
    if ptr.is_null() { return; }
    drop(unsafe { Box::from_raw(std::ptr::slice_from_raw_parts_mut(ptr, len)) });
}

unsafe fn hand_out(bytes: Vec<u8>, out: *mut *mut u8, out_len: *mut usize) {
    let len = bytes.len();
    let raw = Box::into_raw(bytes.into_boxed_slice());
    unsafe { *out = raw.cast::<u8>(); *out_len = len; }
}

fn panic_message(payload: &Box<dyn std::any::Any + Send>) -> String {
    payload.downcast_ref::<&str>().map(|s| s.to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "unknown panic".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    /// Minimal little-endian TIFF: 1x1 8-bit grayscale strip plus a DNGVersion tag.
    fn tiff_bytes() -> Vec<u8> {
        let entries: [(u16, u16, u32, u32); 9] = [
            (256, 4, 1, 1), (257, 4, 1, 1), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1),
            (273, 4, 1, 122), (277, 3, 1, 1), (279, 4, 1, 1), (50706, 1, 4, 0x0000_0401),
        ];
        let mut data = vec![0x49, 0x49, 42, 0, 8, 0, 0, 0, 9, 0];
        for (tag, kind, count, value) in entries {
            data.extend(tag.to_le_bytes()); data.extend(kind.to_le_bytes());
            data.extend(count.to_le_bytes()); data.extend(value.to_le_bytes());
        }
        data.extend(0u32.to_le_bytes());
        assert_eq!(data.len(), 122);
        data.push(0x7f);
        data
    }

    fn write_temp(name: &str, bytes: &[u8]) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("dng-exiftool-ffi-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(name);
        std::fs::File::create(&path).unwrap().write_all(bytes).unwrap();
        path
    }

    #[test]
    fn tiff_record_is_unique_keyed_exiftool_shape() {
        let path = write_temp("fixture.DNG", &tiff_bytes());
        let text = record_json(&path);
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        let object = parsed.as_object().unwrap();
        assert_eq!(object["SourceFile"], serde_json::json!(path.to_string_lossy()));
        assert_eq!(object[ENGINE_TAG], serde_json::json!(engine_version()));
        assert!(!object.contains_key(ERROR_TAG), "{text}");
        assert!(object.contains_key("EXIF:IFD0:ImageWidth"), "{text}");
        assert!(object.contains_key("File:FileType"), "{text}");
        assert!(object.keys().all(|k| k == "SourceFile" || k.contains(':')));
        let keys = raw_keys(&text);
        assert_eq!(keys.len(), object.len(), "duplicate JSON keys in {text}");
        assert!(object.keys().all(|k| EXCLUDED_TAGS.iter().all(|x| !k.ends_with(&format!(":{x}")))));
    }

    #[test]
    fn copy_numbers_make_duplicate_names_distinct() {
        let group = |f0: &str, f1: &str| exiftool_rs::TagGroup { family0: f0.into(), family1: f1.into(), family2: String::new(), family3: "Main".into() };
        assert_eq!(group_key(&group("File", "File"), "FileType", 1), "File:FileType");
        assert_eq!(group_key(&group("EXIF", "IFD0"), "Make", 1), "EXIF:IFD0:Make");
        assert_eq!(group_key(&group("EXIF", "ExifIFD"), "DateTimeOriginal", 2), "EXIF:ExifIFD:Copy1:DateTimeOriginal");
        assert_eq!(group_key(&group("XMP", ""), "Subject", 3), "XMP:Copy2:Subject");
    }

    /// Every key in document order, duplicates included, via a map visitor.
    fn raw_keys(text: &str) -> Vec<String> {
        struct Keys;
        impl<'de> serde::de::Visitor<'de> for Keys {
            type Value = Vec<String>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result { f.write_str("object") }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut map: A) -> Result<Vec<String>, A::Error> {
                let mut keys = Vec::new();
                while let Some(key) = map.next_key::<String>()? { map.next_value::<serde::de::IgnoredAny>()?; keys.push(key); }
                Ok(keys)
            }
        }
        serde::Deserializer::deserialize_map(&mut serde_json::Deserializer::from_str(text), Keys).unwrap()
    }

    #[test]
    fn unreadable_input_is_a_failed_receipt_not_success() {
        let path = write_temp("notes.txt", b"not an image at all\n");
        let parsed: serde_json::Value = serde_json::from_str(&record_json(&path)).unwrap();
        // Plain text is a recognised ExifTool file type, so it yields File tags, never an EXIF record.
        assert_eq!(parsed["File:MIMEType"], serde_json::json!("text/plain"), "{parsed}");
        assert!(parsed.as_object().unwrap().keys().all(|k| !k.starts_with("EXIF:")), "{parsed}");
        let garbage = write_temp("garbage.DNG", &[0x49, 0x49, 42, 0, 200, 0, 0, 0, 1, 2, 3]);
        let parsed: serde_json::Value = serde_json::from_str(&record_json(&garbage)).unwrap();
        assert!(parsed.as_object().unwrap().keys().all(|k| !k.starts_with("EXIF:IFD0:")), "{parsed}");
        let missing = record_json(Path::new("/nonexistent/dng-compare/missing.DNG"));
        let parsed: serde_json::Value = serde_json::from_str(&missing).unwrap();
        assert!(parsed[ERROR_TAG].as_str().unwrap().contains("I/O error"), "{missing}");
    }

    const LIGHTROOM_XMP: &[u8] = br#"<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" crs:Version="15.0"><crs:Look rdf:parseType="Resource"><crs:Name>Adobe Color</crs:Name><crs:Amount>1.000000</crs:Amount></crs:Look><crs:MaskGroupBasedCorrections><rdf:Seq><rdf:li rdf:parseType="Resource"><crs:What>Correction</crs:What><crs:CorrectionMasks><rdf:Seq><rdf:li rdf:parseType="Resource"><crs:What>Mask/Image</crs:What><crs:MaskValue>1</crs:MaskValue></rdf:li><rdf:li rdf:parseType="Resource"><crs:What>Mask/Gradient</crs:What><crs:MaskValue>0.5</crs:MaskValue></rdf:li></rdf:Seq></crs:CorrectionMasks></rdf:li><rdf:li rdf:parseType="Resource"><crs:What>Correction</crs:What><crs:CorrectionMasks><rdf:Seq><rdf:li rdf:parseType="Resource"><crs:What>Mask/Brush</crs:What><crs:MaskValue>0.7</crs:MaskValue></rdf:li></rdf:Seq></crs:CorrectionMasks></rdf:li></rdf:Seq></crs:MaskGroupBasedCorrections><dc:creator><rdf:Seq><rdf:li>one</rdf:li></rdf:Seq></dc:creator></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"#;

    /// The TIFF fixture with an XMP packet appended and referenced from tag 700.
    fn tiff_with_xmp() -> Vec<u8> {
        let entries: [(u16, u16, u32, u32); 10] = [
            (256, 4, 1, 1), (257, 4, 1, 1), (258, 3, 1, 8), (259, 3, 1, 1), (262, 3, 1, 1),
            (273, 4, 1, 134), (277, 3, 1, 1), (279, 4, 1, 1), (700, 1, LIGHTROOM_XMP.len() as u32, 135),
            (50706, 1, 4, 0x0000_0401),
        ];
        let mut data = vec![0x49, 0x49, 42, 0, 8, 0, 0, 0, 10, 0];
        for (tag, kind, count, value) in entries {
            data.extend(tag.to_le_bytes()); data.extend(kind.to_le_bytes());
            data.extend(count.to_le_bytes()); data.extend(value.to_le_bytes());
        }
        data.extend(0u32.to_le_bytes());
        assert_eq!(data.len(), 134);
        data.push(0x7f);
        data.extend_from_slice(LIGHTROOM_XMP);
        data
    }

    #[test]
    fn xmp_structures_are_nested_and_lists_follow_exiftool() {
        let path = write_temp("lightroom.DNG", &tiff_with_xmp());
        let text = record_json(&path);
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        let object = parsed.as_object().unwrap();
        // Perl ExifTool 13.59 `-j -a -G0:1:4 -n -struct` for the same packet.
        assert_eq!(object["XMP:XMP-crs:Look"], serde_json::json!({"Amount": 1.0, "Name": "Adobe Color"}));
        assert_eq!(object["XMP:XMP-crs:MaskGroupBasedCorrections"], serde_json::json!([
            {"CorrectionMasks": [{"MaskValue": 1, "What": "Mask/Image"}, {"MaskValue": 0.5, "What": "Mask/Gradient"}], "What": "Correction"},
            {"CorrectionMasks": [{"MaskValue": 0.7, "What": "Mask/Brush"}], "What": "Correction"}]));
        assert_eq!(object["XMP:XMP-dc:Creator"], serde_json::json!(["one"]));
        assert_eq!(object["EXIF:IFD0:DNGVersion"], serde_json::json!("1 4 0 0"));
        assert_eq!(object["Composite:ImageSize"], serde_json::json!("1 1"));
        assert_eq!(object["File:FileType"], serde_json::json!("DNG"));
        assert_eq!(object["File:ExifByteOrder"], serde_json::json!("II"));
        assert!(!object.contains_key("XMP:XMP-crs:LookName"), "{text}");
        assert!(text.find("\"XMP:XMP-crs:Look\"").unwrap() < text.find("\"XMP:XMP-dc:Creator\"").unwrap());
    }

    #[test]
    fn number_rule_matches_exiftool_escape_json() {
        for ok in ["0", "7", "-3", "12", "1.5", "-0.25", "123456789012345", "1e5", "2.5E-3"] { assert!(is_exiftool_json_number(ok), "{ok}"); }
        for bad in ["", "-", "0221", "1.", ".5", "1e", "1e1234", "abc", "1234567890123456", "1.12345678901234567", "0x10"] {
            assert!(!is_exiftool_json_number(bad), "{bad}");
        }
    }

    #[test]
    fn base64_matches_rfc4648() {
        assert_eq!(base64(b""), ""); assert_eq!(base64(b"f"), "Zg=="); assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v"); assert_eq!(base64(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn c_abi_round_trips_and_frees() {
        let path = write_temp("abi.DNG", &tiff_bytes());
        let bytes = path.as_os_str().as_encoded_bytes();
        let (mut out, mut len): (*mut u8, usize) = (std::ptr::null_mut(), 0);
        let code = unsafe { dng_exiftool_rs_extract_json(bytes.as_ptr(), bytes.len(), &mut out, &mut len) };
        assert_eq!(code, 0);
        let text = unsafe { std::slice::from_raw_parts(out, len) }.to_vec();
        unsafe { dng_exiftool_rs_free(out, len) };
        assert_eq!(String::from_utf8(text).unwrap(), record_json(&path));
        assert_eq!(unsafe { dng_exiftool_rs_extract_json(std::ptr::null(), 0, &mut out, &mut len) }, 2);
        assert_eq!(unsafe { dng_exiftool_rs_engine_version(&mut out, &mut len) }, 0);
        let version = unsafe { std::slice::from_raw_parts(out, len) }.to_vec();
        unsafe { dng_exiftool_rs_free(out, len) };
        assert_eq!(String::from_utf8(version).unwrap(), engine_version());
    }
}

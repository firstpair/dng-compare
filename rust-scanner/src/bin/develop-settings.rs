//! Capture develop settings from several editors into one comparable archive.
//!
//! The Rust counterpart of `develop_settings.py`. Both write the same archive
//! layout (`ARCHIVE/settings/LABEL.jsonl.gz`, `LABEL.provenance.json`,
//! `settings.sqlite3`) and the same typed values, so either can add captures to
//! an archive and either can compare or reindex it. This version reads the XMP
//! packet directly from each DNG instead of running ExifTool. Nothing here
//! writes to photos, catalogs or settings files.

use flate2::{Compression, read::MultiGzDecoder, write::GzEncoder};
use rusqlite::{Connection, OpenFlags, params, types::Value as SqlValue};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

const SCHEMA_VERSION: &str = "develop-settings-v1";
const CRS: &str = "http://ns.adobe.com/camera-raw-settings/1.0/";
const RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
const XML_NS: &str = "http://www.w3.org/XML/1998/namespace";
const INDEX_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS captures(label TEXT PRIMARY KEY, app TEXT NOT NULL, source TEXT NOT NULL,
  record_file TEXT NOT NULL, record_sha256 TEXT NOT NULL, photos INTEGER NOT NULL, created_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, tag TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS settings(label TEXT NOT NULL REFERENCES captures(label), photo_id INTEGER NOT NULL REFERENCES photos(id),
  tag_id INTEGER NOT NULL REFERENCES tags(id), value_json TEXT NOT NULL, number REAL, PRIMARY KEY(label,photo_id,tag_id));
CREATE INDEX IF NOT EXISTS settings_tag ON settings(tag_id,label,photo_id);
CREATE VIEW IF NOT EXISTS setting_values AS SELECT s.label,c.app,p.name photo,t.tag,s.value_json,s.number
  FROM settings s JOIN captures c ON c.label=s.label JOIN photos p ON p.id=s.photo_id JOIN tags t ON t.id=s.tag_id;
";

type Result<T> = std::result::Result<T, String>;

fn err<E: std::fmt::Display>(context: &str) -> impl Fn(E) -> String + '_ {
    move |e| format!("{context}: {e}")
}

// --- Types ----------------------------------------------------------------------------------

/// One typed setting value. Integers are tried before floats when reading JSON.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
enum Setting {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    List(Vec<Setting>),
    Map(BTreeMap<String, Setting>),
}

type Settings = BTreeMap<String, Setting>;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
enum App {
    #[serde(rename = "xmp")]
    Xmp,
    #[serde(rename = "lrcat")]
    Lrcat,
    #[serde(rename = "capture-one")]
    CaptureOne,
}

impl App {
    fn parse(text: &str) -> Result<App> {
        match text {
            "xmp" => Ok(App::Xmp),
            "lrcat" => Ok(App::Lrcat),
            "capture-one" => Ok(App::CaptureOne),
            _ => Err(format!("Unknown app {text}; use xmp, lrcat or capture-one")),
        }
    }
    fn name(self) -> &'static str {
        match self {
            App::Xmp => "xmp",
            App::Lrcat => "lrcat",
            App::CaptureOne => "capture-one",
        }
    }
}

/// Where one photo's settings were read from.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
enum Source {
    Snapshot(SnapshotSource),
    Catalog(CatalogSource),
    File(FileIdentity),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileIdentity {
    path: String,
    size: u64,
    mtime_ns: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct SnapshotSource {
    path: String,
    size: Option<u64>,
    mtime_ns: Option<i64>,
    snapshot: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogSource {
    path: String,
    image_id: Number,
    folder: String,
}

/// The unchanged source of one photo's settings.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
enum Raw {
    CaptureOne(CaptureOneRaw),
    Catalog(CatalogRaw),
    ExifTool(ExifToolRaw),
    XmpPacket(XmpPacketRaw),
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CaptureOneRaw {
    cos_xml: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogRaw {
    text: Option<String>,
    digest: Option<String>,
    virtual_copy: Option<String>,
    history: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExifToolRaw {
    exiftool: Map<String, Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    warnings: Option<Vec<String>>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct XmpPacketRaw {
    xmp_packet: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    photo: String,
    source: Source,
    settings: Settings,
    raw: Raw,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
struct CaptureSource {
    folder: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    snapshot: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    catalog: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    settings_dir: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    extensions: Option<Vec<String>>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Provenance {
    schema: String,
    label: String,
    app: App,
    source: CaptureSource,
    photos: usize,
    record_file: String,
    record_sha256: String,
    created_utc: String,
    #[serde(default)]
    implementation: Option<String>,
    tool_commit: Option<String>,
    #[serde(default)]
    notes: Vec<String>,
}

#[derive(Debug, Serialize)]
struct Agreement {
    left_tag: String,
    right_tag: String,
    photos: usize,
    identical: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    numeric: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    median_difference: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    median_abs_difference: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    p90_abs_difference: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    correlation: Option<f64>,
}

// --- Typed values -------------------------------------------------------------------------------

fn is_number(text: &str) -> bool {
    let b = text.as_bytes();
    let mut i = usize::from(matches!(b.first(), Some(b'+' | b'-')));
    let int_digits = b[i..].iter().take_while(|c| c.is_ascii_digit()).count();
    i += int_digits;
    let mut frac_digits = 0;
    if b.get(i) == Some(&b'.') {
        i += 1;
        frac_digits = b[i..].iter().take_while(|c| c.is_ascii_digit()).count();
        i += frac_digits;
    }
    if int_digits == 0 && frac_digits == 0 {
        return false;
    }
    if matches!(b.get(i), Some(b'e' | b'E')) {
        i += 1;
        if matches!(b.get(i), Some(b'+' | b'-')) {
            i += 1;
        }
        let exp = b[i..].iter().take_while(|c| c.is_ascii_digit()).count();
        if exp == 0 {
            return false;
        }
        i += exp;
    }
    i == b.len()
}

/// Text that is exactly a number or boolean becomes one; integers longer than
/// 15 digits stay text. Same rules as `typed()` in develop_settings.py.
fn typed_text(value: &str) -> Setting {
    let text = value.trim();
    if is_number(text) {
        let integral = !text.contains('.') && !text.to_ascii_lowercase().contains('e');
        if integral && text.trim_start_matches(['+', '-']).len() > 15 {
            return Setting::Text(value.to_string());
        }
        if integral
            && let Ok(n) = text.trim_start_matches('+').parse::<i64>() {
                return Setting::Int(n);
            }
        if let Ok(f) = text.parse::<f64>() {
            return Setting::Float(f);
        }
    }
    match text {
        "True" | "true" => Setting::Bool(true),
        "False" | "false" => Setting::Bool(false),
        _ => Setting::Text(value.to_string()),
    }
}

fn typed(value: Setting) -> Setting {
    match value {
        Setting::Text(text) => typed_text(&text),
        Setting::List(items) => Setting::List(items.into_iter().map(typed).collect()),
        Setting::Map(map) => Setting::Map(map.into_iter().map(|(k, v)| (k, typed(v))).collect()),
        other => other,
    }
}

fn from_json(value: &Value) -> Setting {
    match value {
        Value::Null => Setting::Null,
        Value::Bool(b) => Setting::Bool(*b),
        Value::Number(n) => n.as_i64().map(Setting::Int).unwrap_or_else(|| Setting::Float(n.as_f64().unwrap_or(f64::NAN))),
        Value::String(s) => Setting::Text(s.clone()),
        Value::Array(items) => Setting::List(items.iter().map(from_json).collect()),
        Value::Object(map) => Setting::Map(map.iter().map(|(k, v)| (k.clone(), from_json(v))).collect()),
    }
}

fn number_of(value: &Setting) -> Option<f64> {
    match value {
        Setting::Int(n) => Some(*n as f64),
        Setting::Float(f) if f.is_finite() => Some(*f),
        _ => None,
    }
}

fn value_json(value: &Setting) -> String {
    serde_json::to_string(value).expect("settings serialize")
}

// --- Lua table literals (Lightroom Classic settings text) ---------------------------------------

#[derive(Clone, Debug, PartialEq)]
enum LuaToken {
    Value(Setting),
    Name(String),
    Symbol(char),
}

fn lua_string(body: &str) -> Result<String> {
    // Lua strings are bytes: decimal escapes are bytes, other characters are the stored UTF-8.
    let mut out = Vec::new();
    let mut chars = body.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\\' {
            let mut buf = [0; 4];
            out.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            continue;
        }
        let n = chars.next().ok_or("Unterminated Lua escape")?;
        if n.is_ascii_digit() {
            let mut digits = n.to_string();
            while digits.len() < 3 && chars.peek().is_some_and(|d| d.is_ascii_digit()) {
                digits.push(chars.next().unwrap());
            }
            let byte: u32 = digits.parse().unwrap();
            out.push(u8::try_from(byte).map_err(|_| format!("Lua escape out of range: \\{digits}"))?);
        } else {
            let mapped = match n {
                'n' => '\n',
                't' => '\t',
                'r' => '\r',
                'a' => '\x07',
                'b' => '\x08',
                'f' => '\x0c',
                'v' => '\x0b',
                '\\' | '"' | '\'' | '\n' => n,
                _ => return Err(format!("Unsupported Lua string escape: \\{n}")),
            };
            out.push(mapped as u8);
        }
    }
    String::from_utf8(out).map_err(|e| format!("Lua string is not UTF-8: {e}"))
}

fn lua_tokens(text: &str) -> Result<Vec<LuaToken>> {
    let b = text.as_bytes();
    let mut i = 0;
    let mut tokens = Vec::new();
    while i < b.len() {
        let c = b[i];
        if c.is_ascii_whitespace() {
            i += 1;
        } else if text[i..].starts_with("--") {
            while i < b.len() && b[i] != b'\n' {
                i += 1;
            }
        } else if c == b'"' || c == b'\'' {
            let start = i + 1;
            i = start;
            while i < b.len() && b[i] != c {
                i += if b[i] == b'\\' { 2 } else { 1 };
            }
            if i >= b.len() {
                return Err("Unterminated Lua string".into());
            }
            tokens.push(LuaToken::Value(Setting::Text(lua_string(&text[start..i])?)));
            i += 1;
        } else if c.is_ascii_digit() || ((c == b'-' || c == b'+' || c == b'.') && b.get(i + 1).is_some_and(|d| d.is_ascii_digit() || *d == b'.')) {
            let start = i;
            i += 1;
            if text[start..].trim_start_matches(['+', '-']).to_ascii_lowercase().starts_with("0x") {
                while i < b.len() && (b[i].is_ascii_alphanumeric()) {
                    i += 1;
                }
                let digits = text[start..i].trim_start_matches('+');
                let (sign, hex) = digits.strip_prefix('-').map_or((1, digits), |h| (-1, h));
                let n = i64::from_str_radix(&hex[2..], 16).map_err(err("Lua hex number"))?;
                tokens.push(LuaToken::Value(Setting::Int(sign * n)));
                continue;
            }
            while i < b.len() && (b[i].is_ascii_digit() || b[i] == b'.' || b[i] == b'e' || b[i] == b'E'
                || ((b[i] == b'-' || b[i] == b'+') && matches!(b[i - 1], b'e' | b'E')))
            {
                i += 1;
            }
            let number = &text[start..i];
            if !is_number(number) {
                return Err(format!("Unparseable Lua number {number}"));
            }
            tokens.push(LuaToken::Value(typed_text(number)));
        } else if c.is_ascii_alphabetic() || c == b'_' {
            let start = i;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
                i += 1;
            }
            tokens.push(match &text[start..i] {
                "true" => LuaToken::Value(Setting::Bool(true)),
                "false" => LuaToken::Value(Setting::Bool(false)),
                "nil" => LuaToken::Value(Setting::Null),
                name => LuaToken::Name(name.to_string()),
            });
        } else if b"{}[]=,;".contains(&c) {
            tokens.push(LuaToken::Symbol(c as char));
            i += 1;
        } else {
            return Err(format!("Unparseable Lua text at offset {i}"));
        }
    }
    Ok(tokens)
}

fn lua_key(key: &Setting) -> String {
    match key {
        Setting::Text(s) => s.clone(),
        Setting::Int(n) => n.to_string(),
        Setting::Float(f) => format!("{f:?}"),
        Setting::Bool(true) => "True".into(),
        Setting::Bool(false) => "False".into(),
        other => value_json(other),
    }
}

fn lua_value(tokens: &[LuaToken], mut i: usize) -> Result<(Setting, usize)> {
    match tokens.get(i) {
        Some(LuaToken::Value(v)) => return Ok((v.clone(), i + 1)),
        Some(LuaToken::Symbol('{')) => {}
        other => return Err(format!("Unexpected Lua token: {other:?}")),
    }
    i += 1;
    let mut array = Vec::new();
    let mut mapping = BTreeMap::new();
    loop {
        match tokens.get(i) {
            None => return Err("Unterminated Lua table".into()),
            Some(LuaToken::Symbol('}')) => break,
            _ => {}
        }
        if let (Some(LuaToken::Name(name)), Some(LuaToken::Symbol('='))) = (tokens.get(i), tokens.get(i + 1)) {
            let (item, next) = lua_value(tokens, i + 2)?;
            mapping.insert(name.clone(), item);
            i = next;
        } else if tokens.get(i) == Some(&LuaToken::Symbol('[')) {
            let (key, next) = lua_value(tokens, i + 1)?;
            if tokens.get(next) != Some(&LuaToken::Symbol(']')) || tokens.get(next + 1) != Some(&LuaToken::Symbol('=')) {
                return Err("Malformed Lua key".into());
            }
            let (item, next) = lua_value(tokens, next + 2)?;
            mapping.insert(lua_key(&key), item);
            i = next;
        } else {
            let (item, next) = lua_value(tokens, i)?;
            array.push(item);
            i = next;
        }
        if matches!(tokens.get(i), Some(LuaToken::Symbol(',' | ';'))) {
            i += 1;
        }
    }
    let value = if !array.is_empty() && !mapping.is_empty() {
        for (n, item) in array.into_iter().enumerate() {
            mapping.insert((n + 1).to_string(), item);
        }
        Setting::Map(mapping)
    } else if array.is_empty() {
        Setting::Map(mapping)
    } else {
        Setting::List(array)
    };
    Ok((value, i + 1))
}

/// Parse Lightroom's `s = { ... }` settings text.
fn parse_lua_assignment(text: &str) -> Result<Setting> {
    let mut tokens = lua_tokens(text)?;
    if matches!(tokens.first(), Some(LuaToken::Name(_))) && tokens.get(1) == Some(&LuaToken::Symbol('=')) {
        tokens.drain(..2);
    }
    let (value, end) = lua_value(&tokens, 0)?;
    if end != tokens.len() {
        return Err("Trailing Lua tokens".into());
    }
    Ok(value)
}

// --- Minimal XML (XMP packets and Capture One settings) ---------------------------------------

#[derive(Debug, Default)]
struct Element {
    ns: String,
    local: String,
    attributes: Vec<Attribute>,
    children: Vec<Element>,
    text: String,
}

#[derive(Debug)]
struct Attribute {
    ns: String,
    local: String,
    value: String,
}

impl Element {
    fn attribute(&self, ns: &str, local: &str) -> Option<&str> {
        self.attributes.iter().find(|a| a.ns == ns && a.local == local).map(|a| a.value.as_str())
    }
    fn is(&self, ns: &str, local: &str) -> bool {
        self.ns == ns && self.local == local
    }
    fn descendants<'a>(&'a self, out: &mut Vec<&'a Element>) {
        for child in &self.children {
            out.push(child);
            child.descendants(out);
        }
    }
}

fn unescape_xml(text: &str) -> Result<String> {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find('&') {
        out.push_str(&rest[..at]);
        let end = rest[at..].find(';').ok_or("Unterminated XML entity")? + at;
        let entity = &rest[at + 1..end];
        match entity {
            "amp" => out.push('&'),
            "lt" => out.push('<'),
            "gt" => out.push('>'),
            "quot" => out.push('"'),
            "apos" => out.push('\''),
            _ => {
                let code = if let Some(hex) = entity.strip_prefix("#x").or_else(|| entity.strip_prefix("#X")) {
                    u32::from_str_radix(hex, 16)
                } else if let Some(dec) = entity.strip_prefix('#') {
                    dec.parse()
                } else {
                    return Err(format!("Unknown XML entity &{entity};"));
                }
                .map_err(err("XML character reference"))?;
                out.push(char::from_u32(code).ok_or("Invalid XML character reference")?);
            }
        }
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

fn split_name(qname: &str) -> (&str, &str) {
    qname.split_once(':').unwrap_or(("", qname))
}

/// Parse a complete XML document into namespace-resolved elements.
fn parse_xml(text: &str) -> Result<Element> {
    struct Open {
        element: Element,
        qname: String,
    }
    let mut scopes: Vec<HashMap<String, String>> = vec![HashMap::from([("xml".to_string(), XML_NS.to_string())])];
    let mut stack: Vec<Open> = Vec::new();
    let mut root: Option<Element> = None;
    let mut i = 0;
    let resolve = |scopes: &[HashMap<String, String>], prefix: &str, default: bool| -> Result<String> {
        if prefix.is_empty() && !default {
            return Ok(String::new());
        }
        for scope in scopes.iter().rev() {
            if let Some(uri) = scope.get(prefix) {
                return Ok(uri.clone());
            }
        }
        if prefix.is_empty() { Ok(String::new()) } else { Err(format!("Unbound XML prefix {prefix}")) }
    };
    while i < text.len() {
        let rest = &text[i..];
        if !rest.starts_with('<') {
            let end = rest.find('<').unwrap_or(rest.len());
            if let Some(open) = stack.last_mut() {
                open.element.text.push_str(&unescape_xml(&rest[..end])?);
            }
            i += end;
            continue;
        }
        if rest.starts_with("<?") {
            i += rest.find("?>").ok_or("Unterminated processing instruction")? + 2;
        } else if rest.starts_with("<!--") {
            i += rest.find("-->").ok_or("Unterminated comment")? + 3;
        } else if rest.starts_with("<![CDATA[") {
            let end = rest.find("]]>").ok_or("Unterminated CDATA")?;
            if let Some(open) = stack.last_mut() {
                open.element.text.push_str(&rest[9..end]);
            }
            i += end + 3;
        } else if rest.starts_with("<!") {
            i += rest.find('>').ok_or("Unterminated declaration")? + 1;
        } else if let Some(close) = rest.strip_prefix("</") {
            let end = close.find('>').ok_or("Unterminated end tag")?;
            let name = close[..end].trim();
            let open = stack.pop().ok_or("Unexpected end tag")?;
            if open.qname != name {
                return Err(format!("Mismatched end tag {name}"));
            }
            scopes.pop();
            match stack.last_mut() {
                Some(parent) => parent.element.children.push(open.element),
                None => root = Some(open.element),
            }
            i += end + 3;
        } else {
            // Start or empty tag: find the closing '>' outside quoted values.
            let b = rest.as_bytes();
            let mut j = 1;
            let mut quote = 0u8;
            while j < b.len() && (quote != 0 || b[j] != b'>') {
                if quote == 0 && (b[j] == b'"' || b[j] == b'\'') {
                    quote = b[j];
                } else if b[j] == quote {
                    quote = 0;
                }
                j += 1;
            }
            if j >= b.len() {
                return Err("Unterminated start tag".into());
            }
            let empty = b[j - 1] == b'/';
            let inner = &rest[1..if empty { j - 1 } else { j }];
            let name_end = inner.find(|c: char| c.is_whitespace()).unwrap_or(inner.len());
            let qname = inner[..name_end].to_string();
            let mut raw_attributes = Vec::new();
            let mut attrs = inner[name_end..].trim_start();
            while !attrs.is_empty() {
                let eq = attrs.find('=').ok_or("Malformed XML attribute")?;
                let key = attrs[..eq].trim().to_string();
                let after = attrs[eq + 1..].trim_start();
                let q = after.chars().next().ok_or("Missing attribute value")?;
                if q != '"' && q != '\'' {
                    return Err("Unquoted XML attribute".into());
                }
                let close = after[1..].find(q).ok_or("Unterminated attribute value")? + 1;
                raw_attributes.push((key, unescape_xml(&after[1..close])?));
                attrs = after[close + 1..].trim_start();
            }
            let mut scope = HashMap::new();
            for (key, value) in &raw_attributes {
                if key == "xmlns" {
                    scope.insert(String::new(), value.clone());
                } else if let Some(prefix) = key.strip_prefix("xmlns:") {
                    scope.insert(prefix.to_string(), value.clone());
                }
            }
            scopes.push(scope);
            let (prefix, local) = split_name(&qname);
            let mut element = Element { ns: resolve(&scopes, prefix, true)?, local: local.to_string(), ..Default::default() };
            for (key, value) in &raw_attributes {
                if key == "xmlns" || key.starts_with("xmlns:") {
                    continue;
                }
                let (prefix, local) = split_name(key);
                element.attributes.push(Attribute { ns: resolve(&scopes, prefix, false)?, local: local.to_string(), value: value.clone() });
            }
            let open = Open { element, qname };
            if empty {
                scopes.pop();
                match stack.last_mut() {
                    Some(parent) => parent.element.children.push(open.element),
                    None => root = Some(open.element),
                }
            } else {
                stack.push(open);
            }
            i += j + 1;
        }
    }
    if !stack.is_empty() {
        return Err(format!("Unclosed element {}", stack.last().map(|o| o.qname.as_str()).unwrap_or("")));
    }
    root.ok_or_else(|| "XML has no root element".to_string())
}

// --- XMP to ExifTool-style settings -------------------------------------------------------------

/// A property value, following RDF: resource, arrays, structures or text.
fn rdf_value(element: &Element) -> Setting {
    if let Some(resource) = element.attribute(RDF, "resource") {
        return Setting::Text(resource.to_string());
    }
    for child in &element.children {
        if child.ns == RDF && matches!(child.local.as_str(), "Seq" | "Bag") {
            return Setting::List(child.children.iter().filter(|li| li.is(RDF, "li")).map(rdf_value).collect());
        }
        if child.is(RDF, "Alt") {
            let items: Vec<&Element> = child.children.iter().filter(|li| li.is(RDF, "li")).collect();
            let chosen = items.iter().find(|li| li.attribute(XML_NS, "lang") == Some("x-default")).or(items.first());
            return chosen.map(|li| rdf_value(li)).unwrap_or(Setting::Text(String::new()));
        }
        if child.is(RDF, "Description") {
            return Setting::Map(rdf_fields(child));
        }
    }
    if element.attribute(RDF, "parseType") == Some("Resource") || !element.children.is_empty() || has_fields(element) {
        return Setting::Map(rdf_fields(element));
    }
    Setting::Text(element.text.clone())
}

fn has_fields(element: &Element) -> bool {
    element.attributes.iter().any(|a| a.ns != RDF && a.ns != XML_NS)
}

fn rdf_fields(element: &Element) -> BTreeMap<String, Setting> {
    let mut fields = BTreeMap::new();
    for a in element.attributes.iter().filter(|a| a.ns != RDF && a.ns != XML_NS) {
        fields.insert(a.local.clone(), Setting::Text(a.value.clone()));
    }
    for child in element.children.iter().filter(|c| c.ns != RDF) {
        fields.insert(child.local.clone(), rdf_value(child));
    }
    fields
}

/// Flatten structures into `Parent/Field` keys, as DNG Compare does with ExifTool output.
fn flatten(prefix: &str, value: Setting, out: &mut Settings) {
    match value {
        Setting::Map(map) => {
            for (key, child) in map {
                flatten(&format!("{prefix}/{key}"), child, out);
            }
        }
        other => {
            out.insert(prefix.to_string(), other);
        }
    }
}

fn xmp_settings(packet: &str) -> Result<Settings> {
    let root = parse_xml(packet)?;
    let mut all = vec![&root];
    root.descendants(&mut all);
    let mut settings = Settings::new();
    // Only top-level descriptions: nested ones are structure values (for example the Look).
    let descriptions = all.into_iter().filter(|e| e.is(RDF, "RDF")).flat_map(|rdf| rdf.children.iter().filter(|e| e.is(RDF, "Description")));
    for description in descriptions {
        for a in description.attributes.iter().filter(|a| a.ns == CRS) {
            settings.insert(format!("XMP-crs:{}", a.local), typed_text(&a.value));
        }
        for child in description.children.iter().filter(|c| c.ns == CRS) {
            flatten(&format!("XMP-crs:{}", child.local), typed(rdf_value(child)), &mut settings);
        }
    }
    Ok(settings)
}

// --- Reading files ------------------------------------------------------------------------------

fn identity(path: &Path) -> Result<FileIdentity> {
    let m = fs::metadata(path).map_err(err(&path.display().to_string()))?;
    Ok(FileIdentity {
        path: path.display().to_string(),
        size: m.size(),
        mtime_ns: m.mtime() * 1_000_000_000 + m.mtime_nsec(),
    })
}

/// Read the XMP packet (tag 700) from a TIFF/DNG's first IFD without reading image data.
fn dng_xmp(path: &Path) -> Result<Option<String>> {
    let mut f = File::open(path).map_err(err(&path.display().to_string()))?;
    let mut header = [0u8; 8];
    f.read_exact(&mut header).map_err(err("TIFF header"))?;
    let little = match &header[..2] {
        b"II" => true,
        b"MM" => false,
        _ => return Err(format!("{}: not a TIFF/DNG file", path.display())),
    };
    let u16_at = |b: &[u8]| if little { u16::from_le_bytes([b[0], b[1]]) } else { u16::from_be_bytes([b[0], b[1]]) };
    let u32_at = |b: &[u8]| if little { u32::from_le_bytes([b[0], b[1], b[2], b[3]]) } else { u32::from_be_bytes([b[0], b[1], b[2], b[3]]) };
    if u16_at(&header[2..4]) != 42 {
        return Err(format!("{}: unsupported TIFF variant", path.display()));
    }
    f.seek(SeekFrom::Start(u64::from(u32_at(&header[4..8])))).map_err(err("IFD0"))?;
    let mut count = [0u8; 2];
    f.read_exact(&mut count).map_err(err("IFD0 count"))?;
    let mut entries = vec![0u8; usize::from(u16_at(&count)) * 12];
    f.read_exact(&mut entries).map_err(err("IFD0 entries"))?;
    for entry in entries.chunks(12) {
        if u16_at(&entry[0..2]) != 700 {
            continue;
        }
        let kind = u16_at(&entry[2..4]);
        if kind != 1 && kind != 7 {
            return Err(format!("{}: XMP tag has type {kind}", path.display()));
        }
        let length = u32_at(&entry[4..8]) as usize;
        let mut packet = vec![0u8; length];
        if length <= 4 {
            packet.copy_from_slice(&entry[8..8 + length]);
        } else {
            f.seek(SeekFrom::Start(u64::from(u32_at(&entry[8..12])))).map_err(err("XMP offset"))?;
            f.read_exact(&mut packet).map_err(err("XMP packet"))?;
        }
        while packet.last() == Some(&0) {
            packet.pop();
        }
        return String::from_utf8(packet).map(Some).map_err(err("XMP packet is not UTF-8"));
    }
    Ok(None)
}

/// Read XMP from a JPEG's APP1 segments: the standard packet plus any extended XMP
/// chunks (assembled by GUID and offset). Packets are joined with a NUL separator.
fn jpeg_xmp(path: &Path) -> Result<Option<String>> {
    const STANDARD: &[u8] = b"http://ns.adobe.com/xap/1.0/\0";
    const EXTENDED: &[u8] = b"http://ns.adobe.com/xmp/extension/\0";
    let mut f = BufReader::new(File::open(path).map_err(err(&path.display().to_string()))?);
    let mut marker = [0u8; 2];
    f.read_exact(&mut marker).map_err(err("JPEG"))?;
    if marker != [0xFF, 0xD8] {
        return Err(format!("{}: not a JPEG file", path.display()));
    }
    let mut standard: Option<Vec<u8>> = None;
    // Extended XMP by GUID: declared full length and chunks by offset.
    type Chunks = (usize, BTreeMap<u32, Vec<u8>>);
    let mut extended: BTreeMap<Vec<u8>, Chunks> = BTreeMap::new();
    loop {
        f.read_exact(&mut marker).map_err(err("JPEG marker"))?;
        if marker[0] != 0xFF {
            return Err(format!("{}: malformed JPEG marker", path.display()));
        }
        let kind = marker[1];
        if kind == 0xD8 || kind == 0x01 || (0xD0..=0xD7).contains(&kind) || kind == 0xFF {
            continue;
        }
        if kind == 0xDA || kind == 0xD9 {
            break; // image data or end: no more metadata segments
        }
        let mut length = [0u8; 2];
        f.read_exact(&mut length).map_err(err("JPEG segment"))?;
        let length = usize::from(u16::from_be_bytes(length)).checked_sub(2).ok_or("JPEG segment length")?;
        let mut data = vec![0u8; length];
        f.read_exact(&mut data).map_err(err("JPEG segment"))?;
        if kind != 0xE1 {
            continue;
        }
        if let Some(packet) = data.strip_prefix(STANDARD) {
            standard = Some(packet.to_vec());
        } else if let Some(chunk) = data.strip_prefix(EXTENDED) {
            if chunk.len() < 40 {
                return Err(format!("{}: short extended XMP chunk", path.display()));
            }
            let guid = chunk[..32].to_vec();
            let full = u32::from_be_bytes([chunk[32], chunk[33], chunk[34], chunk[35]]) as usize;
            let offset = u32::from_be_bytes([chunk[36], chunk[37], chunk[38], chunk[39]]);
            extended.entry(guid).or_insert((full, BTreeMap::new())).1.insert(offset, chunk[40..].to_vec());
        }
    }
    let mut packets = Vec::new();
    if let Some(p) = standard {
        packets.push(String::from_utf8(p).map_err(err("XMP packet is not UTF-8"))?);
    }
    for (full, chunks) in extended.into_values() {
        let mut joined = Vec::with_capacity(full);
        for (offset, data) in chunks {
            if offset as usize != joined.len() {
                return Err(format!("{}: extended XMP has a gap", path.display()));
            }
            joined.extend_from_slice(&data);
        }
        if joined.len() != full {
            return Err(format!("{}: extended XMP is incomplete", path.display()));
        }
        packets.push(String::from_utf8(joined).map_err(err("extended XMP is not UTF-8"))?);
    }
    Ok((!packets.is_empty()).then(|| packets.join("\0")))
}

/// Settings from one or more NUL-separated XMP packets (JPEG standard plus extended XMP).
fn xmp_packets_settings(packets: &str) -> Result<Settings> {
    let mut settings = Settings::new();
    for packet in packets.split('\0').filter(|p| !p.trim().is_empty()) {
        settings.extend(xmp_settings(packet)?);
    }
    Ok(settings)
}

fn sorted_files(folder: &Path, keep: impl Fn(&Path) -> bool) -> Result<Vec<PathBuf>> {
    let mut paths: Vec<PathBuf> = fs::read_dir(folder)
        .map_err(err(&folder.display().to_string()))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.is_file() && keep(p))
        .collect();
    paths.sort_by(|a, b| a.as_os_str().cmp(b.as_os_str()));
    Ok(paths)
}

fn file_name(path: &Path) -> String {
    path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

fn read_gzip_lines(path: &Path) -> Result<Vec<String>> {
    let file = File::open(path).map_err(err(&path.display().to_string()))?;
    BufReader::new(MultiGzDecoder::new(file)).lines().collect::<std::io::Result<_>>().map_err(err(&path.display().to_string()))
}

fn capture_xmp(folder: &Path, snapshot: Option<&Path>, extensions: &[String]) -> Result<Vec<Record>> {
    if let Some(snapshot) = snapshot {
        let mut records = Vec::new();
        for line in read_gzip_lines(snapshot)? {
            let row: Map<String, Value> = serde_json::from_str(&line).map_err(err("snapshot row"))?;
            let file = row.get("file").and_then(Value::as_str).ok_or("Snapshot row has no file")?;
            let tags = row.get("tags").and_then(Value::as_object).ok_or_else(|| format!("Snapshot row has no tags: {file}"))?;
            let path = Path::new(file);
            let settings = tags.iter().filter(|(k, _)| k.starts_with("XMP-crs:")).map(|(k, v)| (k.clone(), typed(from_json(v)))).collect();
            records.push(Record {
                photo: file_name(path),
                source: Source::Snapshot(SnapshotSource {
                    path: if path.is_absolute() { file.to_string() } else { folder.join(file_name(path)).display().to_string() },
                    size: row.get("size").and_then(Value::as_u64),
                    mtime_ns: row.get("mtime_ns").and_then(Value::as_i64),
                    snapshot: snapshot.display().to_string(),
                }),
                settings,
                raw: Raw::ExifTool(ExifToolRaw { exiftool: tags.clone(), warnings: None }),
            });
        }
        return Ok(records);
    }
    let mut records = Vec::new();
    let wanted = |p: &Path| p.extension().is_some_and(|e| extensions.iter().any(|x| e.eq_ignore_ascii_case(x)));
    for path in sorted_files(folder, wanted)? {
        let before = identity(&path)?;
        let is_jpeg = path.extension().is_some_and(|e| e.eq_ignore_ascii_case("jpg") || e.eq_ignore_ascii_case("jpeg"));
        let packet = if is_jpeg { jpeg_xmp(&path)? } else { dng_xmp(&path)? };
        if identity(&path)? != before {
            return Err(format!("File changed while its metadata was read: {}", path.display()));
        }
        let settings = match &packet {
            Some(p) => xmp_packets_settings(p).map_err(err(&path.display().to_string()))?,
            None => Settings::new(),
        };
        records.push(Record { photo: file_name(&path), source: Source::File(before), settings, raw: Raw::XmpPacket(XmpPacketRaw { xmp_packet: packet }) });
    }
    Ok(records)
}

fn sql_number(value: SqlValue) -> Result<Number> {
    match value {
        SqlValue::Integer(n) => Ok(Number::from(n)),
        SqlValue::Real(f) => Number::from_f64(f).ok_or_else(|| "Non-finite image id".to_string()),
        other => Err(format!("Unexpected image id {other:?}")),
    }
}

fn capture_lrcat(folder: &Path, catalog: &Path) -> Result<(Vec<Record>, Vec<String>)> {
    let folder = format!("{}/", fs::canonicalize(folder).map_err(err("folder"))?.display().to_string().trim_end_matches('/'));
    let db = Connection::open_with_flags(catalog, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX).map_err(err("catalog"))?;
    db.busy_timeout(std::time::Duration::from_secs(5)).map_err(err("catalog"))?;
    db.execute_batch("PRAGMA query_only=1; BEGIN").map_err(err("catalog"))?;
    let mut records = Vec::new();
    {
        let mut images = db
            .prepare(
                "SELECT i.id_local, f.baseName || '.' || f.extension, i.copyName, d.text, d.digest
                 FROM AgLibraryRootFolder r JOIN AgLibraryFolder fo ON fo.rootFolder = r.id_local
                 JOIN AgLibraryFile f ON f.folder = fo.id_local JOIN Adobe_images i ON i.rootFile = f.id_local
                 LEFT JOIN Adobe_imageDevelopSettings d ON d.image = i.id_local
                 WHERE r.absolutePath || fo.pathFromRoot = ? ORDER BY f.baseName, i.id_local",
            )
            .map_err(err("catalog query"))?;
        let mut history = db
            .prepare("SELECT name FROM Adobe_libraryImageDevelopHistoryStep WHERE image=? ORDER BY dateCreated, id_local")
            .map_err(err("history query"))?;
        let rows = images
            .query_map(params![folder], |r| {
                Ok((r.get::<_, SqlValue>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<String>>(2)?, r.get::<_, Option<String>>(3)?, r.get::<_, Option<String>>(4)?))
            })
            .map_err(err("catalog query"))?;
        for row in rows {
            let (image, name, copy, text, digest) = row.map_err(err("catalog row"))?;
            let parsed = match text.as_deref().filter(|t| !t.is_empty()) {
                Some(t) => parse_lua_assignment(t).map_err(err(&format!("image {image:?}")))?,
                None => Setting::Map(BTreeMap::new()),
            };
            let Setting::Map(values) = parsed else {
                return Err(format!("Image {image:?} settings are not a table"));
            };
            let steps = history
                .query_map([image.clone()], |r| r.get::<_, String>(0))
                .map_err(err("history"))?
                .collect::<rusqlite::Result<Vec<_>>>()
                .map_err(err("history"))?;
            records.push(Record {
                photo: match &copy {
                    Some(c) => format!("{name} [{c}]"),
                    None => name,
                },
                source: Source::Catalog(CatalogSource { path: catalog.display().to_string(), image_id: sql_number(image)?, folder: folder.clone() }),
                settings: values.into_iter().map(|(k, v)| (format!("XMP-crs:{k}"), v)).collect(),
                raw: Raw::Catalog(CatalogRaw { text, digest, virtual_copy: copy, history: steps }),
            });
        }
    }
    db.execute_batch("ROLLBACK").map_err(err("catalog"))?;
    // Unrendered Auto: Classic stores -999999 until it computes the photo.
    let pending = records
        .iter()
        .filter(|r| r.settings.values().any(|v| number_of(v) == Some(-999999.0)))
        .map(|r| r.photo.clone())
        .collect();
    Ok((records, pending))
}

fn capture_one(settings_dir: &Path) -> Result<Vec<Record>> {
    let mut records = Vec::new();
    for path in sorted_files(settings_dir, |p| p.extension().is_some_and(|e| e == "cos"))? {
        let before = identity(&path)?;
        let text = fs::read_to_string(&path).map_err(err(&path.display().to_string()))?;
        if identity(&path)? != before {
            return Err(format!("Settings file changed while read: {}", path.display()));
        }
        let root = parse_xml(&text).map_err(err(&path.display().to_string()))?;
        let mut settings = Settings::new();
        let mut put = |group: &str, e: &Element| {
            let key = e.attribute("", "K").unwrap_or("None");
            settings.insert(format!("CaptureOne:{group}:{key}"), typed_text(e.attribute("", "V").unwrap_or("")));
        };
        for e in root.children.iter().filter(|e| e.local == "E") {
            put("Image", e);
        }
        for variant in root.children.iter().filter(|e| e.local == "VAR") {
            for e in variant.children.iter().filter(|e| e.local == "E") {
                put("Variant", e);
            }
            for block in variant.children.iter().filter(|e| e.local != "E") {
                let mut all = Vec::new();
                block.descendants(&mut all);
                for e in all.into_iter().filter(|e| e.local == "E") {
                    put(&block.local, e);
                }
            }
        }
        let name = file_name(&path);
        records.push(Record { photo: name.trim_end_matches(".cos").to_string(), source: Source::File(before), settings, raw: Raw::CaptureOne(CaptureOneRaw { cos_xml: text }) });
    }
    Ok(records)
}

// --- Archive ------------------------------------------------------------------------------------

fn sha256_file(path: &Path) -> Result<String> {
    let bytes = fs::read(path).map_err(err(&path.display().to_string()))?;
    Ok(Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect())
}

fn utc_now() -> String {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    let (secs, micros) = (now.as_secs() as i64, now.subsec_micros());
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{micros:06}+00:00", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn git_commit() -> Option<String> {
    let repository = Path::new(env!("CARGO_MANIFEST_DIR")).parent()?;
    let out = Command::new("git").args(["rev-parse", "HEAD"]).current_dir(repository).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn valid_label(label: &str) -> bool {
    let mut chars = label.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric()) && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// Write records as gzip JSON lines, sync, and check that they read back unchanged.
fn write_verified(path: &Path, records: &[Record]) -> Result<()> {
    let mut gz = GzEncoder::new(File::create(path).map_err(err("capture"))?, Compression::default());
    for r in records {
        let line = serde_json::to_string(r).map_err(err("record"))?;
        gz.write_all(line.as_bytes()).and_then(|_| gz.write_all(b"\n")).map_err(err("capture"))?;
    }
    gz.finish().and_then(|f| f.sync_all()).map_err(err("capture"))?;
    let saved: Vec<Record> = read_gzip_lines(path)?
        .iter()
        .map(|l| serde_json::from_str(l).map_err(err("read-back")))
        .collect::<Result<_>>()?;
    if saved != records {
        return Err("Capture read-back failed".into());
    }
    Ok(())
}

fn write_capture(archive: &Path, label: &str, app: App, source: CaptureSource, records: &[Record], notes: Vec<String>) -> Result<Provenance> {
    if !valid_label(label) {
        return Err("Label may contain letters, digits, dot, dash and underscore".into());
    }
    let mut names: Vec<&str> = records.iter().map(|r| r.photo.as_str()).collect();
    names.sort_unstable();
    if names.windows(2).any(|w| w[0] == w[1]) {
        return Err("Duplicate photo names in capture".into());
    }
    let folder = archive.join("settings");
    fs::create_dir_all(&folder).map_err(err("archive"))?;
    let out = folder.join(format!("{label}.jsonl.gz"));
    let provenance_path = folder.join(format!("{label}.provenance.json"));
    if out.exists() || provenance_path.exists() {
        return Err(format!("Capture {label} exists; choose a new label"));
    }
    let temporary = folder.join(format!("{label}.jsonl.tmp"));
    let published = write_verified(&temporary, records).and_then(|()| {
        fs::hard_link(&temporary, &out).map_err(err("capture exists")) // never replaces an existing capture
    });
    // The temporary file never outlives the attempt, whether it succeeded or failed.
    let _ = fs::remove_file(&temporary);
    published?;
    let provenance = Provenance {
        schema: SCHEMA_VERSION.into(),
        label: label.into(),
        app,
        source,
        photos: records.len(),
        record_file: file_name(&out),
        record_sha256: sha256_file(&out)?,
        created_utc: utc_now(),
        implementation: Some("rust".into()),
        tool_commit: git_commit(),
        notes,
    };
    fs::write(&provenance_path, serde_json::to_string_pretty(&provenance).map_err(err("provenance"))? + "\n").map_err(err("provenance"))?;
    index(archive, &provenance, records)?;
    Ok(provenance)
}

fn open_index(archive: &Path) -> Result<Connection> {
    let db = Connection::open(archive.join("settings").join("settings.sqlite3")).map_err(err("index"))?;
    db.busy_timeout(std::time::Duration::from_secs(60)).map_err(err("index"))?;
    db.execute_batch(INDEX_SCHEMA).map_err(err("index schema"))?;
    Ok(db)
}

fn index(archive: &Path, p: &Provenance, records: &[Record]) -> Result<()> {
    let mut db = open_index(archive)?;
    let tx = db.transaction().map_err(err("index"))?;
    let source = serde_json::to_string(&p.source).map_err(err("source"))?;
    tx.execute("INSERT INTO captures VALUES (?,?,?,?,?,?,?)", params![p.label, p.app.name(), source, p.record_file, p.record_sha256, p.photos as i64, p.created_utc])
        .map_err(err("index capture"))?;
    let mut ids: HashMap<(&str, String), i64> = HashMap::new();
    for r in records {
        let photo = key_id(&tx, &mut ids, "photos", "name", &r.photo)?;
        for (tag, value) in &r.settings {
            let tag_id = key_id(&tx, &mut ids, "tags", "tag", tag)?;
            tx.execute("INSERT INTO settings VALUES (?,?,?,?,?)", params![p.label, photo, tag_id, value_json(value), number_of(value)])
                .map_err(err("index setting"))?;
        }
    }
    tx.commit().map_err(err("index commit"))
}

fn key_id(db: &Connection, ids: &mut HashMap<(&'static str, String), i64>, table: &'static str, column: &str, value: &str) -> Result<i64> {
    if let Some(id) = ids.get(&(table, value.to_string())) {
        return Ok(*id);
    }
    db.execute(&format!("INSERT OR IGNORE INTO {table}({column}) VALUES (?)"), [value]).map_err(err(table))?;
    let id: i64 = db.query_row(&format!("SELECT id FROM {table} WHERE {column}=?"), [value], |r| r.get(0)).map_err(err(table))?;
    ids.insert((table, value.to_string()), id);
    Ok(id)
}

fn rebuild_index(archive: &Path) -> Result<()> {
    let folder = archive.join("settings");
    let database = folder.join("settings.sqlite3");
    if database.exists() {
        fs::remove_file(&database).map_err(err("index"))?;
    }
    let provenances = sorted_files(&folder, |p| file_name(p).ends_with(".provenance.json"))?;
    for path in provenances {
        let p: Provenance = serde_json::from_str(&fs::read_to_string(&path).map_err(err("provenance"))?).map_err(err(&path.display().to_string()))?;
        let out = folder.join(&p.record_file);
        if sha256_file(&out)? != p.record_sha256 {
            return Err(format!("Capture file changed: {}", p.record_file));
        }
        let records: Vec<Record> = read_gzip_lines(&out)?
            .iter()
            .map(|l| serde_json::from_str(l).map_err(err(&p.record_file)))
            .collect::<Result<_>>()?;
        index(archive, &p, &records)?;
    }
    Ok(())
}

fn median(sorted: &[f64]) -> f64 {
    let n = sorted.len();
    if n % 2 == 1 { sorted[n / 2] } else { (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0 }
}

fn correlation(xs: &[f64], ys: &[f64]) -> f64 {
    let n = xs.len() as f64;
    let (mx, my) = (xs.iter().sum::<f64>() / n, ys.iter().sum::<f64>() / n);
    let (mut sxy, mut sxx, mut syy) = (0.0, 0.0, 0.0);
    for (x, y) in xs.iter().zip(ys) {
        sxy += (x - mx) * (y - my);
        sxx += (x - mx) * (x - mx);
        syy += (y - my) * (y - my);
    }
    sxy / (sxx * syy).sqrt()
}

fn distinct(values: &[f64]) -> usize {
    let mut v: Vec<u64> = values.iter().map(|f| f.to_bits()).collect();
    v.sort_unstable();
    v.dedup();
    v.len()
}

fn compare(archive: &Path, left: &str, right: &str, mapping: Vec<(String, String)>) -> Result<Vec<Agreement>> {
    let db = open_index(archive)?;
    let pairs = if mapping.is_empty() {
        let mut s = db
            .prepare(
                "SELECT DISTINCT t.tag FROM settings a JOIN settings b ON b.photo_id=a.photo_id AND b.tag_id=a.tag_id \
                 JOIN tags t ON t.id=a.tag_id WHERE a.label=? AND b.label=? ORDER BY t.tag",
            )
            .map_err(err("compare"))?;
        s.query_map(params![left, right], |r| r.get::<_, String>(0)).map_err(err("compare"))?.map(|t| t.map(|t| (t.clone(), t))).collect::<rusqlite::Result<Vec<_>>>().map_err(err("compare"))?
    } else {
        mapping
    };
    let mut query = db
        .prepare(
            "SELECT a.value_json,b.value_json,a.number,b.number FROM settings a JOIN settings b ON b.photo_id=a.photo_id \
             JOIN tags ta ON ta.id=a.tag_id JOIN tags tb ON tb.id=b.tag_id \
             WHERE a.label=? AND b.label=? AND ta.tag=? AND tb.tag=?",
        )
        .map_err(err("compare"))?;
    let mut results = Vec::new();
    for (left_tag, right_tag) in pairs {
        let rows = query
            .query_map(params![left, right, left_tag, right_tag], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<f64>>(2)?, r.get::<_, Option<f64>>(3)?)))
            .map_err(err("compare"))?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(err("compare"))?;
        if rows.is_empty() {
            continue;
        }
        let identical = rows.iter().filter(|(a, b, x, y)| a == b || (x.is_some() && x == y)).count();
        let numeric: Vec<(f64, f64)> = rows.iter().filter_map(|(_, _, x, y)| Some(((*x)?, (*y)?))).collect();
        let mut agreement = Agreement {
            left_tag,
            right_tag,
            photos: rows.len(),
            identical,
            numeric: None,
            median_difference: None,
            median_abs_difference: None,
            p90_abs_difference: None,
            correlation: None,
        };
        if !numeric.is_empty() {
            let mut diff: Vec<f64> = numeric.iter().map(|(x, y)| y - x).collect();
            diff.sort_by(f64::total_cmp);
            let mut absolute: Vec<f64> = diff.iter().map(|d| d.abs()).collect();
            absolute.sort_by(f64::total_cmp);
            let p90 = ((0.9 * absolute.len() as f64) as usize).min(absolute.len() - 1);
            agreement.numeric = Some(numeric.len());
            agreement.median_difference = Some(median(&diff));
            agreement.median_abs_difference = Some(median(&absolute));
            agreement.p90_abs_difference = Some(absolute[p90]);
            let (xs, ys): (Vec<f64>, Vec<f64>) = numeric.into_iter().unzip();
            if xs.len() > 1 && distinct(&xs) > 1 && distinct(&ys) > 1 {
                agreement.correlation = Some(correlation(&xs, &ys));
            }
        }
        results.push(agreement);
    }
    Ok(results)
}

// --- Command line -------------------------------------------------------------------------------

const USAGE: &str = "usage:
  develop-settings capture ARCHIVE LABEL {xmp|lrcat|capture-one} FOLDER [--catalog LRCAT] [--settings-dir DIR] [--snapshot JSONL.GZ] [--extensions dng,jpg] [--allow-pending]
  develop-settings compare ARCHIVE LEFT RIGHT [--map LEFT_TAG=RIGHT_TAG ...]
  develop-settings reindex ARCHIVE";

struct Options {
    positional: Vec<String>,
    values: Vec<(String, String)>,
    flags: Vec<String>,
}

fn options(args: &[String]) -> Result<Options> {
    let mut o = Options { positional: Vec::new(), values: Vec::new(), flags: Vec::new() };
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        if a == "--allow-pending" {
            o.flags.push(a.clone());
        } else if a.starts_with("--") {
            let value = args.get(i + 1).ok_or_else(|| format!("{a} needs a value"))?;
            o.values.push((a.clone(), value.clone()));
            i += 1;
        } else {
            o.positional.push(a.clone());
        }
        i += 1;
    }
    Ok(o)
}

impl Options {
    fn value(&self, name: &str) -> Option<&str> {
        self.values.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }
}

fn run(args: &[String]) -> Result<()> {
    let command = args.first().map(String::as_str).ok_or(USAGE)?;
    let o = options(&args[1..])?;
    match (command, o.positional.as_slice()) {
        ("capture", [archive, label, app, folder]) => {
            let (archive, folder, app) = (Path::new(archive), Path::new(folder), App::parse(app)?);
            let mut notes = Vec::new();
            let mut source = CaptureSource { folder: folder.display().to_string(), ..Default::default() };
            let records = match app {
                App::Xmp => {
                    let snapshot = o.value("--snapshot").map(Path::new);
                    source.snapshot = snapshot.map(|s| s.display().to_string());
                    let extensions: Vec<String> = o
                        .value("--extensions")
                        .unwrap_or("dng")
                        .split(',')
                        .map(|e| e.trim().trim_start_matches('.').to_ascii_lowercase())
                        .filter(|e| !e.is_empty())
                        .collect();
                    if extensions != ["dng"] {
                        source.extensions = Some(extensions.clone());
                    }
                    capture_xmp(folder, snapshot, &extensions)?
                }
                App::Lrcat => {
                    let catalog = Path::new(o.value("--catalog").ok_or("lrcat requires --catalog")?);
                    source.catalog = Some(catalog.display().to_string());
                    let (records, pending) = capture_lrcat(folder, catalog)?;
                    if !pending.is_empty() && !o.flags.iter().any(|f| f == "--allow-pending") {
                        return Err(format!("{} photos have uncomputed Auto values (-999999); build previews first or pass --allow-pending", pending.len()));
                    }
                    if !pending.is_empty() {
                        notes.push(format!("{} photos recorded with uncomputed Auto placeholders", pending.len()));
                    }
                    records
                }
                App::CaptureOne => {
                    let settings_dir = match o.value("--settings-dir") {
                        Some(d) => PathBuf::from(d),
                        None => {
                            let mut dirs: Vec<PathBuf> = fs::read_dir(folder.join("CaptureOne"))
                                .map(|d| d.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.is_dir() && file_name(p).starts_with("Settings")).collect())
                                .unwrap_or_default();
                            dirs.sort();
                            if dirs.len() != 1 {
                                return Err(format!("pass --settings-dir; found {} CaptureOne/Settings* folders", dirs.len()));
                            }
                            dirs.remove(0)
                        }
                    };
                    source.settings_dir = Some(settings_dir.display().to_string());
                    capture_one(&settings_dir)?
                }
            };
            if records.is_empty() {
                return Err("No photos found".into());
            }
            let provenance = write_capture(archive, label, app, source, &records, notes)?;
            println!("{}", serde_json::to_string(&provenance).map_err(err("provenance"))?);
        }
        ("compare", [archive, left, right]) => {
            let mut mapping = Vec::new();
            for (k, v) in &o.values {
                if k != "--map" {
                    return Err(format!("Unknown option {k}"));
                }
                let (a, b) = v.split_once('=').ok_or("--map takes LEFT_TAG=RIGHT_TAG")?;
                mapping.push((a.to_string(), b.to_string()));
            }
            for row in compare(Path::new(archive), left, right, mapping)? {
                println!("{}", serde_json::to_string(&row).map_err(err("result"))?);
            }
        }
        ("reindex", [archive]) => rebuild_index(Path::new(archive))?,
        _ => return Err(USAGE.into()),
    }
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typed_values() {
        assert_eq!(typed_text("+0.34"), Setting::Float(0.34));
        assert_eq!(typed_text("+56"), Setting::Int(56));
        assert_eq!(typed_text("True"), Setting::Bool(true));
        assert_eq!(typed_text("1;1;1"), Setting::Text("1;1;1".into()));
        assert_eq!(typed_text("1234567890123456789"), Setting::Text("1234567890123456789".into()));
        assert_eq!(typed_text("66192840B295D81C39ED6F4E74CD19F6"), Setting::Text("66192840B295D81C39ED6F4E74CD19F6".into()));
        assert_eq!(value_json(&Setting::List(vec![Setting::Int(0), Setting::Float(1.5)])), "[0,1.5]");
    }

    #[test]
    fn lua_settings() {
        let text = "s = {\n\tAutoTone = true,\n\tExposure2012 = 0.4,\n\tCurve = { 0, 0, 255, 255, },\n\tLook = { Name = \"Adobe Color\" },\n\tRaw = \"Caf\\195\\169 \\\"q\\\"\",\n\tEmpty = {},\n}\n";
        let Setting::Map(m) = parse_lua_assignment(text).unwrap() else { panic!() };
        assert_eq!(m["AutoTone"], Setting::Bool(true));
        assert_eq!(m["Exposure2012"], Setting::Float(0.4));
        assert_eq!(m["Curve"], Setting::List(vec![Setting::Int(0), Setting::Int(0), Setting::Int(255), Setting::Int(255)]));
        assert_eq!(m["Raw"], Setting::Text("Café \"q\"".into()));
        assert_eq!(m["Empty"], Setting::Map(BTreeMap::new()));
        assert!(parse_lua_assignment("s = { a = os.exit() }").is_err());
    }

    #[test]
    fn xmp_structures() {
        let packet = r#"<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:Exposure2012="+0.34" crs:Temperature="5600" crs:WhiteBalance="As Shot &amp; more">
<crs:ToneCurvePV2012><rdf:Seq><rdf:li>0, 0</rdf:li><rdf:li>255, 255</rdf:li></rdf:Seq></crs:ToneCurvePV2012>
<crs:Look><rdf:Description crs:Name="Adobe Color"><crs:Parameters crs:Version="18.7" crs:ConvertToGrayscale="False"/></rdf:Description></crs:Look>
<crs:Group><rdf:Alt><rdf:li xml:lang="x-default">Profiles</rdf:li></rdf:Alt></crs:Group>
</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"#;
        let s = xmp_settings(packet).unwrap();
        assert!(!s.contains_key("XMP-crs:Name"), "nested description must not be read as top level");
        assert_eq!(s["XMP-crs:Exposure2012"], Setting::Float(0.34));
        assert_eq!(s["XMP-crs:WhiteBalance"], Setting::Text("As Shot & more".into()));
        assert_eq!(s["XMP-crs:ToneCurvePV2012"], Setting::List(vec![Setting::Text("0, 0".into()), Setting::Text("255, 255".into())]));
        assert_eq!(s["XMP-crs:Look/Name"], Setting::Text("Adobe Color".into()));
        assert_eq!(s["XMP-crs:Look/Parameters/Version"], Setting::Float(18.7));
        assert_eq!(s["XMP-crs:Look/Parameters/ConvertToGrayscale"], Setting::Bool(false));
        assert_eq!(s["XMP-crs:Group"], Setting::Text("Profiles".into()));
    }

    #[test]
    fn jpeg_xmp_segments() {
        let packet = r#"<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:Exposure2012="+0.42"/></rdf:RDF></x:xmpmeta>"#;
        let extended = r#"<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:Shadows2012="+12"/></rdf:RDF></x:xmpmeta>"#;
        let mut jpeg = vec![0xFF, 0xD8];
        let mut segment = |payload: Vec<u8>| {
            jpeg.extend_from_slice(&[0xFF, 0xE1]);
            jpeg.extend_from_slice(&((payload.len() + 2) as u16).to_be_bytes());
            jpeg.extend_from_slice(&payload);
        };
        segment([b"http://ns.adobe.com/xap/1.0/\0".as_slice(), packet.as_bytes()].concat());
        let (a, b) = extended.as_bytes().split_at(40);
        for (offset, part) in [(0u32, a), (40u32, b)] {
            let mut chunk = b"http://ns.adobe.com/xmp/extension/\0".to_vec();
            chunk.extend_from_slice(&[b'A'; 32]);
            chunk.extend_from_slice(&(extended.len() as u32).to_be_bytes());
            chunk.extend_from_slice(&offset.to_be_bytes());
            chunk.extend_from_slice(part);
            segment(chunk);
        }
        jpeg.extend_from_slice(&[0xFF, 0xDA, 0x00, 0x02, 0xFF, 0xD9]);
        let path = std::env::temp_dir().join(format!("develop-settings-test-{}.jpg", std::process::id()));
        fs::write(&path, &jpeg).unwrap();
        let packets = jpeg_xmp(&path).unwrap().unwrap();
        fs::remove_file(&path).unwrap();
        let s = xmp_packets_settings(&packets).unwrap();
        assert_eq!(s["XMP-crs:Exposure2012"], Setting::Float(0.42));
        assert_eq!(s["XMP-crs:Shadows2012"], Setting::Int(12));
    }
}

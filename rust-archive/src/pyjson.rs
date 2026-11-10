//! JSON as dng-compare's Python reads and writes it: `json.loads` semantics
//! (int/float distinction, last duplicate key wins in the first key's place)
//! and `json.dumps` layouts with `ensure_ascii`, including Python's float repr.

use std::collections::HashMap;
use std::fmt::Write as _;

#[derive(Clone, Debug)]
pub enum J {
    Null,
    Bool(bool),
    Int(i128),
    /// An integer outside i128, kept as its decimal text.
    Big(String),
    Float(f64),
    Str(String),
    Arr(Vec<J>),
    Obj(Vec<(String, J)>),
}

impl J {
    pub fn obj() -> J { J::Obj(Vec::new()) }
    pub fn get(&self, key: &str) -> Option<&J> {
        match self { J::Obj(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v), _ => None }
    }
    pub fn get_mut(&mut self, key: &str) -> Option<&mut J> {
        match self { J::Obj(m) => m.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v), _ => None }
    }
    /// `d[key] = value`: replaces in place, else appends.
    pub fn set(&mut self, key: &str, value: J) {
        if let J::Obj(m) = self {
            if let Some(slot) = m.iter_mut().find(|(k, _)| k == key) { slot.1 = value; } else { m.push((key.to_string(), value)); }
        }
    }
    pub fn with(mut self, key: &str, value: J) -> J { self.set(key, value); self }
    pub fn has(&self, key: &str) -> bool { self.get(key).is_some() }
    pub fn as_str(&self) -> Option<&str> { if let J::Str(s) = self { Some(s) } else { None } }
    pub fn as_arr(&self) -> &[J] { if let J::Arr(a) = self { a } else { &[] } }
    pub fn as_i128(&self) -> Option<i128> {
        match self { J::Int(i) => Some(*i), J::Bool(b) => Some(*b as i128), _ => None }
    }
    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            J::Null => false, J::Bool(b) => *b, J::Int(i) => *i != 0, J::Big(_) => true,
            J::Float(f) => *f != 0.0, J::Str(s) => !s.is_empty(), J::Arr(a) => !a.is_empty(), J::Obj(m) => !m.is_empty(),
        }
    }
}

pub fn s(x: impl Into<String>) -> J { J::Str(x.into()) }
pub fn int(x: impl Into<i128>) -> J { J::Int(x.into()) }
pub fn opt_s(x: Option<impl Into<String>>) -> J { x.map_or(J::Null, |v| J::Str(v.into())) }

// ---------------------------------------------------------------- parsing

pub fn parse(text: &str) -> Result<J, String> {
    let mut p = Parser { b: text.as_bytes(), i: 0 };
    p.ws();
    let v = p.value(0)?;
    p.ws();
    if p.i != p.b.len() { return Err(p.fail("Extra data")); }
    Ok(v)
}

struct Parser<'a> { b: &'a [u8], i: usize }

impl Parser<'_> {
    fn fail(&self, what: &str) -> String { format!("{what}: char {}", self.i) }
    fn ws(&mut self) { while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') { self.i += 1; } }
    fn lit(&mut self, word: &str, v: J) -> Result<J, String> {
        if self.b[self.i..].starts_with(word.as_bytes()) { self.i += word.len(); Ok(v) } else { Err(self.fail("Expecting value")) }
    }
    fn value(&mut self, depth: usize) -> Result<J, String> {
        if depth > 10_000 { return Err(self.fail("Nesting too deep")); }
        match self.b.get(self.i) {
            None => Err(self.fail("Expecting value")),
            Some(b'"') => Ok(J::Str(self.string()?)),
            Some(b'{') => {
                self.i += 1; self.ws();
                let mut items: Vec<(String, J)> = Vec::new();
                let mut index: HashMap<String, usize> = HashMap::new();
                if self.b.get(self.i) == Some(&b'}') { self.i += 1; return Ok(J::Obj(items)); }
                loop {
                    self.ws();
                    if self.b.get(self.i) != Some(&b'"') { return Err(self.fail("Expecting property name enclosed in double quotes")); }
                    let k = self.string()?;
                    self.ws();
                    if self.b.get(self.i) != Some(&b':') { return Err(self.fail("Expecting ':' delimiter")); }
                    self.i += 1; self.ws();
                    let v = self.value(depth + 1)?;
                    match index.get(&k) { Some(&at) => items[at].1 = v, None => { index.insert(k.clone(), items.len()); items.push((k, v)); } }
                    self.ws();
                    match self.b.get(self.i) {
                        Some(b',') => { self.i += 1; }
                        Some(b'}') => { self.i += 1; return Ok(J::Obj(items)); }
                        _ => return Err(self.fail("Expecting ',' delimiter")),
                    }
                }
            }
            Some(b'[') => {
                self.i += 1; self.ws();
                let mut items = Vec::new();
                if self.b.get(self.i) == Some(&b']') { self.i += 1; return Ok(J::Arr(items)); }
                loop {
                    self.ws();
                    items.push(self.value(depth + 1)?);
                    self.ws();
                    match self.b.get(self.i) {
                        Some(b',') => { self.i += 1; }
                        Some(b']') => { self.i += 1; return Ok(J::Arr(items)); }
                        _ => return Err(self.fail("Expecting ',' delimiter")),
                    }
                }
            }
            Some(b't') => self.lit("true", J::Bool(true)),
            Some(b'f') => self.lit("false", J::Bool(false)),
            Some(b'n') => self.lit("null", J::Null),
            Some(b'N') => self.lit("NaN", J::Float(f64::NAN)),
            Some(b'I') => self.lit("Infinity", J::Float(f64::INFINITY)),
            Some(b'-') if self.b.get(self.i + 1) == Some(&b'I') => { self.i += 1; self.lit("Infinity", J::Float(f64::NEG_INFINITY)) }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.fail("Expecting value")),
        }
    }
    fn number(&mut self) -> Result<J, String> {
        let start = self.i;
        if self.b[self.i] == b'-' { self.i += 1; }
        let digits = |p: &mut Self| { let s = p.i; while p.i < p.b.len() && p.b[p.i].is_ascii_digit() { p.i += 1; } p.i - s };
        match self.b.get(self.i) {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => { digits(self); }
            _ => return Err(self.fail("Expecting value")),
        }
        let mut float = false;
        if self.b.get(self.i) == Some(&b'.') && self.b.get(self.i + 1).is_some_and(u8::is_ascii_digit) { self.i += 1; digits(self); float = true; }
        if matches!(self.b.get(self.i), Some(b'e' | b'E')) {
            let save = self.i;
            self.i += 1;
            if matches!(self.b.get(self.i), Some(b'+' | b'-')) { self.i += 1; }
            if digits(self) == 0 { self.i = save; } else { float = true; }
        }
        let text = std::str::from_utf8(&self.b[start..self.i]).unwrap();
        if float { return Ok(J::Float(text.parse::<f64>().map_err(|e| e.to_string())?)); }
        Ok(match text.parse::<i128>() {
            Ok(v) => J::Int(v),
            Err(_) => J::Big(text.trim_start_matches('-').to_string()).neg_if(text.starts_with('-')),
        })
    }
    fn hex4(&mut self) -> Result<u32, String> {
        let h = self.b.get(self.i..self.i + 4).and_then(|h| std::str::from_utf8(h).ok()).and_then(|h| u32::from_str_radix(h, 16).ok());
        match h { Some(v) => { self.i += 4; Ok(v) } None => Err(self.fail("Invalid \\uXXXX escape")) }
    }
    fn string(&mut self) -> Result<String, String> {
        self.i += 1;
        let mut out = String::new();
        loop {
            let start = self.i;
            while self.i < self.b.len() && self.b[self.i] != b'"' && self.b[self.i] != b'\\' && self.b[self.i] >= 0x20 { self.i += 1; }
            out.push_str(std::str::from_utf8(&self.b[start..self.i]).map_err(|e| e.to_string())?);
            match self.b.get(self.i) {
                Some(b'"') => { self.i += 1; return Ok(out); }
                Some(b'\\') => {
                    self.i += 1;
                    let c = *self.b.get(self.i).ok_or_else(|| self.fail("Unterminated string"))?;
                    self.i += 1;
                    match c {
                        b'"' => out.push('"'), b'\\' => out.push('\\'), b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'), b'f' => out.push('\u{c}'), b'n' => out.push('\n'),
                        b'r' => out.push('\r'), b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let mut code = hi;
                            if (0xd800..0xdc00).contains(&hi) && self.b.get(self.i..self.i + 2) == Some(b"\\u") {
                                let save = self.i;
                                self.i += 2;
                                let lo = self.hex4()?;
                                if (0xdc00..0xe000).contains(&lo) { code = 0x10000 + ((hi - 0xd800) << 10) + (lo - 0xdc00); } else { self.i = save; }
                            }
                            // Lone surrogates have no Rust char; Python would keep them.
                            out.push(char::from_u32(code).unwrap_or('\u{fffd}'));
                        }
                        _ => return Err(self.fail("Invalid \\escape")),
                    }
                }
                None => return Err(self.fail("Unterminated string")),
                Some(_) => return Err(self.fail("Invalid control character")),
            }
        }
    }
}

impl J {
    fn neg_if(self, neg: bool) -> J { match self { J::Big(d) if neg => J::Big(format!("-{d}")), v => v } }
}

// ---------------------------------------------------------------- output

/// Python's `repr(float)`.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() { return "nan".into(); }
    if x.is_infinite() { return if x > 0.0 { "inf".into() } else { "-inf".into() }; }
    if x == 0.0 { return if x.is_sign_negative() { "-0.0".into() } else { "0.0".into() }; }
    let e = format!("{:e}", x.abs());
    let (mant, exp) = e.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let nd = digits.len() as i32;
    let decpt = exp + 1;
    let body = if decpt > -4 && decpt <= 16 {
        if decpt <= 0 { format!("0.{}{}", "0".repeat((-decpt) as usize), digits) }
        else if decpt >= nd { format!("{}{}.0", digits, "0".repeat((decpt - nd) as usize)) }
        else { format!("{}.{}", &digits[..decpt as usize], &digits[decpt as usize..]) }
    } else {
        let m = if nd == 1 { digits.clone() } else { format!("{}.{}", &digits[..1], &digits[1..]) };
        format!("{}e{}{:02}", m, if exp < 0 { '-' } else { '+' }, exp.abs())
    };
    if x < 0.0 { format!("-{body}") } else { body }
}

fn json_float(x: f64) -> String {
    if x.is_nan() { "NaN".into() } else if x == f64::INFINITY { "Infinity".into() } else if x == f64::NEG_INFINITY { "-Infinity".into() } else { float_repr(x) }
}

/// A JSON string literal with `ensure_ascii=True`.
pub fn quote(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""), '\\' => out.push_str("\\\\"), '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"), '\t' => out.push_str("\\t"), '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) { let _ = write!(out, "\\u{:04x}", unit); }
            }
        }
    }
    out.push('"');
}

fn scalar(out: &mut String, v: &J) -> bool {
    match v {
        J::Null => out.push_str("null"),
        J::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        J::Int(i) => { let _ = write!(out, "{i}"); }
        J::Big(d) => out.push_str(d),
        J::Float(f) => out.push_str(&json_float(*f)),
        J::Str(s) => quote(out, s),
        _ => return false,
    }
    true
}

fn dump(out: &mut String, v: &J, sort: bool, item: &str, colon: &str) {
    if scalar(out, v) { return; }
    match v {
        J::Arr(a) => {
            out.push('[');
            for (k, x) in a.iter().enumerate() { if k > 0 { out.push_str(item); } dump(out, x, sort, item, colon); }
            out.push(']');
        }
        J::Obj(m) => {
            out.push('{');
            let mut entries: Vec<&(String, J)> = m.iter().collect();
            if sort { entries.sort_by(|a, b| a.0.cmp(&b.0)); }
            for (k, (key, x)) in entries.into_iter().enumerate() {
                if k > 0 { out.push_str(item); }
                quote(out, key); out.push_str(colon); dump(out, x, sort, item, colon);
            }
            out.push('}');
        }
        _ => unreachable!(),
    }
}

/// `json.dumps(v, ensure_ascii=True, sort_keys=True, separators=(',', ':'))`.
pub fn canonical(v: &J) -> String { let mut o = String::new(); dump(&mut o, v, true, ",", ":"); o }
/// `json.dumps(v)`.
pub fn default(v: &J) -> String { let mut o = String::new(); dump(&mut o, v, false, ", ", ": "); o }
/// `json.dumps(v, indent=2)`.
pub fn indent(v: &J) -> String { let mut o = String::new(); dump_indent(&mut o, v, 0); o }

fn dump_indent(out: &mut String, v: &J, depth: usize) {
    if scalar(out, v) { return; }
    let pad = |out: &mut String, d: usize| { out.push('\n'); for _ in 0..d { out.push_str("  "); } };
    match v {
        J::Arr(a) if a.is_empty() => out.push_str("[]"),
        J::Obj(m) if m.is_empty() => out.push_str("{}"),
        J::Arr(a) => {
            out.push('[');
            for (k, x) in a.iter().enumerate() { if k > 0 { out.push(','); } pad(out, depth + 1); dump_indent(out, x, depth + 1); }
            pad(out, depth); out.push(']');
        }
        J::Obj(m) => {
            out.push('{');
            for (k, (key, x)) in m.iter().enumerate() {
                if k > 0 { out.push(','); }
                pad(out, depth + 1); quote(out, key); out.push_str(": "); dump_indent(out, x, depth + 1);
            }
            pad(out, depth); out.push('}');
        }
        _ => unreachable!(),
    }
}

// ---------------------------------------------------------------- Python semantics

/// Python `==` on json.loads values (1 == 1.0 == True).
pub fn py_eq(a: &J, b: &J) -> bool {
    use J::*;
    let num = |v: &J| -> Option<Result<i128, f64>> {
        match v { Int(i) => Some(Ok(*i)), Bool(x) => Some(Ok(*x as i128)), Float(f) => Some(Err(*f)), _ => None }
    };
    if let (Some(x), Some(y)) = (num(a), num(b)) {
        return match (x, y) {
            (Ok(i), Ok(j)) => i == j,
            (Err(f), Err(g)) => f == g,
            (Ok(i), Err(f)) | (Err(f), Ok(i)) => f.is_finite() && f.fract() == 0.0 && f.abs() < 1.7e38 && f as i128 == i,
        };
    }
    match (a, b) {
        (Null, Null) => true,
        (Big(x), Big(y)) => x == y,
        (Str(x), Str(y)) => x == y,
        (Arr(x), Arr(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| py_eq(p, q)),
        (Obj(x), Obj(y)) => x.len() == y.len() && x.iter().all(|(k, v)| b.get(k).is_some_and(|w| py_eq(v, w))),
        _ => false,
    }
}

/// Python `repr(str)`.
pub fn py_repr_str(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::new();
    out.push(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"), '\n' => out.push_str("\\n"), '\r' => out.push_str("\\r"), '\t' => out.push_str("\\t"),
            c if c == q => { out.push('\\'); out.push(c); }
            c if (c as u32) < 0x20 || (0x7f..=0xa0).contains(&(c as u32)) => { let _ = write!(out, "\\x{:02x}", c as u32); }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

/// Python `repr(bytes)`.
pub fn py_repr_bytes(b: &[u8]) -> String {
    let q = if b.contains(&b'\'') && !b.contains(&b'"') { '"' } else { '\'' };
    let mut out = String::from("b");
    out.push(q);
    for &c in b {
        match c {
            b'\\' => out.push_str("\\\\"), b'\n' => out.push_str("\\n"), b'\r' => out.push_str("\\r"), b'\t' => out.push_str("\\t"),
            c if c as char == q => { out.push('\\'); out.push(c as char); }
            0x20..=0x7e => out.push(c as char),
            c => { let _ = write!(out, "\\x{c:02x}"); }
        }
    }
    out.push(q);
    out
}

fn py_repr(v: &J) -> String {
    match v {
        J::Str(s) => py_repr_str(s),
        J::Arr(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        J::Obj(m) => format!("{{{}}}", m.iter().map(|(k, x)| format!("{}: {}", py_repr_str(k), py_repr(x))).collect::<Vec<_>>().join(", ")),
        other => py_str(other),
    }
}

/// Python `str(v)`.
pub fn py_str(v: &J) -> String {
    match v {
        J::Null => "None".into(), J::Bool(true) => "True".into(), J::Bool(false) => "False".into(),
        J::Int(i) => i.to_string(), J::Big(d) => d.clone(), J::Float(f) => float_repr(*f), J::Str(s) => s.clone(),
        other => py_repr(other),
    }
}

/// Python `f'{n:,}'`.
pub fn thousands(n: i128) -> String {
    let d = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (k, c) in d.chars().enumerate() { if k > 0 && (d.len() - k) % 3 == 0 { out.push(','); } out.push(c); }
    if n < 0 { format!("-{out}") } else { out }
}

/// Python `round(x, 2)` as a float.
pub fn round2(x: f64) -> f64 { format!("{x:.2}").parse().unwrap_or(x) }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn floats_print_as_python() {
        for (x, want) in [(1e-5, "1e-05"), (1e16, "1e+16"), (0.1, "0.1"), (1.0, "1.0"), (123.456, "123.456"),
                          (0.0001, "0.0001"), (1234567890123456.0, "1234567890123456.0"), (-2.5e-7, "-2.5e-07"),
                          (1.5e300, "1.5e+300"), (13.59, "13.59"), (-0.0, "-0.0"), (5e-324, "5e-324")] {
            assert_eq!(float_repr(x), want);
        }
    }
    #[test]
    fn canonical_matches_python() {
        let v = parse(r#"{"b":[1,2.0,"é\u007f😀"],"a":{"y":null,"x":true},"b":-0,"c":1E2}"#).unwrap();
        assert_eq!(canonical(&v), r#"{"a":{"x":true,"y":null},"b":0,"c":100.0}"#);
        let v = parse("[\"\u{e9}\\u007f\\ud83d\\ude00\\n\"]").unwrap();
        assert_eq!(canonical(&v), "[\"\\u00e9\\u007f\\ud83d\\ude00\\n\"]");
        assert_eq!(default(&parse(r#"{"a":[1,{}],"b":"x"}"#).unwrap()), r#"{"a": [1, {}], "b": "x"}"#);
        assert_eq!(indent(&parse(r#"{"a":[1,[]],"b":{}}"#).unwrap()), "{\n  \"a\": [\n    1,\n    []\n  ],\n  \"b\": {}\n}");
        assert_eq!(thousands(1234567), "1,234,567");
        assert!(py_eq(&J::Int(1), &J::Float(1.0)) && py_eq(&J::Bool(true), &J::Int(1)) && !py_eq(&J::Int(1), &s("1")));
    }
}

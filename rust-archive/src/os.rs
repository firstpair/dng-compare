//! File-system facts in the shapes compare_dng.py and preserve_apo.py record,
//! and Python's OSError texts.

use crate::pyjson::{J, int, py_repr_str};
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::fs;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

unsafe extern "C" {
    fn strerror(errnum: c_int) -> *const c_char;
    fn flock(fd: c_int, operation: c_int) -> c_int;
    fn signal(sig: c_int, handler: usize) -> usize;
}

#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn listxattr(path: *const c_char, namebuf: *mut c_char, size: usize, options: c_int) -> isize;
    fn getxattr(path: *const c_char, name: *const c_char, value: *mut c_void, size: usize, position: u32, options: c_int) -> isize;
    fn setxattr(path: *const c_char, name: *const c_char, value: *const c_void, size: usize, position: u32, options: c_int) -> c_int;
}
#[cfg(target_os = "linux")]
unsafe extern "C" {
    fn llistxattr(path: *const c_char, list: *mut c_char, size: usize) -> isize;
    fn lgetxattr(path: *const c_char, name: *const c_char, value: *mut c_void, size: usize) -> isize;
    #[link_name = "setxattr"]
    fn setxattr_linux(path: *const c_char, name: *const c_char, value: *const c_void, size: usize, flags: c_int) -> c_int;
}

/// Python's `str(OSError)`: `[Errno N] message: 'path'`.
pub fn os_error(e: &io::Error, path: Option<&str>) -> String {
    match e.raw_os_error() {
        Some(code) => {
            let msg = unsafe { CStr::from_ptr(strerror(code)) }.to_string_lossy().into_owned();
            match path { Some(p) => format!("[Errno {code}] {msg}: {}", py_repr_str(p)), None => format!("[Errno {code}] {msg}") }
        }
        None => e.to_string(),
    }
}

fn ns(sec: i64, nsec: i64) -> i128 { sec as i128 * 1_000_000_000 + nsec as i128 }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stat { pub dev: i128, pub ino: u64, pub size: u64, pub mtime_ns: i128, pub ctime_ns: i128 }

pub fn stat(path: &str) -> Result<Stat, String> {
    let m = fs::metadata(path).map_err(|e| os_error(&e, Some(path)))?;
    Ok(Stat { dev: m.dev() as i64 as i128, ino: m.ino(), size: m.size(),
              mtime_ns: ns(m.mtime(), m.mtime_nsec()), ctime_ns: ns(m.ctime(), m.ctime_nsec()) })
}

impl Stat {
    /// compare_dng.stamp
    pub fn stamp(&self) -> J { J::Arr(vec![J::Int(self.dev), int(self.ino), int(self.size), J::Int(self.mtime_ns), J::Int(self.ctime_ns)]) }
    /// preserve_apo.identity
    pub fn identity(&self) -> J { J::Arr(vec![int(self.ino), int(self.size), J::Int(self.mtime_ns), J::Int(self.ctime_ns)]) }
}

pub fn is_file(path: &str) -> bool { fs::metadata(path).is_ok_and(|m| m.is_file()) }

fn cpath(path: &str) -> CString { CString::new(Path::new(path).as_os_str().as_bytes()).unwrap_or_default() }

fn xattr_error(path: &str) -> String { os_error(&io::Error::last_os_error(), Some(path)) }

/// Names and values of a path's extended attributes, not following symlinks, in the OS's order.
pub fn xattrs(path: &str) -> Result<Vec<(String, Vec<u8>)>, String> {
    let p = cpath(path);
    #[cfg(target_os = "macos")]
    let list = |buf: *mut c_char, n: usize| unsafe { listxattr(p.as_ptr(), buf, n, 1) };
    #[cfg(target_os = "linux")]
    let list = |buf: *mut c_char, n: usize| unsafe { llistxattr(p.as_ptr(), buf, n) };
    let n = list(std::ptr::null_mut(), 0);
    if n < 0 { return Err(xattr_error(path)); }
    if n == 0 { return Ok(Vec::new()); }
    let mut buf = vec![0u8; n as usize];
    let n = list(buf.as_mut_ptr() as *mut c_char, buf.len());
    if n < 0 { return Err(xattr_error(path)); }
    let mut out = Vec::new();
    for name in buf[..n as usize].split(|b| *b == 0).filter(|x| !x.is_empty()) {
        let cname = CString::new(name).unwrap();
        #[cfg(target_os = "macos")]
        let get = |v: *mut c_void, n: usize| unsafe { getxattr(p.as_ptr(), cname.as_ptr(), v, n, 0, 1) };
        #[cfg(target_os = "linux")]
        let get = |v: *mut c_void, n: usize| unsafe { lgetxattr(p.as_ptr(), cname.as_ptr(), v, n) };
        let size = get(std::ptr::null_mut(), 0);
        if size < 0 { return Err(xattr_error(path)); }
        let mut value = vec![0u8; size as usize];
        let got = if size == 0 { 0 } else { get(value.as_mut_ptr() as *mut c_void, value.len()) };
        if got < 0 { return Err(xattr_error(path)); }
        value.truncate(got as usize);
        out.push((String::from_utf8_lossy(name).into_owned(), value));
    }
    Ok(out)
}

pub fn set_xattr(path: &str, name: &str, value: &[u8]) -> Result<(), String> {
    let p = cpath(path);
    let n = CString::new(name).map_err(|e| e.to_string())?;
    #[cfg(target_os = "macos")]
    let rc = unsafe { setxattr(p.as_ptr(), n.as_ptr(), value.as_ptr() as *const c_void, value.len(), 0, 0) };
    #[cfg(target_os = "linux")]
    let rc = unsafe { setxattr_linux(p.as_ptr(), n.as_ptr(), value.as_ptr() as *const c_void, value.len(), 0) };
    if rc < 0 { Err(xattr_error(path)) } else { Ok(()) }
}

pub fn base64(data: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | b[2] as u32;
        for k in 0..4 {
            if k <= chunk.len() { out.push(A[(n >> (18 - 6 * k) & 63) as usize] as char); } else { out.push('='); }
        }
    }
    out
}

pub fn unbase64(text: &str) -> Vec<u8> {
    let val = |c: u8| match c { b'A'..=b'Z' => c - b'A', b'a'..=b'z' => c - b'a' + 26, b'0'..=b'9' => c - b'0' + 52, b'+' => 62, _ => 63 };
    let clean: Vec<u8> = text.bytes().filter(|c| *c != b'=').collect();
    let mut out = Vec::new();
    for chunk in clean.chunks(4) {
        let mut n = 0u32;
        for (k, c) in chunk.iter().enumerate() { n |= (val(*c) as u32) << (18 - 6 * k); }
        for k in 0..chunk.len().saturating_sub(1) { out.push((n >> (16 - 8 * k)) as u8); }
    }
    out
}

/// preserve_apo.attrs
pub fn attrs(path: &str) -> Result<J, String> {
    let m = fs::symlink_metadata(path).map_err(|e| os_error(&e, Some(path)))?;
    #[cfg(target_os = "macos")]
    let flags = { use std::os::macos::fs::MetadataExt as _; m.st_flags() as i128 };
    #[cfg(not(target_os = "macos"))]
    let flags = 0i128;
    let x = xattrs(path)?;
    Ok(J::obj().with("mode", int(m.mode() & 0o7777)).with("uid", int(m.uid())).with("gid", int(m.gid()))
        .with("mtime_ns", J::Int(ns(m.mtime(), m.mtime_nsec()))).with("atime_ns", J::Int(ns(m.atime(), m.atime_nsec())))
        .with("flags", J::Int(flags))
        .with("xattrs", J::Obj(x.into_iter().map(|(n, v)| (n, J::Str(base64(&v)))).collect())))
}

/// An exclusive, non-blocking flock held as long as the returned file lives.
pub fn try_lock(path: &Path, truncate: bool) -> io::Result<Option<fs::File>> {
    use std::os::fd::AsRawFd;
    let f = fs::OpenOptions::new().create(true).write(true).append(!truncate).truncate(truncate).open(path)?;
    if unsafe { flock(f.as_raw_fd(), 2 | 4) } != 0 { return Ok(None); }
    Ok(Some(f))
}

pub static INTERRUPTED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
extern "C" fn on_sigint(_: c_int) { INTERRUPTED.store(true, std::sync::atomic::Ordering::SeqCst); }
pub fn catch_sigint() { unsafe { signal(2, on_sigint as *const () as usize); } }

/// os.path.realpath for a directory that may not exist.
pub fn realpath(path: &str) -> String {
    if let Ok(p) = fs::canonicalize(path) { return p.to_string_lossy().into_owned(); }
    let mut parts: Vec<&str> = Vec::new();
    for c in path.split('/') {
        match c { "" | "." => {}, ".." => { parts.pop(); }, c => parts.push(c) }
    }
    format!("/{}", parts.join("/"))
}

/// Path.resolve() (non-strict) of an absolute or relative path.
pub fn resolve(path: &str) -> String {
    let abs = if path.starts_with('/') { path.to_string() } else {
        format!("{}/{}", std::env::current_dir().map(|d| d.to_string_lossy().into_owned()).unwrap_or_default(), path)
    };
    if let Ok(p) = fs::canonicalize(&abs) { return p.to_string_lossy().into_owned(); }
    match abs.rsplit_once('/') {
        Some((parent, name)) if !parent.is_empty() => {
            let parent = resolve(parent);
            if parent == "/" { format!("/{name}") } else { format!("{parent}/{name}") }
        }
        _ => realpath(&abs),
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn base64_round_trips() {
        for data in [&b""[..], b"k", b"ke", b"kept\x00bytes"] {
            assert_eq!(super::unbase64(&super::base64(data)), data);
        }
        assert_eq!(super::base64(b"kept\x00bytes"), "a2VwdABieXRlcw==");
    }
}

//! phindex indexes of the source and reference trees, read-only: a pair whose
//! current rows have equal SHA-256 is identical without reading either file
//! (preserve_apo.indexed_sha256), and a pair can be settled from its rows
//! (compare_dng.PhindexIndexes.settle). The OCaml library's Phindex.

use crate::os;
use crate::pyjson::{J, s};
use rusqlite::{Connection, OpenFlags, types::Value};
use std::sync::Mutex;

pub struct Indexes { dbs: [Mutex<Connection>; 2] }

impl Indexes {
    pub fn open(left: &str, right: &str) -> Result<Indexes, String> {
        let one = |p: &str| -> Result<Mutex<Connection>, String> {
            let uri = format!("file:{}?immutable=1", os::resolve(p));
            Connection::open_with_flags(uri, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_NO_MUTEX)
                .map(Mutex::new).map_err(|e| e.to_string())
        };
        Ok(Indexes { dbs: [one(left)?, one(right)?] })
    }

    fn row(&self, side: usize, sql: &str, path: &str, size: u64, mtime_ns: i128) -> Option<Vec<Value>> {
        let db = self.dbs[side].lock().unwrap();
        let row: Vec<Value> = db.query_row(sql, [path], |r| (0..r.as_ref().column_count()).map(|i| r.get::<_, Value>(i)).collect()).ok()?;
        let num = |v: &Value, want: i128| match v { Value::Integer(i) => *i as i128 == want, Value::Real(f) => *f == want as f64, _ => false };
        (truthy(&row[2]) && num(&row[0], size as i128) && num(&row[1], mtime_ns)).then_some(row)
    }

    /// preserve_apo.indexed_sha256
    pub fn sha256(&self, side: usize, path: &str, st: &os::Stat) -> Option<String> {
        let row = self.row(side, "SELECT size, mtime_ns, sha256 FROM files WHERE path=? AND gone=0", path, st.size, st.mtime_ns)?;
        match &row[2] { Value::Text(t) => Some(t.clone()), _ => None }
    }

    /// compare_dng.PhindexIndexes.settle
    pub fn settle(&self, pair: &(String, String), stamps: &J, compare_only: bool) -> Option<J> {
        let st = stamps.as_arr();
        let rows: Vec<Option<Vec<Value>>> = [&pair.0, &pair.1].iter().enumerate().map(|(i, p)| {
            let s = st.get(i)?.as_arr();
            self.row(i, "SELECT size, mtime_ns, sha256, dng_blocks FROM files WHERE path=? AND gone=0", p,
                     s.get(2)?.as_i128()? as u64, s.get(3)?.as_i128()?)
        }).collect();
        let (Some(a), Some(b)) = (&rows[0], &rows[1]) else { return None };
        let result = J::obj().with("left", s(pair.0.as_str())).with("right", s(pair.1.as_str())).with("stamps", stamps.clone());
        if a[2] == b[2] {
            let settled = J::obj().with("status", s("equal")).with("method", s("phindex_sha256"));
            return Some(result.with("raw", settled.clone()).with("auxiliary", settled));
        }
        if compare_only && truthy(&a[3]) && a[3] == b[3] {
            return Some(result.with("raw", J::obj().with("status", s("equal")).with("method", s("phindex_dng_blocks")))
                .with("auxiliary", J::obj().with("status", s("not_compared"))));
        }
        None
    }
}

fn truthy(v: &Value) -> bool {
    match v { Value::Null => false, Value::Integer(i) => *i != 0, Value::Real(f) => *f != 0.0, Value::Text(t) => !t.is_empty(), Value::Blob(b) => !b.is_empty() }
}

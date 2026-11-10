//! phindex indexes of the source and reference trees, read-only: a pair whose
//! current rows have equal SHA-256 is identical without reading either file
//! (preserve_apo.indexed_sha256), and a pair can be settled from its rows
//! (compare_dng.PhindexIndexes.settle). The OCaml library's Phindex.
//!
//! [`Index`] is one index asked where a content is now: the live files it
//! lists with a SHA-256, and the files that share an edit-surviving identity
//! (`dng_blocks`, `image_data`) with a content or a path. Its answers are
//! hints; the reader checks every file it is pointed at by digest.

use crate::os;
use crate::pyjson::{J, s};
use rusqlite::{Connection, OpenFlags, types::Value};
use std::path::Path;
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

/// The identities that survive a metadata edit, as columns of `files`.
const IDENTITIES: [&str; 2] = ["dng_blocks", "image_data"];

const HOLDING: &str = "SELECT path, size, sha256 FROM files WHERE sha256=?1 AND gone=0 LIMIT ?2";

/// A file an index lists: its path, and the size and SHA-256 of its row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Listed { pub path: String, pub size: u64, pub sha256: String }

/// One phindex index, opened read-only with immutable=1 (nothing is written
/// beside it, not even -wal or -shm). Every lookup is a point query on an
/// index of `files`; a lookup the database has no index for returns nothing
/// rather than scanning (`unindexed` names it).
pub struct Index { db: Connection, by_sha256: bool, identities: Vec<&'static str>, unindexed: Vec<&'static str> }

fn sql(e: rusqlite::Error) -> String { e.to_string() }

impl Index {
    pub fn open(path: impl AsRef<Path>) -> Result<Index, String> {
        let path = path.as_ref();
        if !path.is_file() { return Err(format!("no phindex index at {}", path.display())); }
        // In a URI filename % ? and # are syntax.
        let name = os::resolve(&path.to_string_lossy()).replace('%', "%25").replace('?', "%3f").replace('#', "%23");
        let db = Connection::open_with_flags(format!("file:{name}?immutable=1"),
                                             OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI).map_err(sql)?;
        let names = |q: &str| -> Result<Vec<String>, String> {
            db.prepare(q).map_err(sql)?.query_map([], |r| r.get(0)).map_err(sql)?.collect::<Result<_, _>>().map_err(sql)
        };
        let columns = names("SELECT name FROM pragma_table_info('files')")?;
        if !["path", "size", "sha256", "gone"].iter().all(|c| columns.iter().any(|n| n == c)) {
            return Err(format!("{} is not a phindex index", path.display()));
        }
        // The columns an index of files starts with: only those are looked up.
        let leading = names("SELECT i.name FROM pragma_index_list('files') l, pragma_index_info(l.name) i WHERE i.seqno=0")?;
        let indexed = |c: &str| leading.iter().any(|n| n == c);
        let mut unindexed: Vec<&'static str> = if indexed("sha256") { Vec::new() } else { vec!["sha256"] };
        let mut identities = Vec::new();
        for c in IDENTITIES {
            if !columns.iter().any(|n| n == c) { continue; }
            if indexed(c) { identities.push(c); } else { unindexed.push(c); }
        }
        Ok(Index { by_sha256: indexed("sha256"), db, identities, unindexed })
    }

    /// Columns this index has no index for, so they are not looked up.
    pub fn unindexed(&self) -> &[&'static str] { &self.unindexed }

    fn listed(&self, query: &str, key: &str, limit: usize) -> Result<Vec<Listed>, String> {
        let mut st = self.db.prepare_cached(query).map_err(sql)?;
        let rows = st.query_map(rusqlite::params![key, limit as i64], |r| {
            Ok(Listed { path: r.get(0)?, size: r.get::<_, i64>(1)? as u64, sha256: r.get::<_, Option<String>>(2)?.unwrap_or_default() })
        }).map_err(sql)?;
        rows.collect::<Result<_, _>>().map_err(sql)
    }

    /// The identity columns of rows chosen by `key` (sha256 or path); NULL
    /// for a column this index does not answer for.
    fn identity_query(&self, key: &str) -> String {
        let columns = IDENTITIES.map(|c| if self.identities.contains(&c) { c } else { "NULL" }).join(", ");
        format!("SELECT {columns} FROM files WHERE {key}=?1 LIMIT ?2")
    }

    fn sharing_query(column: &str) -> String { format!("SELECT path, size, sha256 FROM files WHERE {column}=?1 AND gone=0 LIMIT ?2") }

    /// Live files (not gone) listed with this SHA-256, at most `limit`.
    pub fn holding(&self, sha256: &str, limit: usize) -> Result<Vec<Listed>, String> {
        if !self.by_sha256 { return Ok(Vec::new()); }
        self.listed(HOLDING, sha256, limit)
    }

    /// The edit-surviving identities, as (column, value), of the rows listed
    /// with this SHA-256, gone or not (a gone row keeps its digests), and of
    /// the rows at `paths`, whatever content they list now (a row whose file
    /// was edited in place has a new SHA-256 and the same identity).
    pub fn identities(&self, sha256: &str, paths: &[&str], limit: usize) -> Result<Vec<(&'static str, String)>, String> {
        let mut out: Vec<(&'static str, String)> = Vec::new();
        if self.identities.is_empty() { return Ok(out); }
        let mut take = |query: String, key: &str| -> Result<(), String> {
            let mut st = self.db.prepare_cached(&query).map_err(sql)?;
            let mut rows = st.query(rusqlite::params![key, limit as i64]).map_err(sql)?;
            while let Some(r) = rows.next().map_err(sql)? {
                for (i, c) in IDENTITIES.into_iter().enumerate() {
                    let v = r.get::<_, Option<String>>(i).map_err(sql)?.filter(|v| !v.is_empty());
                    if let Some(v) = v.filter(|v| !out.contains(&(c, v.clone()))) { out.push((c, v)); }
                }
            }
            Ok(())
        };
        if self.by_sha256 { take(self.identity_query("sha256"), sha256)?; }
        for p in paths { take(self.identity_query("path"), p)?; }
        Ok(out)
    }

    /// Live files listed with this identity, at most `limit`.
    pub fn sharing(&self, column: &str, value: &str, limit: usize) -> Result<Vec<Listed>, String> {
        let Some(c) = self.identities.iter().find(|c| **c == column) else { return Ok(Vec::new()) };
        self.listed(&Index::sharing_query(c), value, limit)
    }

    /// Each lookup this index answers, with its EXPLAIN QUERY PLAN.
    pub fn plans(&self) -> Result<Vec<(String, String)>, String> {
        let mut queries = Vec::new();
        if self.by_sha256 { queries.push(HOLDING.to_string()); }
        if !self.identities.is_empty() {
            if self.by_sha256 { queries.push(self.identity_query("sha256")); }
            queries.push(self.identity_query("path"));
        }
        queries.extend(self.identities.iter().map(|c| Index::sharing_query(c)));
        queries.into_iter().map(|q| {
            let plan: Vec<String> = self.db.prepare(&format!("EXPLAIN QUERY PLAN {q}")).map_err(sql)?
                .query_map(rusqlite::params!["", 1], |r| r.get(3)).map_err(sql)?.collect::<Result<_, _>>().map_err(sql)?;
            Ok((q, plan.join("; ")))
        }).collect()
    }
}

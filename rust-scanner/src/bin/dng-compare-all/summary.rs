//! compare_dng.Summary, with Python's Counter and dict insertion orders.

use crate::pyjson::{J, indent, round2, s};
use std::collections::HashMap;
use std::path::Path;

#[derive(Default)]
pub struct Counter { keys: Vec<String>, at: HashMap<String, usize>, values: Vec<i128> }

impl Counter {
    pub fn add(&mut self, key: &str, n: i128) {
        match self.at.get(key) {
            Some(&i) => self.values[i] += n,
            None => { self.at.insert(key.to_string(), self.keys.len()); self.keys.push(key.to_string()); self.values.push(n); }
        }
    }
    pub fn get(&self, key: &str) -> i128 { self.at.get(key).map_or(0, |&i| self.values[i]) }
    fn json(&self) -> J { J::Obj(self.keys.iter().zip(&self.values).map(|(k, v)| (k.clone(), J::Int(*v))).collect()) }
}

#[derive(Default)]
pub struct Summary {
    pub counts: Counter,
    tags: Vec<(String, i128, Vec<J>)>,
    tag_at: HashMap<String, usize>,
    patterns: Vec<(Vec<String>, i128)>,
    pattern_at: HashMap<Vec<String>, usize>,
    bytes_read: i128,
    coverage: Vec<(String, [bool; 3])>,
    coverage_at: HashMap<String, usize>,
}

fn status(v: Option<&J>) -> Option<&str> { v.and_then(|g| g.get("status")).and_then(J::as_str) }

impl Summary {
    pub fn covered_raw(&self, left: &str) -> bool { self.coverage_at.get(left).is_some_and(|&i| self.coverage[i].1[0]) }

    pub fn add(&mut self, r: &J) {
        let left = r.get("left").and_then(J::as_str).unwrap_or("").to_string();
        let i = *self.coverage_at.entry(left.clone()).or_insert_with(|| { self.coverage.push((left.clone(), [false; 3])); self.coverage.len() - 1 });
        let raw_status = status(r.get("raw")).unwrap_or("");
        let raw_equal = raw_status == "equal";
        let aux = status(r.get("auxiliary"));
        let meta_status = r.get("metadata_status").and_then(J::as_str);
        let warnings = r.get("metadata_warnings").is_some_and(J::truthy);
        let embedded: &[J] = r.get("differences").and_then(|d| d.get("embedded")).map_or(&[], J::as_arr);
        let c = &mut self.coverage[i].1;
        c[0] |= raw_equal;
        c[1] |= raw_equal && aux == Some("equal");
        c[2] |= raw_equal && meta_status == Some("ok") && !warnings
            && !embedded.iter().any(|x| x.get("kind").and_then(J::as_str) != Some("added"));
        self.counts.add("pairs_reported", 1);
        self.counts.add(&format!("raw_{raw_status}"), 1);
        self.counts.add(&format!("auxiliary_{}", aux.unwrap_or("unverified")), 1);
        self.counts.add(&format!("metadata_{}", meta_status.unwrap_or("error")), 1);
        if warnings { self.counts.add("metadata_with_warnings", 1); }
        for group in ["raw", "auxiliary"] {
            if let Some(images) = r.get(group).and_then(|g| g.get("images")) {
                self.bytes_read += images.as_arr().iter().map(|p| p.get("bytes_read").and_then(J::as_i128).unwrap_or(0)).sum::<i128>();
            }
        }
        if !embedded.is_empty() { self.counts.add("embedded_metadata_different", 1); }
        if meta_status == Some("ok") {
            let pattern: Vec<String> = embedded.iter().map(|c| format!("{}:{}",
                c.get("kind").and_then(J::as_str).unwrap_or(""), c.get("tag").and_then(J::as_str).unwrap_or(""))).collect();
            match self.pattern_at.get(&pattern) {
                Some(&k) => self.patterns[k].1 += 1,
                None => { self.pattern_at.insert(pattern.clone(), self.patterns.len()); self.patterns.push((pattern, 1)); }
            }
        }
        if let Some(J::Obj(cats)) = r.get("differences") {
            for (category, changes) in cats {
                for change in changes.as_arr() {
                    let key = format!("{category}/{}:{}", change.get("kind").and_then(J::as_str).unwrap_or(""),
                                      change.get("tag").and_then(J::as_str).unwrap_or(""));
                    let k = *self.tag_at.entry(key.clone()).or_insert_with(|| { self.tags.push((key, 0, vec![])); self.tags.len() - 1 });
                    let record = &mut self.tags[k];
                    record.1 += 1;
                    if record.2.len() < 3 { record.2.push(change.clone().with("file", s(left.as_str()))); }
                }
            }
        }
    }

    pub fn write(&self, path: &Path, context: &J, left_files: usize, complete: bool, elapsed: f64) -> std::io::Result<()> {
        let names = ["raw", "all_images", "metadata_preserved"];
        let covered: Vec<i128> = (0..3).map(|k| self.coverage.iter().filter(|c| c.1[k]).count() as i128).collect();
        let mut report = context.clone().with("complete", J::Bool(complete)).with("elapsed_seconds", J::Float(round2(elapsed)));
        report.set("left_files_covered", J::Obj(names.iter().zip(&covered).map(|(n, c)| (n.to_string(), J::Int(*c))).collect()));
        report.set("left_subsumed_by_right", J::Obj(names.iter().zip(&covered).map(|(n, c)| (n.to_string(), J::Bool(*c == left_files as i128))).collect()));
        report.set("counts", self.counts.json());
        report.set("verified_payload_bytes", J::Int(self.bytes_read));
        let mut patterns: Vec<&(Vec<String>, i128)> = self.patterns.iter().collect();
        patterns.sort_by(|a, b| b.1.cmp(&a.1));
        report.set("metadata_patterns", J::Arr(patterns.into_iter().map(|(p, n)| J::obj().with("count", J::Int(*n))
            .with("changes", J::Arr(p.iter().map(|x| s(x.as_str())).collect()))).collect()));
        let mut tags: Vec<&(String, i128, Vec<J>)> = self.tags.iter().collect();
        tags.sort_by(|a, b| b.1.cmp(&a.1));
        report.set("metadata_tags", J::Obj(tags.into_iter().map(|(k, n, ex)| (k.clone(),
            J::obj().with("count", J::Int(*n)).with("examples", J::Arr(ex.clone())))).collect()));
        let temp = path.with_extension("tmp");
        std::fs::write(&temp, indent(&report) + "\n")?;
        std::fs::rename(temp, path)
    }
}

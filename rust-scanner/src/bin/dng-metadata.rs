use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const EXCLUDE_IMAGE: &[&str] = &["PreviewImage", "JpgFromRaw", "ThumbnailImage", "OtherImage",
    "RawImage", "OriginalRawFileData"];
const LAYOUT: &[&str] = &["StripOffsets", "StripByteCounts", "TileOffsets", "TileByteCounts",
    "PreviewImageStart", "PreviewImageLength", "JpgFromRawStart", "JpgFromRawLength",
    "ThumbnailOffset", "ThumbnailLength", "FreeOffsets", "FreeByteCounts"];

fn hex_decode(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 { return None }
    s.as_bytes().chunks(2).map(|c| {
        u8::from_str_radix(std::str::from_utf8(c).ok()?, 16).ok()
    }).collect()
}
fn load_pairs(path: &Path, pairs: &mut BTreeMap<PathBuf, PathBuf>) -> Result<()> {
    let input = BufReader::new(File::open(path)?);
    for line in input.lines() {
        let line=line?;
        let columns:Vec<_>=line.split('\t').collect();
        if columns.len()!=4 {continue}
        if let (Some(a), Some(b))=(hex_decode(columns[0]),hex_decode(columns[1])) {
            pairs.insert(PathBuf::from(std::ffi::OsString::from_vec(a)),
                PathBuf::from(std::ffi::OsString::from_vec(b)));
        }
    }
    Ok(())
}
fn normalize_key(key: &str) -> String {
    key.split(':').filter(|part| {
        !part.strip_prefix("Copy").is_some_and(|digits|
            !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
    }).collect::<Vec<_>>().join(":")
}
fn flatten(prefix: &str, value: &Value, output: &mut BTreeMap<String, Value>) {
    if let Value::Object(map)=value {
        for (key, child) in map {
            flatten(&format!("{prefix}/{key}"), child, output);
        }
    } else {
        output.insert(prefix.to_string(), value.clone());
    }
}
fn parse_tags(value: &Value) -> Result<(String, BTreeMap<String, Value>, Vec<String>, Option<String>)> {
    let map=value.as_object().ok_or("ExifTool returned a non-object record")?;
    let path=map.get("SourceFile").and_then(Value::as_str)
        .ok_or("ExifTool record has no SourceFile")?.to_string();
    let mut grouped:BTreeMap<String,Vec<Value>>=BTreeMap::new();
    let mut warnings=Vec::new();let mut error=None;
    for (key, val) in map {
        if key=="SourceFile" {continue}
        match key.rsplit(':').next().unwrap_or(key) {
            "Warning"=>{warnings.push(val.to_string());continue}
            "Error"=>{error=Some(val.to_string());continue}
            _=>()
        }
        grouped.entry(normalize_key(key)).or_default().push(val.clone());
    }
    let mut tags=BTreeMap::new();
    for (key,mut values) in grouped {
        if values.len()==1 {flatten(&key,&values.remove(0),&mut tags)}
        else {
            values.sort_by_key(|v|v.to_string());
            tags.insert(key,Value::Array(values));
        }
    }
    Ok((path,tags,warnings,error))
}
fn compact(value: &Value) -> Value {
    let text=value.to_string();
    if text.len()<=1024 {return value.clone()}
    let prefix:String=text.chars().take(120).collect();
    json!({"json_length":text.len(),"prefix":prefix,"value_omitted":true})
}
fn compare_tags(left:&BTreeMap<String,Value>,right:&BTreeMap<String,Value>) -> Value {
    let mut result:BTreeMap<String,Vec<Value>>=BTreeMap::new();
    for category in ["embedded","filesystem","layout"] {result.insert(category.to_string(),Vec::new());}
    let keys:BTreeSet<_>=left.keys().chain(right.keys()).collect();
    for key in keys {
        if key.starts_with("ExifTool:") || key.starts_with("Composite:") {continue}
        if matches!(key.as_str(),"System:Directory"|"System:FileName"|"System:FileAccessDate") {continue}
        let a=left.get(key);let b=right.get(key);
        if a==b {continue}
        let tag=key.rsplit(':').next().unwrap_or(key);
        let category=if key.starts_with("System:") {"filesystem"}
            else if LAYOUT.contains(&tag) {"layout"} else {"embedded"};
        let mut change=json!({"tag":key,"kind":match (a,b){(None,_)=>"added",(_,None)=>"removed",_=>"changed"}});
        if let Some(value)=a {change["left"]=compact(value)}
        if let Some(value)=b {change["right"]=compact(value)}
        result.get_mut(category).unwrap().push(change);
    }
    json!(result)
}
fn exiftool(paths:&[PathBuf]) -> Result<HashMap<String,(BTreeMap<String,Value>,Vec<String>,Option<String>)>> {
    let mut command=Command::new("exiftool");
    command.args(["-j","-G1:4","-n","-struct","-b","-u","-api","LargeFileSupport=1",
        "-all","-DNGPrivateData","-MakerNotes"]);
    for tag in EXCLUDE_IMAGE {command.args(["-x",tag]);}
    for path in paths {command.arg(path);}
    let output=command.output()?;
    let records:Vec<Value>=serde_json::from_slice(&output.stdout).map_err(|e|
        format!("ExifTool JSON failed: {e}; stderr={}",String::from_utf8_lossy(&output.stderr)))?;
    let mut parsed=HashMap::new();
    for entry in &records {
        let (path,tags,warnings,error)=parse_tags(entry)?;
        parsed.insert(path,(tags,warnings,error));
    }
    Ok(parsed)
}
fn record(pair:&(PathBuf,PathBuf), data:&HashMap<String,(BTreeMap<String,Value>,Vec<String>,Option<String>)>) -> Value {
    let a=pair.0.to_string_lossy();let b=pair.1.to_string_lossy();
    match (data.get(a.as_ref()),data.get(b.as_ref())) {
        (Some((left,lwarn,None)),Some((right,rwarn,None)))=>{
            let mut warnings=lwarn.clone();warnings.extend(rwarn.clone());
            json!({"left":a,"right":b,"status":"ok","warnings":warnings,
                "changes":compare_tags(left,right)})
        }
        (x,y)=>json!({"left":a,"right":b,"status":"error",
            "error_left":x.and_then(|v|v.2.as_deref()).unwrap_or(if x.is_none(){"ExifTool returned no record"}else{""}),
            "error_right":y.and_then(|v|v.2.as_deref()).unwrap_or(if y.is_none(){"ExifTool returned no record"}else{""})})
    }
}
fn write_batch(path:&Path,records:&[Value])->Result<()> {
    let temp=path.with_extension("tmp");
    let mut file=File::create(&temp)?;
    for record in records {writeln!(file,"{}",record)?;}
    file.sync_all()?;
    fs::rename(temp,path)?;
    Ok(())
}
#[derive(Default)]
struct Summary {
    pairs:usize,errors:usize,warnings:usize,embedded_pairs:usize,filesystem_pairs:usize,
    layout_pairs:usize,tags:HashMap<String,usize>,patterns:HashMap<String,usize>,
    examples:HashMap<String,Vec<Value>>,
}
impl Summary {
    fn add(&mut self,record:&Value){
        self.pairs+=1;
        if record["status"]!="ok" {self.errors+=1;return}
        if record["warnings"].as_array().is_some_and(|v|!v.is_empty()){self.warnings+=1;}
        let mut embedded_pattern=Vec::new();
        for (category,counter) in [("embedded",&mut self.embedded_pairs),
            ("filesystem",&mut self.filesystem_pairs),("layout",&mut self.layout_pairs)] {
            let Some(changes)=record["changes"][category].as_array() else {continue};
            if !changes.is_empty(){*counter+=1;}
            for change in changes {
                let key=format!("{category}/{}:{}",change["kind"].as_str().unwrap_or("?"),
                    change["tag"].as_str().unwrap_or("?"));
                *self.tags.entry(key.clone()).or_default()+=1;
                let examples=self.examples.entry(key.clone()).or_default();
                if examples.len()<2 {examples.push(json!({"file":record["left"],"change":change}));}
                if category=="embedded" {embedded_pattern.push(key);}
            }
        }
        embedded_pattern.sort();
        *self.patterns.entry(embedded_pattern.join("\n")).or_default()+=1;
    }
    fn write(&self,path:&Path,total:usize,complete:bool,elapsed:f64)->Result<()> {
        let mut tags:Vec<_>=self.tags.iter().collect();tags.sort_by(|a,b|b.1.cmp(a.1));
        let mut patterns:Vec<_>=self.patterns.iter().collect();patterns.sort_by(|a,b|b.1.cmp(a.1));
        let report=json!({"total_pairs":total,"pairs_recorded":self.pairs,"complete":complete,
            "elapsed_seconds":elapsed,"metadata_errors":self.errors,
            "pairs_with_warnings":self.warnings,
            "pairs_with_embedded_changes":self.embedded_pairs,
            "pairs_with_filesystem_changes":self.filesystem_pairs,
            "pairs_with_layout_changes":self.layout_pairs,
            "top_changed_tags":tags.iter().take(100).map(|(name,count)|json!({"tag":name,"count":count,
                "examples":self.examples.get(*name)})).collect::<Vec<_>>(),
            "top_embedded_patterns":patterns.iter().take(20).map(|(pattern,count)|json!({"count":count,
                "changes":if pattern.is_empty(){Vec::<&str>::new()}else{pattern.split('\n').collect()}})).collect::<Vec<_>>()});
        let temp=path.with_extension("tmp");fs::write(&temp,serde_json::to_vec_pretty(&report)?)?;
        fs::rename(temp,path)?;Ok(())
    }
}
fn main()->Result<()> {
    let args:Vec<String>=std::env::args().collect();
    if args.len()!=5 {return Err("Usage: dng-metadata IMPORTED_VERIFIED.tsv RUST_VERIFIED.tsv REPORT_DIR BATCH_SIZE".into())}
    let imported=PathBuf::from(&args[1]);let verified=PathBuf::from(&args[2]);
    let out=PathBuf::from(&args[3]);let batch_size:usize=args[4].parse()?;
    if batch_size==0 || batch_size>64 {return Err("BATCH_SIZE must be 1..64".into())}
    fs::create_dir_all(&out)?;let batch_dir=out.join("batches");fs::create_dir_all(&batch_dir)?;
    let mut pairs=BTreeMap::new();load_pairs(&imported,&mut pairs)?;load_pairs(&verified,&mut pairs)?;
    let pairs:Vec<_>=pairs.into_iter().collect();
    let total=pairs.len();
    let mut summary=Summary::default();
    let start=Instant::now();
    for (batch_index,chunk) in pairs.chunks(batch_size).enumerate() {
        let path=batch_dir.join(format!("{batch_index:06}.jsonl"));
        if path.exists() {
            let mut prior=Vec::new();
            for line in BufReader::new(File::open(&path)?).lines() {prior.push(serde_json::from_str::<Value>(&line?)?);}
            if prior.len()!=chunk.len() {return Err(format!("Incomplete existing batch: {}",path.display()).into())}
            for record in &prior {summary.add(record)}
            continue
        }
        let batch:Vec<_>=chunk.iter().map(|(a,b)|(a.clone(),b.clone())).collect();
        let paths:Vec<_>=batch.iter().flat_map(|(a,b)|[a.clone(),b.clone()]).collect();
        let mut records=Vec::with_capacity(batch.len());
        match exiftool(&paths) {
            Ok(data)=>for pair in &batch {records.push(record(pair,&data));},
            Err(error)=>for pair in &batch {records.push(json!({"left":pair.0,"right":pair.1,
                "status":"error","error":error.to_string()}));},
        }
        write_batch(&path,&records)?;
        for record in &records {summary.add(record)}
        if batch_index%8==0 || summary.pairs==total {
            summary.write(&out.join("summary.json"),total,summary.pairs==total,start.elapsed().as_secs_f64())?;
            eprintln!("Metadata {}/{}; embedded changes {}; errors {}",summary.pairs,total,
                summary.embedded_pairs,summary.errors);
        }
    }
    // A single JSONL view for readers; the per-batch files remain the resume cache.
    let final_path=out.join("pairs.jsonl");let temp=out.join("pairs.tmp");
    let mut combined=File::create(&temp)?;
    for i in 0..pairs.len().div_ceil(batch_size) {
        let file=batch_dir.join(format!("{i:06}.jsonl"));
        std::io::copy(&mut File::open(file)?,&mut combined)?;
    }
    combined.sync_all()?;fs::rename(temp,final_path)?;
    summary.write(&out.join("summary.json"),total,true,start.elapsed().as_secs_f64())?;
    Ok(())
}

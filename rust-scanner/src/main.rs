use std::collections::{BTreeSet, HashMap, HashSet};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const BLOCK: usize = 1024 * 1024;
const ENCODING: &[u16] = &[256, 257, 258, 259, 262, 266, 277, 278, 284, 317, 322, 323,
    338, 339, 347, 512, 515, 517, 518, 519, 520, 521, 529, 530, 531, 532,
    50871, 50974, 52543, 52544];

fn err(s: impl Into<String>) -> Box<dyn std::error::Error> { s.into().into() }
fn u16_at(b: &[u8], big: bool) -> u16 {
    if big { u16::from_be_bytes([b[0], b[1]]) } else { u16::from_le_bytes([b[0], b[1]]) }
}
fn u32_at(b: &[u8], big: bool) -> u32 {
    if big { u32::from_be_bytes(b[0..4].try_into().unwrap()) } else { u32::from_le_bytes(b[0..4].try_into().unwrap()) }
}
fn u64_at(b: &[u8], big: bool) -> u64 {
    if big { u64::from_be_bytes(b[0..8].try_into().unwrap()) } else { u64::from_le_bytes(b[0..8].try_into().unwrap()) }
}
fn read_at(f: &mut File, size: u64, off: u64, len: usize) -> Result<Vec<u8>> {
    if off.checked_add(len as u64).is_none_or(|end| end > size) { return Err(err("TIFF offset outside file")); }
    f.seek(SeekFrom::Start(off))?;
    let mut out = vec![0; len];
    f.read_exact(&mut out)?;
    Ok(out)
}
fn type_size(kind: u16) -> Option<u64> {
    match kind { 1|2|6|7 => Some(1), 3|8 => Some(2), 4|9|11|13 => Some(4),
                 5|10|12|16|17|18 => Some(8), _ => None }
}
#[derive(Clone)]
struct Tag { kind: u16, count: u64, data: Vec<u8> }
impl Tag {
    fn nums(&self, big: bool) -> Result<Vec<u64>> {
        let width = match self.kind { 1|6|7 => 1, 3|8 => 2, 4|9|13 => 4, 16|17|18 => 8,
            _ => return Err(err("Unsupported integer TIFF tag type")) };
        if self.count > 1_000_000 { return Err(err("Too many TIFF integer values")); }
        let mut out = Vec::with_capacity(self.count as usize);
        for chunk in self.data.chunks_exact(width) {
            out.push(match width { 1 => chunk[0] as u64, 2 => u16_at(chunk,big) as u64,
                4 => u32_at(chunk,big) as u64, _ => u64_at(chunk,big) });
        }
        Ok(out)
    }
}
#[derive(Clone)]
struct Image { signature: Vec<u8>, offsets: Vec<u64>, counts: Vec<u64> }
struct Tiff { raw: Vec<Image>, xmp: Vec<Vec<u8>>, big: bool }
impl Tiff {
    fn parse(f: &mut File, size: u64) -> Result<Self> {
        let header = read_at(f,size,0,16)?;
        let big = match &header[..2] { b"MM" => true, b"II" => false, _ => return Err(err("Not a TIFF file")) };
        let magic = u16_at(&header[2..4],big);
        let (large,first) = match magic {
            42 => (false,u32_at(&header[4..8],big) as u64),
            43 if u16_at(&header[4..6],big)==8 && u16_at(&header[6..8],big)==0 =>
                (true,u64_at(&header[8..16],big)),
            _ => return Err(err("Unsupported TIFF header"))
        };
        let mut pending = vec![first];
        let mut seen = HashSet::new();
        let mut raw = Vec::new();
        let mut xmp = Vec::new();
        let mut has_dng_version = false;
        while let Some(off) = pending.pop() {
            if off==0 || !seen.insert(off) { continue; }
            if seen.len()>4096 { return Err(err("Too many TIFF IFDs")); }
            let count_len = if large {8} else {2};
            let nbytes = read_at(f,size,off,count_len)?;
            let n = if large {u64_at(&nbytes,big)} else {u16_at(&nbytes,big) as u64};
            if n>8192 { return Err(err("Too many TIFF tags")); }
            let entry_len = if large {20} else {12};
            let value_len = if large {8} else {4};
            let entries = read_at(f,size,off+count_len as u64,n as usize*entry_len+value_len)?;
            let mut tags: HashMap<u16,Tag> = HashMap::new();
            for i in 0..n as usize {
                let e=&entries[i*entry_len..(i+1)*entry_len];
                let code=u16_at(&e[..2],big);
                if code==50706 { has_dng_version=true; }
                if !ENCODING.contains(&code) && ![254,273,279,324,325,330,700,50706].contains(&code) {continue;}
                let kind=u16_at(&e[2..4],big);
                let Some(unit)=type_size(kind) else {return Err(err(format!("Unknown type {kind} for selected tag")));};
                let count=if large {u64_at(&e[4..12],big)} else {u32_at(&e[4..8],big) as u64};
                let len=count.checked_mul(unit).ok_or_else(||err("TIFF tag overflow"))?;
                if len>16*1024*1024 {return Err(err("TIFF tag too large"));}
                let slot=&e[entry_len-value_len..entry_len];
                let data=if len<=value_len as u64 {slot[..len as usize].to_vec()} else {
                    let pos=if large {u64_at(slot,big)} else {u32_at(slot,big) as u64};
                    read_at(f,size,pos,len as usize)?
                };
                tags.insert(code,Tag{kind,count,data});
            }
            let tail=&entries[n as usize*entry_len..];
            if let Some(packet)=tags.get(&700) {xmp.push(packet.data.clone());}
            let next=if large {u64_at(tail,big)} else {u32_at(tail,big) as u64};
            if next!=0 {pending.push(next);}
            if let Some(sub)=tags.get(&330) { for pos in sub.nums(big)?.into_iter().rev() { pending.push(pos); } }
            let subtype=tags.get(&254).map(|t|t.nums(big)).transpose()?.and_then(|v|v.first().copied()).unwrap_or(0);
            let photo=tags.get(&262).map(|t|t.nums(big)).transpose()?.and_then(|v|v.first().copied());
            if subtype==0 && photo.is_some_and(|p|[0,1,2,32803,34892].contains(&p)) {
                let offsets=tags.get(&324).or_else(||tags.get(&273)).ok_or_else(||err("Raw IFD missing offsets"))?.nums(big)?;
                let counts=tags.get(&325).or_else(||tags.get(&279)).ok_or_else(||err("Raw IFD missing byte counts"))?.nums(big)?;
                if offsets.is_empty() || offsets.len()!=counts.len() {return Err(err("Invalid raw block directory"));}
                let mut signature=vec![big as u8];
                for code in ENCODING {
                    if let Some(tag)=tags.get(code) {
                        signature.extend_from_slice(&code.to_le_bytes());
                        signature.extend_from_slice(&tag.kind.to_le_bytes());
                        signature.extend_from_slice(&tag.count.to_le_bytes());
                        signature.extend_from_slice(&(tag.data.len() as u64).to_le_bytes());
                        signature.extend_from_slice(&tag.data);
                    }
                }
                raw.push(Image{signature,offsets,counts});
            }
        }
        if !has_dng_version || raw.is_empty() {return Err(err("No DNG raw image found"));}
        Ok(Self{raw,xmp,big})
    }
}
#[derive(Clone,PartialEq,Eq)]
struct Stamp {dev:u64,ino:u64,size:u64,mtime:i128,ctime:i128}
impl Stamp {
    // macOS assigns a new device number when an external volume is remounted.
    // Path, inode, size, and both timestamps remain the cache identity.
    fn same_cached_file(&self, old:&Self)->bool {
        self.ino==old.ino && self.size==old.size && self.mtime==old.mtime && self.ctime==old.ctime
    }
}
fn stamp(path:&Path)->Result<Stamp>{
    let m=fs::metadata(path)?;
    Ok(Stamp{dev:m.dev(),ino:m.ino(),size:m.len(),
        mtime:m.mtime() as i128*1_000_000_000+m.mtime_nsec() as i128,
        ctime:m.ctime() as i128*1_000_000_000+m.ctime_nsec() as i128})
}
fn stamp_text(s:&Stamp)->String {format!("{},{},{},{},{}",s.dev,s.ino,s.size,s.mtime,s.ctime)}
fn parse_stamp(s:&str)->Option<Stamp>{
    let parts:Vec<_>=s.split(',').collect();
    if parts.len()!=5{return None}
    Some(Stamp{dev:parts[0].parse().ok()?,ino:parts[1].parse().ok()?,size:parts[2].parse().ok()?,
        mtime:parts[3].parse().ok()?,ctime:parts[4].parse().ok()?})
}
struct Rate {start:Instant,bytes:u64,per_second:f64}
impl Rate {
    fn new(mib:f64)->Self{Self{start:Instant::now(),bytes:0,per_second:mib*1024.0*1024.0}}
    fn reserve(&mut self,n:u64){
        self.bytes+=n;
        let target=Duration::from_secs_f64(self.bytes as f64/self.per_second);
        if let Some(wait)=target.checked_sub(self.start.elapsed()){std::thread::sleep(wait);}
    }
}
fn compare_blocks(a:&Image,b:&Image,fa:&mut File,fb:&mut File,sa:u64,sb:u64,rate:&mut Rate)->Result<bool>{
    if a.signature!=b.signature || a.counts!=b.counts || a.offsets.len()!=b.offsets.len() {return Ok(false)}
    let mut ba=vec![0;BLOCK];let mut bb=vec![0;BLOCK];
    for ((oa,ob),count) in a.offsets.iter().zip(&b.offsets).zip(&a.counts) {
        if *count==0 || oa.checked_add(*count).is_none_or(|n|n>sa) || ob.checked_add(*count).is_none_or(|n|n>sb){return Err(err("Raw block outside file"));}
        fa.seek(SeekFrom::Start(*oa))?;fb.seek(SeekFrom::Start(*ob))?;
        let mut remain=*count;
        while remain>0 {
            let n=(remain as usize).min(BLOCK);
            rate.reserve(n as u64*2);
            fa.read_exact(&mut ba[..n])?;fb.read_exact(&mut bb[..n])?;
            if ba[..n]!=bb[..n] {return Ok(false)}
            remain-=n as u64;
        }
    }
    Ok(true)
}
fn whole_file_equal(fa:&mut File,fb:&mut File,sa:u64,sb:u64,rate:&mut Rate)->Result<bool>{
    if sa!=sb {return Ok(false)}
    fa.seek(SeekFrom::Start(0))?;fb.seek(SeekFrom::Start(0))?;
    let mut ba=vec![0;BLOCK];let mut bb=vec![0;BLOCK];
    let mut remain=sa;
    while remain>0 {
        let n=(remain as usize).min(BLOCK);
        rate.reserve(n as u64*2);
        fa.read_exact(&mut ba[..n])?;fb.read_exact(&mut bb[..n])?;
        if ba[..n]!=bb[..n] {return Ok(false)}
        remain-=n as u64;
    }
    Ok(true)
}
fn compare(a:&Path,b:&Path,rate:&mut Rate)->Result<(bool,Stamp,Stamp)> {
    let sa=stamp(a)?;let sb=stamp(b)?;
    let mut fa=File::open(a)?;let mut fb=File::open(b)?;
    let ta_result=Tiff::parse(&mut fa,sa.size);let tb_result=Tiff::parse(&mut fb,sb.size);
    let (ta,tb)=match (ta_result,tb_result) {
        (Ok(ta),Ok(tb))=>(ta,tb),
        (left_parse,right_parse)=>{
            if whole_file_equal(&mut fa,&mut fb,sa.size,sb.size,rate)? {
                if sa!=stamp(a)? || sb!=stamp(b)? {return Err(err("File changed during comparison"))}
                return Ok((true,sa,sb));
            }
            return Err(err(format!("DNG parsing failed: left={}, right={}",
                left_parse.err().map_or_else(||"ok".to_string(),|e|e.to_string()),
                right_parse.err().map_or_else(||"ok".to_string(),|e|e.to_string()))));
        }
    };
    if ta.big!=tb.big || ta.raw.len()!=tb.raw.len(){return Ok((false,sa,sb))}
    for (pa,pb) in ta.raw.iter().zip(&tb.raw) {
        match compare_blocks(pa,pb,&mut fa,&mut fb,sa.size,sb.size,rate) {
            Ok(true)=>(),
            Ok(false)|Err(_)=>{
                if whole_file_equal(&mut fa,&mut fb,sa.size,sb.size,rate)? {
                    if sa!=stamp(a)? || sb!=stamp(b)? {return Err(err("File changed during comparison"))}
                    return Ok((true,sa,sb));
                }
                return Ok((false,sa,sb));
            }
        }
    }
    if sa!=stamp(a)? || sb!=stamp(b)? {return Err(err("File changed during comparison"));}
    Ok((true,sa,sb))
}
fn walk(root:&Path,mut visit:impl FnMut(&Path,&std::ffi::OsStr)->Result<()>)->Result<()> {
    let mut pending=vec![root.to_path_buf()];
    while let Some(dir)=pending.pop(){
        for item in fs::read_dir(&dir)? {
            let item=item?;let path=item.path();let kind=item.file_type()?;
            if kind.is_dir(){pending.push(path)}
            else if kind.is_file(){
                let name=item.file_name();let bytes=name.as_bytes();
                if bytes.len()>=4 && bytes[0..2]!=*b"._" && bytes[bytes.len()-4..].eq_ignore_ascii_case(b".dng") {
                    visit(&path,&name)?;
                }
            }
        }
    }
    Ok(())
}
fn hex(bytes:&[u8])->String{const D:&[u8]=b"0123456789abcdef";let mut s=String::with_capacity(bytes.len()*2);for b in bytes{s.push(D[(b>>4) as usize] as char);s.push(D[(b&15) as usize] as char)}s}
fn unhex(s:&str)->Option<Vec<u8>>{if s.len()%2!=0{return None}let mut v=Vec::new();for p in s.as_bytes().chunks(2){v.push(u8::from_str_radix(std::str::from_utf8(p).ok()?,16).ok()?)}Some(v)}
#[derive(Clone)]
struct Record{right:PathBuf,left_stamp:Stamp,right_stamp:Stamp}
fn load_records(path:&Path,out:&mut HashMap<PathBuf,Record>)->Result<()> {
    if !path.exists(){return Ok(())}
    for line in BufReader::new(File::open(path)?).lines(){
        let line=line?;let fields:Vec<_>=line.split('\t').collect();
        if fields.len()!=4{continue}
        let (Some(a),Some(b),Some(sa),Some(sb))=(unhex(fields[0]),unhex(fields[1]),parse_stamp(fields[2]),parse_stamp(fields[3])) else {continue};
        out.insert(PathBuf::from(std::ffi::OsString::from_vec(a)),Record{
            right:PathBuf::from(std::ffi::OsString::from_vec(b)),left_stamp:sa,right_stamp:sb});
    }
    Ok(())
}
fn json_escape(s:&str)->String {
    let mut out=String::from("\"");
    for c in s.chars(){match c{'"'=>out.push_str("\\\""),'\\'=>out.push_str("\\\\"),'\n'=>out.push_str("\\n"),'\r'=>out.push_str("\\r"),'\t'=>out.push_str("\\t"),c if c<' '=>out.push_str(&format!("\\u{:04x}",c as u32)),_=>out.push(c)}}
    out.push('"');out
}
fn save_summary(dir:&Path,left:&Path,right:&Path,total:usize,verified:usize,missing:usize,
    uncertain:usize,new:usize,rate:&Rate,complete:bool)->Result<()> {
    let summary=format!("{{\n  \"left\": {},\n  \"right\": {},\n  \"total_left\": {},\n  \"verified_equal\": {},\n  \"missing_name\": {},\n  \"unverified\": {},\n  \"newly_checked\": {},\n  \"read_bytes_this_run\": {},\n  \"complete\": {},\n  \"left_subsumed_by_right\": {}\n}}\n",
        json_escape(&left.to_string_lossy()),json_escape(&right.to_string_lossy()),total,verified,missing,
        uncertain,new,rate.bytes,complete,complete&&verified==total);
    let tmp=dir.join("summary.tmp");fs::write(&tmp,summary)?;fs::rename(tmp,dir.join("summary.json"))?;Ok(())
}
fn sha256(bytes:&[u8])->String {
    let digest=Sha256::digest(bytes);
    hex(&digest)
}
fn archive_one(source:&Path,root:&Path,out:&Path)->Value {
    let rel=source.strip_prefix(root).unwrap();
    let result=(||->Result<Value>{
        let before=stamp(source)?;
        let mut file=File::open(source)?;
        let (packets,method)=match Tiff::parse(&mut file,before.size) {
            Ok(tiff)=>(tiff.xmp,"tiff_tag_700"),
            Err(parse_error)=>{
                let output=Command::new("exiftool").args(["-b","-XMP"]).arg(source).output()?;
                if !output.status.success() && output.stdout.is_empty() {
                    return Err(err(format!("TIFF parse failed: {parse_error}; ExifTool: {}",
                        String::from_utf8_lossy(&output.stderr))));
                }
                (if output.stdout.is_empty(){Vec::new()}else{vec![output.stdout]},"exiftool_fallback")
            }
        };
        let mut saved=Vec::new();
        for (index,packet) in packets.iter().enumerate(){
            let suffix=if packets.len()==1 {".xmp".to_string()} else {format!(".ifd{index:03}.xmp")};
            let mut destination=out.join("packets").join(rel).into_os_string();
            destination.push(suffix);
            let destination=PathBuf::from(destination);
            fs::create_dir_all(destination.parent().unwrap())?;
            let mut output=File::create(&destination)?;
            output.write_all(packet)?;
            drop(output);
            if fs::read(&destination)?!=*packet {return Err(err("Archived XMP verification failed"))}
            saved.push(json!({"archive_rel":destination.strip_prefix(out)?.to_string_lossy(),
                "ifd_index":index,"bytes":packet.len(),"sha256":sha256(packet)}));
        }
        if before!=stamp(source)? {return Err(err("Source DNG changed during XMP extraction"))}
        Ok(json!({"source_rel":rel.to_string_lossy(),"source_stamp":stamp_text(&before),
            "status":"ok","method":method,"packets":saved}))
    })();
    match result {
        Ok(record)=>record,
        Err(error)=>json!({"source_rel":rel.to_string_lossy(),"status":"error","error":error.to_string()})
    }
}
#[derive(Default)]
struct XmpSummary {processed:usize,with_xmp:usize,without_xmp:usize,packets:usize,bytes:u64,errors:usize,fallback:usize}
impl XmpSummary {
    fn add(&mut self,record:&Value){
        self.processed+=1;
        if record["status"]!="ok" {self.errors+=1;return}
        if record["method"]=="exiftool_fallback" {self.fallback+=1;}
        let packets=record["packets"].as_array().map(Vec::as_slice).unwrap_or(&[]);
        if packets.is_empty(){self.without_xmp+=1}else{self.with_xmp+=1}
        self.packets+=packets.len();
        self.bytes+=packets.iter().map(|p|p["bytes"].as_u64().unwrap_or(0)).sum::<u64>();
    }
    fn write(&self,root:&Path,out:&Path,total:usize)->Result<()> {
        let complete=self.processed==total;
        let data=json!({"source":root,"total_dng":total,
            "processed":self.processed,"with_xmp":self.with_xmp,"without_xmp":self.without_xmp,
            "xmp_packets_saved":self.packets,"xmp_bytes_saved":self.bytes,
            "exiftool_fallback_files":self.fallback,"errors":self.errors,
            "complete":complete,"all_xmp_saved":complete&&self.errors==0});
        let temp=out.join("summary.tmp");fs::write(&temp,serde_json::to_vec_pretty(&data)?)?;
        fs::rename(temp,out.join("summary.json"))?;Ok(())
    }
}
fn valid_archive_batch(path:&Path,chunk:&[PathBuf],root:&Path,out:&Path)->Option<Vec<Value>> {
    let mut records=Vec::new();
    for line in BufReader::new(File::open(path).ok()?).lines() {
        records.push(serde_json::from_str::<Value>(&line.ok()?).ok()?);
    }
    if records.len()!=chunk.len(){return None}
    for (record,source) in records.iter().zip(chunk){
        if record["status"]!="ok" || record["source_rel"].as_str()!=source.strip_prefix(root).ok()?.to_str(){return None}
        let old=parse_stamp(record["source_stamp"].as_str()?)?;
        if !stamp(source).ok()?.same_cached_file(&old){return None}
        for packet in record["packets"].as_array()? {
            let bytes=fs::read(out.join(packet["archive_rel"].as_str()?)).ok()?;
            if bytes.len() as u64!=packet["bytes"].as_u64()? || sha256(&bytes)!=packet["sha256"].as_str()? {return None}
        }
    }
    Some(records)
}
fn archive_xmp(root:&Path,out:&Path)->Result<()> {
    let root=fs::canonicalize(root)?;
    fs::create_dir_all(out)?;
    let batch_dir=out.join("batches");fs::create_dir_all(&batch_dir)?;
    let mut sources=BTreeSet::new();
    walk(&root,|path,_|{sources.insert(path.to_path_buf());Ok(())})?;
    let sources:Vec<_>=sources.into_iter().collect();
    let mut summary=XmpSummary::default();
    for (batch_index,chunk) in sources.chunks(256).enumerate(){
        let path=batch_dir.join(format!("{batch_index:06}.jsonl"));
        let records=if path.exists(){valid_archive_batch(&path,chunk,&root,out)}else{None};
        let records=match records {
            Some(saved)=>saved,
            None=>{
                let fresh:Vec<_>=chunk.iter().map(|source|archive_one(source,&root,out)).collect();
                let temp=path.with_extension("tmp");let mut file=File::create(&temp)?;
                for record in &fresh {writeln!(file,"{record}")?;}
                file.sync_all()?;fs::rename(temp,&path)?;fresh
            }
        };
        for record in &records{summary.add(record)}
        summary.write(&root,out,sources.len())?;
        eprintln!("XMP archived {}/{} DNGs; packets {}; errors {}",summary.processed,sources.len(),summary.packets,summary.errors);
    }
    let temp=out.join("manifest.tmp");let mut manifest=File::create(&temp)?;
    for i in 0..sources.len().div_ceil(256){
        std::io::copy(&mut File::open(batch_dir.join(format!("{i:06}.jsonl")))?,&mut manifest)?;
    }
    manifest.sync_all()?;fs::rename(temp,out.join("manifest.jsonl"))?;
    summary.write(&root,out,sources.len())?;
    Ok(())
}
fn main()->Result<()> {
    let args:Vec<String>=env::args().collect();
    if args.len()==4 && args[1]=="--archive-xmp" {
        return archive_xmp(Path::new(&args[2]),Path::new(&args[3]));
    }
    if args.len()<4 {return Err(err("Usage: dng-subset LEFT RIGHT REPORT [--rate-mib N] [--max-new N] [--import-cache FILE] [--after SUMMARY]"))}
    let left_input=PathBuf::from(&args[1]);let right_input=PathBuf::from(&args[2]);
    let report=PathBuf::from(&args[3]);
    let mut rate_mib:f64=32.0;let mut max_new=usize::MAX;let mut import=None;let mut after=None;
    let mut i=4;while i<args.len(){
        if i+1>=args.len(){return Err(err("Option missing value"))}
        match args[i].as_str(){
            "--rate-mib"=>rate_mib=args[i+1].parse()?,"--max-new"=>max_new=args[i+1].parse()?,
            "--import-cache"=>import=Some(PathBuf::from(&args[i+1])),
            "--after"=>after=Some(PathBuf::from(&args[i+1])),
            _=>return Err(err(format!("Unknown option: {}",args[i])))
        }i+=2;
    }
    if !rate_mib.is_finite() || rate_mib<=0.0 || rate_mib>512.0 {return Err(err("rate-mib must be in (0,512]"))}
    if let Some(first_report)=after {
        eprintln!("Waiting for {} to complete and for both volumes to be mounted...",first_report.display());
        loop {
            let first_complete=fs::read_to_string(&first_report).ok()
                .is_some_and(|s|s.contains("\"complete\": true"));
            if first_complete && left_input.is_dir() && right_input.is_dir() {break}
            std::thread::sleep(Duration::from_secs(60));
        }
        eprintln!("Earlier scan complete; starting {} against {}.",left_input.display(),right_input.display());
    }
    let left=fs::canonicalize(&left_input)?;let right=fs::canonicalize(&right_input)?;
    fs::create_dir_all(&report)?;
    let mut sources=BTreeSet::new();
    eprintln!("Indexing left DNGs...");
    walk(&left,|p,_|{sources.insert(p.to_path_buf());Ok(())})?;
    let wanted:HashSet<_>=sources.iter().filter_map(|p|p.file_name().map(|s|s.to_os_string())).collect();
    let mut targets:HashMap<_,Vec<PathBuf>>=HashMap::new();
    eprintln!("Indexing right DNG names...");
    walk(&right,|p,name|{if wanted.contains(name){targets.entry(name.to_os_string()).or_default().push(p.to_path_buf());}Ok(())})?;
    let mut saved=HashMap::new();
    if let Some(path)=import.as_deref(){load_records(path,&mut saved)?;}
    load_records(&report.join("verified.tsv"),&mut saved)?;
    let mut output=BufWriter::new(OpenOptions::new().create(true).append(true).open(report.join("verified.tsv"))?);
    let mut not_confirmed=Vec::new();
    let mut verified=0;let mut missing=0;let mut uncertain=0;let mut new=0;
    let mut rate=Rate::new(rate_mib);
    let total=sources.len();
    for path in &sources{
        let Some(name)=path.file_name() else {continue};
        let Some(candidates)=targets.get(name) else {missing+=1;not_confirmed.push(format!("MISSING\t{}",path.display()));continue};
        if let Some(record)=saved.get(path) {
            if candidates.contains(&record.right)
                && stamp(path).ok().is_some_and(|s|s.same_cached_file(&record.left_stamp))
                && stamp(&record.right).ok().is_some_and(|s|s.same_cached_file(&record.right_stamp))
                {verified+=1;continue}
        }
        if new>=max_new {not_confirmed.push(format!("PENDING\t{}",path.display()));continue}
        let mut ordered=candidates.clone();
        ordered.sort_by_key(|p|(p.strip_prefix(&right).ok()!=path.strip_prefix(&left).ok(),p.clone()));
        let mut confirmed=false;let mut errors=Vec::new();
        for target in ordered {
            match compare(path,&target,&mut rate){
                Ok((true,sa,sb))=>{
                    writeln!(output,"{}\t{}\t{}\t{}",hex(path.as_os_str().as_bytes()),
                        hex(target.as_os_str().as_bytes()),stamp_text(&sa),stamp_text(&sb))?;
                    output.flush()?;verified+=1;confirmed=true;break;
                }
                Ok((false,_,_))=>errors.push(format!("{}: encoded blocks differ or images differ",target.display())),
                Err(e)=>errors.push(format!("{}: {e}",target.display())),
            }
        }
        new+=1;
        if !confirmed {uncertain+=1;not_confirmed.push(format!("UNVERIFIED\t{}\t{}",path.display(),errors.join(" | ")));}
        if new%8==0 {
            save_summary(&report,&left,&right,total,verified,missing,uncertain,new,&rate,false)?;
            eprintln!("{}/{} Apo verified; {} new this run; {:.2} GiB read; {} unverified",verified,total,new,rate.bytes as f64/1073741824.0,uncertain);
        }
    }
    let complete=verified+missing+uncertain==total;
    save_summary(&report,&left,&right,total,verified,missing,uncertain,new,&rate,complete)?;
    fs::write(report.join("not-confirmed.tsv"),not_confirmed.join("\n")+"\n")?;
    eprintln!("Done: {verified}/{total} verified; complete={complete}; read {:.2} GiB",rate.bytes as f64/1073741824.0);
    if !complete || verified!=total {
        return Err(err("Raw subsumption is not confirmed; inspect summary.json and not-confirmed.tsv"));
    }
    Ok(())
}

use sha2::{Digest,Sha256};
use std::{fs::File,io::{self,Read},process::{Command,Stdio}};
const BLOCK:usize=1024*1024;
fn hash_reader(mut r:impl Read)->io::Result<String>{let mut h=Sha256::new();let mut b=vec![0;BLOCK];loop{let n=r.read(&mut b)?;if n==0{break}h.update(&b[..n]);}Ok(format!("{:x}",h.finalize()))}
fn hash(p:&str)->io::Result<String>{hash_reader(File::open(p)?)}
fn main()->Result<(),Box<dyn std::error::Error>>{
 let a:Vec<String>=std::env::args().collect();
 match a.get(1).map(String::as_str){
 Some("hash")=>println!("{}",hash(&a[2])?),
 Some("compare")=>{
 let source=File::open(&a[2])?;let mut reference=if a[3].is_empty(){None}else{Some(File::open(&a[3])?)};
 let mut source=source;let mut sh=Sha256::new();let mut rh=Sha256::new();let mut left=vec![0;BLOCK];let mut right=vec![0;BLOCK];let mut equal=reference.is_some();
 loop{let n=source.read(&mut left)?;if n==0{break}sh.update(&left[..n]);let mut got=0;
 if let Some(r)=reference.as_mut(){while got<n{let k=r.read(&mut right[got..n])?;if k==0{break}got+=k;}rh.update(&right[..got]);}
 if got!=n||left[..n]!=right[..got]{equal=false;}}
 if let Some(r)=reference.as_mut(){loop{let n=r.read(&mut right)?;if n==0{break}rh.update(&right[..n]);equal=false;}}
 println!("{}\t{}\t{}",format!("{:x}",sh.finalize()),if reference.is_some(){format!("{:x}",rh.finalize())}else{"-".into()},if equal{"1"}else{"0"});
 },
 Some("delta")=>{
 let status=Command::new("xdelta3").args(["-e","-1","-S","none","-n","-a","-D","-R","-A","-B","16777216","-W","8388608","-s",&a[2],&a[3],&a[4]]).stdout(Stdio::null()).status()?;
 if !status.success(){return Err("VCDIFF encode failed".into())}
 File::open(&a[4])?.sync_all()?;
 let delta_hash=hash(&a[4])?;
 let mut child=Command::new("xdelta3").args(["-d","-D","-R","-c","-s",&a[2],&a[4]]).stdout(Stdio::piped()).spawn()?;
 let result=hash_reader(child.stdout.take().unwrap());let status=child.wait()?;let recovered=result?;
 if !status.success()||recovered!=a[5]{return Err("Reconstructed original SHA-256 mismatch".into())}
 println!("{}",delta_hash);
 },_=>return Err("expected hash, compare, or delta".into())}Ok(())
}

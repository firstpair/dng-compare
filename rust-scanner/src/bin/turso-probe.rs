//! Direct embedded Turso MVCC smoke test through firstpair-tursolite; no CLI
//! or Python database wrapper.
use firstpair_tursolite::{Database, Engine, Journal, Options, Seal, Value};
use std::{env, path::Path, sync::Arc, thread};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = env::args().nth(1).ok_or("database path required")?;
    let options = Options::new().engine(Engine::Turso).journal(Journal::Mvcc).seal(Seal::KeepWal);
    let db = Arc::new(Database::open(Path::new(&path), &options)?);
    let conn = db.connect()?;
    conn.exec("PRAGMA synchronous=FULL")?;
    conn.exec("CREATE TABLE IF NOT EXISTS native_probe(id INTEGER PRIMARY KEY, worker INTEGER)")?;
    assert!(db.mvcc());
    let workers = 4;
    let mut handles = Vec::new();
    for worker in 0..workers {
        let db = db.clone();
        handles.push(thread::spawn(move || -> Result<(), String> {
            let c = db.connect().map_err(|e| e.to_string())?;
            for i in 0..16 {
                c.transaction(|| c.run("INSERT OR REPLACE INTO native_probe VALUES (?,?)", &[Value::Integer(worker * 16 + i), Value::Integer(worker)]))
                    .map_err(|e| e.to_string())?;
            }
            Ok(())
        }));
    }
    for handle in handles {
        handle.join().map_err(|_| "writer panicked")??;
    }
    let count = conn.query("SELECT count(*) FROM native_probe", &[])?[0][0].as_i64()?;
    assert_eq!(count, 64);
    drop(conn);
    Arc::into_inner(db).ok_or("database still shared")?.close()?;
    println!("Turso 0.8.3 embedded Rust: journal_mode=mvcc, concurrent_writers={workers}, records={count}");
    Ok(())
}

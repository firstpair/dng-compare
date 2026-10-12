//! Direct embedded Turso 0.8 MVCC smoke test; no CLI or Python database wrapper.
use std::{env, thread};
use turso::Builder;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = env::args().nth(1).ok_or("database path required")?;
    let db = pollster::block_on(Builder::new_local(&path).build())?;
    let conn = db.connect()?;
    let mut setting = pollster::block_on(conn.query("PRAGMA journal_mode=mvcc", ()))?;
    while pollster::block_on(setting.next())?.is_some() {}
    drop(setting);
    pollster::block_on(conn.execute_batch("PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS native_probe(id INTEGER PRIMARY KEY, value TEXT NOT NULL);"))?;
    let mode = {
        let mut rows = pollster::block_on(conn.query("PRAGMA journal_mode", ()))?;
        pollster::block_on(rows.next())?.ok_or("journal mode missing")?.get::<String>(0)?
    };
    assert_eq!(mode, "mvcc");
    let workers = 4;
    let mut handles = Vec::new();
    for worker in 0..workers {
        let db = db.clone();
        handles.push(thread::spawn(move || -> Result<(), String> {
            let c = db.connect().map_err(|e| e.to_string())?;
            c.busy_timeout(std::time::Duration::from_secs(60)).map_err(|e| e.to_string())?;
            pollster::block_on(async {
                for i in 0..16 {
                    c.execute("BEGIN CONCURRENT", ()).await.map_err(|e| e.to_string())?;
                    c.execute("INSERT OR REPLACE INTO native_probe VALUES (?,?)", turso::params![worker * 16 + i, format!("worker-{worker}-{i}")]).await.map_err(|e| e.to_string())?;
                    c.execute("COMMIT", ()).await.map_err(|e| e.to_string())?;
                }
                Ok(())
            })
        }));
    }
    for handle in handles { handle.join().map_err(|_| "writer panicked")??; }
    let mut rows = pollster::block_on(conn.query("SELECT count(*) FROM native_probe", ()))?;
    let count = pollster::block_on(rows.next())?.ok_or("count missing")?.get::<i64>(0)?;
    assert_eq!(count, 64);
    drop(rows);
    let mut checkpoint = pollster::block_on(conn.query("PRAGMA wal_checkpoint(TRUNCATE)", ()))?;
    while pollster::block_on(checkpoint.next())?.is_some() {}
    println!("Turso 0.8.2 embedded Rust: journal_mode={mode}, concurrent_writers={workers}, records={count}");
    Ok(())
}

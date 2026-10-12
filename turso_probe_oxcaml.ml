open Turso_oxcaml
let () =
  let path = Sys.argv.(1) in
  if version () <> "0.8.2" then failwith "Wrong embedded Turso version";
  let database = open_database path in
  let connection = connect database in
  enable_mvcc connection;
  ignore (execute connection "CREATE TABLE IF NOT EXISTS ocaml_probe(id INTEGER PRIMARY KEY, bytes BLOB, real REAL, nullable TEXT)" [||]);
  let writer worker =
    let c = connect database in
    for i = 0 to 15 do
      transaction c (fun () ->
        ignore (execute c "INSERT OR REPLACE INTO ocaml_probe VALUES (?,?,?,?)"
          [|Integer (Int64.of_int (worker * 16 + i)); Blob (Bytes.of_string "\000\255typed"); Real 1.25; Null|]))
    done;
    close c
  in
  let domains = Array.init 4 (fun worker -> Domain.spawn (fun () -> writer worker)) in
  Array.iter Domain.join domains;
  (match execute connection "SELECT count(*) FROM ocaml_probe" [||] with
   | [[|Integer 64L|]] -> () | _ -> failwith "Concurrent writer coverage mismatch");
  (match execute connection "SELECT bytes,real,nullable FROM ocaml_probe WHERE id=?" [|Integer 0L|] with
   | [[|Blob b; Real r; Null|]] when Bytes.to_string b = "\000\255typed" && r = 1.25 -> ()
   | _ -> failwith "Typed values did not round-trip");
  checkpoint connection;
  close connection;close_database database;
  Printf.printf "Turso %s embedded OxCaml: journal_mode=mvcc, concurrent_writers=4, records=64\n" (version ())

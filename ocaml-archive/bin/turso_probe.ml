(* Embedded Turso from OCaml through firstpair-tursolite: MVCC, four domains
   writing one database, and typed values. No subprocess database service. *)
open Tursolite

let () =
  let path = Sys.argv.(1) in
  if turso_version () <> "0.8.3" then failwith "Wrong embedded Turso version";
  let database = open_database ~engine:Turso ~journal:Mvcc ~seal:Sealed_wal path in
  let connection = connect database in
  exec connection "PRAGMA synchronous=FULL";
  exec connection "CREATE TABLE IF NOT EXISTS ocaml_probe(id INTEGER PRIMARY KEY, bytes BLOB, real REAL, nullable TEXT)";
  let writer worker =
    let c = connect database in
    for i = 0 to 15 do
      transaction c (fun () ->
        run c "INSERT OR REPLACE INTO ocaml_probe VALUES (?,?,?,?)"
          [Integer (Int64.of_int (worker * 16 + i)); Blob "\000\255typed"; Real 1.25; Null])
    done;
    disconnect c in
  let spawn = (Domain.spawn [@alert "-do_not_spawn_domains"] [@alert "-unsafe_multidomain"]) in
  List.iter Domain.join (List.init 4 (fun worker -> spawn (fun () -> writer worker)));
  (match query connection "SELECT count(*) FROM ocaml_probe" [] with
   | [ [| Integer 64L |] ] -> () | _ -> failwith "Concurrent writer coverage mismatch");
  (match query connection "SELECT bytes,real,nullable FROM ocaml_probe WHERE id=?" [Integer 0L] with
   | [ [| Blob "\000\255typed"; Real 1.25; Null |] ] -> ()
   | _ -> failwith "Typed values did not round-trip");
  disconnect connection;
  close database;
  Printf.printf "Turso %s embedded OCaml: journal_mode=mvcc, concurrent_writers=4, records=64\n" (turso_version ())

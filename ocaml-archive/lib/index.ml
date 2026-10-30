(* metadata/differences.sqlite3 from metadata/batches/*.jsonl.gz, as
   metadata_index.py builds it: the same schema, the same rows, and values
   keyed by the SHA-256 of the same canonical JSON, so either tool can add
   batches to an index the other made. Each batch is recorded with its file's
   SHA-256 and is not ingested again while that is unchanged. *)

let schema = {|
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS batches(name TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', extension TEXT NOT NULL, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE, compared INTEGER NOT NULL, errors INTEGER NOT NULL, UNIQUE(path,version));
CREATE TABLE IF NOT EXISTS fields(id INTEGER PRIMARY KEY, tag TEXT UNIQUE NOT NULL, category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vals(id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, json_zlib BLOB NOT NULL, json_bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS differences(file_id INTEGER REFERENCES files(id) ON DELETE CASCADE, field_id INTEGER REFERENCES fields(id), kind TEXT NOT NULL, apo_value INTEGER REFERENCES vals(id), bumer_value INTEGER REFERENCES vals(id), PRIMARY KEY(file_id,field_id));
CREATE INDEX IF NOT EXISTS differences_field_kind ON differences(field_id,kind,file_id);
CREATE INDEX IF NOT EXISTS differences_values ON differences(field_id,apo_value,bumer_value);
CREATE INDEX IF NOT EXISTS files_extension ON files(extension,id);
CREATE TABLE IF NOT EXISTS coverage(batch TEXT REFERENCES batches(name) ON DELETE CASCADE, field_id INTEGER REFERENCES fields(id), extension TEXT, apo_present INTEGER, bumer_present INTEGER, PRIMARY KEY(batch,field_id,extension));
CREATE VIEW IF NOT EXISTS field_coverage AS SELECT t.tag,t.category,c.extension,SUM(c.apo_present) apo_present,SUM(c.bumer_present) bumer_present FROM coverage c JOIN fields t ON t.id=c.field_id GROUP BY c.field_id,c.extension;
CREATE VIEW IF NOT EXISTS change_counts AS SELECT t.tag,t.category,d.kind,f.extension,COUNT(*) affected_files FROM differences d JOIN fields t ON t.id=d.field_id JOIN files f ON f.id=d.file_id GROUP BY d.field_id,d.kind,f.extension;
CREATE VIEW IF NOT EXISTS field_changes AS SELECT t.tag,t.category,d.kind,COUNT(*) affected_files,(SELECT COUNT(*) FROM files WHERE compared=1) compared_files,ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM files WHERE compared=1),4) percent_compared FROM differences d JOIN fields t ON t.id=d.field_id JOIN files f ON f.id=d.file_id WHERE f.compared=1 GROUP BY d.field_id,d.kind;
|}

(* Indexes made before file versions keep their rows, as version ''. *)
let migrate_version = {|
PRAGMA foreign_keys=OFF;
BEGIN;
CREATE TABLE files_versioned(id INTEGER PRIMARY KEY, path TEXT NOT NULL, version TEXT NOT NULL DEFAULT '', extension TEXT NOT NULL, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE, compared INTEGER NOT NULL, errors INTEGER NOT NULL, UNIQUE(path,version));
INSERT INTO files_versioned(id,path,version,extension,batch,compared,errors) SELECT id,path,'',extension,batch,compared,errors FROM files;
DROP VIEW IF EXISTS field_coverage;
DROP VIEW IF EXISTS change_counts;
DROP VIEW IF EXISTS field_changes;
DROP INDEX IF EXISTS files_extension;
DROP TABLE files;
ALTER TABLE files_versioned RENAME TO files;
COMMIT;
PRAGMA foreign_keys=ON;
|}

let check db what rc =
  if not (Sqlite3.Rc.is_success rc) then
    failwith (Printf.sprintf "%s: %s (%s)" what (Sqlite3.Rc.to_string rc) (Sqlite3.errmsg db))

let exec db sql = check db "sqlite" (Sqlite3.exec db sql)

(* Run [sql] with [params]; rows as arrays of data. *)
let query db sql params =
  let st = Sqlite3.prepare db sql in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
    List.iteri (fun i p -> check db "bind" (Sqlite3.bind st (i + 1) p)) params;
    let rec rows acc = match Sqlite3.step st with
      | Sqlite3.Rc.ROW -> rows (Sqlite3.row_data st :: acc)
      | Sqlite3.Rc.DONE -> List.rev acc
      | rc -> check db sql rc; List.rev acc in
    rows [])

let run db sql params = ignore (query db sql params)

let text s = Sqlite3.Data.TEXT s
let int n = Sqlite3.Data.INT (Int64.of_int n)
let int_of = function Sqlite3.Data.INT n -> Int64.to_int n | _ -> failwith "expected an integer"

let open_index archive =
  let db = Native.open_sqlite (Filename.concat archive "metadata/differences.sqlite3") in
  let columns = List.map (fun r -> match r.(1) with Sqlite3.Data.TEXT s -> s | _ -> "")
      (query db "PRAGMA table_info(files)" []) in
  if columns <> [] && not (List.mem "version" columns) then exec db migrate_version;
  exec db schema;
  db

let ingest db path =
  let bytes = In_channel.with_open_bin path In_channel.input_all in
  let digest = Native.sha_string bytes and name = Filename.basename path in
  match query db "SELECT sha256 FROM batches WHERE name=?" [text name] with
  | [ [| Sqlite3.Data.TEXT d |] ] when d = digest -> false
  | _ ->
      let rows = List.filter_map (fun line -> if line = "" then None else Some (Json.parse line))
          (String.split_on_char '\n' (Native.gunzip bytes)) in
      let fields = Hashtbl.create 256 and values = Hashtbl.create 1024 and coverage = Hashtbl.create 1024 in
      let field tag category =
        match Hashtbl.find_opt fields tag with
        | Some id -> id
        | None ->
            run db "INSERT OR IGNORE INTO fields(tag,category) VALUES (?,?)" [text tag; text category];
            let id = int_of (List.hd (query db "SELECT id FROM fields WHERE tag=?" [text tag])).(0) in
            Hashtbl.replace fields tag id; id in
      let value v =
        let raw = Json.dumps ~compact:true ~sort_keys:true v in
        let h = Native.sha_string raw in
        match Hashtbl.find_opt values h with
        | Some id -> id
        | None ->
            run db "INSERT OR IGNORE INTO vals(sha256,json_zlib,json_bytes) VALUES (?,?,?)"
              [text h; Sqlite3.Data.BLOB (Native.zlib_compress raw 9); int (String.length raw)];
            let id = int_of (List.hd (query db "SELECT id FROM vals WHERE sha256=?" [text h])).(0) in
            Hashtbl.replace values h id; id in
      let get r k = match Json.member k r with Some v -> v | None -> failwith (name ^ ": row without " ^ k) in
      let opt_value d k = match Json.member k d with
        | Some v -> Sqlite3.Data.INT (Int64.of_int (value v)) | None -> Sqlite3.Data.NULL in
      exec db "BEGIN";
      (try
         run db "DELETE FROM batches WHERE name=?" [text name];
         run db "INSERT INTO batches VALUES (?,?)" [text name; text digest];
         let order = ref [] in
         List.iter (fun r ->
           let skip = Json.truthy (Option.value (Json.member "ignored_system_file" r) ~default:Json.Null)
                      || Filename.basename (Json.str (get r "apo")) = ".DS_Store" in
           if not skip then begin
             let rel = Json.str (get r "relative_path") in
             let ext = Native.suffix rel in
             let errors = match get r "errors" with Json.Arr l -> List.length l | _ -> 0 in
             let compared = Json.truthy (get r "bumer") && errors = 0 in
             let version = match Json.member "version" r with Some (Json.Str v) -> v | _ -> "" in
             run db "INSERT INTO files(path,version,extension,batch,compared,errors) VALUES (?,?,?,?,?,?)"
               [text rel; text version; text ext; text name; int (Bool.to_int compared); int errors];
             let fid = Int64.to_int (Sqlite3.last_insert_rowid db) in
             if compared then begin
               List.iter (fun side ->
                 match get r (side ^ "_metadata") with
                 | Json.Obj tags -> List.iter (fun (tag, _) ->
                     if not (tag = "SourceFile" || String.starts_with ~prefix:"ExifTool:" tag
                             || String.starts_with ~prefix:"Composite:" tag) then begin
                       let tid = field tag (if String.starts_with ~prefix:"File:" tag then "filesystem" else "embedded") in
                       let key = (tid, ext, side) in
                       if not (Hashtbl.mem coverage key) then order := (tid, ext) :: !order;
                       Hashtbl.replace coverage key (1 + Option.value (Hashtbl.find_opt coverage key) ~default:0)
                     end) tags
                 | _ -> ()) ["apo"; "bumer"];
               (match get r "differences" with
                | Json.Arr ds -> List.iter (fun d ->
                    run db "INSERT INTO differences VALUES (?,?,?,?,?)"
                      [int fid; int (field (Json.str (get d "tag")) (Json.str (get d "category")));
                       text (Json.str (get d "kind")); opt_value d "apo"; opt_value d "bumer"]) ds
                | _ -> ())
             end
           end) rows;
         let count k = Option.value (Hashtbl.find_opt coverage k) ~default:0 in
         List.iter (fun (tid, ext) ->
           run db "INSERT INTO coverage VALUES (?,?,?,?,?)"
             [text name; int tid; text ext; int (count (tid, ext, "apo")); int (count (tid, ext, "bumer"))])
           (List.sort_uniq compare !order);
         exec db "COMMIT"
       with e -> ignore (Sqlite3.exec db "ROLLBACK"); raise e);
      true

(* Every batch of [archive], in name order; prints the summary line
   metadata_index.py prints. *)
let index_archive archive =
  let db = open_index archive in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.db_close db)) (fun () ->
    let dir = Filename.concat archive "metadata/batches" in
    let batches = List.sort compare (List.filter (fun f -> Filename.check_suffix f ".jsonl.gz")
        (Array.to_list (try Sys.readdir dir with Sys_error _ -> [||]))) in
    List.iter (fun f -> ignore (ingest db (Filename.concat dir f))) batches;
    let one sql = match query db sql [] with [ [| v |] ] -> v | _ -> Sqlite3.Data.NULL in
    let n sql = Json.int (int_of (one sql)) in
    print_endline (Json.dumps (Json.Obj [
      ("indexed_files", n "SELECT COUNT(*) FROM files");
      ("differences", n "SELECT COUNT(*) FROM differences");
      ("integrity", Json.Str (match one "PRAGMA integrity_check" with Sqlite3.Data.TEXT s -> s | _ -> "?"))])))

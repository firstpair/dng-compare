(* phindex indexes of the two trees (docs/native-all.md, "Using phindex
   indexes"): a file's row is current when its path is in [files] with gone=0,
   a non-empty sha256, and the size and mtime_ns of its stat now. Opened
   read-only and immutable, as compare_dng.py and preserve_apo.py open them;
   each process (the forked workers too) opens its own connection. *)

type t = { path : string; mutable conn : (int * Sqlite3.db) option }

let open_ path =
  let path = try Unix.realpath path with Unix.Unix_error _ -> path in
  { path; conn = None }

let db t =
  let pid = Unix.getpid () in
  match t.conn with
  | Some (p, d) when p = pid -> d
  | _ ->
      let d = Sqlite3.db_open ~mode:`READONLY ~uri:true ("file:" ^ t.path ^ "?immutable=1") in
      t.conn <- Some (pid, d); d

type row = { sha256 : string; dng_blocks : string option }

let row t path ~size ~mtime_ns =
  let st = Sqlite3.prepare (db t) "SELECT size, mtime_ns, sha256, dng_blocks FROM files WHERE path=? AND gone=0" in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
    ignore (Sqlite3.bind st 1 (Sqlite3.Data.TEXT path));
    match Sqlite3.step st with
    | Sqlite3.Rc.ROW ->
        let text = function Sqlite3.Data.TEXT s | Sqlite3.Data.BLOB s -> Some s | _ -> None in
        let int = function Sqlite3.Data.INT n -> Some n | _ -> None in
        (match int (Sqlite3.column st 0), int (Sqlite3.column st 1), text (Sqlite3.column st 2) with
         | Some s, Some m, Some sha when sha <> "" && s = size && m = mtime_ns ->
             Some { sha256 = sha; dng_blocks = (match text (Sqlite3.column st 3) with Some "" -> None | b -> b) }
         | _ -> None)
    | Sqlite3.Rc.DONE -> None
    | rc -> failwith (Printf.sprintf "%s: %s (%s)" t.path (Sqlite3.Rc.to_string rc) (Sqlite3.errmsg (db t))))

(* The preservation side: preserve_apo's indexed_sha256 needs no dng_blocks
   column, so the row is read with the columns that table has. *)
let sha256 t path ~size ~mtime_ns =
  let st = Sqlite3.prepare (db t) "SELECT size, mtime_ns, sha256 FROM files WHERE path=? AND gone=0" in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
    ignore (Sqlite3.bind st 1 (Sqlite3.Data.TEXT path));
    match Sqlite3.step st with
    | Sqlite3.Rc.ROW ->
        (match Sqlite3.column st 0, Sqlite3.column st 1, Sqlite3.column st 2 with
         | Sqlite3.Data.INT s, Sqlite3.Data.INT m, Sqlite3.Data.TEXT sha when sha <> "" && s = size && m = mtime_ns -> Some sha
         | _ -> None)
    | _ -> None)

(* The metadata record phindex keeps for [path] (phindex_metadata.py's
   Index.record): the JSON of table [metadata], raw deflate, read as json.loads
   reads it, when the [files] row has the size and mtime_ns of [stat]; else the
   reason there is none. *)
let record t path ~size ~mtime_ns =
  let one sql f =
    let st = Sqlite3.prepare (db t) sql in
    Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
      ignore (Sqlite3.bind st 1 (Sqlite3.Data.TEXT path));
      match Sqlite3.step st with
      | Sqlite3.Rc.ROW -> Some (f st)
      | Sqlite3.Rc.DONE -> None
      | rc -> failwith (Printf.sprintf "%s: %s (%s)" t.path (Sqlite3.Rc.to_string rc) (Sqlite3.errmsg (db t)))) in
  match one "SELECT size, mtime_ns FROM files WHERE path=? AND gone=0" (fun st -> Sqlite3.column st 0, Sqlite3.column st 1) with
  | None -> Error "not in the phindex index"
  | Some (s, m) when not (s = Sqlite3.Data.INT size && m = Sqlite3.Data.INT mtime_ns) -> Error "phindex row is out of date"
  | Some _ ->
      match one "SELECT record_deflate FROM metadata WHERE path=?" (fun st -> Sqlite3.column st 0) with
      | None -> Error "phindex has no metadata record"
      | Some (Sqlite3.Data.BLOB b) -> Ok (Py.dedup (Json.parse (Native.inflate_raw b)))
      | Some _ -> failwith (path ^ ": phindex record_deflate is not a blob")

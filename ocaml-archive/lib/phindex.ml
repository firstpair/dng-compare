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

(* The SHA-256 phindex holds for [path] when its row has the size and
   mtime_ns given, else None (also for an index without the column). The
   preservation side (preserve_apo's indexed_sha256) and phindex-metadata's
   byte-identical pairs both use it. *)
let sha256 t path ~size ~mtime_ns =
  match Sqlite3.prepare (db t) "SELECT size, mtime_ns, sha256 FROM files WHERE path=? AND gone=0" with
  | exception Sqlite3.Error _ -> None
  | st ->
      Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
        ignore (Sqlite3.bind st 1 (Sqlite3.Data.TEXT path));
        match Sqlite3.step st with
        | Sqlite3.Rc.ROW when Sqlite3.column st 0 = Sqlite3.Data.INT size && Sqlite3.column st 1 = Sqlite3.Data.INT mtime_ns ->
            (match Sqlite3.column st 2 with Sqlite3.Data.TEXT s when s <> "" -> Some s | _ -> None)
        | _ -> None)

(* [record]'s reason for a file whose type phindex does not read metadata
   from: not an error; the row lists the side under "no_metadata". *)
let not_read_reason = "metadata not read for this file type"

(* Whether phindex identified [path] and did not attempt its metadata, as it
   does for a file type the engine does not read (a .cop cache proxy). An index
   from before the identity columns has none. *)
let not_read t path =
  let st = try Some (Sqlite3.prepare (db t)
    "SELECT sha256 IS NOT NULL AND md_digest IS NULL AND md_err IS NULL FROM files WHERE path=? AND gone=0")
    with Sqlite3.Error _ -> None in
  match st with
  | None -> false
  | Some st ->
      Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
        ignore (Sqlite3.bind st 1 (Sqlite3.Data.TEXT path));
        match Sqlite3.step st with
        | Sqlite3.Rc.ROW -> Sqlite3.column st 0 = Sqlite3.Data.INT 1L
        | _ -> false)

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
      | None when not_read t path -> Error not_read_reason
      | None -> Error "phindex has no metadata record"
      | Some (Sqlite3.Data.BLOB b) -> Ok (Py.dedup (Json.parse (Native.inflate_raw b)))
      | Some _ -> failwith (path ^ ": phindex record_deflate is not a blob")

(* ---- Where a content is now (docs/dngc-archive-spec.md, 5.5) ----

   One index asked where a content is: the live files it lists with a SHA-256,
   and the files that share an edit-surviving identity (dng_blocks,
   image_data) with a content or a path. Its answers are hints; the reader
   checks every file it is pointed at by digest. The Rust library's
   phindex::Index and content_index.py do the same. *)

(* A file an index lists: its path, and the size and SHA-256 of its row. *)
type listed = { path : string; size : int64; sha256 : string }

(* Opened read-only with immutable=1 (nothing is written beside it). Every
   lookup is a point query on an index of [files]; a column the database has
   no index for is not looked up ([unindexed] names it). *)
type lookup = { ldb : Sqlite3.db; by_sha256 : bool; identities : string list; unindexed : string list }

(* The identities that survive a metadata edit, as columns of [files]. *)
let identity_columns = ["dng_blocks"; "image_data"]

let holding_query = "SELECT path, size, sha256 FROM files WHERE sha256=?1 AND gone=0 LIMIT ?2"

let lookup_rows db sql params =
  let st = Sqlite3.prepare db sql in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
    List.iteri (fun i p -> ignore (Sqlite3.bind st (i + 1) p)) params;
    let rec go acc = match Sqlite3.step st with
      | Sqlite3.Rc.ROW -> go (Sqlite3.row_data st :: acc)
      | Sqlite3.Rc.DONE -> List.rev acc
      | rc -> failwith (Printf.sprintf "%s (%s)" (Sqlite3.Rc.to_string rc) (Sqlite3.errmsg db)) in
    go [])

let open_lookup path =
  if not (Sys.file_exists path) || Sys.is_directory path then failwith ("no phindex index at " ^ path);
  let real = try Unix.realpath path with Unix.Unix_error _ -> path in
  (* In a URI filename % ? and # are syntax. *)
  let b = Buffer.create (String.length real) in
  String.iter (function '%' -> Buffer.add_string b "%25" | '?' -> Buffer.add_string b "%3f"
                        | '#' -> Buffer.add_string b "%23" | c -> Buffer.add_char b c) real;
  let ldb = Sqlite3.db_open ~mode:`READONLY ~uri:true ("file:" ^ Buffer.contents b ^ "?immutable=1") in
  let names q = List.filter_map (fun r -> match r.(0) with Sqlite3.Data.TEXT s -> Some s | _ -> None)
      (try lookup_rows ldb q [] with Failure _ | Sqlite3.Error _ -> []) in
  let columns = names "SELECT name FROM pragma_table_info('files')" in
  if not (List.for_all (fun c -> List.mem c columns) ["path"; "size"; "sha256"; "gone"]) then begin
    ignore (Sqlite3.db_close ldb);
    failwith (path ^ " is not a phindex index")
  end;
  (* The columns an index of files starts with: only those are looked up. *)
  let leading = names "SELECT i.name FROM pragma_index_list('files') l, pragma_index_info(l.name) i WHERE i.seqno=0" in
  let by_sha256 = List.mem "sha256" leading in
  let present = List.filter (fun c -> List.mem c columns) identity_columns in
  { ldb; by_sha256; identities = List.filter (fun c -> List.mem c leading) present;
    unindexed = (if by_sha256 then [] else ["sha256"]) @ List.filter (fun c -> not (List.mem c leading)) present }

let close_lookup t = ignore (Sqlite3.db_close t.ldb)

let listed_rows t query key limit =
  List.filter_map (fun r ->
    match r.(0), r.(1) with
    | Sqlite3.Data.TEXT path, Sqlite3.Data.INT size ->
        Some { path; size; sha256 = (match r.(2) with Sqlite3.Data.TEXT s -> s | _ -> "") }
    | _ -> None)
    (lookup_rows t.ldb query [Sqlite3.Data.TEXT key; Sqlite3.Data.INT (Int64.of_int limit)])

let identity_query t key =
  Printf.sprintf "SELECT %s FROM files WHERE %s=?1 LIMIT ?2"
    (String.concat ", " (List.map (fun c -> if List.mem c t.identities then c else "NULL") identity_columns)) key

let sharing_query column = Printf.sprintf "SELECT path, size, sha256 FROM files WHERE %s=?1 AND gone=0 LIMIT ?2" column

(* Live files (not gone) listed with this SHA-256, at most [limit]. *)
let holding t sha256 limit = if t.by_sha256 then listed_rows t holding_query sha256 limit else []

(* The edit-surviving identities, as (column, value), of the rows listed with
   this SHA-256, gone or not (a gone row keeps its digests), and of the rows
   at [paths], whatever content they list now (a row whose file was edited in
   place has a new SHA-256 and the same identity). *)
let identities t sha256 paths limit =
  if t.identities = [] then []
  else begin
    let out = ref [] in
    let take query key =
      List.iter (fun r ->
        List.iteri (fun i c ->
          match r.(i) with
          | Sqlite3.Data.TEXT v when v <> "" && not (List.mem (c, v) !out) -> out := (c, v) :: !out
          | _ -> ()) identity_columns)
        (lookup_rows t.ldb query [Sqlite3.Data.TEXT key; Sqlite3.Data.INT (Int64.of_int limit)]) in
    if t.by_sha256 then take (identity_query t "sha256") sha256;
    List.iter (take (identity_query t "path")) paths;
    List.rev !out
  end

(* Live files listed with this identity, at most [limit]. *)
let sharing t column value limit =
  if List.mem column t.identities then listed_rows t (sharing_query column) value limit else []

(* Each lookup this index answers, with its EXPLAIN QUERY PLAN. *)
let plans t =
  let queries =
    (if t.by_sha256 then [holding_query] else [])
    @ (if t.identities = [] then [] else (if t.by_sha256 then [identity_query t "sha256"] else []) @ [identity_query t "path"])
    @ List.map sharing_query t.identities in
  List.map (fun q ->
    q, String.concat "; " (List.filter_map (fun r -> match r.(3) with Sqlite3.Data.TEXT s -> Some s | _ -> None)
                             (lookup_rows t.ldb ("EXPLAIN QUERY PLAN " ^ q) [Sqlite3.Data.TEXT ""; Sqlite3.Data.INT 1L]))) queries

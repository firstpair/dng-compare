(* dng-archive phindex-metadata: phindex_metadata.py in OCaml. For listed pairs
   whose phindex rows are current, it writes an archive's metadata section
   (metadata/batches/phindex-NNNNNN.jsonl.gz in portable_metadata's row format,
   then metadata/differences.sqlite3 with [Index]) from the exiftool-rs records
   the two phindex indexes keep, without reading the files. The rows parse to
   the Python tool's rows: records are carried value for value, in their key
   order, and each line is json.dumps(row, ensure_ascii=True)'s text. Records
   of another engine, or an archive whose metadata came from one, are refused. *)

let engine_tag = "ExifTool:DngCompareEngine"
let prefix = "phindex-"

let usage_text =
  "usage: dng-archive phindex-metadata LEFT RIGHT ARCHIVE --pairs FILE --left-index FILE --right-index FILE [--batch-size N] [--progress-secs S]"

let usage_error msg =
  prerr_endline usage_text;
  prerr_endline ("dng-archive phindex-metadata: error: " ^ msg);
  raise (Py.System_exit 2)

(* raise SystemExit(msg): the message on stderr, exit status 1. *)
exception Stop of string

(* str(pathlib.Path(p)): no empty or "." components, no trailing slash. *)
let path_str p =
  let parts = List.filter (fun c -> c <> "" && c <> ".") (String.split_on_char '/' p) in
  let body = String.concat "/" parts in
  if String.length p > 0 && p.[0] = '/' then "/" ^ body else if body = "" then "." else body

(* os.path.join(root, f) *)
let join root f =
  if String.length f > 0 && f.[0] = '/' then f
  else if String.ends_with ~suffix:"/" root then root ^ f else root ^ "/" ^ f

let components p = List.filter (fun c -> c <> "" && c <> ".") (String.split_on_char '/' p)

(* os.path.commonpath([p, root]) == root, for absolute paths. *)
let under root p =
  let rec prefix a b = match a, b with
    | [], _ -> true
    | x :: a, y :: b -> x = y && prefix a b
    | _ :: _, [] -> false in
  prefix (components root) (components p)

(* os.path.relpath(p, root), for absolute paths. *)
let relpath p root =
  let norm p = List.rev (List.fold_left (fun acc c ->
    if c = ".." then (match acc with _ :: rest -> rest | [] -> []) else c :: acc) [] (components p)) in
  let rec drop a b = match a, b with
    | x :: a', y :: b' when x = y -> drop a' b'
    | _ -> a, b in
  let rest, up = drop (norm p) (norm root) in
  match List.map (fun _ -> "..") up @ rest with [] -> "." | l -> String.concat "/" l

(* str.splitlines() *)
let splitlines s =
  let n = String.length s and lines = ref [] and start = ref 0 and i = ref 0 in
  let cut stop next = lines := String.sub s !start (stop - !start) :: !lines; start := next; i := next in
  while !i < n do
    match s.[!i] with
    | '\r' when !i + 1 < n && s.[!i + 1] = '\n' -> cut !i (!i + 2)
    | '\n' | '\r' | '\011' | '\012' | '\x1c' | '\x1d' | '\x1e' -> cut !i (!i + 1)
    | '\xc2' when !i + 1 < n && s.[!i + 1] = '\x85' -> cut !i (!i + 2)
    | '\xe2' when !i + 2 < n && s.[!i + 1] = '\x80' && (s.[!i + 2] = '\xa8' || s.[!i + 2] = '\xa9') -> cut !i (!i + 3)
    | _ -> incr i
  done;
  if !start < n then lines := String.sub s !start (n - !start) :: !lines;
  List.rev !lines

let read_file path =
  match In_channel.with_open_bin path In_channel.input_all with
  | s -> s
  | exception Sys_error _ ->
      (* The OSError text Python reports. *)
      (match Unix.openfile path [Unix.O_RDONLY] 0 with
       | fd -> Unix.close fd; failwith (path ^ ": unreadable")
       | exception Unix.Unix_error (e, _, _) -> failwith (Py.oserror e path))

let read_pairs path roots =
  let left_root, right_root = roots in
  let shown = path_str path in
  List.concat (List.mapi (fun k line ->
    if Py.strip line = "" || String.starts_with ~prefix:"#" (Py.strip line) then []
    else match String.split_on_char '\t' line with
      | [l; r] when l <> "" && r <> "" -> [join left_root l, join right_root r]
      | _ -> failwith (Printf.sprintf "%s:%d: expected LEFT<TAB>RIGHT" shown (k + 1)))
    (splitlines (read_file path)))

(* metadata_index.read_rows *)
let read_rows path =
  List.filter_map (fun line -> if line = "" then None else Some (Py.dedup (Json.parse line)))
    (String.split_on_char '\n' (Native.gunzip (In_channel.with_open_bin path In_channel.input_all)))

let get k r = match Json.member k r with Some v -> v | None -> failwith ("row without " ^ k)

let batch_files archive =
  let dir = Filename.concat archive "metadata/batches" in
  List.map (Filename.concat dir) (List.sort compare (List.filter (fun f -> Filename.check_suffix f ".jsonl.gz")
    (Array.to_list (try Sys.readdir dir with Sys_error _ -> [||]))))

(* The engine of the archive's existing batches: a stamp, "Perl ExifTool", or
   Null when no row has metadata. *)
let existing_engine archive =
  let exception Found of Json.t in
  try
    List.iter (fun path ->
      List.iter (fun row ->
        List.iter (fun side ->
          match Json.member side row with
          | Some m when Json.truthy m ->
              raise (Found (Option.value (Json.member engine_tag m) ~default:(Json.Str "Perl ExifTool")))
          | _ -> ()) ["apo_metadata"; "bumer_metadata"]) (read_rows path)) (batch_files archive);
    Json.Null
  with Found e -> e

(* portable_metadata.properties *)
let properties p =
  let s = Native.stat p in
  let mode, birth = Native.stat_extra p in
  let xattrs = List.map (fun n -> n, Json.Str (Native.base64_encode (Native.getxattr_follow p n)))
      (Native.listxattr_follow p) in
  Json.Obj [("mode", Json.int mode); ("uid", Json.int s.uid); ("gid", Json.int s.gid);
            ("mtime_ns", Json.int64 s.mtime_ns); ("atime_ns", Json.int64 s.atime_ns);
            ("ctime_ns", Json.int64 s.ctime_ns); ("birthtime", Json.opt Json.float birth);
            ("flags", Json.int64 s.flags); ("xattrs", Json.Obj xattrs)]

let skipped k = k = "SourceFile" || String.starts_with ~prefix:"ExifTool:" k || String.starts_with ~prefix:"Composite:" k

(* portable_metadata.compare, with each change's category added. *)
let compare_records left right =
  let fields = function Json.Obj l -> l | _ -> [] in
  let l = fields left and r = fields right in
  let keys = List.sort_uniq compare (List.map fst l @ List.map fst r) in
  List.filter_map (fun k ->
    if skipped k then None else
    match List.assoc_opt k l, List.assoc_opt k r with
    | Some a, Some b when Py.equal a b -> None
    | a, b ->
        let kind = match a, b with None, _ -> "added_in_bumer" | _, None -> "missing_in_bumer" | _ -> "changed_in_bumer" in
        Some (Json.Obj ([("tag", Json.Str k); ("kind", Json.Str kind)]
                        @ (match a with Some v -> [("apo", v)] | None -> [])
                        @ (match b with Some v -> [("bumer", v)] | None -> [])
                        @ [("category", Json.Str (if String.starts_with ~prefix:"File:" k then "filesystem" else "embedded"))]))) keys

(* Path.with_suffix(".tmp") *)
let with_tmp path = Filename.concat (Filename.dirname path) (Filename.remove_extension (Filename.basename path) ^ ".tmp")

(* portable_metadata.atomic_json *)
let atomic_json path v =
  let temp = with_tmp path in
  Out_channel.with_open_bin temp (fun oc ->
    output_string oc (Json.dumps_indent v); flush oc; Unix.fsync (Unix.descr_of_out_channel oc));
  Sys.rename temp path

(* Python's Counter: keys in first-insertion order. *)
type counter = { mutable keys : string list; table : (string, int) Hashtbl.t }
let counter () = { keys = []; table = Hashtbl.create 16 }
let bump c k n =
  match Hashtbl.find_opt c.table k with
  | Some v -> Hashtbl.replace c.table k (v + n)
  | None -> c.keys <- c.keys @ [k]; Hashtbl.replace c.table k n
let count_of c k = Option.value (Hashtbl.find_opt c.table k) ~default:0

type options = {
  left : string; right : string; archive : string; pairs : string;
  left_index : string; right_index : string; batch_size : int; progress_secs : float;
}

let parse_args args =
  let valued = ["--pairs"; "--left-index"; "--right-index"; "--batch-size"; "--progress-secs"] in
  let rec go pos opts = function
    | [] -> List.rev pos, opts
    | f :: rest when String.contains f '=' && String.starts_with ~prefix:"--" f ->
        let i = String.index f '=' in
        go pos opts (String.sub f 0 i :: String.sub f (i + 1) (String.length f - i - 1) :: rest)
    | f :: v :: rest when List.mem f valued -> go pos ((f, v) :: opts) rest
    | f :: [] when List.mem f valued -> usage_error (Printf.sprintf "argument %s: expected one argument" f)
    | f :: _ when String.length f > 1 && f.[0] = '-' -> usage_error ("unrecognized arguments: " ^ f)
    | a :: rest -> go (a :: pos) opts rest in
  let pos, opts = go [] [] args in
  let opt k = List.assoc_opt k opts in
  let num conv kind k default =
    match opt k with
    | None -> default
    | Some v -> (match conv (String.trim v) with
        | Some n -> n
        | None -> usage_error (Printf.sprintf "argument %s: invalid %s value: %s" k kind (Py.repr_string ~bytes:false v))) in
  match pos with
  | [left; right; archive] ->
      (match List.filter (fun k -> opt k = None) ["--pairs"; "--left-index"; "--right-index"] with
       | [] -> ()
       | missing -> usage_error ("the following arguments are required: " ^ String.concat ", " missing));
      { left; right; archive; pairs = Option.get (opt "--pairs");
        left_index = Option.get (opt "--left-index"); right_index = Option.get (opt "--right-index");
        batch_size = num int_of_string_opt "int" "--batch-size" 1000;
        progress_secs = num float_of_string_opt "float" "--progress-secs" 60. }
  | _ when List.length pos < 3 ->
      usage_error ("the following arguments are required: "
                   ^ String.concat ", " (List.filteri (fun i _ -> i >= List.length pos) ["left"; "right"; "archive"]))
  | _ :: _ :: _ :: extra :: _ -> usage_error ("unrecognized arguments: " ^ extra)
  | _ -> usage_error "bad arguments"

(* One pair's row, as phindex_metadata.py builds it. [check] refuses a record
   whose engine stamp differs from the run's or the archive's. *)
let pair_row ~check rel (left, right) indexes =
  let errors = ref [] and warnings = ref [] and records = ref [] and fields = ref [] in
  let add k v = fields := !fields @ [(k, v)] in
  let set k v = fields := List.map (fun (k', v') -> if k' = k then (k', v) else (k', v')) !fields in
  add "relative_path" (Json.Str rel); add "apo" (Json.Str left); add "bumer" (Json.Str right);
  List.iter (fun (side, path, index) ->
    match
      let s = Native.stat path in
      add (side ^ "_stamp") (Json.Arr [Json.int64 s.ino; Json.int64 s.size; Json.int64 s.mtime_ns; Json.int64 s.ctime_ns]);
      (* An unreadable attribute makes the stamp None, as in Python. *)
      (try if side = "apo" then add "apo_filesystem" (properties path)
       with Unix.Unix_error _ as e -> set "apo_stamp" Json.Null; raise e);
      s
    with
    | exception Unix.Unix_error (e, _, _) ->
        if not (List.mem_assoc (side ^ "_stamp") !fields) then add (side ^ "_stamp") Json.Null;
        errors := !errors @ [Json.Obj [("side", Json.Str side); ("error", Json.Str (Py.oserror e path))]]
    | s ->
        match Phindex.record index path ~size:s.size ~mtime_ns:s.mtime_ns with
        | Error reason -> errors := !errors @ [Json.Obj [("side", Json.Str side); ("error", Json.Str reason)]]
        | Ok record ->
            (match Json.member engine_tag record with
             | None | Some Json.Null -> raise (Stop (Printf.sprintf "%s: phindex record has no %s" path engine_tag))
             | Some stamp -> check path stamp);
            records := (side, record) :: !records;
            (match record with
             | Json.Obj l -> List.iter (fun (k, v) ->
                 let last = match String.rindex_opt k ':' with Some i -> String.sub k (i + 1) (String.length k - i - 1) | None -> k in
                 if last = "Error" then errors := !errors @ [Json.Obj [("side", Json.Str side); ("error", v)]];
                 if last = "Warning" then warnings := !warnings @ [Json.Obj [("side", Json.Str side); ("warning", v)]]) l
             | _ -> failwith (path ^ ": phindex record is not a JSON object")))
    [("apo", left, fst indexes); ("bumer", right, snd indexes)];
  let meta side = Option.value (List.assoc_opt side !records) ~default:(Json.Obj []) in
  let a = meta "apo" and b = meta "bumer" in
  let changes = if !errors = [] then compare_records a b else [] in
  Json.Obj (!fields @ [("apo_metadata", a); ("bumer_metadata", b); ("differences", Json.Arr changes);
                       ("errors", Json.Arr !errors); ("warnings", Json.Arr !warnings)])

let write_batch dest rows =
  let temp = with_tmp dest in
  let text = String.concat "" (List.map (fun r -> Json.dumps r ^ "\n") rows) in
  Out_channel.with_open_bin temp (fun oc -> output_string oc (Native.gzip text 1));
  Native.fsync_path temp;
  let saved = List.filter_map (fun l -> if l = "" then None else Some (Json.parse l))
      (String.split_on_char '\n' (Native.gunzip (In_channel.with_open_bin temp In_channel.input_all))) in
  if not (List.length saved = List.length rows && List.for_all2 Py.equal saved rows) then
    raise (Stop "Metadata archive read-back failed");
  Sys.rename temp dest

let run_ args =
  let o = parse_args args in
  if not (1 <= o.batch_size && o.batch_size <= 100000) then usage_error "batch-size must be 1..100000";
  let roots = List.map (fun p -> Preserve.resolve (Preserve.absolute p)) [o.left; o.right] in
  if not (List.for_all (fun p -> match Native.stat p with s -> s.kind = `Dir | exception Unix.Unix_error _ -> false) roots) then
    usage_error "Both roots must be readable directories";
  let left_root = List.nth roots 0 and right_root = List.nth roots 1 in
  List.iter (fun p -> if not (Preserve.is_file p) then usage_error ("phindex index not found: " ^ path_str p))
    [o.left_index; o.right_index];
  let pairs, archive =
    try
      let pairs = read_pairs o.pairs (left_root, right_root) in
      (* Given ARCHIVE/bytes, the metadata section is at the archive's top. *)
      pairs, Preserve.validate_output left_root right_root (Preserve.archive_top o.archive)
    with Failure m | Sys_error m -> usage_error m | Unix.Unix_error (e, _, p) -> usage_error (Py.oserror e p) in
  List.iter (fun (l, _) -> if not (under left_root l) then usage_error (Printf.sprintf "%s: not under %s" l left_root)) pairs;
  let indexes = Phindex.open_ o.left_index, Phindex.open_ o.right_index in
  let total = List.length pairs and pairs = Array.of_list pairs in
  try
    Preserve.with_archive_lock archive ".metadata-preservation.lock" (fun () ->
      let batches = Filename.concat archive "metadata/batches" in
      Native.mkdir_p batches;
      let prior = existing_engine archive in
      let engine = ref Json.Null in
      let db = Index.open_index archive in
      Fun.protect ~finally:(fun () -> Native.close_db db) (fun () ->
        let counts = counter () and tags = Hashtbl.create 64 and tag_order = ref [] in
        let start = Unix.gettimeofday () in
        let last = ref start in
        Printf.printf "%s pairs; archive %s\n%!" (Py.thousands total) archive;
        let check path stamp =
          if not (Json.truthy !engine) then engine := stamp;
          if not (Py.equal stamp !engine) then
            raise (Stop (Printf.sprintf "%s: engine %s differs from %s; not mixing engines" path (Py.repr stamp) (Py.repr !engine)));
          if prior <> Json.Null && not (Py.equal stamp prior) then
            raise (Stop (Printf.sprintf "%s: its metadata comes from %s, these records from %s; not mixing engines"
                           archive (Py.repr prior) (Py.repr stamp))) in
        (* What the archive's batches already hold. A pair whose source is
           recorded with its current size and mtime_ns is not read again (a
           rerun, or a resumed run); a new content of a recorded relative path
           (a refilled shuttle) gets this run's UTC stamp as its version, as
           the index tells contents of one path apart. New batches continue
           the numbering. *)
        let recorded = Hashtbl.create 1024 and known = Hashtbl.create 1024 and numbers = ref [] in
        List.iter (fun path ->
          let f = Filename.basename path in
          let n = String.length prefix in
          if String.length f = n + 15 && String.starts_with ~prefix f && Filename.check_suffix f ".jsonl.gz" then
            Option.iter (fun k -> numbers := k :: !numbers)
              (let d = String.sub f n 6 in if String.for_all (fun c -> c >= '0' && c <= '9') d then int_of_string_opt d else None);
          List.iter (fun r ->
            let rel = match Json.member "relative_path" r with Some v -> v | None -> Json.Null in
            Hashtbl.replace known rel ();
            match Json.member "apo_stamp" r with
            | Some (Json.Arr (_ :: Json.Num size :: Json.Num mtime :: _)) ->
                Hashtbl.replace recorded (rel, Int64.of_string size, Int64.of_string mtime) r
            | _ -> ()) (read_rows path)) (batch_files archive);
        let number = ref (match !numbers with [] -> 0 | l -> 1 + List.fold_left max 0 l) in
        let version =
          let t = Unix.gmtime (Unix.time ()) in
          Printf.sprintf "%04d%02d%02dT%02d%02d%02dZ" (t.tm_year + 1900) (t.tm_mon + 1) t.tm_mday t.tm_hour t.tm_min t.tm_sec in
        let reused = ref [] and todo = ref [] and missing = ref [] in
        Array.iter (fun (l, r) ->
          let rel = relpath l left_root in
          match Native.stat l with
          | exception Unix.Unix_error (Unix.ENOENT, _, _) -> missing := rel :: !missing
          | st ->
              match Hashtbl.find_opt recorded (Json.Str rel, st.size, st.mtime_ns) with
              | Some row -> reused := row :: !reused
              | None -> todo := (rel, (l, r)) :: !todo) pairs;
        let reused = List.rev !reused and todo = Array.of_list (List.rev !todo) and missing = List.rev !missing in
        List.iter (fun rel -> Printf.printf "MISSING %s\n%!" rel) missing;
        let count rows = List.iter (fun row ->
          let diffs = match get "differences" row with Json.Arr l -> l | _ -> [] in
          let embedded d = Json.member "category" d = Some (Json.Str "embedded") in
          bump counts "files_processed" 1;
          bump counts "metadata_errors" (Bool.to_int (Json.truthy (get "errors" row)));
          bump counts "files_with_embedded_differences" (Bool.to_int (List.exists embedded diffs));
          List.iter (fun d -> if embedded d then begin
              let key = Json.str (get "kind" d), Json.str (get "tag" d) in
              if not (Hashtbl.mem tags key) then tag_order := key :: !tag_order;
              Hashtbl.replace tags key (1 + Option.value (Hashtbl.find_opt tags key) ~default:0)
            end) diffs;
          if not (Json.truthy !engine) then
            engine := (match List.find_map (fun side -> Json.member engine_tag (get side row)) ["apo_metadata"; "bumer_metadata"] with
                       | Some v -> v | None -> Json.Null)) rows in
        count reused;
        let total_new = Array.length todo and first = ref 0 in
        while !first < total_new do
          let chunk = Array.to_list (Array.sub todo !first (min o.batch_size (total_new - !first))) in
          let dest = Filename.concat batches (Printf.sprintf "%s%06d.jsonl.gz" prefix !number) in
          incr number;
          let rows = List.map (fun (rel, pair) ->
            let row = pair_row ~check rel pair indexes in
            if Hashtbl.mem known (Json.Str rel) then
              (match row with
               | Json.Obj ((k, v) :: rest) -> Json.Obj ((k, v) :: ("version", Json.Str version) :: rest)
               | r -> r)
            else row) chunk in
          write_batch dest rows;
          ignore (Index.ingest db dest);
          count rows;
          let now = Unix.gettimeofday () in
          if now -. !last >= o.progress_secs || !first + o.batch_size >= total_new then begin
            last := now;
            Printf.printf "%s/%s new pairs | errors=%s | %.0fs\n%!" (Py.thousands (min (!first + o.batch_size) total_new))
              (Py.thousands total_new) (Py.thousands (count_of counts "metadata_errors")) (now -. start)
          end;
          first := !first + o.batch_size
        done;
        (* Batches an interrupted run wrote but did not index (ingest is idempotent). *)
        List.iter (fun path -> if String.starts_with ~prefix (Filename.basename path) then ignore (Index.ingest db path))
          (batch_files archive);
        (* Counter.most_common(100): by count, ties in first-seen order. *)
        let common = List.filteri (fun i _ -> i < 100)
            (List.stable_sort (fun (_, a) (_, b) -> compare b a)
               (List.map (fun k -> k, Hashtbl.find tags k) (List.rev !tag_order))) in
        let s x = Json.Str x in
        atomic_json (Filename.concat archive "metadata/summary.json") (Json.Obj (
          [("schema", s "apo-bumer-metadata-v1"); ("source", s left_root); ("reference", s right_root);
           ("total_files", Json.int total); ("complete", Json.Bool true)]
          @ List.map (fun k -> k, Json.int (count_of counts k)) counts.keys
          @ [("rows_added", Json.int total_new); ("missing_sources", Json.int (List.length missing));
             ("elapsed_seconds", Json.float (Unix.gettimeofday () -. start)); ("safe_to_delete_source", Json.Bool false);
             ("note", s "From phindex indexes (exiftool-rs records; values over 1 KiB kept as long-sha256 references). \
                         Exact original files are preserved separately.");
             ("common_changes", Json.Arr (List.map (fun ((kind, tag), n) ->
                Json.Obj [("kind", s kind); ("tag", s tag); ("count", Json.int n)]) common))]));
        let resolved p = Preserve.resolve (Preserve.absolute p) in
        atomic_json (Filename.concat archive "metadata/provenance.json") (Json.Obj [
          ("tool", s "dng-archive phindex-metadata"); ("engine", !engine); ("source", s left_root); ("reference", s right_root);
          ("left_index", s (resolved o.left_index)); ("right_index", s (resolved o.right_index));
          ("pairs", s (resolved o.pairs)); ("long_values", s "long-sha256:<hex>:<length> for strings over 1 KiB")]);
        let n sql = match Index.query db sql [] with [ [| v |] ] -> Json.int (Index.int_of v) | _ -> Json.Null in
        print_endline (Json.dumps (Json.Obj [
          ("indexed_files", n "SELECT COUNT(*) FROM files"); ("differences", n "SELECT COUNT(*) FROM differences");
          ("errors", Json.int (count_of counts "metadata_errors")); ("engine", !engine)]));
        flush stdout;
        if count_of counts "metadata_errors" = 0 && missing = [] then 0 else 1))
  with Stop msg -> flush stdout; prerr_endline msg; 1

(* dng-archive phindex-metadata ARGS: its exit status; usage errors give 2. *)
let run args = try run_ args with Py.System_exit n -> n

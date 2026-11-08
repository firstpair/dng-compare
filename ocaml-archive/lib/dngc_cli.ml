(* dngc: one command for dng-compare's comparison, preservation and metadata
   tools, as a native binary (docs/dngc.md). dngc.py is the reference; this
   module implements its verbs, options, defaults, archive layout, messages and
   exit statuses by calling the library directly:

     run      DNG pairs through [All.run_options]; other and reference-less
              pairs through [Preserve.scan]; --metadata phindex through
              [Phindex_metadata.run]; the whole-tree walk in OCaml.
     restore  [Preserve.restore]
     index    [Index.index_archive]
     query, status, verify  here, on the archive's SQLite files.

   Still spawned: `phindex align` for --index without --pairs, and, with
   --impl rust or --impl python, the Rust binary or the Python tools, as
   dngc.py runs them. Each step prints a "+ " line as dngc.py does; for a step
   run in this process it is the equivalent dng-archive command. *)

let schema = "dngc-archive-1"

exception Die = Py.System_exit

(* dngc.py's die(): "dngc: error: MSG" on stderr. *)
let die ?(status = 2) msg =
  Printf.eprintf "dngc: error: %s\n%!" msg;
  raise (Die status)

(* ---- Paths, as Python spells them ---- *)

let is_abs p = String.length p > 0 && p.[0] = '/'

(* os.path.join(a, b) *)
let join = Phindex_metadata.join

(* os.path.abspath: joined to the working directory, then normpath. *)
let abspath p =
  let p = if is_abs p then p else join (Sys.getcwd ()) p in
  let parts = List.fold_left (fun acc c ->
    match c with
    | "" | "." -> acc
    | ".." -> (match acc with _ :: rest -> rest | [] -> [])
    | c -> c :: acc) [] (String.split_on_char '/' p) in
  "/" ^ String.concat "/" (List.rev parts)

(* pathlib.Path(p).absolute() *)
let path_absolute p = Phindex_metadata.path_str (if is_abs p then p else join (Sys.getcwd ()) p)

(* os.path.realpath *)
let realpath p = Preserve.resolve (Preserve.absolute p)

let exists p = Sys.file_exists p
let is_file = Preserve.is_file
let is_dir p = match Native.stat p with s -> s.kind = `Dir | exception Unix.Unix_error _ -> false
(* os.access(p, os.X_OK) *)
let executable p = p <> "" && (try Unix.access p [Unix.X_OK]; true with Unix.Unix_error _ -> false)

let read_text p = In_channel.with_open_bin p In_channel.input_all

let write_text p s = Out_channel.with_open_bin p (fun oc -> output_string oc s)

let rec rm_rf p =
  match Native.stat ~follow:false p with
  | s when s.kind = `Dir ->
      Array.iter (fun n -> rm_rf (Filename.concat p n)) (try Sys.readdir p with Sys_error _ -> [||]);
      (try Unix.rmdir p with Unix.Unix_error _ -> ())
  | _ -> (try Sys.remove p with Sys_error _ -> ())
  | exception Unix.Unix_error _ -> ()

let which exe =
  List.find_map (fun d -> let p = Filename.concat d exe in if executable p then Some p else None)
    (String.split_on_char ':' (Option.value (Sys.getenv_opt "PATH") ~default:""))

(* ---- The checkout and the programs dngc may run ---- *)

(* dngc.py's HERE: the directory holding the Python tools (DNG_COMPARE_DIR, else
   the nearest directory above this executable holding compare_dng.py). *)
let here () = Lazy.force Compare.compare_dir

let here_or_die what =
  match here () with
  | Some d -> d
  | None -> die (what ^ " needs the dng-compare checkout; set DNG_COMPARE_DIR")

let python () =
  let venv = Option.map (fun d -> Filename.concat d ".venv/bin/python") (here ()) in
  match venv with
  | Some p when exists p -> p
  | _ -> (match which "python3" with Some p -> p | None -> "python3")

let first_executable candidates = List.find_opt (fun c -> c <> "" && executable c) candidates

let rust_binary () =
  let c = match Sys.getenv_opt "DNG_COMPARE_ALL_RUST" with
    | Some c when c <> "" -> c
    | _ -> (match here () with Some d -> Filename.concat d "rust-scanner/target/release/dng-compare-all" | None -> "") in
  if executable c then Some c else None

let phindex_binary () =
  first_executable (List.filter_map Fun.id [
    Sys.getenv_opt "PHINDEX";
    Option.map (fun d -> Filename.concat (Filename.dirname d) "phindex/rust/target/release/phindex") (here ());
    which "phindex"])

let echo command = Printf.printf "+ %s\n%!" (String.concat " " command)

(* dngc.py's call(): run a program in the checkout, its output passed through. *)
let call command =
  echo command;
  let dir = Option.value (here ()) ~default:(Sys.getcwd ()) in
  let exe = List.hd command in
  let exe = if String.contains exe '/' then exe else Option.value (which exe) ~default:exe in
  let pid = Native.spawn_in dir (Array.of_list (exe :: List.tl command)) Unix.stdout Unix.stderr in
  Native.waitpid_raw pid false

let error_text = function
  | Failure e | Sys_error e | Json.Bad e -> e
  | Unix.Unix_error (e, f, a) -> Printf.sprintf "%s: %s %s" f (Unix.error_message e) a
  | e -> Printexc.to_string e

(* A step run in this process: [command] is the equivalent dng-archive command.
   Its exit status, as that command would give it. *)
let step command f =
  echo command;
  match f () with
  | n -> flush stdout; n
  | exception Die n -> flush stdout; n
  | exception (Sys.Break as e) -> raise e
  | exception (Stack_overflow | Out_of_memory as e) -> raise e
  | exception e -> flush stdout; Printf.eprintf "dngc: %s\n%!" (error_text e); 1

(* ---- Archive layout ---- *)

type archive = {
  root : string; legacy : bool; report : string; bytes : string; metadata : string; manifest : string;
}

let archive_of p =
  (* The sections by the rule every tool shares (Preserve.byte_section): archives
     from before dngc keep their byte section at the top; ARCHIVE/bytes given
     directly is the byte section of its parent. *)
  let given = path_absolute p in
  let root = Preserve.archive_top given and bytes = Preserve.byte_section given in
  { root; legacy = bytes = root; report = join root "report"; bytes;
    metadata = join root "metadata"; manifest = join root "ARCHIVE.json" }

let read_manifest a =
  if exists a.manifest then Json.parse (read_text a.manifest)
  else Json.Obj [("schema", Json.Str schema); ("runs", Json.Arr [])]

let runs m = match Json.member "runs" m with Some (Json.Arr l) -> l | _ -> failwith "ARCHIVE.json: no runs"

let record_run a entry =
  let m = read_manifest a in
  let m = match m with
    | Json.Obj l -> Json.Obj (List.map (fun (k, v) -> k, if k = "runs" then Json.Arr (runs m @ [entry]) else v) l)
    | _ -> failwith "ARCHIVE.json is not an object" in
  Native.mkdir_p a.root;
  let tmp = join a.root "ARCHIVE.tmp" in
  write_text tmp (Json.dumps_indent m ^ "\n");
  Sys.rename tmp a.manifest

(* time.strftime('%Y-%m-%dT%H:%M:%S%z') *)
let timestamp () =
  let t = Unix.time () in
  let l = Unix.localtime t in
  (* The local time read as UTC, minus the time: the zone's offset. *)
  let days_from_civil y m d =
    let y = if m <= 2 then y - 1 else y in
    let era = (if y >= 0 then y else y - 399) / 400 in
    let yoe = y - era * 400 in
    let doy = (153 * (if m > 2 then m - 3 else m + 9) + 2) / 5 + d - 1 in
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy in
    era * 146097 + doe - 719468 in
  let local = float (days_from_civil (l.tm_year + 1900) (l.tm_mon + 1) l.tm_mday) *. 86400.
              +. float (l.tm_hour * 3600 + l.tm_min * 60 + l.tm_sec) in
  let off = int_of_float (Float.round (local -. Float.of_int (int_of_float t))) / 60 in
  Printf.sprintf "%04d-%02d-%02dT%02d:%02d:%02d%c%02d%02d" (l.tm_year + 1900) (l.tm_mon + 1) l.tm_mday
    l.tm_hour l.tm_min l.tm_sec (if off < 0 then '-' else '+') (abs off / 60) (abs off mod 60)

(* ---- Pairs ---- *)

(* Every regular file under root, relative, in os.walk's sorted order; symlinks
   and .DS_Store are left out, symlinked directories are not entered. *)
let walk_files root =
  let out = ref [] in
  let rec go dir rel =
    let names = List.sort compare (Array.to_list (Sys.readdir dir)) in
    let sub = List.filter_map (fun n ->
      let p = Filename.concat dir n and r = if rel = "" then n else rel ^ "/" ^ n in
      match (Native.stat ~follow:false p).kind with
      | `Reg -> if n <> ".DS_Store" then out := r :: !out; None
      | `Dir -> Some (p, r)
      | _ -> None
      | exception Unix.Unix_error _ -> None) names in
    List.iter (fun (p, r) -> go p r) sub in
  go root "";
  List.rev !out

let split_tab line = String.split_on_char '\t' line

(* Every file under LEFT with its counterpart under RIGHT, or None: by content
   with `phindex align` when indexes are given, else by the same relative
   path, then by a file name unique under RIGHT. *)
let whole_tree_pairs left right index tmp =
  let left = realpath left and right = realpath right in
  match index with
  | Some (li, ri) ->
      let binary = match phindex_binary () with
        | Some b -> b
        | None -> die "--index needs the phindex binary (PHINDEX, a sibling phindex checkout, or PATH)" in
      let out = join tmp "align" in
      if call [binary; "align"; "--left"; abspath li; "--left-root"; left; "--right"; abspath ri;
               "--right-root"; right; "--out"; out] <> 0 then die ~status:1 "phindex align failed";
      let lines = Phindex_metadata.splitlines (read_text (join out "files.tsv")) in
      let pairs = List.map (fun line ->
        match split_tab line with
        | [l; r; tier; _] ->
            if tier = "not-indexed" then die ~status:1 (l ^ ": not in the left phindex index; index LEFT first");
            join left l, (if r = "" then None else Some (join right r))
        | _ -> failwith ("phindex align: unexpected line in files.tsv: " ^ line))
          (match lines with _ :: rest -> rest | [] -> []) in
      let known = Hashtbl.create 1024 in
      List.iter (fun (p, _) -> Hashtbl.replace known p ()) pairs;
      let missing = List.filter (fun f -> not (Hashtbl.mem known (join left f))) (walk_files left) in
      (match missing with
       | m :: _ -> die ~status:1 (Printf.sprintf "%d files under LEFT are not in its phindex index (e.g. %s); index LEFT first"
                                    (List.length missing) m)
       | [] -> ());
      pairs
  | None ->
      Printf.printf "Walking %s and %s ...\n%!" left right;
      let lefts = walk_files left and rights = walk_files right in
      let right_set = Hashtbl.create 1024 and by_name = Hashtbl.create 1024 in
      List.iter (fun r ->
        Hashtbl.replace right_set r ();
        let n = Filename.basename r in
        Hashtbl.replace by_name n (r :: Option.value (Hashtbl.find_opt by_name n) ~default:[])) rights;
      List.map (fun l ->
        let r = if Hashtbl.mem right_set l then Some l
          else match Hashtbl.find_opt by_name (Filename.basename l) with Some [r] -> Some r | _ -> None in
        join left l, Option.map (join right) r) lefts

(* LEFT<TAB>RIGHT lines; blank and # lines skipped. An empty RIGHT is none. *)
let read_pairs path =
  List.concat (List.mapi (fun i line ->
    let s = Py.strip line in
    if s = "" || s.[0] = '#' then []
    else match split_tab line with
      | [a; b] -> [(a, if b = "" then None else Some b)]
      | _ -> failwith (Printf.sprintf "%s:%d: expected LEFT<TAB>RIGHT" path (i + 1)))
    (Phindex_metadata.splitlines (read_text path)))

(* ---- run ---- *)

type run_args = {
  left : string; right : string; archive : string; pairs : string option;
  bytes : string; metadata : string; index : (string * string) option;
  workers : int; impl : string; max_mib : float option;
}

let num_text n = string_of_int n

let cmd_run (a : run_args) =
  let arc = archive_of a.archive in
  if a.bytes = "none" && a.metadata = "none" then die "nothing to do: --bytes none and --metadata none";
  if a.metadata = "phindex" && a.index = None then die "--metadata phindex needs --index LEFT.sqlite RIGHT.sqlite";
  Option.iter (fun (l, r) -> List.iter (fun p -> if not (is_file p) then die ("phindex index not found: " ^ p)) [l; r]) a.index;
  let rate_value = match a.max_mib with Some r when r <> 0. -> r | _ -> 100000. in
  let rate = match a.max_mib with Some r when r <> 0. -> Json.py_float r | _ -> "100000" in
  let given_pairs = Option.map abspath a.pairs in
  let started = timestamp () in
  let statuses = ref [] in
  let status k n = statuses := !statuses @ [(k, n)] in
  let tmp = Filename.temp_dir "dngc-" "" in
  let left_abs = abspath a.left and right_abs = abspath a.right in
  let index_abs = Option.map (fun (l, r) -> abspath l, abspath r) a.index in
  let interrupted = ref false in
  Fun.protect ~finally:(fun () -> rm_rf tmp) (fun () ->
    let pairs, pairs_file = match a.pairs with
      | Some p -> read_pairs p, p
      | None ->
          (* An emptied source is an empty directory: covered with 0 files. *)
          if not (is_dir a.left) then
            die (a.left ^ ": no such directory; an emptied source must still exist (empty) for a whole-tree run");
          let pairs = whole_tree_pairs a.left a.right a.index tmp in
          let listed = join tmp "whole-tree-pairs.tsv" in
          write_text listed (String.concat "" (List.filter_map (fun (l, r) ->
            Option.map (fun r -> l ^ "\t" ^ r ^ "\n") r) pairs));
          Printf.printf "%s files under LEFT; %s with a counterpart\n%!" (Py.thousands (List.length pairs))
            (Py.thousands (List.length (List.filter (fun (_, r) -> r <> None) pairs)));
          pairs, listed in
    let is_dng = function Some p -> String.ends_with ~suffix:".dng" (String.lowercase_ascii p) | None -> false in
    let dng = List.filter (fun (l, r) -> is_dng (Some l) && is_dng r) pairs in
    let other = List.filter (fun (l, r) -> not (is_dng (Some l) && is_dng r)) pairs in
    (* A repeated run trusts the list it checked before and reuses everything still valid. *)
    let repeat = exists (join arc.report "pairs.jsonl") in
    let engine_metadata = a.metadata = "exiftool" in
    let index_args = match index_abs with Some (l, r) -> ["--left-index"; l; "--right-index"; r] | None -> [] in
    let workers = num_text a.workers in
    if dng <> [] then begin
      let dng_file = join tmp "dng-pairs.tsv" in
      write_text dng_file (String.concat "" (List.map (fun (l, r) -> l ^ "\t" ^ Option.get r ^ "\n") dng));
      let rc =
        if a.impl = "ocaml" || a.impl = "rust" then begin
          let binary = if a.impl = "ocaml" then Some "dng-archive" else rust_binary () in
          if binary = None then die (Printf.sprintf "no %s build; see docs/native-all.md" a.impl);
          if a.bytes = "full" then die "--bytes full needs --impl python";
          let args = [left_abs; right_abs; "--pairs"; dng_file; "--report"; arc.report]
                     @ (if a.bytes = "delta" then ["--archive"; arc.bytes] else ["--compare-only"])
                     @ ["--workers"; workers; "--exiftool-processes"; workers; "--batch-size"; "128";
                        "--progress-secs"; "60"; "--rate-mib"; rate] @ index_args
                     @ (if repeat then ["--trust-pairs"] else []) @ (if engine_metadata then [] else ["--skip-metadata"]) in
          if a.impl = "rust" then call (Option.get binary :: args)
          else
            step ("dng-archive" :: "all" :: args) (fun () ->
              All.run_options {
                All.left = a.left; right = a.right; pairs_file = dng_file; report = arc.report;
                archive = (if a.bytes = "delta" then Some arc.bytes else None);
                left_index = Option.map fst a.index; right_index = Option.map snd a.index;
                workers = a.workers; batch_size = 128; exiftool_processes = a.workers; progress_secs = 60.;
                rate_mib = rate_value; fresh = false; trust_pairs = repeat; skip_metadata = not engine_metadata })
        end else begin
          let command = [python (); join (here_or_die "--impl python") "compare_dng.py"; left_abs; right_abs;
                         "--output"; arc.report; "--workers"; workers; "--exiftool-processes"; workers;
                         "--batch-size"; "128"; "--progress-secs"; "60"; "--pairs-only"; "--pairs"; dng_file]
                        @ (if a.bytes <> "none" then ["--archive"; arc.bytes] else ["--compare-only"]) @ index_args in
          if a.bytes = "full" then die "--bytes full is not wired yet; use preserve_apo.py scan --full-copy";
          call command
        end in
      status "compare_and_preserve" rc;
      if rc = 130 then interrupted := true
    end;
    if not !interrupted && other <> [] && a.bytes <> "none" then begin
      (* A file with no counterpart is listed without a reference: kept as a full copy. *)
      let jsonl = join tmp "other-pairs.jsonl" in
      write_text jsonl (String.concat "" (List.map (fun (l, r) ->
        Json.dumps (Json.Obj [("source", Json.Str (join left_abs l));
                              ("reference", match r with Some r -> Json.Str (join right_abs r) | None -> Json.Null)])
        ^ "\n") other));
      let pworkers = min a.workers 36 in
      let source_index = match index_abs with Some (l, r) -> ["--source-index"; l; "--reference-index"; r] | None -> [] in
      let rc =
        if a.impl = "python" then
          call ([python (); join (here_or_die "--impl python") "preserve_apo.py"; "scan"; left_abs; right_abs; arc.bytes;
                 "--delta"; "--pairs"; jsonl; "--workers"; num_text pworkers; "--rate-mib"; rate] @ source_index)
        else
          step (["dng-archive"; "preserve"; left_abs; right_abs; arc.bytes; "--delta"; "--pairs"; jsonl;
                 "--workers"; num_text pworkers; "--rate-mib"; rate] @ source_index) (fun () ->
            if not (rate_value > 0.) then die "Rate must be positive";
            let real p = try Unix.realpath p with Unix.Unix_error (e, _, _) -> failwith (Py.oserror e p) in
            (* Listed paths resolve like the roots they must lie under, as preserve_apo.py resolves them. *)
            (* A listed source that is gone is reported missing by the preservation. *)
            let ppairs = List.map (fun (s, r) -> realpath s, Option.map real r) (Preserve.load_pairs jsonl) in
            let indexes = Option.map (fun (l, r) ->
              { Preserve.source_index = Phindex.open_ l; reference_index = Phindex.open_ r }) index_abs in
            if Preserve.scan ?indexes ~source:(real left_abs) ~reference:(real right_abs) ~archive:arc.bytes
                 ~rate:rate_value ~full_copy:false ~delta_mode:true ~workers:pworkers ~pairs:ppairs () then 0
            else begin
              prerr_endline "Preservation incomplete; inspect errors and rerun before deleting source"; 1
            end) in
      status "preserve_other" rc
    end;
    if not !interrupted && a.metadata = "phindex" then begin
      let li, ri = Option.get a.index in
      let all_pairs = join tmp "all-pairs.tsv" in
      write_text all_pairs (String.concat "\n" (Phindex_metadata.splitlines (read_text pairs_file))
                            |> fun s -> if s = "" then s else s ^ "\n");
      let rc =
        if a.impl = "ocaml" then
          let args = [a.left; a.right; arc.root; "--pairs"; all_pairs; "--left-index"; li; "--right-index"; ri] in
          step ("dng-archive" :: "phindex-metadata" :: args) (fun () -> Phindex_metadata.run args)
        else
          let li, ri = Option.get index_abs in
          call [python (); join (here_or_die "the Python metadata step") "phindex_metadata.py"; left_abs; right_abs;
                arc.root; "--pairs"; all_pairs; "--left-index"; li; "--right-index"; ri] in
      status "metadata" rc
    end);
  if !interrupted then 130
  else begin
    let s x = Json.Str x in
    record_run arc (Json.Obj [
      ("started", s started); ("finished", s (timestamp ())); ("left", s left_abs); ("right", s right_abs);
      ("pairs", s (Option.value given_pairs ~default:"whole tree")); ("bytes", s a.bytes); ("metadata", s a.metadata);
      ("impl", s a.impl);
      ("index", match index_abs with Some (l, r) -> Json.Arr [s l; s r] | None -> Json.Null);
      ("exit_statuses", Json.Obj (List.map (fun (k, n) -> k, Json.int n) !statuses))]);
    List.fold_left (fun m (_, n) -> max m n) 0 !statuses
  end

(* ---- restore, index, query ---- *)

let cmd_restore ?version archive rel output =
  let arc = archive_of archive in
  step (["dng-archive"; "restore"; arc.bytes; rel; output] @ (match version with Some v -> ["--version"; v] | None -> []))
    (fun () -> Preserve.restore ?version arc.bytes rel output; 0)

let cmd_index archive =
  let arc = archive_of archive in
  step ["dng-archive"; "index"; arc.root] (fun () -> Index.index_archive arc.root; 0)

let presets = [
  "changes", "SELECT tag, category, kind, affected_files, percent_compared FROM field_changes ORDER BY affected_files DESC LIMIT 50";
  "files", "SELECT f.path, d.kind FROM differences d JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id \
            WHERE t.tag=? ORDER BY f.path";
  "values", "SELECT f.path, d.kind, a.json_zlib, b.json_zlib FROM differences d JOIN files f ON f.id=d.file_id \
             JOIN fields t ON t.id=d.field_id LEFT JOIN vals a ON a.id=d.apo_value LEFT JOIN vals b ON b.id=d.bumer_value \
             WHERE t.tag=? ORDER BY f.path";
]

(* sqlite3.connect(f'file:{path}?mode=ro', uri=True). The system SQLite
   (3.43 on macOS) cannot open a WAL database read-only while its -shm file is
   missing, which the SQLite bundled with Python (3.53) can. Then, with no WAL
   content to read, the file is opened immutable; with WAL content, a private
   copy of the database and its WAL is read. Neither writes the archive: a
   read-write connection would checkpoint the WAL and delete it on close. *)
let copies = ref []
let () = at_exit (fun () -> List.iter rm_rf !copies)

let open_ro path =
  let db = Sqlite3.db_open ~uri:true ("file:" ^ path ^ "?mode=ro") in
  match Sqlite3.exec db "PRAGMA schema_version" with
  | Sqlite3.Rc.OK -> db
  | _ ->
      ignore (Sqlite3.db_close db);
      let wal = path ^ "-wal" in
      if not (exists wal) || (Native.stat wal).size = 0L then
        Sqlite3.db_open ~mode:`READONLY ~uri:true ("file:" ^ path ^ "?immutable=1")
      else begin
        let dir = Filename.temp_dir "dngc-db-" "" in
        copies := dir :: !copies;
        let copy src dst = Out_channel.with_open_bin dst (fun oc -> output_string oc (read_text src)) in
        let target = Filename.concat dir (Filename.basename path) in
        copy path target; copy wal (target ^ "-wal");
        Sqlite3.db_open target
      end

let close_ro db = ignore (Sqlite3.db_close db)

let check rc db = match rc with
  | Sqlite3.Rc.OK | Sqlite3.Rc.DONE | Sqlite3.Rc.ROW -> ()
  | rc -> failwith (Printf.sprintf "%s (%s)" (Sqlite3.errmsg db) (Sqlite3.Rc.to_string rc))

(* Rows of a statement with its parameters; Python's sqlite3 refuses a
   statement whose parameter count differs from those supplied. *)
let statement db sql params =
  let st = try Sqlite3.prepare db sql with Sqlite3.Error m -> failwith m in
  (match Sqlite3.prepare_tail st with
   | None -> ()
   | Some t -> ignore (Sqlite3.finalize t); ignore (Sqlite3.finalize st);
       failwith "You can only execute one statement at a time."
   (* Only whitespace or comments after the statement: nothing more to run. *)
   | exception Sqlite3.Error m when String.starts_with ~prefix:"No code compiled" m -> ()
   | exception Sqlite3.Error m -> ignore (Sqlite3.finalize st); failwith m);
  let wanted = Sqlite3.bind_parameter_count st in
  if wanted <> List.length params then
    failwith (Printf.sprintf "Incorrect number of bindings supplied. The current statement uses %d, and there are %d supplied."
                wanted (List.length params));
  List.iteri (fun i p -> check (Sqlite3.bind st (i + 1) p) db) params;
  st

let fold_rows db st f init =
  let rec go acc = match Sqlite3.step st with
    | Sqlite3.Rc.ROW -> go (f acc (Sqlite3.row_data st))
    | Sqlite3.Rc.DONE -> acc
    | rc -> check rc db; acc in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () -> go init)

let rows db sql params = List.rev (fold_rows db (statement db sql params) (fun acc r -> r :: acc) [])

let one_int db sql = match rows db sql [] with
  | [ [| Sqlite3.Data.INT n |] ] -> Json.int64 n
  | _ -> failwith ("no count from " ^ sql)

(* str(v) for what Python's sqlite3 returns; blobs are zlib JSON values. *)
let cell = function
  | Sqlite3.Data.NONE | Sqlite3.Data.NULL -> "None"
  | Sqlite3.Data.INT n -> Int64.to_string n
  | Sqlite3.Data.FLOAT x -> Json.py_float x
  | Sqlite3.Data.TEXT s -> s
  | Sqlite3.Data.BLOB b -> Json.dumps ~ensure_ascii:false (Py.dedup (Json.parse (Native.zlib_decompress b)))

let cmd_query archive words =
  let db_path = join (archive_of archive).metadata "differences.sqlite3" in
  if not (exists db_path) then
    die (Printf.sprintf "no metadata index in %s; run `dngc run ... --metadata` or `dngc index`" archive);
  let db = open_ro db_path in
  Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
    let words = if words = [] then ["changes"] else words in
    let sql, params = match List.assoc_opt (List.hd words) presets with
      | Some sql when List.hd words = "changes" -> sql, []
      | Some sql -> sql, (match words with _ :: tag :: _ -> [Sqlite3.Data.TEXT tag] | _ -> [])
      | None -> String.concat " " words, [] in
    let st = statement db sql params in
    let n = Sqlite3.column_count st in
    if n = 0 then failwith "the statement returns no columns";
    print_endline (String.concat "\t" (List.init n (Sqlite3.column_name st)));
    fold_rows db st (fun () r -> print_endline (String.concat "\t" (Array.to_list (Array.map cell r)))) ();
    0)

(* ---- status, verify ---- *)

let read_json p = Json.parse (read_text p)
let get k v = match Json.member k v with Some x -> x | None -> Json.Null

let table_exists db name =
  rows db "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?" [Sqlite3.Data.TEXT name] <> []

let sections arc =
  let out = ref [] in
  let add k v = out := !out @ [(k, v)] in
  let summary = join arc.report "summary.json" in
  if exists summary then begin
    let s = read_json summary in
    let counts = match Json.member "counts" s with Some c -> c | None -> Json.Obj [] in
    add "report" (Json.Obj [("complete", get "complete" s); ("pairs", get "pairs_reported" counts);
                            ("raw_equal", get "raw_equal" counts); ("selected", get "selected_pairs" s)])
  end;
  let recovery = join arc.bytes "recovery.sqlite3" in
  if exists recovery then begin
    let bs = join arc.bytes "summary.json" in
    let s = if exists bs then read_json bs else Json.Obj [] in
    let db = open_ro recovery in
    Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
      add "bytes" (Json.Obj [
        ("files", one_int db "SELECT count(*) FROM files"); ("errors", one_int db "SELECT count(*) FROM errors");
        ("earlier_versions", if table_exists db "versions" then one_int db "SELECT count(*) FROM versions" else Json.int 0);
        ("last_run_total", get "total_files" s); ("last_run_complete", get "byte_preservation_complete" s);
        ("layout", Json.Str (if arc.legacy then "legacy (top level)" else "bytes/"))]))
  end;
  let index = join arc.metadata "differences.sqlite3" in
  if exists index then begin
    let db = open_ro index in
    let prov = join arc.metadata "provenance.json" and top = join arc.root "provenance.json" in
    let engine = if exists prov then get "engine" (read_json prov)
      else if exists top then get "engine" (read_json top) else Json.Null in
    Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
      add "metadata" (Json.Obj [
        ("files", one_int db "SELECT count(*) FROM files"); ("differences", one_int db "SELECT count(*) FROM differences");
        ("engine", if Json.truthy engine then engine else Json.Str "Perl ExifTool (no engine stamp)")]))
  end;
  !out

(* Every preserved content of each relative path: the current record and the
   earlier ones a refilled source left (restore --version LABEL). *)
let cmd_versions archive rel =
  let arc = archive_of archive in
  let path = join arc.bytes "recovery.sqlite3" in
  if not (exists path) then die ("no byte section in " ^ archive);
  let db = open_ro path in
  Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
    let where, params = match rel with
      | Some r -> " WHERE path=?", [Sqlite3.Data.TEXT r] | None -> "", [] in
    let text = function Sqlite3.Data.TEXT t -> t | _ -> "" in
    let current = List.map (fun r -> text r.(0), Preserve.label_of (Json.parse (text r.(1))), "current")
        (rows db ("SELECT path, record FROM files" ^ where) params) in
    let earlier = if table_exists db "versions" then
        List.map (fun r -> text r.(0), text r.(1), "earlier") (rows db ("SELECT path, version FROM versions" ^ where) params)
      else [] in
    print_endline "path\tversion\tstate";
    List.iter (fun (p, v, st) -> Printf.printf "%s\t%s\t%s\n" p v st) (List.sort compare (current @ earlier));
    0)

let cmd_status archive =
  let arc = archive_of archive in
  if not (is_dir arc.root) then die (arc.root ^ ": no such archive");
  print_endline (Json.dumps_indent (Json.Obj [
    ("archive", Json.Str arc.root); ("sections", Json.Obj (sections arc));
    ("runs", Json.int (List.length (runs (read_manifest arc))))]));
  0

let cmd_verify archive restore_sample =
  let arc = archive_of archive in
  let problems = ref [] in
  let problem p = problems := !problems @ [p] in
  let s = sections arc in
  (match List.assoc_opt "bytes" s with
   | Some b ->
       (match get "errors" b with
        | Json.Num n when Json.truthy (Json.Num n) -> problem (Printf.sprintf "bytes: %s files with preservation errors" n)
        | _ -> ());
       let db = open_ro (join arc.bytes "recovery.sqlite3") in
       (* Payloads of the current records and of the earlier versions; the source is never read. *)
       let missing = Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
         let tables = "files" :: (if table_exists db "versions" then ["versions"] else []) in
         List.fold_left (fun n table ->
         fold_rows db (statement db ("SELECT path, record FROM " ^ table) []) (fun n row ->
           let r = match row with [| _; Sqlite3.Data.TEXT t |] -> Json.parse t | _ -> Json.Obj [] in
           List.fold_left (fun n key ->
             match Json.member key r with
             | Some (Json.Str p) when p <> "" && not (exists (join arc.bytes p)) -> n + 1
             | _ -> n) n ["delta"; "original"]) n) 0 tables) in
       if missing > 0 then problem (Printf.sprintf "bytes: %d deltas or originals named in records are missing" missing)
   | None -> ());
  if List.mem_assoc "metadata" s then begin
    let db = open_ro (join arc.metadata "differences.sqlite3") in
    Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
      match rows db "PRAGMA quick_check" [] with
      | [| Sqlite3.Data.TEXT "ok" |] :: _ -> ()
      | row :: _ -> problem ("metadata index: " ^ cell row.(0))
      | [] -> problem "metadata index: None")
  end;
  (match List.assoc_opt "report" s with
   | Some r when not (Json.truthy (get "complete" r)) -> problem "report: the comparison has not finished"
   | _ -> ());
  print_endline (Json.dumps_indent (Json.Obj [
    ("archive", Json.Str arc.root); ("sections", Json.Arr (List.map (fun (k, _) -> Json.Str k)
                                                             (List.sort compare s)));
    ("problems", Json.Arr (List.map (fun p -> Json.Str p) !problems))]));
  if restore_sample <> 0 && List.mem_assoc "bytes" s then begin
    let db = open_ro (join arc.bytes "recovery.sqlite3") in
    let paths = Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
      List.filter_map (function [| Sqlite3.Data.TEXT p |] -> Some p | _ -> None)
        (rows db "SELECT path FROM files ORDER BY random() LIMIT ?" [Sqlite3.Data.INT (Int64.of_int restore_sample)])) in
    let td = Filename.temp_dir "dngc-verify-" "" in
    Fun.protect ~finally:(fun () -> rm_rf td) (fun () ->
      List.iteri (fun i p ->
        let out = join td (string_of_int i) in
        if step ["dng-archive"; "restore"; arc.bytes; p; out] (fun () -> Preserve.restore arc.bytes p out; 0) <> 0 then
          problem ("restore failed: " ^ p)) paths)
  end;
  if !problems <> [] then 1 else 0

(* ---- migrate ---- *)

(* What the byte tools (preserve_apo.py, dng-archive preserve/all, compare_dng.py's
   archive step, preserve_years.py, finalize_year.py, database_backend.py) write at
   the top of a legacy archive: the byte section, moved into bytes/. *)
let byte_entries = [
  "recovery.sqlite3"; "recovery.sqlite3-wal"; "recovery.sqlite3-shm"; "recovery.sqlite3-journal";
  "recovery.sqlite3.engine.json"; "recovery.sqlite3.engine.tmp"; "recovery.sqlite3.before-turso";
  "deltas"; "originals"; "manifest.jsonl"; "inventory.json"; "directories.json"; "summary.json";
  "inventory.json.tmp"; "directories.json.tmp"; "summary.json.tmp"; "matching.json";
  "xmp-original"; "image-comparison"; "finalization-backend.json"; ".byte-preservation.lock"]

(* The flocks the tools hold while they write. *)
let locks = [".byte-preservation.lock"; ".metadata-preservation.lock"; "bytes/.byte-preservation.lock"; "report/.lock"]
let migration_log = "MIGRATION.jsonl"

let lexists p = match Native.stat ~follow:false p with _ -> true | exception Unix.Unix_error _ -> false

let write_manifest a m =
  Native.mkdir_p a.root;
  let tmp = join a.root "ARCHIVE.tmp" in
  write_text tmp (Json.dumps_indent m ^ "\n");
  Sys.rename tmp a.manifest

(* m.setdefault(key, []).append(v) *)
let append_to key v = function
  | Json.Obj l when List.mem_assoc key l ->
      Json.Obj (List.map (fun (k, x) -> k, if k = key then (match x with Json.Arr l -> Json.Arr (l @ [v]) | _ -> x) else x) l)
  | Json.Obj l -> Json.Obj (l @ [(key, Json.Arr [v])])
  | _ -> failwith "ARCHIVE.json is not an object"

let op e = match Json.member "op" e with Some (Json.Str s) -> s | _ -> ""
let field_str k e = match Json.member k e with Some (Json.Str s) -> s | _ -> ""

(* The log's entries since its last begin, and the state they leave. *)
let migration_state a =
  let log = join a.root migration_log in
  let entries = if exists log then
      List.filter_map (fun l -> if Py.strip l = "" then None else Some (Json.parse l))
        (Phindex_metadata.splitlines (read_text log)) else [] in
  let rec from_last_begin acc = function
    | [] -> acc
    | e :: rest -> from_last_begin (if op e = "begin" then Some [e] else Option.map (fun l -> l @ [e]) acc) rest in
  match from_last_begin None entries with
  | None -> None, []
  | Some current ->
      let has o = List.exists (fun e -> op e = o) current in
      Some (if has "undo-done" then "undone" else if has "undo-begin" then "undoing"
            else if has "done" then "migrated" else "migrating"), current

(* One line per step, on disk before the step happens. *)
let log_step a entry =
  let fd = Unix.openfile (join a.root migration_log) [Unix.O_WRONLY; Unix.O_APPEND; Unix.O_CREAT; Unix.O_CLOEXEC] 0o644 in
  Fun.protect ~finally:(fun () -> Unix.close fd) (fun () ->
    let line = Json.dumps (Json.Obj entry) ^ "\n" in
    ignore (Unix.write_substring fd line 0 (String.length line));
    Unix.fsync fd)

let now () = Json.Str (timestamp ())

(* Every tool lock in the archive, taken without waiting; refuses while one is held. *)
let hold_locks a =
  let held = ref [] in
  let release () = List.iter Unix.close !held; held := [] in
  List.iter (fun name ->
    let p = join a.root name in
    if is_file p then begin
      let fd = Unix.openfile p [Unix.O_RDONLY; Unix.O_CLOEXEC] 0 in
      if Native.flock_exclusive fd then held := fd :: !held
      else (Unix.close fd; release (); die ~status:1 (name ^ " is held: another process is writing this archive"))
    end) locks;
  if exists (join a.root ".running") then (release (); die ~status:1 ".running exists: preserve_years.py is writing this archive");
  List.iter (fun d ->
    let marker = join a.root (d ^ "recovery.sqlite3.engine.json") in
    let pid = if exists marker then (match Json.member "active_pid" (read_json marker) with
        | Some (Json.Num n) -> int_of_string_opt n | _ -> None) else None in
    match pid with
    | Some pid when pid <> 0 && pid <> Unix.getpid () ->
        (match Unix.kill pid 0 with
         | () -> release (); die ~status:1 (Printf.sprintf "%srecovery.sqlite3 is open in Turso MVCC by process %d" d pid)
         | exception Unix.Unix_error (Unix.ESRCH, _, _) -> ()
         | exception Unix.Unix_error _ -> release (); die ~status:1 (Printf.sprintf "%srecovery.sqlite3 is open in Turso MVCC by process %d" d pid))
    | _ -> ()) [""; "bytes/"];
  List.iter (fun d ->
    let wal = join a.root (d ^ "recovery.sqlite3-wal") in
    if exists wal && (Native.stat wal).size > 0L && not (exists (join a.root (d ^ ".byte-preservation.lock"))) then begin
      release ();
      die ~status:1 (Printf.sprintf "%srecovery.sqlite3-wal is not empty and there is no %s.byte-preservation.lock \
                                     to show that no writer is active" d d)
    end) [""; "bytes/"];
  !held

(* Sources, engine and counts from the archive's own records. *)
let migration_summary a =
  let read p = match if exists p then Json.parse (read_text p) else Json.Obj [] with
    | Json.Obj _ as v -> Py.dedup v | _ -> Json.Obj [] | exception Json.Bad _ -> Json.Obj [] in
  let prov = read (join a.root "provenance.json") and mprov = read (join a.root "metadata/provenance.json") in
  let s = read (join a.root "bytes/summary.json") in
  let either k x y = let v = get k x in if Json.truthy v then v else get k y in
  let runs = join a.root "runs.jsonl" in
  Json.Obj [
    ("source", either "source" s prov); ("reference", either "reference" s prov);
    ("engine", either "engine" mprov prov); ("total_files", get "total_files" s);
    ("verified_files", get "verified_files" s); ("errors", get "errors" s);
    ("byte_preservation_complete", get "byte_preservation_complete" s);
    ("runs_logged", if exists runs then
        Json.int (List.length (List.filter (fun l -> Py.strip l <> "") (Phindex_metadata.splitlines (read_text runs))))
      else Json.Null)]

(* shutil.copytree(src, dst, symlinks=True): contents, modes and times. *)
let rec copytree src dst =
  Unix.mkdir dst 0o777;
  Array.iter (fun n ->
    let s = Filename.concat src n and d = Filename.concat dst n in
    let st = Native.stat ~follow:false s in
    match st.kind with
    | `Link -> Unix.symlink (Unix.readlink s) d
    | `Dir -> copytree s d
    | _ ->
        Out_channel.with_open_bin d (fun oc ->
          In_channel.with_open_bin s (fun ic -> ignore (Native.sha_channel ic ~each:(fun b k -> Out_channel.output oc b 0 k))));
        Unix.chmod d (st.mode land 0o7777);
        Native.utimens d st.atime_ns st.mtime_ns)
    (let names = Sys.readdir src in Array.sort compare names; names);
  let st = Native.stat src in
  Unix.chmod dst (st.mode land 0o7777);
  Native.utimens dst st.atime_ns st.mtime_ns

let migrate_undo a dry_run =
  let root = a.root in
  let state, entries = migration_state a in
  if not (List.mem state [Some "migrating"; Some "migrated"; Some "undoing"]) then
    die ~status:1 (Printf.sprintf "%s: %s records no migration to undo" root migration_log);
  let held = hold_locks a in
  let moves = List.rev (List.filter (fun e -> op e = "move") entries) in
  let plan = List.filter_map (fun e ->
    let from_ = field_str "from" e and to_ = field_str "to" e in
    let src = join root to_ and dst = join root from_ in
    if lexists src && not (lexists dst) then Some (`Move (to_, from_))
    else if lexists dst && not (lexists src) then None
    else die ~status:1 (Printf.sprintf "cannot undo the move of %s: %s and %s %s" from_ to_ from_
                          (if lexists src then "both exist" else "are both missing"))) moves in
  let plan = plan
    @ (if List.exists (fun e -> op e = "copy") entries then
         List.filter_map (fun n -> if lexists (join root n) then Some (`Remove n) else None) ["report.migrating"; "report"]
       else [])
    @ (if List.exists (fun e -> op e = "mkdir") entries && is_dir (join root "bytes") then [`Rmdir] else []) in
  Printf.printf "dngc migrate --undo %s%s\n%!" root (if dry_run then " (dry run: nothing is changed)" else "");
  if not dry_run && state <> Some "undoing" then log_step a [("op", Json.Str "undo-begin"); ("time", now ())];
  List.iter (fun step ->
    (match step with
     | `Move (f, t) -> Printf.printf "move %s -> %s\n%!" f t
     | `Remove n -> Printf.printf "remove %s\n%!" n
     | `Rmdir -> print_endline "rmdir bytes");
    if not dry_run then
      match step with
      | `Move (f, t) ->
          log_step a [("op", Json.Str "unmove"); ("from", Json.Str f); ("to", Json.Str t)];
          Unix.rename (join root f) (join root t)
      | `Remove n -> log_step a [("op", Json.Str "remove"); ("path", Json.Str n)]; rm_rf (join root n)
      | `Rmdir ->
          if Sys.readdir (join root "bytes") <> [||] then print_endline "bytes/ is not empty; left in place"
          else (log_step a [("op", Json.Str "rmdir"); ("path", Json.Str "bytes")]; Unix.rmdir (join root "bytes"))) plan;
  if dry_run then 0
  else begin
    let restored = List.filter_map (function `Move (_, t) -> Some (Json.Str t) | _ -> None) plan in
    write_manifest a (append_to "migrations" (Json.Obj [
        ("undone", now ()); ("restored", Json.Arr restored);
        ("report_removed", Json.Bool (List.mem (`Remove "report") plan))]) (read_manifest a));
    log_step a [("op", Json.Str "undo-done"); ("time", now ())];
    List.iter Unix.close held;
    Printf.printf "Undone: %d paths moved back\n%!" (List.length restored);
    0
  end

let cmd_migrate archive ~dry_run ~report ~undo ~no_verify =
  let a = archive_of archive in
  let root = a.root in
  if not (is_dir root) then die (root ^ ": no such archive");
  if undo then begin
    if report <> None then die "--report does not go with --undo";
    migrate_undo a dry_run
  end else begin
    let state, entries = migration_state a in
    if state = Some "undoing" then die ~status:1 ("an undo was interrupted; finish it with dngc migrate --undo " ^ root);
    let resuming = state = Some "migrating" in
    let report = Option.map abspath report in
    let report =
      if resuming then begin
        let logged = match entries with e :: _ -> (match Json.member "report" e with Some (Json.Str r) -> Some r | _ -> None) | [] -> None in
        (match report with
         | Some r when Some r <> logged ->
             die ~status:1 (Printf.sprintf "the interrupted migration was started with --report %s; rerun it with that"
                              (match logged with Some l -> l | None -> "None"))
         | _ -> ());
        logged
      end else begin
        if not (lexists (join root "recovery.sqlite3")) then begin
          let msg = root ^ " is not a legacy archive (no recovery.sqlite3 at the top); nothing to migrate" in
          if report <> None then die ~status:1 msg;
          print_endline msg;
          raise (Die 0)
        end;
        if lexists (join root "bytes") then
          die ~status:1 (root ^ " has recovery.sqlite3 at the top and a bytes/ directory; resolve by hand");
        report
      end in
    Option.iter (fun r ->
      if not (is_dir r) then die (Printf.sprintf "--report: %s is not a directory" r);
      if lexists (join root "report") && not resuming then
        die ~status:1 (Printf.sprintf "%s exists; not copying %s" (join root "report") r)) report;
    let held = hold_locks a in
    Option.iter (fun r ->
      let l = join r ".lock" in
      if is_file l then begin
        let fd = Unix.openfile l [Unix.O_RDONLY; Unix.O_CLOEXEC] 0 in
        let ok = Native.flock_exclusive fd in
        Unix.close fd;
        if not ok then die ~status:1 (r ^ "/.lock is held: a comparison is writing that report")
      end) report;
    let names = List.filter (fun n -> lexists (join root n)) byte_entries in
    let device = (Native.stat root).dev in
    List.iter (fun n ->
      if (Native.stat ~follow:false (join root n)).dev <> device then
        die ~status:1 (Printf.sprintf "%s is on another device than %s; a rename cannot move it and migrate does not copy" n root))
      (names @ (if lexists (join root "bytes") then ["bytes"] else []));
    let plan = (if lexists (join root "bytes") then [] else [`Mkdir])
               @ List.map (fun n -> `Move n) names
               @ (match report with Some r when not (lexists (join root "report")) -> [`Copy r] | _ -> []) in
    let keep = List.sort compare (List.filter (fun n -> not (List.mem n names) && n <> "bytes")
                                    (Array.to_list (Sys.readdir root))) in
    Printf.printf "dngc migrate %s%s\n%!" root (if dry_run then " (dry run: nothing is changed)" else "");
    if resuming then Printf.printf "resuming the migration that %s records\n%!" migration_log;
    if not dry_run && not resuming then
      log_step a [("op", Json.Str "begin"); ("time", now ()); ("archive", Json.Str root);
                  ("report", match report with Some r -> Json.Str r | None -> Json.Null)];
    List.iter (fun step ->
      (match step with
       | `Mkdir -> print_endline "mkdir bytes"
       | `Move n -> Printf.printf "move %s -> bytes/%s\n" n n
       | `Copy r -> Printf.printf "copy %s -> report\n" r);
      flush stdout;
      if not dry_run then
        match step with
        | `Mkdir -> log_step a [("op", Json.Str "mkdir"); ("path", Json.Str "bytes")]; Unix.mkdir (join root "bytes") 0o777
        | `Move n ->
            log_step a [("op", Json.Str "move"); ("from", Json.Str n); ("to", Json.Str ("bytes/" ^ n))];
            Unix.rename (join root n) (join root ("bytes/" ^ n))
        | `Copy r ->
            log_step a [("op", Json.Str "copy"); ("from", Json.Str r); ("to", Json.Str "report")];
            let partial = join root "report.migrating" in
            if lexists partial then rm_rf partial;
            copytree r partial;
            Unix.rename partial (join root "report")) plan;
    List.iter (fun k -> Printf.printf "keep %s\n" k) keep;
    Printf.printf "write ARCHIVE.json (%s)\n%!" (if exists a.manifest then "append" else "new");
    if dry_run then 0
    else begin
      let _, entries = migration_state a in
      let seen = Hashtbl.create 16 in
      let moved = List.filter_map (fun e ->
        if op e = "move" && not (Hashtbl.mem seen (field_str "to" e)) then begin
          Hashtbl.replace seen (field_str "to" e) ();
          Some (Json.Obj [("path", Json.Str (field_str "to" e)); ("from", Json.Str (field_str "from" e))])
        end else None) entries in
      write_manifest a (append_to "migrations" (Json.Obj [
          ("migrated", now ()); ("moved", Json.Arr moved);
          ("report", match report with
            | Some r -> Json.Obj [("path", Json.Str "report"); ("from", Json.Str r)] | None -> Json.Null);
          ("summary", migration_summary a)]) (read_manifest a));
      log_step a [("op", Json.Str "done"); ("time", now ())];
      List.iter Unix.close held;
      Printf.printf "Migrated %d paths into %s\n%!" (List.length moved) (join root "bytes");
      if no_verify then 0
      else if cmd_verify root 3 <> 0 then begin
        Printf.eprintf "dngc: error: verify found problems after the migration; it is kept \
                        (dngc migrate --undo %s reverses it)\n%!" root;
        1
      end else 0
    end
  end

(* ---- The command line, as dngc.py's argparse parser reads it ---- *)

let top_usage = "usage: dngc [-h] {run,restore,versions,index,query,verify,status,migrate} ..."

let top_help = top_usage ^ {|

dngc: one command for dng-compare's comparison, preservation and metadata
tools.

positional arguments:
  {run,restore,versions,index,query,verify,status,migrate}
    run                 compare, preserve and extract metadata in one pass
    restore             rebuild a preserved file byte for byte
    versions            the preserved contents of each path, current and
                        earlier
    index               (re)build the searchable metadata index
    query               query the metadata index: changes | files TAG | values
                        TAG | SQL
    verify              check the archive: records, files, index; optionally
                        restore a sample
    status              what the archive holds
    migrate             move a legacy archive's byte section into bytes/

options:
  -h, --help            show this help message and exit|}

type option_spec = { flag : string; nargs : int; kind : [ `Str | `Int | `Float ]; choices : string list }

type verb = {
  name : string; usage : string; help : string;
  positionals : string list; rest : bool;  (* a final nargs='*' positional *)
  options : option_spec list;
}

let opt ?(nargs = 1) ?(kind = `Str) ?(choices = []) flag = { flag; nargs; kind; choices }

let run_usage = {|usage: dngc run [-h] [--pairs PAIRS] [--bytes {delta,full,none}]
                [--metadata {exiftool,phindex,none}]
                [--index LEFT.sqlite RIGHT.sqlite] [--workers WORKERS]
                [--impl {ocaml,rust,python}] [--max-mib MAX_MIB]
                left right archive|}

let simple_help usage positionals width =
  usage ^ "\n\npositional arguments:\n" ^ String.concat "" (List.map (fun p -> "  " ^ p ^ "\n") positionals)
  ^ "\noptions:\n  -h, --help" ^ String.make width ' ' ^ "show this help message and exit"

let migrate_usage = {|usage: dngc migrate [-h] [--dry-run] [--report DIR] [--undo] [--no-verify]
                    archive|}

let verbs = [
  { name = "run"; usage = run_usage; positionals = ["left"; "right"; "archive"]; rest = false;
    help = run_usage ^ {|

positional arguments:
  left
  right
  archive

options:
  -h, --help            show this help message and exit
  --pairs PAIRS         LEFT<TAB>RIGHT lines (e.g. phindex align output);
                        default: the whole tree
  --bytes {delta,full,none}
  --metadata {exiftool,phindex,none}
  --index LEFT.sqlite RIGHT.sqlite
                        phindex indexes of the two trees
  --workers WORKERS
  --impl {ocaml,rust,python}
  --max-mib MAX_MIB     limit reads to this many MiB/s (default: no limit)|};
    options = [opt "--pairs"; opt "--bytes" ~choices:["delta"; "full"; "none"];
               opt "--metadata" ~choices:["exiftool"; "phindex"; "none"]; opt "--index" ~nargs:2;
               opt "--workers" ~kind:`Int; opt "--impl" ~choices:["ocaml"; "rust"; "python"];
               opt "--max-mib" ~kind:`Float] };
  { name = "restore"; usage = "usage: dngc restore [-h] [--version LABEL] archive relative_path output";
    help = {|usage: dngc restore [-h] [--version LABEL] archive relative_path output

positional arguments:
  archive
  relative_path
  output

options:
  -h, --help       show this help message and exit
  --version LABEL  an earlier content of the path (see dngc versions)|};
    positionals = ["archive"; "relative_path"; "output"]; rest = false; options = [opt "--version"] };
  { name = "versions"; usage = "usage: dngc versions [-h] archive [relative_path]";
    help = simple_help "usage: dngc versions [-h] archive [relative_path]" ["archive"; "relative_path"] 5;
    positionals = ["archive"]; rest = true; options = [] };
  { name = "index"; usage = "usage: dngc index [-h] archive";
    help = simple_help "usage: dngc index [-h] archive" ["archive"] 2;
    positionals = ["archive"]; rest = false; options = [] };
  { name = "query"; usage = "usage: dngc query [-h] archive [query ...]";
    help = simple_help "usage: dngc query [-h] archive [query ...]" ["archive"; "query"] 2;
    positionals = ["archive"]; rest = true; options = [] };
  { name = "verify"; usage = "usage: dngc verify [-h] [--restore-sample N] archive";
    help = "usage: dngc verify [-h] [--restore-sample N] archive\n\npositional arguments:\n  archive\n\n\
            options:\n  -h, --help          show this help message and exit\n  --restore-sample N";
    positionals = ["archive"]; rest = false; options = [opt "--restore-sample" ~kind:`Int] };
  { name = "status"; usage = "usage: dngc status [-h] archive";
    help = simple_help "usage: dngc status [-h] archive" ["archive"] 2;
    positionals = ["archive"]; rest = false; options = [] };
  { name = "migrate"; usage = migrate_usage;
    help = migrate_usage ^ {|

positional arguments:
  archive

options:
  -h, --help    show this help message and exit
  --dry-run     print the plan only
  --report DIR  also copy this comparison report into report/
  --undo        move back what MIGRATION.jsonl records
  --no-verify   skip verify --restore-sample 3 afterwards|};
    positionals = ["archive"]; rest = false;
    options = [opt "--dry-run" ~nargs:0; opt "--report"; opt "--undo" ~nargs:0; opt "--no-verify" ~nargs:0] };
]

let parser_error usage prog msg =
  prerr_endline usage;
  Printf.eprintf "%s: error: %s\n%!" prog msg;
  raise (Die 2)

let quote s = Py.repr_string ~bytes:false s

(* argparse treats '-x...' as an option, except a negative number. *)
let option_like s =
  String.length s > 1 && s.[0] = '-' && not (match float_of_string_opt s with Some _ -> true | None -> false)

(* Positional values and option values of one verb's arguments. *)
let parse_verb v args =
  let err msg = parser_error v.usage ("dngc " ^ v.name) msg in
  let lookup name =
    if name = "-h" || (String.length name > 2 && String.starts_with ~prefix:name "--help") then `Help
    else match List.find_opt (fun o -> o.flag = name) v.options with
      | Some o -> `Opt o
      | None ->
          let cands = List.filter (fun o -> String.length name > 2 && String.starts_with ~prefix:name o.flag) v.options in
          (match cands with
           | [o] -> `Opt o
           | [] -> `Unknown
           | l -> err (Printf.sprintf "ambiguous option: %s could match %s" name
                         (String.concat ", " (List.map (fun o -> o.flag) l)))) in
  let pos = ref [] and opts = Hashtbl.create 8 and extras = ref [] in
  let convert o raw =
    let value = match o.kind with
      | `Str -> raw
      | `Int -> (match int_of_string_opt (String.trim raw) with
          | Some n -> string_of_int n | None -> err (Printf.sprintf "argument %s: invalid int value: %s" o.flag (quote raw)))
      | `Float -> (match float_of_string_opt (String.trim raw) with
          | Some _ -> String.trim raw | None -> err (Printf.sprintf "argument %s: invalid float value: %s" o.flag (quote raw))) in
    if o.choices <> [] && not (List.mem value o.choices) then
      err (Printf.sprintf "argument %s: invalid choice: %s (choose from %s)" o.flag (quote value)
             (String.concat ", " (List.map quote o.choices)));
    value in
  let rec go = function
    | [] -> ()
    | "--" :: rest -> pos := List.rev_append rest !pos
    | a :: rest when option_like a ->
        let name, inline = match String.index_opt a '=' with
          | Some i when String.starts_with ~prefix:"--" a -> String.sub a 0 i, Some (String.sub a (i + 1) (String.length a - i - 1))
          | _ -> a, None in
        (match lookup name with
         | `Help -> print_endline v.help; raise (Die 0)
         | `Unknown -> extras := a :: !extras; go rest
         | `Opt o ->
             let expected = if o.nargs = 1 then "expected one argument" else Printf.sprintf "expected %d arguments" o.nargs in
             let values, rest = match inline with
               | Some x when o.nargs = 0 -> err (Printf.sprintf "argument %s: ignored explicit argument %s" o.flag (quote x))
               | Some x -> if o.nargs = 1 then [x], rest else err (Printf.sprintf "argument %s: %s" o.flag expected)
               | None ->
                   let rec take n l acc = if n = 0 then List.rev acc, l else match l with
                     | x :: l when not (option_like x) && x <> "--" -> take (n - 1) l (x :: acc)
                     | _ -> err (Printf.sprintf "argument %s: %s" o.flag expected) in
                   take o.nargs rest [] in
             Hashtbl.replace opts o.flag (List.map (convert o) values);
             go rest)
    | a :: rest -> pos := a :: !pos; go rest in
  go args;
  let pos = List.rev !pos in
  let n = List.length v.positionals in
  if List.length pos < n then
    err ("the following arguments are required: " ^ String.concat ", " (List.filteri (fun i _ -> i >= List.length pos) v.positionals));
  let extra = (if v.rest then [] else List.filteri (fun i _ -> i >= n) pos) @ List.rev !extras in
  if extra <> [] then parser_error top_usage "dngc" ("unrecognized arguments: " ^ String.concat " " extra);
  pos, (fun flag -> Hashtbl.find_opt opts flag)

(* os.cpu_count() // 4 or 2 *)
let default_workers () = match Domain.recommended_domain_count () / 4 with 0 -> 2 | n -> n

let dispatch argv =
  match argv with
  | [] -> parser_error top_usage "dngc" "the following arguments are required: verb"
  | a :: _ when a = "-h" || (String.length a > 2 && String.starts_with ~prefix:a "--help") -> print_endline top_help; 0
  | name :: args ->
      match List.find_opt (fun v -> v.name = name) verbs with
      | None when option_like name -> parser_error top_usage "dngc" ("unrecognized arguments: " ^ name)
      | None ->
          parser_error top_usage "dngc"
            (Printf.sprintf "argument verb: invalid choice: %s (choose from %s)" (quote name)
               (String.concat ", " (List.map (fun v -> quote v.name) verbs)))
      | Some v ->
          let pos, get = parse_verb v args in
          let one flag = Option.map List.hd (get flag) in
          match v.name, pos with
          | "run", [left; right; archive] ->
              cmd_run { left; right; archive; pairs = one "--pairs";
                        bytes = Option.value (one "--bytes") ~default:"delta";
                        metadata = Option.value (one "--metadata") ~default:"exiftool";
                        index = (match get "--index" with Some [l; r] -> Some (l, r) | _ -> None);
                        workers = (match one "--workers" with Some w -> int_of_string w | None -> default_workers ());
                        impl = Option.value (one "--impl") ~default:"ocaml";
                        max_mib = Option.map float_of_string (one "--max-mib") }
          | "restore", [archive; rel; output] -> cmd_restore ?version:(one "--version") archive rel output
          | "versions", [archive] -> cmd_versions archive None
          | "versions", [archive; rel] -> cmd_versions archive (Some rel)
          | "versions", _ :: _ :: extra -> parser_error top_usage "dngc" ("unrecognized arguments: " ^ String.concat " " extra)
          | "index", [archive] -> cmd_index archive
          | "query", archive :: words -> cmd_query archive words
          | "verify", [archive] ->
              cmd_verify archive (match one "--restore-sample" with Some n -> int_of_string n | None -> 0)
          | "status", [archive] -> cmd_status archive
          | "migrate", [archive] ->
              cmd_migrate archive ~dry_run:(get "--dry-run" <> None) ~report:(one "--report")
                ~undo:(get "--undo" <> None) ~no_verify:(get "--no-verify" <> None)
          | _ -> parser_error top_usage "dngc" "bad arguments"

let main argv =
  try dispatch argv with
  | Die n -> n
  | Sys.Break -> prerr_endline "dngc: interrupted"; 130
  | e -> flush stdout; Printf.eprintf "dngc: %s\n%!" (error_text e); 1

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
     skeleton [Skeleton_section.build] and [info] (dngc.py runs the Rust tool).

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
  | Failure e | Sys_error e | Json.Bad e | Tursolite.Error { message = e; _ } -> e
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
let walk_files ?(progress_secs = 30.) root =
  let out = ref [] and count = ref 0 and dirs = ref 0 in
  let start = Unix.gettimeofday () in
  let last = ref start in
  let rec go dir rel =
    incr dirs;
    let now = Unix.gettimeofday () in
    if now -. !last >= progress_secs then begin
      last := now;
      Printf.printf "  walked %s files in %s folders under %s (%.0fs; now in %s)\n%!"
        (Py.thousands !count) (Py.thousands !dirs) root (now -. start) (if rel = "" then "." else rel)
    end;
    let names = List.sort compare (Array.to_list (Sys.readdir dir)) in
    let sub = List.filter_map (fun n ->
      let p = Filename.concat dir n and r = if rel = "" then n else rel ^ "/" ^ n in
      match (Native.stat ~follow:false p).kind with
      | `Reg -> if n <> ".DS_Store" then (out := r :: !out; incr count); None
      | `Dir -> Some (p, r)
      | _ -> None
      | exception Unix.Unix_error _ -> None) names in
    List.iter (fun (p, r) -> go p r) sub in
  go root "";
  Printf.printf "  walked %s files under %s in %.0fs\n%!" (Py.thousands !count) root (Unix.gettimeofday () -. start);
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
      Printf.printf "Checking that every file under %s is in its index ...\n%!" left;
      let unlisted = List.filter (fun f -> not (Hashtbl.mem known (join left f))) (walk_files left) in
      (* phindex align leaves out names beginning with a dot (.DS_Store, AppleDouble
         files, editors' and copiers' hidden files); they may still be unique, so
         they are kept as full copies. Any other unlisted file means a stale index. *)
      let hidden, missing = List.partition (fun f -> (Filename.basename f).[0] = '.') unlisted in
      (match missing with
       | m :: _ -> die ~status:1 (Printf.sprintf "%d files under LEFT are not in its phindex index (e.g. %s); index LEFT first"
                                    (List.length missing) m)
       | [] -> ());
      if hidden <> [] then
        Printf.printf "%d hidden files (names beginning with a dot) are not aligned; kept as full copies\n%!"
          (List.length hidden);
      pairs @ List.map (fun f -> join left f, None) hidden
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

(* Defined with ls, drop, trash and keep below: the tombstoned paths a run
   skips, and their removal from a rebuilt metadata index. *)
let dropped_paths_hook : (archive -> (string, unit) Hashtbl.t) ref = ref (fun _ -> Hashtbl.create 1)
let apply_tombstones_hook : (archive -> unit) ref = ref (fun _ -> ())

type run_args = {
  left : string; right : string; archive : string; pairs : string option;
  bytes : string; metadata : string; index : (string * string) option;
  workers : int; impl : string; max_mib : float option; trust_files : bool;
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
    let pairs = match a.pairs with
      | Some p -> read_pairs p
      | None ->
          (* An emptied source is an empty directory: covered with 0 files. *)
          if not (is_dir a.left) then
            die (a.left ^ ": no such directory; an emptied source must still exist (empty) for a whole-tree run");
          let pairs = whole_tree_pairs a.left a.right a.index tmp in
          Printf.printf "%s files under LEFT; %s with a counterpart\n%!" (Py.thousands (List.length pairs))
            (Py.thousands (List.length (List.filter (fun (_, r) -> r <> None) pairs)));
          pairs in
    (* Paths dngc drop left a tombstone for are not preserved again. Listed
       paths may be relative to LEFT, absolute, or resolved (realpath). *)
    let dropped = !dropped_paths_hook arc in
    let pairs, pairs_given = if Hashtbl.length dropped = 0 then pairs, a.pairs else begin
      let roots = List.sort_uniq compare [abspath a.left; realpath a.left] in
      let relative l = match List.find_map (fun root ->
          let full = if is_abs l then l else join root l in
          let prefix = root ^ "/" in
          if String.starts_with ~prefix full then Some (String.sub full (String.length prefix) (String.length full - String.length prefix))
          else None) roots with Some r -> r | None -> l in
      let kept = List.filter (fun (l, _) -> not (Hashtbl.mem dropped (relative l))) pairs in
      let n = List.length pairs - List.length kept in
      if n = 0 then pairs, a.pairs else begin
        Printf.printf "%s listed files were dropped from the archive (dngc drop) and are skipped\n%!" (Py.thousands n);
        kept, Option.map (fun _ ->
          let f = join tmp "pairs-without-dropped.tsv" in
          write_text f (String.concat "" (List.map (fun (l, r) -> l ^ "\t" ^ Option.value r ~default:"" ^ "\n") kept)); f) a.pairs
      end
    end in
    let pairs_file = match pairs_given with
      | Some p -> p
      | None ->
          let listed = join tmp "whole-tree-pairs.tsv" in
          write_text listed (String.concat "" (List.filter_map (fun (l, r) ->
            Option.map (fun r -> l ^ "\t" ^ r ^ "\n") r) pairs));
          listed in
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
                     @ (if repeat then ["--trust-pairs"] else []) @ (if engine_metadata then [] else ["--skip-metadata"])
                     @ (if a.trust_files then ["--trust-files"] else []) in
          if a.impl = "rust" then call (Option.get binary :: args)
          else
            step ("dng-archive" :: "all" :: args) (fun () ->
              All.run_options {
                All.left = a.left; right = a.right; pairs_file = dng_file; report = arc.report;
                archive = (if a.bytes = "delta" then Some arc.bytes else None);
                left_index = Option.map fst a.index; right_index = Option.map snd a.index;
                workers = a.workers; batch_size = 128; exiftool_processes = a.workers; progress_secs = 60.;
                rate_mib = rate_value; fresh = false; trust_pairs = repeat; skip_metadata = not engine_metadata;
                trust_files = a.trust_files })
        end else begin
          let command = [python (); join (here_or_die "--impl python") "compare_dng.py"; left_abs; right_abs;
                         "--output"; arc.report; "--workers"; workers; "--exiftool-processes"; workers;
                         "--batch-size"; "128"; "--progress-secs"; "60"; "--pairs-only"; "--pairs"; dng_file]
                        @ (if a.bytes <> "none" then ["--archive"; arc.bytes] else ["--compare-only"]) @ index_args
                        @ (if a.trust_files then ["--trust-files"] else []) in
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
                 "--delta"; "--pairs"; jsonl; "--workers"; num_text pworkers; "--rate-mib"; rate] @ source_index
                @ (if a.trust_files then ["--trust-files"] else []))
        else
          step (["dng-archive"; "preserve"; left_abs; right_abs; arc.bytes; "--delta"; "--pairs"; jsonl;
                 "--workers"; num_text pworkers; "--rate-mib"; rate] @ source_index
                @ (if a.trust_files then ["--trust-files"] else [])) (fun () ->
            if not (rate_value > 0.) then die "Rate must be positive";
            let real p = try Unix.realpath p with Unix.Unix_error (e, _, _) -> failwith (Py.oserror e p) in
            (* Each directory resolved once: a realpath per file looks up every
               component, slow on a USB-bridged RAID for a million files. *)
            let dirs = Hashtbl.create 1024 in
            let real_file p =
              let d = Filename.dirname p in
              let rd = match Hashtbl.find_opt dirs d with
                | Some r -> r | None -> let r = Preserve.resolve d in Hashtbl.add dirs d r; r in
              Filename.concat rd (Filename.basename p) in
            (* Listed paths resolve like the roots they must lie under, as preserve_apo.py resolves them. *)
            (* A listed source that is gone is reported missing by the preservation. *)
            let ppairs = List.map (fun (s, r) -> real_file s, Option.map real_file r) (Preserve.load_pairs jsonl) in
            let indexes = Option.map (fun (l, r) ->
              { Preserve.source_index = Phindex.open_ l; reference_index = Phindex.open_ r }) index_abs in
            if Preserve.scan ?indexes ~trust_files:a.trust_files ~source:(real left_abs) ~reference:(real right_abs) ~archive:arc.bytes
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
          let args = [a.left; a.right; arc.root; "--pairs"; all_pairs; "--left-index"; li; "--right-index"; ri;
                      "--workers"; string_of_int a.workers] in
          step ("dng-archive" :: "phindex-metadata" :: args) (fun () -> Phindex_metadata.run args)
        else
          let li, ri = Option.get index_abs in
          call [python (); join (here_or_die "the Python metadata step") "phindex_metadata.py"; left_abs; right_abs;
                arc.root; "--pairs"; all_pairs; "--left-index"; li; "--right-index"; ri; "--workers"; string_of_int a.workers] in
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

(* Where a record's reference is now, for the commands that rebuild records
   (restore, verify --restore-sample, keep): --reference-root, --index and
   --candidates (docs/dngc-archive-spec.md, 5.5). *)
type where = { reference_root : string option; index : string list; candidates : int option }
let here = { reference_root = None; index = []; candidates = None }

(* Those options, checked, as dng-archive restore takes them. *)
let where_args w =
  (match w.candidates with Some n when n < 0 -> die "--candidates must not be negative" | _ -> ());
  (match w.reference_root with Some r -> ["--reference-root"; r] | None -> [])
  @ List.concat_map (fun i -> ["--index"; i]) w.index
  @ (match w.candidates with Some n -> ["--candidates"; string_of_int n] | None -> [])

(* [f] with the indexes of [w] open. *)
let with_indexes w f =
  let indexes = List.map Phindex.open_lookup w.index in
  Fun.protect ~finally:(fun () -> List.iter Phindex.close_lookup indexes) (fun () -> f indexes)

let cmd_restore ?version ?(where = here) archive rel output =
  let arc = archive_of archive in
  let args = where_args where in
  step (["dng-archive"; "restore"; arc.bytes; rel; output] @ (match version with Some v -> ["--version"; v] | None -> []) @ args)
    (fun () ->
      with_indexes where (fun indexes ->
        Preserve.restore ~top:arc.root ?version ?reference_root:where.reference_root ~indexes ?candidates:where.candidates
          arc.bytes rel output);
      0)

let cmd_index archive =
  let arc = archive_of archive in
  let status = step ["dng-archive"; "index"; arc.root] (fun () -> Index.index_archive arc.root; 0) in
  !apply_tombstones_hook arc;
  status

let presets = [
  "changes", "SELECT tag, category, kind, affected_files, percent_compared FROM field_changes ORDER BY affected_files DESC LIMIT 50";
  "files", "SELECT f.path, d.kind FROM differences d JOIN files f ON f.id=d.file_id JOIN fields t ON t.id=d.field_id \
            WHERE t.tag=? ORDER BY f.path";
  "values", "SELECT f.path, d.kind, a.json_zlib, b.json_zlib FROM differences d JOIN files f ON f.id=d.file_id \
             JOIN fields t ON t.id=d.field_id LEFT JOIN vals a ON a.id=d.apo_value LEFT JOIN vals b ON b.id=d.bumer_value \
             WHERE t.tag=? ORDER BY f.path";
]

(* Read-only, leaving nothing behind: Preserve.readonly_db (immutable=1 when
   no writer holds the lock and the WAL is empty or absent, else a private copy),
   as dngc.py's archive_layout.open_readonly. *)
let open_ro path = Preserve.readonly_db path

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
        ("trashed_records", if table_exists db "trash" then one_int db "SELECT count(*) FROM trash WHERE state='current'" else Json.int 0);
        ("dropped_paths", if table_exists db "dropped" then one_int db "SELECT count(*) FROM dropped" else Json.int 0);
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
  (* The skeleton section (spec section 13): how many contents it can rebuild;
     null when its database cannot be read. *)
  let skeletons = Skeleton.store_path arc.root in
  if exists skeletons then
    add "skeletons" (Json.Obj [("skeletons",
      match open_ro skeletons with
      | db -> Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
          try one_int db "SELECT count(*) FROM skeletons" with Failure _ | Sqlite3.Error _ -> Json.Null)
      | exception (Failure _ | Sqlite3.Error _ | Sys_error _) -> Json.Null)]);
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

(* An index larger than this is copied to a local file before its quick_check. *)
let quick_check_copy_bytes = Int64.mul 256L 1048576L

let cmd_verify ?(where = here) archive restore_sample =
  ignore (where_args where);
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
         let total = List.fold_left (fun n table ->
           match rows db ("SELECT COUNT(*) FROM " ^ table) [] with
           | [| Sqlite3.Data.INT k |] :: _ -> n + Int64.to_int k | _ -> n) 0 tables in
         let t0 = Unix.gettimeofday () in
         let last = ref t0 and checked = ref 0 in
         List.fold_left (fun n table ->
         fold_rows db (statement db ("SELECT path, record FROM " ^ table) []) (fun n row ->
           let r = match row with [| _; Sqlite3.Data.TEXT t |] -> Json.parse t | _ -> Json.Obj [] in
           incr checked;
           let now = Unix.gettimeofday () in
           if now -. !last >= 30. then begin
             last := now;
             Printf.eprintf "verify: bytes: %s of %s records checked | %.0fs\n%!"
               (Py.thousands !checked) (Py.thousands total) (now -. t0)
           end;
           List.fold_left (fun n key ->
             match Json.member key r with
             | Some (Json.Str p) when p <> "" && not (exists (join arc.bytes p)) -> n + 1
             | _ -> n) n ["delta"; "original"]) n) 0 tables) in
       if missing > 0 then problem (Printf.sprintf "bytes: %d deltas or originals named in records are missing" missing);
       (* Payloads of trashed records: under trash/DROP/, or still in place if a drop was interrupted. *)
       let db = open_ro (join arc.bytes "recovery.sqlite3") in
       let lost = Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
         if not (table_exists db "trash") then 0 else
           List.fold_left (fun n row -> match row with
             | [| Sqlite3.Data.TEXT d; Sqlite3.Data.TEXT r |] ->
                 let r = Json.parse r in
                 List.fold_left (fun n key -> match Json.member key r with
                   | Some (Json.Str f) when f <> "" && not (exists (join (join (join arc.root "trash") d) f))
                                            && not (exists (join arc.bytes f)) -> n + 1
                   | _ -> n) n ["delta"; "original"]
             | _ -> n) 0 (rows db "SELECT drop_id, record FROM trash" [])) in
       if lost > 0 then problem (Printf.sprintf "trash: %d payloads of trashed records are missing" lost)
   | None -> ());
  if List.mem_assoc "metadata" s then begin
    (* quick_check reads the index page by page; on spinning disks a large
       index takes most of an hour that way. A large one is read once, in
       order, into a local copy, and the copy is checked. As dngc.py does. *)
    let index = join arc.metadata "differences.sqlite3" in
    let td = Filename.temp_dir "dngc-verify-" "" in
    Fun.protect ~finally:(fun () -> rm_rf td) (fun () ->
      let size = (Unix.LargeFile.stat index).Unix.LargeFile.st_size in
      let checked =
        if size > quick_check_copy_bytes then begin
          Printf.eprintf "verify: metadata index: copying %s MB to a local file for its quick_check\n%!"
            (Py.thousands (Int64.to_int (Int64.div size 1048576L)));
          let local = join td "differences.sqlite3" in
          In_channel.with_open_bin index (fun ic -> Out_channel.with_open_bin local (fun oc ->
            let buf = Bytes.create (8 * 1048576) in
            let rec go () = match In_channel.input ic buf 0 (Bytes.length buf) with
              | 0 -> ()
              | n -> Out_channel.output oc buf 0 n; go () in
            go ()));
          local
        end else index in
      let db = open_ro checked in
      Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
        match rows db "PRAGMA quick_check" [] with
        | [| Sqlite3.Data.TEXT "ok" |] :: _ -> ()
        | row :: _ -> problem ("metadata index: " ^ cell row.(0))
        | [] -> problem "metadata index: None"))
  end;
  (match List.assoc_opt "report" s with
   | Some r when not (Json.truthy (get "complete" r)) -> problem "report: the comparison has not finished"
   | _ -> ());
  (* The skeleton section: its database opens, counts its rows and passes quick_check. *)
  let skeletons = Option.map (get "skeletons") (List.assoc_opt "skeletons" s) in
  (match skeletons with
   | Some Json.Null -> problem "skeleton section: skeletons.sqlite3 cannot be read"
   | Some _ ->
       let db = open_ro (Skeleton.store_path arc.root) in
       Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
         match rows db "PRAGMA quick_check" [] with
         | [| Sqlite3.Data.TEXT "ok" |] :: _ -> ()
         | row :: _ -> problem ("skeleton section: " ^ cell row.(0))
         | [] -> problem "skeleton section: None")
   | None -> ());
  print_endline (Json.dumps_indent (Json.Obj (
    [("archive", Json.Str arc.root); ("sections", Json.Arr (List.map (fun (k, _) -> Json.Str k)
                                                              (List.sort compare s)))]
    @ (match skeletons with Some n -> [("skeletons", n)] | None -> [])
    @ [("problems", Json.Arr (List.map (fun p -> Json.Str p) !problems))])));
  if restore_sample <> 0 && List.mem_assoc "bytes" s then begin
    let db = open_ro (join arc.bytes "recovery.sqlite3") in
    let paths = Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
      List.filter_map (function [| Sqlite3.Data.TEXT p |] -> Some p | _ -> None)
        (rows db "SELECT path FROM files ORDER BY random() LIMIT ?" [Sqlite3.Data.INT (Int64.of_int restore_sample)])) in
    let td = Filename.temp_dir "dngc-verify-" "" in
    Fun.protect ~finally:(fun () -> rm_rf td) (fun () ->
      List.iteri (fun i p ->
        let out = join td (string_of_int i) in
        if step (["dng-archive"; "restore"; arc.bytes; p; out] @ where_args where)
             (fun () ->
               with_indexes where (fun indexes ->
                 Preserve.restore ~top:arc.root ?reference_root:where.reference_root ~indexes ?candidates:where.candidates
                   arc.bytes p out);
               0) <> 0 then
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
  "xmp-original"; "image-comparison"; "finalization-backend.json"; ".byte-preservation.lock"; "versions"]

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

(* ---- ls, drop, trash, keep: as archive_ops.py ---- *)

let trash_dir = "trash"
let kinds = ["identical"; "delta"; "full"]

(* '1 record', '2 records', with thousands separators. *)
let count ?plural n word =
  Py.thousands n ^ " " ^ (if n = 1 then word else Option.value plural ~default:(word ^ "s"))

let jget k r = match Json.member k r with Some v -> v | None -> Json.Null
let truthy k r = Json.truthy (jget k r)
let kind_of r = if truthy "delta" r then "delta" else if truthy "original" r then "full" else "identical"

(* The byte-section paths of a record's payloads. *)
let payloads r = List.filter_map (fun k -> match jget k r with Json.Str p when p <> "" -> Some p | _ -> None) ["delta"; "original"]

(* fnmatch.fnmatchcase: '*' any run of characters ('/' too), '?' one,
   '[...]' a set ('!' negates; a ']' first is literal; an unclosed '[' is literal). *)
let fnmatch pat s =
  let np = String.length pat and ns = String.length s in
  let rec set_end i = if i >= np then None else if pat.[i] = ']' then Some i else set_end (i + 1) in
  let rec go pi si =
    if pi = np then si = ns
    else match pat.[pi] with
      | '*' -> let rec any k = k <= ns && (go (pi + 1) k || any (k + 1)) in any si
      | '?' -> si < ns && go (pi + 1) (si + 1)
      | '[' ->
          let start = pi + 1 in
          let neg = start < np && pat.[start] = '!' in
          let first = if neg then start + 1 else start in
          (match (if first < np && pat.[first] = ']' then set_end (first + 1) else set_end first) with
           | None -> si < ns && s.[si] = '[' && go (pi + 1) (si + 1)
           | Some close ->
               si < ns &&
               (let c = s.[si] in
                let rec inside i = i < close && (
                  if i + 2 < close && pat.[i + 1] = '-' then (c >= pat.[i] && c <= pat.[i + 2]) || inside (i + 3)
                  else pat.[i] = c || inside (i + 1)) in
                inside first <> neg) && go (close + 1) (si + 1))
      | c -> si < ns && s.[si] = c && go (pi + 1) (si + 1) in
  go 0 0

let recovery_path (arc : archive) =
  let p = join arc.bytes "recovery.sqlite3" in
  if not (exists p) then die ("no byte section in " ^ arc.root);
  p

let text_of = function Sqlite3.Data.TEXT t -> t | _ -> ""

(* Relative paths with a tombstone: later runs skip them. *)
let dropped_paths (arc : archive) =
  let p = join arc.bytes "recovery.sqlite3" in
  let set = Hashtbl.create 64 in
  if exists p then begin
    let db = open_ro p in
    Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
      if table_exists db "dropped" then
        List.iter (fun r -> Hashtbl.replace set (text_of r.(0)) ()) (rows db "SELECT path FROM dropped" []))
  end;
  set

(* Relative paths, one per line (or NUL-separated), from a file or stdin ('-'). *)
let read_list source nul =
  let text = if source = "-" then In_channel.input_all stdin else read_text source in
  List.filter (fun p -> String.exists (fun c -> c <> '\r') p) (String.split_on_char (if nul then '\000' else '\n') text)

let emit lines nul = List.iter (fun l -> print_string l; print_char (if nul then '\000' else '\n')) lines; flush stdout

(* (path, record) of the current records that match a glob or are listed, by path. *)
let select db globs listed =
  List.filter_map (fun r ->
    let path = text_of r.(0) in
    if (match listed with Some l -> Hashtbl.mem l path | None -> false) || List.exists (fun g -> fnmatch g path) globs
    then Some (path, Json.parse (text_of r.(1))) else None)
    (rows db "SELECT path, record FROM files ORDER BY path" [])

let changed_paths (arc : archive) tag =
  let index = join arc.metadata "differences.sqlite3" in
  if not (exists index) then die ("no metadata index in " ^ arc.root);
  let db = open_ro index in
  let set = Hashtbl.create 64 in
  Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
    List.iter (fun r -> Hashtbl.replace set (text_of r.(0)) ())
      (rows db "SELECT DISTINCT f.path FROM differences d JOIN fields t ON t.id=d.field_id JOIN files f ON f.id=d.file_id \
                WHERE t.tag=? OR t.tag LIKE '%:' || ?" [Sqlite3.Data.TEXT tag; Sqlite3.Data.TEXT tag]));
  set

type ls_args = { globs : string list; kind : string option; changed : string option; trashed : bool;
                 print : string; as_json : bool; nul : bool }

let cmd_ls archive (o : ls_args) =
  let arc = archive_of archive in
  let db = open_ro (recovery_path arc) in
  let all = Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
    if o.trashed then
      (if table_exists db "trash" then
         List.map (fun r -> text_of r.(0), Json.parse (text_of r.(1)), Some (text_of r.(2)))
           (rows db "SELECT path, record, drop_id FROM trash WHERE state='current' ORDER BY path, drop_id" [])
       else [])
    else List.map (fun r -> text_of r.(0), Json.parse (text_of r.(1)), None)
        (rows db "SELECT path, record FROM files ORDER BY path" [])) in
  let all = if o.globs = [] then all else List.filter (fun (p, _, _) -> List.exists (fun g -> fnmatch g p) o.globs) all in
  let all = match o.kind with Some k -> List.filter (fun (_, r, _) -> kind_of r = k) all | None -> all in
  let all = match o.changed with
    | Some tag -> let c = changed_paths arc tag in List.filter (fun (p, _, _) -> Hashtbl.mem c p) all
    | None -> all in
  if o.as_json then
    emit (List.map (fun (p, r, d) ->
      Json.dumps (Json.Obj ([("path", Json.Str p); ("kind", Json.Str (kind_of r))]
                            @ (match d with Some d -> [("drop", Json.Str d)] | None -> []) @ [("record", r)]))) all) o.nul
  else
    emit (List.filter_map (fun (p, r, _) ->
      match o.print with
      | "path" -> Some p
      | key -> (match jget key r with Json.Str v when v <> "" -> Some v | _ -> None)) all) o.nul;
  0

(* ARCHIVE.json: d.update(changes) for the drop with this id. *)
let manifest_update (arc : archive) id changes =
  let set_all fields l = List.fold_left (fun l (k, v) ->
    if List.mem_assoc k l then List.map (fun (k', v') -> k', if k' = k then v else v') l else l @ [(k, v)]) l fields in
  let m = match read_manifest arc with
    | Json.Obj l -> Json.Obj (List.map (fun (k, v) ->
        if k <> "drops" then k, v else k, (match v with
          | Json.Arr ds -> Json.Arr (List.map (fun d -> match d with
              | Json.Obj f when jget "id" d = Json.Str id -> Json.Obj (set_all changes f)
              | d -> d) ds)
          | v -> v)) l)
    | m -> m in
  write_manifest arc m

let manifest_append (arc : archive) key entry = write_manifest arc (append_to key entry (read_manifest arc))

let drops (arc : archive) = match Json.member "drops" (read_manifest arc) with Some (Json.Arr l) -> l | _ -> []

let new_drop_id (arc : archive) =
  let taken = List.map (fun d -> Json.str (jget "id" d)) (drops arc) in
  let t = Unix.gmtime (Unix.time ()) in
  let base = Printf.sprintf "%04d%02d%02dT%02d%02d%02dZ" (t.tm_year + 1900) (t.tm_mon + 1) t.tm_mday t.tm_hour t.tm_min t.tm_sec in
  let rec go n = let c = if n = 1 then base else Printf.sprintf "%s-%d" base n in
    if List.mem c taken || exists (join (join arc.root trash_dir) c) then go (n + 1) else c in
  go 1

let size_of p = try Int64.to_int (Unix.LargeFile.stat p).Unix.LargeFile.st_size with Unix.Unix_error _ -> 0

(* Remove empty directories from [d] up to, not including, [stop]. *)
let rec prune d stop =
  if d <> stop && is_dir d && (try Sys.readdir d = [||] with Sys_error _ -> false) then begin
    Unix.rmdir d; prune (Filename.dirname d) stop
  end

let exec_sql db sql params = fold_rows db (statement db sql params) (fun () _ -> ()) ()
let transaction db f =
  check (Sqlite3.exec db "BEGIN") db;
  match f () with
  | v -> check (Sqlite3.exec db "COMMIT") db; v
  | exception e -> ignore (Sqlite3.exec db "ROLLBACK"); raise e
let open_rw p = Sqlite3.db_open p
let close_rw db = ignore (Sqlite3.db_close db)

type select_args = { sglobs : string list; from : string option; snul : bool }

let resolve_selection db (o : select_args) =
  let listed = Option.map (fun f ->
    let h = Hashtbl.create 64 in List.iter (fun p -> Hashtbl.replace h p ()) (read_list f o.snul); h) o.from in
  if o.sglobs = [] && listed = None then die "nothing selected: give --glob PATTERN or --from FILE";
  let selected = select db o.sglobs listed in
  Option.iter (fun l ->
    let present = Hashtbl.create 64 in
    List.iter (fun (p, _) -> Hashtbl.replace present p ()) selected;
    List.iter (fun p -> Printf.eprintf "not in the archive: %s\n%!" p)
      (List.sort compare (Hashtbl.fold (fun p () acc -> if Hashtbl.mem present p then acc else p :: acc) l []))) listed;
  selected

let sql_of_json = function
  | Json.Null -> Sqlite3.Data.NULL
  | Json.Str s -> Sqlite3.Data.TEXT s
  | Json.Num n -> (match Int64.of_string_opt n with Some i -> Sqlite3.Data.INT i | None -> Sqlite3.Data.FLOAT (float_of_string n))
  | v -> Sqlite3.Data.TEXT (Json.dumps v)

let json_of_sql = function
  | Sqlite3.Data.NULL | Sqlite3.Data.NONE -> Json.Null
  | Sqlite3.Data.INT n -> Json.int64 n
  | Sqlite3.Data.FLOAT x -> Json.Num (Json.py_float x)
  | Sqlite3.Data.TEXT s -> Json.Str s
  | Sqlite3.Data.BLOB b -> Json.Str b

let metadata_rw (arc : archive) = let p = join arc.metadata "differences.sqlite3" in if exists p then Some (open_rw p) else None

(* Move (or delete) the metadata index rows of the dropped paths. *)
let drop_metadata (arc : archive) did paths force =
  Option.iter (fun db ->
    Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
      let cols = List.map (fun r -> text_of r.(1)) (rows db "PRAGMA table_info(files)" []) in
      transaction db (fun () ->
        exec_sql db "CREATE TABLE IF NOT EXISTS trash_files (drop_id TEXT NOT NULL, path TEXT NOT NULL, \
                     row TEXT NOT NULL, differences TEXT NOT NULL)" [];
        List.iter (fun p ->
          List.iter (fun row ->
            let fields = List.mapi (fun i c -> c, json_of_sql row.(i)) cols in
            let fid = row.(Option.get (List.find_index (( = ) "id") cols)) in
            let diffs = List.map (fun d -> Json.Arr (Array.to_list (Array.map json_of_sql d)))
                (rows db "SELECT field_id, kind, apo_value, bumer_value FROM differences WHERE file_id=? ORDER BY rowid" [fid]) in
            if not force then
              exec_sql db "INSERT INTO trash_files VALUES (?,?,?,?)"
                [Sqlite3.Data.TEXT did; Sqlite3.Data.TEXT p; Sqlite3.Data.TEXT (Json.dumps (Json.Obj fields));
                 Sqlite3.Data.TEXT (Json.dumps (Json.Arr diffs))];
            exec_sql db "DELETE FROM differences WHERE file_id=?" [fid];
            exec_sql db "DELETE FROM files WHERE id=?" [fid])
            (rows db (Printf.sprintf "SELECT %s FROM files WHERE path=? ORDER BY id" (String.concat ", " cols)) [Sqlite3.Data.TEXT p]))
          (List.sort compare paths)))) (metadata_rw arc)

(* After the metadata index is rebuilt from the batches: rows of dropped paths go again. *)
let apply_tombstones (arc : archive) =
  let dropped = dropped_paths arc in
  if Hashtbl.length dropped > 0 then
    Option.iter (fun db ->
      Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
        transaction db (fun () ->
          List.iter (fun p ->
            List.iter (fun r ->
              exec_sql db "DELETE FROM differences WHERE file_id=?" [r.(0)];
              exec_sql db "DELETE FROM files WHERE id=?" [r.(0)]) (rows db "SELECT id FROM files WHERE path=?" [Sqlite3.Data.TEXT p]))
            (List.sort compare (Hashtbl.fold (fun p () acc -> p :: acc) dropped []))))) (metadata_rw arc)

let release_locks held = List.iter Unix.close held

let cmd_drop archive (o : select_args) ~yes ~force =
  let arc = archive_of archive in
  let path = recovery_path arc in
  let db = open_ro path in
  let selected, earlier = Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
    let selected = resolve_selection db o in
    let wanted = Hashtbl.create 64 in
    List.iter (fun (p, _) -> Hashtbl.replace wanted p ()) selected;
    selected, (if table_exists db "versions" then
                 List.filter_map (fun r -> let p = text_of r.(0) in
                   if Hashtbl.mem wanted p then Some (p, text_of r.(1), Json.parse (text_of r.(2))) else None)
                   (rows db "SELECT path, version, record FROM versions ORDER BY path, version" [])
               else [])) in
  let files = List.concat_map (fun (_, r) -> payloads r) selected @ List.concat_map (fun (_, _, r) -> payloads r) earlier in
  let total = List.fold_left (fun n f -> n + size_of (join arc.bytes f)) 0 files in
  let counts = Printf.sprintf "%s (%s) and %s (%s)" (count (List.length selected) "record")
      (count (List.length earlier) "earlier version") (count (List.length files) "payload") (count total "byte") in
  if not (yes || force) then begin
    emit (List.map fst selected) false;
    Printf.printf "would move %s to the trash; --yes to do it, --force to delete them instead\n%!" counts; 0
  end else if selected = [] then (print_endline "nothing to drop"; 0)
  else begin
    let held = hold_locks arc in
    let did = new_drop_id arc in
    let db = open_rw path in
    Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
      transaction db (fun () ->
        exec_sql db "CREATE TABLE IF NOT EXISTS dropped (path TEXT PRIMARY KEY, drop_id TEXT NOT NULL)" [];
        exec_sql db "CREATE TABLE IF NOT EXISTS trash (drop_id TEXT NOT NULL, state TEXT NOT NULL, path TEXT NOT NULL, \
                     version TEXT NOT NULL, record TEXT NOT NULL)" [];
        let t s = Sqlite3.Data.TEXT s in
        List.iter (fun (p, r) ->
          if not force then exec_sql db "INSERT INTO trash VALUES (?,?,?,?,?)" [t did; t "current"; t p; t ""; t (Json.dumps r)];
          exec_sql db "DELETE FROM files WHERE path=?" [t p];
          exec_sql db "INSERT OR REPLACE INTO dropped VALUES (?,?)" [t p; t did]) selected;
        List.iter (fun (p, v, r) ->
          if not force then exec_sql db "INSERT INTO trash VALUES (?,?,?,?,?)" [t did; t "earlier"; t p; t v; t (Json.dumps r)];
          exec_sql db "DELETE FROM versions WHERE path=? AND version=?" [t p; t v]) earlier));
    List.iter (fun f ->
      let src = join arc.bytes f in
      if exists src then begin
        if force then Sys.remove src
        else begin
          let dst = join (join (join arc.root trash_dir) did) f in
          Native.mkdir_p (Filename.dirname dst);
          Sys.rename src dst
        end;
        prune (Filename.dirname src) arc.bytes
      end) files;
    drop_metadata arc did (List.map fst selected) force;
    manifest_append arc "drops" (Json.Obj [
      ("id", Json.Str did); ("time", Json.Str (timestamp ())); ("state", Json.Str (if force then "deleted" else "trashed"));
      ("globs", Json.Arr (List.map (fun g -> Json.Str g) o.sglobs)); ("listed", Json.Bool (o.from <> None));
      ("records", Json.int (List.length selected)); ("earlier_versions", Json.int (List.length earlier));
      ("payloads", Json.int (List.length files)); ("payload_bytes", Json.int total)]);
    release_locks held;
    Printf.printf "drop %s: %s %s%s; later runs skip these paths\n%!" did (if force then "deleted" else "moved") counts
      (if force then "" else " to the trash");
    0
  end

let find_drop (arc : archive) id =
  match List.find_opt (fun d -> jget "id" d = Json.Str id) (drops arc) with
  | Some d -> d
  | None -> die ~status:1 (Printf.sprintf "no drop %s in %s" id arc.root)

let num k d = match jget k d with Json.Num n -> int_of_string n | _ -> 0
let dstr k d = match jget k d with Json.Str s -> s | Json.Num n -> n | _ -> ""

let restore_metadata (arc : archive) did =
  Option.iter (fun db ->
    Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
      transaction db (fun () ->
        if table_exists db "trash_files" then begin
          List.iter (fun r ->
            let fields = match Json.parse (text_of r.(0)) with
              | Json.Obj l -> List.filter (fun (k, _) -> k <> "id") l | _ -> [] in
            exec_sql db (Printf.sprintf "INSERT INTO files (%s) VALUES (%s)" (String.concat ", " (List.map fst fields))
                           (String.concat ", " (List.map (fun _ -> "?") fields))) (List.map (fun (_, v) -> sql_of_json v) fields);
            let fid = Sqlite3.Data.INT (Sqlite3.last_insert_rowid db) in
            (match Json.parse (text_of r.(1)) with
             | Json.Arr ds -> List.iter (function
                 | Json.Arr [f; k; a; b] -> exec_sql db "INSERT INTO differences VALUES (?,?,?,?,?)" [fid; sql_of_json f; sql_of_json k; sql_of_json a; sql_of_json b]
                 | _ -> ()) ds
             | _ -> ()))
            (rows db "SELECT row, differences FROM trash_files WHERE drop_id=? ORDER BY rowid" [Sqlite3.Data.TEXT did]);
          exec_sql db "DELETE FROM trash_files WHERE drop_id=?" [Sqlite3.Data.TEXT did]
        end))) (metadata_rw arc)

let cmd_trash archive action drop_id ~yes =
  let arc = archive_of archive in
  let all = drops arc in
  let t s = Sqlite3.Data.TEXT s in
  match action with
  | "list" ->
      print_endline "drop\tstate\trecords\tearlier\tpayload_bytes\ttime";
      List.iter (fun d -> Printf.printf "%s\t%s\t%s\t%s\t%s\t%s\n" (dstr "id" d) (dstr "state" d) (dstr "records" d)
                   (dstr "earlier_versions" d) (dstr "payload_bytes" d) (dstr "time" d)) all;
      0
  | "empty" ->
      let targets = match drop_id with Some id -> [find_drop arc id] | None -> all in
      let targets = List.filter (fun d -> dstr "state" d = "trashed") targets in
      if not yes then begin
        List.iter (fun d -> Printf.printf "would delete drop %s: %s (%s) and %s (%s); --yes to do it\n" (dstr "id" d)
                     (count (num "records" d) "record") (count (num "earlier_versions" d) "earlier version")
                     (count (num "payloads" d) "payload") (count (num "payload_bytes" d) "byte")) targets;
        0
      end else begin
        let held = hold_locks arc in
        List.iter (fun d ->
          let id = dstr "id" d in
          let db = open_rw (recovery_path arc) in
          Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
            transaction db (fun () -> exec_sql db "DELETE FROM trash WHERE drop_id=?" [t id]));
          Option.iter (fun m -> Fun.protect ~finally:(fun () -> close_rw m) (fun () ->
            transaction m (fun () -> if table_exists m "trash_files" then exec_sql m "DELETE FROM trash_files WHERE drop_id=?" [t id])))
            (metadata_rw arc);
          rm_rf (join (join arc.root trash_dir) id);
          prune (join arc.root trash_dir) arc.root;
          manifest_update arc id [("state", Json.Str "emptied"); ("emptied", Json.Str (timestamp ()))];
          Printf.printf "drop %s emptied: %s and %s deleted; later runs still skip these paths\n%!" id
            (count (num "records" d) "record") (count (num "payload_bytes" d) "byte")) targets;
        release_locks held;
        0
      end
  | _ ->
      let id = match drop_id with
        | Some id -> id
        | None -> die (Printf.sprintf "trash %s needs a drop id (see dngc trash ARCHIVE)" action) in
      let d = find_drop arc id in
      if action = "release" then begin
        let held = hold_locks arc in
        let db = open_rw (recovery_path arc) in
        let n = Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
          if table_exists db "dropped" then
            transaction db (fun () -> exec_sql db "DELETE FROM dropped WHERE drop_id=?" [t id]; Sqlite3.changes db)
          else 0) in
        manifest_update arc id [("released", Json.Str (timestamp ()))];
        release_locks held;
        Printf.printf "drop %s: %s released; later runs preserve them again\n%!" id (count n "path");
        0
      end else begin
        if dstr "state" d <> "trashed" then die ~status:1 (Printf.sprintf "drop %s is %s, not in the trash" id (dstr "state" d));
        let held = hold_locks arc in
        let db = open_rw (recovery_path arc) in
        let restored = Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
          let trashed = List.map (fun r -> text_of r.(0), text_of r.(1), text_of r.(2), text_of r.(3))
              (rows db "SELECT state, path, version, record FROM trash WHERE drop_id=? ORDER BY rowid" [t id]) in
          (match List.find_opt (fun (s, p, _, _) -> s = "current" && rows db "SELECT 1 FROM files WHERE path=?" [t p] <> []) trashed with
           | Some (_, p, _, _) -> die ~status:1 (Printf.sprintf "%s has a newer record; not restoring drop %s" p id)
           | None -> ());
          List.iter (fun (_, _, _, r) ->
            List.iter (fun f ->
              let src = join (join (join arc.root trash_dir) id) f and dst = join arc.bytes f in
              if exists src && not (exists dst) then (Native.mkdir_p (Filename.dirname dst); Sys.rename src dst))
              (payloads (Json.parse r))) trashed;
          transaction db (fun () ->
            List.iter (fun (s, p, v, r) ->
              if s = "current" then exec_sql db "INSERT INTO files VALUES (?,?)" [t p; t r]
              else exec_sql db "INSERT OR REPLACE INTO versions VALUES (?,?,?)" [t p; t v; t r]) trashed;
            exec_sql db "DELETE FROM trash WHERE drop_id=?" [t id];
            exec_sql db "DELETE FROM dropped WHERE drop_id=?" [t id]);
          trashed) in
        restore_metadata arc id;
        rm_rf (join (join arc.root trash_dir) id);
        prune (join arc.root trash_dir) arc.root;
        manifest_update arc id [("state", Json.Str "restored"); ("restored", Json.Str (timestamp ()))];
        release_locks held;
        let current = List.length (List.filter (fun (s, _, _, _) -> s = "current") restored) in
        Printf.printf "drop %s restored: %s (%s)\n%!" id (count current "record")
          (count (List.length restored - current) "earlier version");
        0
      end

(* Set a record's field in place, or add it at the end, as a Python dict does. *)
let set_field k v = function
  | Json.Obj l when List.mem_assoc k l -> Json.Obj (List.map (fun (k', v') -> k', if k' = k then v else v') l)
  | Json.Obj l -> Json.Obj (l @ [(k, v)])
  | r -> r

let cmd_keep ?(where = here) archive (o : select_args) ~yes =
  let arc = archive_of archive in
  ignore (where_args where);
  let indexes = List.map Phindex.open_lookup where.index in
  let path = recovery_path arc in
  let db = open_ro path in
  let selected = Fun.protect ~finally:(fun () -> close_ro db) (fun () -> resolve_selection db o) in
  let todo = List.filter (fun (_, r) -> kind_of r <> "full") selected in
  let full = List.length selected - List.length todo in
  let size r = match jget "source_identity" r with Json.Arr (_ :: Json.Num n :: _) -> int_of_string n | _ -> 0 in
  let total = List.fold_left (fun n (_, r) -> n + size r) 0 todo in
  if not yes then begin
    emit (List.map fst todo) false;
    Printf.printf "would keep %s (%s) as full copies in the archive; %s already %s; --yes to do it\n%!"
      (count (List.length todo) "file") (count total "byte") (count full "file") (if full = 1 then "is" else "are");
    0
  end else begin
    let held = hold_locks arc in
    let kept = ref 0 and errors = ref 0 and kept_bytes = ref 0 in
    let db = open_rw path in
    Fun.protect ~finally:(fun () -> close_rw db) (fun () ->
      List.iter (fun (p, r) ->
        let target = join (join arc.bytes "originals") p in
        match
          if exists target then failwith (target ^ " exists");
          Native.mkdir_p (Filename.dirname target);
          Preserve.reconstruct ~top:arc.root ~indexes ?candidates:where.candidates
            (Preserve.relocate where.reference_root r) arc.bytes target
        with
        | exception e ->
            Printf.eprintf "%s: %s\n%!" p (match e with Failure m -> m | e -> error_text e);
            incr errors
        | () ->
            let delta = match jget "delta" r with Json.Str d when d <> "" -> Some d | _ -> None in
            let bytes = size_of target in
            let r = set_field "original" (Json.Str ("originals/" ^ p)) r |> set_field "preserved_bytes" (Json.int bytes) in
            let r = List.fold_left (fun r k -> match r with
              | Json.Obj l when List.mem_assoc k l -> set_field k Json.Null r | r -> r) r ["delta"; "delta_sha256"; "delta_format"] in
            transaction db (fun () ->
              exec_sql db "UPDATE files SET record=? WHERE path=?" [Sqlite3.Data.TEXT (Json.dumps r); Sqlite3.Data.TEXT p]);
            Option.iter (fun d -> let f = join arc.bytes d in
              if exists f then (Sys.remove f; prune (Filename.dirname f) arc.bytes)) delta;
            incr kept; kept_bytes := !kept_bytes + bytes) todo);
    manifest_append arc "keeps" (Json.Obj [
      ("time", Json.Str (timestamp ())); ("globs", Json.Arr (List.map (fun g -> Json.Str g) o.sglobs));
      ("listed", Json.Bool (o.from <> None)); ("files", Json.int !kept); ("bytes", Json.int !kept_bytes);
      ("errors", Json.int !errors)]);
    release_locks held;
    Printf.printf "kept %s (%s) as full copies; %s already %s; %s\n%!" (count !kept "file") (count !kept_bytes "byte")
      (count full "file") (if full = 1 then "was" else "were") (count !errors "error");
    if !errors > 0 then 1 else 0
  end

let () = dropped_paths_hook := dropped_paths; apply_tombstones_hook := apply_tombstones

(* restore of a dropped path: say where it went instead of "not in the archive". *)
let refuse_dropped archive rel =
  let arc = archive_of archive in
  let recovery = join arc.bytes "recovery.sqlite3" in
  if exists recovery then begin
    let db = open_ro recovery in
    let t s = Sqlite3.Data.TEXT s in
    let refusal = Fun.protect ~finally:(fun () -> close_ro db) (fun () ->
      if rows db "SELECT 1 FROM files WHERE path=?" [t rel] = [] && table_exists db "dropped" then
        match rows db "SELECT drop_id FROM dropped WHERE path=?" [t rel] with
        | [| Sqlite3.Data.TEXT d |] :: _ ->
            let in_trash = table_exists db "trash" && rows db "SELECT 1 FROM trash WHERE path=? AND drop_id=?" [t rel; t d] <> [] in
            Some (Printf.sprintf "%s was dropped from the archive (drop %s); %s" rel d
                    (if in_trash then "dngc trash restore " ^ d ^ " puts it back" else "its record was deleted"))
        | _ -> None
      else None) in
    Option.iter (fun m -> die ~status:1 m) refusal
  end

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
      else Json.Null);
    ("metadata_files", let index = join a.root "metadata/differences.sqlite3" in
      if exists index then begin
        let db = Preserve.readonly_db index in
        Fun.protect ~finally:(fun () -> ignore (Sqlite3.db_close db)) (fun () ->
          let st = Sqlite3.prepare db "SELECT count(*) FROM files" in
          let n = match Sqlite3.step st with Sqlite3.Rc.ROW -> (match Sqlite3.column st 0 with Sqlite3.Data.INT n -> Json.int64 n | _ -> Json.Null) | _ -> Json.Null in
          ignore (Sqlite3.finalize st); n)
      end else Json.Null)]

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
    @ (if List.exists (fun e -> op e = "mkdir") entries && is_dir (join root "bytes") then [`Rmdir] else [])
    (* ARCHIVE.json that a metadata-only migration wrote; the log keeps the record. *)
    @ (if List.exists (fun e -> op e = "manifest") entries && exists a.manifest then [`Remove "ARCHIVE.json"] else []) in
  let manifest_made = List.exists (fun e -> op e = "manifest") entries in
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
      | `Remove n -> log_step a [("op", Json.Str "remove"); ("path", Json.Str n)];
          if n = "ARCHIVE.json" then Sys.remove (join root n) else rm_rf (join root n)
      | `Rmdir ->
          if Sys.readdir (join root "bytes") <> [||] then print_endline "bytes/ is not empty; left in place"
          else (log_step a [("op", Json.Str "rmdir"); ("path", Json.Str "bytes")]; Unix.rmdir (join root "bytes"))) plan;
  if dry_run then 0
  else if manifest_made then begin
    log_step a [("op", Json.Str "undo-done"); ("time", now ())];
    List.iter Unix.close held;
    print_endline "Undone: ARCHIVE.json removed";
    0
  end else begin
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
    (* false: a metadata-only archive, which only gains ARCHIVE.json *)
    let has_bytes = ref true in
    let report =
      if resuming then begin
        (match entries with
         | e :: _ -> (match Json.member "byte_section" e with Some (Json.Bool false) -> has_bytes := false | _ -> ())
         | [] -> ());
        let logged = match entries with e :: _ -> (match Json.member "report" e with Some (Json.Str r) -> Some r | _ -> None) | [] -> None in
        (match report with
         | Some r when Some r <> logged ->
             die ~status:1 (Printf.sprintf "the interrupted migration was started with --report %s; rerun it with that"
                              (match logged with Some l -> l | None -> "None"))
         | _ -> ());
        logged
      end else begin
        (* An archive with metadata but no byte section and no ARCHIVE.json (such
           as a Semantic Unison semdiff archive) gains ARCHIVE.json and nothing else. *)
        let metadata_only = not (lexists (join root "recovery.sqlite3")) && not (lexists (join root "bytes"))
                            && not (exists a.manifest)
                            && (lexists (join root "metadata") || lexists (join root "provenance.json")) in
        if metadata_only then has_bytes := false
        else if not (lexists (join root "recovery.sqlite3")) then begin
          let msg = root ^ " is not a legacy archive (no recovery.sqlite3 at the top); nothing to migrate" in
          if report <> None then die ~status:1 msg;
          print_endline msg;
          raise (Die 0)
        end
        else if lexists (join root "bytes") then
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
    let has_bytes = !has_bytes in
    let names = if has_bytes then List.filter (fun n -> lexists (join root n)) byte_entries else [] in
    let device = (Native.stat root).dev in
    List.iter (fun n ->
      if (Native.stat ~follow:false (join root n)).dev <> device then
        die ~status:1 (Printf.sprintf "%s is on another device than %s; a rename cannot move it and migrate does not copy" n root))
      (names @ (if lexists (join root "bytes") then ["bytes"] else []));
    let plan = (if not has_bytes || lexists (join root "bytes") then [] else [`Mkdir])
               @ List.map (fun n -> `Move n) names
               @ (match report with Some r when not (lexists (join root "report")) -> [`Copy r] | _ -> []) in
    let keep = List.sort compare (List.filter (fun n -> not (List.mem n names) && (n <> "bytes" || not has_bytes))
                                    (Array.to_list (Sys.readdir root))) in
    Printf.printf "dngc migrate %s%s\n%!" root (if dry_run then " (dry run: nothing is changed)" else "");
    if resuming then Printf.printf "resuming the migration that %s records\n%!" migration_log;
    if not dry_run && not resuming then
      log_step a [("op", Json.Str "begin"); ("time", now ()); ("archive", Json.Str root);
                  ("report", match report with Some r -> Json.Str r | None -> Json.Null);
                  ("byte_section", Json.Bool has_bytes)];
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
      if not has_bytes then log_step a [("op", Json.Str "manifest"); ("path", Json.Str "ARCHIVE.json")];
      write_manifest a (append_to "migrations" (Json.Obj [
          ("migrated", now ()); ("moved", Json.Arr moved);
          ("report", match report with
            | Some r -> Json.Obj [("path", Json.Str "report"); ("from", Json.Str r)] | None -> Json.Null);
          ("summary", migration_summary a)]) (read_manifest a));
      log_step a [("op", Json.Str "done"); ("time", now ())];
      List.iter Unix.close held;
      if has_bytes then Printf.printf "Migrated %d paths into %s\n%!" (List.length moved) (join root "bytes")
      else Printf.printf "Wrote %s; there is no byte section to move\n%!" a.manifest;
      if no_verify then 0
      else if cmd_verify root 3 <> 0 then begin
        Printf.eprintf "dngc: error: verify found problems after the migration; it is kept \
                        (dngc migrate --undo %s reverses it)\n%!" root;
        1
      end else 0
    end
  end

(* ---- The command line, as dngc.py's argparse parser reads it ---- *)

let top_usage = "usage: dngc [-h]\n            {run,restore,versions,index,query,verify,status,ls,drop,trash,keep,migrate,skeleton} ..."

let top_help = top_usage ^ {|

dngc: one command for dng-compare's comparison, preservation and metadata
tools.

positional arguments:
  {run,restore,versions,index,query,verify,status,ls,drop,trash,keep,migrate,skeleton}
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
    ls                  list the archive's records
    drop                move records to the trash (--yes) or delete them
                        (--force)
    trash               list drops, restore one, empty the trash, or release
                        tombstones
    keep                keep records as full copies, so their reference files
                        can go
    migrate             move a legacy archive's byte section into bytes/
    skeleton            build or count skeletons, which rebuild reference
                        files after they are edited

options:
  -h, --help            show this help message and exit|}

type option_spec = { flag : string; nargs : int; kind : [ `Str | `Int | `Float ]; choices : string list;
                     aliases : string list; append : bool }

type verb = {
  name : string; usage : string; help : string;
  positionals : string list; rest : bool;  (* a final nargs='*' positional *)
  options : option_spec list;
}

let opt ?(nargs = 1) ?(kind = `Str) ?(choices = []) ?(aliases = []) ?(append = false) flag =
  { flag; nargs; kind; choices; aliases; append }

let run_usage = {|usage: dngc run [-h] [--pairs PAIRS] [--bytes {delta,full,none}]
                [--metadata {exiftool,phindex,none}]
                [--index LEFT.sqlite RIGHT.sqlite] [--workers WORKERS]
                [--impl {ocaml,rust,python}] [--max-mib MAX_MIB]
                [--trust-files]
                left right archive|}

let simple_help usage positionals width =
  usage ^ "\n\npositional arguments:\n" ^ String.concat "" (List.map (fun p -> "  " ^ p ^ "\n") positionals)
  ^ "\noptions:\n  -h, --help" ^ String.make width ' ' ^ "show this help message and exit"

let migrate_usage = {|usage: dngc migrate [-h] [--dry-run] [--report DIR] [--undo] [--no-verify]
                    archive|}

let ls_usage = {|usage: dngc ls [-h] [--glob PATTERN] [--kind {identical,delta,full}]
               [--changed TAG] [--trashed] [--print {path,source,reference}]
               [--json] [-0]
               archive|}
let drop_usage = {|usage: dngc drop [-h] [--glob PATTERN] [--from FILE] [-0] [--yes] [--force]
                 archive|}
let trash_usage = "usage: dngc trash [-h] [--yes] archive [{list,restore,empty,release}] [drop]"
let keep_usage = "usage: dngc keep [-h] [--glob PATTERN] [--from FILE] [-0] [--yes]\n                 [--reference-root DIR] [--index FILE] [--candidates N]\n                 archive"

let skeleton_usage = "usage: dngc skeleton [-h] [--workers WORKERS] [--limit N] {build,info} archive"

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
  --max-mib MAX_MIB     limit reads to this many MiB/s (default: no limit)
  --trust-files         skip the final identity check: nothing changes the
                        trees during the run|};
    options = [opt "--pairs"; opt "--bytes" ~choices:["delta"; "full"; "none"];
               opt "--metadata" ~choices:["exiftool"; "phindex"; "none"]; opt "--index" ~nargs:2;
               opt "--workers" ~kind:`Int; opt "--impl" ~choices:["ocaml"; "rust"; "python"];
               opt "--max-mib" ~kind:`Float; opt "--trust-files" ~nargs:0] };
  { name = "restore"; usage = "usage: dngc restore [-h] [--version LABEL] [--reference-root DIR]\n                    [--index FILE] [--candidates N]\n                    archive relative_path output";
    help = {|usage: dngc restore [-h] [--version LABEL] [--reference-root DIR]
                    [--index FILE] [--candidates N]
                    archive relative_path output

positional arguments:
  archive
  relative_path
  output

options:
  -h, --help            show this help message and exit
  --version LABEL       an earlier content of the path (see dngc versions)
  --reference-root DIR  the reference tree, when it has moved: references are
                        read below it
  --index FILE          a phindex index to find a moved, renamed or edited
                        reference in (repeatable)
  --candidates N        the most files tried per record in each index step
                        (default 8; 0: none)|};
    positionals = ["archive"; "relative_path"; "output"]; rest = false;
    options = [opt "--version"; opt "--reference-root"; opt "--index" ~append:true; opt "--candidates" ~kind:`Int] };
  { name = "versions"; usage = "usage: dngc versions [-h] archive [relative_path]";
    help = simple_help "usage: dngc versions [-h] archive [relative_path]" ["archive"; "relative_path"] 5;
    positionals = ["archive"]; rest = true; options = [] };
  { name = "index"; usage = "usage: dngc index [-h] archive";
    help = simple_help "usage: dngc index [-h] archive" ["archive"] 2;
    positionals = ["archive"]; rest = false; options = [] };
  { name = "query"; usage = "usage: dngc query [-h] archive [query ...]";
    help = simple_help "usage: dngc query [-h] archive [query ...]" ["archive"; "query"] 2;
    positionals = ["archive"]; rest = true; options = [] };
  { name = "verify"; usage = "usage: dngc verify [-h] [--restore-sample N] [--reference-root DIR]\n                   [--index FILE] [--candidates N]\n                   archive";
    help = {|usage: dngc verify [-h] [--restore-sample N] [--reference-root DIR]
                   [--index FILE] [--candidates N]
                   archive

positional arguments:
  archive

options:
  -h, --help            show this help message and exit
  --restore-sample N    also rebuild N records at random
  --reference-root DIR  the reference tree, when it has moved: references are
                        read below it
  --index FILE          a phindex index to find a moved, renamed or edited
                        reference in (repeatable)
  --candidates N        the most files tried per record in each index step
                        (default 8; 0: none)|};
    positionals = ["archive"]; rest = false; options = [opt "--restore-sample" ~kind:`Int; opt "--reference-root"; opt "--index" ~append:true; opt "--candidates" ~kind:`Int] };
  { name = "status"; usage = "usage: dngc status [-h] archive";
    help = simple_help "usage: dngc status [-h] archive" ["archive"] 2;
    positionals = ["archive"]; rest = false; options = [] };
  { name = "ls"; usage = ls_usage; help = ls_usage ^ {|

positional arguments:
  archive

options:
  -h, --help            show this help message and exit
  --glob PATTERN        relative paths matching this pattern; repeatable
  --kind {identical,delta,full}
  --changed TAG         files whose metadata tag differs between the trees
  --trashed             records in the trash instead
  --print {path,source,reference}
                        what to print per record
  --json                one JSON object per record
  -0, --null            end each entry with NUL, for xargs -0|};
    positionals = ["archive"]; rest = false;
    options = [opt "--glob" ~append:true; opt "--kind" ~choices:kinds; opt "--changed"; opt "--trashed" ~nargs:0;
               opt "--print" ~choices:["path"; "source"; "reference"]; opt "--json" ~nargs:0;
               opt "--null" ~nargs:0 ~aliases:["-0"]] };
  { name = "drop"; usage = drop_usage; help = drop_usage ^ {|

positional arguments:
  archive

options:
  -h, --help      show this help message and exit
  --glob PATTERN  relative paths matching this pattern; repeatable
  --from FILE     relative paths, one per line; - for stdin
  -0, --null      --from entries end with NUL
  --yes           move them to the trash (default: only list them)
  --force         delete them instead of moving them to the trash|};
    positionals = ["archive"]; rest = false;
    options = [opt "--glob" ~append:true; opt "--from"; opt "--null" ~nargs:0 ~aliases:["-0"];
               opt "--yes" ~nargs:0; opt "--force" ~nargs:0] };
  { name = "trash"; usage = trash_usage; help = trash_usage ^ {|

positional arguments:
  archive
  {list,restore,empty,release}
  drop                  a drop id (see dngc trash ARCHIVE)

options:
  -h, --help            show this help message and exit
  --yes                 empty: delete (default: only list)|};
    positionals = ["archive"]; rest = true; options = [opt "--yes" ~nargs:0] };
  { name = "keep"; usage = keep_usage; help = keep_usage ^ {|

positional arguments:
  archive

options:
  -h, --help            show this help message and exit
  --glob PATTERN        relative paths matching this pattern; repeatable
  --from FILE           relative paths, one per line; - for stdin
  -0, --null            --from entries end with NUL
  --yes                 do it (default: only list)
  --reference-root DIR  the reference tree, when it has moved: references are
                        read below it
  --index FILE          a phindex index to find a moved, renamed or edited
                        reference in (repeatable)
  --candidates N        the most files tried per record in each index step
                        (default 8; 0: none)|};
    positionals = ["archive"]; rest = false;
    options = [opt "--glob" ~append:true; opt "--from"; opt "--null" ~nargs:0 ~aliases:["-0"]; opt "--yes" ~nargs:0;
               opt "--reference-root"; opt "--index" ~append:true; opt "--candidates" ~kind:`Int] };
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
  { name = "skeleton"; usage = skeleton_usage;
    help = skeleton_usage ^ {|

positional arguments:
  {build,info}
  archive

options:
  -h, --help         show this help message and exit
  --workers WORKERS
  --limit N          build at most N skeletons|};
    positionals = ["action"; "archive"]; rest = false;
    options = [opt "--workers" ~kind:`Int; opt "--limit" ~kind:`Int] };
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
    else match List.find_opt (fun o -> o.flag = name || List.mem name o.aliases) v.options with
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
    | a :: rest when option_like a || List.exists (fun o -> List.mem a o.aliases || o.flag = a) v.options ->
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
             let values = List.map (convert o) values in
             Hashtbl.replace opts o.flag
               (if o.append then Option.value (Hashtbl.find_opt opts o.flag) ~default:[] @ values else values);
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
          let where () = { reference_root = one "--reference-root"; index = Option.value (get "--index") ~default:[];
                           candidates = Option.map int_of_string (one "--candidates") } in
          match v.name, pos with
          | "run", [left; right; archive] ->
              cmd_run { left; right; archive; pairs = one "--pairs";
                        bytes = Option.value (one "--bytes") ~default:"delta";
                        metadata = Option.value (one "--metadata") ~default:"exiftool";
                        index = (match get "--index" with Some [l; r] -> Some (l, r) | _ -> None);
                        workers = (match one "--workers" with Some w -> int_of_string w | None -> default_workers ());
                        impl = Option.value (one "--impl") ~default:"ocaml";
                        max_mib = Option.map float_of_string (one "--max-mib");
                        trust_files = get "--trust-files" <> None }
          | "restore", [archive; rel; output] ->
              if one "--version" = None then refuse_dropped archive rel;
              cmd_restore ?version:(one "--version") ~where:(where ()) archive rel output
          | "versions", [archive] -> cmd_versions archive None
          | "versions", [archive; rel] -> cmd_versions archive (Some rel)
          | "versions", _ :: _ :: extra -> parser_error top_usage "dngc" ("unrecognized arguments: " ^ String.concat " " extra)
          | "index", [archive] -> cmd_index archive
          | "query", archive :: words -> cmd_query archive words
          | "verify", [archive] ->
              cmd_verify ~where:(where ()) archive (match one "--restore-sample" with Some n -> int_of_string n | None -> 0)
          | "status", [archive] -> cmd_status archive
          | "ls", [archive] ->
              cmd_ls archive { globs = Option.value (get "--glob") ~default:[]; kind = one "--kind"; changed = one "--changed";
                               trashed = get "--trashed" <> None; print = Option.value (one "--print") ~default:"path";
                               as_json = get "--json" <> None; nul = get "--null" <> None }
          | "drop", [archive] ->
              cmd_drop archive { sglobs = Option.value (get "--glob") ~default:[]; from = one "--from"; snul = get "--null" <> None }
                ~yes:(get "--yes" <> None) ~force:(get "--force" <> None)
          | "keep", [archive] ->
              cmd_keep ~where:(where ()) archive { sglobs = Option.value (get "--glob") ~default:[]; from = one "--from"; snul = get "--null" <> None }
                ~yes:(get "--yes" <> None)
          | "trash", archive :: more ->
              let choices = ["list"; "restore"; "empty"; "release"] in
              let action, id = match more with
                | [] -> "list", None
                | [a] -> a, None
                | [a; d] -> a, Some d
                | _ :: _ :: extra -> parser_error top_usage "dngc" ("unrecognized arguments: " ^ String.concat " " extra) in
              if not (List.mem action choices) then
                parser_error trash_usage "dngc trash"
                  (Printf.sprintf "argument action: invalid choice: %s (choose from %s)" (quote action)
                     (String.concat ", " (List.map quote choices)));
              cmd_trash archive action id ~yes:(get "--yes" <> None)
          | "skeleton", [action; archive] ->
              let choices = ["build"; "info"] in
              if not (List.mem action choices) then
                parser_error skeleton_usage "dngc skeleton"
                  (Printf.sprintf "argument action: invalid choice: %s (choose from %s)" (quote action)
                     (String.concat ", " (List.map quote choices)));
              let arc = archive_of archive in
              if action = "info" then Skeleton_section.info arc.root
              else Skeleton_section.build ?limit:(Option.map int_of_string (one "--limit"))
                  ~workers:(match one "--workers" with Some w -> int_of_string w | None -> 4) arc.root
          | "migrate", [archive] ->
              cmd_migrate archive ~dry_run:(get "--dry-run" <> None) ~report:(one "--report")
                ~undo:(get "--undo" <> None) ~no_verify:(get "--no-verify" <> None)
          | _ -> parser_error top_usage "dngc" "bad arguments"

let main argv =
  try dispatch argv with
  | Die n -> n
  | Sys.Break -> prerr_endline "dngc: interrupted"; 130
  | e -> flush stdout; Printf.eprintf "dngc: %s\n%!" (error_text e); 1

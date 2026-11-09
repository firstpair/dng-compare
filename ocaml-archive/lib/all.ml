(* dng-archive all: compare_dng.py --pairs-only and preserve_apo.py scan
   --delta --pairs in one pass (docs/native-all.md). Each pair is read from
   disk once into memory; its pixel comparison and the preservation of its
   left file use those bytes. The report and archive directories are the ones
   the Python commands write, so either side resumes or restores the other's.

   Process model: per batch, ExifTool runs over the batch's files while
   forked worker processes compare and preserve pairs; the parent alone writes
   the databases and the report. *)

open Compare

let usage_text =
  "usage: dng-archive all LEFT RIGHT --pairs FILE --report DIR [--archive DIR | --compare-only]\n\
  \                       [--left-index FILE --right-index FILE] [--workers N] [--batch-size N]\n\
  \                       [--exiftool-processes N] [--progress-secs S] [--rate-mib R] [--fresh]\n\
  \                       [--trust-pairs] [--skip-metadata] [--trust-files]"

(* argparse's parser.error: usage and message on stderr, status 2. *)
let usage_error msg =
  prerr_endline usage_text;
  prerr_endline ("dng-archive all: error: " ^ msg);
  raise (Py.System_exit 2)

type options = {
  left : string; right : string; pairs_file : string; report : string; archive : string option;
  left_index : string option; right_index : string option;
  workers : int; batch_size : int; exiftool_processes : int; progress_secs : float; rate_mib : float; fresh : bool;
  trust_pairs : bool; skip_metadata : bool; trust_files : bool;
}

let parse_args args =
  let valued = ["--pairs"; "--report"; "--archive"; "--left-index"; "--right-index"; "--workers"; "--batch-size";
                "--exiftool-processes"; "--progress-secs"; "--rate-mib"] in
  let rec go pos opts = function
    | [] -> List.rev pos, opts
    | f :: rest when String.contains f '=' && String.starts_with ~prefix:"--" f ->
        let i = String.index f '=' in
        go pos opts (String.sub f 0 i :: String.sub f (i + 1) (String.length f - i - 1) :: rest)
    | f :: v :: rest when List.mem f valued -> go pos ((f, v) :: opts) rest
    | f :: [] when List.mem f valued -> usage_error (Printf.sprintf "argument %s: expected one argument" f)
    | "--fresh" :: rest -> go pos (("--fresh", "") :: opts) rest
    | "--compare-only" :: rest -> go pos (("--compare-only", "") :: opts) rest
    | "--trust-pairs" :: rest -> go pos (("--trust-pairs", "") :: opts) rest
    | "--skip-metadata" :: rest -> go pos (("--skip-metadata", "") :: opts) rest
    | "--trust-files" :: rest -> go pos (("--trust-files", "") :: opts) rest
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
  let int k d = num int_of_string_opt "int" k d and float k d = num float_of_string_opt "float" k d in
  match pos with
  | [left; right] ->
      (match List.filter (fun k -> opt k = None) ["--pairs"; "--report"] with
       | [] -> ()
       | missing -> usage_error ("the following arguments are required: " ^ String.concat ", " missing));
      let required k = Option.get (opt k) in
      let compare_only = List.mem_assoc "--compare-only" opts in
      if compare_only && opt "--archive" <> None then usage_error "--archive and --compare-only exclude each other";
      (* Preservation is the default: without --archive the archive goes in REPORT/archive. *)
      let archive = if compare_only then None
        else Some (Option.value (opt "--archive") ~default:(Filename.concat (required "--report") "archive")) in
      { left; right; pairs_file = required "--pairs"; report = required "--report"; archive;
        left_index = opt "--left-index"; right_index = opt "--right-index";
        workers = int "--workers" 2; batch_size = int "--batch-size" 32;
        exiftool_processes = int "--exiftool-processes" 1; progress_secs = float "--progress-secs" 0.;
        rate_mib = float "--rate-mib" 200.; fresh = List.mem_assoc "--fresh" opts;
        trust_pairs = List.mem_assoc "--trust-pairs" opts; skip_metadata = List.mem_assoc "--skip-metadata" opts;
        trust_files = List.mem_assoc "--trust-files" opts }
  | _ -> usage_error "the following arguments are required: left, right"

(* ---- Pair listing, as compare_dng.py's read_pairs / listed_inventory / match ---- *)

let read_pairs path =
  let text = In_channel.with_open_bin path In_channel.input_all in
  let lines = String.split_on_char '\n' text in
  (* Drop the empty piece after a final newline (linear: pair files have ~500k lines). *)
  let lines = match List.rev lines with "" :: rest -> List.rev rest | _ -> lines in
  List.concat (List.mapi (fun i line ->
    let line = if String.ends_with ~suffix:"\r" line then String.sub line 0 (String.length line - 1) else line in
    let stripped = Py.strip line in
    if stripped = "" || String.starts_with ~prefix:"#" stripped then []
    else match String.split_on_char '\t' line with
      | [a; b] when a <> "" && b <> "" -> [(a, b)]
      | _ -> failwith (Printf.sprintf "%s:%d: expected LEFT<TAB>RIGHT" path (i + 1))) lines)

let join root name = if Filename.is_relative name then Filename.concat root name else name

let real_dir_cache = Hashtbl.create 64
let real_dir d = match Hashtbl.find_opt real_dir_cache d with
  | Some r -> r
  | None -> let r = Preserve.resolve d in Hashtbl.add real_dir_cache d r; r

(* os.path.join(real_dir(dirname(j)), basename(j)) *)
let listed_path root name =
  let j = join root name in
  Filename.concat (real_dir (Filename.dirname j)) (Filename.basename j)

let is_dng_file path =
  String.ends_with ~suffix:".dng" (String.lowercase_ascii path)
  && (match Native.stat path with s -> s.kind = `Reg | exception Unix.Unix_error _ -> false)

(* Basename -> paths, in first-listed order, for each side. *)
type side = { mutable names : string list; paths : (string, string list) Hashtbl.t }

let add_side side path =
  let name = Filename.basename path in
  let prior = Option.value (Hashtbl.find_opt side.paths name) ~default:[] in
  if prior = [] then side.names <- name :: side.names;
  if not (List.mem path prior) then Hashtbl.replace side.paths name (prior @ [path])

let side_paths side = List.concat_map (fun n -> Hashtbl.find side.paths n) (List.rev side.names)

(* ---- Per-pair work, in a worker process ---- *)

type job = {
  index : int; pair_l : string; pair_r : string;
  compare : bool;                      (* not reused from the cache nor settled from the indexes *)
  preserve : Preserve.item option;     (* the left file's preservation, attached to this pair *)
}

type done_ = {
  job_index : int;
  result : (string * Json.t) list option;
  preserved : (string * string option * Preserve.outcome * float) option;
}

let run_job (session : Preserve.session option) job =
  let data = lazy (read_file job.pair_l, read_file job.pair_r) in
  (* The preserved file's identities and attributes come before the read. *)
  let pre = match session, job.preserve with
    | Some s, Some it ->
        let base = Preserve.base_of s it in
        Some (base, try Ok (Preserve.identity it.path, Preserve.attrs it.path, Option.map Preserve.identity base)
                    with e -> Error e)
    | _ -> None in
  let result = if job.compare then Some (Odict.to_list (compare_pixels job.pair_l job.pair_r ~data)) else None in
  let preserved = match session, job.preserve, pre with
    | Some s, Some it, Some (base, ids) ->
        let b = Preserve.worker_budget s in
        let read_before = b.bytes in
        let preloaded base' =
          if base' <> base then failwith "Reference changed during preservation";
          match ids with
          | Error e -> raise e
          | Ok (before, meta, prior) ->
              { Preserve.pre_before = before; pre_meta = meta; pre_prior = prior;
                contents = lazy (let a, r = Lazy.force data in a, Option.map (fun _ -> r) base) } in
        let outcome = Preserve.process ~preloaded s b it in
        Some (it.rel, it.saved, outcome, b.bytes -. read_before)
    | _ -> None in
  { job_index = job.index; result; preserved }

(* ---- The command ---- *)

let interrupted () =
  prerr_endline "Interrupted; rerun the same command to resume from the cache.";
  raise (Py.System_exit 130)

let set_field k v = function
  | Json.Obj l when List.mem_assoc k l -> Json.Obj (List.map (fun (k', v') -> k', if k' = k then v else v') l)
  | Json.Obj l -> Json.Obj (l @ [(k, v)])
  | v -> v

(* The command for an options record, as dngc calls it: the exit status
   compare_dng.py would give. Failures other than usage errors and
   interruption propagate as exceptions. *)
let rec run_options o =
  try run_checked o with Py.System_exit n -> n

and run_checked o =
  Sys.catch_break true;
  (* The byte section by the shared rule (Preserve.byte_section). *)
  let o = { o with archive = Option.map Preserve.byte_section o.archive } in
  if o.workers < 1 || o.batch_size < 1 then usage_error "workers, batch-size, limit and sample must be positive";
  if o.exiftool_processes < 1 then usage_error "exiftool-processes must be positive";
  if o.progress_secs < 0. then usage_error "progress-secs must not be negative";
  if (o.left_index = None) <> (o.right_index = None) then usage_error "--left-index and --right-index go together";
  (match o.left_index, o.right_index with
   | Some a, Some b when not (Preserve.is_file a && Preserve.is_file b) -> usage_error "phindex index not found"
   | _ -> ());
  if not (o.rate_mib > 0.) then usage_error "Rate must be positive";
  let on_path exe = List.exists (fun d -> Sys.file_exists (Filename.concat d exe))
      (String.split_on_char ':' (Option.value (Sys.getenv_opt "PATH") ~default:"")) in
  if not o.skip_metadata && not (on_path "exiftool") then usage_error "ExifTool is required: brew install exiftool";
  let roots = List.map (fun p -> Preserve.resolve (Preserve.absolute p)) [o.left; o.right] in
  if not (List.for_all (fun p -> match Native.stat p with s -> s.kind = `Dir | exception Unix.Unix_error _ -> false) roots) then
    usage_error "Both roots must be readable directories";
  let left_root = List.nth roots 0 and right_root = List.nth roots 1 in
  Native.mkdir_p o.report;
  let lock = Unix.openfile (Filename.concat o.report ".lock") [Unix.O_WRONLY; Unix.O_CREAT; Unix.O_TRUNC; Unix.O_CLOEXEC] 0o644 in
  if not (Native.flock_exclusive lock) then usage_error "Another comparison is already using this output directory";
  let started = Unix.gettimeofday () in
  let listed = try read_pairs o.pairs_file with Failure m -> usage_error m | Sys_error m -> usage_error m in
  if listed = [] then usage_error "--pairs-only needs --pair or --pairs";
  Printf.printf "dng-archive all %s (%s)\n%s the %s listed pairs...\n%!" Build_info.commit
    (if Sys.ocaml_version = "" then "" else "OCaml " ^ Sys.ocaml_version)
    (if o.trust_pairs then "Trusting" else "Checking") (Py.thousands (List.length listed));
  let left = { names = []; paths = Hashtbl.create 64 } and right = { names = []; paths = Hashtbl.create 64 } in
  let resolved = List.map (fun (a, b) ->
    let pa = listed_path left_root a and pb = listed_path right_root b in
    (* --trust-pairs: the list was checked before; a file gone since fails when it is read. *)
    let is_dng_file p = if o.trust_pairs then String.ends_with ~suffix:".dng" (String.lowercase_ascii p) else is_dng_file p in
    let check p name root =
      if not (is_dng_file p) then begin
        let dng = String.ends_with ~suffix:".dng" (String.lowercase_ascii p) in
        let gone = match Native.stat ~follow:false p with _ -> false | exception Unix.Unix_error _ -> true in
        (* A listed file that is gone (a wiped shuttle) is named as missing. *)
        usage_error (Printf.sprintf "%s: %s under %s" name (if dng && gone then "missing" else "not a DNG") root)
      end in
    check pa a left_root; check pb b right_root;
    add_side left pa; add_side right pb;
    pa, pb) listed in
  (* explicit_pairs: each listed left with its listed rights, in order. *)
  let explicit = Hashtbl.create 64 in
  List.iter (fun (a, b) ->
    let prior = Option.value (Hashtbl.find_opt explicit a) ~default:[] in
    if not (List.mem b prior) then Hashtbl.replace explicit a (prior @ [b])) resolved;
  (* match: by sorted basename, each left with its explicit targets. *)
  let pairs = Array.of_list (List.concat_map (fun name ->
    List.concat_map (fun s -> List.map (fun t -> s, t) (Hashtbl.find explicit s)) (Hashtbl.find left.paths name))
    (List.sort compare left.names)) in
  let total = Array.length pairs in
  let left_files = List.length (side_paths left) in
  let context = ref [
    ("version", Json.int version); ("roots", Json.Arr (List.map jstr roots)); ("matched_pairs", Json.int total);
    ("explicit_pairs", Json.int (Hashtbl.fold (fun _ l n -> n + List.length l) explicit 0));
    ("pairs_only", Json.Bool true); ("left_files", Json.int left_files);
    ("right_files", Json.int (List.length (side_paths right)));
    ("duplicate_names", Json.int 0); ("left_only_names", Json.int 0);
    ("direction", jstr "left must be contained in right; extra right files are ignored")] in
  Out_channel.with_open_bin (Filename.concat o.report "unmatched.json") (fun oc ->
    output_string oc (Json.dumps (Json.Obj [("duplicate_candidates", Json.Arr []); ("left_only", Json.Obj [])])));
  context := !context @ [("selected_pairs", Json.int total)];
  Printf.printf "%s candidate pairs; processing %s; duplicate names: 0\n%!" (Py.thousands total) (Py.thousands total);
  let indexes = match o.left_index, o.right_index with
    | Some a, Some b -> Some (Phindex.open_ a, Phindex.open_ b) | _ -> None in
  (* Preservation of each listed left, against its last listed right, as
     preserve_apo.py scan --delta --pairs does with resolved paths. *)
  let session_and_lock = match o.archive with
    | None -> None
    | Some archive ->
        Preserve.check_options ~rate:o.rate_mib ~workers:o.workers;
        Printf.printf "Byte section: %s\n%!" archive;
        let archive = Preserve.validate_output left_root right_root archive in
        (* A listed source that is gone (--trust-pairs over a wiped shuttle) is
           reported missing by the preservation, as far as it resolves. *)
        (* Through the directory cache, as the pair check resolved them: a
           realpath per file costs a lookup of every component, and on a slow
           volume (120 a second on a USB-bridged RAID) 713,000 pairs took hours.
           Listed files are not symlinks (align and the walk leave them out). *)
        let real p = Filename.concat (real_dir (Filename.dirname p)) (Filename.basename p) in
        let order = ref [] and reference = Hashtbl.create 64 in
        List.iter (fun (a, b) ->
          let a = real a and b = real b in
          if not (Hashtbl.mem reference a) then order := a :: !order;
          Hashtbl.replace reference a b) resolved;
        let ppairs = List.rev_map (fun a -> a, Some (Hashtbl.find reference a)) !order in
        Native.mkdir_p archive;
        let fd = Unix.openfile (Filename.concat archive ".byte-preservation.lock") [Unix.O_WRONLY; Unix.O_CREAT; Unix.O_APPEND; Unix.O_CLOEXEC] 0o644 in
        if not (Native.flock_exclusive fd) then failwith "Another process owns this archive phase: .byte-preservation.lock";
        let indexes = Option.map (fun (a, b) -> { Preserve.source_index = a; reference_index = b }) indexes in
        let s = Preserve.open_session ?indexes ~source:left_root ~reference:right_root ~archive ~rate:o.rate_mib
            ~full_copy:false ~delta_mode:true ~workers:o.workers ~pairs:ppairs () in
        Some (s, fd, reference) in
  let session = Option.map (fun (s, _, _) -> s) session_and_lock in
  (* Identical files are recorded first, in bulk; the pairs carry the rest. *)
  let handled = match session with Some s -> Preserve.bulk_identical s | None -> Hashtbl.create 1 in
  (* Attach each preserved file to the first matched pair holding it and its reference. *)
  let attached = Array.make total None in
  (match session_and_lock with
   | Some (s, _, _) ->
       let by_key = Hashtbl.create 64 in
       Array.iter (fun (it : Preserve.item) ->
         if not (Hashtbl.mem handled it.rel) then
           Hashtbl.replace by_key (it.path, Option.value it.reference_of ~default:"") it) s.items;
       Array.iteri (fun i (l, r) ->
         let real p = Filename.concat (real_dir (Filename.dirname p)) (Filename.basename p) in
         let key = (real l, real r) in
         match Hashtbl.find_opt by_key key with
         | Some it -> attached.(i) <- Some it; Hashtbl.remove by_key key
         | None -> ()) pairs
   | None -> ());
  let unattached = match session with
    | Some s ->
        let held = Hashtbl.create 64 in
        Array.iter (Option.iter (fun (it : Preserve.item) -> Hashtbl.replace held it.rel ())) attached;
        List.filter (fun (it : Preserve.item) -> not (Hashtbl.mem held it.rel) && not (Hashtbl.mem handled it.rel))
          (Array.to_list s.items)
    | None -> [] in
  let db = Native.open_sqlite (Filename.concat o.report "cache.sqlite3") in
  Index.exec db "CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, stamps TEXT, result TEXT)";
  let summary = Compare.summary () in
  let reused = ref 0 and last_progress = ref 0. and last_summary = ref neg_infinity in
  let comparing = Unix.gettimeofday () in
  (* (time, pairs done) at recent progress lines: the rate behind the ETA. *)
  let window = ref [] in
  let compare_only = o.archive = None in
  let settle (l, r) stamps =
    match indexes, stamps with
    | Some (li, ri), Json.Arr [sl; sr] ->
        let row ix p st = match st with
          | Json.Arr [_; _; Json.Num size; Json.Num mtime; _] ->
              Phindex.row ix p ~size:(Int64.of_string size) ~mtime_ns:(Int64.of_string mtime)
          | _ -> None in
        (match row li l sl, row ri r sr with
         | Some a, Some b ->
             let base = [("left", jstr l); ("right", jstr r); ("stamps", stamps)] in
             if a.sha256 = b.sha256 then
               let settled = obj [("status", jstr "equal"); ("method", jstr "phindex_sha256")] in
               Some (base @ [("raw", settled); ("auxiliary", settled)], "phindex_sha256")
             else if compare_only && a.dng_blocks <> None && a.dng_blocks = b.dng_blocks then
               Some (base @ [("raw", obj [("status", jstr "equal"); ("method", jstr "phindex_dng_blocks")]);
                             ("auxiliary", obj [("status", jstr "not_compared")])], "phindex_dng_blocks")
             else None
         | _ -> None)
    | _ -> None in
  (* A cached result is reused when both files are unchanged. The device
     number is left out of that test: macOS numbers a volume when it is
     mounted, so a remount would otherwise discard every cached result. *)
  let same_stamps a b =
    let tails j = match Json.parse j with
      | Json.Arr sides -> Some (List.map (function Json.Arr (_ :: rest) -> rest | _ -> []) sides)
      | _ -> None
      | exception _ -> None in
    match tails a, tails b with Some x, Some y -> x = y | _ -> false in
  (* A result cached by a --skip-metadata run has no metadata comparison, so a
     run that compares metadata does not reuse it. *)
  let reusable res = o.skip_metadata
    || (match Json.member "metadata_status" (Json.parse res) with Some (Json.Str "skipped") -> false | _ -> true) in
  let out = open_out_bin (Filename.concat o.report "pairs.jsonl") in
  (try
    let start = ref 0 in
    while !start < total do
      let batch = Array.sub pairs !start (min o.batch_size (total - !start)) in
      let first = !start in
      start := !start + o.batch_size;
      let results = Array.make (Array.length batch) Json.Null in
      let pending = ref [] in
      Index.exec db "BEGIN";
      Array.iteri (fun k (l, r) ->
        let key = canonical (Json.Arr [Json.int version; jstr l; jstr r]) in
        let row = if o.fresh then [] else Index.query db "SELECT stamps,result FROM results WHERE key=?" [Index.text key] in
        let current = try Some (canonical (Json.Arr [stamp l; stamp r])) with Unix.Unix_error _ -> None in
        match row with
        | [ [| Tursolite.Text st; Tursolite.Text res |] ]
          when (match current with Some c -> same_stamps st c | None -> false) && reusable res ->
            results.(k) <- Json.parse res; incr reused
        | _ -> pending := (k, current) :: !pending) batch;
      let pending = List.rev !pending in
      let settled = Hashtbl.create 8 in
      List.iter (fun (k, current) ->
        match current with
        | Some c ->
            (match settle batch.(k) (Json.parse c) with
             | Some (r, method_) -> Hashtbl.replace settled k r; Counter.add summary.counts method_ 1
             | None -> ())
        | None -> ()) pending;
      let is_pending k = List.mem_assoc k pending in
      let jobs = List.filter_map (fun k ->
        let compare = is_pending k && not (Hashtbl.mem settled k) in
        let preserve = attached.(first + k) in
        if compare || preserve <> None then
          Some { index = k; pair_l = fst batch.(k); pair_r = snd batch.(k); compare; preserve }
        else None) (List.init (Array.length batch) Fun.id) in
      let running = if pending = [] || o.skip_metadata then [] else
          start_metadata (List.concat_map (fun (k, _) -> [fst batch.(k); snd batch.(k)]) pending) o.exiftool_processes in
      let compared = Hashtbl.create 8 in
      Preserve.pool ~workers:o.workers (Array.of_list jobs) (run_job session) (function
        | None -> Option.iter Preserve.tick session
        | Some d ->
            Option.iter (fun r -> Hashtbl.replace compared d.job_index r) d.result;
            (match session, d.preserved with Some s, Some p -> Preserve.record s p | _ -> ()));
      let meta, metadata_error = match running with
        | [] -> Hashtbl.create 1, None
        | _ -> (match finish_metadata running with Ok t -> t, None | Error e -> Hashtbl.create 1, Some e) in
      List.iter (fun (k, current) ->
        let l, r = batch.(k) in
        let fields = match Hashtbl.find_opt settled k with
          | Some f -> f
          | None -> (match Hashtbl.find_opt compared k with
              | Some f -> f
              | None -> [("left", jstr l); ("right", jstr r);
                         ("raw", obj [("status", jstr "unverified"); ("error", jstr "worker failed")]);
                         ("error", jstr "worker failed")]) in
        let result = Odict.create () in
        List.iter (fun (k, v) -> Odict.set result k v) fields;
        let lookup p = match Hashtbl.find_opt meta p with
          | Some m -> m
          | None -> Meta_error (Option.value metadata_error ~default:"ExifTool returned no record") in
        (match lookup l, lookup r with
         | _ when o.skip_metadata -> Odict.set result "metadata_status" (jstr "skipped")
         | Tags (ta, wa), Tags (tb, wb) ->
             Odict.set result "metadata_status" (jstr "ok");
             Odict.set result "differences" (compare_metadata ta tb);
             Odict.set result "metadata_warnings" (Json.Arr (List.map jstr (wa @ wb)))
         | a, b ->
             let e = function Meta_error e -> jstr e | Tags _ -> Json.Null in
             Odict.set result "metadata_status" (jstr "error");
             Odict.set result "metadata_error" (Json.Arr [e a; e b]));
        (* A pair compare_pixels could not read keeps its own error; this
           check is for files that changed under a completed comparison. *)
        (match Odict.get result "stamps" with
         | Some stamps ->
             (match (let now = Json.Arr [stamp l; stamp r] in
                     if now <> stamps || Some (canonical stamps) <> current then
                       Some "File changed or was unreadable during comparison" else None) with
              | exception Unix.Unix_error (e, _, p) -> Some (Py.oserror e p)
              | m -> m)
             |> Option.iter (fun m ->
               Odict.set result "error" (jstr m);
               Odict.set result "raw" (set_field "status" (jstr "unverified") (Option.get (Odict.get result "raw")));
               Odict.set result "metadata_status" (jstr "error"))
         | None -> ());
        let status g = Option.bind (Odict.get result g) (fun v -> match Json.member "status" v with Some (Json.Str s) -> Some s | _ -> None) in
        let no_error = match Odict.get result "error" with None | Some (Json.Str "") | Some Json.Null -> true | _ -> false in
        let value = obj (Odict.to_list result) in
        let metadata_done = match Odict.get result "metadata_status" with
          | Some (Json.Str "ok") -> true
          | Some (Json.Str "skipped") -> o.skip_metadata
          | _ -> false in
        if no_error && metadata_done
           && status "raw" <> Some "unverified" && status "auxiliary" <> Some "unverified" then
          Index.run db "INSERT OR REPLACE INTO results VALUES (?,?,?)"
            [Index.text (canonical (Json.Arr [Json.int version; jstr l; jstr r]));
             Index.text (canonical (Option.get (Odict.get result "stamps"))); Index.text (canonical value)];
        results.(k) <- value) pending;
      Array.iter (fun result -> output_string out (canonical result ^ "\n"); Compare.add summary result) results;
      flush out;
      Index.exec db "COMMIT";
      let elapsed = Unix.gettimeofday () -. started in
      context := List.remove_assoc "cached_pairs_reused" !context @ [("cached_pairs_reused", Json.int !reused)];
      let done_ = Counter.get summary.counts "pairs_reported" in
      (* The running summary at most every 30 s: it aggregates every metadata
         difference with examples, megabytes on a large run, and written after
         each batch it made the run slower the further it went. The final one
         is written below, after the last pair. *)
      if done_ >= total || elapsed -. !last_summary >= 30. then begin
        last_summary := elapsed;
        Compare.write summary (Filename.concat o.report "summary.json") !context ~complete:false ~elapsed
      end;
      if done_ >= total || elapsed -. !last_progress >= o.progress_secs then begin
        last_progress := elapsed;
        (* Every pair counts, cached, settled or compared alike, over the last
           five progress intervals: the ETA follows the current mix instead of
           assuming that each remaining pair must be read. *)
        let now = Unix.gettimeofday () in
        window := List.filteri (fun i _ -> i < 5) ((now, done_) :: !window);
        let t0, d0 = match List.rev !window with
          | [_] | [] -> comparing, 0
          | oldest :: _ -> oldest in
        let rate = if now > t0 then float (done_ - d0) /. (now -. t0) else 0. in
        let eta = if rate > 0. then float (total - done_) /. rate else 0. in
        let c k = Py.thousands (Counter.get summary.counts k) in
        Printf.printf "%s/%s | raw equal=%s different=%s unverified=%s | %.1fs | cached=%s%s\n%!"
          (Py.thousands done_) (Py.thousands total) (c "raw_equal") (c "raw_different") (c "raw_unverified")
          elapsed (Py.thousands !reused)
          (if o.progress_secs = 0. then ""
           else if rate > 0. then Printf.sprintf " | %.1f pairs/s, ETA %dh%02dm" rate
               (int_of_float (Float.floor (eta /. 3600.))) (int_of_float (Float.floor (Float.rem eta 3600. /. 60.)))
           else " | reusing saved results")
      end
    done
  with Sys.Break -> close_out_noerr out; Native.close_db db; interrupted ());
  close_out out;
  Native.close_db db;
  Compare.write summary (Filename.concat o.report "summary.json") !context ~complete:true
    ~elapsed:(Unix.gettimeofday () -. started);
  let uncovered = List.filter (fun p ->
    match Hashtbl.find_opt summary.coverage p with Some c -> not c.(0) | None -> true) (side_paths left) in
  Out_channel.with_open_bin (Filename.concat o.report "not-confirmed.json") (fun oc ->
    output_string oc (Json.dumps_indent (Json.Arr (List.map jstr uncovered)) ^ "\n"));
  Printf.printf "Raw pixels preserved for %s/%s left files.\n%!"
    (Py.thousands (left_files - List.length uncovered)) (Py.thousands left_files);
  let preserved = match session_and_lock with
    | None -> true
    | Some (s, fd, _) ->
        let ok = Fun.protect ~finally:(fun () -> Native.close_db s.db; Unix.close fd) (fun () ->
          (* Files no matched pair carried, if any: preserved from disk. *)
          List.iter (fun it ->
            let b = Preserve.worker_budget s in
            let before = b.bytes in
            Preserve.record s (it.Preserve.rel, it.saved, Preserve.process s b it, b.bytes -. before)) unattached;
          Preserve.finish ~trust_files:o.trust_files s) in
        if not ok then prerr_endline "Preservation incomplete; inspect errors and rerun before deleting source";
        ok in
  Unix.close lock;
  if uncovered <> [] || not preserved then 1 else 0

(* dng-archive all ARGS. *)
let run args = try run_options (parse_args args) with Py.System_exit n -> n

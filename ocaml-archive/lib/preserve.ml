(* Lossless preservation of listed source files, as preserve_apo.py does it
   with --pairs: each source is compared with its reference; an identical file
   is recorded by SHA-256 only; a differing one is kept as a standard VCDIFF
   delta (xdelta3, RFC 3284) against its reference, decoded again to the exact
   source SHA-256 before it counts, or as a full original copy where there is no
   reference. Records go to recovery.sqlite3 in preserve_apo's JSON form, so
   either tool restores what the other preserved. Neither the source nor the
   reference tree is written. *)

open Native

let identity p = let s = stat p in Json.Arr [Json.int64 s.ino; Json.int64 s.size; Json.int64 s.mtime_ns; Json.int64 s.ctime_ns]

let xattrs p = List.map (fun n -> n, Json.Str (base64_encode (getxattr p n))) (listxattr p)

let attrs p =
  let s = stat ~follow:false p in
  Json.Obj [("mode", Json.int s.mode); ("uid", Json.int s.uid); ("gid", Json.int s.gid);
            ("mtime_ns", Json.int64 s.mtime_ns); ("atime_ns", Json.int64 s.atime_ns);
            ("flags", Json.int64 s.flags); ("xattrs", Json.Obj (xattrs p))]

let is_file p = match stat p with s -> s.kind = `Reg | exception Unix.Unix_error _ -> false

(* The archive's own byte budget: [rate] MiB/s, like preserve_apo's Budget. *)
type budget = { rate : float; start : float; mutable bytes : float }
let budget rate = { rate = rate *. 1048576.; start = Unix.gettimeofday (); bytes = 0. }
let charge b n =
  b.bytes <- b.bytes +. float n;
  let delay = b.bytes /. b.rate -. (Unix.gettimeofday () -. b.start) in
  if delay > 0. then Unix.sleepf delay

let atomic_json path v =
  let temp = path ^ ".tmp" in
  Out_channel.with_open_bin temp (fun oc ->
    output_string oc (Json.dumps_indent v); flush oc; Unix.fsync (Unix.descr_of_out_channel oc));
  Sys.rename temp path

(* Path.resolve(): the real path of the longest existing prefix. *)
let rec resolve p =
  match Unix.realpath p with
  | r -> r
  | exception Unix.Unix_error _ ->
      let parent = Filename.dirname p in
      if parent = p then p else Filename.concat (resolve parent) (Filename.basename p)

let absolute p = if Filename.is_relative p then Filename.concat (Sys.getcwd ()) p else p

let relative_to root p =
  if p = root then "."
  else if String.starts_with ~prefix:(root ^ "/") p then String.sub p (String.length root + 1) (String.length p - String.length root - 1)
  else failwith (p ^ " is not under " ^ root)

let inside root p = p = root || String.starts_with ~prefix:(root ^ "/") p

(* The byte section of [archive], the rule every dng-compare tool shares
   (archive_layout.py, docs/dngc.md): the archive itself when it holds
   recovery.sqlite3 and no bytes/ (a legacy archive); the path itself when
   its last component is bytes (given directly, never nested); otherwise
   ARCHIVE/bytes, created for a new or empty archive. *)
let strip_slashes p =
  let n = ref (String.length p) in
  while !n > 1 && p.[!n - 1] = '/' do decr n done;
  String.sub p 0 !n

let byte_section archive =
  let a = strip_slashes archive in
  if Sys.file_exists (Filename.concat a "recovery.sqlite3") && not (Sys.file_exists (Filename.concat a "bytes")) then a
  else if Filename.basename a = "bytes" then a
  else Filename.concat a "bytes"

(* The archive's top, where metadata/, provenance.json and ARCHIVE.json stay:
   the parent of ARCHIVE/bytes given directly. *)
let archive_top archive =
  let a = strip_slashes archive in
  if Filename.basename a = "bytes" then Filename.dirname a else a

(* Reject an archive that could alter or consume either input root. *)
let validate_output source reference archive =
  let archive = resolve (absolute archive) in
  List.iter (fun root ->
    let root = Unix.realpath root in
    if (stat root).kind <> `Dir then failwith ("Input root is not a directory: " ^ root);
    if inside root archive || inside archive root then
      failwith ("Output must be outside and disjoint from input roots: " ^ archive)) [source; reference];
  archive

let with_archive_lock archive name f =
  mkdir_p archive;
  let fd = Unix.openfile (Filename.concat archive name) [Unix.O_WRONLY; Unix.O_CREAT; Unix.O_APPEND] 0o644 in
  Fun.protect ~finally:(fun () -> Unix.close fd) (fun () ->
    if not (flock_exclusive fd) then failwith ("Another process owns this archive phase: " ^ name);
    f ())

let xdelta args =
  let pid = Unix.create_process "xdelta3" (Array.of_list ("xdelta3" :: args)) Unix.stdin Unix.stdout Unix.stderr in
  pid

(* Decode [record]'s delta, hashing (and writing to [dst]) the result; checks
   the delta's own hash first and the decoded SHA-256 after. *)
let stream_delta ?dst ?budget record archive =
  let get k = Json.str (Option.get (Json.member k record)) in
  let delta = Filename.concat archive (get "delta") in
  if sha_file delta <> get "delta_sha256" then failwith "VCDIFF archive hash mismatch";
  let rd, wr = Unix.pipe ~cloexec:true () in
  let err_rd, err_wr = Unix.pipe ~cloexec:true () in
  let pid = Unix.create_process "xdelta3" [|"xdelta3"; "-d"; "-D"; "-R"; "-c"; "-s"; get "reference"; delta|]
      Unix.stdin wr err_wr in
  Unix.close wr; Unix.close err_wr;
  let ic = Unix.in_channel_of_descr rd and ec = Unix.in_channel_of_descr err_rd in
  let h = Fun.protect ~finally:(fun () -> close_in_noerr ic) (fun () ->
    sha_channel ic ~each:(fun buf n ->
      Option.iter (fun b -> charge b n) budget;
      Option.iter (fun oc -> Out_channel.output oc buf 0 n) dst)) in
  let stderr = In_channel.input_all ec in
  close_in_noerr ec;
  (match snd (Unix.waitpid [] pid) with
   | Unix.WEXITED 0 -> ()
   | _ -> failwith ("VCDIFF decode failed: " ^ stderr));
  if h <> get "source_sha256" then failwith "Reconstructed original SHA-256 mismatch";
  h

let changed_error = "File changed during comparison"

let size_of_ident = function Json.Arr [_; Json.Num s; _; _] -> Int64.of_string s | _ -> 0L
let mtime_of_ident = function Json.Arr [_; _; Json.Num s; _] -> Int64.of_string s | _ -> 0L

(* What the one-pass `all` command already holds of a pair: identities and
   attributes taken before the read, and both files' bytes, read once. *)
type preloaded = {
  pre_before : Json.t; pre_meta : Json.t; pre_prior : Json.t option;
  contents : (string * string option) Lazy.t;
}

(* Optional phindex indexes of the source and reference trees. *)
type indexes = { source_index : Phindex.t; reference_index : Phindex.t }

let indexed_sha256 index path ident =
  Phindex.sha256 index path ~size:(size_of_ident ident) ~mtime_ns:(mtime_of_ident ident)

(* An archived content's version: its source's modification time (UTC) and
   the start of its SHA-256, e.g. 20261029T171631Z-b755b147cbed, as
   preserve_apo.version_label. *)
let version_label ~mtime_ns ~sha256 =
  let t = Unix.gmtime (Int64.to_float (Int64.div mtime_ns 1_000_000_000L)) in
  Printf.sprintf "%04d%02d%02dT%02d%02d%02dZ-%s" (t.tm_year + 1900) (t.tm_mon + 1) t.tm_mday t.tm_hour t.tm_min t.tm_sec
    (String.sub sha256 0 (min 12 (String.length sha256)))

(* [versioned]: the relative path already has a record of other content, whose
   payload stays where it is; this content's payload goes to versions/LABEL/. *)
let preserve_file ?preloaded ?indexes ?(versioned = false) source base archive rel budget ~full_copy ~delta_mode =
  let before, meta, prior = match preloaded with
    | Some p -> p.pre_before, p.pre_meta, p.pre_prior
    | None -> let before = identity source in let meta = attrs source in before, meta, Option.map identity base in
  let size = size_of_ident before in
  (* preserve_apo's phindex shortcut: equal current SHA-256 rows, nothing read. *)
  let shortcut = match indexes, base, prior with
    | Some ix, Some b, Some pr when not full_copy ->
        (match indexed_sha256 ix.source_index source before, indexed_sha256 ix.reference_index b pr with
         | Some h, Some h' when h = h' -> Some (b, h)
         | _ -> None)
    | _ -> None in
  match shortcut with
  | Some (b, h) ->
      Json.Obj [
        ("relative_path", Json.Str rel); ("source", Json.Str source); ("reference", Json.Str b);
        ("source_identity", before); ("reference_identity", Option.get prior);
        ("source_attrs", meta); ("source_sha256", Json.Str h); ("reference_sha256", Json.Str h);
        ("original", Json.Null); ("whole_file_equal", Json.Bool true);
        ("preserved_bytes", Json.int 0); ("reconstruction_verified", Json.Bool true)]
  | None ->
  let source_hash, reference_hash, changed = match preloaded with
    | Some p ->
        (* From the bytes in memory: the same hashes and comparison, the same charge. *)
        let src, ref_ = Lazy.force p.contents in
        charge budget (String.length src + (match ref_ with Some r -> String.length r | None -> 0));
        let changed = match ref_ with Some r -> r <> src | None -> src <> "" in
        sha_string src, sha_string (Option.value ref_ ~default:""), changed
    | None ->
  (* One pass compares the two files and hashes both. *)
  let sh = sha_init () and bh = sha_init () in
  let changed = ref false in
  In_channel.with_open_bin source (fun sf ->
    let bf = Option.map open_in_bin base in
    Fun.protect ~finally:(fun () -> Option.iter close_in_noerr bf) (fun () ->
      let a = Bytes.create block and b = Bytes.create block in
      let rec fill ic off n = if off = n then off else
          let k = input ic b off (n - off) in if k = 0 then off else fill ic (off + k) n in
      let rec loop () =
        let n = input sf a 0 block in
        if n > 0 then begin
          let got = match bf with Some ic -> fill ic 0 n | None -> 0 in
          charge budget (n + got); sha_update sh a 0 n; sha_update bh b 0 got;
          if got <> n || Bytes.sub a 0 n <> Bytes.sub b 0 n then changed := true;
          loop ()
        end in
      loop ();
      Option.iter (fun ic ->
        let rec rest () = let n = input ic b 0 block in
          if n > 0 then (sha_update bh b 0 n; charge budget n; changed := true; rest ()) in
        rest ()) bf));
  sha_final sh, sha_final bh, !changed in
  let unchanged () = identity source = before && (match base, prior with Some b, Some p -> identity b = p | _ -> true) in
  if not (unchanged ()) then failwith changed_error;
  let prefix = if versioned then
      "versions/" ^ version_label ~mtime_ns:(mtime_of_ident before) ~sha256:source_hash ^ "/" else "" in
  let original =
    if (changed && not (base <> None && delta_mode)) || base = None || full_copy then begin
      let original_rel = prefix ^ "originals/" ^ rel in
      let dest = Filename.concat archive original_rel in
      mkdir_p (Filename.dirname dest);
      let temp = dest ^ ".partial" in
      let copy_hash = match preloaded with
        | Some p ->
            let src = fst (Lazy.force p.contents) in
            Out_channel.with_open_bin temp (fun oc ->
              output_string oc src; charge budget (2 * String.length src);
              flush oc; Unix.fsync (Unix.descr_of_out_channel oc));
            source_hash
        | None ->
            In_channel.with_open_bin source (fun ic ->
              Out_channel.with_open_bin temp (fun oc ->
                let h = sha_channel ic ~each:(fun buf n -> Out_channel.output oc buf 0 n; charge budget (2 * n)) in
                flush oc; Unix.fsync (Unix.descr_of_out_channel oc); h)) in
      if copy_hash <> source_hash then failwith "Source changed while copying";
      (match meta with
       | Json.Obj m ->
           (match List.assoc "xattrs" m with
            | Json.Obj x -> List.iter (fun (n, v) -> setxattr temp n (base64_decode (Json.str v))) x | _ -> ());
           let num k = match List.assoc k m with Json.Num s -> Int64.of_string s | _ -> 0L in
           Unix.chmod temp (Int64.to_int (num "mode"));
           utimens temp (num "atime_ns") (num "mtime_ns")
       | _ -> ());
      (* Independently reread the archive copy before marking success. *)
      if sha_file temp <> source_hash then failwith "Archive copy read-back failed";
      Sys.rename temp dest;
      Some original_rel
    end else None in
  let fields = ref [
    ("relative_path", Json.Str rel); ("source", Json.Str source);
    ("reference", Json.opt (fun b -> Json.Str b) base);
    ("source_identity", before); ("reference_identity", Json.opt Fun.id prior);
    ("source_attrs", meta); ("source_sha256", Json.Str source_hash);
    ("reference_sha256", if base <> None then Json.Str reference_hash else Json.Null);
    ("original", Json.opt (fun o -> Json.Str o) original);
    ("whole_file_equal", Json.Bool (base <> None && not changed));
    ("preserved_bytes", Json.int64 (if original <> None then size else 0L));
    ("reconstruction_verified", Json.Bool true)] in
  let set k v = if List.mem_assoc k !fields then fields := List.map (fun (k', v') -> k', if k' = k then v else v') !fields
    else fields := !fields @ [(k, v)] in
  (match base with
   | Some b when changed && delta_mode && not full_copy ->
       let delta_rel = prefix ^ "deltas/" ^ rel ^ ".vcdiff" in
       let dest = Filename.concat archive delta_rel in
       mkdir_p (Filename.dirname dest);
       let temp = dest ^ ".partial" in
       if Sys.file_exists temp then Sys.remove temp;
       let ref_size = match prior with Some p -> Int64.to_int (size_of_ident p) | None -> 0 in
       charge budget (Int64.to_int size + ref_size);
       let pid = xdelta ["-e"; "-1"; "-S"; "none"; "-n"; "-a"; "-D"; "-R"; "-A";
                         "-B"; "16777216"; "-W"; "8388608"; "-s"; b; source; temp] in
       (match snd (Unix.waitpid [] pid) with Unix.WEXITED 0 -> () | _ -> failwith "VCDIFF encode failed");
       fsync_path temp;
       Sys.rename temp dest;
       set "delta" (Json.Str delta_rel);
       set "delta_sha256" (Json.Str (sha_file dest));
       set "delta_format" (Json.Str "VCDIFF RFC 3284");
       set "preserved_bytes" (Json.int64 (stat dest).size);
       ignore (stream_delta ~budget (Json.Obj !fields) archive)
   | _ -> ());
  if not (unchanged ()) then failwith "File changed during preservation";
  (match attrs source, meta with
   | Json.Obj a, Json.Obj m when List.assoc "xattrs" a <> List.assoc "xattrs" m -> failwith "Source attributes changed"
   | _ -> ());
  Json.Obj !fields

type outcome = Kept of Json.t | Unchanged | Failed of string

let field k r = match Json.member k r with Some v -> v | None -> Json.Null
let num_field k r = match field k r with Json.Num s -> Int64.of_string s | _ -> 0L
let size_of_identity r k = size_of_ident (field k r)

(* A saved record still holds when both files are as recorded and its payload
   hashes as recorded. *)
let still_valid r archive ~full_copy p =
  let base = match field "reference" r with Json.Str b -> Some b | _ -> None in
  (try identity p = field "source_identity" r with Unix.Unix_error _ -> false)
  && (match base with None -> true | Some b -> (try identity b = field "reference_identity" r with Unix.Unix_error _ -> false))
  && (match field "original" r with
      | Json.Str o -> let o = Filename.concat archive o in is_file o && sha_file o = Json.str (field "source_sha256" r)
      | _ -> not full_copy)
  && (match field "delta" r with
      | Json.Str d -> let d = Filename.concat archive d in is_file d && sha_file d = Json.str (field "delta_sha256" r)
      | _ -> true)

let exec db sql = Index.exec db sql
let query = Index.query
let text = Index.text

(* Workers k, k+workers, ...; each a forked process. The parent alone writes
   the database. A child that fails outside [job] ends with status 1 and never
   returns into the parent's code. *)
let pool ~workers items job on_result =
  let workers = max 1 (min workers (Array.length items)) in
  let children = List.init workers (fun k ->
    let input, output = Unix.pipe ~cloexec:true () in
    match Unix.fork () with
    | 0 ->
        (try
           Unix.close input;
           let oc = Unix.out_channel_of_descr output in
           let i = ref k in
           while !i < Array.length items do
             Marshal.to_channel oc (job items.(!i)) []; flush oc; i := !i + workers
           done;
           close_out oc; Unix._exit 0
         with _ -> Unix._exit 1)
    | pid -> Unix.close output; pid, Unix.in_channel_of_descr input) in
  let pending = ref (List.map snd children) in
  while !pending <> [] do
    let ready = match Unix.select (List.map Unix.descr_of_in_channel !pending) [] [] 60. with
      | r, _, _ -> r
      | exception Unix.Unix_error (Unix.EINTR, _, _) -> [] in
    on_result None;
    pending := List.filter (fun ic ->
      if not (List.mem (Unix.descr_of_in_channel ic) ready) then true else
      match Marshal.from_channel ic with
      | r -> on_result (Some r); true
      | exception End_of_file -> close_in ic; false) !pending
  done;
  List.iter (fun (pid, _) -> ignore (Unix.waitpid [] pid)) children

let load_pairs path =
  In_channel.with_open_bin path In_channel.input_lines
  |> List.filter (fun l -> String.trim l <> "")
  |> List.map (fun l ->
      let r = Json.parse l in
      Json.str (field "source" r), (match field "reference" r with Json.Str s -> Some s | _ -> None))

(* Directories sort as Python sorts paths: by their components. *)
let path_compare a b = compare (String.split_on_char '/' a) (String.split_on_char '/' b)

(* One listed source: its path, reference, relative path and saved record. *)
type item = { path : string; reference_of : string option; rel : string; saved : string option; versioned : bool }

(* An open preservation run over listed pairs: the archive's database and
   inventory files, its totals and its summary. [scan] runs one by itself;
   the `all` command runs one alongside its comparison. *)
type session = {
  source : string; reference : string; archive : string; db : Native.db;
  full_copy : bool; delta_mode : bool; workers : int; rate : float;
  items : item array; files : string list; missing : string list;
  start : float; mutable bytes_read : float; mutable last : float;
  write_summary : float -> bool -> unit; account : Json.t -> int -> unit;
  indexes : indexes option;
}

let open_session ?indexes ~source ~reference ~archive ~rate ~full_copy ~delta_mode ~workers ~pairs () =
  mkdir_p archive;
  let db = open_sqlite (Filename.concat archive "recovery.sqlite3") in
  exec db "PRAGMA journal_mode=WAL";
  exec db "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, record TEXT NOT NULL)";
  exec db "CREATE TABLE IF NOT EXISTS errors (path TEXT PRIMARY KEY, error TEXT NOT NULL)";
  (* Earlier contents of a relative path: a record replaced by one of a later
     content is kept here under its version label, its payload where it was. *)
  exec db "CREATE TABLE IF NOT EXISTS versions (path TEXT NOT NULL, version TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(path, version))";
  (* A listed subset: each source with its own reference, both under their roots.
     A listed source that is gone (a wiped shuttle) is reported, not preserved;
     its earlier record, if any, stays as it is. *)
  let missing = ref [] in
  let listed = List.filter_map (fun (s, r) ->
    let p = absolute s in
    ignore (relative_to source p);
    Option.iter (fun r -> ignore (relative_to reference (absolute r))) r;
    match (stat ~follow:false p).kind with
    | `Reg -> Some (p, Option.map absolute r)
    | _ -> failwith ("Unsupported source entry: " ^ p)
    | exception Unix.Unix_error (Unix.ENOENT, _, _) -> missing := relative_to source p :: !missing; None) pairs in
  let missing = List.rev !missing in
  let files = List.map fst listed in
  let dirs = List.sort_uniq path_compare (List.map Filename.dirname files) in
  atomic_json (Filename.concat archive "directories.json")
    (Json.Arr (List.map (fun d -> Json.Obj [("path", Json.Str (relative_to source d)); ("attributes", attrs d)]) dirs));
  atomic_json (Filename.concat archive "inventory.json")
    (Json.Arr (List.map (fun p -> Json.Str (relative_to source p)) files));
  let total = List.length files and start = Unix.gettimeofday () in
  let stored = ref 0L and source_bytes = ref 0L and changed_files = ref 0 in
  let account r dir =
    stored := Int64.add !stored (Int64.mul (Int64.of_int dir) (num_field "preserved_bytes" r));
    source_bytes := Int64.add !source_bytes (Int64.mul (Int64.of_int dir) (size_of_identity r "source_identity"));
    if field "whole_file_equal" r <> Json.Bool true then changed_files := !changed_files + dir in
  let saved = Hashtbl.create 1024 in
  List.iter (fun row -> match row with
    | [| Tursolite.Text rel; Tursolite.Text t |] -> Hashtbl.replace saved rel t; account (Json.parse t) 1
    | _ -> ()) (query db "SELECT path,record FROM files" []);
  let count sql = match query db sql [] with [ [| Tursolite.Integer n |] ] -> Int64.to_int n | _ -> 0 in
  let summary bytes_read complete =
    let verified = count "SELECT count(*) FROM files" and errors = count "SELECT count(*) FROM errors" in
    let data = Json.Obj [
      ("source", Json.Str source); ("reference", Json.Str reference); ("total_files", Json.int total);
      ("verified_files", Json.int verified); ("errors", Json.int errors); ("complete", Json.Bool complete);
      ("preservation_workers", Json.int workers); ("safe_to_delete_source", Json.Bool false);
      ("byte_preservation_complete", Json.Bool (complete && verified = total && errors = 0));
      ("bytes_read_this_run", Json.float bytes_read);
      ("elapsed_seconds", Json.float (Unix.gettimeofday () -. start));
      ("matching", Json.Str "Listed pairs; every candidate is byte-verified.");
      ("note", Json.Str "Deletion readiness also requires complete metadata comparison, final identity validation, and inventory coverage. Recovery references require hash-identical reference files.");
      ("storage_mode", Json.Str (if full_copy then "independent_originals"
                                 else if delta_mode then "standard_vcdiff_and_sha256_references"
                                 else "originals_for_differing_files_and_hash_references_for_identical_files"));
      ("source_bytes_verified", Json.int64 !source_bytes); ("preserved_payload_bytes", Json.int64 !stored);
      ("payload_to_verified_original_ratio",
       if !source_bytes = 0L then Json.Null else Json.float (Int64.to_float !stored /. Int64.to_float !source_bytes));
      ("byte_different_files", Json.int !changed_files); ("missing_sources", Json.int (List.length missing))] in
    atomic_json (Filename.concat archive "summary.json") data;
    print_endline (Json.dumps data) in
  let versioned = Hashtbl.create 64 in
  List.iter (function [| Tursolite.Text rel |] -> Hashtbl.replace versioned rel () | _ -> ())
    (query db "SELECT DISTINCT path FROM versions" []);
  let items = Array.of_list (List.map (fun (p, r) ->
    let rel = relative_to source p in
    let s = Hashtbl.find_opt saved rel in
    Hashtbl.remove saved rel; (* later records replace these *)
    { path = p; reference_of = r; rel; saved = s; versioned = s <> None || Hashtbl.mem versioned rel }) listed) in
  List.iter (fun rel -> Printf.printf "MISSING %s\n" rel) missing;
  let session = { source; reference; archive; db; full_copy; delta_mode; workers; rate; items; files; missing;
                  start; bytes_read = 0.; last = Unix.gettimeofday (); write_summary = summary; account; indexes } in
  summary 0. false;
  session

let summary s complete = s.write_summary s.bytes_read complete

(* A worker process's byte budget: its share of the rate over the whole run,
   counting what the run had read when the process was forked. One per process. *)
let process_budget = ref None
let worker_budget s =
  match !process_budget with
  | Some (pid, b) when pid = Unix.getpid () -> b
  | _ ->
      let b : budget = { rate = s.rate /. float s.workers *. 1048576.; start = s.start; bytes = s.bytes_read /. float s.workers } in
      process_budget := Some (Unix.getpid (), b); b

let error_message = function
  | Failure e | Sys_error e -> e
  | Unix.Unix_error (e, f, a) -> Printf.sprintf "%s: %s %s" (Unix.error_message e) f a
  | Json.Bad e -> e
  | e -> Printexc.to_string e

(* The base a listed source is preserved against: its listed reference, else
   the same relative path under the reference root, if that is a file. *)
let base_of s it =
  let base = match it.reference_of with Some r -> r | None -> Filename.concat s.reference it.rel in
  if is_file base then Some base else None

(* preserve_apo's process(): None when the saved record still holds. *)
let process ?preloaded s budget it =
  try
    match it.saved with
    | Some t when still_valid (Json.parse t) s.archive ~full_copy:s.full_copy it.path -> Unchanged
    | _ ->
        let base = base_of s it in
        let preloaded = Option.map (fun f -> f base) preloaded in
        let record = preserve_file ?preloaded ?indexes:s.indexes ~versioned:it.versioned it.path base s.archive it.rel budget
            ~full_copy:s.full_copy ~delta_mode:s.delta_mode in
        let ref_rel = Json.opt (fun b -> Json.Str (relative_to s.reference b)) base in
        Kept (match record with Json.Obj l -> Json.Obj (l @ [("reference_relative_path", ref_rel)]) | v -> v)
  with e -> Failed (error_message e)

(* The coordinator's side: one finished item into the database. *)
let record s (rel, saved, outcome, read) =
  s.bytes_read <- s.bytes_read +. read;
  (match outcome with
   | Unchanged -> Index.run s.db "DELETE FROM errors WHERE path=?" [text rel]
   | Kept r ->
       (* The earlier content's record is kept as a version, never replaced. *)
       Option.iter (fun t ->
         let old = Json.parse t in
         let label = version_label ~mtime_ns:(mtime_of_ident (field "source_identity" old))
             ~sha256:(Json.str (field "source_sha256" old)) in
         Index.run s.db "INSERT OR IGNORE INTO versions VALUES (?,?,?)" [text rel; text label; text t]) saved;
       Index.run s.db "INSERT OR REPLACE INTO files VALUES (?,?)" [text rel; text (Json.dumps r)];
       Index.run s.db "DELETE FROM errors WHERE path=?" [text rel];
       Option.iter (fun t -> s.account (Json.parse t) (-1)) saved;
       s.account r 1
   | Failed e ->
       (* An earlier record stays: this run's error is recorded beside it. *)
       if saved = None then Index.run s.db "DELETE FROM files WHERE path=?" [text rel];
       Index.run s.db "INSERT OR REPLACE INTO errors VALUES (?,?)" [text rel; text e];
       Printf.printf "ERROR %s %s\n%!" rel e)

let tick s = if Unix.gettimeofday () -. s.last > 60. then (summary s false; s.last <- Unix.gettimeofday ())

(* Identical files in bulk. A new item whose source and reference have current
   phindex rows with the same SHA-256 needs only a reference record, the one
   [preserve_file]'s index shortcut writes, and nothing is read. Going through
   the worker pool, each such record cost a fork and a round trip; on a real
   archive that was a few records a second. Here they are written in order, in
   this process, 5,000 to a transaction. Returns the relative paths recorded;
   the pool takes the rest. *)
let bulk_identical s =
  let handled = Hashtbl.create 1024 in
  (match s.indexes with
   | Some ix when not s.full_copy ->
       let start = Unix.gettimeofday () in
       let last = ref start and checked = ref 0 and in_tx = ref 0 in
       let commit () = if !in_tx > 0 then (Index.exec s.db "COMMIT"; in_tx := 0) in
       let report () =
         Printf.printf "  identical files from the phindex indexes: %s recorded of %s new, %.0fs\n%!"
           (Py.thousands (Hashtbl.length handled)) (Py.thousands !checked) (Unix.gettimeofday () -. start) in
       (try
         Array.iter (fun it ->
           if it.saved = None then begin
             incr checked;
             let base = match it.reference_of with Some r -> r | None -> Filename.concat s.reference it.rel in
             match identity it.path, identity base with
             | exception Unix.Unix_error _ -> ()  (* the pool reports it *)
             | before, prior ->
                 (match indexed_sha256 ix.source_index it.path before, indexed_sha256 ix.reference_index base prior with
                  | Some h, Some h' when h = h' ->
                      (match attrs it.path with
                       | exception Unix.Unix_error _ -> ()
                       | meta ->
                           let r = Json.Obj [
                               ("relative_path", Json.Str it.rel); ("source", Json.Str it.path); ("reference", Json.Str base);
                               ("source_identity", before); ("reference_identity", prior);
                               ("source_attrs", meta); ("source_sha256", Json.Str h); ("reference_sha256", Json.Str h);
                               ("original", Json.Null); ("whole_file_equal", Json.Bool true);
                               ("preserved_bytes", Json.int 0); ("reconstruction_verified", Json.Bool true);
                               ("reference_relative_path", Json.Str (relative_to s.reference base))] in
                           if !in_tx = 0 then Index.exec s.db "BEGIN";
                           record s (it.rel, None, Kept r, 0.);
                           Hashtbl.replace handled it.rel ();
                           incr in_tx;
                           if !in_tx >= 5000 then commit ())
                  | _ -> ())
           end;
           let now = Unix.gettimeofday () in
           if now -. !last >= 30. then (commit (); report (); summary s false; last := now)) s.items
       with e -> commit (); raise e);
       commit ();
       if !checked > 0 then report ()
   | _ -> ());
  handled

(* After every item: identities again, the manifest, the final summary; true
   when every listed file is preserved without error. *)
let finish s =
  if not (List.for_all is_file s.files) then failwith "Source inventory changed";
  (* Identities again before the decision, for the files of this run; records
     of earlier runs may name sources a wiped shuttle no longer holds. *)
  Array.iter (fun it ->
    match query s.db "SELECT record FROM files WHERE path=?" [text it.rel] with
    | [ [| Tursolite.Text t |] ] ->
        let r = Json.parse t in
        let p = Filename.concat s.source it.rel in
        let base = match field "reference" r with Json.Str b -> Some b | _ -> None in
        let same = (try identity p = field "source_identity" r with Unix.Unix_error _ -> false)
                   && (match base with None -> true
                                     | Some b -> try identity b = field "reference_identity" r with Unix.Unix_error _ -> false) in
        if not same then Index.run s.db "INSERT OR REPLACE INTO errors VALUES (?,?)" [text it.rel; text "Identity changed before completion"]
    | _ -> ()) s.items;
  Out_channel.with_open_bin (Filename.concat s.archive "manifest.jsonl") (fun oc ->
    List.iter (function [| Tursolite.Text t |] -> output_string oc t; output_char oc '\n' | _ -> ())
      (query s.db "SELECT record FROM files ORDER BY path" []);
    flush oc; Unix.fsync (Unix.descr_of_out_channel oc));
  summary s true;
  (* The archive may hold earlier subsets; this run succeeds when every listed file is. *)
  let ok rel = query s.db "SELECT 1 FROM files WHERE path=?" [text rel] <> []
               && query s.db "SELECT 1 FROM errors WHERE path=?" [text rel] = [] in
  let success = Array.for_all (fun it -> ok it.rel) s.items && s.missing = [] in
  exec s.db "PRAGMA wal_checkpoint(TRUNCATE)";
  success

let check_options ~rate ~workers =
  if workers < 1 || workers > 36 then failwith "Preservation workers must be 1..36";
  if not (rate > 0. && Float.is_finite rate) then failwith "Rate must be finite and positive"

let scan ?indexes ~source ~reference ~archive ~rate ~full_copy ~delta_mode ~workers ~pairs () =
  check_options ~rate ~workers;
  let archive = validate_output source reference archive in
  with_archive_lock archive ".byte-preservation.lock" (fun () ->
    let s = open_session ?indexes ~source ~reference ~archive ~rate ~full_copy ~delta_mode ~workers ~pairs () in
    Fun.protect ~finally:(fun () -> Native.close_db s.db) (fun () ->
      (* Each worker process keeps its own share of the rate for its whole run. *)
      let job it =
        let b = worker_budget s in
        let read_before = b.bytes in
        let outcome = process s b it in
        it.rel, it.saved, outcome, b.bytes -. read_before in
      let handled = bulk_identical s in
      let rest = Array.of_list (List.filter (fun it -> not (Hashtbl.mem handled it.rel)) (Array.to_list s.items)) in
      pool ~workers rest job (fun result ->
        Option.iter (record s) result;
        tick s);
      finish s))

(* Rebuild a preserved file at [target], which must not exist; the result
   is written next to it, verified, and linked into place. *)
let reconstruct record archive target =
  if Sys.file_exists target then failwith (target ^ " exists");
  let temp = target ^ ".recovery-partial" in
  let fd = Unix.openfile temp [Unix.O_WRONLY; Unix.O_CREAT; Unix.O_EXCL] 0o600 in
  let oc = Unix.out_channel_of_descr fd in
  (try
     (match field "delta" record with
      | Json.Str _ ->
          if sha_file (Json.str (field "reference" record)) <> Json.str (field "reference_sha256" record) then
            failwith "Bumer reference SHA-256 mismatch";
          ignore (stream_delta ~dst:oc record archive)
      | _ ->
          let src = match field "original" record with
            | Json.Str o -> Filename.concat archive o | _ -> Json.str (field "reference" record) in
          let h = In_channel.with_open_bin src (fun ic -> sha_channel ic ~each:(fun buf n -> Out_channel.output oc buf 0 n)) in
          if h <> Json.str (field "source_sha256" record) then failwith "Original file recovery hash mismatch");
     flush oc; Unix.fsync fd; close_out oc
   with e -> close_out_noerr oc; (try Sys.remove temp with Sys_error _ -> ()); raise e);
  Unix.link temp target;
  Sys.remove temp

(* A read-only SQLite connection that leaves nothing behind
   (archive_layout.open_readonly). A plain mode=ro open of a closed WAL
   database creates its -wal and -shm files; with no writer holding the lock
   (the byte section's .byte-preservation.lock, the top's
   .metadata-preservation.lock for metadata/differences.sqlite3) and no WAL
   content, the file is opened immutable=1, which creates nothing; otherwise
   a private copy of the database and its WAL is read. *)
let writer_lock path =
  if Filename.basename path = "differences.sqlite3" then
    Filename.concat (Filename.dirname (Filename.dirname path)) ".metadata-preservation.lock"
  else Filename.concat (Filename.dirname path) ".byte-preservation.lock"

let lock_held lock =
  is_file lock &&
  (let fd = Unix.openfile lock [Unix.O_RDONLY; Unix.O_CLOEXEC] 0 in
   let free = flock_exclusive fd in
   Unix.close fd;
   not free)

let readonly_copies = ref []
let rec remove_tree p =
  match stat ~follow:false p with
  | s when s.kind = `Dir ->
      Array.iter (fun n -> remove_tree (Filename.concat p n)) (try Sys.readdir p with Sys_error _ -> [||]);
      (try Unix.rmdir p with Unix.Unix_error _ -> ())
  | _ -> (try Sys.remove p with Sys_error _ -> ())
  | exception Unix.Unix_error _ -> ()
let () = at_exit (fun () -> List.iter remove_tree !readonly_copies)

let readonly_db path =
  let wal = path ^ "-wal" in
  if not (lock_held (writer_lock path)) && (not (Sys.file_exists wal) || (stat wal).size = 0L) then
    Sqlite3.db_open ~mode:`READONLY ~uri:true ("file:" ^ path ^ "?immutable=1")
  else begin
    let dir = Filename.temp_dir "dngc-db-" "" in
    readonly_copies := dir :: !readonly_copies;
    let copy src dst = Out_channel.with_open_bin dst (fun oc ->
      output_string oc (In_channel.with_open_bin src In_channel.input_all)) in
    let target = Filename.concat dir (Filename.basename path) in
    copy path target;
    if Sys.file_exists wal then copy wal (target ^ "-wal");
    Sqlite3.db_open target
  end

let label_of r = version_label ~mtime_ns:(mtime_of_ident (field "source_identity" r)) ~sha256:(Json.str (field "source_sha256" r))

let restore ?reference_root ?version archive rel output =
  let path = Filename.concat archive "recovery.sqlite3" in
  (* Read without leaving -wal/-shm files behind; an archive on the Turso
     engine is read through its engine. *)
  let lookup, close =
    if Sys.file_exists (Native.marker_of path) then
      let db = open_sqlite path in
      (fun sql params -> List.map (fun r -> Array.map (function Tursolite.Text t -> Some t | _ -> None) r)
                            (query db sql (List.map text params))), (fun () -> Native.close_db db)
    else if not (Sys.file_exists path) then failwith ("unable to open database file: " ^ path)
    else
      let db = readonly_db path in
      (fun sql params ->
        let st = Sqlite3.prepare db sql in
        List.iteri (fun i p -> ignore (Sqlite3.bind st (i + 1) (Sqlite3.Data.TEXT p))) params;
        let rec go acc = match Sqlite3.step st with
          | Sqlite3.Rc.ROW -> go (Array.map (function Sqlite3.Data.TEXT t -> Some t | _ -> None) (Sqlite3.row_data st) :: acc)
          | _ -> ignore (Sqlite3.finalize st); List.rev acc in
        go []), (fun () -> ignore (Sqlite3.db_close db)) in
  let record = Fun.protect ~finally:close (fun () ->
    let query _ sql params = lookup sql (List.map (function Tursolite.Text t -> t | _ -> "") params) in
    let db = () in
    let current = match query db "SELECT record FROM files WHERE path=?" [text rel] with
      | [ [| Some t |] ] -> Some (Json.parse t) | _ -> None in
    match version, current with
    | None, Some r -> r
    | None, None -> failwith "File not preserved"
    | Some v, Some r when label_of r = v -> r
    | Some v, _ ->
        (* An earlier content of the path, by its label. *)
        let has = query db "SELECT name FROM sqlite_master WHERE type='table' AND name='versions'" [] <> [] in
        match if has then query db "SELECT record FROM versions WHERE path=? AND version=?" [text rel; text v] else [] with
        | [ [| Some t |] ] -> Json.parse t
        | _ -> failwith "Version not preserved") in
  let record = match reference_root, record, field "reference" record with
    | Some root, Json.Obj l, Json.Str _ ->
        let r = Json.str (field "reference_relative_path" record) in
        if not (Filename.is_relative r) || List.mem ".." (String.split_on_char '/' r) then failwith "Unsafe reference path";
        Json.Obj (List.map (fun (k, v) -> k, if k = "reference" then Json.Str (Filename.concat root r) else v) l)
    | _ -> record in
  reconstruct record archive output;
  (match field "source_attrs" record with
   | Json.Obj a ->
       (match List.assoc_opt "xattrs" a with
        | Some (Json.Obj x) -> List.iter (fun (n, v) -> setxattr output n (base64_decode (Json.str v))) x | _ -> ());
       let num k = match List.assoc_opt k a with Some (Json.Num s) -> Int64.of_string s | _ -> 0L in
       Unix.chmod output (Int64.to_int (num "mode"));
       utimens output (num "atime_ns") (num "mtime_ns")
   | _ -> ());
  Printf.printf "Restored and SHA-256 verified %s\n" output

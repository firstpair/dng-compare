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

let preserve_file source base archive rel budget ~full_copy ~delta_mode =
  let before = identity source and meta = attrs source in
  let prior = Option.map identity base in
  let size = match before with Json.Arr [_; Json.Num s; _; _] -> Int64.of_string s | _ -> 0L in
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
  let source_hash = sha_final sh and reference_hash = sha_final bh in
  let changed = !changed in
  let unchanged () = identity source = before && (match base, prior with Some b, Some p -> identity b = p | _ -> true) in
  if not (unchanged ()) then failwith changed_error;
  let original =
    if (changed && not (base <> None && delta_mode)) || base = None || full_copy then begin
      let original_rel = "originals/" ^ rel in
      let dest = Filename.concat archive original_rel in
      mkdir_p (Filename.dirname dest);
      let temp = dest ^ ".partial" in
      let copy_hash = In_channel.with_open_bin source (fun ic ->
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
       let delta_rel = "deltas/" ^ rel ^ ".vcdiff" in
       let dest = Filename.concat archive delta_rel in
       mkdir_p (Filename.dirname dest);
       let temp = dest ^ ".partial" in
       if Sys.file_exists temp then Sys.remove temp;
       let ref_size = match prior with Some (Json.Arr [_; Json.Num s; _; _]) -> int_of_string s | _ -> 0 in
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
let size_of_identity r k = match field k r with Json.Arr [_; Json.Num s; _; _] -> Int64.of_string s | _ -> 0L

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

(* Workers k, k+workers, ...; each a forked process with its own share of the
   byte budget. The parent alone writes the database. *)
let pool ~workers items job on_result =
  let workers = max 1 (min workers (Array.length items)) in
  let children = List.init workers (fun k ->
    let input, output = Unix.pipe ~cloexec:true () in
    match Unix.fork () with
    | 0 ->
        Unix.close input;
        let oc = Unix.out_channel_of_descr output in
        let i = ref k in
        while !i < Array.length items do
          Marshal.to_channel oc (job items.(!i)) []; flush oc; i := !i + workers
        done;
        close_out oc; Unix._exit 0
    | pid -> Unix.close output; pid, Unix.in_channel_of_descr input) in
  let pending = ref (List.map snd children) in
  while !pending <> [] do
    let ready, _, _ = Unix.select (List.map Unix.descr_of_in_channel !pending) [] [] 60. in
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

let scan ~source ~reference ~archive ~rate ~full_copy ~delta_mode ~workers ~pairs =
  if workers < 1 || workers > 36 then failwith "Preservation workers must be 1..36";
  if not (rate > 0. && Float.is_finite rate) then failwith "Rate must be finite and positive";
  let archive = validate_output source reference archive in
  with_archive_lock archive ".byte-preservation.lock" (fun () ->
    mkdir_p archive;
    let db = open_sqlite (Filename.concat archive "recovery.sqlite3") in
    Fun.protect ~finally:(fun () -> ignore (Sqlite3.db_close db)) (fun () ->
    exec db "PRAGMA journal_mode=WAL";
    exec db "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, record TEXT NOT NULL)";
    exec db "CREATE TABLE IF NOT EXISTS errors (path TEXT PRIMARY KEY, error TEXT NOT NULL)";
    (* A listed subset: each source with its own reference, both under their roots. *)
    let listed = List.map (fun (s, r) ->
      let p = absolute s in
      ignore (relative_to source p);
      Option.iter (fun r -> ignore (relative_to reference (absolute r))) r;
      if (stat ~follow:false p).kind <> `Reg then failwith ("Unsupported source entry: " ^ p);
      p, Option.map absolute r) pairs in
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
      | [| Sqlite3.Data.TEXT rel; Sqlite3.Data.TEXT t |] -> Hashtbl.replace saved rel t; account (Json.parse t) 1
      | _ -> ()) (query db "SELECT path,record FROM files" []);
    let count sql = match query db sql [] with [ [| Sqlite3.Data.INT n |] ] -> Int64.to_int n | _ -> 0 in
    let bytes_read = ref 0. in
    let summary complete =
      let verified = count "SELECT count(*) FROM files" and errors = count "SELECT count(*) FROM errors" in
      let data = Json.Obj [
        ("source", Json.Str source); ("reference", Json.Str reference); ("total_files", Json.int total);
        ("verified_files", Json.int verified); ("errors", Json.int errors); ("complete", Json.Bool complete);
        ("preservation_workers", Json.int workers); ("safe_to_delete_source", Json.Bool false);
        ("byte_preservation_complete", Json.Bool (complete && verified = total && errors = 0));
        ("bytes_read_this_run", Json.float !bytes_read);
        ("elapsed_seconds", Json.float (Unix.gettimeofday () -. start));
        ("matching", Json.Str "Listed pairs; every candidate is byte-verified.");
        ("note", Json.Str "Deletion readiness also requires complete metadata comparison, final identity validation, and inventory coverage. Recovery references require hash-identical reference files.");
        ("storage_mode", Json.Str (if full_copy then "independent_originals"
                                   else if delta_mode then "standard_vcdiff_and_sha256_references"
                                   else "originals_for_differing_files_and_hash_references_for_identical_files"));
        ("source_bytes_verified", Json.int64 !source_bytes); ("preserved_payload_bytes", Json.int64 !stored);
        ("payload_to_verified_original_ratio",
         if !source_bytes = 0L then Json.Null else Json.float (Int64.to_float !stored /. Int64.to_float !source_bytes));
        ("byte_different_files", Json.int !changed_files)] in
      atomic_json (Filename.concat archive "summary.json") data;
      print_endline (Json.dumps data) in
    summary false;
    let items = Array.of_list (List.map (fun (p, r) ->
      let rel = relative_to source p in
      let s = Hashtbl.find_opt saved rel in
      Hashtbl.remove saved rel; (* later records replace these *)
      p, r, rel, s) listed) in
    (* Each worker process keeps its own share of the rate for its whole run. *)
    let worker_budget = ref None in
    let job (p, r, rel, s) =
      let b = match !worker_budget with
        | Some b -> b
        | None -> let b = budget (rate /. float workers) in worker_budget := Some b; b in
      let read_before = b.bytes in
      let outcome = try
          match s with
          | Some t when still_valid (Json.parse t) archive ~full_copy p -> Unchanged
          | _ ->
              let base = match r with Some r -> r | None -> Filename.concat reference rel in
              let base = if is_file base then Some base else None in
              let record = preserve_file p base archive rel b ~full_copy ~delta_mode in
              let ref_rel = Json.opt (fun b -> Json.Str (relative_to reference b)) base in
              Kept (match record with Json.Obj l -> Json.Obj (l @ [("reference_relative_path", ref_rel)]) | v -> v)
        with Failure e | Sys_error e -> Failed e
           | Unix.Unix_error (e, f, a) -> Failed (Printf.sprintf "%s: %s %s" (Unix.error_message e) f a) in
      rel, s, outcome, b.bytes -. read_before in
    let last = ref (Unix.gettimeofday ()) in
    pool ~workers items job (fun result ->
      (match result with
       | None -> ()
       | Some (rel, s, outcome, read) ->
           bytes_read := !bytes_read +. read;
           (match outcome with
            | Unchanged -> Index.run db "DELETE FROM errors WHERE path=?" [text rel]
            | Kept r ->
                Index.run db "INSERT OR REPLACE INTO files VALUES (?,?)" [text rel; text (Json.dumps r)];
                Index.run db "DELETE FROM errors WHERE path=?" [text rel];
                Option.iter (fun t -> account (Json.parse t) (-1)) s;
                account r 1
            | Failed e ->
                Index.run db "DELETE FROM files WHERE path=?" [text rel];
                Index.run db "INSERT OR REPLACE INTO errors VALUES (?,?)" [text rel; text e];
                Option.iter (fun t -> account (Json.parse t) (-1)) s;
                Printf.printf "ERROR %s %s\n%!" rel e));
      if Unix.gettimeofday () -. !last > 60. then (summary false; last := Unix.gettimeofday ()));
    if not (List.for_all is_file files) then failwith "Source inventory changed";
    (* Identities again before the decision, for the files of this run; records
       of earlier runs may name sources a wiped shuttle no longer holds. *)
    List.iter (fun (_, _, rel, _) ->
      match query db "SELECT record FROM files WHERE path=?" [text rel] with
      | [ [| Sqlite3.Data.TEXT t |] ] ->
          let r = Json.parse t in
          let p = Filename.concat source rel in
          let base = match field "reference" r with Json.Str b -> Some b | _ -> None in
          let same = (try identity p = field "source_identity" r with Unix.Unix_error _ -> false)
                     && (match base with None -> true
                                       | Some b -> try identity b = field "reference_identity" r with Unix.Unix_error _ -> false) in
          if not same then Index.run db "INSERT OR REPLACE INTO errors VALUES (?,?)" [text rel; text "Identity changed before completion"]
      | _ -> ()) (Array.to_list items);
    Out_channel.with_open_bin (Filename.concat archive "manifest.jsonl") (fun oc ->
      List.iter (function [| Sqlite3.Data.TEXT t |] -> output_string oc t; output_char oc '\n' | _ -> ())
        (query db "SELECT record FROM files ORDER BY path" []);
      flush oc; Unix.fsync (Unix.descr_of_out_channel oc));
    summary true;
    (* The archive may hold earlier subsets; this run succeeds when every listed file is. *)
    let ok rel = query db "SELECT 1 FROM files WHERE path=?" [text rel] <> []
                 && query db "SELECT 1 FROM errors WHERE path=?" [text rel] = [] in
    let success = Array.for_all (fun (_, _, rel, _) -> ok rel) items in
    exec db "PRAGMA wal_checkpoint(TRUNCATE)";
    success))

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

let restore ?reference_root archive rel output =
  let db = open_sqlite (Filename.concat archive "recovery.sqlite3") in
  let record = Fun.protect ~finally:(fun () -> ignore (Sqlite3.db_close db)) (fun () ->
    match query db "SELECT record FROM files WHERE path=?" [text rel] with
    | [ [| Sqlite3.Data.TEXT t |] ] -> Json.parse t
    | _ -> failwith "File not preserved") in
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

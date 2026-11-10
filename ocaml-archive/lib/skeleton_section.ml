(* The skeleton section of an archive as a whole (docs/dngc-archive-spec.md,
   section 13): `dngc skeleton build ARCHIVE` writes a skeleton for every
   reference file the archive's records name, so the records still rebuild
   after those files are edited; `dngc skeleton info ARCHIVE` counts them. As
   the Rust tool dngc-skeleton does (rust-archive/src/bin/dngc-skeleton.rs),
   on the same files. *)

open Native

type job = { path : string; sha256 : string; size : int64; mtime_ns : int64 }

type outcome =
  | Built of { sha256 : string; record : string; packed : string; raw : int }
  | Skipped of string * string * string  (* reason, path, detail *)

(* One reference file: its skeleton, taken only while the file's size and
   modification time are those of the record that names its content. *)
let work job =
  let skip reason detail = Skipped (reason, job.path, detail) in
  match stat job.path with
  | exception Unix.Unix_error (e, _, _) -> skip "missing" (Unix.error_message e)
  | st when st.size <> job.size || st.mtime_ns <> job.mtime_ns ->
      skip "changed" "size or modification time differs from the record"
  | st ->
      match Skeleton.build job.path with
      | exception Failure e -> skip (if String.starts_with ~prefix:"no skeleton walker" e then "no-walker" else "error") e
      | exception Sys_error e -> skip "error" e
      | exception Unix.Unix_error (e, _, _) -> skip "error" (Unix.error_message e)
      | sk ->
          (* A whole-file skeleton was read in full: check it is the content the record names. *)
          if sk.Skeleton.blocks = [] && sha_string sk.Skeleton.bytes <> job.sha256 then skip "changed" "bytes differ from the record"
          else
            let identity = Json.Arr [ Json.int64 st.ino; Json.int64 st.size; Json.int64 st.mtime_ns; Json.int64 st.ctime_ns ] in
            match Skeleton.encode ~sha256:job.sha256 ~path:job.path ~identity sk with
            | record, packed -> Built { sha256 = job.sha256; record; packed; raw = String.length sk.Skeleton.bytes }
            | exception (Failure e | Sys_error e) -> skip "error" e

let text_rows db sql =
  List.map (Array.map (function Sqlite3.Data.TEXT t -> t | _ -> "")) (Skeleton.rows db sql [])

let mb n = float n /. 1e6

let build ?limit ~workers archive =
  let top = Preserve.archive_top archive and bytes = Preserve.byte_section archive in
  let recovery = Filename.concat bytes "recovery.sqlite3" in
  if not (Sys.file_exists recovery) then failwith ("no byte section in " ^ top);
  let db = Skeleton.create top in
  let lock = Unix.openfile (Filename.concat top "skeletons/.lock") [ Unix.O_WRONLY; Unix.O_CREAT; Unix.O_CLOEXEC ] 0o644 in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.db_close db); Skeleton.tidy top; Unix.close lock) (fun () ->
    if not (flock_exclusive lock) then failwith "another process is building this archive's skeletons";
    (* Each record's base content, unless it already has a skeleton. *)
    let total = ref 0 and jobs = ref [] and sent = ref 0 in
    let listed = Hashtbl.create 1024 in
    let records = Preserve.readonly_db recovery in
    Fun.protect ~finally:(fun () -> ignore (Sqlite3.db_close records)) (fun () ->
      List.iter (fun row ->
        incr total;
        let r = Json.parse row.(0) in
        let str k = match Json.member k r with Some (Json.Str s) -> Some s | _ -> None in
        let truthy k = match Json.member k r with Some v -> Json.truthy v | None -> false in
        let num i = match Json.member "reference_identity" r with
          | Some (Json.Arr l) -> (match List.nth_opt l i with
              | Some (Json.Num s) -> Option.value (Int64.of_string_opt s) ~default:(-1L) | _ -> -1L)
          | _ -> -1L in
        let delta = truthy "delta" in
        if delta || not (truthy "original") then
          match str "reference", str (if delta then "reference_sha256" else "source_sha256") with
          | Some path, Some sha256 when (match limit with Some l -> !sent < l | None -> true)
                                        && not (Hashtbl.mem listed (sha256, path)) && not (Skeleton.has db sha256) ->
              Hashtbl.replace listed (sha256, path) ();
              incr sent;
              jobs := { path; sha256; size = num 1; mtime_ns = num 2 } :: !jobs
          | _ -> ())
        (text_rows records "SELECT record FROM files ORDER BY path"));
    let jobs = Array.of_list (List.rev !jobs) in
    Printf.printf "%s records; skeletons in %s\n%!" (Py.thousands !total) (Skeleton.store_path top);
    let start = Unix.gettimeofday () in
    let last = ref start in
    let seen = ref (!total - Array.length jobs) and built = ref 0 and raw = ref 0 and packed_bytes = ref 0 and pending = ref 0 in
    let skipped = Hashtbl.create 8 in
    let skipped_total () = Hashtbl.fold (fun _ n acc -> acc + n) skipped 0 in
    let log = open_out_gen [ Open_wronly; Open_creat; Open_append; Open_binary ] 0o644 (Filename.concat top "skeletons/skipped.tsv") in
    let clean s = String.map (function '\t' | '\n' -> ' ' | c -> c) s in
    let interrupted = ref false in
    Sys.catch_break true;
    Skeleton.exec db "BEGIN";
    Fun.protect ~finally:(fun () -> Sys.catch_break false; close_out_noerr log) (fun () ->
      (try
         Preserve.pool ~wait:1. ~workers jobs work (fun result ->
           (match result with
            | None -> ()
            | Some (Built b) ->
                incr seen;
                Skeleton.put db ~sha256:b.sha256 ~record:b.record ~packed:b.packed;
                incr built; raw := !raw + b.raw; packed_bytes := !packed_bytes + String.length b.packed; incr pending
            | Some (Skipped (reason, path, detail)) ->
                incr seen;
                Hashtbl.replace skipped reason (1 + Option.value (Hashtbl.find_opt skipped reason) ~default:0);
                Printf.fprintf log "%s\t%s\t%s\n%!" reason path (clean detail));
           if !pending >= 2000 then (Skeleton.exec db "COMMIT"; Skeleton.exec db "BEGIN"; pending := 0);
           let now = Unix.gettimeofday () in
           if now -. !last >= 5. then begin
             last := now;
             Printf.printf "  %s of %s records, %s skeletons built (%.1f MB), %d skipped, %.0f s\n%!"
               (Py.thousands !seen) (Py.thousands !total) (Py.thousands !built) (mb !packed_bytes) (skipped_total ()) (now -. start)
           end)
       with Sys.Break -> interrupted := true);
      Skeleton.exec db "COMMIT";
      Skeleton.finish db);
    Printf.printf "%s skeletons built in %.0f s: %.1f MB stored for %.1f MB of skeleton bytes%s\n"
      (Py.thousands !built) (Unix.gettimeofday () -. start) (mb !packed_bytes) (mb !raw)
      (if !interrupted then " (interrupted; run again to continue)" else "");
    List.iter (fun (reason, n) -> Printf.printf "  skipped, %s: %s\n" reason (Py.thousands n))
      (List.sort compare (Hashtbl.fold (fun k v acc -> (k, v) :: acc) skipped []));
    if Hashtbl.length skipped > 0 then Printf.printf "  listed in %s\n" (Filename.concat top "skeletons/skipped.tsv");
    flush stdout;
    if !interrupted then 130 else 0)

let info archive =
  let top = Preserve.archive_top archive in
  let path = Skeleton.store_path top in
  if not (Sys.file_exists path) then (Printf.printf "no skeleton section in %s\n%!" top; 1)
  else begin
    let db = Preserve.readonly_db path in
    Fun.protect ~finally:(fun () -> ignore (Sqlite3.db_close db)) (fun () ->
      (match Skeleton.rows db "SELECT count(*), coalesce(sum(length(skeleton)),0) FROM skeletons" [] with
       | [ [| Sqlite3.Data.INT n; Sqlite3.Data.INT stored |] ] ->
           Printf.printf "%s skeletons, %.1f MB stored\n" (Py.thousands (Int64.to_int n)) (Int64.to_float stored /. 1e6)
       | _ -> failwith "no count of skeletons");
      List.iter (function
        | [| Sqlite3.Data.TEXT f; Sqlite3.Data.INT c |] -> Printf.printf "  %s: %s\n" f (Py.thousands (Int64.to_int c))
        | _ -> ())
        (Skeleton.rows db "SELECT json_extract(record,'$.format'), count(*) FROM skeletons GROUP BY 1 ORDER BY 2 DESC" []);
      flush stdout;
      0)
  end

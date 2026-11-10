(* The archive-format conformance suite, with this library (docs/dngc-archive-spec.md).

   Reading: every case of ../conformance/corpus is read with the library's own
   code: the byte-section rule, the record text (json.dumps form), every current
   record rebuilt with Preserve.reconstruct, earlier versions under their
   labels. A valid case must read cleanly, verify cleanly, and refuse to restore
   dropped paths; a case broken in its payloads or references must fail to
   rebuild.

   Writing: dngc run (--impl ocaml) and the phindex-metadata step write
   archives from a case's trees, and conformance/validate_archive.py, written
   from the specification alone, must find nothing wrong.

   Records hold absolute paths, so each case is materialized at
   /private/tmp/dngc-conformance/<case>, where make_corpus.py built it. *)

open Dng_archive

let build = "/private/tmp/dngc-conformance"
let rebuild_rules = ["payload-hash"; "payload-missing"; "rebuild"; "reference"]

(* The checkout: the first ancestor of the working directory with conformance/corpus. *)
let repo =
  let rec up d =
    if Sys.file_exists (Filename.concat d "conformance/corpus") then d
    else let p = Filename.dirname d in if p = d then failwith "no conformance/corpus above the build" else up p in
  up (Sys.getcwd ())

let corpus = Filename.concat repo "conformance/corpus"
let failures = ref 0
let fail fmt = Printf.ksprintf (fun m -> incr failures; Printf.printf "FAIL %s\n%!" m) fmt

let rec rm_rf p =
  match (Unix.lstat p).Unix.st_kind with
  | Unix.S_DIR -> Array.iter (fun n -> rm_rf (Filename.concat p n)) (Sys.readdir p); Unix.rmdir p
  | _ -> Sys.remove p
  | exception Unix.Unix_error _ -> ()

let rec copy_tree src dst =
  Unix.mkdir dst 0o755;
  Array.iter (fun n ->
    let s = Filename.concat src n and d = Filename.concat dst n in
    if Sys.is_directory s then copy_tree s d
    else begin
      (* Times kept: the phindex indexes of a case match its files by size and mtime. *)
      Out_channel.with_open_bin d (fun oc -> output_string oc (In_channel.with_open_bin s In_channel.input_all));
      let st = Unix.stat s in
      Unix.utimes d st.Unix.st_atime st.Unix.st_mtime
    end)
    (Sys.readdir src)

let materialize name =
  let dst = Filename.concat build name in
  rm_rf dst;
  (try Unix.mkdir build 0o755 with Unix.Unix_error (Unix.EEXIST, _, _) -> ());
  copy_tree (Filename.concat corpus name) dst;
  dst

let rows db sql =
  let st = Sqlite3.prepare db sql in
  let out = ref [] in
  while Sqlite3.step st = Sqlite3.Rc.ROW do
    out := Array.map (function Sqlite3.Data.TEXT s -> s | _ -> "") (Sqlite3.row_data st) :: !out
  done;
  ignore (Sqlite3.finalize st);
  List.rev !out

let has_table db name = rows db (Printf.sprintf "SELECT name FROM sqlite_master WHERE type='table' AND name='%s'" name) <> []

let expected_rules case =
  match Json.member "rules" (Json.parse (In_channel.with_open_bin (Filename.concat case "expected.json") In_channel.input_all)) with
  | Some (Json.Arr l) -> List.filter_map (function Json.Str s -> Some s | _ -> None) l
  | _ -> []

(* What reading this case found wrong: one entry per record that does not rebuild. *)
let read_case name case =
  let top = Filename.concat case "archive" in
  (* [3] the byte-section rule, derived here independently of Preserve.byte_section. *)
  let legacy = Sys.file_exists (Filename.concat top "recovery.sqlite3") && not (Sys.file_exists (Filename.concat top "bytes")) in
  let bytes = if legacy then top else Filename.concat top "bytes" in
  if Preserve.byte_section top <> bytes then fail "%s: byte section %s, the rule gives %s" name (Preserve.byte_section top) bytes;
  if Preserve.archive_top top <> top then fail "%s: archive top %s" name (Preserve.archive_top top);
  let db = Preserve.readonly_db (Filename.concat bytes "recovery.sqlite3") in
  let broken = ref [] in
  let scratch = Filename.temp_dir "dngc-conformance-" "" in
  List.iteri (fun i r ->
    let path = r.(0) and text = r.(1) in
    let record = Json.parse text in
    if Json.dumps record <> text then fail "%s %s: record not in json.dumps form" name path;
    if Json.member "relative_path" record <> Some (Json.Str path) then fail "%s %s: relative_path" name path;
    match Preserve.reconstruct record bytes (Filename.concat scratch (string_of_int i)) with
    | () -> ()
    | exception e -> broken := (path ^ ": " ^ Printexc.to_string e) :: !broken)
    (rows db "SELECT path, record FROM files ORDER BY path");
  if has_table db "versions" then
    List.iter (fun r ->
      if Preserve.label_of (Json.parse r.(2)) <> r.(1) then fail "%s %s: earlier version stored as %s" name r.(0) r.(1))
      (rows db "SELECT path, version, record FROM versions");
  let trashed = if has_table db "trash" then List.map (fun r -> r.(0)) (rows db "SELECT path FROM trash WHERE state='current'") else [] in
  ignore (Sqlite3.db_close db);
  rm_rf scratch;
  !broken, top, trashed

(* Dngc_cli.main with stdout sent to /dev/null; its exit status. *)
let dngc args =
  flush stdout;
  let saved = Unix.dup Unix.stdout in
  let null = Unix.openfile "/dev/null" [Unix.O_WRONLY] 0 in
  Unix.dup2 null Unix.stdout; Unix.close null;
  let status = Fun.protect ~finally:(fun () -> flush stdout; Unix.dup2 saved Unix.stdout; Unix.close saved)
      (fun () -> Dngc_cli.main args) in
  status

let validator_clean archive =
  let python = Option.value (Sys.getenv_opt "PYTHON") ~default:"python3" in
  Sys.command (Printf.sprintf "%s %s %s --rebuild > /dev/null" (Filename.quote python)
                 (Filename.quote (Filename.concat repo "conformance/validate_archive.py")) (Filename.quote archive)) = 0

let () =
  let names = List.sort compare (List.filter (fun n -> Sys.file_exists (Filename.concat (Filename.concat corpus n) "expected.json"))
                                  (Array.to_list (Sys.readdir corpus))) in
  if names = [] then fail "no corpus cases";
  (* Reading *)
  List.iter (fun name ->
    let case = materialize name in
    let rules = expected_rules case in
    let broken, top, trashed = read_case name case in
    if List.exists (fun r -> List.mem r rebuild_rules) rules then begin
      if broken = [] then fail "%s: broken (%s) yet every record rebuilt" name (String.concat ", " rules)
    end else begin
      List.iter (fun b -> fail "%s: %s" name b) broken;
      if rules = [] then begin
        if dngc ["verify"; top] <> 0 then fail "%s: verify reports problems" name;
        List.iter (fun p ->
          if dngc ["restore"; top; p; Filename.concat case "refused"] <> 1 then fail "%s: restore of dropped %s not refused" name p)
          trashed
      end
    end) names;
  (* Writing *)
  if List.mem "current" names then begin
    let case = materialize "current" in
    let arc = Filename.concat case "ocaml-archive" in
    let left = Filename.concat case "L" and right = Filename.concat case "R" in
    let st = dngc ["run"; left; right; arc; "--impl"; "ocaml"; "--metadata"; "none"] in
    if st > 1 then fail "dngc run --impl ocaml exited %d" st
    else if not (validator_clean arc) then fail "the archive dngc run --impl ocaml wrote does not validate";
    let md = Filename.concat case "ocaml-metadata" in
    let st = Phindex_metadata.run [left; right; md; "--pairs"; Filename.concat case "pairs.tsv";
                                   "--left-index"; Filename.concat case "l.sqlite"; "--right-index"; Filename.concat case "r.sqlite";
                                   "--workers"; "2"] in
    if st <> 0 then fail "phindex-metadata exited %d: its rows had errors" st
    else if not (validator_clean md) then fail "the metadata section phindex-metadata wrote does not validate"
  end;
  Printf.printf "conformance: %d cases read, %s\n" (List.length names)
    (if !failures = 0 then "all as the spec says" else Printf.sprintf "%d failures" !failures);
  exit (if !failures = 0 then 0 else 1)

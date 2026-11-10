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

   Skeletons (section 13): the walkers, marks, store and rebuild on synthetic
   TIFF and JPEG files; the `current` case restored after every reference
   file is edited; an archive of image pairs written by dngc run, its section
   built by dngc skeleton build and compared with the Rust tool's when that is
   built.

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

(* Dngc_cli.main with stdout sent to /dev/null (or the file [out]); its exit status. *)
let dngc ?(out = "/dev/null") args =
  flush stdout;
  let saved = Unix.dup Unix.stdout in
  let null = Unix.openfile out [Unix.O_WRONLY; Unix.O_CREAT; Unix.O_TRUNC] 0o644 in
  Unix.dup2 null Unix.stdout; Unix.close null;
  let status = Fun.protect ~finally:(fun () -> flush stdout; Unix.dup2 saved Unix.stdout; Unix.close saved)
      (fun () -> Dngc_cli.main args) in
  status

(* ---- Skeletons (section 13) ---- *)

let read_file p = In_channel.with_open_bin p In_channel.input_all
let write_file p s = Out_channel.with_open_bin p (fun oc -> output_string oc s)

(* The fixtures of the Rust library's tests (rust-archive/src/skeleton.rs). *)
let noise n seed =
  let x = ref (max 1 ((seed * 2654435761) land 0xFFFFFFFF)) in
  String.init n (fun _ ->
    x := !x lxor ((!x lsl 13) land 0xFFFFFFFF);
    x := !x lxor (!x lsr 17);
    x := !x lxor ((!x lsl 5) land 0xFFFFFFFF);
    Char.chr (!x land 255))

let le16 n = String.init 2 (fun i -> Char.chr ((n lsr (8 * i)) land 255))
let le32 n = String.init 4 (fun i -> Char.chr ((n lsr (8 * i)) land 255))

(* A little-endian TIFF: XMP, a large private tag, one strip and a JPEG
   preview, then IFD0. A longer XMP packet moves everything after it. *)
let tiff ?(strip = noise 200_000 2) xmp =
  let priv = noise 800 1 and preview = noise 30_000 3 in
  let b = Buffer.create 300_000 in
  Buffer.add_string b "II*\000\000\000\000\000";
  let place d = let at = Buffer.length b in Buffer.add_string b d; if Buffer.length b mod 2 = 1 then Buffer.add_char b '\000'; at in
  let x = place xmp in let p = place priv in let s = place strip in let j = place preview in
  let ifd = Buffer.length b in
  let entries = [ (273, 4, 1, s); (279, 4, 1, String.length strip); (513, 4, 1, j); (514, 4, 1, String.length preview);
                  (700, 1, String.length xmp, x); (50740, 1, String.length priv, p) ] in
  Buffer.add_string b (le16 (List.length entries));
  List.iter (fun (tag, typ, count, value) -> Buffer.add_string b (le16 tag ^ le16 typ ^ le32 count ^ le32 value)) entries;
  Buffer.add_string b (le32 0);
  let out = Buffer.to_bytes b in
  Bytes.blit_string (le32 ifd) 0 out 4 4;
  Bytes.to_string out

let jpeg segments scan =
  let b = Buffer.create 200_000 in
  Buffer.add_string b "\xff\xd8";
  List.iter (fun (marker, data) ->
    let n = String.length data + 2 in
    Buffer.add_char b '\xff'; Buffer.add_char b (Char.chr marker);
    Buffer.add_char b (Char.chr (n lsr 8)); Buffer.add_char b (Char.chr (n land 255)); Buffer.add_string b data) segments;
  Buffer.add_string b "\xff\xda\000\002"; Buffer.add_string b scan; Buffer.add_string b "\xff\xd9";
  Buffer.contents b

let jpeg_scan () =
  let b = Buffer.create 150_000 in
  String.iter (fun c -> if c <> '\xff' then Buffer.add_char b c) (noise 150_000 5);
  Buffer.contents b
let jpeg_old () = jpeg [ (0xe0, "JFIF\000"); (0xe1, "Exif\000\000old"); (0xe2, noise 3000 4); (0xdb, noise 130 6) ] (jpeg_scan ())
let jpeg_new () = jpeg [ (0xe0, "JFIF\000"); (0xe1, "Exif\000\000new and longer"); (0xed, "Photoshop 3.0\000caption");
                         (0xe1, noise 900 7); (0xe2, noise 3000 4); (0xdb, noise 130 6) ] (jpeg_scan ())
let xmp_old = "<x:xmpmeta>rating 0</x:xmpmeta>"
let xmp_new = "<x:xmpmeta>rating 5, exposure +0.35, blacks +12 " ^ String.make 3001 ' ' ^ "</x:xmpmeta>"

let roles sk = List.map (fun b -> b.Skeleton.role) sk.Skeleton.blocks

(* Skeleton of [old_], rebuilt from [new_]: the skeleton, and the bytes rebuilt or the error. *)
let round dir old_ new_ =
  let o = Filename.concat dir "old" and n = Filename.concat dir "new" and out = Filename.concat dir "out" in
  write_file o old_; write_file n new_;
  let sk = Skeleton.build o in
  let got = match Out_channel.with_open_bin out (fun oc -> Skeleton.rebuild sk n oc) with
    | sha ->
        let bytes = read_file out in
        if sha <> Native.sha_string bytes then fail "skeleton: rebuild returned another SHA-256 than it wrote";
        Ok bytes
    | exception Failure e -> Error e in
  sk, got

let contains s sub =
  let n = String.length sub in
  let rec go i = i + n <= String.length s && (String.sub s i n = sub || go (i + 1)) in
  go 0

let skeleton_units () =
  let dir = Filename.temp_dir "dngc-skeleton-test-" "" in
  Fun.protect ~finally:(fun () -> rm_rf dir) (fun () ->
    (* A TIFF comes back from an edited one whose blocks moved. *)
    let old_ = tiff xmp_old in
    let sk, got = round dir old_ (tiff xmp_new) in
    if got <> Ok old_ then fail "skeleton: a TIFF does not come back from an edited one with moved blocks";
    if sk.Skeleton.format <> "tiff" then fail "skeleton: format %s for a TIFF" sk.Skeleton.format;
    if roles sk <> [ "0/tag50740"; "0/strips"; "0/jpeg" ] then fail "skeleton: TIFF blocks %s" (String.concat " " (roles sk));
    if String.length sk.Skeleton.bytes >= 300 then fail "skeleton: %d bytes kept of a TIFF" (String.length sk.Skeleton.bytes);
    (* The mark of a block: SHA-256 of both ends, 32 hex digits. *)
    let strip = List.find (fun b -> b.Skeleton.role = "0/strips") sk.Skeleton.blocks in
    let body = String.sub old_ strip.Skeleton.at strip.Skeleton.len in
    let ends = String.sub body 0 4096 ^ String.sub body (String.length body - 4096) 4096 in
    if strip.Skeleton.mark <> String.sub (Native.sha_string ends) 0 32 then fail "skeleton: a long block's mark";
    let small = List.find (fun b -> b.Skeleton.role = "0/tag50740") sk.Skeleton.blocks in
    if small.Skeleton.mark <> String.sub (Native.sha_string (String.sub old_ small.Skeleton.at small.Skeleton.len)) 0 32 then
      fail "skeleton: a short block's mark";
    (* A changed block is reported, not rebuilt. *)
    let flip s i = String.mapi (fun k c -> if k = i then Char.chr (Char.code c lxor 1) else c) s in
    (match snd (round dir old_ (flip old_ strip.Skeleton.at)) with
     | Error e when contains e "0/strips" -> ()
     | Error e -> fail "skeleton: a changed block reported as: %s" e
     | Ok _ -> fail "skeleton: a changed block was rebuilt");
    (* A change between a block's marked ends: only the caller's SHA-256 check sees it. *)
    (match snd (round dir old_ (flip old_ (strip.Skeleton.at + strip.Skeleton.len / 2))) with
     | Ok bytes when bytes <> old_ && String.length bytes = String.length old_ -> ()
     | Ok _ -> fail "skeleton: a change inside a block gave the old bytes"
     | Error e -> fail "skeleton: a change inside a block: %s" e);
    (* A big-endian TIFF with two strips two bytes apart (one block), a SubIFD
       with tiles and a run no tag points at. *)
    let be16 n = String.init 2 (fun i -> Char.chr ((n lsr (8 * (1 - i))) land 255)) in
    let be32 n = String.init 4 (fun i -> Char.chr ((n lsr (8 * (3 - i))) land 255)) in
    let big xmp =
      let b = Buffer.create 400_000 in
      Buffer.add_string b "MM\000*\000\000\000\000";
      let place d = let at = Buffer.length b in Buffer.add_string b d; at in
      let x = place xmp in
      let s1 = place (noise 5000 11) in ignore (place "\000\000");
      let s2 = place (noise 7001 12) in ignore (place "\000");
      let hidden = place (noise 70_000 13) in ignore hidden;
      let t1 = place (noise 9000 14) in
      let offsets = place (be32 s1 ^ be32 s2) in
      let counts = place (be32 5000 ^ be32 7001) in
      let sub = place "" in
      Buffer.add_string b (be16 2);
      Buffer.add_string b (be16 324 ^ be16 4 ^ be32 1 ^ be32 t1);
      Buffer.add_string b (be16 325 ^ be16 3 ^ be32 1 ^ be16 9000 ^ be16 0);
      Buffer.add_string b (be32 0);
      let ifd = place "" in
      Buffer.add_string b (be16 4);
      Buffer.add_string b (be16 273 ^ be16 4 ^ be32 2 ^ be32 offsets);
      Buffer.add_string b (be16 279 ^ be16 4 ^ be32 2 ^ be32 counts);
      Buffer.add_string b (be16 330 ^ be16 13 ^ be32 1 ^ be32 sub);
      Buffer.add_string b (be16 700 ^ be16 1 ^ be32 (String.length xmp) ^ be32 x);
      Buffer.add_string b (be32 0);
      let out = Buffer.to_bytes b in
      Bytes.blit_string (be32 ifd) 0 out 4 4;
      Bytes.to_string out in
    let old_ = big (String.make 600 'x') in
    let sk, got = round dir old_ (big (String.make 2777 'y')) in
    if got <> Ok old_ then fail "skeleton: a big-endian TIFF does not come back";
    if roles sk <> [ "0/strips"; "gap"; "0/330.0/tiles" ] then fail "skeleton: big-endian TIFF blocks %s" (String.concat " " (roles sk));
    (match sk.Skeleton.blocks with
     | s :: g :: _ ->
         if s.Skeleton.len <> 5000 + 2 + 7001 then fail "skeleton: strips two bytes apart are %d bytes" s.Skeleton.len;
         if g.Skeleton.len <> 70_001 then fail "skeleton: the gap is %d bytes" g.Skeleton.len
     | _ -> ());
    (* A JPEG comes back after its metadata segments change. *)
    let old_ = jpeg_old () in
    let sk, got = round dir old_ (jpeg_new ()) in
    if got <> Ok old_ then fail "skeleton: a JPEG does not come back after its metadata segments change";
    if sk.Skeleton.format <> "jpeg" || roles sk <> [ "sege2"; "scan" ] then fail "skeleton: JPEG blocks %s" (String.concat " " (roles sk));
    (* A small file without a walker is its own skeleton. *)
    let old_ = "<x:xmpmeta>sidecar</x:xmpmeta>" in
    let sk, got = round dir old_ "anything else" in
    if sk.Skeleton.format <> "whole" || sk.Skeleton.blocks <> [] || got <> Ok old_ then fail "skeleton: a small file is not its own skeleton";
    (* A large one has none. *)
    write_file (Filename.concat dir "large") (String.make (Skeleton.whole + 1) 'z');
    (match Skeleton.build (Filename.concat dir "large") with
     | _ -> fail "skeleton: a large file without a walker got a skeleton"
     | exception Failure e -> if e <> Skeleton.no_walker then fail "skeleton: a large file without a walker: %s" e);
    (* The store gives back what was put, and keeps the first skeleton of a content. *)
    let old_ = tiff "<x:xmpmeta/>" in
    let f = Filename.concat dir "f.dng" in
    write_file f old_;
    let sk = Skeleton.build f and sha = Native.sha_string old_ in
    let record, packed = Skeleton.encode ~sha256:sha ~path:"/somewhere/f.dng"
        ~identity:(Json.Arr [ Json.int 1; Json.int (String.length old_); Json.int 2; Json.int 3 ]) sk in
    if Json.dumps (Json.parse record) <> record then fail "skeleton: a record is not in json.dumps form";
    if String.length packed < 4 || String.sub packed 0 4 <> "\x28\xb5\x2f\xfd" then fail "skeleton: the stored bytes are not a zstd frame";
    let db = Skeleton.create dir in
    Skeleton.put db ~sha256:sha ~record ~packed;
    Skeleton.put db ~sha256:sha ~record:"{}" ~packed:"";
    if not (Skeleton.has db sha) || Skeleton.has db "00" || Skeleton.count db <> 1 then fail "skeleton: the store's rows";
    (match Skeleton.get db sha with
     | Some (r, back) -> if back <> sk || Json.dumps r <> record then fail "skeleton: the store gave back another skeleton"
     | None -> fail "skeleton: the store lost a row");
    if Skeleton.get db "00" <> None then fail "skeleton: a row for a content without one";
    (* A row whose bytes are not those of skeleton_sha256 is refused. *)
    Skeleton.put db ~sha256:"11" ~record ~packed:(Skeleton.compress "other bytes");
    (match Skeleton.get db "11" with
     | _ -> fail "skeleton: a row with other bytes was accepted"
     | exception Failure _ -> ());
    Skeleton.finish db;
    ignore (Sqlite3.db_close db);
    Skeleton.tidy dir;
    if Array.to_list (Sys.readdir (Filename.concat dir "skeletons")) <> [ "skeletons.sqlite3" ] then
      fail "skeleton: a finished store is not one file")

let records_of bytes =
  let db = Preserve.readonly_db (Filename.concat bytes "recovery.sqlite3") in
  let l = List.map (fun r -> r.(0), Json.parse r.(1)) (rows db "SELECT path, record FROM files ORDER BY path") in
  ignore (Sqlite3.db_close db);
  l

let str k r = match Json.member k r with Some (Json.Str s) -> Some s | _ -> None

(* What a record needs of its reference: (reference, SHA-256 of its content); none for a full record. *)
let base_of record =
  let truthy k = match Json.member k record with Some v -> Json.truthy v | None -> false in
  if truthy "delta" then Option.bind (str "reference" record) (fun r -> Option.map (fun h -> r, h) (str "reference_sha256" record))
  else if truthy "original" then None
  else Option.bind (str "reference" record) (fun r -> Option.map (fun h -> r, h) (str "source_sha256" record))

(* Every record of [top] restored with the library and with dngc restore, and
   compared with the file under [left]; temporary bases must be gone after. *)
let restore_all what top left =
  let bytes = Preserve.byte_section top in
  let scratch = Filename.temp_dir "dngc-conformance-" "" in
  let temps = Filename.concat scratch "tmp" in
  Unix.mkdir temps 0o755;
  let saved = Filename.get_temp_dir_name () in
  Filename.set_temp_dir_name temps;
  Fun.protect ~finally:(fun () -> Filename.set_temp_dir_name saved; rm_rf scratch) (fun () ->
    List.iteri (fun i (path, record) ->
      let want = read_file (Filename.concat left path) in
      let out = Filename.concat scratch (string_of_int i) in
      (match Preserve.reconstruct ~top record bytes out with
       | () -> if read_file out <> want then fail "%s %s: rebuilt other bytes" what path
       | exception e -> fail "%s %s: %s" what path (Printexc.to_string e));
      let out = out ^ ".cli" in
      if dngc [ "restore"; top; path; out ] <> 0 then fail "%s %s: dngc restore failed" what path
      else if read_file out <> want then fail "%s %s: dngc restore wrote other bytes" what path)
      (records_of bytes);
    match Sys.readdir temps with
    | [||] -> ()
    | left -> fail "%s: temporary files left behind: %s" what (String.concat " " (Array.to_list left)))

(* How many records fail to rebuild, and the first error. *)
let broken_records top =
  let bytes = Preserve.byte_section top in
  let scratch = Filename.temp_dir "dngc-conformance-" "" in
  Fun.protect ~finally:(fun () -> rm_rf scratch) (fun () ->
    List.filter_map (fun (path, record) ->
      match Preserve.reconstruct ~top record bytes (Filename.concat scratch (string_of_int (Hashtbl.hash path))) with
      | () -> None
      | exception e -> Some (path, match e with Failure m | Sys_error m -> m | e -> Printexc.to_string e)) (records_of bytes))

let verify_skeletons what top want =
  let out = Filename.temp_file "dngc-verify-" ".json" in
  Fun.protect ~finally:(fun () -> Sys.remove out) (fun () ->
    if dngc ~out [ "verify"; top ] <> 0 then fail "%s: verify reports problems" what;
    let report = Json.parse (read_file out) in
    if Json.member "skeletons" report <> Some (Json.int want) then
      fail "%s: verify counts %s skeletons, not %d" what
        (match Json.member "skeletons" report with Some v -> Json.dumps v | None -> "no") want;
    match Json.member "sections" report with
    | Some (Json.Arr l) when List.mem (Json.Str "skeletons") l -> ()
    | _ -> fail "%s: verify does not list the skeleton section" what)

(* Section 13 on the corpus: after every reference file is edited, each record
   of the `current` case still restores, its base rebuilt from the skeleton
   section. Without the section the same edits fail as they always did. *)
let skeletons_on_current () =
  let case = materialize "current" in
  let top = Filename.concat case "archive" and left = Filename.concat case "L" in
  let bytes = Preserve.byte_section top in
  let bases = List.sort_uniq compare (List.filter_map (fun (_, r) -> base_of r) (records_of bytes)) in
  if bases = [] then fail "skeletons: the current case names no reference";
  let db = Skeleton.create top in
  List.iter (fun (reference, sha256) ->
    let record, packed = Skeleton.encode ~sha256 ~path:reference ~identity:(Preserve.identity reference) (Skeleton.build reference) in
    Skeleton.put db ~sha256 ~record ~packed) bases;
  Skeleton.finish db;
  ignore (Sqlite3.db_close db);
  Skeleton.tidy top;
  let contents = List.length (List.sort_uniq compare (List.map snd bases)) in
  verify_skeletons "skeletons, current" top contents;
  restore_all "skeletons, current, references as they were" top left;
  List.iter (fun reference -> write_file reference (read_file reference ^ " edited after the archive was written"))
    (List.sort_uniq compare (List.map fst bases));
  restore_all "skeletons, current, references edited" top left;
  (* A corrupt section is a problem verify names. *)
  let store = Skeleton.store_path top in
  let kept = read_file store in
  write_file store (String.sub kept 0 100 ^ String.make 4000 '\xff');
  if dngc [ "verify"; top ] <> 1 then fail "skeletons: verify passes a corrupt skeleton section";
  (* Without the section: the errors a reader without section 13 gives. *)
  rm_rf (Filename.concat top "skeletons");
  let broken = broken_records top in
  if List.length broken <> List.length (List.filter (fun (_, r) -> base_of r <> None) (records_of bytes)) then
    fail "skeletons: %d records fail without the section" (List.length broken);
  List.iter (fun (path, e) ->
    if e <> "Bumer reference SHA-256 mismatch" && e <> "Original file recovery hash mismatch" then
      fail "skeletons: %s without the section fails with: %s" path e) broken

(* Section 13 end to end on image files: dngc run writes an archive of TIFF
   and JPEG pairs, dngc skeleton build its skeleton section, the reference
   files are rewritten with other metadata (their blocks move), and every
   record restores. With the Rust tool at hand, its section must hold the same
   records, and this library must read it. *)
let skeletons_on_images () =
  let case = Filename.concat build "skeleton-ocaml" in
  rm_rf case;
  (try Unix.mkdir build 0o755 with Unix.Unix_error (Unix.EEXIST, _, _) -> ());
  Unix.mkdir case 0o755;
  let left = Filename.concat case "L" and right = Filename.concat case "R" and top = Filename.concat case "archive" in
  Unix.mkdir left 0o755; Unix.mkdir right 0o755;
  let put dir name bytes = write_file (Filename.concat dir name) bytes in
  (* a.tif differs between the trees (a delta); the others are identical. *)
  put left "a.tif" (tiff ~strip:(noise 200_000 8) xmp_old); put right "a.tif" (tiff xmp_old);
  put left "b.jpg" (jpeg_old ()); put right "b.jpg" (jpeg_old ());
  let xmp_c = "<x:xmpmeta>another image</x:xmpmeta>" in
  put left "c.tif" (tiff xmp_c); put right "c.tif" (tiff xmp_c);
  put left "d.xmp" "<x:xmpmeta>sidecar</x:xmpmeta>"; put right "d.xmp" "<x:xmpmeta>sidecar</x:xmpmeta>";
  put left "e.bin" (noise (Skeleton.whole + 10) 9); put right "e.bin" (noise (Skeleton.whole + 10) 9);
  let st = dngc [ "run"; left; right; top; "--impl"; "ocaml"; "--metadata"; "none" ] in
  if st > 1 then fail "skeletons: dngc run exited %d" st;
  let bytes = Preserve.byte_section top in
  let kinds = List.map (fun (p, r) -> p, (match Json.member "delta" r with Some (Json.Str _) -> "delta" | _ -> "identical")) (records_of bytes) in
  if kinds <> [ ("a.tif", "delta"); ("b.jpg", "identical"); ("c.tif", "identical"); ("d.xmp", "identical"); ("e.bin", "identical") ] then
    fail "skeletons: dngc run wrote %s" (String.concat " " (List.map (fun (p, k) -> p ^ ":" ^ k) kinds));
  if dngc [ "skeleton"; "build"; top; "--workers"; "2" ] <> 0 then fail "skeletons: dngc skeleton build failed";
  (* e.bin has no walker and is too large. *)
  verify_skeletons "skeletons, images" top 4;
  let store = Skeleton.store_path top in
  if List.sort compare (Array.to_list (Sys.readdir (Filename.concat top "skeletons"))) <> [ ".lock"; "skeletons.sqlite3"; "skipped.tsv" ] then
    fail "skeletons: the finished section holds %s" (String.concat " " (Array.to_list (Sys.readdir (Filename.concat top "skeletons"))));
  let skipped = read_file (Filename.concat top "skeletons/skipped.tsv") in
  if skipped <> Printf.sprintf "no-walker\t%s\t%s\n" (Filename.concat right "e.bin") Skeleton.no_walker then
    fail "skeletons: skipped.tsv holds %S" skipped;
  let stored () =
    let db = Preserve.readonly_db store in
    let l = rows db "SELECT sha256, record FROM skeletons ORDER BY sha256" in
    ignore (Sqlite3.db_close db);
    l in
  let ours = stored () in
  List.iter (fun r ->
    let record = Json.parse r.(1) in
    if Json.dumps record <> r.(1) then fail "skeletons: record of %s not in json.dumps form" r.(0);
    if Json.member "sha256" record <> Some (Json.Str r.(0)) then fail "skeletons: record of %s names another content" r.(0);
    match str "path" record with
    | Some p when Native.sha_file p = r.(0) -> ()
    | _ -> fail "skeletons: record of %s does not name a file with that content" r.(0)) ours;
  (* A second build adds nothing. *)
  if dngc [ "skeleton"; "build"; top ] <> 0 || stored () <> ours then fail "skeletons: a second build changed the section";
  if dngc [ "skeleton"; "info"; top ] <> 0 then fail "skeletons: dngc skeleton info failed";
  (* The Rust tool's section of the same archive. *)
  let rust = List.find_opt (fun p -> p <> "" && Sys.file_exists p)
      [ Option.value (Sys.getenv_opt "DNGC_SKELETON") ~default:""; Filename.concat repo "rust-archive/target/release/dngc-skeleton" ] in
  let aside = Filename.concat case "skeletons-ocaml" in
  (match rust with
   | None -> print_endline "conformance: no dngc-skeleton (Rust) built; its section is not compared"
   | Some tool ->
       Sys.rename (Filename.concat top "skeletons") aside;
       if Sys.command (Printf.sprintf "%s build %s > /dev/null" (Filename.quote tool) (Filename.quote top)) <> 0 then
         fail "skeletons: the Rust tool failed"
       else begin
         let theirs = stored () in
         if List.map (fun r -> r.(0), r.(1)) theirs <> List.map (fun r -> r.(0), r.(1)) ours then
           fail "skeletons: the Rust tool wrote other records:\n%s\nnot\n%s"
             (String.concat "\n" (List.map (fun r -> r.(1)) theirs)) (String.concat "\n" (List.map (fun r -> r.(1)) ours))
         else print_endline "conformance: the Rust tool's skeleton records equal this library's"
       end);
  let edit () =
    put right "a.tif" (tiff xmp_new); put right "c.tif" (tiff (xmp_new ^ " again"));
    put right "b.jpg" (jpeg_new ()); put right "d.xmp" "<x:xmpmeta>sidecar, rated</x:xmpmeta>" in
  let all_but_large what =
    (* e.bin has no skeleton: it restores only while its reference is as it was. *)
    restore_all what top left in
  all_but_large "skeletons, images, references as they were";
  edit ();
  all_but_large (if rust = None then "skeletons, images, references edited" else "skeletons, images, references edited, Rust's section");
  if rust <> None then begin
    rm_rf (Filename.concat top "skeletons");
    Sys.rename aside (Filename.concat top "skeletons");
    all_but_large "skeletons, images, references edited, this library's section"
  end;
  (* A reference whose block changed does not rebuild, and nothing is written. *)
  put right "a.tif" (tiff ~strip:(noise 200_000 10) xmp_new);
  put right "c.tif" (tiff ~strip:(noise 200_000 10) xmp_new);
  put right "e.bin" (noise (Skeleton.whole + 10) 9 ^ "edited");
  (match List.sort compare (broken_records top) with
   | [ ("a.tif", a); ("c.tif", c); ("e.bin", e) ] ->
       if not (contains a "0/strips" && String.starts_with ~prefix:"Bumer reference SHA-256 mismatch, and its skeleton does not rebuild it" a) then
         fail "skeletons: a delta whose reference lost a block fails with: %s" a;
       if not (contains c "0/strips") then fail "skeletons: an identical record whose reference lost a block fails with: %s" c;
       if e <> "Original file recovery hash mismatch" then fail "skeletons: a reference without a skeleton fails with: %s" e
   | l -> fail "skeletons: after a block changed, these fail: %s" (String.concat "; " (List.map (fun (p, e) -> p ^ ": " ^ e) l)))

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
  (* Skeletons *)
  skeleton_units ();
  if List.mem "current" names then skeletons_on_current ();
  skeletons_on_images ();
  Printf.printf "conformance: %d cases read, %s\n" (List.length names)
    (if !failures = 0 then "all as the spec says" else Printf.sprintf "%d failures" !failures);
  exit (if !failures = 0 then 0 else 1)

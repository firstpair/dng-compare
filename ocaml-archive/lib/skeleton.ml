(* Skeletons (docs/dngc-archive-spec.md, section 13): what makes a content
   rebuildable from an edited version of itself.

   A metadata edit (a rating, develop settings, IPTC) rewrites a file's small
   structures and carries its large blocks over unchanged, perhaps to other
   offsets. A skeleton of a file is every byte outside its blocks, with a map
   of where each block goes; the blocks themselves are not stored. Given a
   later version of the file, the blocks are found in it by their marks and
   laid around the skeleton, and the whole is checked against the content's
   SHA-256.

   Walkers: classic TIFF (DNG and TIFF-based raws) and JPEG. A file of another
   format no larger than [whole] is its own skeleton, with no blocks; a larger
   one has no skeleton. The walkers give the same blocks as the Rust library's
   (rust-archive/src/skeleton.rs).

   The store is ARCHIVE/skeletons/skeletons.sqlite3, one row per content; a
   row's skeleton is one zstd frame, coded and decoded by the zstd program. *)

open Native

(* An out-of-line TIFF value or JPEG segment this large is a block. *)
let block_min = 512
(* A run this long that no tag points at is a block. *)
let gap = 65536
(* A block's mark hashes this many bytes from each end (the whole block when
   it is no longer than twice this). *)
let mark_bytes = 4096
(* Strips or tiles of one image this close together are one block. *)
let pad = 3
(* A file without a walker is stored whole up to this size. *)
let whole = 1 lsl 20

(* XMP, IPTC and Photoshop resources: what editors rewrite, kept in the skeleton. *)
let volatile = [700; 33723; 34377]
(* SubIFDs, Exif, GPS and Interoperability. *)
let pointers = [330; 34665; 34853; 40965]

type block = { at : int; len : int; mark : string; role : string }

(* A file's skeleton: its format, size, blocks in file order, and every byte
   outside them, concatenated in file order. *)
type t = { format : string; size : int; blocks : block list; bytes : string }

let no_walker = "no skeleton walker for this format"

(* Up to [len] bytes at [at]; fewer at the end of the file. *)
let read_at fd at len =
  if len <= 0 then "" else begin
    ignore (Unix.LargeFile.lseek fd (Int64.of_int at) Unix.SEEK_SET);
    let chunk = Bytes.create (min len 1048576) in
    let b = Buffer.create (min len 1048576) in
    let rec go left =
      if left > 0 then
        match Unix.read fd chunk 0 (min left (Bytes.length chunk)) with
        | 0 -> ()
        | n -> Buffer.add_subbytes b chunk 0 n; go (left - n)
        | exception Unix.Unix_error (Unix.EINTR, _, _) -> go left in
    go len;
    Buffer.contents b
  end

let with_file path f =
  let fd = try Unix.openfile path [Unix.O_RDONLY; Unix.O_CLOEXEC] 0
    with Unix.Unix_error (e, _, _) -> failwith (path ^ ": " ^ Unix.error_message e) in
  Fun.protect ~finally:(fun () -> Unix.close fd) (fun () ->
    f fd (Int64.to_int (Unix.LargeFile.fstat fd).Unix.LargeFile.st_size))

let rd16 le s o = if le then String.get_uint16_le s o else String.get_uint16_be s o
let rd32 le s o = Int32.to_int (if le then String.get_int32_le s o else String.get_int32_be s o) land 0xFFFFFFFF

type entry = { tag : int; typ : int; raw : string; total : int }

let type_size = function 3 | 8 -> 2 | 4 | 9 | 11 | 13 -> 4 | 5 | 10 | 12 -> 8 | _ -> 1

(* An entry's unsigned integer values (BYTE, SHORT, LONG, IFD). *)
let values fd le size e =
  match (match e.typ with 1 -> 1 | 3 -> 2 | 4 | 13 -> 4 | _ -> 0) with
  | 0 -> []
  | width ->
      let buf =
        if e.total <= 4 then String.sub e.raw 0 e.total
        else
          let at = rd32 le e.raw 0 in
          if at + e.total > size then "" else read_at fd at e.total in
      if String.length buf <> e.total then []
      else List.init (e.total / width) (fun k ->
          let o = k * width in
          match width with 1 -> Char.code buf.[o] | 2 -> rd16 le buf o | _ -> rd32 le buf o)

(* (offset, length, role) *)
type extent = int * int * string

let tiff fd size : extent list =
  let h = read_at fd 0 8 in
  let le = match if String.length h >= 2 then String.sub h 0 2 else "" with
    | "II" -> true | "MM" -> false | _ -> failwith "not a TIFF" in
  if String.length h < 8 || rd16 le h 2 <> 42 then failwith "not a classic TIFF";
  let blocks = ref [] and claims = ref [ (0, 8) ] in
  let seen = Hashtbl.create 16 in
  let queue = Queue.create () in
  Queue.add (rd32 le h 4, "0") queue;
  while not (Queue.is_empty queue) do
    let first, role = Queue.pop queue in
    let off = ref first and n = ref 0 and going = ref true in
    while !going && !off <> 0 && !off + 6 <= size && not (Hashtbl.mem seen !off) do
      Hashtbl.replace seen !off ();
      let head = read_at fd !off 2 in
      if String.length head < 2 then going := false
      else begin
        let cnt = rd16 le head 0 in
        let data = read_at fd (!off + 2) (cnt * 12 + 4) in
        if String.length data < cnt * 12 + 4 then going := false
        else begin
          let r = if !n = 0 then role else Printf.sprintf "%s+%d" role !n in
          claims := (!off, 6 + cnt * 12) :: !claims;
          let entries = List.init cnt (fun k ->
              let o = k * 12 in
              let typ = rd16 le data (o + 2) in
              { tag = rd16 le data o; typ; raw = String.sub data (o + 8) 4; total = type_size typ * rd32 le data (o + 4) }) in
          let find tag = List.find_opt (fun e -> e.tag = tag) entries in
          let add o c name = if c > 0 && o + c <= size then blocks := (o, c, r ^ "/" ^ name) :: !blocks in
          List.iter (fun (otag, ctag, name) ->
            match find otag, find ctag with
            | Some o, Some c ->
                let rec zip os cs = match os, cs with
                  | o :: os, c :: cs -> add o c name; zip os cs
                  | _ -> () in
                let os = values fd le size o in
                zip os (values fd le size c)
            | _ -> ()) [ (273, 279, "strips"); (324, 325, "tiles") ];
          (match find 513, find 514 with
           | Some o, Some c ->
               let os = values fd le size o in
               (match os, values fd le size c with
                | o :: _, c :: _ -> add o c "jpeg"
                | _ -> ())
           | _ -> ());
          List.iter (fun e ->
            if e.total > 4 then begin
              let at = rd32 le e.raw 0 in
              if at + e.total <= size then begin
                claims := (at, e.total) :: !claims;
                if e.total >= block_min && not (List.mem e.tag volatile) && not (List.mem e.tag pointers)
                   && not (List.mem e.tag [273; 279; 324; 325]) then
                  blocks := (at, e.total, Printf.sprintf "%s/tag%d" r e.tag) :: !blocks
              end
            end) entries;
          List.iter (fun tag ->
            match find tag with
            | Some e -> List.iteri (fun k o -> Queue.add (o, Printf.sprintf "%s/%d.%d" r tag k) queue) (values fd le size e)
            | None -> ()) pointers;
          off := rd32 le data (cnt * 12);
          incr n
        end
      end
    done
  done;
  (* Data only a maker's own structures reach: no tag points at it, and an
     edit carries it over as it is. *)
  let all = List.sort compare ((size, 0) :: !claims @ List.map (fun (o, l, _) -> (o, l)) !blocks) in
  let pos = ref 0 in
  List.iter (fun (o, l) ->
    if o > !pos && o - !pos >= gap then blocks := (!pos, o - !pos, "gap") :: !blocks;
    pos := max !pos (o + l)) all;
  !blocks

let jpeg fd size : extent list =
  let blocks = ref [] in
  let pos = ref 2 and going = ref true in
  while !going && !pos + 4 <= size do
    let m = read_at fd !pos 4 in
    if String.length m < 4 || m.[0] <> '\xff' then going := false
    else begin
      let marker = Char.code m.[1] in
      if marker = 0xff then incr pos
      (* Start of scan, or a marker without a length: the image runs from here. *)
      else if marker = 0xda || marker = 0x00 || marker = 0x01 || (marker >= 0xd0 && marker <= 0xd9) then going := false
      else begin
        let seg = rd16 false m 2 + 2 in
        if seg < 4 || !pos + seg > size then going := false
        else begin
          (* Exif and XMP (APP1), IPTC (APP13) and comments stay in the skeleton. *)
          if not (List.mem marker [0xe1; 0xed; 0xfe]) && seg >= block_min then
            blocks := (!pos, seg, Printf.sprintf "seg%02x" marker) :: !blocks;
          pos := !pos + seg
        end
      end
    end
  done;
  if !pos < size then blocks := (!pos, size - !pos, "scan") :: !blocks;
  !blocks

(* Sorted, without overlaps; the strips or tiles of one image merged when no
   more than [pad] bytes apart. *)
let normalize (blocks : extent list) : extent list =
  let out = List.fold_left (fun out (o, l, role) ->
    match out with
    | (lo, ll, lrole) :: rest ->
        let stop = lo + ll in
        if o < stop then out
        else if role = lrole && (String.ends_with ~suffix:"strips" role || String.ends_with ~suffix:"tiles" role) && o - stop <= pad then
          (lo, o + l - lo, lrole) :: rest
        else (o, l, role) :: out
    | [] -> [ (o, l, role) ]) [] (List.sort_uniq compare blocks) in
  List.rev out

(* A file's format and blocks, decided by its first bytes. *)
let walk fd size =
  let magic = read_at fd 0 4 in
  if magic = "II*\000" || magic = "MM\000*" then "tiff", normalize (tiff fd size)
  else if String.length magic >= 2 && String.sub magic 0 2 = "\xff\xd8" then "jpeg", normalize (jpeg fd size)
  else failwith no_walker

let sha_add c s = sha_update c (Bytes.unsafe_of_string s) 0 (String.length s)

let mark fd at len =
  let c = sha_init () in
  if len <= 2 * mark_bytes then sha_add c (read_at fd at len)
  else begin
    sha_add c (read_at fd at mark_bytes);
    sha_add c (read_at fd (at + len - mark_bytes) mark_bytes)
  end;
  String.sub (sha_final c) 0 32

(* The skeleton of the file at [path]. Reads its structures, the bytes outside
   its blocks, and each block's two ends; never a whole block. *)
let build path =
  with_file path (fun fd size ->
    let format, extents = match walk fd size with
      | w -> w
      | exception (Failure _ | Unix.Unix_error _) when size <= whole -> "whole", [] in
    let bytes = Buffer.create 65536 in
    let pos = ref 0 in
    let blocks = List.map (fun (at, len, role) ->
        Buffer.add_string bytes (read_at fd !pos (at - !pos));
        pos := at + len;
        { at; len; mark = mark fd at len; role }) extents in
    Buffer.add_string bytes (read_at fd !pos (size - !pos));
    let outside = size - List.fold_left (fun n b -> n + b.len) 0 blocks in
    if Buffer.length bytes <> outside then failwith "file changed while its skeleton was read";
    { format; size; blocks; bytes = Buffer.contents bytes })

(* Rebuild the skeleton's content into [oc] with blocks taken from [edited], a
   later version of the same file; returns the SHA-256 of what was written.
   The caller compares it with the content's SHA-256: a mark covers only a
   block's ends. *)
let rebuild sk edited oc =
  let h = sha_init () in
  let put s o n = sha_update h (Bytes.unsafe_of_string s) o n; Out_channel.output_substring oc s o n in
  if sk.blocks = [] then begin
    if String.length sk.bytes <> sk.size then failwith "skeleton shorter than its content";
    put sk.bytes 0 (String.length sk.bytes);
    sha_final h
  end else
    with_file edited (fun fd size ->
      let _, extents = walk fd size in
      let found = Hashtbl.create 16 in
      List.iter (fun (at, len, _) ->
        let key = (len, mark fd at len) in
        if not (Hashtbl.mem found key) then Hashtbl.replace found key at) extents;
      let pos = ref 0 and skel = ref 0 in
      let buf = Bytes.create 1048576 in
      List.iter (fun b ->
        let n = b.at - !pos in
        if n < 0 || !skel + n > String.length sk.bytes then failwith "skeleton shorter than its map";
        put sk.bytes !skel n;
        skel := !skel + n;
        let at = match Hashtbl.find_opt found (b.len, b.mark) with
          | Some at -> at
          | None -> failwith (Printf.sprintf "block %s (%d bytes) is not in %s" b.role b.len edited) in
        ignore (Unix.LargeFile.lseek fd (Int64.of_int at) Unix.SEEK_SET);
        let left = ref b.len in
        while !left > 0 do
          match Unix.read fd buf 0 (min !left (Bytes.length buf)) with
          | 0 -> failwith (edited ^ ": failed to fill whole buffer")
          | k -> put (Bytes.unsafe_to_string buf) 0 k; left := !left - k
          | exception Unix.Unix_error (Unix.EINTR, _, _) -> ()
        done;
        pos := b.at + b.len) sk.blocks;
      put sk.bytes !skel (String.length sk.bytes - !skel);
      if !skel + (sk.size - !pos) <> String.length sk.bytes then failwith "skeleton longer than its map";
      sha_final h)

(* ---- zstd, through the zstd program, as deltas go through xdelta3 ---- *)

let zstd_program () = Option.value (Sys.getenv_opt "DNGC_ZSTD") ~default:"zstd"

(* [input] through `zstd ARGS`, its standard output returned. *)
let zstd args input =
  let temp = Filename.temp_file "dngc-zstd-" "" in
  Fun.protect ~finally:(fun () -> try Sys.remove temp with Sys_error _ -> ()) (fun () ->
    Out_channel.with_open_bin temp (fun oc -> output_string oc input);
    let src = Unix.openfile temp [Unix.O_RDONLY; Unix.O_CLOEXEC] 0 in
    let rd, wr = Unix.pipe ~cloexec:true () in
    let program = zstd_program () in
    let pid = try Unix.create_process program (Array.of_list (program :: "-q" :: "-c" :: args)) src wr Unix.stderr
      with Unix.Unix_error (e, _, _) -> Unix.close src; Unix.close rd; Unix.close wr;
        failwith (program ^ ": " ^ Unix.error_message e) in
    Unix.close src; Unix.close wr;
    let ic = Unix.in_channel_of_descr rd in
    let out = Fun.protect ~finally:(fun () -> close_in_noerr ic) (fun () -> In_channel.input_all ic) in
    let rec wait () = try snd (Unix.waitpid [] pid) with Unix.Unix_error (Unix.EINTR, _, _) -> wait () in
    match wait () with
    | Unix.WEXITED 0 -> out
    | _ -> failwith (program ^ " " ^ String.concat " " args ^ " failed"))

let compress bytes = zstd [ "-19" ] bytes
let decompress packed = zstd [ "-d" ] packed

(* ---- Records (section 13.2) ---- *)

let blocks_json sk =
  Json.Arr (List.map (fun b -> Json.Arr [ Json.int b.at; Json.int b.len; Json.Str b.mark; Json.Str b.role ]) sk.blocks)

(* A skeleton from a record and its uncompressed bytes. *)
let of_record record bytes =
  let n = function
    | Json.Num s -> (match int_of_string_opt s with Some v when v >= 0 -> v | _ -> failwith "bad number in a skeleton record")
    | _ -> failwith "bad number in a skeleton record" in
  let text = function Json.Str s -> s | _ -> "" in
  let blocks = match Json.member "blocks" record with
    | Some (Json.Arr l) ->
        List.map (function
          | Json.Arr [ at; len; mark; role ] -> { at = n at; len = n len; mark = text mark; role = text role }
          | _ -> failwith "bad block in a skeleton record") l
    | _ -> [] in
  let size = n (Option.value (Json.member "size" record) ~default:Json.Null) in
  ignore (List.fold_left (fun pos b ->
    if b.at < pos || b.at + b.len > size then failwith "blocks out of order in a skeleton record";
    b.at + b.len) 0 blocks);
  { format = text (Option.value (Json.member "format" record) ~default:Json.Null); size; blocks; bytes }

(* The record of a skeleton of the content [sha256], found at [path] with
   [identity], and its compressed bytes. *)
let encode ~sha256 ~path ~identity sk =
  let record = Json.Obj [
      ("sha256", Json.Str sha256); ("size", Json.int sk.size); ("format", Json.Str sk.format);
      ("path", Json.Str path); ("identity", identity); ("blocks", blocks_json sk);
      ("skeleton_bytes", Json.int (String.length sk.bytes)); ("skeleton_sha256", Json.Str (sha_string sk.bytes));
      ("skeleton_format", Json.Str "zstd") ] in
  Json.dumps record, compress sk.bytes

(* ---- The store: ARCHIVE/skeletons/skeletons.sqlite3 ---- *)

let store_path top = Filename.concat (Filename.concat top "skeletons") "skeletons.sqlite3"

let check db rc = match rc with
  | Sqlite3.Rc.OK | Sqlite3.Rc.DONE | Sqlite3.Rc.ROW -> ()
  | rc -> failwith (Printf.sprintf "%s (%s)" (Sqlite3.errmsg db) (Sqlite3.Rc.to_string rc))

let exec db sql = check db (Sqlite3.exec db sql)

let rows db sql params =
  let st = try Sqlite3.prepare db sql with Sqlite3.Error m -> failwith m in
  Fun.protect ~finally:(fun () -> ignore (Sqlite3.finalize st)) (fun () ->
    List.iteri (fun i p -> check db (Sqlite3.bind st (i + 1) p)) params;
    let rec go acc = match Sqlite3.step st with
      | Sqlite3.Rc.ROW -> go (Sqlite3.row_data st :: acc)
      | rc -> check db rc; List.rev acc in
    go [])

(* The section opened for writing, created when missing. *)
let create top =
  mkdir_p (Filename.concat top "skeletons");
  let db = Sqlite3.db_open (store_path top) in
  exec db "PRAGMA journal_mode=WAL";
  exec db "PRAGMA synchronous=NORMAL";
  exec db "CREATE TABLE IF NOT EXISTS skeletons(sha256 TEXT PRIMARY KEY, record TEXT NOT NULL, skeleton BLOB NOT NULL)";
  db

(* A finished section is one plain file, readable from read-only media. *)
let finish db =
  ignore (rows db "PRAGMA wal_checkpoint(TRUNCATE)" []);
  ignore (rows db "PRAGMA journal_mode=DELETE" [])

(* After the finished section's database is closed: some SQLite builds (the
   macOS system library) leave the -shm file of a database that has left WAL
   mode. Removed when the database is in rollback-journal mode and has no WAL. *)
let tidy top =
  let path = store_path top in
  let rollback = try In_channel.with_open_bin path (fun ic -> In_channel.seek ic 18L; In_channel.really_input_string ic 2) = Some ""
    with Sys_error _ -> false in
  if rollback && not (Sys.file_exists (path ^ "-wal")) then (try Sys.remove (path ^ "-shm") with Sys_error _ -> ())

let has db sha256 = rows db "SELECT 1 FROM skeletons WHERE sha256=?" [ Sqlite3.Data.TEXT sha256 ] <> []

(* Add a row; a content that already has a skeleton keeps it. *)
let put db ~sha256 ~record ~packed =
  ignore (rows db "INSERT OR IGNORE INTO skeletons(sha256, record, skeleton) VALUES(?, ?, ?)"
            [ Sqlite3.Data.TEXT sha256; Sqlite3.Data.TEXT record; Sqlite3.Data.BLOB packed ])

let count db = match rows db "SELECT count(*) FROM skeletons" [] with
  | [ [| Sqlite3.Data.INT n |] ] -> Int64.to_int n
  | _ -> failwith "no count of skeletons"

(* A content's record and skeleton, its bytes checked against skeleton_sha256. *)
let get db sha256 =
  match rows db "SELECT record, skeleton FROM skeletons WHERE sha256=?" [ Sqlite3.Data.TEXT sha256 ] with
  | [ [| Sqlite3.Data.TEXT text; (Sqlite3.Data.BLOB packed | Sqlite3.Data.TEXT packed) |] ] ->
      let record = Json.parse text in
      let bytes = decompress packed in
      if Some (Json.Str (sha_string bytes)) <> Json.member "skeleton_sha256" record then
        failwith (Printf.sprintf "skeleton of %s does not match its skeleton_sha256" sha256);
      Some (record, of_record record bytes)
  | [] -> None
  | _ -> failwith (Printf.sprintf "bad skeleton row for %s" sha256)

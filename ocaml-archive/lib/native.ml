(* Native calls (dng_archive_stubs.c) and the file helpers built on them. *)

external sha_init : unit -> bytes = "dng_archive_sha_init"
external sha_update : bytes -> bytes -> int -> int -> unit = "dng_archive_sha_update" [@@noalloc]
external sha_final : bytes -> string = "dng_archive_sha_final"
external zlib_compress : string -> int -> string = "dng_archive_zlib_compress"
external gunzip : string -> string = "dng_archive_gunzip"
external stat_raw : string -> bool -> int64 array = "dng_archive_stat"
external listxattr_raw : string -> string list = "dng_archive_listxattr"
external getxattr : string -> string -> string = "dng_archive_getxattr"
external setxattr : string -> string -> string -> unit = "dng_archive_setxattr"
external utimens : string -> int64 -> int64 -> unit = "dng_archive_utimens"
external flock_exclusive : Unix.file_descr -> bool = "dng_archive_flock_exclusive"

let block = 1048576

type stat = {
  ino : int64; size : int64; mtime_ns : int64; ctime_ns : int64; atime_ns : int64;
  mode : int; uid : int; gid : int; flags : int64; kind : [ `Reg | `Dir | `Link | `Other ];
}

let stat ?(follow = true) path =
  let a = stat_raw path follow in
  { ino = a.(0); size = a.(1); mtime_ns = a.(2); ctime_ns = a.(3); atime_ns = a.(4);
    mode = Int64.to_int a.(5); uid = Int64.to_int a.(6); gid = Int64.to_int a.(7); flags = a.(8);
    kind = (match a.(9) with 0L -> `Reg | 1L -> `Dir | 2L -> `Link | _ -> `Other) }

let listxattr path = List.rev (listxattr_raw path)

let sha_string s =
  let c = sha_init () in
  sha_update c (Bytes.unsafe_of_string s) 0 (String.length s);
  sha_final c

(* Hash a channel to its end; [each] sees every block read. *)
let sha_channel ?(each = fun _ _ -> ()) ic =
  let c = sha_init () and buf = Bytes.create block in
  let rec loop () =
    let n = input ic buf 0 block in
    if n > 0 then (sha_update c buf 0 n; each buf n; loop ()) in
  loop ();
  sha_final c

let sha_file path = In_channel.with_open_bin path (fun ic -> sha_channel ic)

let fsync_path path =
  let fd = Unix.openfile path [Unix.O_RDONLY] 0 in
  Fun.protect ~finally:(fun () -> Unix.close fd) (fun () -> Unix.fsync fd)

(* base64.b64encode / b64decode, standard alphabet with padding. *)
let alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

let base64_encode s =
  let n = String.length s in
  let b = Buffer.create ((n + 2) / 3 * 4) in
  let byte i = if i < n then Char.code s.[i] else 0 in
  let rec go i =
    if i < n then begin
      let v = (byte i lsl 16) lor (byte (i + 1) lsl 8) lor byte (i + 2) in
      Buffer.add_char b alphabet.[(v lsr 18) land 63];
      Buffer.add_char b alphabet.[(v lsr 12) land 63];
      Buffer.add_char b (if i + 1 < n then alphabet.[(v lsr 6) land 63] else '=');
      Buffer.add_char b (if i + 2 < n then alphabet.[v land 63] else '=');
      go (i + 3)
    end in
  go 0;
  Buffer.contents b

let base64_decode s =
  let value c = match String.index_opt alphabet c with Some v -> v | None -> failwith "bad base64" in
  let b = Buffer.create (String.length s * 3 / 4) in
  let n = String.length s in
  if n mod 4 <> 0 then failwith "bad base64 length";
  let rec go i =
    if i < n then begin
      let q k = if s.[i + k] = '=' then 0 else value s.[i + k] in
      let v = (q 0 lsl 18) lor (q 1 lsl 12) lor (q 2 lsl 6) lor q 3 in
      Buffer.add_char b (Char.chr ((v lsr 16) land 255));
      if s.[i + 2] <> '=' then Buffer.add_char b (Char.chr ((v lsr 8) land 255));
      if s.[i + 3] <> '=' then Buffer.add_char b (Char.chr (v land 255));
      go (i + 4)
    end in
  go 0;
  Buffer.contents b

let rec mkdir_p path =
  if not (Sys.file_exists path) then begin
    mkdir_p (Filename.dirname path);
    try Unix.mkdir path 0o777 with Unix.Unix_error (Unix.EEXIST, _, _) -> ()
  end

(* Path.suffix.lower(): the last dot-separated suffix of the final name. *)
let suffix path =
  let name = Filename.basename path in
  match String.rindex_opt name '.' with
  | Some i when i > 0 && i < String.length name - 1 -> String.lowercase_ascii (String.sub name i (String.length name - i))
  | _ -> ""

(* SQLite file opened the way dng-compare's database_backend opens it, which
   only the legacy engine can do from here: an archive moved to the Turso
   engine is refused rather than written behind its marker. *)
let open_sqlite path =
  let marker = path ^ ".engine.json" in
  (if Sys.file_exists marker then
     match Json.member "engine" (Json.parse (In_channel.with_open_bin marker In_channel.input_all)) with
     | Some (Json.Str "sqlite") | None -> ()
     | Some (Json.Str e) -> failwith (path ^ " uses the " ^ e ^ " engine; open it with dng-compare's Python tools")
     | Some _ -> failwith ("bad engine marker " ^ marker)
   else match Sys.getenv_opt "DNG_DATABASE_BACKEND" with
     | None | Some "sqlite" -> ()
     | Some e -> failwith ("DNG_DATABASE_BACKEND=" ^ e ^ ": dng-archive writes SQLite only"));
  let db = Sqlite3.db_open path in
  Sqlite3.busy_timeout db 60000;
  db

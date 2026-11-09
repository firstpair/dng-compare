(* compare_dng.py's per-pair comparison and its report, from files read once
   into memory: compare_pixels (encoded blocks, with the decoder child of
   decoded_status_isolated), metadata (Perl ExifTool, normalize_tags, flatten,
   compare_metadata, compact) and Summary. Results are Json values whose
   canonical text is byte-identical to what compare_dng.py writes. *)

let version = 4

let canonical v = Json.dumps ~compact:true ~sort_keys:true v

let jstr s = Json.Str s
let obj = Json.(fun l -> Obj l)

(* An ordered dict, as Python keeps one: updates stay at the key's place. *)
module Odict = struct
  type t = { mutable keys : string list; tbl : (string, Json.t) Hashtbl.t }
  let create () = { keys = []; tbl = Hashtbl.create 16 }
  let set d k v = if not (Hashtbl.mem d.tbl k) then d.keys <- k :: d.keys; Hashtbl.replace d.tbl k v
  let get d k = Hashtbl.find_opt d.tbl k
  let mem d k = Hashtbl.mem d.tbl k
  let to_list d = List.rev_map (fun k -> k, Hashtbl.find d.tbl k) d.keys
end

(* str(exc) of the exceptions a comparison meets. *)
let error_text = function
  | Failure m -> m
  | Tursolite.Error { message; _ } -> message
  | Unix.Unix_error (e, _, path) -> Py.oserror e path
  | Sys_error m -> m
  | Not_found -> "Not_found"
  | Invalid_argument m -> m
  | Json.Bad m -> m
  | e -> Printexc.to_string e

let stamp path =
  let s = Native.stat path in
  Json.Arr [Json.int64 s.dev; Json.int64 s.ino; Json.int64 s.size; Json.int64 s.mtime_ns; Json.int64 s.ctime_ns]

let stamp_size = function Json.Arr [_; _; Json.Num s; _; _] -> int_of_string s | _ -> 0

(* The whole file, read once, into a buffer of its stat size (and on, should
   it have grown meanwhile). *)
let read_file path =
  let fd = Unix.openfile path [Unix.O_RDONLY; Unix.O_CLOEXEC] 0 in
  Fun.protect ~finally:(fun () -> Unix.close fd) (fun () ->
    let size = Int64.to_int (Unix.LargeFile.fstat fd).st_size in
    let buf = Bytes.create size in
    let rec fill off =
      if off = size then off else
      match Unix.read fd buf off (min (size - off) (1 lsl 24)) with
      | 0 -> off
      | n -> fill (off + n)
      | exception Unix.Unix_error (Unix.EINTR, _, _) -> fill off in
    let got = fill 0 in
    if got < size then Bytes.sub_string buf 0 got
    else begin
      let extra = Buffer.create 0 and chunk = Bytes.create 65536 in
      let rec more () = match Unix.read fd chunk 0 65536 with
        | 0 -> () | n -> Buffer.add_subbytes extra chunk 0 n; more ()
        | exception Unix.Unix_error (Unix.EINTR, _, _) -> more () in
      more ();
      if Buffer.length extra = 0 then Bytes.unsafe_to_string buf
      else Bytes.unsafe_to_string buf ^ Buffer.contents extra
    end)

(* ---- The decoder child, as decoded_status_isolated runs it. ---- *)

(* The directory holding compare_dng.py: DNG_COMPARE_DIR, else the nearest
   directory above this executable that holds it. *)
let compare_dir = lazy (
  match Sys.getenv_opt "DNG_COMPARE_DIR" with
  | Some d -> Some d
  | None ->
      let exe = try Unix.realpath Sys.executable_name with Unix.Unix_error _ -> Sys.executable_name in
      let rec up d =
        if Sys.file_exists (Filename.concat d "compare_dng.py") then Some d
        else let p = Filename.dirname d in if p = d then None else up p in
      up (Filename.dirname exe))

let python dir = match Sys.getenv_opt "DNG_COMPARE_PYTHON" with
  | Some p -> p
  | None -> Filename.concat dir ".venv/bin/python"

let decode_timeout = 1800.

let decoded_status_isolated left right group index =
  match Lazy.force compare_dir with
  | None -> "unverified", Some "decoder failed: compare_dng.py not found; set DNG_COMPARE_DIR"
  | Some dir ->
      let code = "import sys, compare_dng; compare_dng.decode_child(*sys.argv[1:4], int(sys.argv[4]))" in
      let out_r, out_w = Unix.pipe ~cloexec:true () and err_r, err_w = Unix.pipe ~cloexec:true () in
      let pid = Native.spawn_in dir [| python dir; "-c"; code; left; right; group; string_of_int index |] out_w err_w in
      Unix.close out_w; Unix.close err_w;
      let out = Buffer.create 256 and err = Buffer.create 256 in
      let buf = Bytes.create 65536 in
      let deadline = Unix.gettimeofday () +. decode_timeout in
      let rec drain fds =
        if fds = [] then true else
        let left_time = deadline -. Unix.gettimeofday () in
        if left_time <= 0. then false else
        match Unix.select fds [] [] left_time with
        | exception Unix.Unix_error (Unix.EINTR, _, _) -> drain fds
        | ready, _, _ ->
            let fds = List.filter (fun fd ->
              if not (List.mem fd ready) then true else
              let n = Unix.read fd buf 0 (Bytes.length buf) in
              if n = 0 then false
              else (Buffer.add_subbytes (if fd = out_r then out else err) buf 0 n; true)) fds in
            drain fds in
      let finished = drain [out_r; err_r] in
      Unix.close out_r; Unix.close err_r;
      if not finished then begin
        (try Unix.kill pid Sys.sigkill with Unix.Unix_error _ -> ());
        ignore (Native.waitpid_raw pid false);
        "unverified", Some "decoder timed out"
      end else
        let rc = Native.waitpid_raw pid false in
        if rc < 0 then "unverified", Some (Printf.sprintf "decoder crashed (signal %d)" (-rc))
        else
          let failed () =
            "unverified", Some ("decoder failed: " ^ Py.last_chars 500 (String.trim (Buffer.contents err))) in
          match List.rev (String.split_on_char '\n' (String.trim (Buffer.contents out))) with
          | last :: _ when String.trim last <> "" ->
              (match Json.parse last with
               | Json.Arr [Json.Str status; Json.Null] -> status, None
               | Json.Arr [Json.Str status; error] -> status, Some (Py.str error)
               | _ -> failed ()
               | exception _ -> failed ())
          | _ -> failed ()

(* ---- compare_pixels ---- *)

let group_status results =
  let statuses = List.map (fun r -> match Json.member "status" r with Some (Json.Str s) -> s | _ -> "") results in
  if results = [] || List.mem "unverified" statuses then "unverified"
  else if List.mem "different" statuses then "different" else "equal"

let compare_page fa fb a b size_a size_b (left, right, group, index) =
  let shape p = Json.Arr (List.map Json.int p.Tiff.shape) in
  let da, na = Tiff.payload_digest fa a size_a in
  let db, nb = Tiff.payload_digest fb b size_b in
  let base = [("left_shape", shape a); ("right_shape", shape b); ("left_sha256", jstr da);
              ("right_sha256", jstr db); ("bytes_read", Json.int (na + nb))] in
  if Tiff.signature fa a = Tiff.signature fb b && da = db then
    obj (base @ [("status", jstr "equal"); ("method", jstr "encoded_blocks")])
  else
    let status, error = decoded_status_isolated left right group index in
    obj (base @ [("status", jstr status); ("method", jstr (if error = None then "decoded_samples" else "decode_failed"))]
         @ (match error with Some e -> [("error", jstr e)] | None -> []))

(* [data] gives both files' bytes, read once; [before] is their stamps. *)
let compare_pixels left right ~data =
  let result = Odict.create () in
  Odict.set result "left" (jstr left);
  Odict.set result "right" (jstr right);
  (try
     let before = [stamp left; stamp right] in
     let data_l, data_r = Lazy.force data in
     let fa, pa0 = Tiff.open_file data_l in
     let fb, pb0 = Tiff.open_file data_r in
     let has_version = function Some (_, p) -> Hashtbl.mem p.Tiff.tags 50706 | None -> failwith "index 0 out of range(0)" in
     if not (has_version pa0) || not (has_version pb0) then failwith "Missing DNGVersion tag";
     let ra, xa = Tiff.page_groups fa pa0 in
     let rb, xb = Tiff.page_groups fb pb0 in
     let size_a = stamp_size (List.nth before 0) and size_b = stamp_size (List.nth before 1) in
     List.iter (fun (name, ga, gb) ->
       if List.length ga <> List.length gb then
         Odict.set result name (obj [("status", jstr "unverified"); ("error", jstr "Image count differs");
                                     ("left_count", Json.int (List.length ga)); ("right_count", Json.int (List.length gb))])
       else begin
         let images = List.mapi (fun i (a, b) -> compare_page fa fb a b size_a size_b (left, right, name, i))
             (List.combine ga gb) in
         let status = if images <> [] || name = "raw" then group_status images else "equal" in
         Odict.set result name (obj [("status", jstr status); ("images", Json.Arr images)])
       end) [("raw", ra, rb); ("auxiliary", xa, xb)];
     if before <> [stamp left; stamp right] then failwith "File changed during pixel comparison";
     Odict.set result "stamps" (Json.Arr before)
   with e ->
     let m = error_text e in
     Odict.set result "raw" (obj [("status", jstr "unverified"); ("error", jstr m)]);
     Odict.set result "error" (jstr m));
  result

(* ---- metadata ---- *)

let image_tags = ["PreviewImage"; "JpgFromRaw"; "ThumbnailImage"; "OtherImage"; "RawImage"; "OriginalRawFileData"]
let exiftool_command =
  ["exiftool"; "-j"; "-G1:4"; "-n"; "-struct"; "-b"; "-u"; "-api"; "LargeFileSupport=1"; "-all";
   "-DNGPrivateData"; "-MakerNotes"] @ List.concat_map (fun t -> ["-x"; t]) image_tags

type meta = Meta_error of string | Tags of (string * Json.t) list * string list

let last_component key = match List.rev (String.split_on_char ':' key) with c :: _ -> c | [] -> key

(* re.sub(r':Copy\d+(?=:)', '', key) *)
let strip_copy key =
  let n = String.length key in
  let b = Buffer.create n in
  let rec go i =
    if i >= n then ()
    else if i + 5 <= n && String.sub key i 5 = ":Copy" then begin
      let j = ref (i + 5) in
      while !j < n && key.[!j] >= '0' && key.[!j] <= '9' do incr j done;
      if !j > i + 5 && !j < n && key.[!j] = ':' then go !j
      else (Buffer.add_char b ':'; go (i + 1))
    end else (Buffer.add_char b key.[i]; go (i + 1)) in
  go 0;
  Buffer.contents b

let normalize_tags entry =
  let groups = Odict.create () in
  List.iter (fun (k, v) ->
    let k = strip_copy k in
    let prior = match Odict.get groups k with Some (Json.Arr l) -> l | _ -> [] in
    Odict.set groups k (Json.Arr (prior @ [v]))) entry;
  List.map (fun (k, vs) -> match vs with
    | Json.Arr [v] -> k, v
    | Json.Arr vs ->
        let keyed = List.map (fun v -> canonical v, v) vs in
        k, obj [("duplicate_values", Json.Arr (List.map snd (List.stable_sort (fun (a, _) (b, _) -> compare a b) keyed)))]
    | v -> k, v) (Odict.to_list groups)

let flatten entry =
  let out = Odict.create () in
  let rec go prefix = function
    | Json.Obj l -> List.iter (fun (k, v) -> go (if prefix = "" then k else prefix ^ "/" ^ k) v) l
    | v -> Odict.set out prefix v in
  go "" (obj entry);
  Odict.to_list out

let parse_exiftool stdout stderr =
  let entries = match Json.parse stdout with
    | Json.Arr l -> l
    | _ -> failwith ("ExifTool failed: " ^ Py.first_chars 1000 stderr)
    | exception _ -> failwith ("ExifTool failed: " ^ Py.first_chars 1000 stderr) in
  List.map (fun entry ->
    match Py.dedup entry with
    | Json.Obj fields ->
        let path = match List.assoc_opt "SourceFile" fields with Some v -> Py.str v | None -> failwith "'SourceFile'" in
        let fields = List.remove_assoc "SourceFile" fields in
        let pick tag = List.filter_map (fun (k, v) -> if last_component k = tag then Some (Py.str v) else None) fields in
        (match pick "Error" with
         | [] -> path, Tags (flatten (normalize_tags fields), pick "Warning")
         | errors -> path, Meta_error (String.concat "; " errors))
    | _ -> failwith "'str' object has no attribute 'pop'") entries

(* One ExifTool process over [paths], its output in temporary files. *)
type running = { pid : int; out : string; err : string }

let start_exiftool paths =
  let out = Filename.temp_file "dng-archive-exiftool" ".json" and err = Filename.temp_file "dng-archive-exiftool" ".err" in
  let fo = Unix.openfile out [Unix.O_WRONLY; Unix.O_TRUNC; Unix.O_CLOEXEC] 0o600 in
  let fe = Unix.openfile err [Unix.O_WRONLY; Unix.O_TRUNC; Unix.O_CLOEXEC] 0o600 in
  let devnull = Unix.openfile "/dev/null" [Unix.O_RDONLY; Unix.O_CLOEXEC] 0 in
  let pid = Fun.protect ~finally:(fun () -> Unix.close fo; Unix.close fe; Unix.close devnull) (fun () ->
    Unix.create_process "exiftool" (Array.of_list (exiftool_command @ paths)) devnull fo fe) in
  { pid; out; err }

let finish_exiftool r =
  ignore (Native.waitpid_raw r.pid false);
  let slurp p = let s = In_channel.with_open_bin p In_channel.input_all in Sys.remove p; s in
  let out = slurp r.out and err = slurp r.err in
  parse_exiftool out err

(* metadata_parallel: up to [processes] ExifTool processes over one batch. *)
let start_metadata paths processes =
  if processes <= 1 || List.length paths < 2 then [start_exiftool paths]
  else
    let n = List.length paths in
    let size = (n + processes - 1) / processes in
    let rec chunks l = if l = [] then [] else
        let rec take k l acc = if k = 0 || l = [] then List.rev acc, l else take (k - 1) (List.tl l) (List.hd l :: acc) in
        let c, rest = take size l [] in c :: chunks rest in
    List.map start_exiftool (chunks paths)

(* The batch's records, or the first failing chunk's error, as pool.map raises it. *)
let finish_metadata running =
  let results = List.map (fun r -> try Ok (finish_exiftool r) with e -> Error (error_text e)) running in
  match List.find_map (function Error e -> Some e | Ok _ -> None) results with
  | Some e -> Error e
  | None ->
      let tbl = Hashtbl.create 64 in
      List.iter (function Ok l -> List.iter (fun (p, m) -> Hashtbl.replace tbl p m) l | Error _ -> ()) results;
      Ok tbl

let layout = ["StripOffsets"; "StripByteCounts"; "TileOffsets"; "TileByteCounts"; "PreviewImageStart";
              "PreviewImageLength"; "JpgFromRawStart"; "JpgFromRawLength"; "ThumbnailOffset";
              "ThumbnailLength"; "FreeOffsets"; "FreeByteCounts"]

let compact v =
  let encoded = canonical v in
  if String.length encoded > 1024 then
    obj [("sha256", jstr (Native.sha_string encoded)); ("json_length", Json.int (String.length encoded));
         ("prefix", jstr (String.sub encoded 0 120))]
  else v

let compare_metadata a b =
  let ta = Hashtbl.create 256 and tb = Hashtbl.create 256 in
  List.iter (fun (k, v) -> Hashtbl.replace ta k v) a;
  List.iter (fun (k, v) -> Hashtbl.replace tb k v) b;
  let keys = List.sort_uniq compare (List.map fst a @ List.map fst b) in
  let embedded = ref [] and filesystem = ref [] and layout_changes = ref [] in
  List.iter (fun key ->
    let starts p = String.starts_with ~prefix:p key in
    let va = Hashtbl.find_opt ta key and vb = Hashtbl.find_opt tb key in
    let same = match va, vb with Some x, Some y -> Py.equal x y | _ -> false in
    let tag = last_component key in
    if starts "ExifTool:" || starts "Composite:" || same
       || (List.mem tag ["Directory"; "FileName"; "FileAccessDate"] && starts "System:") then ()
    else begin
      let kind = if va = None then "added" else if vb = None then "removed" else "changed" in
      let change = [("tag", jstr key); ("kind", jstr kind)]
                   @ (match va with Some v -> [("left", compact v)] | None -> [])
                   @ (match vb with Some v -> [("right", compact v)] | None -> []) in
      let into = if starts "System:" then filesystem else if List.mem tag layout then layout_changes else embedded in
      into := obj change :: !into
    end) keys;
  obj [("embedded", Json.Arr (List.rev !embedded)); ("filesystem", Json.Arr (List.rev !filesystem));
       ("layout", Json.Arr (List.rev !layout_changes))]

(* ---- Summary ---- *)

module Counter = struct
  type t = { mutable order : string list; tbl : (string, int) Hashtbl.t }
  let create () = { order = []; tbl = Hashtbl.create 16 }
  let add c k n =
    if not (Hashtbl.mem c.tbl k) then c.order <- k :: c.order;
    Hashtbl.replace c.tbl k (n + Option.value (Hashtbl.find_opt c.tbl k) ~default:0)
  let get c k = Option.value (Hashtbl.find_opt c.tbl k) ~default:0
  let items c = List.rev_map (fun k -> k, Hashtbl.find c.tbl k) c.order
end

type summary = {
  counts : Counter.t;
  tags : (string, int ref * Json.t list ref) Hashtbl.t;
  mutable tag_order : string list;
  patterns : Counter.t;
  pattern_values : (string, Json.t) Hashtbl.t;
  mutable bytes_read : int;
  coverage : (string, bool array) Hashtbl.t;
  mutable coverage_order : string list;
}

let summary () = { counts = Counter.create (); tags = Hashtbl.create 64; tag_order = []; patterns = Counter.create ();
                   pattern_values = Hashtbl.create 16; bytes_read = 0; coverage = Hashtbl.create 64; coverage_order = [] }

let get k r = Json.member k r
let get_str k r = match get k r with Some (Json.Str s) -> Some s | _ -> None
let list_of = function Some (Json.Arr l) -> l | _ -> []

let add s result =
  let left = Option.get (get_str "left" result) in
  let covered = match Hashtbl.find_opt s.coverage left with
    | Some c -> c
    | None -> let c = Array.make 3 false in Hashtbl.add s.coverage left c; s.coverage_order <- left :: s.coverage_order; c in
  let raw = Option.get (get "raw" result) in
  let raw_status = Option.value (get_str "status" raw) ~default:"" in
  let raw_equal = raw_status = "equal" in
  let aux_status = Option.bind (get "auxiliary" result) (get_str "status") in
  let metadata_status = get_str "metadata_status" result in
  let warnings = list_of (get "metadata_warnings" result) in
  let differences = get "differences" result in
  let embedded = list_of (Option.bind differences (get "embedded")) in
  covered.(0) <- covered.(0) || raw_equal;
  covered.(1) <- covered.(1) || (raw_equal && aux_status = Some "equal");
  covered.(2) <- covered.(2) || (raw_equal && metadata_status = Some "ok" && warnings = []
                                 && not (List.exists (fun c -> get_str "kind" c <> Some "added") embedded));
  Counter.add s.counts "pairs_reported" 1;
  Counter.add s.counts ("raw_" ^ raw_status) 1;
  Counter.add s.counts ("auxiliary_" ^ Option.value aux_status ~default:"unverified") 1;
  Counter.add s.counts ("metadata_" ^ Option.value metadata_status ~default:"error") 1;
  if warnings <> [] then Counter.add s.counts "metadata_with_warnings" 1;
  List.iter (fun g ->
    List.iter (fun p -> match get "bytes_read" p with
      | Some (Json.Num n) -> s.bytes_read <- s.bytes_read + int_of_string n | _ -> ())
      (list_of (Option.bind (get g result) (get "images")))) ["raw"; "auxiliary"];
  if embedded <> [] then Counter.add s.counts "embedded_metadata_different" 1;
  let pattern = List.map (fun c ->
    Option.value (get_str "kind" c) ~default:"" ^ ":" ^ Option.value (get_str "tag" c) ~default:"") embedded in
  if metadata_status = Some "ok" then begin
    let key = canonical (Json.Arr (List.map jstr pattern)) in
    Hashtbl.replace s.pattern_values key (Json.Arr (List.map jstr pattern));
    Counter.add s.patterns key 1
  end;
  (match differences with
   | Some (Json.Obj categories) ->
       List.iter (fun (category, changes) ->
         List.iter (fun change ->
           let key = Printf.sprintf "%s/%s:%s" category (Option.value (get_str "kind" change) ~default:"")
               (Option.value (get_str "tag" change) ~default:"") in
           let count, examples = match Hashtbl.find_opt s.tags key with
             | Some r -> r
             | None -> let r = ref 0, ref [] in Hashtbl.add s.tags key r; s.tag_order <- key :: s.tag_order; r in
           incr count;
           if List.length !examples < 3 then
             examples := !examples @ [(match change with Json.Obj l -> obj (l @ [("file", jstr left)]) | v -> v)])
           (list_of (Some changes))) categories
   | _ -> ())

let round2 x = float_of_string (Printf.sprintf "%.2f" x)

let write s path context ~complete ~elapsed =
  let covered k = Hashtbl.fold (fun _ c n -> if c.(k) then n + 1 else n) s.coverage 0 in
  let names = ["raw"; "all_images"; "metadata_preserved"] in
  let left_files = match List.assoc_opt "left_files" context with Some (Json.Num n) -> int_of_string n | _ -> 0 in
  let by_count l = List.stable_sort (fun (_, a) (_, b) -> compare b a) l in
  let report = context @ [
    ("complete", Json.Bool complete); ("elapsed_seconds", Json.float (round2 elapsed));
    ("left_files_covered", obj (List.mapi (fun i k -> k, Json.int (covered i)) names));
    ("left_subsumed_by_right", obj (List.mapi (fun i k -> k, Json.Bool (covered i = left_files)) names));
    ("counts", obj (List.map (fun (k, n) -> k, Json.int n) (Counter.items s.counts)));
    ("verified_payload_bytes", Json.int s.bytes_read);
    ("metadata_patterns", Json.Arr (List.map (fun (k, n) ->
       obj [("count", Json.int n); ("changes", Hashtbl.find s.pattern_values k)]) (by_count (Counter.items s.patterns))));
    ("metadata_tags", obj (List.map (fun (k, _) ->
       let count, examples = Hashtbl.find s.tags k in
       k, obj [("count", Json.int !count); ("examples", Json.Arr !examples)])
       (by_count (List.rev_map (fun k -> k, !(fst (Hashtbl.find s.tags k))) s.tag_order))))] in
  let temp = Filename.remove_extension path ^ ".tmp" in
  Out_channel.with_open_bin temp (fun oc -> output_string oc (Json.dumps_indent (obj report) ^ "\n"));
  Sys.rename temp path

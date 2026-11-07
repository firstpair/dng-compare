(* The TIFF/BigTIFF structure compare_dng.py reads through tifffile, from a
   file's bytes in memory: the pages it walks (the main IFD chain, each page
   followed depth first by its SubIFDs), and per page what compare_pixels uses
   of tifffile's TiffPage: subfiletype, photometric, shape, dataoffsets,
   databytecounts and the tag values of the encoding signature. Defaults,
   corrections and error texts follow tifffile 2026.9.20; its vendor-format
   special cases (LSM, STK, NDPI, Philips, Vista, ScanImage) are not
   reproduced, since DNGs are none of those. Every error is [Failure] with the
   text str(exc) gives in Python. *)

type value =
  | Raw of string                 (* BYTE, UNDEFINED: tifffile keeps the bytes *)
  | Text of string                (* ASCII, as tifffile strips it *)
  | Ints of int array
  | Floats of float array

type tag = { code : int; dtype : int; count : int; value : value; numpy : bool }

type file = {
  data : string; size : int; le : bool; big : bool;
  offsetsize : int; tagnosize : int; tagsize : int; threshold : int;
}

type page = {
  tags : (int, tag) Hashtbl.t;    (* the first tag of each code *)
  subfiletype : int;
  photometric : int;
  shape : int list;
  dataoffsets : int array;
  databytecounts : int array;
  subifds : int array option;     (* Some when tag 330 is present *)
}

let fail fmt = Printf.ksprintf failwith fmt

(* fh.read(n) at [off]: fewer bytes at the end of the file. *)
let read f off n =
  if off >= f.size || n <= 0 || off < 0 then ""
  else String.sub f.data off (min n (f.size - off))

let uint le s off n =
  let v = ref 0 in
  for k = 0 to n - 1 do
    let byte = Char.code s.[off + (if le then n - 1 - k else k)] in
    v := (!v lsl 8) lor byte
  done;
  !v

let signed v bits = if v land (1 lsl (bits - 1)) <> 0 then v - (1 lsl bits) else v

(* struct.unpack of one unsigned integer from exactly [n] bytes. *)
let unpack f s n =
  if String.length s < n then fail "unpack requires a buffer of %d bytes" n;
  uint f.le s 0 n

let tag_tuple = [273; 279; 282; 283; 324; 325; 330; 338; 513; 514; 530; 531; 34736; 50838]
let tag_readers = [301; 320; 33471; 33560; 33628; 33629; 33630; 33631; 33723; 33923; 34118; 34361;
                   34362; 34363; 34386; 34412; 34665; 34680; 34682; 34853; 37706; 37724; 40100;
                   40965; 43314; 50288; 50296; 50839; 51123; 51192; 65426; 65432; 65439]

(* Bytes per value of each TIFF data type, None for an invalid type. *)
let type_size = function
  | 1 | 2 | 6 | 7 -> Some 1 | 3 | 8 -> Some 2 | 4 | 9 | 11 | 13 -> Some 4
  | 5 | 10 | 12 | 16 | 17 | 18 -> Some 8 | _ -> None

let decode f dtype count raw =
  match dtype with
  | 1 | 7 -> Raw raw
  | 2 ->
      let n = ref (String.length raw) in
      while !n > 0 && raw.[!n - 1] = '\000' do decr n done;
      Text (Py.strip (String.sub raw 0 !n))
  | 11 -> Floats (Array.init count (fun i -> Int32.float_of_bits (Int32.of_int (uint f.le raw (4 * i) 4))))
  | 12 -> Floats (Array.init count (fun i ->
      let hi = uint f.le raw (8 * i + if f.le then 4 else 0) 4 and lo = uint f.le raw (8 * i + if f.le then 0 else 4) 4 in
      Int64.float_of_bits (Int64.logor (Int64.shift_left (Int64.of_int hi) 32) (Int64.of_int lo))))
  | _ ->
      let width, n, sign = match dtype with
        | 3 -> 2, count, false | 8 -> 2, count, true | 4 | 13 -> 4, count, false | 9 -> 4, count, true
        | 5 -> 4, 2 * count, false | 10 -> 4, 2 * count, true | 17 -> 8, count, true | _ -> 8, count, false in
      Ints (Array.init n (fun i ->
        let v = uint f.le raw (width * i) width in
        if sign && width < 8 then signed v (8 * width) else v))

(* One IFD entry; None where tifffile drops it (invalid type or value offset). *)
let read_tag f header =
  let code = uint f.le header 0 2 and dtype = uint f.le header 2 2 in
  let count = uint f.le header 4 (if f.big then 8 else 4) in
  let field = String.sub header (if f.big then 12 else 8) f.offsetsize in
  match type_size dtype with
  | None -> None
  | Some unit ->
      let valuesize = count * unit in
      let raw =
        if valuesize > f.threshold || List.mem code tag_readers then begin
          let valueoffset = uint f.le field 0 f.offsetsize in
          if valueoffset < 8 || valuesize < 0 || valueoffset + valuesize > f.size then None
          else Some (String.sub f.data valueoffset valuesize)
        end else Some (String.sub field 0 valuesize) in
      Option.map (fun raw ->
        let numpy = count > 1024 && not (List.mem code tag_tuple) && not (List.mem dtype [1; 2; 7]) in
        { code; dtype; count; value = decode f dtype count raw; numpy }) raw

let ints = function
  | Ints a -> a
  | Floats a -> Array.map int_of_float a
  | Raw s -> Array.init (String.length s) (fun i -> Char.code s.[i])
  | Text _ -> [||]

(* A tag value read as tifffile's scalar attribute: its first number. *)
let scalar t = let a = ints t.value in if Array.length a > 0 then Some a.(0) else None

let is_scalar t = match t.value with
  | Ints a -> Array.length a = 1 && not (List.mem t.code tag_tuple)
  | Floats a -> Array.length a = 1 && not (List.mem t.code tag_tuple)
  | _ -> false

(* jpeg_shape: (precision, height, width, components) of the SOF marker. *)
let jpeg_shape s =
  let n = String.length s in
  let be i k = if i + k > n then raise Exit else uint false s i k in
  let rec go i =
    if i >= n then raise Exit else
    let marker = be i 2 in
    let i = i + 2 in
    if marker = 0xffd8 || (marker >= 0xffd0 && marker <= 0xffd7) || marker = 0xff01 then go i
    else if marker = 0xffd9 then raise Exit
    else
      let length = be i 2 in
      let i = i + 2 in
      if marker >= 0xffc0 && marker <= 0xffc3 then (be i 1, be (i + 1) 2, be (i + 3) 2, be (i + 5) 1)
      else if marker = 0xffda then raise Exit
      else go (i + length - 2) in
  go 0

let product = List.fold_left ( * ) 1

let parse_page f offset =
  let tagno_s = read f offset f.tagnosize in
  if String.length tagno_s < f.tagnosize then fail "corrupted tag list @%d" offset;
  let tagno = uint f.le tagno_s 0 f.tagnosize in
  if tagno > 4096 then fail "suspicious number of tags %d" tagno;
  let data = read f (offset + f.tagnosize) (f.tagsize * tagno) in
  if String.length data <> f.tagsize * tagno then failwith "corrupted IFD structure";
  let tags = Hashtbl.create 32 in
  let ntags = ref 0 in
  for i = 0 to tagno - 1 do
    match read_tag f (String.sub data (i * f.tagsize) f.tagsize) with
    | Some t -> incr ntags; if not (Hashtbl.mem tags t.code) then Hashtbl.add tags t.code t
    | None -> ()
  done;
  let get code = Hashtbl.find_opt tags code in
  let has code = Hashtbl.mem tags code in
  let attr code default = match Option.bind (get code) scalar with Some v -> v | None -> default in
  let empty = { tags; subfiletype = 0; photometric = 0; shape = []; dataoffsets = [||];
                databytecounts = [||]; subifds = Option.map (fun t -> ints t.value) (get 330) } in
  if !ntags = 0 || (!ntags = 1 && has 52545) then empty else begin
    let subfiletype = match get 254 with
      | Some t when is_scalar t -> (ints t.value).(0)
      | Some _ -> 0
      | None -> 0 in
    let subfiletype =
      if subfiletype = 0 then (match attr 255 0 with 2 -> 1 | 3 -> 2 | _ -> 0) else subfiletype in
    let imagewidth = ref (attr 256 0) and imagelength = ref (attr 257 0) in
    let compression = ref (attr 259 1) and photometric = ref (attr 262 0) in
    let samplesperpixel = ref (attr 277 1) and rowsperstrip = ref (attr 278 (1 lsl 32 - 1)) in
    let planarconfig = attr 284 1 and imagedepth = attr 32997 1 in
    let bitspersample = ref (`Int 1) in
    let first_of codes = List.fold_left (fun acc c -> match acc with Some _ -> acc | None -> get c) None codes in
    let dataoffsets = ref (match first_of [324; 273; 513] with Some t -> ints t.value | None -> [||]) in
    let databytecounts = ref (match first_of [325; 279; 514] with Some t -> ints t.value | None -> [||]) in
    if List.mem !compression [2; 3; 4] && Array.length !dataoffsets = 1 && !dataoffsets.(0) > 0
       && (Array.length !databytecounts = 0 || !databytecounts.(0) = 0) then begin
      let o = !dataoffsets.(0) in
      let next = try unpack f (read f (offset + f.tagnosize + tagno * f.tagsize) f.offsetsize) f.offsetsize
        with Failure _ -> 0 in
      databytecounts := [| if next > o then next - o else f.size - o |]
    end;
    if !imagewidth = 0 && !imagelength = 0 && Array.length !dataoffsets > 0 && Array.length !databytecounts > 0 then begin
      match jpeg_shape (read f !dataoffsets.(0) (min !databytecounts.(0) 4096)) with
      | precision, length, width, spp ->
          imagelength := length; imagewidth := width; samplesperpixel := spp;
          if not (has 258) then bitspersample := `Int (if precision <= 8 then 8 else 16);
          if not (has 262) && spp = 3 then photometric := 6;
          if not (has 259) then compression := 6;
          if not (has 278) then rowsperstrip := length
      | exception _ -> ()
    end else if !compression = 6 then begin
      if not (has 262) then photometric := 6 else if !photometric = 2 then photometric := 6;
      if not (has 258) then bitspersample := `Int 8;
      if not (has 277) && List.mem !photometric [0; 1; 2; 6] then samplesperpixel := 3
    end;
    (match get 258 with
     | Some t when !bitspersample = `Int 1 ->
         let v = ints t.value in
         if t.count = 1 then (if Array.length v > 0 then bitspersample := `Int v.(0))
         else begin
           let v = Array.sub v 0 (min (Array.length v) (max 0 !samplesperpixel)) in
           if Array.length v = 0 then failwith "tuple index out of range";
           if Array.exists (fun x -> x <> v.(0)) v then bitspersample := `Tuple else bitspersample := `Int v.(0)
         end
     | _ -> ());
    if has 322 then rowsperstrip := 0
    else if has 257 then begin
      (match get 278 with None -> rowsperstrip := !imagelength | Some t when t.count > 1 -> rowsperstrip := !imagelength | _ -> ());
      rowsperstrip := min !rowsperstrip !imagelength
    end;
    let l = !imagelength and w = !imagewidth and s = !samplesperpixel in
    let shape =
      if !photometric = 2 || s > 1 then
        if planarconfig = 1 then (if imagedepth = 1 then [l; w; s] else [imagedepth; l; w; s])
        else (if imagedepth = 1 then [s; l; w] else [s; imagedepth; l; w])
      else if imagedepth = 1 then [l; w] else [imagedepth; l; w] in
    if Array.length !databytecounts = 0 then
      databytecounts := [| product shape * (match !bitspersample with
        | `Int b -> b / 8
        | `Tuple -> failwith "unsupported operand type(s) for //: 'tuple' and 'int'") |];
    if l > 0 && !rowsperstrip > 0 then begin
      let maxstrips = (l + !rowsperstrip - 1) / !rowsperstrip * imagedepth in
      let maxstrips = if planarconfig = 2 then maxstrips * s else maxstrips in
      let trim a = if Array.length a > maxstrips then Array.sub a 0 maxstrips else a in
      databytecounts := trim !databytecounts;
      dataoffsets := trim !dataoffsets
    end;
    { empty with subfiletype; photometric = !photometric; shape;
                 dataoffsets = !dataoffsets; databytecounts = !databytecounts }
  end

(* TiffFile(...): the header and the first page. *)
let open_file data =
  let size = String.length data in
  let f0 = { data; size; le = true; big = false; offsetsize = 4; tagnosize = 2; tagsize = 12; threshold = 4 } in
  let header = read f0 0 4 in
  let le = match if String.length header >= 2 then String.sub header 0 2 else header with
    | "II" | "EP" -> true | "MM" -> false
    | _ -> fail "not a TIFF file: header=%s" (Py.repr_bytes header) in
  let f0 = { f0 with le } in
  let version = unpack f0 (if String.length header > 2 then String.sub header 2 (String.length header - 2) else "") 2 in
  let f, first =
    if version = 43 then begin
      let s = read f0 4 4 in
      if String.length s < 4 then failwith "unpack requires a buffer of 4 bytes";
      let offsetsize = uint le s 0 2 and zero = uint le s 2 2 in
      if zero <> 0 || offsetsize <> 8 then fail "invalid BigTIFF offset size (%d, %d)" offsetsize zero;
      { f0 with big = true; offsetsize = 8; tagnosize = 8; tagsize = 20; threshold = 8 }, 8
    end else if List.mem version [42; 0x4352; 0x55; 0x4f52; 0x5352] then f0, 4
    else if version = 0x4e31 then (if not le then failwith "invalid NIFF file"; f0, 4)
    else fail "invalid TIFF version=%d" version in
  let offset = unpack f (read f first f.offsetsize) f.offsetsize in
  let page0 = if offset = 0 || offset >= f.size then None else Some (offset, parse_page f offset) in
  f, page0

(* Offset of the IFD after the one at [offset], as TiffPages._seek finds it. *)
let next_offset f offset =
  let s = read f offset f.tagnosize in
  if String.length s < f.tagnosize then None else
  let tagno = uint f.le s 0 f.tagnosize in
  if tagno > 4096 then None else
  let rest = read f (offset + f.tagnosize) (tagno * f.tagsize + f.offsetsize) in
  if String.length rest < f.offsetsize then None else
  let next = uint f.le rest (String.length rest - f.offsetsize) f.offsetsize in
  if next = 0 || next >= f.size then None else Some next

(* pages(tiff.pages): every page, each followed by its SubIFDs, depth first. *)
let all_pages f page0 =
  let out = ref [] in
  let rec visit page =
    out := page :: !out;
    match page.subifds with
    | Some offs when Array.length offs > 0 && offs.(0) <> 0 && offs.(0) < f.size ->
        Array.iter (fun o -> visit (parse_page f o)) offs
    | _ -> () in
  (match page0 with
   | None -> ()
   | Some (o0, p0) ->
       let seen = Hashtbl.create 8 in
       let rec chain o p =
         Hashtbl.replace seen o ();
         visit p;
         match next_offset f o with
         | Some n when not (Hashtbl.mem seen n) -> chain n (parse_page f n)
         | _ -> () in
       chain o0 p0);
  List.rev !out

let page_groups f page0 =
  List.partition (fun p -> p.subfiletype = 0 && List.mem p.photometric [0; 1; 2; 32803; 34892]) (all_pages f page0)

let encoding = [256; 257; 258; 259; 262; 266; 277; 278; 284; 317; 322; 323; 338; 339; 347; 512; 515; 517;
                518; 519; 520; 521; 529; 530; 531; 532; 50871; 50974; 52543; 52544]

(* Text equal exactly when tifffile's repr of the tag values is: integers of
   any width print alike, float and int do not, bytes and text are kept, and
   numpy arrays (over 1024 values) print their type, size and ends only. *)
let value_key t =
  let join f a = String.concat "," (Array.to_list (Array.map f a)) in
  let numpy_ends f a =
    let n = Array.length a in
    if n <= 6 then join f a else join f (Array.append (Array.sub a 0 3) (Array.sub a (n - 3) 3)) in
  match t.value with
  | Raw s -> "b" ^ s
  | Text s -> "s" ^ s
  | Ints a when t.numpy -> Printf.sprintf "n%d:%d:%s" t.dtype (Array.length a) (numpy_ends string_of_int a)
  | Floats a when t.numpy -> Printf.sprintf "n%d:%d:%s" t.dtype (Array.length a) (numpy_ends Json.py_float a)
  | Ints a -> "i" ^ join string_of_int a
  | Floats a -> "f" ^ join Json.py_float a

let signature f page =
  (if f.le then "<" else ">") :: List.filter_map (fun code ->
    Option.map (fun t -> Printf.sprintf "%d=%s" code (value_key t)) (Hashtbl.find_opt page.tags code)) encoding

(* payload_digest: SHA-256 over each block as an 8-byte little-endian length
   and the bytes; [size] is the stat size the blocks must lie within. *)
let payload_digest f page size =
  let offsets = page.dataoffsets and counts = page.databytecounts in
  if Array.length offsets = 0 || Array.length offsets <> Array.length counts then
    failwith "Image has no complete strip/tile directory";
  let c = Native.sha_init () in
  let total = ref 0 in
  let len = Bytes.create 8 in
  Array.iteri (fun i offset ->
    let count = counts.(i) in
    if offset <= 0 || count <= 0 || offset + count > size then
      fail "Invalid image block: offset=%d, length=%d" offset count;
    Bytes.set_int64_le len 0 (Int64.of_int count);
    Native.sha_update c len 0 8;
    if offset + count > f.size then failwith "Truncated image block";
    Native.sha_update c (Bytes.unsafe_of_string f.data) offset count;
    total := !total + count) offsets;
  Native.sha_final c, !total

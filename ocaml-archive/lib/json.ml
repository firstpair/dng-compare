(* JSON as dng-compare's Python reads and writes it. Numbers keep their source
   text; [dumps] prints them the way Python's json.dumps prints the value
   json.loads made of them, so a value's canonical text, and the SHA-256 the
   metadata index keys it by, match Python's exactly. *)

type t = Null | Bool of bool | Num of string | Str of string | Arr of t list | Obj of (string * t) list

exception Bad of string

let utf8 b code =
  let add n = Buffer.add_char b (Char.chr n) in
  if code < 0x80 then add code
  else if code < 0x800 then (add (0xc0 lor (code lsr 6)); add (0x80 lor (code land 0x3f)))
  else if code < 0x10000 then
    (add (0xe0 lor (code lsr 12)); add (0x80 lor ((code lsr 6) land 0x3f)); add (0x80 lor (code land 0x3f)))
  else
    (add (0xf0 lor (code lsr 18)); add (0x80 lor ((code lsr 12) land 0x3f));
     add (0x80 lor ((code lsr 6) land 0x3f)); add (0x80 lor (code land 0x3f)))

let parse text =
  let i = ref 0 and n = String.length text in
  let fail what = raise (Bad (Printf.sprintf "%s at byte %d" what !i)) in
  let peek () = if !i < n then text.[!i] else fail "unexpected end" in
  let skip () = while !i < n && (match text.[!i] with ' ' | '\t' | '\n' | '\r' -> true | _ -> false) do incr i done in
  let expect c = skip (); if peek () <> c then fail (Printf.sprintf "expected %c" c); incr i in
  let literal word v =
    if !i + String.length word <= n && String.sub text !i (String.length word) = word
    then (i := !i + String.length word; v) else fail "bad literal" in
  let hex4 () =
    if !i + 4 > n then fail "short \\u escape";
    let v = try int_of_string ("0x" ^ String.sub text !i 4) with Failure _ -> fail "bad \\u escape" in
    i := !i + 4; v in
  let string () =
    expect '"';
    let b = Buffer.create 32 in
    let rec loop () = match peek () with
      | '"' -> incr i
      | '\\' ->
          incr i;
          let c = peek () in incr i;
          (match c with
           | '"' | '\\' | '/' -> Buffer.add_char b c
           | 'b' -> Buffer.add_char b '\b' | 'f' -> Buffer.add_char b '\012'
           | 'n' -> Buffer.add_char b '\n' | 'r' -> Buffer.add_char b '\r' | 't' -> Buffer.add_char b '\t'
           | 'u' ->
               let hi = hex4 () in
               if hi >= 0xd800 && hi < 0xdc00 && !i + 6 <= n && text.[!i] = '\\' && text.[!i + 1] = 'u' then begin
                 let save = !i in
                 i := !i + 2;
                 let lo = hex4 () in
                 if lo >= 0xdc00 && lo < 0xe000 then utf8 b (0x10000 + ((hi - 0xd800) lsl 10) + (lo - 0xdc00))
                 else (i := save; utf8 b hi)
               end else utf8 b hi
           | _ -> fail "bad escape");
          loop ()
      | c -> Buffer.add_char b c; incr i; loop () in
    loop (); Buffer.contents b in
  let rec value () =
    skip ();
    match peek () with
    | '"' -> Str (string ())
    | '{' ->
        incr i; skip ();
        if peek () = '}' then (incr i; Obj []) else
        let rec fields acc =
          skip ();
          let k = string () in expect ':';
          let v = value () in
          skip ();
          match peek () with
          | ',' -> incr i; fields ((k, v) :: acc)
          | '}' -> incr i; Obj (List.rev ((k, v) :: acc))
          | _ -> fail "expected , or }" in
        fields []
    | '[' ->
        incr i; skip ();
        if peek () = ']' then (incr i; Arr []) else
        let rec items acc =
          let v = value () in
          skip ();
          match peek () with
          | ',' -> incr i; items (v :: acc)
          | ']' -> incr i; Arr (List.rev (v :: acc))
          | _ -> fail "expected , or ]" in
        items []
    | 't' -> literal "true" (Bool true)
    | 'f' -> literal "false" (Bool false)
    | 'n' -> literal "null" Null
    | 'N' -> literal "NaN" (Num "NaN")
    | 'I' -> literal "Infinity" (Num "Infinity")
    | '-' when !i + 1 < n && text.[!i + 1] = 'I' -> incr i; literal "Infinity" (Num "-Infinity")
    | '-' | '0'..'9' ->
        let start = !i in
        while !i < n && (match text.[!i] with '-' | '+' | '.' | 'e' | 'E' | '0'..'9' -> true | _ -> false) do incr i done;
        Num (String.sub text start (!i - start))
    | _ -> fail "unexpected character" in
  let v = value () in
  skip ();
  if !i <> n then fail "trailing data";
  v

(* Python's repr of a float: the shortest digits that read back exactly,
   fixed notation for 1e-4 <= |x| < 1e16, otherwise d.ddde+XX. *)
let py_float x =
  if Float.is_nan x then "NaN"
  else if x = Float.infinity then "Infinity"
  else if x = Float.neg_infinity then "-Infinity"
  else begin
    let rec shortest p =
      let s = Printf.sprintf "%.*e" (p - 1) x in
      if p >= 17 || float_of_string s = x then s else shortest (p + 1) in
    let s = shortest 1 in
    let neg = s.[0] = '-' in
    let s = if neg then String.sub s 1 (String.length s - 1) else s in
    let e = String.index s 'e' in
    let mant = String.sub s 0 e and exp = int_of_string (String.sub s (e + 1) (String.length s - e - 1)) in
    let digits = String.concat "" (String.split_on_char '.' mant) in
    (* Trailing zeros only appear for 0.0; keep one digit. *)
    let digits =
      let n = ref (String.length digits) in
      while !n > 1 && digits.[!n - 1] = '0' do decr n done;
      String.sub digits 0 !n in
    let nd = String.length digits and decpt = exp + 1 in
    let body =
      if decpt > -4 && decpt <= 16 then
        if decpt <= 0 then "0." ^ String.make (-decpt) '0' ^ digits
        else if decpt >= nd then digits ^ String.make (decpt - nd) '0' ^ ".0"
        else String.sub digits 0 decpt ^ "." ^ String.sub digits decpt (nd - decpt)
      else
        let m = if nd = 1 then digits else String.sub digits 0 1 ^ "." ^ String.sub digits 1 (nd - 1) in
        Printf.sprintf "%se%c%02d" m (if exp < 0 then '-' else '+') (abs exp) in
    (if neg then "-" else "") ^ body
  end

let is_integer s = String.for_all (function '-' | '0'..'9' -> true | _ -> false) s

(* What json.dumps prints for the number json.loads read from [s]. *)
let py_number s =
  match s with
  | "NaN" | "Infinity" | "-Infinity" -> s
  | _ when is_integer s ->
      let neg = s.[0] = '-' in
      let d = if neg then String.sub s 1 (String.length s - 1) else s in
      let k = ref 0 in
      while !k < String.length d - 1 && d.[!k] = '0' do incr k done;
      let d = String.sub d !k (String.length d - !k) in
      if d = "0" then "0" else (if neg then "-" else "") ^ d
  | _ -> py_float (float_of_string s)

(* ensure_ascii: ASCII control characters and every non-ASCII code point as
   \uXXXX, astral ones as a surrogate pair, like Python's json module. *)
let add_string b s =
  Buffer.add_char b '"';
  let n = String.length s and i = ref 0 in
  let u code = Buffer.add_string b (Printf.sprintf "\\u%04x" code) in
  while !i < n do
    let c = Char.code s.[!i] in
    if c < 0x80 then begin
      (match s.[!i] with
       | '"' -> Buffer.add_string b "\\\"" | '\\' -> Buffer.add_string b "\\\\"
       | '\n' -> Buffer.add_string b "\\n" | '\r' -> Buffer.add_string b "\\r"
       | '\t' -> Buffer.add_string b "\\t" | '\b' -> Buffer.add_string b "\\b"
       | '\012' -> Buffer.add_string b "\\f"
       | ch when c < 0x20 -> ignore ch; u c
       | ch -> Buffer.add_char b ch);
      incr i
    end else begin
      let d = String.get_utf_8_uchar s !i in
      let code = if Uchar.utf_decode_is_valid d then Uchar.to_int (Uchar.utf_decode_uchar d) else 0xfffd in
      if code >= 0x10000 then begin
        let v = code - 0x10000 in u (0xd800 lor (v lsr 10)); u (0xdc00 lor (v land 0x3ff))
      end else u code;
      i := !i + Uchar.utf_decode_length d
    end
  done;
  Buffer.add_char b '"'

(* json.dumps(v, ensure_ascii=True) with Python's separators: [compact] gives
   (',', ':'), otherwise the default (', ', ': '); [sort_keys] orders keys by
   code point, which is UTF-8 byte order. *)
let dumps ?(compact = false) ?(sort_keys = false) v =
  let b = Buffer.create 256 in
  let item, colon = if compact then ",", ":" else ", ", ": " in
  let rec go = function
    | Null -> Buffer.add_string b "null"
    | Bool true -> Buffer.add_string b "true"
    | Bool false -> Buffer.add_string b "false"
    | Num s -> Buffer.add_string b (py_number s)
    | Str s -> add_string b s
    | Arr l ->
        Buffer.add_char b '[';
        List.iteri (fun k x -> if k > 0 then Buffer.add_string b item; go x) l;
        Buffer.add_char b ']'
    | Obj l ->
        let l = if sort_keys then List.stable_sort (fun (a, _) (c, _) -> compare a c) l else l in
        Buffer.add_char b '{';
        List.iteri (fun k (key, x) ->
          if k > 0 then Buffer.add_string b item;
          add_string b key; Buffer.add_string b colon; go x) l;
        Buffer.add_char b '}' in
  go v;
  Buffer.contents b

(* json.dump(v, indent=2): the layout of the archive's summary and inventory files. *)
let dumps_indent v =
  let b = Buffer.create 1024 in
  let rec go depth = function
    | Arr [] -> Buffer.add_string b "[]"
    | Obj [] -> Buffer.add_string b "{}"
    | Arr l ->
        Buffer.add_char b '[';
        List.iteri (fun k x ->
          Buffer.add_string b (if k > 0 then ",\n" else "\n");
          Buffer.add_string b (String.make (2 * (depth + 1)) ' '); go (depth + 1) x) l;
        Buffer.add_string b ("\n" ^ String.make (2 * depth) ' ' ^ "]")
    | Obj l ->
        Buffer.add_char b '{';
        List.iteri (fun k (key, x) ->
          Buffer.add_string b (if k > 0 then ",\n" else "\n");
          Buffer.add_string b (String.make (2 * (depth + 1)) ' ');
          add_string b key; Buffer.add_string b ": "; go (depth + 1) x) l;
        Buffer.add_string b ("\n" ^ String.make (2 * depth) ' ' ^ "}")
    | v -> Buffer.add_string b (dumps v) in
  go 0 v;
  Buffer.contents b

let member k = function Obj l -> List.assoc_opt k l | _ -> None
let str = function Str s -> s | _ -> raise (Bad "expected a string")
let int64 n = Num (Int64.to_string n)
let int n = Num (string_of_int n)
let float x = Num (py_float x)
let opt f = function None -> Null | Some x -> f x
(* Python truthiness, for r['bumer'] and similar tests. *)
let truthy = function
  | Null | Bool false | Str "" | Arr [] | Obj [] -> false
  | Num s -> (match float_of_string_opt s with Some 0. -> false | _ -> true)
  | _ -> true

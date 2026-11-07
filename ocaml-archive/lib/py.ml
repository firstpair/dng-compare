(* Python's text for the values and errors that compare_dng.py writes into its
   reports: str() and repr() of JSON values, bytes reprs, OSError messages and
   the {:,} thousands format. *)

(* repr of a str: single quotes unless only double quotes avoid escaping. *)
let repr_string ?(prefix = "") ~bytes s =
  let quote = if String.contains s '\'' && not (String.contains s '"') then '"' else '\'' in
  let b = Buffer.create (String.length s + 3) in
  Buffer.add_string b prefix;
  Buffer.add_char b quote;
  String.iter (fun c ->
    match c with
    | '\\' -> Buffer.add_string b "\\\\"
    | '\n' -> Buffer.add_string b "\\n"
    | '\r' -> Buffer.add_string b "\\r"
    | '\t' -> Buffer.add_string b "\\t"
    | c when c = quote -> Buffer.add_char b '\\'; Buffer.add_char b c
    | c when Char.code c < 0x20 || Char.code c = 0x7f || (bytes && Char.code c >= 0x80) ->
        Buffer.add_string b (Printf.sprintf "\\x%02x" (Char.code c))
    | c -> Buffer.add_char b c) s;
  Buffer.add_char b quote;
  Buffer.contents b

let repr_bytes s = repr_string ~prefix:"b" ~bytes:true s

let rec repr = function
  | Json.Null -> "None"
  | Json.Bool true -> "True"
  | Json.Bool false -> "False"
  | Json.Num s -> Json.py_number s
  | Json.Str s -> repr_string ~bytes:false s
  | Json.Arr l -> "[" ^ String.concat ", " (List.map repr l) ^ "]"
  | Json.Obj l -> "{" ^ String.concat ", " (List.map (fun (k, v) -> repr_string ~bytes:false k ^ ": " ^ repr v) l) ^ "}"

(* str(v) of a value json.loads made. *)
let str = function Json.Str s -> s | v -> repr v

(* str(OSError): "[Errno 2] No such file or directory: '/a/b'". *)
let oserror e path =
  Printf.sprintf "[Errno %d] %s: %s" (Native.errno e) (Unix.error_message e) (repr_string ~bytes:false path)

(* f'{n:,}' *)
let thousands n =
  let s = string_of_int (abs n) in
  let len = String.length s in
  let b = Buffer.create (len + len / 3 + 1) in
  if n < 0 then Buffer.add_char b '-';
  String.iteri (fun i c ->
    if i > 0 && (len - i) mod 3 = 0 then Buffer.add_char b ',';
    Buffer.add_char b c) s;
  Buffer.contents b

(* The last [n] code points of a UTF-8 string, as Python's s[-n:]. *)
let last_chars n s =
  let len = String.length s in
  let rec back i count =
    if i <= 0 || count = n then i
    else
      let j = ref (i - 1) in
      while !j > 0 && Char.code s.[!j] land 0xc0 = 0x80 do decr j done;
      back !j (count + 1) in
  let start = back len 0 in
  String.sub s start (len - start)

(* The first [n] code points, as s[:n]. *)
let first_chars n s =
  let len = String.length s in
  let rec fwd i count =
    if i >= len || count = n then i
    else
      let j = ref (i + 1) in
      while !j < len && Char.code s.[!j] land 0xc0 = 0x80 do incr j done;
      fwd !j (count + 1) in
  String.sub s 0 (fwd 0 0)

(* str.strip() for the ASCII whitespace Python strips. *)
let strip s =
  let ws c = c = ' ' || (c >= '\t' && c <= '\r') || (c >= '\x1c' && c <= '\x1f') in
  let n = String.length s in
  let i = ref 0 and j = ref n in
  while !i < n && ws s.[!i] do incr i done;
  while !j > !i && ws s.[!j - 1] do decr j done;
  String.sub s !i (!j - !i)

(* Python == on json.loads values: dicts ignore key order, True == 1, ints and
   floats compare by value, NaN equals nothing. *)
let rec equal a b =
  let number = function
    | Json.Num s -> Some s | Json.Bool true -> Some "1" | Json.Bool false -> Some "0" | _ -> None in
  match a, b with
  | Json.Null, Json.Null -> true
  | Json.Str x, Json.Str y -> x = y
  | Json.Arr x, Json.Arr y -> List.length x = List.length y && List.for_all2 equal x y
  | Json.Obj x, Json.Obj y ->
      List.length x = List.length y
      && List.for_all (fun (k, v) -> match List.assoc_opt k y with Some w -> equal v w | None -> false) x
  | _ ->
      match number a, number b with
      | Some x, Some y ->
          if Json.is_integer x && Json.is_integer y then Json.py_number x = Json.py_number y
          else
            let f = float_of_string x and g = float_of_string y in
            (not (Float.is_nan f)) && f = g
      | _ -> false

(* json.loads keeps the last value of a repeated key, at the key's first place. *)
let rec dedup = function
  | Json.Obj l ->
      let seen = Hashtbl.create 16 in
      List.iter (fun (k, v) -> Hashtbl.replace seen k v) l;
      Json.Obj (List.filter_map (fun (k, _) ->
        match Hashtbl.find_opt seen k with
        | Some v -> Hashtbl.remove seen k; Some (k, dedup v)
        | None -> None) l)
  | Json.Arr l -> Json.Arr (List.map dedup l)
  | v -> v

(* dng-compare metadata worker over the in-process exiftool-rs engine.
   usage: metadata-extract-exiftool-rs-oxcaml WORKERS [--engine exiftool-rs]
          metadata-extract-exiftool-rs-oxcaml --engine-version [exiftool-rs]
   NUL-delimited paths on stdin; a JSON array of records in input order on stdout. *)
let usage = "usage: metadata-extract-exiftool-rs-oxcaml WORKERS [--engine exiftool-rs] | --engine-version [exiftool-rs]"

let read_all ic =
  let b = Buffer.create 4096 and bytes = Bytes.create 65536 in
  let rec loop () = match input ic bytes 0 (Bytes.length bytes) with
    | 0 -> Buffer.contents b
    | n -> Buffer.add_subbytes b bytes 0 n; loop () in
  loop ()

let reject engine = failwith ("this worker only runs the exiftool-rs engine, not " ^ engine)

let main () =
  let workers = match List.tl (Array.to_list Sys.argv) with
    | ["--engine-version"] | ["--engine-version"; "exiftool-rs"] ->
      print_endline (Dng_exiftool_rs.version ()); exit 0
    | ["--engine-version"; engine] -> reject engine
    | [w] | [w; "--engine"; "exiftool-rs"] -> (try int_of_string w with Failure _ -> failwith usage)
    | [_; "--engine"; engine] -> reject engine
    | _ -> failwith usage in
  if workers < 1 || workers > 36 then invalid_arg "workers must be 1..36";
  let paths = read_all stdin |> String.split_on_char '\000' |> List.filter (( <> ) "") in
  let records = Dng_exiftool_rs.extract_all ~workers paths in
  print_string "[";
  List.iteri (fun i record -> if i > 0 then print_string ","; print_string record) records;
  print_string "]\n"; flush stdout

let () = try main () with e -> prerr_endline (Printexc.to_string e); exit 1

(* dng-archive: the OCaml command for dng-compare archives.

     dng-archive index ARCHIVE
     dng-archive preserve SOURCE REFERENCE ARCHIVE --pairs FILE [--delta] [--full-copy]
                          [--workers N] [--rate-mib R]
     dng-archive restore ARCHIVE RELATIVE_PATH OUTPUT [--reference-root DIR]

   index is metadata_index.py; preserve and restore are preserve_apo.py's
   scan --pairs and restore, on the same archive files. *)

open Dng_archive

let usage () =
  prerr_endline "usage: dng-archive index ARCHIVE\n\
                \       dng-archive preserve SOURCE REFERENCE ARCHIVE --pairs FILE [--delta] [--full-copy] [--workers N] [--rate-mib R]\n\
                \       dng-archive restore ARCHIVE RELATIVE_PATH OUTPUT [--reference-root DIR]";
  exit 2

(* Positional arguments and --flag [value] options. *)
let split args valued =
  let rec go pos opts = function
    | [] -> List.rev pos, opts
    | f :: v :: rest when List.mem f valued -> go pos ((f, v) :: opts) rest
    | f :: _ when List.mem f valued -> Printf.eprintf "%s needs a value\n" f; usage ()
    | f :: rest when String.length f > 2 && String.sub f 0 2 = "--" -> go pos ((f, "") :: opts) rest
    | a :: rest -> go (a :: pos) opts rest in
  go [] [] args

let real p = try Unix.realpath p with Unix.Unix_error (e, _, _) ->
  Printf.eprintf "%s: %s\n" p (Unix.error_message e); exit 2

let main () =
  match List.tl (Array.to_list Sys.argv) with
  | "index" :: rest ->
      (match split rest [] with [archive], [] -> Index.index_archive archive | _ -> usage ())
  | "preserve" :: rest ->
      let pos, opts = split rest ["--pairs"; "--workers"; "--rate-mib"] in
      let known = ["--pairs"; "--workers"; "--rate-mib"; "--delta"; "--full-copy"] in
      List.iter (fun (f, _) -> if not (List.mem f known) then (Printf.eprintf "unknown option %s\n" f; usage ())) opts;
      (match pos, List.assoc_opt "--pairs" opts with
       | [source; reference; archive], Some pairs ->
           let workers = int_of_string (Option.value (List.assoc_opt "--workers" opts) ~default:"1") in
           let rate = float_of_string (Option.value (List.assoc_opt "--rate-mib" opts) ~default:"200") in
           (* Listed paths resolve like the roots they must lie under. *)
           let pairs = List.map (fun (s, r) -> real s, Option.map real r) (Preserve.load_pairs pairs) in
           if not (Preserve.scan ~source:(real source) ~reference:(real reference) ~archive ~rate
                     ~full_copy:(List.mem_assoc "--full-copy" opts) ~delta_mode:(List.mem_assoc "--delta" opts)
                     ~workers ~pairs) then begin
             prerr_endline "Preservation incomplete; inspect errors and rerun before deleting source";
             exit 1
           end
       | _ -> usage ())
  | "restore" :: rest ->
      (match split rest ["--reference-root"] with
       | [archive; rel; output], opts ->
           Preserve.restore ?reference_root:(List.assoc_opt "--reference-root" opts) archive rel output
       | _ -> usage ())
  | _ -> usage ()

let () =
  try main () with
  | Failure e | Sys_error e | Json.Bad e -> prerr_endline ("dng-archive: " ^ e); exit 1
  | Unix.Unix_error (e, f, a) -> Printf.eprintf "dng-archive: %s: %s %s\n" f (Unix.error_message e) a; exit 1

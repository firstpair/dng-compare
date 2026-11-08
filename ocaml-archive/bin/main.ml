(* dng-archive: the OCaml command for dng-compare archives.

     dng-archive index ARCHIVE
     dng-archive preserve SOURCE REFERENCE ARCHIVE --pairs FILE [--delta] [--full-copy]
                          [--workers N] [--rate-mib R] [--source-index FILE --reference-index FILE]
     dng-archive restore ARCHIVE RELATIVE_PATH OUTPUT [--reference-root DIR]
     dng-archive all LEFT RIGHT --pairs FILE --report DIR [--archive DIR] [options]
     dng-archive phindex-metadata LEFT RIGHT ARCHIVE --pairs FILE --left-index FILE --right-index FILE
                                  [--batch-size N] [--progress-secs S]

   all is compare_dng.py --pairs-only and preserve_apo.py scan --delta --pairs
   in one pass (docs/native-all.md); index is metadata_index.py; preserve and restore are preserve_apo.py's
   scan --pairs and restore, on the same archive files; phindex-metadata is phindex_metadata.py. *)

open Dng_archive

let usage () =
  prerr_endline "usage: dng-archive index ARCHIVE\n\
                \       dng-archive preserve SOURCE REFERENCE ARCHIVE --pairs FILE [--delta] [--full-copy] [--workers N] [--rate-mib R] [--source-index FILE --reference-index FILE]\n\
                \       dng-archive restore ARCHIVE RELATIVE_PATH OUTPUT [--reference-root DIR]\n\
                \       dng-archive all LEFT RIGHT --pairs FILE --report DIR [--archive DIR] [--left-index FILE --right-index FILE] [--workers N] [--batch-size N] [--exiftool-processes N] [--progress-secs S] [--rate-mib R] [--fresh] [--compare-only] [--trust-pairs] [--skip-metadata]\n\
                \       dng-archive phindex-metadata LEFT RIGHT ARCHIVE --pairs FILE --left-index FILE --right-index FILE [--batch-size N] [--progress-secs S]";
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
      let valued = ["--pairs"; "--workers"; "--rate-mib"; "--source-index"; "--reference-index"] in
      let pos, opts = split rest valued in
      let known = valued @ ["--delta"; "--full-copy"] in
      List.iter (fun (f, _) -> if not (List.mem f known) then (Printf.eprintf "unknown option %s\n" f; usage ())) opts;
      (match pos, List.assoc_opt "--pairs" opts with
       | [source; reference; archive], Some pairs ->
           let workers = int_of_string (Option.value (List.assoc_opt "--workers" opts) ~default:"1") in
           let rate = float_of_string (Option.value (List.assoc_opt "--rate-mib" opts) ~default:"200") in
           (* Listed paths resolve like the roots they must lie under. *)
           let pairs = List.map (fun (s, r) -> real s, Option.map real r) (Preserve.load_pairs pairs) in
           (* preserve_apo.py's --source-index/--reference-index: identical files
              whose phindex rows are current are not read. *)
           let indexes = match List.assoc_opt "--source-index" opts, List.assoc_opt "--reference-index" opts with
             | Some a, Some b -> Some { Preserve.source_index = Phindex.open_ a; reference_index = Phindex.open_ b }
             | None, None -> None
             | _ -> prerr_endline "--source-index and --reference-index go together"; usage () in
           if not (Preserve.scan ?indexes ~source:(real source) ~reference:(real reference) ~archive ~rate
                     ~full_copy:(List.mem_assoc "--full-copy" opts) ~delta_mode:(List.mem_assoc "--delta" opts)
                     ~workers ~pairs ()) then begin
             prerr_endline "Preservation incomplete; inspect errors and rerun before deleting source";
             exit 1
           end
       | _ -> usage ())
  | "all" :: rest -> exit (All.run rest)
  | "phindex-metadata" :: rest -> exit (Phindex_metadata.run rest)
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

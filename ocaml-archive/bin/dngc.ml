(* dngc: one command for dng-compare's comparison, preservation and metadata
   tools, native (docs/dngc.md). The implementation is Dng_archive.Dngc_cli. *)

let () = exit (Dng_archive.Dngc_cli.main (List.tl (Array.to_list Sys.argv)))

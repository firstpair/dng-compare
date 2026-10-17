(* Emit the dune c_library_flags sexp that links libdng_exiftool_ffi, and the
   absolute path of the static library for the copy rule in lib/dune.
   DNG_EXIFTOOL_FFI_LIBDIR: directory holding the Rust artifacts
   (default: <project>/../rust-scanner/target/release).
   DNG_EXIFTOOL_FFI_LINK: "static" (default; the library is copied into the
   build tree and linked by path, so executables are self-contained and relink
   when it changes) or "dynamic" (shared library plus an rpath to the directory). *)
let absolute path =
  if Filename.is_relative path then Filename.concat (Sys.getcwd ()) path else path

let rec normalize path =
  let dir = Filename.dirname path and base = Filename.basename path in
  if dir = path then path
  else if base = Filename.current_dir_name then normalize dir
  else if base = Filename.parent_dir_name then Filename.dirname (normalize dir)
  else Filename.concat (normalize dir) base

let () =
  let project_root = match Sys.argv with [| _; root |] -> root | _ -> failwith "usage: library_flags PROJECT_ROOT" in
  (* The rule runs inside _build/default/lib; the source project is two levels above _build/default. *)
  let project = normalize (Filename.concat (absolute project_root) "../..") in
  let libdir = match Sys.getenv_opt "DNG_EXIFTOOL_FFI_LIBDIR" with
    | Some dir when dir <> "" -> normalize (absolute dir)
    | _ -> normalize (Filename.concat project "../rust-scanner/target/release") in
  let link = Option.value (Sys.getenv_opt "DNG_EXIFTOOL_FFI_LINK") ~default:"static" in
  let macos = Sys.os_type = "Unix" && (try let ic = Unix.open_process_in "uname -s" in
      let line = input_line ic in ignore (Unix.close_process_in ic); line = "Darwin" with _ -> false) in
  let system_libs = if macos then ["-liconv"; "-lc"; "-lm"]
                    else ["-lgcc_s"; "-lutil"; "-lrt"; "-lpthread"; "-lm"; "-ldl"; "-lc"] in
  (* Executables link from _build/default, where the tracked copy is lib/libdng_exiftool_ffi.a. *)
  let flags, library_path = match link with
    | "static" -> "lib/libdng_exiftool_ffi.a" :: system_libs, Filename.concat libdir "libdng_exiftool_ffi.a"
    | "dynamic" -> ["-L" ^ libdir; "-ldng_exiftool_ffi"; "-Wl,-rpath," ^ libdir], ""
    | other -> failwith ("DNG_EXIFTOOL_FFI_LINK must be static or dynamic, not " ^ other) in
  let oc = open_out "library_flags.sexp" in
  output_string oc "("; List.iter (fun f -> Printf.fprintf oc "%S " f) flags; output_string oc ")\n"; close_out oc;
  let oc = open_out "library_path.txt" in output_string oc library_path; close_out oc

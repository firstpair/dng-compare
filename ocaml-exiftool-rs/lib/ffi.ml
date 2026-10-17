(* Direct bindings to the dng-exiftool-ffi C ABI. *)
external version : unit -> string = "dng_exiftool_rs_version_stub"
external extract_json : string -> string = "dng_exiftool_rs_extract_json_stub"

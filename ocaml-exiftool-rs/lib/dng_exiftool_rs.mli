(** In-process metadata extraction through the exiftool-rs engine C ABI
    (dng-exiftool-ffi). Records are exiftool-rs metadata: one ExifTool-shaped
    JSON object per file, a separate identity from Perl ExifTool output. *)

val version : unit -> string
(** Engine identity, e.g. ["exiftool-rs 0.10.0 (ExifTool 13.59 tables)"]. *)

val backend : string
(** Parallel pool compiled in: ["domains-stock"] (OCaml 5 [Domain.spawn]) or
    ["domains-oxcaml"] (OxCaml [Domain.Safe.spawn] with portable closures). *)

val recommended_workers : unit -> int
(** The runtime's recommended domain count; [extract_all] never exceeds it. *)

val extract_json : string -> string
(** One JSON object for the path. Extraction failures are reported inside the
    record under ["ExifTool:Error"]. Raises [Failure] only when the engine
    itself fails (a panic or invalid arguments). *)

val extract_all : workers:int -> string list -> string list
(** Records in input order, extracted in parallel by [min workers paths]
    domains, capped at [recommended_workers ()]. Raises [Failure] when any
    extraction raised, so a batch never silently loses a record. *)

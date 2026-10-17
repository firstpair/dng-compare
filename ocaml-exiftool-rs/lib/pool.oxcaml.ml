(* OxCaml pool: Domain.Safe.spawn requires portable closures, so workers share
   only an immutable path array and an atomic counter, and each returns its own
   (index, result) list. The domain-count alert is acknowledged: callers are
   capped at recommended_domain_count by the library. *)
module Iarray = Stdlib_stable.Iarray
let backend = "domains-oxcaml"
let recommended () = Domain.recommended_domain_count ()

(* Declared here with the portable modality so the worker closure may call it;
   Ffi keeps the stock-syntax declaration of the same stub. *)
external extract_json : string -> string @@ portable = "dng_exiftool_rs_extract_json_stub"
let extract (path : string) = try Ok (extract_json path) with Failure message -> Error message

let run ~workers (paths : string array) : (int * (string, string) result) list =
  let paths : string iarray = Iarray.of_array paths in
  let next : int Atomic.t = Atomic.make 0 in
  let work () =
    let rec loop acc =
      let i = Atomic.fetch_and_add next 1 in
      if i < Iarray.length paths then loop ((i, extract (Iarray.get paths i)) :: acc) else acc in
    loop [] in
  List.init workers (fun _ -> (Domain.Safe.spawn [@alert "-do_not_spawn_domains"]) work)
  |> List.concat_map Domain.join

(* Stock OCaml 5 pool: Domain.spawn workers pull indexes from an atomic counter
   and each returns its own (index, result) list, so no array is shared. *)
let backend = "domains-stock"
let recommended () = Domain.recommended_domain_count ()

let extract path = try Ok (Ffi.extract_json path) with Failure message -> Error message

let run ~workers (paths : string array) : (int * (string, string) result) list =
  let next = Atomic.make 0 in
  let work () =
    let rec loop acc =
      let i = Atomic.fetch_and_add next 1 in
      if i < Array.length paths then loop ((i, extract paths.(i)) :: acc) else acc in
    loop [] in
  List.init workers (fun _ -> Domain.spawn work) |> List.concat_map Domain.join

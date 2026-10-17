let version = Ffi.version
let backend = Pool.backend
let recommended_workers = Pool.recommended
let extract_json = Ffi.extract_json

let extract_all ~workers paths =
  if workers < 1 then invalid_arg "workers must be positive";
  let paths = Array.of_list paths in
  let n = Array.length paths in
  if n = 0 then [] else begin
    let workers = min workers (min n (max 1 (Pool.recommended ()))) in
    let results = Array.make n None in
    List.iter (fun (i, r) -> results.(i) <- Some r) (Pool.run ~workers paths);
    Array.to_list (Array.mapi (fun i slot -> match slot with
      | Some (Ok record) -> record
      | Some (Error message) -> failwith (Printf.sprintf "%s: %s" paths.(i) message)
      | None -> failwith ("missing result for " ^ paths.(i))) results)
  end

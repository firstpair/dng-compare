(* Direct in-process Turso SDK C ABI binding. No subprocess database service. *)
type database
type native_connection
type connection = { database : database; native : native_connection }
type value = Null | Integer of int64 | Real of float | Text of string | Blob of bytes
external version : unit -> string = "dng_turso_version"
external open_database : string -> database = "dng_turso_open"
external connect_native : database -> native_connection = "dng_turso_connect"
external execute_native : native_connection -> string -> value array -> value array list = "dng_turso_execute"
external close_native : native_connection -> unit = "dng_turso_close_connection"
external close_database : database -> unit = "dng_turso_close_database"
let connect database = { database; native = connect_native database }
let execute connection sql parameters = execute_native connection.native sql parameters
let close connection = close_native connection.native
let enable_mvcc connection =
  match execute connection "PRAGMA journal_mode=mvcc" [||] with
  | [[|Text "mvcc"|]] -> ignore (execute connection "PRAGMA synchronous=FULL" [||])
  | _ -> failwith "Turso did not enable MVCC"
let transaction connection work =
  ignore (execute connection "BEGIN CONCURRENT" [||]);
  try
    let result = work () in
    ignore (execute connection "COMMIT" [||]); result
  with error ->
    (try ignore (execute connection "ROLLBACK" [||]) with _ -> ());
    raise error
let checkpoint connection = ignore (execute connection "PRAGMA wal_checkpoint(TRUNCATE)" [||])

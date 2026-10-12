(* Native byte comparison and independent VCDIFF reconstruction hashing.
   SHA-256 uses the system CommonCrypto implementation through small C bindings. *)
external sha_init : unit -> bytes = "dng_sha_init"
external sha_update : bytes -> bytes -> int -> unit = "dng_sha_update" [@@noalloc]
external sha_final : bytes -> string = "dng_sha_final"
let block=1048576
let hash_channel ic =
 let ctx=sha_init () and b=Bytes.create block in
 let rec loop ()=let n=input ic b 0 block in if n>0 then (sha_update ctx b n;loop ()) in
 loop ();sha_final ctx
let hash p = let ic=open_in_bin p in Fun.protect ~finally:(fun ()->close_in ic) (fun ()->hash_channel ic)
let run args =
 let pid=Unix.create_process "xdelta3" (Array.of_list ("xdelta3"::args)) Unix.stdin Unix.stderr Unix.stderr in
 match snd(Unix.waitpid [] pid) with Unix.WEXITED 0->()|_->failwith "VCDIFF encode failed"
let main () = match Sys.argv.(1) with
| "hash" -> print_endline(hash Sys.argv.(2))
| "compare" ->
 let src=open_in_bin Sys.argv.(2) and refc=if Sys.argv.(3)="" then None else Some(open_in_bin Sys.argv.(3)) in
 Fun.protect ~finally:(fun ()->close_in src;Option.iter close_in refc) (fun ()->
 let sh=sha_init () and rh=sha_init () and left=Bytes.create block and right=Bytes.create block in
 let equal=ref(refc<>None) in
 let rec fill ic off n = if off=n then off else let k=input ic right off (n-off) in if k=0 then off else fill ic (off+k) n in
 let rec loop () = let n=input src left 0 block in if n>0 then begin
 sha_update sh left n;let got=match refc with None->0|Some ic->fill ic 0 n in
 sha_update rh right got;
 if got<>n || Bytes.compare (Bytes.sub left 0 n) (Bytes.sub right 0 got)<>0 then equal:=false;
 loop () end in loop ();
 Option.iter (fun ic ->let rec rest ()=let n=input ic right 0 block in if n>0 then (equal:=false;sha_update rh right n;rest ()) in rest ()) refc;
 Printf.printf "%s\t%s\t%d\n" (sha_final sh) (match refc with None->"-"|Some _->sha_final rh) (if !equal then 1 else 0))
| "delta" ->
 let reference=Sys.argv.(2) and source=Sys.argv.(3) and delta=Sys.argv.(4) and expected=Sys.argv.(5) in
 run ["-e";"-1";"-S";"none";"-n";"-a";"-D";"-R";"-A";"-B";"16777216";"-W";"8388608";"-s";reference;source;delta];
 let fd=Unix.openfile delta [Unix.O_RDONLY] 0 in Unix.fsync fd;Unix.close fd;
 let delta_hash=hash delta in
 let rd,wr=Unix.pipe ~cloexec:true () in
 let pid=Unix.create_process "xdelta3" [|"xdelta3";"-d";"-D";"-R";"-c";"-s";reference;delta|] Unix.stdin wr Unix.stderr in
 Unix.close wr;let ic=Unix.in_channel_of_descr rd in
 let recovered=Fun.protect ~finally:(fun ()->close_in ic) (fun ()->hash_channel ic) in
 let status=snd(Unix.waitpid [] pid) in
 if status<>Unix.WEXITED 0 || recovered<>expected then failwith "Reconstructed original SHA-256 mismatch";
 print_endline delta_hash
| _ -> failwith "expected hash, compare, or delta"
let ()=try main () with e->prerr_endline(Printexc.to_string e);exit 1

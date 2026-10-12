(* OxCaml worker pool. NUL-delimited paths on stdin; original ExifTool JSON on stdout. *)
type job = { pid : int; index : int; out : Buffer.t; err : Buffer.t;
             mutable fds : (Unix.file_descr * Buffer.t) list }
let flags = ["-j"; "-a"; "-G0:1:4"; "-n"; "-struct"; "-b"; "-u"; "-U";
 "-api"; "LargeFileSupport=1"; "-all"; "-DNGPrivateData"; "-MakerNotes";
 "-x"; "PreviewImage"; "-x"; "JpgFromRaw"; "-x"; "ThumbnailImage";
 "-x"; "OtherImage"; "-x"; "RawImage"; "-x"; "OriginalRawFileData"]
let read_all ic =
 let b=Buffer.create 4096 and bytes=Bytes.create 65536 in
 let rec loop () = match input ic bytes 0 (Bytes.length bytes) with
 | 0 -> Buffer.contents b | n -> Buffer.add_subbytes b bytes 0 n; loop () in loop ()
let spawn index path =
 let ro,wo=Unix.pipe ~cloexec:true () and re,we=Unix.pipe ~cloexec:true () in
 let args=Array.of_list ("exiftool" :: flags @ [path]) in
 let pid=Unix.create_process "exiftool" args Unix.stdin wo we in
 Unix.close wo; Unix.close we; Unix.set_nonblock ro; Unix.set_nonblock re;
 let out=Buffer.create 4096 and err=Buffer.create 256 in
 {pid;index;out;err;fds=[ro,out;re,err]}
let main () =
 let workers=int_of_string Sys.argv.(1) in
 if workers<1 || workers>36 then invalid_arg "workers must be 1..36";
 let paths=read_all stdin |> String.split_on_char '\000' |> List.filter ((<>) "") |> Array.of_list in
 let results=Array.make (Array.length paths) None in
 let next=ref 0 and active=ref [] and done_count=ref 0 in
 let bytes=Bytes.create 65536 in
 while !done_count<Array.length paths do
  while List.length !active<workers && !next<Array.length paths do
   active := spawn !next paths.(!next) :: !active; incr next
  done;
  let fds=List.concat_map (fun j -> List.map fst j.fds) !active in
  let ready,_,_=Unix.select fds [] [] 0.1 in
  List.iter (fun j ->
   j.fds <- List.filter (fun (fd,b) ->
    if not (List.mem fd ready) then true else
     try match Unix.read fd bytes 0 (Bytes.length bytes) with
     | 0 -> Unix.close fd; false
     | n -> Buffer.add_subbytes b bytes 0 n; true
     with Unix.Unix_error ((Unix.EAGAIN|Unix.EWOULDBLOCK),_,_) -> true) j.fds) !active;
  active := List.filter (fun j ->
   if j.fds<>[] then true else begin
    let _,status=Unix.waitpid [] j.pid in
    let output=String.trim (Buffer.contents j.out) in
    if String.length output<2 || output.[0]<>'[' || output.[String.length output-1]<>']'
    then failwith ("ExifTool returned invalid JSON array: " ^ Buffer.contents j.err);
    results.(j.index) <- Some (output,Buffer.contents j.err,status); incr done_count; false
   end) !active
 done;
 output_char stdout '['; let first=ref true in
 Array.iter (function None -> failwith "missing result" | Some (s,e,_) ->
  output_string stderr e;
  let inner=String.trim (String.sub s 1 (String.length s-2)) in
  if inner<>"" then begin
   if not !first then output_char stdout ','; output_string stdout inner; first:=false
  end) results;
 output_string stdout "]\n"; flush stdout
let () = try main () with e -> prerr_endline (Printexc.to_string e); exit 1

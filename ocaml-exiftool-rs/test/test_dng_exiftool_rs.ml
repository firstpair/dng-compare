(* Synthetic fixtures only: a hand-built 1x1 TIFF with a DNGVersion tag. *)
let le16 n = String.init 2 (fun i -> Char.chr ((n lsr (8 * i)) land 255))
let le32 n = String.init 4 (fun i -> Char.chr ((n lsr (8 * i)) land 255))
let tiff () =
  let entries = [256,4,1,1; 257,4,1,1; 258,3,1,8; 259,3,1,1; 262,3,1,1; 273,4,1,122; 277,3,1,1; 279,4,1,1; 50706,1,4,0x0401] in
  let b = Buffer.create 128 in
  Buffer.add_string b "II"; Buffer.add_string b (le16 42); Buffer.add_string b (le32 8); Buffer.add_string b (le16 (List.length entries));
  List.iter (fun (t,k,c,v) -> Buffer.add_string b (le16 t); Buffer.add_string b (le16 k); Buffer.add_string b (le32 c); Buffer.add_string b (le32 v)) entries;
  Buffer.add_string b (le32 0); assert (Buffer.length b = 122); Buffer.add_char b '\127'; Buffer.contents b

let contains text needle =
  let n = String.length needle and t = String.length text in
  let rec go i = i + n <= t && (String.sub text i n = needle || go (i + 1)) in go 0

let () =
  let dir = Filename.concat (Filename.get_temp_dir_name ()) (Printf.sprintf "dng-exiftool-rs-%d" (Unix.getpid ())) in
  (try Sys.mkdir dir 0o755 with Sys_error _ -> ());
  let fixture = Filename.concat dir "fixture.DNG" in
  Out_channel.with_open_bin fixture (fun oc -> output_string oc (tiff ()));
  let missing = Filename.concat dir "missing.DNG" in
  let version = Dng_exiftool_rs.version () in
  assert (contains version "exiftool-rs");
  Printf.printf "engine %s backend %s recommended %d\n" version Dng_exiftool_rs.backend (Dng_exiftool_rs.recommended_workers ());
  let paths = List.concat (List.init 5 (fun _ -> [fixture; missing])) in
  let records = Dng_exiftool_rs.extract_all ~workers:4 paths in
  assert (List.length records = List.length paths);
  List.iteri (fun i record ->
    assert (String.length record > 2 && record.[0] = '{' && record.[String.length record - 1] = '}');
    assert (contains record (Printf.sprintf "\"SourceFile\":%S" (List.nth paths i)));
    assert (contains record "\"ExifTool:DngCompareEngine\":");
    if i mod 2 = 0 then begin
      assert (contains record "\"EXIF:IFD0:ImageWidth\":1");
      assert (contains record "\"File:FileType\":");
      assert (not (contains record "\"ExifTool:Error\""))
    end else assert (contains record "\"ExifTool:Error\":\"I/O error")) records;
  assert (Dng_exiftool_rs.extract_all ~workers:3 [] = []);
  assert (List.nth records 0 = Dng_exiftool_rs.extract_json fixture);
  print_endline "ok"

/* OCaml stubs over the dng-exiftool-ffi C ABI. Extraction runs with the OCaml
 * runtime lock released, so domains and threads extract in parallel. */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <caml/mlvalues.h>
#include <caml/memory.h>
#include <caml/alloc.h>
#include <caml/fail.h>
#include <caml/threads.h>
#include "dng_exiftool_rs.h"

static value take_string(uint8_t *ptr, size_t len) {
  value s = caml_alloc_initialized_string(len, (const char *)ptr);
  dng_exiftool_rs_free(ptr, len);
  return s;
}

CAMLprim value dng_exiftool_rs_version_stub(value unit) {
  CAMLparam1(unit);
  uint8_t *out = NULL; size_t len = 0;
  if (dng_exiftool_rs_engine_version(&out, &len) != 0) caml_failwith("exiftool-rs engine version unavailable");
  CAMLreturn(take_string(out, len));
}

CAMLprim value dng_exiftool_rs_extract_json_stub(value path) {
  CAMLparam1(path);
  size_t path_len = caml_string_length(path);
  /* Copy the path: the GC may move the OCaml string while the runtime lock is released. */
  uint8_t *copy = malloc(path_len ? path_len : 1);
  if (!copy) caml_raise_out_of_memory();
  memcpy(copy, String_val(path), path_len);
  uint8_t *out = NULL; size_t len = 0;
  caml_release_runtime_system();
  int32_t code = dng_exiftool_rs_extract_json(copy, path_len, &out, &len);
  caml_acquire_runtime_system();
  free(copy);
  if (code != 0) {
    char message[2048];
    snprintf(message, sizeof(message), "exiftool-rs engine status %d: %.*s", (int)code,
             (int)(len < 1900 ? len : 1900), out ? (const char *)out : "");
    if (out) dng_exiftool_rs_free(out, len);
    caml_failwith(message);
  }
  CAMLreturn(take_string(out, len));
}

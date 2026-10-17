/* C ABI of the optional exiftool-rs metadata engine (dng-exiftool-ffi).
 * Buffers returned through `out` are owned by the caller and must be released
 * with dng_exiftool_rs_free using the same pointer and length. */
#ifndef DNG_EXIFTOOL_RS_H
#define DNG_EXIFTOOL_RS_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
/* 0: `out` holds a complete JSON object for `path` (extraction failures are
 *    reported inside the record as ExifTool:ExifTool:Error, never as success).
 * 2: invalid arguments; nothing allocated.
 * 3: the engine panicked; `out` holds a UTF-8 message. */
int32_t dng_exiftool_rs_extract_json(const uint8_t *path, size_t path_len, uint8_t **out, size_t *out_len);
/* Writes "exiftool-rs X.Y.Z (ExifTool N.NN tables)". Returns 0, or 2 for null arguments. */
int32_t dng_exiftool_rs_engine_version(uint8_t **out, size_t *out_len);
void dng_exiftool_rs_free(uint8_t *ptr, size_t len);
#ifdef __cplusplus
}
#endif
#endif

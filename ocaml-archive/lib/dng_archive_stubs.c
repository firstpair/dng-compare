/* Native calls for dng-archive: SHA-256, zlib and gzip, nanosecond stat, extended
   attributes, utimensat and flock. Each one mirrors what preserve_apo.py and
   metadata_index.py get from Python, so archives written by either side read
   the same. */
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <sys/wait.h>
#include <unistd.h>
#include <signal.h>
#include <zlib.h>
#include <caml/alloc.h>
#include <caml/fail.h>
#include <caml/memory.h>
#include <caml/mlvalues.h>
#include <caml/signals.h>
#include <caml/unixsupport.h>

#ifdef __APPLE__
#include <CommonCrypto/CommonDigest.h>
typedef CC_SHA256_CTX sha_ctx;
#define SHA_INIT CC_SHA256_Init
#define SHA_UPDATE(c, d, n) CC_SHA256_Update(c, d, (CC_LONG)(n))
#define SHA_FINAL(o, c) CC_SHA256_Final(o, c)
#else
#include <openssl/sha.h>
typedef SHA256_CTX sha_ctx;
#define SHA_INIT SHA256_Init
#define SHA_UPDATE SHA256_Update
#define SHA_FINAL SHA256_Final
#endif

CAMLprim value dng_archive_sha_init(value unit) {
  CAMLparam1(unit);
  CAMLlocal1(v);
  v = caml_alloc_string(sizeof(sha_ctx));
  SHA_INIT((sha_ctx *)Bytes_val(v));
  CAMLreturn(v);
}

/* The data is copied by the caller's buffer discipline: no allocation here. */
CAMLprim value dng_archive_sha_update(value ctx, value data, value off, value len) {
  SHA_UPDATE((sha_ctx *)Bytes_val(ctx), Bytes_val(data) + Long_val(off), Long_val(len));
  return Val_unit;
}

CAMLprim value dng_archive_sha_final(value ctx) {
  CAMLparam1(ctx);
  CAMLlocal1(v);
  static const char hex[] = "0123456789abcdef";
  unsigned char out[32];
  SHA_FINAL(out, (sha_ctx *)Bytes_val(ctx));
  v = caml_alloc_string(64);
  for (int i = 0; i < 32; i++) {
    Bytes_val(v)[2 * i] = hex[out[i] >> 4];
    Bytes_val(v)[2 * i + 1] = hex[out[i] & 15];
  }
  CAMLreturn(v);
}

/* zlib.compress(data, level): a zlib stream, as metadata_index stores values. */
CAMLprim value dng_archive_zlib_compress(value data, value level) {
  CAMLparam2(data, level);
  CAMLlocal1(v);
  uLong n = caml_string_length(data);
  uLongf bound = compressBound(n);
  unsigned char *buf = malloc(bound ? bound : 1);
  if (!buf) caml_raise_out_of_memory();
  if (compress2(buf, &bound, (const Bytef *)String_val(data), n, Int_val(level)) != Z_OK) {
    free(buf);
    caml_failwith("zlib compress failed");
  }
  v = caml_alloc_initialized_string(bound, (const char *)buf);
  free(buf);
  CAMLreturn(v);
}

/* A whole gzip file's contents, CRC and length trailer checked by zlib, like
   Python's gzip.decompress. Concatenated members are read in turn. */
CAMLprim value dng_archive_gunzip(value data) {
  CAMLparam1(data);
  CAMLlocal1(v);
  size_t n = caml_string_length(data), cap = n * 4 + 65536, used = 0;
  unsigned char *out = malloc(cap);
  if (!out) caml_raise_out_of_memory();
  z_stream s;
  memset(&s, 0, sizeof s);
  if (inflateInit2(&s, 15 + 16) != Z_OK) { free(out); caml_failwith("gzip: inflateInit failed"); }
  s.next_in = (Bytef *)String_val(data);
  s.avail_in = n;
  int rc;
  for (;;) {
    if (used == cap) {
      unsigned char *bigger = realloc(out, cap * 2);
      if (!bigger) { inflateEnd(&s); free(out); caml_raise_out_of_memory(); }
      out = bigger; cap *= 2;
    }
    s.next_out = out + used;
    s.avail_out = cap - used;
    rc = inflate(&s, Z_NO_FLUSH);
    used = cap - s.avail_out;
    if (rc == Z_STREAM_END) {
      if (s.avail_in == 0) break;
      if (inflateReset(&s) != Z_OK) { rc = Z_DATA_ERROR; break; }
      continue;
    }
    if (rc != Z_OK && !(rc == Z_BUF_ERROR && s.avail_out == 0)) break;
  }
  inflateEnd(&s);
  if (rc != Z_STREAM_END) { free(out); caml_failwith("gzip: corrupt or truncated stream"); }
  v = caml_alloc_initialized_string(used, (const char *)out);
  free(out);
  CAMLreturn(v);
}

static int64_t ns(struct timespec t) { return (int64_t)t.tv_sec * 1000000000 + t.tv_nsec; }

/* [| ino; size; mtime_ns; ctime_ns; atime_ns; mode; uid; gid; flags; kind; dev |]
   kind: 0 regular, 1 directory, 2 symlink, 3 other. */
CAMLprim value dng_archive_stat(value path, value follow) {
  CAMLparam2(path, follow);
  CAMLlocal1(v);
  struct stat st;
  char *p = caml_stat_strdup(String_val(path));
  caml_enter_blocking_section();
  int rc = Bool_val(follow) ? stat(p, &st) : lstat(p, &st);
  caml_leave_blocking_section();
  caml_stat_free(p);
  if (rc < 0) uerror("stat", path);
#ifdef __APPLE__
  struct timespec m = st.st_mtimespec, c = st.st_ctimespec, a = st.st_atimespec;
  int64_t flags = st.st_flags;
#else
  struct timespec m = st.st_mtim, c = st.st_ctim, a = st.st_atim;
  int64_t flags = 0;
#endif
  int64_t kind = S_ISREG(st.st_mode) ? 0 : S_ISDIR(st.st_mode) ? 1 : S_ISLNK(st.st_mode) ? 2 : 3;
  int64_t f[11] = {(int64_t)st.st_ino, (int64_t)st.st_size, ns(m), ns(c), ns(a),
                   st.st_mode & 07777, st.st_uid, st.st_gid, flags, kind, (int64_t)st.st_dev};
  v = caml_alloc_tuple(11);
  for (int i = 0; i < 11; i++) Store_field(v, i, caml_copy_int64(f[i]));
  CAMLreturn(v);
}

/* What os.stat (following a symlink) adds for portable_metadata.properties:
   the whole st_mode, and st_birthtime as Python computes the float
   (seconds + 1e-9 * nanoseconds), None where the platform has none. */
CAMLprim value dng_archive_stat_extra(value path) {
  CAMLparam1(path);
  CAMLlocal3(v, some, birth);
  struct stat st;
  if (stat(String_val(path), &st) < 0) uerror("stat", path);
  v = caml_alloc_tuple(2);
  Store_field(v, 0, Val_long(st.st_mode));
#ifdef __APPLE__
  unsigned long bsec = (long)st.st_birthtimespec.tv_sec, bnsec = st.st_birthtimespec.tv_nsec;
  birth = caml_copy_double(bsec + 1e-9 * bnsec);
  some = caml_alloc_small(1, 0);
  Field(some, 0) = birth;
  Store_field(v, 1, some);
#else
  Store_field(v, 1, Val_none);
#endif
  CAMLreturn(v);
}

#ifdef __APPLE__
#define XLIST(p, b, n) listxattr(p, b, n, XATTR_NOFOLLOW)
#define XGET(p, k, b, n) getxattr(p, k, b, n, 0, XATTR_NOFOLLOW)
#define XSET(p, k, b, n) setxattr(p, k, b, n, 0, 0)
#else
#define XLIST(p, b, n) llistxattr(p, b, n)
#define XGET(p, k, b, n) lgetxattr(p, k, b, n)
#define XSET(p, k, b, n) setxattr(p, k, b, n, 0)
#endif

/* Attribute names of a path, not following a symlink, in the order listed. */
CAMLprim value dng_archive_listxattr(value path) {
  CAMLparam1(path);
  CAMLlocal3(list, cell, name);
  ssize_t n = XLIST(String_val(path), NULL, 0);
  if (n < 0) uerror("listxattr", path);
  char *buf = malloc(n ? n : 1);
  if (!buf) caml_raise_out_of_memory();
  n = XLIST(String_val(path), buf, n);
  if (n < 0) { free(buf); uerror("listxattr", path); }
  list = Val_emptylist;
  /* Build in reverse, then the caller reverses: keeps listing order. */
  for (ssize_t i = 0; i < n;) {
    size_t len = strlen(buf + i);
    if (len) {
      name = caml_alloc_initialized_string(len, buf + i);
      cell = caml_alloc_small(2, 0);
      Field(cell, 0) = name;
      Field(cell, 1) = list;
      list = cell;
    }
    i += len + 1;
  }
  free(buf);
  CAMLreturn(list);
}

CAMLprim value dng_archive_getxattr(value path, value name) {
  CAMLparam2(path, name);
  CAMLlocal1(v);
  ssize_t n = XGET(String_val(path), String_val(name), NULL, 0);
  if (n < 0) uerror("getxattr", path);
  char *buf = malloc(n ? n : 1);
  if (!buf) caml_raise_out_of_memory();
  n = XGET(String_val(path), String_val(name), buf, n);
  if (n < 0) { free(buf); uerror("getxattr", path); }
  v = caml_alloc_initialized_string(n, buf);
  free(buf);
  CAMLreturn(v);
}

CAMLprim value dng_archive_setxattr(value path, value name, value data) {
  CAMLparam3(path, name, data);
  if (XSET(String_val(path), String_val(name), String_val(data), caml_string_length(data)) < 0)
    uerror("setxattr", path);
  CAMLreturn(Val_unit);
}

/* os.utime(path, ns=(atime_ns, mtime_ns)) */
CAMLprim value dng_archive_utimens(value path, value atime, value mtime) {
  CAMLparam3(path, atime, mtime);
  struct timespec t[2];
  int64_t a = Int64_val(atime), m = Int64_val(mtime);
  t[0].tv_sec = a / 1000000000; t[0].tv_nsec = a % 1000000000;
  t[1].tv_sec = m / 1000000000; t[1].tv_nsec = m % 1000000000;
  if (utimensat(AT_FDCWD, String_val(path), t, 0) < 0) uerror("utimensat", path);
  CAMLreturn(Val_unit);
}

/* fcntl.flock(fd, LOCK_EX | LOCK_NB): true when taken, false when another
   process holds it. The same lock preserve_apo.py takes. */
CAMLprim value dng_archive_flock_exclusive(value fd) {
  if (flock(Int_val(fd), LOCK_EX | LOCK_NB) == 0) return Val_true;
  if (errno == EWOULDBLOCK) return Val_false;
  uerror("flock", Nothing);
}

/* The errno a Unix.error stands for, as Python's OSError.errno reports it. */
CAMLprim value dng_archive_errno(value err) {
  return Val_int(code_of_unix_error(err));
}

/* waitpid with the raw status, which Unix.waitpid maps to OCaml's own signal
   numbers: the exit code, -N for a child ended by signal N (Python's
   returncode), or 1000 while a WNOHANG child still runs. */
CAMLprim value dng_archive_waitpid(value pid, value nohang) {
  int status = 0;
  pid_t rc;
  caml_enter_blocking_section();
  do rc = waitpid(Int_val(pid), &status, Bool_val(nohang) ? WNOHANG : 0);
  while (rc < 0 && errno == EINTR);
  caml_leave_blocking_section();
  if (rc < 0) uerror("waitpid", Nothing);
  if (rc == 0) return Val_int(1000);
  if (WIFSIGNALED(status)) return Val_int(-WTERMSIG(status));
  return Val_int(WEXITSTATUS(status));
}

/* fork, chdir, exec: subprocess.run(..., cwd=DIR) with stdout and stderr on
   the given descriptors. Returns the child's pid. */
CAMLprim value dng_archive_spawn_in(value dir, value argv, value out, value err) {
  CAMLparam4(dir, argv, out, err);
  int n = Wosize_val(argv);
  char **args = malloc((n + 1) * sizeof(char *));
  if (!args) caml_raise_out_of_memory();
  for (int i = 0; i < n; i++) args[i] = caml_stat_strdup(String_val(Field(argv, i)));
  args[n] = NULL;
  char *d = caml_stat_strdup(String_val(dir));
  int fo = Int_val(out), fe = Int_val(err);
  pid_t pid = fork();
  if (pid == 0) {
    int devnull = open("/dev/null", O_RDONLY);
    if (devnull >= 0) dup2(devnull, 0);
    dup2(fo, 1); dup2(fe, 2);
    if (chdir(d) < 0) { perror(d); _exit(127); }
    execv(args[0], args);
    perror(args[0]);
    _exit(127);
  }
  for (int i = 0; i < n; i++) caml_stat_free(args[i]);
  free(args);
  caml_stat_free(d);
  if (pid < 0) uerror("fork", Nothing);
  CAMLreturn(Val_int(pid));
}

/* gzip.compress(data, compresslevel=level): one gzip member. The header
   carries no name or time, so it differs from Python's in those bytes only. */
CAMLprim value dng_archive_gzip(value data, value level) {
  CAMLparam2(data, level);
  CAMLlocal1(v);
  size_t n = caml_string_length(data);
  z_stream s;
  memset(&s, 0, sizeof s);
  if (deflateInit2(&s, Int_val(level), Z_DEFLATED, 15 + 16, 8, Z_DEFAULT_STRATEGY) != Z_OK)
    caml_failwith("gzip: deflateInit failed");
  uLong bound = deflateBound(&s, n);
  unsigned char *buf = malloc(bound ? bound : 1);
  if (!buf) { deflateEnd(&s); caml_raise_out_of_memory(); }
  s.next_in = (Bytef *)String_val(data);
  s.avail_in = n;
  s.next_out = buf;
  s.avail_out = bound;
  int rc = deflate(&s, Z_FINISH);
  size_t used = bound - s.avail_out;
  deflateEnd(&s);
  if (rc != Z_STREAM_END) { free(buf); caml_failwith("gzip: compress failed"); }
  v = caml_alloc_initialized_string(used, (const char *)buf);
  free(buf);
  CAMLreturn(v);
}

/* zlib.decompress(data, -15): a raw deflate stream, as phindex stores records. */
CAMLprim value dng_archive_inflate_raw(value data) {
  CAMLparam1(data);
  CAMLlocal1(v);
  size_t n = caml_string_length(data), cap = n * 8 + 65536, used = 0;
  unsigned char *out = malloc(cap);
  if (!out) caml_raise_out_of_memory();
  z_stream s;
  memset(&s, 0, sizeof s);
  if (inflateInit2(&s, -15) != Z_OK) { free(out); caml_failwith("inflate: inflateInit failed"); }
  s.next_in = (Bytef *)String_val(data);
  s.avail_in = n;
  int rc;
  for (;;) {
    if (used == cap) {
      unsigned char *bigger = realloc(out, cap * 2);
      if (!bigger) { inflateEnd(&s); free(out); caml_raise_out_of_memory(); }
      out = bigger; cap *= 2;
    }
    s.next_out = out + used;
    s.avail_out = cap - used;
    rc = inflate(&s, Z_NO_FLUSH);
    used = cap - s.avail_out;
    if (rc == Z_STREAM_END) break;
    if (rc != Z_OK && !(rc == Z_BUF_ERROR && s.avail_out == 0)) break;
  }
  inflateEnd(&s);
  if (rc != Z_STREAM_END) { free(out); caml_failwith("inflate: corrupt or truncated deflate stream"); }
  v = caml_alloc_initialized_string(used, (const char *)out);
  free(out);
  CAMLreturn(v);
}

/* listxattr and getxattr following a symlink, as os.listxattr(p) and
   os.getxattr(p, n) (xattr_portable's on macOS) read them. */
#ifdef __APPLE__
#define XLISTF(p, b, n) listxattr(p, b, n, 0)
#define XGETF(p, k, b, n) getxattr(p, k, b, n, 0, 0)
#else
#define XLISTF(p, b, n) listxattr(p, b, n)
#define XGETF(p, k, b, n) getxattr(p, k, b, n)
#endif

CAMLprim value dng_archive_listxattr_follow(value path) {
  CAMLparam1(path);
  CAMLlocal3(list, cell, name);
  ssize_t n = XLISTF(String_val(path), NULL, 0);
  if (n < 0) uerror("listxattr", path);
  char *buf = malloc(n ? n : 1);
  if (!buf) caml_raise_out_of_memory();
  n = XLISTF(String_val(path), buf, n);
  if (n < 0) { free(buf); uerror("listxattr", path); }
  list = Val_emptylist;
  for (ssize_t i = 0; i < n;) {
    size_t len = strlen(buf + i);
    if (len) {
      name = caml_alloc_initialized_string(len, buf + i);
      cell = caml_alloc_small(2, 0);
      Field(cell, 0) = name;
      Field(cell, 1) = list;
      list = cell;
    }
    i += len + 1;
  }
  free(buf);
  CAMLreturn(list);
}

CAMLprim value dng_archive_getxattr_follow(value path, value name) {
  CAMLparam2(path, name);
  CAMLlocal1(v);
  ssize_t n = XGETF(String_val(path), String_val(name), NULL, 0);
  if (n < 0) uerror("getxattr", path);
  char *buf = malloc(n ? n : 1);
  if (!buf) caml_raise_out_of_memory();
  n = XGETF(String_val(path), String_val(name), buf, n);
  if (n < 0) { free(buf); uerror("getxattr", path); }
  v = caml_alloc_initialized_string(n, buf);
  free(buf);
  CAMLreturn(v);
}

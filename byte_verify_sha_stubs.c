#include <CommonCrypto/CommonDigest.h>
#include <caml/mlvalues.h>
#include <caml/memory.h>
#include <caml/alloc.h>
CAMLprim value dng_sha_init(value unit) { CAMLparam1(unit); CAMLlocal1(v); v=caml_alloc_string(sizeof(CC_SHA256_CTX)); CC_SHA256_Init((CC_SHA256_CTX *)Bytes_val(v)); CAMLreturn(v); }
CAMLprim value dng_sha_update(value ctx,value data,value length) { CC_SHA256_Update((CC_SHA256_CTX *)Bytes_val(ctx),Bytes_val(data),(CC_LONG)Long_val(length)); return Val_unit; }
CAMLprim value dng_sha_final(value ctx) { CAMLparam1(ctx); CAMLlocal1(v); unsigned char out[CC_SHA256_DIGEST_LENGTH]; CC_SHA256_Final(out,(CC_SHA256_CTX *)Bytes_val(ctx)); v=caml_alloc_string(64); static const char hex[]="0123456789abcdef"; for(int i=0;i<32;i++){Bytes_val(v)[2*i]=hex[out[i]>>4];Bytes_val(v)[2*i+1]=hex[out[i]&15];} CAMLreturn(v); }

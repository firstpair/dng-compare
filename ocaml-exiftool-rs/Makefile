# Standalone build of the OCaml/OxCaml exiftool-rs binding.
# DUNE may point at a switch, e.g. DUNE="opam exec --switch 5.2.0+ox -- dune".
# DNG_EXIFTOOL_FFI_LIBDIR locates libdng_exiftool_ffi (default ../rust-scanner/target/release).
DUNE ?= dune
.PHONY: build test clean
build:
	$(DUNE) build @install
test:
	$(DUNE) test --force
clean:
	$(DUNE) clean

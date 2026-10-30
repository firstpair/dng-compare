# Standalone build of dng-archive, the OCaml/OxCaml metadata index and
# preservation archive. DUNE may point at a switch, e.g.
# DUNE="opam exec --switch 5.2.0+ox -- dune".
DUNE ?= dune
.PHONY: build test clean
build:
	$(DUNE) build @install
test:
	$(DUNE) test --force
clean:
	$(DUNE) clean

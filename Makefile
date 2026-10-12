UV ?= uv
PYTHON ?= $(UV) run --locked python
CARGO ?= cargo
CARGO_JOBS ?= 4
# Both worker sources use standard OCaml. Keep their historical executable
# names because DNG_METADATA_BACKEND selects its wire format from the suffix.
OCAMLOPT ?= ocamlopt
OXCAMLOPT ?= $(OCAMLOPT)
OCAMLFLAGS ?= -O3
TURSO_SDK_LIBDIR ?= $(CURDIR)/rust-scanner/target/release
PAUSE_LIVE_JOB ?= 0
BENCHMARK_PAUSE_ARGS = $(if $(filter 1,$(PAUSE_LIVE_JOB)),--live-pid "$(LIVE_PID)" --pause-live-job)
BENCHMARK_PATH_ARGS = $(if $(strip $(PATH_CONTAINS)),--path-contains "$(PATH_CONTAINS)")
BENCHMARK_DATASET_ARGS = $(if $(strip $(DATASET)),--dataset "$(DATASET)")

.PHONY: setup build build-rust build-ocaml build-oxcaml build-turso test test-python test-rust test-turso benchmark benchmark-memory benchmark-turso check-benchmark-args check-benchmark-year
setup:
	$(UV) sync --locked
build: build-rust build-oxcaml
build-rust:
	$(CARGO) build --locked --release --manifest-path rust-scanner/Cargo.toml --bins -j $(CARGO_JOBS)
build-ocaml: build-oxcaml
build-oxcaml:
	$(OXCAMLOPT) $(OCAMLFLAGS) -I +unix unix.cmxa metadata_extract_oxcaml.ml -o metadata-extract-oxcaml
	$(OXCAMLOPT) $(OCAMLFLAGS) -I +unix unix.cmxa byte_verify_sha_stubs.c byte_verify_oxcaml.ml -o byte-verify-oxcaml
test: test-python test-rust
test-python:
	$(PYTHON) -m unittest discover -v
test-rust:
	$(CARGO) test --locked --manifest-path rust-scanner/Cargo.toml -j $(CARGO_JOBS)
build-turso:
	$(CARGO) build --locked --release --manifest-path rust-scanner/Cargo.toml --features turso-probe -p dng-subset -p turso_sdk_kit -j $(CARGO_JOBS)
	$(OXCAMLOPT) $(OCAMLFLAGS) -I +unix unix.cmxa turso_oxcaml_stubs.c turso_oxcaml.ml turso_probe_oxcaml.ml -cclib "-L$(TURSO_SDK_LIBDIR)" -cclib -lturso_sdk_kit -cclib "-Wl,-rpath,$(TURSO_SDK_LIBDIR)" -o turso-probe-oxcaml
test-turso:
	DNG_DATABASE_BACKEND=turso DNG_METADATA_SHARDS=2 $(PYTHON) -m unittest discover -v
check-benchmark-args:
	@test -n "$(ARCHIVE)" || { echo 'Set ARCHIVE to the existing archive directory.' >&2; exit 2; }
	@test "$(PAUSE_LIVE_JOB)" = 0 || test "$(PAUSE_LIVE_JOB)" = 1 || { echo 'PAUSE_LIVE_JOB must be 0 or 1.' >&2; exit 2; }
	@test -z "$(LIVE_PID)" || test "$(PAUSE_LIVE_JOB)" = 1 || { echo 'LIVE_PID requires explicit PAUSE_LIVE_JOB=1.' >&2; exit 2; }
	@test "$(PAUSE_LIVE_JOB)" = 0 || test -n "$(LIVE_PID)" || { echo 'PAUSE_LIVE_JOB=1 requires LIVE_PID.' >&2; exit 2; }
check-benchmark-year: check-benchmark-args
	@test -n "$(YEAR)" || { echo 'Set YEAR to an explicit LABEL/YYYY archive scope.' >&2; exit 2; }
benchmark: check-benchmark-year build
	$(PYTHON) benchmark_metadata_workers.py "$(ARCHIVE)" --year "$(YEAR)" $(BENCHMARK_PATH_ARGS) $(BENCHMARK_PAUSE_ARGS)
benchmark-memory: check-benchmark-args build
	$(PYTHON) benchmark_metadata_memory.py "$(ARCHIVE)" $(BENCHMARK_PAUSE_ARGS)
benchmark-turso: check-benchmark-year
	$(PYTHON) benchmark_turso_metadata.py "$(ARCHIVE)" --year "$(YEAR)" $(BENCHMARK_PATH_ARGS) $(BENCHMARK_DATASET_ARGS) $(BENCHMARK_PAUSE_ARGS)

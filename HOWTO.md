# HOWTO: compare and preserve a DNG tree

Use this workflow when the source tree may contain older metadata or edits than
your main copy. Keep both trees unchanged while you compare them. The left tree
is the one whose coverage you are checking; additional right-tree files are
allowed.

## 1. Install and check the tools

```sh
brew install exiftool xdelta ocaml
git clone https://github.com/firstpair/dng-compare.git
cd dng-compare
make setup
rustup toolchain install 1.99.0
make build
make test
```

Optional: `make build-exiftool-rs` adds the in-process exiftool-rs engine
(`DNG_METADATA_ENGINE=exiftool-rs`, `preserve_years.py --metadata-engine exiftool-rs`);
it needs an opam switch with dune (OxCaml `5.2.0+ox` by default, or `OXCAML_SWITCH=`
for stock OCaml 5) and links GPL-3.0-or-later code. See the manual section
"Optional exiftool-rs engine".


Install uv and rustup first if necessary. Python-only comparison needs ExifTool
and `make setup`; building the native workers is optional for that command. See
the [manual](docs/dng-compare-manual.md#install-and-build) for platform support and
compiler overrides.

## 2. Try a small comparison

```sh
uv run python compare_dng.py /media/source/foto /media/reference/foto \
  --sample 12 --workers 2 --output reports/sample
```

Inspect `reports/sample/pairs.jsonl`. A sample establishes only those pairs.
Exit 1 can mean the rest of the source tree is untested. A filename match is a
candidate; the program still reads and compares the images.

## 3. Compare the complete tree

```sh
uv run python compare_dng.py /media/source/foto /media/reference/foto \
  --workers 2 --output reports/full
```

For HDDs, start with one or two workers. For SSDs, try four and measure. Different
compression can require two decoded arrays per active worker, so more workers
also mean more memory.

Rerun the same command to resume. Use a different report directory for another
root pair, a deliberately fresh audit or an independent run. `--fresh` ignores
the Python comparison cache. Do not run multiple writers in the same directory.

## 4. Read the three separate verdicts

```sh
uv run python - <<'PY'
import json
from pathlib import Path
s = json.loads(Path('reports/full/summary.json').read_text())
print('All candidate pairs processed:', s['complete'])
print('Covered left files:', s['left_files_covered'])
print('Whole-tree verdicts:', s['left_subsumed_by_right'])
print('Counts:', s['counts'])
PY
```

- `raw: true`: every left DNG has an equal stored-sample copy.
- `all_images: true`: raw and auxiliary images are equal in a matching copy.
- `metadata_preserved: true`: a raw-equal copy has no removed or changed exposed
  embedded metadata fields, and no ExifTool warnings; added fields are allowed.

`complete: true` alone does not mean the files matched. Look in
`not-confirmed.json` and `pairs.jsonl` for missing files, real decoded-sample
differences, decoder failures or changed sources. Renamed files need an explicit
mapping (`--pair LEFT RIGHT` or `--pairs FILE`) or another identity investigation.

## 5. Preserve meaningful differences

If the raw samples match but XMP differs, retain both settings versions or
preserve the older whole file. The archive layer can store verified whole-file
originals and VCDIFF deltas. Follow the
[preservation workflow](docs/dng-compare-manual.md#preserve-exact-original-files)
and test restore into a new directory before making a separate deletion decision.

Do not delete a tree solely because the raw verdict is true. The comparator does
not account for a Lightroom catalog, renamed non-DNG originals, sidecar contents,
all filesystem metadata or your desired number of independent backups.

For a rehearsal using only generated images, follow the
[synthetic example](docs/dng-compare-manual.md#a-reproducible-synthetic-example).
For bounded scanning, worker protocols, XMP extraction and all options, continue
with the [complete manual](docs/dng-compare-manual.md).

## 6. Record each editor's settings

To keep and compare how different editors set up the same photographs, record
each one as a capture in a settings archive. Use either `develop_settings.py` or
the Rust `develop-settings`; both write the same archive:

```sh
uv run python develop_settings.py capture ARCHIVE lr xmp FOLDER
uv run python develop_settings.py capture ARCHIVE lr-export xmp EXPORTS --extensions jpg
uv run python develop_settings.py capture ARCHIVE lrc lrcat FOLDER --catalog CATALOG.lrcat
uv run python develop_settings.py capture ARCHIVE c1 capture-one FOLDER
uv run python develop_settings.py compare ARCHIVE lr lrc
```

Lightroom's exported JPEGs embed their full settings; Capture One's do not, so
read its `.cos` files. See
[develop settings across editors](docs/dng-compare-manual.md#develop-settings-across-editors).

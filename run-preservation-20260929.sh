#!/bin/bash
# Portable successor of the historical archive wrapper; input roots are required.
set -euo pipefail
if [ "$#" -ne 3 ]; then
  echo 'Usage: run-preservation-20260929.sh SOURCE REFERENCE ARCHIVE' >&2
  exit 2
fi
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE=$1
REFERENCE=$2
ARCHIVE=$3
if [ ! -x "$PROJECT/rust-scanner/target/release/dng-subset" ]; then
  echo 'Build dng-subset with make build-rust first.' >&2
  exit 2
fi
uv run --python 3.14 --no-project --with-requirements "$PROJECT/requirements.txt" python - "$SOURCE" "$REFERENCE" "$ARCHIVE" "$PROJECT" <<'PY'
import pathlib,sys
sys.path.insert(0,sys.argv[4])
from preserve_apo import validate_output
validate_output(pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]))
PY
mkdir -p "$ARCHIVE"
mkdir "$ARCHIVE/.running" || exit 1
trap 'rmdir "$ARCHIVE/.running"' EXIT
uv run --python 3.14 --no-project --with-requirements "$PROJECT/requirements.txt" python "$PROJECT/preserve_apo.py" scan "$SOURCE" "$REFERENCE" "$ARCHIVE" --delta --rate-mib 200
uv run --python 3.14 --no-project --with-requirements "$PROJECT/requirements.txt" python "$PROJECT/portable_metadata.py" "$SOURCE" "$REFERENCE" "$ARCHIVE" --batch-size 16
"$PROJECT/rust-scanner/target/release/dng-subset" --archive-xmp "$SOURCE" "$ARCHIVE/xmp-original"
uv run --python 3.14 --no-project --with-requirements "$PROJECT/requirements.txt" python "$PROJECT/finalize_preservation.py" "$ARCHIVE"

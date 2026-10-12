slug: dng-compare
shelf: howto
default_edition: full

# Publication contract

This source repository owns all code, the manual in `docs/dng-compare-manual.md`,
the supplied unchanged Sierra artwork, build inputs and versioned artifacts.
FirstPair owns catalog entries, stable download/reader routes and Blob manifests.
Follow the shared `~/src/firstpair/AGENTS.md` publication and source-ownership rules.

The code and manual are MIT licensed. Retain the upstream Turso header notice.
Private photograph inventories, metadata reports, recovery payloads, compiler
outputs, environments and private historical execution records are excluded.

Commit and push the complete source, then run the canonical preflight and builder:

```sh
uv run --no-project python "$HOME/src/firstpair/publishing/scripts/git_publish_preflight.py" .
"$HOME/src/firstpair/publishing/scripts/build-library-book.sh" --repo-root "$PWD" --print-plan
"$HOME/src/firstpair/publishing/scripts/build-library-book.sh" --repo-root "$PWD"
"$HOME/src/firstpair/publishing/scripts/verify-library-book.sh" "$PWD/dist"
```

The builder needs its pinned publishing toolchain. Choose its `python3` from
your tested uv-managed interpreter when invoking shell wrappers. Review every
PDF page and check EPUB/HTML contents, links, cover and version binding before
committing and pushing the generated artifacts. Do not substitute fixture
test results for a completed real-photo comparison.

Publish only this title through the shared FirstPair publisher:

```sh
cd "$HOME/src/firstpair"
npm run library:publish -- /absolute/path/to/dng-compare \
  --slug dng-compare --full --title 'DNG Compare' \
  --description 'A manual for comparing DNG raw samples, examining metadata changes and preserving exact recoverable bytes with Python, Rust and OCaml.' \
  --kicker 'Same samples, different metadata' \
  --tags photography,dng,metadata,rust,ocaml \
  --source https://github.com/firstpair/dng-compare --no-deploy
```

Both repositories must be clean and pushed before uploading. Commit and push
the library manifests in the same publication session, then deploy and verify.
Reader-facing URLs use `https://firstpair.org/dng-compare/pdf/`,
`/dng-compare/epub/`, `/read/dng-compare/` and `/read/dng-compare/chapters/`.
Do not re-upload unchanged artifacts for a catalog-only update.

Canonical story source and TextPack live in `docs/blog/dng-compare/`, with
slug `dng-compare` and destination `https://firstpair.press/dng-compare/`.
Use the stamped, committed, pushed TextPack and normal iCloud Blogs delivery.
File delivery does not publish Ghost. Add a library `post` link only after
the canonical story is live and verified.

# DNG Compare public source

This repository owns the Python coordinator, Rust and OCaml workers, tests,
manual, supplied cover and publication artifacts. The original private
comparison inventories, image files and recovery archives are not part of it.

- Use `uv` for Python. Rust uses the pinned 1.99.0 toolchain and edition 2024;
  verify the actual versions. Distinguish standard OCaml from OxCaml in claims.
- Preserve source and reference trees. Tests use disposable synthetic fixtures,
  never an existing photo archive. Recovery writes only to a new destination.
- Treat encoded-block equality, decoded stored samples, rendered appearance,
  whole-file SHA256, native Lightroom fingerprints and metadata as separate
  identities. Missing, failed or unsupported work is not a successful match.
- Preserve failed receipts and cache/identity gates. Never manufacture successful
  completion, weaken source-stability checks or cancel another live job for a test.
- Keep CLI examples, dependencies, worker options and report semantics aligned
  with actual code. Document optional/experimental routes and platform limits.
- Keep personal paths, photo inventories, reports, credentials, environments and
  generated native binaries out of Git. Retain third-party license notices.
- Cookbook and TextPack publication follows [FIRSTPAIR.md](FIRSTPAIR.md) and
  the shared `~/src/firstpair/AGENTS.md`: clean pushed source, pinned builds,
  visual artifact verification, one-title uploads, committed delivery manifests.
- Canonical story source lives in `docs/blog/dng-compare/post.md`. Publish its
  stamped pack to iCloud only after source and artifacts are committed/pushed.
  A delivered TextPack is not a published Ghost article.

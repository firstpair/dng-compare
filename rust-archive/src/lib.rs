//! dng-archive: the dngc archive format (docs/dngc-archive-spec.md) in Rust,
//! beside the OCaml dng-archive library (ocaml-archive/).
//!
//! - [`read`]: open an archive, list its records and earlier versions, rebuild
//!   and restore a content byte for byte ([`Reader`]).
//! - [`skeleton`]: skeletons, which rebuild a content from an edited version
//!   of itself.
//! - [`archive`]: write the byte section: records, deltas, full copies and the
//!   section's files, as dng-compare-all does for listed pairs.
//! - [`phindex`]: read phindex indexes to settle identical pairs unread.
//! - [`pyjson`]: JSON exactly as Python's json module reads and writes it, so
//!   records keep the text the other implementations write.
//! - [`os`]: file identities, attributes and extended attributes.

pub mod archive;
pub mod os;
pub mod phindex;
pub mod pyjson;
pub mod read;
pub mod skeleton;

pub use read::{Kind, Reader, kind, sections, version_label};

/// Lower-case hex of a digest.
pub fn hex(bytes: &[u8]) -> String {
    const D: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes { out.push(D[(b >> 4) as usize] as char); out.push(D[(b & 15) as usize] as char); }
    out
}

#[cfg(test)]
mod conformance_tests;

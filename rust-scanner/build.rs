// The source commit (first six hex digits, -dirty when built from uncommitted
// changes), shown in dng-compare-all's progress lines.
use std::process::Command;

fn main() {
    let git = |args: &[&str]| Command::new("git").args(args).output().ok().filter(|o| o.status.success());
    let commit = git(&["rev-parse", "--short=6", "HEAD"])
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|| "unknown".into());
    let dirty = commit != "unknown" && git(&["diff", "--quiet", "HEAD", "--", "."]).is_none();
    println!("cargo:rustc-env=DNG_COMPARE_COMMIT={commit}{}", if dirty { "-dirty" } else { "" });
    for p in ["../.git/HEAD", "../.git/index", "src", "Cargo.toml"] {
        println!("cargo:rerun-if-changed={p}");
    }
}

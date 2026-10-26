//! `cfg(` (other than `cfg(test)`) may appear only in `src/platform/` and `src/main.rs`, so the app
//! logic stays identical on web and native (docs/nsh-client.md).

use std::path::{Path, PathBuf};

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            rust_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path);
        }
    }
}

#[test]
fn cfg_only_in_platform_and_main() {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    rust_files(&src, &mut files);
    let needle = ["cf", "g("].concat();
    let mut offenders = Vec::new();
    for file in files {
        let rel = file.strip_prefix(&src).unwrap();
        if rel.starts_with("platform") || rel == Path::new("main.rs") {
            continue;
        }
        let text = std::fs::read_to_string(&file).unwrap();
        for (n, line) in text.lines().enumerate() {
            if line.replace("cfg(test)", "").contains(&needle) {
                offenders.push(format!("{}:{}: {}", rel.display(), n + 1, line.trim()));
            }
        }
    }
    assert!(offenders.is_empty(), "cfg( outside src/platform and src/main.rs:\n{}", offenders.join("\n"));
}

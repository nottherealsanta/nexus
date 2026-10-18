//! Crash log: a panic (or a fatal error returned from `main`) restores the terminal and is
//! appended to `~/.nexus/tui-panic.log` with a backtrace, so the message is not lost in the
//! alternate screen. The file is bounded: past `LIMIT` bytes the oldest half is dropped.
use std::{fs, io::Write, path::PathBuf};

const LIMIT: u64 = 256 * 1024;

fn path() -> Option<PathBuf> {
    let home = std::env::var_os("NEXUS_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".nexus")))?;
    Some(home.join("tui-panic.log"))
}

/// Append one entry (bounded); returns where it went.
pub fn record(kind: &str, detail: &str) -> Option<PathBuf> {
    let path = path()?;
    fs::create_dir_all(path.parent()?).ok()?;
    if fs::metadata(&path).is_ok_and(|m| m.len() > LIMIT) {
        if let Ok(old) = fs::read(&path) {
            let mut keep = old[old.len() / 2..].to_vec();
            if let Some(nl) = keep.iter().position(|b| *b == b'\n') {
                keep.drain(..=nl);
            }
            let _ = fs::write(&path, keep);
        }
    }
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let mut file = fs::OpenOptions::new().create(true).append(true).open(&path).ok()?;
    let text: String = detail.chars().take(64 * 1024).collect();
    writeln!(file, "--- {kind} at unix {secs} (nexus-ratatui {})\n{text}\n", env!("CARGO_PKG_VERSION")).ok()?;
    Some(path)
}

/// Install the hook. `restore` puts the terminal back before the report is printed.
pub fn install(restore: fn()) {
    std::panic::set_hook(Box::new(move |info| {
        restore();
        let backtrace = std::backtrace::Backtrace::force_capture();
        let detail = format!("{info}\n{backtrace}");
        let logged = record("panic", &detail);
        eprintln!("nexus-ratatui panicked: {info}");
        if let Some(path) = logged {
            eprintln!("details: {}", path.display());
        }
    }));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_are_appended_and_bounded() {
        let dir = std::env::temp_dir().join(format!("nexus-panic-{}", std::process::id()));
        std::env::set_var("NEXUS_HOME", &dir);
        let file = record("test", "first").unwrap();
        assert!(fs::read_to_string(&file).unwrap().contains("first"));
        let big = "x".repeat(60 * 1024);
        for _ in 0..8 {
            record("test", &big);
        }
        assert!(fs::metadata(&file).unwrap().len() < LIMIT + 70 * 1024);
        let _ = fs::remove_dir_all(dir);
    }
}

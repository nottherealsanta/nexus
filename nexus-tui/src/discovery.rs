//! Finding (and, as the local bootstrap, starting) Nexus Core.
//!
//! The documented mechanism (docs/daemon.md): the daemon writes
//! `<home>/daemon.json` (`url`, `pid`, `token`, `started`; mode 0600) while it
//! runs, and a client trusts the file only after `GET /api/info` answers with
//! that token. `<home>` is `$NEXUS10_HOME` or `~/.nexus10`. This module never
//! touches `~/.nexus` and never reads anything else in the home directory except
//! the daemon log tail shown while Core starts.
//!
//! Starting Core uses the CLI's own mechanism, `nexus daemon start`, which
//! waits (15 s at most) until the daemon answers. Override the command with
//! `NEXUS_TUI_CORE_CMD` (for example `uv run --project ../nexus-core nexus`).

use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Deserialize;

use crate::transport::Http;

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct DaemonInfo {
    pub url: String,
    pub token: String,
    pub pid: i64,
    #[serde(default)]
    pub started: Option<f64>,
}

/// What identifies a daemon instance. A change means a restarted Core.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub pid: i64,
    pub token: String,
}

impl DaemonInfo {
    pub fn identity(&self) -> Identity {
        Identity {
            pid: self.pid,
            token: self.token.clone(),
        }
    }
}

/// `$NEXUS10_HOME`, else `~/.nexus`. `~/.nexus` (or anything inside it) is used only once it
/// carries Core's marker; until `nexus migrate` has run it is old Nexus's, or a pre-release
/// `~/.nexus10` is waiting to move in.
pub fn home() -> Result<PathBuf, String> {
    let home_dir = std::env::var_os("HOME").map(PathBuf::from);
    let path = resolve_home(
        std::env::var_os("NEXUS10_HOME").map(PathBuf::from),
        home_dir.clone(),
    )?;
    if let Some(h) = home_dir {
        check_migrated(&path, &h)?;
    }
    Ok(path)
}

pub const MARKER: &str = ".nexus10-home";

pub fn resolve_home(
    override_dir: Option<PathBuf>,
    home_dir: Option<PathBuf>,
) -> Result<PathBuf, String> {
    Ok(match override_dir.filter(|p| !p.as_os_str().is_empty()) {
        Some(p) => expand_tilde(&p, home_dir.as_deref()),
        None => home_dir
            .as_deref()
            .ok_or("cannot find the home directory; set NEXUS10_HOME")?
            .join(".nexus"),
    })
}

/// Refuse `<user home>/.nexus` until it is Nexus 0.10's. A machine with neither it nor
/// `~/.nexus10` is fresh: Core creates and marks the directory on first start.
pub fn check_migrated(path: &Path, user_home: &Path) -> Result<(), String> {
    let default = user_home.join(".nexus");
    if !path.starts_with(&default) || default.join(MARKER).is_file() {
        return Ok(());
    }
    if !default.exists() && !user_home.join(".nexus10").exists() {
        return Ok(());
    }
    Err(format!(
        "{} is not set up for Nexus 0.10 (it holds old Nexus or a pre-release home); run `nexus migrate`",
        default.display()
    ))
}

fn expand_tilde(path: &Path, home_dir: Option<&Path>) -> PathBuf {
    match (path.strip_prefix("~"), home_dir) {
        (Ok(rest), Some(h)) => h.join(rest),
        _ => path.to_path_buf(),
    }
}

pub fn info_path(home: &Path) -> PathBuf {
    home.join("daemon.json")
}

pub fn log_path(home: &Path) -> PathBuf {
    home.join("logs").join("daemon.log")
}

/// Parse `daemon.json`; `None` when absent or unreadable (the daemon is down
/// or mid-write; callers poll).
pub fn read_info(home: &Path) -> Option<DaemonInfo> {
    let text = std::fs::read_to_string(info_path(home)).ok()?;
    parse_info(&text)
}

pub fn parse_info(text: &str) -> Option<DaemonInfo> {
    serde_json::from_str(text).ok()
}

/// The last `lines` lines of the daemon log, for the "starting Nexus Core"
/// screen when startup is slow or fails.
pub fn log_tail(home: &Path, lines: usize) -> String {
    let text = std::fs::read_to_string(log_path(home)).unwrap_or_default();
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

/// `daemon.json` if its daemon answers `/api/info` with that token.
pub async fn discover(home: &Path) -> Option<DaemonInfo> {
    let info = read_info(home)?;
    let http = Http::new(&info.url, &info.token).ok()?;
    match tokio::time::timeout(Duration::from_secs(2), http.get("/api/info", &[])).await {
        Ok(Ok(_)) => Some(info),
        _ => None,
    }
}

/// Start Core in the background through the CLI and wait for it to answer.
pub async fn start_core(home: &Path) -> Result<DaemonInfo, String> {
    let command = std::env::var("NEXUS_TUI_CORE_CMD").unwrap_or_else(|_| "nexus".into());
    let mut parts = command.split_whitespace();
    let program = parts.next().ok_or("NEXUS_TUI_CORE_CMD is empty")?;
    let mut child = tokio::process::Command::new(program)
        .args(parts)
        .args(["daemon", "start"])
        .env("NEXUS10_HOME", home)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("could not run `{command} daemon start`: {e}. Start Core yourself with `nexus daemon start`"))?;
    let wait = tokio::time::timeout(Duration::from_secs(25), child.wait()).await;
    match wait {
        Ok(Ok(status)) if status.success() => {}
        Ok(Ok(status)) => {
            return Err(format!(
                "`{command} daemon start` failed ({status}); see {}",
                log_path(home).display()
            ))
        }
        Ok(Err(e)) => return Err(e.to_string()),
        Err(_) => {
            return Err(format!(
                "Core did not start within 25 s; see {}",
                log_path(home).display()
            ))
        }
    }
    discover(home).await.ok_or_else(|| {
        format!(
            "Core started but did not answer; see {}",
            log_path(home).display()
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn home_resolution() {
        let h = Some(PathBuf::from("/Users/a"));
        assert_eq!(
            resolve_home(None, h.clone()).unwrap(),
            PathBuf::from("/Users/a/.nexus")
        );
        assert_eq!(
            resolve_home(Some("/x/y".into()), h.clone()).unwrap(),
            PathBuf::from("/x/y")
        );
        assert_eq!(
            resolve_home(Some("~/nx".into()), h.clone()).unwrap(),
            PathBuf::from("/Users/a/nx")
        );
        assert_eq!(
            resolve_home(Some("".into()), h.clone()).unwrap(),
            PathBuf::from("/Users/a/.nexus")
        );
        assert_eq!(
            resolve_home(Some("~/.nexus".into()), h.clone()).unwrap(),
            PathBuf::from("/Users/a/.nexus")
        );
        assert!(resolve_home(None, None).is_err());
    }

    #[test]
    fn default_home_needs_core_marker() {
        let root = std::env::temp_dir().join(format!("nexus-home-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let default = root.join(".nexus");

        // Fresh machine: nothing to protect.
        assert!(check_migrated(&default, &root).is_ok());
        // Old Nexus: refused, including paths inside it.
        std::fs::create_dir(&default).unwrap();
        assert!(check_migrated(&default, &root)
            .unwrap_err()
            .contains("nexus migrate"));
        assert!(check_migrated(&default.join("sub"), &root).is_err());
        // Elsewhere is never checked.
        assert!(check_migrated(&root.join("scratch"), &root).is_ok());
        // Marked: used.
        std::fs::write(default.join(MARKER), "{}").unwrap();
        assert!(check_migrated(&default, &root).is_ok());
        // A waiting pre-release home blocks an absent ~/.nexus.
        std::fs::remove_dir_all(&default).unwrap();
        std::fs::create_dir(root.join(".nexus10")).unwrap();
        assert!(check_migrated(&default, &root).is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn parses_daemon_json() {
        let info =
            parse_info(r#"{"url":"http://127.0.0.1:4747","pid":42,"token":"t","started":1.5}"#)
                .unwrap();
        assert_eq!(
            info.identity(),
            Identity {
                pid: 42,
                token: "t".into()
            }
        );
        assert!(parse_info("{").is_none());
        assert!(parse_info(r#"{"url":"u"}"#).is_none());
    }

    #[test]
    fn tail_of_missing_log_is_empty() {
        assert_eq!(log_tail(Path::new("/nonexistent-nexus-home"), 5), "");
    }
}

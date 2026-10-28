//! Client preferences: `<home>/tui.json`.
//!
//! This is the TUI's own file, not Nexus state: Core never reads it and the
//! TUI reads nothing else from the Nexus home (see `discovery`). It remembers
//! layout and appearance across runs:
//!
//! ```json
//! {"ui": {"left_width": 44, "left_rail": false, "left_open": true, "right_open": true},
//!  "theme": {"mode": "dark", "monochrome": false, "animations": true}}
//! ```
//!
//! Every field is optional. A missing, unreadable or corrupt file is ignored,
//! and a save failure never disturbs the UI. Flags and environment override
//! the file for the current run and are not written back (see
//! `App::persist_prefs`). Writes are atomic: a temporary file at mode 0600,
//! then a rename.

use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const FILE: &str = "tui.json";

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct UiPrefs {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub left_width: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub left_rail: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub left_open: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub right_open: Option<bool>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ThemePrefs {
    /// `dark` or `light`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub monochrome: Option<bool>,
    /// `false` is reduced motion.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub animations: Option<bool>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Prefs {
    pub ui: UiPrefs,
    pub theme: ThemePrefs,
}

pub fn path(home: &Path) -> PathBuf {
    home.join(FILE)
}

/// The saved preferences, or the defaults when the file is missing or corrupt.
pub fn load(home: &Path) -> Prefs {
    std::fs::read(path(home))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

/// Write atomically at mode 0600.
pub fn save(home: &Path, prefs: &Prefs) -> std::io::Result<()> {
    let target = path(home);
    let tmp = home.join(format!(".{FILE}.{}.tmp", uuid::Uuid::new_v4().simple()));
    let mut body = serde_json::to_vec_pretty(prefs).map_err(std::io::Error::other)?;
    body.push(b'\n');
    let write = || -> std::io::Result<()> {
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&tmp)?;
        file.write_all(&body)?;
        file.sync_all()?;
        std::fs::rename(&tmp, &target)
    };
    let result = write();
    if result.is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
    result
}

/// Copy into `stored` the fields that differ between `seen` and `now`.
pub fn merge_changes(stored: &mut Prefs, seen: &Prefs, now: &Prefs) {
    macro_rules! field {
        ($($path:tt)+) => {
            if seen.$($path)+ != now.$($path)+ {
                stored.$($path)+ = now.$($path)+.clone();
            }
        };
    }
    field!(ui.left_width);
    field!(ui.left_rail);
    field!(ui.left_open);
    field!(ui.right_open);
    field!(theme.mode);
    field!(theme.monochrome);
    field!(theme.animations);
}

/// Which display options the command line or environment already decided.
#[derive(Debug, Clone, Copy, Default)]
pub struct Overrides {
    /// `--dark` or `--light`.
    pub mode: bool,
    /// `--mono`, `--256` or `--16`.
    pub tier: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("nexus-tui-prefs-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn sample() -> Prefs {
        Prefs {
            ui: UiPrefs {
                left_width: Some(44),
                left_rail: Some(false),
                left_open: Some(true),
                right_open: Some(false),
            },
            theme: ThemePrefs {
                mode: Some("light".into()),
                monochrome: Some(true),
                animations: Some(false),
            },
        }
    }

    #[test]
    fn round_trips_and_writes_a_private_file() {
        let d = dir("round");
        save(&d, &sample()).unwrap();
        assert_eq!(load(&d), sample());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(path(&d)).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        // No temporary file is left behind.
        let names: Vec<_> = std::fs::read_dir(&d)
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names, vec![std::ffi::OsString::from(FILE)]);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn missing_and_corrupt_files_give_defaults() {
        let d = dir("corrupt");
        assert_eq!(load(&d), Prefs::default());
        for bad in ["", "{", "[1,2]", "null", "{\"ui\": 3}", "\u{0}\u{1}"] {
            std::fs::write(path(&d), bad).unwrap();
            assert_eq!(load(&d), Prefs::default(), "{bad:?}");
        }
        // A corrupt file is replaced by the next save.
        save(&d, &sample()).unwrap();
        assert_eq!(load(&d), sample());
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn partial_files_and_unknown_keys_load() {
        let d = dir("partial");
        std::fs::write(
            path(&d),
            r#"{"ui": {"left_width": 50, "future": 1}, "other": true}"#,
        )
        .unwrap();
        let p = load(&d);
        assert_eq!(p.ui.left_width, Some(50));
        assert_eq!(p.ui.left_open, None);
        assert_eq!(p.theme, ThemePrefs::default());
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn saving_to_a_missing_home_fails_quietly() {
        let d = dir("gone");
        let missing = d.join("nope");
        assert!(save(&missing, &sample()).is_err());
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn merge_copies_only_what_changed() {
        let mut stored = sample();
        let seen = Prefs {
            theme: ThemePrefs {
                mode: Some("dark".into()),
                ..ThemePrefs::default()
            },
            ..Prefs::default()
        };
        // A flag made the run dark; the user then widened the sidebar.
        let mut now = seen.clone();
        now.ui.left_width = Some(60);
        merge_changes(&mut stored, &seen, &now);
        assert_eq!(stored.ui.left_width, Some(60));
        assert_eq!(stored.theme.mode.as_deref(), Some("light"));
    }
}

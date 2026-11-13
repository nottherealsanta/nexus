//! Client preferences: `<home>/tui.json`.
//!
//! This is the TUI's own file, not Nexus state: Core never reads it and the
//! TUI reads nothing else from the Nexus home (see `discovery`). It remembers
//! layout and appearance across runs:
//!
//! ```json
//! {"ui": {"left_width": 44, "left_rail": false, "left_open": true, "right_open": true, "scrollbar": true},
//!  "theme": {"mode": "dark", "monochrome": false, "animations": true},
//!  "notifications": {"sound": true},
//!  "layouts": {"<project>\u{1f}<worktree>": {"root": {"node": "group", "id": 1, "tabs": [], "active": 0}, "focused": 1, "next_id": 2}}}
//! ```
//!
//! `layouts` is the tab layout of each project worktree (plan 45, `groups`).
//!
//! Every field is optional. A missing, unreadable or corrupt file is ignored,
//! and a save failure never disturbs the UI. Flags and environment override
//! the file for the current run and are not written back (see
//! `App::persist_prefs`). Writes are atomic: a temporary file at mode 0600,
//! then a rename.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::groups::Layout;

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
    /// The timeline's scroll bar, shown while scrolling and then hidden.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scrollbar: Option<bool>,
    /// `low`, `medium` or `high`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ThemePrefs {
    /// `system`, `dark` or `light`.
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
pub struct NotificationsPrefs {
    /// Cues for a session finishing and for one that needs the user.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sound: Option<bool>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Prefs {
    pub ui: UiPrefs,
    pub theme: ThemePrefs,
    pub notifications: NotificationsPrefs,
    /// The tab layout of each project worktree (plan 45), keyed by
    /// `groups::key`. A layout that fails to parse is dropped, not the file.
    #[serde(
        skip_serializing_if = "BTreeMap::is_empty",
        deserialize_with = "tolerant_layouts"
    )]
    pub layouts: BTreeMap<String, Layout>,
}

/// Each saved layout on its own: one that is damaged is skipped.
fn tolerant_layouts<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> Result<BTreeMap<String, Layout>, D::Error> {
    let raw = BTreeMap::<String, serde_json::Value>::deserialize(d).unwrap_or_default();
    Ok(raw
        .into_iter()
        .filter_map(|(k, v)| serde_json::from_value(v).ok().map(|l| (k, l)))
        .collect())
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
    field!(ui.scrollbar);
    field!(ui.detail);
    field!(theme.mode);
    field!(theme.monochrome);
    field!(theme.animations);
    field!(notifications.sound);
    // Layouts merge per key: another run's other projects survive.
    for (key, layout) in &now.layouts {
        if seen.layouts.get(key) != Some(layout) {
            stored.layouts.insert(key.clone(), layout.clone());
        }
    }
    for key in seen.layouts.keys() {
        if !now.layouts.contains_key(key) {
            stored.layouts.remove(key);
        }
    }
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
                scrollbar: Some(false),
                detail: Some("high".into()),
            },
            theme: ThemePrefs {
                mode: Some("light".into()),
                monochrome: Some(true),
                animations: Some(false),
            },
            notifications: NotificationsPrefs { sound: Some(false) },
            layouts: BTreeMap::new(),
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
    fn layouts_round_trip_merge_per_key_and_skip_damaged_ones() {
        use crate::groups::{Layout, Side, Tab};
        use ratatui::layout::Rect;
        let d = dir("layouts");
        let mut layout = Layout::new();
        layout.open(Tab::session("s1"));
        layout
            .split(Side::Right, Tab::file("a.rs"), Rect::new(0, 0, 100, 30))
            .unwrap();
        let mut prefs = sample();
        prefs
            .layouts
            .insert(crate::groups::key("p", Some("/w")), layout.clone());
        save(&d, &prefs).unwrap();
        assert_eq!(load(&d).layouts, prefs.layouts);
        // A damaged layout is skipped; the rest of the file survives.
        let text = std::fs::read_to_string(path(&d))
            .unwrap()
            .replace("\"node\": \"split\"", "\"node\": 7");
        std::fs::write(path(&d), text).unwrap();
        let back = load(&d);
        assert!(back.layouts.is_empty());
        assert_eq!(back.ui.left_width, Some(44));
        // Merging copies only the layouts this run changed or removed.
        let mut stored = Prefs::default();
        stored.layouts.insert("other".into(), layout.clone());
        stored.layouts.insert("mine".into(), Layout::new());
        let mut seen = Prefs::default();
        seen.layouts.insert("mine".into(), Layout::new());
        let mut now = Prefs::default();
        now.layouts.insert("mine".into(), layout.clone());
        merge_changes(&mut stored, &seen, &now);
        assert_eq!(stored.layouts["mine"], layout);
        assert!(
            stored.layouts.contains_key("other"),
            "another run's project stays"
        );
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

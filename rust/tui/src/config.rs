//! Optional local UI appearance preferences (`~/.config/nexus/tui.json`); never changes the
//! host theme. Reads are bounded and refuse symlinks; writes merge into existing JSON.
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

const MAX_BYTES: u64 = 64 * 1024;

#[derive(Default, serde::Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Animations {
    #[serde(default)]
    #[serde(alias = "animations")]
    pub animate_activity: Option<bool>,
    /// Monochrome / plain glyph rendering preference (`mono` key).
    #[serde(default)]
    pub mono: Option<bool>,
}
/// Alias used by the settings checkbox code.
pub type Appearance = Animations;

pub fn path() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config/nexus/tui.json"))
}
/// Regular file (not a symlink) no larger than `MAX_BYTES`.
fn read_bounded(path: &Path) -> Option<Vec<u8>> {
    let meta = std::fs::symlink_metadata(path).ok()?;
    if !meta.file_type().is_file() || meta.len() > MAX_BYTES {
        return None;
    }
    std::fs::read(path).ok()
}
impl Animations {
    pub fn load() -> Self {
        path().map_or_else(Self::default, |p| Self::load_from(&p))
    }
    pub fn load_from(path: &Path) -> Self {
        read_bounded(path)
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default()
    }
    /// Animation is on unless the user turned it off or the environment asks for plain output.
    pub fn enabled(&self) -> bool {
        self.animate_activity.unwrap_or(true)
            && std::env::var_os("NO_COLOR").is_none()
            && std::env::var("NEXUS_ASCII").as_deref() != Ok("1")
    }
    /// Monochrome: explicit preference or `NO_COLOR`.
    pub fn mono_enabled(&self) -> bool {
        self.mono.unwrap_or(false) || std::env::var_os("NO_COLOR").is_some()
    }
}

/// Checkbox rows for the Appearance page: `(key, label, current value)`.
pub fn checkboxes(a: &Animations) -> [(&'static str, &'static str, bool); 2] {
    [
        (
            "animate_activity",
            "Animate activity",
            a.animate_activity.unwrap_or(true),
        ),
        ("mono", "Monochrome", a.mono.unwrap_or(false)),
    ]
}
/// Flip a checkbox in `a`; returns the new value, or `None` for an unknown key.
pub fn toggle(a: &mut Animations, key: &str) -> Option<bool> {
    let slot = match key {
        "animate_activity" => (&mut a.animate_activity, true),
        "mono" => (&mut a.mono, false),
        _ => return None,
    };
    let next = !slot.0.unwrap_or(slot.1);
    *slot.0 = Some(next);
    Some(next)
}
/// Serialize `a` into `existing` JSON (unknown keys preserved). Non-object input is replaced.
pub fn merge(existing: Option<&[u8]>, a: &Animations) -> Vec<u8> {
    let mut root = existing
        .and_then(|b| serde_json::from_slice::<Value>(b).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}));
    let map = root.as_object_mut().expect("object");
    map.remove("animations");
    for (key, value) in [("animate_activity", a.animate_activity), ("mono", a.mono)] {
        if let Some(v) = value {
            map.insert(key.into(), Value::Bool(v));
        }
    }
    let mut out = serde_json::to_vec_pretty(&root).unwrap_or_default();
    out.push(b'\n');
    out
}
/// Persist to `path` atomically (temp file + rename). Refuses a symlink target.
pub fn save_to(path: &Path, a: &Animations) -> std::io::Result<()> {
    if let Ok(meta) = std::fs::symlink_metadata(path) {
        if !meta.file_type().is_file() {
            return Err(std::io::Error::other("refusing non-regular tui.json"));
        }
    }
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let bytes = merge(read_bounded(path).as_deref(), a);
    let tmp = path.with_extension(format!("json.tmp{}", std::process::id()));
    std::fs::write(&tmp, bytes)?;
    std::fs::rename(&tmp, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&tmp);
    })
}
pub fn save(a: &Animations) -> std::io::Result<()> {
    match path() {
        Some(p) => save_to(&p, a),
        None => Err(std::io::Error::other("HOME is not set")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("nexus-cfg-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }
    #[test]
    fn explicit_animation_preference() {
        let off = Animations {
            animate_activity: Some(false),
            mono: None,
        };
        assert!(!off.enabled());
        let on = Animations {
            animate_activity: Some(true),
            mono: None,
        };
        // NO_COLOR in the environment legitimately disables it too.
        if std::env::var_os("NO_COLOR").is_none() && std::env::var("NEXUS_ASCII").is_err() {
            assert!(on.enabled());
        }
    }
    #[test]
    fn toggle_and_checkboxes() {
        let mut a = Animations::default();
        assert_eq!(checkboxes(&a)[0].2, true);
        assert_eq!(toggle(&mut a, "animate_activity"), Some(false));
        assert_eq!(toggle(&mut a, "mono"), Some(true));
        assert_eq!(toggle(&mut a, "bogus"), None);
        assert_eq!((a.animate_activity, a.mono), (Some(false), Some(true)));
    }
    #[test]
    fn save_merges_and_roundtrips() {
        let d = dir("rt");
        let p = d.join("sub/tui.json");
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(&p, br#"{"keep":1,"animations":true}"#).unwrap();
        let a = Animations {
            animate_activity: Some(false),
            mono: Some(true),
        };
        save_to(&p, &a).unwrap();
        let v: Value = serde_json::from_slice(&std::fs::read(&p).unwrap()).unwrap();
        assert_eq!(v["keep"], 1);
        assert_eq!(v["animate_activity"], false);
        assert!(v.get("animations").is_none());
        assert_eq!(Animations::load_from(&p), a);
    }
    #[test]
    fn rejects_oversize_malformed_and_symlink() {
        let d = dir("bad");
        let big = d.join("big.json");
        std::fs::write(&big, vec![b' '; MAX_BYTES as usize + 1]).unwrap();
        assert_eq!(Animations::load_from(&big), Animations::default());
        let bad = d.join("bad.json");
        std::fs::write(&bad, b"{nope").unwrap();
        assert_eq!(Animations::load_from(&bad), Animations::default());
        #[cfg(unix)]
        {
            let real = d.join("real.json");
            std::fs::write(&real, br#"{"mono":true}"#).unwrap();
            let link = d.join("link.json");
            std::os::unix::fs::symlink(&real, &link).unwrap();
            assert_eq!(Animations::load_from(&link), Animations::default());
            assert!(save_to(&link, &Animations::default()).is_err());
        }
    }
}

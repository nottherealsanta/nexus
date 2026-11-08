//! Local Core discovery honors the host's explicit scratch/state home.
//! The bridge exposes this one bootstrap read, never arbitrary filesystem access.
use nsh_core::prelude::*;
#[cfg(not(any(target_arch = "wasm32", target_os = "ios")))]
use std::{
    fs::File,
    io::Read,
    path::{Path, PathBuf},
};

#[cfg(not(any(target_arch = "wasm32", target_os = "ios")))]
const MAX_BOOTSTRAP: u64 = 16 * 1024;

#[derive(JsObject)]
pub struct DaemonLocation {
    pub url: String,
    pub token: String,
}

/// Read the local daemon bootstrap from the selected native state home.
#[module]
pub struct Discovery;

#[module]
impl Discovery {
    /// Return the local Core address and token, or null when no valid bootstrap exists.
    pub fn local_core() -> Option<DaemonLocation> {
        #[cfg(any(target_arch = "wasm32", target_os = "ios"))]
        {
            None
        }
        #[cfg(not(any(target_arch = "wasm32", target_os = "ios")))]
        {
            let custom = std::env::var_os("NEXUS10_HOME")
                .filter(|value| !value.is_empty())
                .map(PathBuf::from);
            let home = std::env::var_os("HOME").map(PathBuf::from);
            discover(custom.as_deref(), home.as_deref())
        }
    }
}

#[cfg(not(any(target_arch = "wasm32", target_os = "ios")))]
fn discover(custom: Option<&Path>, home: Option<&Path>) -> Option<DaemonLocation> {
    let candidates = if let Some(custom) = custom {
        // An explicit home is authoritative: never fall through to the real daemon.
        vec![custom.to_owned()]
    } else {
        let home = home?;
        let migrated = home.join(".nexus");
        if migrated.join(".nexus10-home").is_file() {
            vec![migrated, home.join(".nexus10")]
        } else {
            vec![home.join(".nexus10")]
        }
    };
    for directory in candidates {
        let Some(bytes) = read_bootstrap(&directory.join("daemon.json")) else {
            continue;
        };
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
            continue;
        };
        let Some(url) = value["url"].as_str() else {
            continue;
        };
        let Some(token) = value["token"].as_str() else {
            continue;
        };
        // Network-bound Core records an empty token, including the default wildcard bind.
        if !url.is_empty() && url.len() <= 2048 && token.len() <= 4096 {
            return Some(DaemonLocation {
                url: url.trim_end_matches('/').to_owned(),
                token: token.to_owned(),
            });
        }
    }
    None
}

#[cfg(not(any(target_arch = "wasm32", target_os = "ios")))]
fn read_bootstrap(path: &Path) -> Option<Vec<u8>> {
    let mut bytes = Vec::new();
    File::open(path)
        .ok()?
        .take(MAX_BOOTSTRAP + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    (bytes.len() as u64 <= MAX_BOOTSTRAP).then_some(bytes)
}

#[cfg(all(test, not(any(target_arch = "wasm32", target_os = "ios"))))]
mod tests {
    use super::*;
    fn root(label: &str) -> PathBuf {
        let root =
            std::env::temp_dir().join(format!("nexus-discovery-{label}-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        root
    }
    fn daemon(root: &Path, url: &str) {
        std::fs::create_dir_all(root).unwrap();
        std::fs::write(
            root.join("daemon.json"),
            serde_json::json!({"url": url, "token":"fixture-token"}).to_string(),
        )
        .unwrap();
    }
    #[test]
    fn explicit_home_never_falls_back_to_personal_state() {
        let root = root("explicit");
        daemon(&root.join(".nexus10"), "http://127.0.0.1:1");
        let custom = root.join("scratch");
        std::fs::create_dir(&custom).unwrap();
        assert!(discover(Some(&custom), Some(&root)).is_none());
        daemon(&custom, "http://127.0.0.1:2/");
        assert_eq!(
            discover(Some(&custom), Some(&root)).unwrap().url,
            "http://127.0.0.1:2"
        );
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn unmarked_legacy_home_is_not_a_nexus_core() {
        let root = root("marker");
        daemon(&root.join(".nexus"), "http://127.0.0.1:1");
        assert!(discover(None, Some(&root)).is_none());
        std::fs::write(root.join(".nexus/.nexus10-home"), "").unwrap();
        assert_eq!(
            discover(None, Some(&root)).unwrap().url,
            "http://127.0.0.1:1"
        );
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn tokenless_network_core_is_discovered() {
        let root = root("tokenless");
        std::fs::write(
            root.join("daemon.json"),
            serde_json::json!({"url": "http://127.0.0.1:4747", "token": ""}).to_string(),
        )
        .unwrap();
        let location = discover(Some(&root), None).unwrap();
        assert_eq!(location.url, "http://127.0.0.1:4747");
        assert!(location.token.is_empty());
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn oversized_or_invalid_bootstrap_is_refused() {
        let root = root("bounds");
        std::fs::write(
            root.join("daemon.json"),
            vec![b'x'; MAX_BOOTSTRAP as usize + 1],
        )
        .unwrap();
        assert!(discover(Some(&root), None).is_none());
        std::fs::write(root.join("daemon.json"), "{}").unwrap();
        assert!(discover(Some(&root), None).is_none());
        std::fs::remove_dir_all(root).unwrap();
    }
}

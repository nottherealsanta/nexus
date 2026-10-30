//! The shared interface manifest (`interface/manifest.toml`), embedded at build
//! time and parsed into typed structs. No command id, label, key or reason is
//! hand-copied into the renderer: everything user-visible about a command comes
//! from here. Schema: `interface/README.md`.

use std::collections::BTreeMap;

use serde::Deserialize;

const SOURCE: &str = include_str!("../../interface/manifest.toml");

#[derive(Debug, Clone, Deserialize)]
pub struct Manifest {
    pub manifest_version: u32,
    pub vocabulary: Vocabulary,
    pub keymap: Keymap,
    pub navigation: Navigation,
    pub settings: Settings,
    #[serde(default)]
    pub command: Vec<Command>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Vocabulary {
    pub terms: Vec<String>,
    pub placements: Vec<String>,
    pub statuses: Vec<String>,
    pub categories: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Keymap {
    pub contexts: Vec<String>,
    pub escape: Vec<EscapeRule>,
}

/// One rule of the Esc precedence chain; the first whose `when` holds wins.
#[derive(Debug, Clone, Deserialize)]
pub struct EscapeRule {
    pub id: String,
    pub when: Vec<String>,
    pub action: String,
    #[serde(default)]
    pub note: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Keys {
    pub tui: Option<String>,
    pub nsh: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Command {
    pub id: String,
    pub title: String,
    pub category: String,
    pub slash: Option<String>,
    #[serde(default)]
    pub slash_aliases: Vec<String>,
    pub keys: Option<Keys>,
    pub key_context: Option<String>,
    #[serde(default)]
    pub when: Vec<String>,
    pub unavailable_reason: Option<String>,
    #[serde(default)]
    pub placement: Vec<String>,
    #[serde(default = "both")]
    pub clients: String,
    /// `blocked_core` or `deferred`; absent means the Core API is ready.
    pub status: Option<String>,
    #[serde(default)]
    pub note: String,
}

fn both() -> String {
    "both".into()
}

impl Command {
    pub fn tui_key(&self) -> Option<&str> {
        self.keys.as_ref().and_then(|k| k.tui.as_deref())
    }
    pub fn for_tui(&self) -> bool {
        self.clients != "nsh"
    }
    /// Every command is reachable from the palette (interface/README.md);
    /// Esc-chain actions and modal dialog keys are not commands to browse.
    pub fn in_palette(&self) -> bool {
        self.for_tui() && self.key_context.as_deref() != Some("escape_chain")
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Navigation {
    pub header: Header,
    pub left_sidebar: LeftSidebar,
    pub right_sidebar: RightSidebar,
    pub centre: Centre,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Header {
    pub leading: String,
    pub centre: String,
    pub trailing: String,
    pub title_click: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct LeftSidebar {
    pub default_tab: String,
    pub settings_entry: String,
    pub tab: Vec<Tab>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Tab {
    pub id: String,
    pub label: String,
    pub command: String,
    pub order: u32,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RightSidebar {
    pub section: Vec<RightSection>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RightSection {
    pub id: String,
    pub label: String,
    pub order: u32,
    pub unavailable_reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Centre {
    pub views: Vec<String>,
    pub default: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Settings {
    pub section: Vec<Section>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Section {
    pub id: String,
    pub label: String,
    pub unavailable_reason: Option<String>,
    /// One entry that opens its own page (Providers, Agents); its rows are drawn on that page.
    #[serde(default)]
    pub page: bool,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub row: Vec<Row>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Row {
    pub id: String,
    pub label: String,
    pub kind: String,
    pub scope: Option<String>,
    #[serde(default = "both")]
    pub clients: String,
    pub default: Option<toml::Value>,
    #[serde(default)]
    pub options: Vec<String>,
    pub command: Option<String>,
    pub unavailable_reason: Option<String>,
    #[serde(default)]
    pub note: String,
}

impl Row {
    pub fn for_tui(&self) -> bool {
        self.clients != "nsh"
    }
}

impl Manifest {
    /// The manifest compiled into this binary.
    pub fn load() -> Self {
        Self::parse(SOURCE)
            .expect("interface/manifest.toml is invalid; run interface/check_manifest.py")
    }

    pub fn parse(text: &str) -> Result<Self, toml::de::Error> {
        toml::from_str(text)
    }

    pub fn command(&self, id: &str) -> Option<&Command> {
        self.command.iter().find(|c| c.id == id)
    }

    /// Settings sections as the TUI shows them (rows for other clients dropped).
    pub fn settings_for_tui(&self) -> Vec<Section> {
        self.settings
            .section
            .iter()
            .map(|s| Section {
                row: s.row.iter().filter(|r| r.for_tui()).cloned().collect(),
                ..s.clone()
            })
            .collect()
    }

    /// Resolve a slash word (`/compact`, alias or name) to a command id.
    pub fn slash(&self, word: &str) -> Option<&Command> {
        self.command.iter().find(|c| {
            c.for_tui()
                && (c.slash.as_deref() == Some(word) || c.slash_aliases.iter().any(|a| a == word))
        })
    }

    /// Keys of the TUI by context, for hints and tests.
    pub fn keys_by_context(&self) -> BTreeMap<String, Vec<(String, String)>> {
        let mut m: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
        for c in self.command.iter().filter(|c| c.for_tui()) {
            if let (Some(k), Some(ctx)) = (c.tui_key(), c.key_context.as_ref()) {
                m.entry(ctx.clone())
                    .or_default()
                    .push((k.to_owned(), c.id.clone()));
            }
        }
        m
    }
}

/// Split a `when` term into (negated, name).
pub fn term(t: &str) -> (bool, &str) {
    match t.strip_prefix('!') {
        Some(rest) => (true, rest),
        None => (false, t),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn copy_session_id_slash_resolves() {
        let m = Manifest::load();
        assert_eq!(
            m.slash("copy-session-id").map(|c| c.id.as_str()),
            Some("session.copy_id")
        );
    }

    #[test]
    fn manifest_parses_with_the_checker_invariants() {
        let m = Manifest::load();
        assert_eq!(m.manifest_version, 1);
        let mut ids = HashSet::new();
        let mut slashes = HashSet::new();
        let terms: HashSet<&str> = m.vocabulary.terms.iter().map(String::as_str).collect();
        for c in &m.command {
            assert!(ids.insert(&c.id), "duplicate id {}", c.id);
            for s in c.slash.iter().chain(c.slash_aliases.iter()) {
                assert!(slashes.insert(s), "duplicate slash {s}");
            }
            for t in &c.when {
                assert!(terms.contains(term(t).1), "{}: unknown term {t}", c.id);
            }
            if c.when != ["always"] {
                assert!(c.unavailable_reason.is_some(), "{} needs a reason", c.id);
            }
        }
        // Every command bound to esc in the escape_chain context is the action
        // of exactly one rule, and every rule's action is a real command.
        for c in m
            .command
            .iter()
            .filter(|c| c.key_context.as_deref() == Some("escape_chain"))
        {
            let n = m.keymap.escape.iter().filter(|r| r.action == c.id).count();
            assert_eq!(
                n, 1,
                "{} must be the action of exactly one escape rule",
                c.id
            );
        }
        for rule in &m.keymap.escape {
            assert!(m.command(&rule.action).is_some(), "{}", rule.action);
            for t in &rule.when {
                assert!(terms.contains(term(t).1));
            }
        }
        let chain = m
            .keymap
            .escape
            .iter()
            .map(|r| r.id.as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            chain,
            [
                "overlay",
                "dialog",
                "text_edit",
                "drawer",
                "alert",
                "interrupt",
                "editor",
                "back",
                "settings",
                "refocus"
            ]
        );
    }

    #[test]
    fn settings_order_is_fixed() {
        let m = Manifest::load();
        let ids: Vec<&str> = m.settings.section.iter().map(|s| s.id.as_str()).collect();
        assert_eq!(
            ids,
            [
                "appearance",
                "providers",
                "agents",
                "tools",
                "skills",
                "mcp",
                "context",
                "dictation",
                "keybindings",
                "notifications",
                "about"
            ]
        );
        let tabs: Vec<&str> = m
            .navigation
            .left_sidebar
            .tab
            .iter()
            .map(|t| t.id.as_str())
            .collect();
        assert_eq!(tabs, ["sessions", "files", "projects"]);
    }

    #[test]
    fn nsh_only_rows_are_dropped() {
        let m = Manifest::load();
        let a = &m.settings_for_tui()[0];
        assert!(a.row.iter().all(|r| r.id != "appearance.font_size"));
        assert!(a.row.iter().any(|r| r.id == "appearance.monochrome"));
    }
}

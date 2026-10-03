//! Versioned owned presentation data; no host/domain reduction in Rust.
use serde::Deserialize;
use serde_json::Value;
#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Snapshot {
    pub schema: u32,
    pub revision: u64,
    /// Monotonic live notification count, independent of transcript replay.
    pub completion_bell: u64,
    pub generation: u64,
    pub composer_key: String,
    pub title: String,
    pub status: String,
    pub lines: Vec<String>,
    pub blocks: Vec<Content>,
    pub blocks_from: usize,
    pub context_lines: Vec<String>,
    pub panel_title: String,
    /// Dim key hints on the bottom row of a list dialog (`Favorite ctrl+f`).
    pub panel_hint: String,
    pub panel_layout: String,
    pub panel_format: String,
    pub preview_image: String,
    pub preview_image_media: String,
    pub panel_loading: bool,
    pub panel_lines: Vec<String>,
    /// One tone per panel line (`title`, `header`, `label`, `kv`, `add`, `del`, `hunk`); empty = plain.
    pub panel_tones: Vec<String>,
    pub items: Vec<Item>,
    pub prompt: Option<Prompt>,
    pub form: Option<Form>,
    pub controls: Vec<Item>,
    pub restore: String,
    pub insert: String,
    pub auto_send_insert: bool,
    pub insert_kind: String,
    pub history: Vec<String>,
    pub agent: String,
    pub agent_color: String,
    pub agent_page: String,
    pub model: String,
    pub provider: String,
    pub effort: String,
    pub context_usage: String,
    pub context_note: String,
    pub context_label: String,
    pub context_used: Option<u64>,
    pub context_window: Option<u64>,
    pub context_marks: Vec<u64>,
    pub context_tiers: Vec<u64>,
    pub attachments: usize,
    pub attachment_lines: Vec<String>,
    /// Queued, steering and interrupt messages waiting for the running turn.
    pub queue_lines: Vec<String>,
    pub update_notice: String,
    /// Settings area list: (label, key, is heading), and the selected index (-1 = none).
    pub nav: Option<Nav>,
    pub sessions: Vec<Session>,
    pub tabs: Vec<Session>,
    pub archived_label: String,
    pub sessions_truncated: bool,
    pub breadcrumb: String,
    pub details_panel: DetailsPanel,
    pub logs: Vec<String>,
    pub theme: String,
    pub sessions_sidebar: bool,
    pub details_sidebar: bool,
    /// Local width arbitration, preserved across Python snapshots.
    pub last_opened: String,
    pub context_preview: bool,
    pub voice_phase: String,
    pub voice_preview: String,
    pub voice_level: f64,
    pub completions: Vec<String>,
    pub completion_query: String,
    pub completion_prefix: String,
}
#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct Item {
    pub label: String,
    pub command: String,
    pub operation: Option<Value>,
    pub toggle_operation: Option<Value>,
    pub toggle_enabled: Option<bool>,
    pub toggle_locked: bool,
    /// Heading shown above the first item of each group (model picker); display only.
    #[serde(default)]
    pub group: String,
    /// Dim text after the label (model picker: the provider/model ref).
    pub detail: String,
    /// The active choice: drawn with a leading `●` in the accent colour.
    pub current: bool,
}
#[derive(Deserialize)]
pub struct Prompt {
    pub kind: String,
    pub id: String,
    pub lines: Vec<String>,
    pub choices: Vec<Choice>,
}
#[derive(Deserialize)]
pub struct Choice {
    pub label: String,
    pub value: String,
    pub key: String,
    pub disabled: bool,
}
#[derive(Deserialize)]
#[serde(default)]
pub struct Form {
    pub id: String,
    pub body: String,
    pub secret: bool,
    pub autosave: bool,
    pub status: String,
    pub revision: u64,
}
impl Default for Form {
    fn default() -> Self {
        Self {
            id: String::new(),
            body: String::new(),
            secret: false,
            autosave: false,
            status: String::new(),
            revision: 0,
        }
    }
}
#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Session {
    #[serde(default)]
    pub group: String,
    pub id: String,
    pub title: String,
    pub workspace: String,
    pub state: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub sub: String,
    #[serde(default)]
    pub active: bool,
}

#[derive(Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Content {
    pub id: String,
    pub title: String,
    pub text: String,
    pub kind: String,
    pub operation: Option<Value>,
    pub path: String,
    pub added: u64,
    pub removed: u64,
    /// Side-by-side rows: old line, old text, new line, new text, kind.
    pub diff_rows: Vec<(u32, String, u32, String, String)>,
    pub gap: u16,
    pub number: u32,
    pub collapsed: bool,
    pub chips: Vec<String>,
    pub chip_operation: Option<Value>,
    pub detail: String,
    /// Full tool heading (`→ Read path`) shown on an expanded group member.
    pub heading: String,
    pub status: String,
    /// Parallel-call marker occupies the left gutter, never the tool text.
    pub batch_glyph: String,
    pub color: String,
    pub rev: String,
    pub count: usize,
    pub failures: usize,
    /// Header chip counts: `[total]`, or `[project, global]` for skills and MCP.
    pub counts: Vec<usize>,
    pub members: Vec<Content>,
    pub output_operation: Option<Value>,
}

#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Nav {
    pub items: Vec<(String, String, bool)>,
    pub selected: i64,
}
#[derive(Default, Deserialize)]
#[serde(default)]
pub struct DetailsPanel {
    pub tab: String,
    pub logs_header: Vec<(String, String)>,
    pub session: Vec<(String, String)>,
    pub files: Vec<FileChange>,
    pub files_summary: String,
    pub mcp: Vec<(String, String, String)>,
}
#[derive(Default, Deserialize)]
#[serde(default)]
pub struct FileChange {
    pub path: String,
    pub added: u64,
    pub removed: u64,
    pub created: bool,
    pub open: bool,
    pub diff: Vec<String>,
}

impl Snapshot {
    /// Schema 2 suffixes are dependent patches; never discard an intermediate patch.
    pub fn restore_blocks(&mut self, previous: &mut Snapshot) -> Result<(), &'static str> {
        if self.schema != 2 {
            return Ok(());
        }
        if self.blocks_from > previous.blocks.len() {
            return Err("invalid transcript patch offset");
        }
        if self.blocks_from > 0
            && (self.generation != previous.generation || self.agent_page != previous.agent_page)
        {
            return Err("transcript patch crosses session/page");
        }
        previous.blocks.truncate(self.blocks_from);
        previous.blocks.append(&mut self.blocks);
        self.blocks = std::mem::take(&mut previous.blocks);
        Ok(())
    }
}
#[cfg(test)]
mod redesign_tests {
    use super::*;
    #[test]
    fn suffix_patches_append_replace_truncate_and_reject_stale_page() {
        let block = |id: &str| Content {
            id: id.into(),
            ..Default::default()
        };
        let mut previous = Snapshot {
            blocks: vec![block("a"), block("b")],
            ..Default::default()
        };
        let mut patch = Snapshot {
            schema: 2,
            blocks_from: 1,
            blocks: vec![block("new")],
            ..Default::default()
        };
        patch.restore_blocks(&mut previous).unwrap();
        assert_eq!(
            patch
                .blocks
                .iter()
                .map(|b| b.id.as_str())
                .collect::<Vec<_>>(),
            vec!["a", "new"]
        );
        let mut truncated = Snapshot {
            schema: 2,
            blocks_from: 1,
            ..Default::default()
        };
        truncated.restore_blocks(&mut patch).unwrap();
        assert_eq!(truncated.blocks.len(), 1);
        let mut wrong = Snapshot {
            schema: 2,
            blocks_from: 1,
            generation: 1,
            ..Default::default()
        };
        assert!(wrong.restore_blocks(&mut truncated).is_err());
        assert_eq!(truncated.blocks.len(), 1);
        wrong.blocks_from = 3;
        assert!(wrong.restore_blocks(&mut truncated).is_err());
        wrong.blocks_from = 0;
        wrong.blocks = vec![block("fresh")];
        wrong.restore_blocks(&mut truncated).unwrap();
        assert_eq!(wrong.blocks[0].id, "fresh");
        let mut legacy = Snapshot {
            schema: 1,
            blocks: vec![block("legacy")],
            ..Default::default()
        };
        legacy.restore_blocks(&mut wrong).unwrap();
        assert_eq!(legacy.blocks[0].id, "legacy");
    }
}

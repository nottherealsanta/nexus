//! Versioned owned presentation data; no host/domain reduction in Rust.
use serde::Deserialize;
use serde_json::Value;
#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Snapshot {
    pub schema: u32,
    pub revision: u64,
    pub generation: u64,
    pub title: String,
    pub status: String,
    pub lines: Vec<String>,
    pub blocks: Vec<Content>,
    pub context_lines: Vec<String>,
    pub panel_title: String,
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
    pub model: String,
    pub provider: String,
    pub effort: String,
    pub context_usage: String,
    pub attachments: usize,
    pub attachment_lines: Vec<String>,
    /// Queued, steering and interrupt messages waiting for the running turn.
    pub queue_lines: Vec<String>,
    pub update_notice: String,
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
    pub context_preview: bool,
    pub voice_phase: String,
    pub voice_preview: String,
    pub voice_level: f64,
    pub completions: Vec<String>,
    pub completion_query: String,
}
#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct Item {
    pub label: String,
    pub command: String,
    pub operation: Option<Value>,
    /// Heading shown above the first item of each group (model picker); display only.
    #[serde(default)]
    pub group: String,
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
    pub status: String,
    pub color: String,
}

#[derive(Default, Deserialize)]
#[serde(default)]
pub struct DetailsPanel {
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

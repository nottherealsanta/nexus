//! Versioned owned presentation data; no host/domain reduction in Rust.
use serde::Deserialize;
use serde_json::Value;
#[derive(Default, Deserialize)]
#[serde(default)]
pub struct Snapshot {
    pub schema: u32,
    pub revision: u64,
    pub event_sent_at: f64,
    pub event_kind: String,
    /// Monotonic live notification count, independent of transcript replay.
    pub completion_bell: u64,
    pub generation: u64,
    pub composer_key: String,
    pub title: String,
    pub status: String,
    pub lines: Vec<String>,
    pub blocks: Vec<Content>,
    pub blocks_from: usize,
    pub reset: bool,
    pub transcript_verbose: bool,
    pub local_ui_enabled: bool,
    pub ui_ack: u64,
    #[serde(skip)]
    pub ui_local_revision: u64,
    pub logs_show_all: bool,
    pub logs_all: Vec<String>,
    pub logs_folded: Vec<String>,
    pub commands: Vec<(String, Vec<String>)>,
    pub command_help: std::collections::HashMap<String, String>,
    pub history_append: Vec<String>,
    #[serde(skip)]
    pub transcript_changed_from: Option<usize>,
    pub context_lines: Vec<String>,
    pub panel_title: String,
    /// Dim key hints on the bottom row of a list dialog (`Favorite ctrl+f`).
    pub panel_hint: String,
    pub panel_layout: String,
    pub panel_format: String,
    /// Space on an item-less page (the tool page) sends this operation; absent when locked.
    pub panel_toggle: Option<Value>,
    pub preview_image: String,
    #[serde(default)]
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
    #[serde(default)]
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
    #[serde(default)]
    pub inline_images: Vec<InlineImage>,
    /// Queued, steering and interrupt messages waiting for the running turn.
    pub queue_lines: Vec<String>,
    /// The same messages with their durable ids, for the boxed ↑ ↓ ✕ list.
    pub queue_items: Vec<QueueItem>,
    /// Local: the Queued section is collapsed (caret click); carried across snapshots.
    #[serde(skip)]
    pub queue_collapsed: bool,
    /// The running turn's status line; absent while idle.
    pub turn_status: Option<TurnStatus>,
    pub update_notice: String,
    /// Dismissible notices from the host (plan §8). Always the newest bounded list;
    /// the client shows each id once and owns timers, dedup and dismissal.
    pub toasts: Vec<ToastWire>,
    /// Settings area list: (label, key, is heading), and the selected index (-1 = none).
    pub nav: Option<Nav>,
    pub sessions: Vec<Session>,
    pub tabs: Vec<Session>,
    pub archived_label: String,
    pub sessions_truncated: bool,
    /// The typed one-page Settings area (`nexus/ui_support/settings_page.py`), or null.
    pub settings_page: Option<Value>,
    /// Appearance and Layout rows inlined into the Settings root list (plus drill summaries), or null.
    pub settings_overview: Option<Value>,
    /// The Sessions dashboard payload (rows, groups, preview), or null while it is closed.
    /// Opaque here: the dashboard module decodes it.
    pub dashboard: Option<Value>,
    pub breadcrumb: String,
    pub details_panel: DetailsPanel,
    pub logs: Vec<String>,
    pub theme: String,
    /// The daemon is unreachable; the shell shows a banner and disables the composer.
    #[serde(default)]
    pub disconnected: bool,
    pub details_sidebar: bool,
    /// Local width arbitration, preserved across Python snapshots.
    pub last_opened: String,
    pub context_preview: bool,
    /// Settings → Layout: the conversation column is at most `render::CENTERED_WIDTH` wide, centered.
    pub centered_layout: bool,
    pub voice_phase: String,
    pub voice_preview: String,
    pub voice_level: f64,
    /// GPT Live call bar (empty object when no call is open).
    pub live: LiveBar,
    pub completions: Vec<String>,
    pub completion_query: String,
    pub completion_prefix: String,
}
/// `Waiting for response… 2.0s` / `4m40s ↓121k [stop]`; times are epoch seconds.
#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct TurnStatus {
    pub activity: String,
    pub activity_since: f64,
    pub turn_since: f64,
    pub output_tokens: u64,
    /// True while the count is a chars/4 estimate of streamed output (shown as `~`).
    pub output_estimated: bool,
}
#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct QueueItem {
    pub id: String,
    /// `S` steer, `Q` queue, `I` interrupt.
    pub tag: String,
    pub mode: String,
    pub text: String,
}
#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct ToastWire {
    pub id: u64,
    pub level: String,
    pub title: String,
    pub body: String,
    pub key: String,
    pub action: Option<ToastAction>,
}
#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct ToastAction {
    pub label: String,
    pub operation: Option<Value>,
}

#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct InlineImage {
    pub id: String,
    pub message: String,
    pub draft_index: Option<usize>,
    pub label: String,
    pub media: String,
    #[serde(default)]
    pub data: String,
    /// Schema-3 content-addressed reference, resolved by the desktop decoder.
    #[serde(default)]
    pub data_ref: String,
    pub operation: Option<Value>,
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
    /// A row button left of the toggle (`[ Restart ]` on an MCP card): click it, or Ctrl+R on the row.
    pub action_label: String,
    pub action_operation: Option<Value>,
    /// Heading shown above the first item of each group (model picker); display only.
    #[serde(default)]
    pub group: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub value: String,
    #[serde(default)]
    pub scope: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub tone: String,
    #[serde(default)]
    pub changed: bool,
    #[serde(default)]
    pub move_up: Option<serde_json::Value>,
    #[serde(default)]
    pub move_down: Option<serde_json::Value>,
    #[serde(default)]
    pub remove: Option<serde_json::Value>,
    /// Dim text after the label (model picker: the provider/model ref).
    pub detail: String,
    /// Right-aligned column before the toggle (tool list: token cost); Rust owns the alignment.
    pub trailing: String,
    /// Dim wrapped lines under the label (skill and MCP cards); the whole card is one selectable item.
    pub lines: Vec<String>,
    pub search: String,
    pub info_operation: Option<Value>,
    /// The active choice: drawn with a leading `●` in the accent colour.
    pub current: bool,
}
impl Item {
    /// Whether the typed picker filter matches this row.
    ///
    /// The label is the visible text (a model name), while `detail` carries the
    /// `provider/model` reference. Matching both lets a user type a provider
    /// name such as `open` for OpenCode Go, as the browser and desktop clients
    /// already do.
    pub fn matches(&self, filter: &str) -> bool {
        let needle = filter.to_lowercase();
        self.label.to_lowercase().contains(&needle)
            || self.detail.to_lowercase().contains(&needle)
            || self.search.to_lowercase().contains(&needle)
    }
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
    pub can_delete: bool,
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
            can_delete: false,
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
    /// Compact "N messages · age" line; the desktop sessions list shows it.
    #[serde(default)]
    pub sub: String,
    #[serde(default)]
    pub active: bool,
    /// The project's display name and the local day label of the session's group.
    #[serde(default)]
    pub project: String,
    #[serde(default)]
    pub day: String,
    /// The project key (a linked worktree's repository, else the workspace) and the
    /// worktree's branch ("" outside a linked worktree).
    #[serde(default)]
    pub repo: String,
    #[serde(default)]
    pub worktree: String,
}

/// The GPT Live call bar: state of the call this client holds open.
#[derive(Clone, Default, Deserialize, PartialEq)]
#[serde(default)]
pub struct LiveBar {
    pub active: bool,
    /// `starting`, `active` or `stopping` (the client's own state).
    pub state: String,
    /// `connecting`, `listening`, `speaking`, `working`, `muted` or `ended`.
    pub phase: String,
    pub model: String,
    pub muted: bool,
    /// Wall-clock start of the call (seconds since the epoch); 0 while connecting.
    pub started: f64,
    #[serde(rename = "in")]
    pub level_in: f64,
    #[serde(rename = "out")]
    pub level_out: f64,
    /// The call's session id when it is not the session on screen.
    pub elsewhere: String,
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
    /// Files a standalone file-change row touched (shown when more than one).
    pub files: u64,
    /// Side-by-side rows: old line, old text, new line, new text, kind.
    pub diff_rows: Vec<(u32, String, u32, String, String)>,
    /// A `change` block's unified rows: kind (`ctx`/`add`/`del`/`sep`/`clip`/`file`), line, text.
    pub diff_lines: Vec<(String, u32, String)>,
    pub gap: u16,
    pub number: u32,
    pub collapsed: bool,
    pub chips: Vec<String>,
    pub chip_operation: Option<Value>,
    /// One operation per chip (an image chip opens that image), in chip order.
    pub chip_operations: Vec<Value>,
    pub detail: String,
    /// Full tool heading (`→ Read path`) shown on an expanded group member.
    pub heading: String,
    /// Complete presentation payload for Rust-local transcript disclosure.
    pub local_ui: bool,
    pub local_open: bool,
    /// Trailing live activity preview; explicit expansion reveals all members.
    #[serde(default)]
    pub preview_limit: usize,
    pub local_detail: String,
    pub local_preview: String,
    pub fold_lines: usize,
    pub turn_id: String,
    pub fold_summary: String,
    pub status: String,
    pub color: String,
    pub rev: String,
    pub count: usize,
    pub failures: usize,
    pub metrics: String,
    /// Header chip counts: `[total]`, or `[project, global]` for skills and MCP.
    pub counts: Vec<usize>,
    pub members: Vec<Content>,
    pub output_operation: Option<Value>,
    /// `tool_group`: the present-tense label (`Running 1 command, Read 3 files`) while a member runs.
    pub running_label: String,
    /// `tool_group`: the finished wall time as text (`1.8s`); empty while running.
    pub elapsed_label: String,
    /// Epoch milliseconds when a group, tool or thought began (0 = unknown), for the live
    /// elapsed time; and the milliseconds it took once finished (0 = unknown or running).
    pub started_ms: u64,
    pub elapsed_ms: u64,
    /// `tool_group`: failed members, drawn under the folded header so a failure is never hidden.
    pub failed_members: Vec<Content>,
    /// `tool_group`: the newest running member, drawn as the live tail of a folded group.
    pub live_member: Option<Box<Content>>,
    /// The full labelled details of a tool (`i`), or the inspector of a context header.
    pub inspect_operation: Option<Value>,
    /// A tool's relevant-only body: `[kind, text]` rows (`nexus/ui_support/tool_signal.py`).
    pub signal: Vec<(String, String)>,
    /// Context blocks: the compact figure (`~348`, `~?`) and whether the block contributes anything.
    pub tokens: String,
    pub active: bool,
}

impl Content {
    /// Whether a context block contributes anything. Hosts that send `tokens` also send
    /// `active`; older ones are judged by their content.
    pub fn is_active(&self) -> bool {
        if !self.tokens.is_empty() {
            return self.active;
        }
        !self.text.is_empty() || self.count > 0 || self.local_preview != "~0"
    }

    /// The token figure beside a context title: `tokens`, else the first `~figure` the
    /// older fields carry. `None` when the host sent no figure at all.
    pub fn token_label(&self) -> Option<String> {
        if !self.tokens.is_empty() {
            return Some(self.tokens.clone());
        }
        [&self.status, &self.local_preview, &self.text]
            .into_iter()
            .find(|text| text.starts_with('~'))
            .map(|text| text.split_whitespace().next().unwrap_or("~?").to_owned())
            .or_else(|| (!self.status.is_empty()).then(|| "~?".to_owned()))
    }

    /// Copy the fields a lazily built view (`Disclosure::block`) must keep so the activity
    /// rows draw: the group's live/failed members and times, a tool's inspector and, when
    /// the row is `open` and not `/verbose`, its relevant-only `signal` rows.
    pub fn carry_presentation(&self, shown: &mut Content, open: bool) {
        shown.running_label = self.running_label.clone();
        shown.elapsed_label = self.elapsed_label.clone();
        shown.started_ms = self.started_ms;
        shown.elapsed_ms = self.elapsed_ms;
        shown.failed_members = self.failed_members.clone();
        shown.live_member = self.live_member.clone();
        shown.inspect_operation = self.inspect_operation.clone();
        shown.tokens = self.tokens.clone();
        shown.active = self.active;
        if open {
            shown.signal = self.signal.clone();
        }
    }
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
    /// Move unchanged sections; do not serialize or clone the mirrored transcript.
    pub fn merge_missing(
        &mut self,
        previous: &mut Snapshot,
        present: &serde_json::Map<String, Value>,
    ) {
        if self.schema != 3 || self.reset {
            return;
        }
        if !present.contains_key("completion_bell") {
            self.completion_bell = std::mem::take(&mut previous.completion_bell);
        }
        if !present.contains_key("composer_key") {
            self.composer_key = previous.composer_key.clone();
        }
        if !present.contains_key("title") {
            self.title = std::mem::take(&mut previous.title);
        }
        if !present.contains_key("status") {
            self.status = std::mem::take(&mut previous.status);
        }
        if !present.contains_key("lines") {
            self.lines = std::mem::take(&mut previous.lines);
        }
        if !present.contains_key("transcript_verbose") {
            self.transcript_verbose = std::mem::take(&mut previous.transcript_verbose);
        }
        if !present.contains_key("context_lines") {
            self.context_lines = std::mem::take(&mut previous.context_lines);
        }
        if !present.contains_key("panel_title") {
            self.panel_title = previous.panel_title.clone();
        }
        if !present.contains_key("panel_hint") {
            self.panel_hint = std::mem::take(&mut previous.panel_hint);
        }
        if !present.contains_key("panel_layout") {
            self.panel_layout = std::mem::take(&mut previous.panel_layout);
        }
        if !present.contains_key("panel_format") {
            self.panel_format = std::mem::take(&mut previous.panel_format);
        }
        if !present.contains_key("preview_image") {
            self.preview_image = std::mem::take(&mut previous.preview_image);
        }
        if !present.contains_key("preview_image_media") {
            self.preview_image_media = std::mem::take(&mut previous.preview_image_media);
        }
        if !present.contains_key("panel_loading") {
            self.panel_loading = std::mem::take(&mut previous.panel_loading);
        }
        if !present.contains_key("panel_lines") {
            self.panel_lines = std::mem::take(&mut previous.panel_lines);
        }
        if !present.contains_key("panel_tones") {
            self.panel_tones = std::mem::take(&mut previous.panel_tones);
        }
        if !present.contains_key("items") {
            self.items = std::mem::take(&mut previous.items);
        }
        if !present.contains_key("prompt") {
            self.prompt = std::mem::take(&mut previous.prompt);
        }
        if !present.contains_key("form") {
            self.form = std::mem::take(&mut previous.form);
        }
        if !present.contains_key("controls") {
            self.controls = std::mem::take(&mut previous.controls);
        }
        if !present.contains_key("history") {
            self.history = std::mem::take(&mut previous.history);
        }
        if !present.contains_key("agent") {
            self.agent = std::mem::take(&mut previous.agent);
        }
        if !present.contains_key("agent_color") {
            self.agent_color = std::mem::take(&mut previous.agent_color);
        }
        if !present.contains_key("agent_page") {
            self.agent_page = previous.agent_page.clone();
        }
        if !present.contains_key("model") {
            self.model = std::mem::take(&mut previous.model);
        }
        if !present.contains_key("provider") {
            self.provider = std::mem::take(&mut previous.provider);
        }
        if !present.contains_key("effort") {
            self.effort = std::mem::take(&mut previous.effort);
        }
        if !present.contains_key("context_usage") {
            self.context_usage = std::mem::take(&mut previous.context_usage);
        }
        if !present.contains_key("context_note") {
            self.context_note = std::mem::take(&mut previous.context_note);
        }
        if !present.contains_key("context_label") {
            self.context_label = std::mem::take(&mut previous.context_label);
        }
        if !present.contains_key("context_used") {
            self.context_used = std::mem::take(&mut previous.context_used);
        }
        if !present.contains_key("context_window") {
            self.context_window = std::mem::take(&mut previous.context_window);
        }
        if !present.contains_key("context_marks") {
            self.context_marks = std::mem::take(&mut previous.context_marks);
        }
        if !present.contains_key("context_tiers") {
            self.context_tiers = std::mem::take(&mut previous.context_tiers);
        }
        if !present.contains_key("attachments") {
            self.attachments = std::mem::take(&mut previous.attachments);
        }
        if !present.contains_key("attachment_lines") {
            self.attachment_lines = std::mem::take(&mut previous.attachment_lines);
        }
        if !present.contains_key("inline_images") {
            self.inline_images = std::mem::take(&mut previous.inline_images);
        }
        if !present.contains_key("queue_lines") {
            self.queue_lines = std::mem::take(&mut previous.queue_lines);
        }
        if !present.contains_key("queue_items") {
            self.queue_items = std::mem::take(&mut previous.queue_items);
        }
        if !present.contains_key("turn_status") {
            self.turn_status = previous.turn_status.take();
        }
        if !present.contains_key("update_notice") {
            self.update_notice = std::mem::take(&mut previous.update_notice);
        }
        if !present.contains_key("nav") {
            self.nav = std::mem::take(&mut previous.nav);
        }
        if !present.contains_key("sessions") {
            self.sessions = std::mem::take(&mut previous.sessions);
        }
        if !present.contains_key("tabs") {
            self.tabs = std::mem::take(&mut previous.tabs);
        }
        if !present.contains_key("archived_label") {
            self.archived_label = std::mem::take(&mut previous.archived_label);
        }
        if !present.contains_key("sessions_truncated") {
            self.sessions_truncated = std::mem::take(&mut previous.sessions_truncated);
        }
        if !present.contains_key("breadcrumb") {
            self.breadcrumb = std::mem::take(&mut previous.breadcrumb);
        }
        if !present.contains_key("details_panel") {
            let tab = previous.details_panel.tab.clone();
            self.details_panel = std::mem::take(&mut previous.details_panel);
            previous.details_panel.tab = tab;
        }
        if !present.contains_key("logs") {
            self.logs = std::mem::take(&mut previous.logs);
        }
        if !present.contains_key("theme") {
            self.theme = previous.theme.clone();
        }
        if !present.contains_key("details_sidebar") {
            self.details_sidebar = std::mem::take(&mut previous.details_sidebar);
        }
        if !present.contains_key("settings_overview") {
            self.settings_overview = std::mem::take(&mut previous.settings_overview);
        }
        if !present.contains_key("settings_page") {
            self.settings_page = std::mem::take(&mut previous.settings_page);
        }
        if !present.contains_key("dashboard") {
            self.dashboard = std::mem::take(&mut previous.dashboard);
        }
        if !present.contains_key("last_opened") {
            self.last_opened = previous.last_opened.clone();
        }
        if !present.contains_key("context_preview") {
            self.context_preview = std::mem::take(&mut previous.context_preview);
        }
        if !present.contains_key("centered_layout") {
            self.centered_layout = previous.centered_layout;
        }
        if !present.contains_key("voice_phase") {
            self.voice_phase = previous.voice_phase.clone();
        }
        if !present.contains_key("voice_preview") {
            self.voice_preview = std::mem::take(&mut previous.voice_preview);
        }
        if !present.contains_key("live") {
            self.live = std::mem::take(&mut previous.live);
        }
        if !present.contains_key("voice_level") {
            self.voice_level = std::mem::take(&mut previous.voice_level);
        }
        if !present.contains_key("completions") {
            self.completions = std::mem::take(&mut previous.completions);
        }
        if !present.contains_key("completion_query") {
            self.completion_query = std::mem::take(&mut previous.completion_query);
        }
        if !present.contains_key("completion_prefix") {
            self.completion_prefix = std::mem::take(&mut previous.completion_prefix);
        }
        if !present.contains_key("local_ui_enabled") {
            self.local_ui_enabled = previous.local_ui_enabled;
        }
        if !present.contains_key("ui_ack") {
            self.ui_ack = previous.ui_ack;
        }
        if !present.contains_key("logs_show_all") {
            self.logs_show_all = previous.logs_show_all;
        }
        if !present.contains_key("logs_all") {
            self.logs_all = std::mem::take(&mut previous.logs_all);
        }
        if !present.contains_key("logs_folded") {
            self.logs_folded = std::mem::take(&mut previous.logs_folded);
        }
        if !present.contains_key("commands") {
            self.commands = std::mem::take(&mut previous.commands);
        }
        if !present.contains_key("command_help") {
            self.command_help = std::mem::take(&mut previous.command_help);
        }
        self.history.append(&mut self.history_append);
    }

    /// Schema 2 suffixes are dependent patches; never discard an intermediate patch.
    pub fn restore_blocks(&mut self, previous: &mut Snapshot) -> Result<(), &'static str> {
        if self.schema != 2 && self.schema != 3 {
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

#[cfg(test)]
mod section_tests {
    use super::*;
    #[test]
    fn omissions_move_sections_without_repeating_one_shots() {
        let mut old = Snapshot {
            schema: 3,
            generation: 1,
            theme: "dark".into(),
            history: vec!["old".into()],
            restore: "once".into(),
            blocks: vec![Content {
                id: "a".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        let fields = serde_json::from_str::<serde_json::Map<String, Value>>(
            r#"{"schema":3,"revision":2,"generation":1,"history_append":["new"]}"#,
        )
        .unwrap();
        let mut next: Snapshot = serde_json::from_value(Value::Object(fields.clone())).unwrap();
        next.merge_missing(&mut old, &fields);
        next.blocks_from = 1;
        next.restore_blocks(&mut old).unwrap();
        assert_eq!(next.theme, "dark");
        assert_eq!(next.history, vec!["old", "new"]);
        assert!(next.restore.is_empty());
        assert_eq!(next.blocks[0].id, "a");
    }
    #[test]
    fn dashboard_is_opaque_kept_across_omissions_and_cleared_by_null() {
        let mut old = Snapshot {
            schema: 3,
            dashboard: Some(serde_json::json!({"rows": []})),
            ..Default::default()
        };
        let fields = serde_json::from_str::<serde_json::Map<String, Value>>(r#"{"schema":3}"#)
            .unwrap();
        let mut next: Snapshot = serde_json::from_value(Value::Object(fields.clone())).unwrap();
        next.merge_missing(&mut old, &fields);
        assert_eq!(next.dashboard, Some(serde_json::json!({"rows": []})));
        let fields = serde_json::from_str::<serde_json::Map<String, Value>>(
            r#"{"schema":3,"dashboard":null}"#,
        )
        .unwrap();
        let mut cleared: Snapshot = serde_json::from_value(Value::Object(fields.clone())).unwrap();
        cleared.merge_missing(&mut next, &fields);
        assert_eq!(cleared.dashboard, None);
    }

    #[test]
    fn activity_fields_decode_and_context_figures_fall_back() {
        let group: Content = serde_json::from_str(
            r#"{"kind":"tool_group","running_label":"Running 1 command","elapsed_label":"1.8s",
                "started_ms":1700000000000,"elapsed_ms":1800,"failures":1,
                "failed_members":[{"id":"bad","status":"failed"}],
                "live_member":{"id":"now","status":"running","inspect_operation":{"kind":"tool_details","id":"now"},
                               "signal":[["out","1  a"],["clip","… 3 lines"]]}}"#,
        )
        .unwrap();
        assert_eq!(group.started_ms, 1_700_000_000_000);
        assert_eq!(group.failed_members[0].id, "bad");
        let live = group.live_member.as_deref().unwrap();
        assert_eq!(live.signal[1], ("clip".to_string(), "… 3 lines".to_string()));
        assert_eq!(live.inspect_operation.as_ref().unwrap()["id"], "now");

        let explicit = Content { tokens: "~348".into(), active: false, text: "body".into(), ..Default::default() };
        assert!(!explicit.is_active(), "explicit `active` wins over content");
        assert_eq!(explicit.token_label().as_deref(), Some("~348"));
        let legacy = Content { status: "~2.1k tokens".into(), ..Default::default() };
        assert!(legacy.is_active());
        assert_eq!(legacy.token_label().as_deref(), Some("~2.1k"));
        let unknown = Content { status: "tokens unknown".into(), ..Default::default() };
        assert_eq!(unknown.token_label().as_deref(), Some("~?"));
        assert_eq!(Content::default().token_label(), None);
        let zero = Content { local_preview: "~0".into(), ..Default::default() };
        assert!(!zero.is_active());
    }

    #[test]
    fn carried_presentation_keeps_signal_only_while_open() {
        let source = Content {
            running_label: "Running 1 command".into(),
            failed_members: vec![Content { id: "bad".into(), ..Default::default() }],
            signal: vec![("text".into(), "x".into())],
            ..Default::default()
        };
        let mut closed = Content::default();
        source.carry_presentation(&mut closed, false);
        assert_eq!(closed.running_label, "Running 1 command");
        assert_eq!(closed.failed_members.len(), 1);
        assert!(closed.signal.is_empty());
        let mut open = Content::default();
        source.carry_presentation(&mut open, true);
        assert_eq!(open.signal.len(), 1);
    }

    #[test]
    fn picker_filter_matches_hidden_model_reference() {
        let item = Item {
            label: "Kimi K3".into(),
            search: "opencode-go/kimi-k3".into(),
            ..Default::default()
        };
        assert!(item.matches("open"));
        assert!(item.matches("OpenCode-Go"));
        assert!(item.matches("kimi"));
        assert!(!item.matches("anthropic"));
    }
}

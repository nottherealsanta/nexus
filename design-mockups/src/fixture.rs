//! One fake world every screen renders, so differences come from design, not data.
use nexus_widgets::lists::Dot;
use nexus_widgets::{Scroll, TextState};
use std::collections::HashSet;

#[derive(Clone)]
pub struct Session {
    pub id: String,
    pub title: String,
    pub workspace: String,
    pub dot: Dot,
    pub sub: String,
    pub age: String,
    pub archived: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Conn {
    Connected,
    Error,
    Off,
    Down,
}
#[derive(Clone)]
pub struct Provider {
    pub id: &'static str,
    pub name: &'static str,
    pub conn: Conn,
    pub auth: &'static str,
    pub models: u32,
    pub account: &'static str,
    pub usage: &'static str,
    pub detail: &'static str,
}

#[derive(Clone)]
pub struct ModelRef {
    pub label: String,
    pub connected: bool,
}
pub fn mref(label: &str, connected: bool) -> ModelRef {
    ModelRef { label: label.into(), connected }
}

#[derive(Clone)]
pub struct Agent {
    pub name: &'static str,
    pub tag: &'static str,
    pub tier_mode: bool,
    pub tier: usize,
    pub effort: usize,
    pub tools_on: u32,
    pub prompt: &'static str,
    pub tokens: u32,
}
#[derive(Clone)]
pub struct Tool {
    pub name: &'static str,
    pub on: bool,
    pub locked: bool,
    pub tokens: u32,
}
#[derive(Clone)]
pub struct Family {
    pub name: &'static str,
    pub tools: Vec<Tool>,
}
#[derive(Clone)]
pub struct Mcp {
    pub name: &'static str,
    pub dot: Dot,
    pub status: &'static str,
    pub tools: u32,
    pub tokens: u32,
    pub enabled: bool,
    pub eager: bool,
    pub cmd: &'static str,
    pub scope: &'static str,
}
#[derive(Clone)]
pub struct Skill {
    pub name: &'static str,
    pub desc: &'static str,
    pub tokens: u32,
    pub on: bool,
}

pub const AREAS: &[(&str, &str)] = &[
    ("appearance", "Appearance"),
    ("layout", "Layout"),
    ("keyboard", "Keyboard"),
    ("providers", "Providers"),
    ("models", "Models"),
    ("agents", "Agents"),
    ("tools", "Tools"),
    ("mcp", "MCP servers"),
    ("skills", "Skills"),
    ("voice", "Voice & speech"),
];
pub const TIERS: [&str; 3] = ["Low", "Medium", "High"];
pub const EFFORTS: [&str; 3] = ["low", "medium", "high"];
pub const DEVICES: [&str; 3] = ["auto", "cpu", "metal"];

pub struct World {
    pub sessions: Vec<Session>,
    pub providers: Vec<Provider>,
    pub default_chain: Vec<ModelRef>,
    pub tiers: [Vec<ModelRef>; 3],
    pub agents: Vec<Agent>,
    pub families: Vec<Family>,
    pub mcp: Vec<Mcp>,
    pub skills: Vec<Skill>,
    pub models: Vec<String>,
    // settings values
    pub theme: usize,
    pub ascii: bool,
    pub reduce_motion: bool,
    pub dense: bool,
    pub sessions_on_start: bool,
    pub details_on_start: bool,
    pub key_hints: bool,
    pub toast_top: usize,
    pub title_on: bool,
    pub effort: usize,
    pub voice_on: bool,
    pub auto_send: bool,
    pub device: usize,
    pub limit: u32,
    pub speech_voice: usize,
    pub speech_speed: u32,
    pub speech_downloaded: bool,
    pub scope: usize,
}

fn s(id: &str, title: &str, ws: &str, dot: Dot, sub: &str, age: &str, archived: bool) -> Session {
    Session { id: id.into(), title: title.into(), workspace: ws.into(), dot, sub: sub.into(), age: age.into(), archived }
}
fn tool(name: &'static str, on: bool, tokens: u32) -> Tool {
    Tool { name, on, locked: false, tokens }
}

impl World {
    pub fn new() -> Self {
        let mut sessions = vec![
            s("s1", "Fix token refresh race", "~/repos/nexus", Dot::Work, "build · running · 4 turns", "2m", false),
            s("s2", "Refresh docs for models", "~/repos/nexus", Dot::Ok, "finished · unread", "1h", false),
            s("s3", "New Session", "~/repos/nexus", Dot::Idle, "build · idle", "3h", false),
            s("s4", "Réparer le rafraîchissement du jeton — 東京", "~/repos/nexus", Dot::Idle, "build · idle · 9 turns", "5h", false),
            s("s5", "Add MCP server diagnostics", "~/repos/nexus", Dot::Err, "orchestrator · failed: rate limit", "1d", false),
            s("s6", "Refresh CSS tokens", "~/repos/site", Dot::Idle, "build · idle", "3d", false),
            s("s7", "Audit dependency licences", "~/repos/site", Dot::Ok, "finished", "4d", false),
            s("s8", "Port settings to one page", "~/repos/notes", Dot::Idle, "advisor · idle", "6d", false),
        ];
        for i in 0..33 {
            sessions.push(s(&format!("a{i}"), &format!("Archived experiment {}", i + 1), if i % 2 == 0 { "~/repos/nexus" } else { "~/repos/site" }, Dot::Idle, "archived", &format!("{}w", i / 4 + 2), true));
        }
        let p = |id, name, conn, auth, models, account, usage, detail| Provider { id, name, conn, auth, models, account, usage, detail };
        let providers = vec![
            p("anthropic", "Anthropic", Conn::Connected, "OAuth", 18, "you@example.com · Max plan", "52% of 5-hour limit · resets 14:20", ""),
            p("openai", "OpenAI", Conn::Connected, "API key ••••3f2a", 34, "key set in credentials.json", "", ""),
            p("google", "Google", Conn::Off, "", 0, "", "", ""),
            p("opencode", "OpenCode Go", Conn::Error, "token expired", 0, "", "", "Token expired on 2026-10-04. Sign in again."),
            p("ollama", "Ollama (local)", Conn::Down, "", 0, "", "", "Not running · localhost:11434"),
        ];
        let models: Vec<String> = [
            "anthropic/claude-fable-5-1", "anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-4-5",
            "openai/gpt-6", "openai/gpt-6-mini", "openai/o5", "google/gemini-3-pro", "google/gemini-3-flash",
            "opencode/qwen3-coder", "opencode/kimi-k3", "ollama/llama4:70b", "ollama/qwen3:32b",
        ].iter().map(|m| m.to_string()).collect();
        let tiers = [
            vec![mref("anthropic/claude-haiku-4-5", true), mref("google/gemini-3-flash", false)],
            vec![mref("anthropic/claude-sonnet-5-5", true), mref("openai/gpt-6-mini", true), mref("google/gemini-3-pro", false)],
            vec![mref("anthropic/claude-opus-5-5", true), mref("openai/gpt-6", true)],
        ];
        let ag = |name, tag, tier_mode, tier, effort, tools_on, prompt, tokens| Agent { name, tag, tier_mode, tier, effort, tools_on, prompt, tokens };
        let agents = vec![
            ag("build", "built-in · edited", true, 1, 1, 22, "~/.nexus/agents/build.md", 1240),
            ag("orchestrator", "built-in", true, 2, 2, 12, "built-in prompt", 980),
            ag("advisor", "built-in", true, 2, 2, 4, "built-in prompt", 410),
            ag("task", "built-in", true, 1, 1, 18, "built-in prompt", 520),
            ag("quick", "built-in", true, 0, 0, 6, "built-in prompt", 180),
            ag("reviewer", "custom", false, 1, 1, 9, "~/.nexus/agents/reviewer.md", 760),
        ];
        let families = vec![
            Family { name: "Files", tools: vec![tool("read", true, 210), tool("write", true, 240), tool("edit", true, 380), tool("glob", true, 150)] },
            Family { name: "Search", tools: vec![tool("grep", true, 260), tool("web_search", false, 340), tool("web_fetch", true, 220)] },
            Family { name: "Shell", tools: vec![Tool { name: "bash", on: true, locked: true, tokens: 520 }, tool("bash_output", true, 180), tool("kill_shell", true, 90)] },
            Family { name: "Other", tools: vec![tool("notebook_edit", true, 300), tool("todo_write", true, 140), tool("list_dir", true, 120), tool("apply_patch", true, 330), tool("search_replace", false, 210), tool("git_diff", true, 190), tool("git_log", true, 150), tool("http_get", false, 200), tool("sleep", true, 60), tool("screenshot", true, 280), tool("clipboard", false, 110), tool("speak", true, 130), tool("memory_read", true, 120), tool("memory_write", true, 140)] },
            Family { name: "Agents", tools: vec![tool("task", true, 410), tool("question", true, 160)] },
        ];
        let mcp = vec![
            Mcp { name: "github", dot: Dot::Ok, status: "running", tools: 31, tokens: 4100, enabled: true, eager: false, cmd: "npx -y @modelcontextprotocol/server-github", scope: "global" },
            Mcp { name: "postgres", dot: Dot::Err, status: "failed to start · exit 1", tools: 0, tokens: 0, enabled: true, eager: true, cmd: "uvx mcp-server-postgres --dsn $DATABASE_URL", scope: "project" },
            Mcp { name: "filesystem", dot: Dot::Idle, status: "disabled", tools: 11, tokens: 1400, enabled: false, eager: false, cmd: "npx -y @modelcontextprotocol/server-filesystem .", scope: "global" },
        ];
        let sk = |name, desc, tokens, on| Skill { name, desc, tokens, on };
        let skills = vec![
            sk("native-app-review", "Screenshot and review the native app", 640, true),
            sk("gpui-nexus", "Work on the GPUI desktop client", 910, true),
            sk("release", "Cut a release through release-please", 380, true),
            sk("code-review", "Review a diff for correctness", 520, false),
            sk("simplify", "Reuse and simplification pass", 300, true),
            sk("dataviz", "Charts and dashboards", 1100, false),
            sk("security-review", "Review pending changes for security", 450, true),
        ];
        Self {
            sessions,
            providers,
            default_chain: vec![mref("anthropic/claude-sonnet-5-5", true), mref("openai/gpt-6", true)],
            tiers,
            agents,
            families,
            mcp,
            skills,
            models,
            theme: 0,
            ascii: false,
            reduce_motion: false,
            dense: false,
            sessions_on_start: true,
            details_on_start: false,
            key_hints: true,
            toast_top: 0,
            title_on: true,
            effort: 1,
            voice_on: true,
            auto_send: false,
            device: 0,
            limit: 60,
            speech_voice: 0,
            speech_speed: 10,
            speech_downloaded: false,
            scope: 0,
        }
    }
}

#[derive(Default)]
pub struct Popup {
    pub id: String,
    pub options: Vec<String>,
    pub current: usize,
    pub highlighted: usize,
}

/// Per-screen view state owned by the viewer.
pub struct View {
    pub area: usize,
    pub tier_tab: usize,
    pub agent_sel: usize,
    pub sess_filter: usize,
    pub sess_sel: usize,
    pub sess_search: TextState,
    pub sess_search_focus: bool,
    pub rename: Option<TextState>,
    pub selected: HashSet<String>,
    pub open: HashSet<String>,
    pub popup: Option<Popup>,
    pub settings_search: TextState,
    pub search_focus: bool,
    pub kb_filter: TextState,
    pub key_entry: Option<TextState>,
    pub signing_in: Option<&'static str>,
    pub refreshing: bool,
    pub confirm: bool,
    pub notifications: bool,
    pub help: bool,
    pub palette_q: TextState,
    pub picker_q: TextState,
    pub picker_target: Option<usize>,
    pub chat_state: usize,
    pub sidebar: bool,
    pub ctx_mode: usize,
    pub page_scroll: Scroll,
    pub sess_scroll: nexus_widgets::ListState,
}
impl View {
    pub fn new() -> Self {
        let mut open = HashSet::new();
        for k in ["prov:anthropic", "prov:openai", "mcp:github"] {
            open.insert(k.to_string());
        }
        Self {
            area: 4,
            tier_tab: 1,
            agent_sel: 0,
            sess_filter: 0,
            sess_sel: 0,
            sess_search: TextState::default(),
            sess_search_focus: false,
            rename: None,
            selected: HashSet::new(),
            open,
            popup: None,
            settings_search: TextState::default(),
            search_focus: false,
            kb_filter: TextState::default(),
            key_entry: None,
            signing_in: None,
            refreshing: false,
            confirm: false,
            notifications: false,
            help: false,
            palette_q: TextState::default(),
            picker_q: TextState::default(),
            picker_target: None,
            chat_state: 0,
            sidebar: false,
            ctx_mode: 0,
            page_scroll: Scroll::default(),
            sess_scroll: Default::default(),
        }
    }
}

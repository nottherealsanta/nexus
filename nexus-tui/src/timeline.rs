//! The session timeline: every model-visible item in order, plus the execution
//! diagnostics Core keeps beside it.
//!
//! The builder is pure and shared by drawing and by keyboard/mouse interaction:
//! it produces both the styled lines and a list of `Block`s (the foldable,
//! selectable units). Folding changes how much is drawn, never what exists.
//! Nothing here talks to Core or to the terminal beyond `Theme`.

use std::collections::HashMap;

use ratatui::style::{Modifier, Style};
use ratatui::text::Span;
use serde_json::Value;

use crate::app::App;
use crate::text::{self, truncate, truncate_middle, width};
use crate::theme::Theme;

const CANVAS: &str = "surface.canvas";
const PANEL: &str = "surface.panel";
const PRIMARY: &str = "text.primary";
const SECONDARY: &str = "text.secondary";
const MUTED: &str = "text.muted";
const ACCENT: &str = "interactive.accent";

pub struct TLine {
    pub spans: Vec<Span<'static>>,
    pub bg: Option<Style>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlockKind {
    Context,
    Turn,
    User,
    Assistant,
    Reasoning,
    Tool,
    Notice,
    Diagnostic,
    Child,
    Older,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Block {
    pub key: String,
    pub kind: BlockKind,
    /// First and one-past-last visible line.
    pub start: usize,
    pub end: usize,
    pub turn: Option<usize>,
    /// Keyboard/pointer selectable.
    pub focusable: bool,
    /// Enter/Space or a click toggles it.
    pub toggle: bool,
    /// What opening the block navigates to (a child session), when anything.
    pub target: Option<String>,
}

pub struct Timeline {
    pub lines: Vec<TLine>,
    pub blocks: Vec<Block>,
    pub turns: usize,
}

/// Turn facts gathered from messages and the durable diagnostics that follow
/// them (usage, steps, model, end reason).
#[derive(Debug, Clone, Default)]
struct TurnMeta {
    start_msg: usize,
    start_time: f64,
    end_time: f64,
    input: u64,
    cached: u64,
    output: u64,
    reasoning: u64,
    steps: u64,
    tools: u64,
    models: Vec<String>,
    stop: Option<String>,
    running: bool,
    /// A later user message exists, so this turn cannot still be running.
    followed: bool,
    error: Option<(String, String)>,
    end_event: bool,
}

impl TurnMeta {
    fn duration(&self) -> f64 {
        (self.end_time - self.start_time).max(0.0)
    }
    fn has_usage(&self) -> bool {
        self.input + self.cached + self.output + self.reasoning > 0
    }
}

struct Done {
    output: String,
    is_error: bool,
}

#[derive(Default)]
struct ToolTrace {
    start: Option<f64>,
    end: Option<f64>,
    error: Option<String>,
}

pub fn build(app: &App, t: &Theme, frame: usize, width: usize) -> Timeline {
    let mut b = Builder {
        app,
        t,
        frame,
        bw: width.saturating_sub(2).max(8),
        out: Vec::new(),
        blocks: Vec::new(),
        turn: None,
        name_w: 5,
    };
    b.run();
    Timeline {
        lines: b.out,
        blocks: b.blocks,
        turns: b.turn.map_or(0, |n| n + 1),
    }
}

struct Builder<'a> {
    app: &'a App,
    t: &'a Theme,
    frame: usize,
    /// Content width (one cell of breathing room on each side).
    bw: usize,
    out: Vec<TLine>,
    blocks: Vec<Block>,
    turn: Option<usize>,
    name_w: usize,
}

fn json_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn arg_json(args: &str) -> Option<Value> {
    serde_json::from_str(args).ok()
}

fn is_notice(text: &str) -> bool {
    let t = text.trim_start();
    t.starts_with("[Nexus notice:")
        || t.starts_with("[Nexus recovery:")
        || t.starts_with("[Notification:")
}

fn fmt_count(n: u64) -> String {
    if n >= 1000 {
        let k = n as f64 / 1000.0;
        if k >= 100.0 {
            format!("{k:.0}k")
        } else {
            format!("{k:.1}k")
        }
    } else {
        n.to_string()
    }
}

fn fmt_duration(secs: f64) -> String {
    let s = secs.max(0.0);
    if s < 0.95 {
        "<1 s".into()
    } else if s < 60.0 {
        format!("{:.0} s", s)
    } else if s < 3600.0 {
        format!(
            "{:.0}m {:02}s",
            (s / 60.0).floor(),
            (s % 60.0).round() as u64
        )
    } else {
        format!(
            "{:.0}h {:02}m",
            (s / 3600.0).floor(),
            ((s % 3600.0) / 60.0).floor() as u64
        )
    }
}

impl Builder<'_> {
    fn st(&self, fg: &str, bg: &str) -> Style {
        self.t.style(fg, bg)
    }
    fn ell(&self) -> &str {
        self.t.structure("ellipsis")
    }
    fn sep(&self) -> String {
        format!(" {} ", self.t.structure("separator"))
    }
    fn is_open(&self, key: &str, default: bool) -> bool {
        self.app
            .ui
            .timeline_open
            .get(key)
            .copied()
            .unwrap_or(default)
    }
    fn selected(&self) -> bool {
        self.app.ui.focus == crate::ui::Focus::Timeline
            && self.app.ui.timeline_sel == Some(self.blocks.len())
    }

    fn line(&mut self, spans: Vec<Span<'static>>) {
        self.out.push(TLine { spans, bg: None });
    }
    fn blank(&mut self) {
        self.line(Vec::new());
    }

    /// Open a block at the current line; `f` appends its lines.
    fn block(
        &mut self,
        key: impl Into<String>,
        kind: BlockKind,
        turn: Option<usize>,
        toggle: bool,
        f: impl FnOnce(&mut Self),
    ) {
        let key = key.into();
        let start = self.out.len();
        let selected = self.selected();
        f(self);
        if self.out.len() == start {
            return;
        }
        let end = self.out.len();
        if selected {
            let sel = self.t.bg("interactive.selection");
            for l in &mut self.out[start..end] {
                if l.bg.is_none() {
                    l.bg = Some(sel);
                }
            }
        }
        self.blocks.push(Block {
            key,
            kind,
            start,
            end,
            turn,
            focusable: true,
            toggle,
            target: None,
        });
        self.blank();
    }

    fn meta(&self) -> Style {
        self.st(MUTED, CANVAS)
    }
    fn primary(&self) -> Style {
        self.st(PRIMARY, CANVAS)
    }
    fn secondary(&self) -> Style {
        self.st(SECONDARY, CANVAS)
    }

    fn status_style(&self, token: &str) -> Style {
        self.st(token, CANVAS)
    }

    /// One line with a left part and a right-aligned meta block.
    fn row(&mut self, left: Vec<Span<'static>>, right: Vec<Span<'static>>) {
        let lw: usize = left.iter().map(|s| width(s.content.as_ref())).sum();
        let rw: usize = right.iter().map(|s| width(s.content.as_ref())).sum();
        let mut spans = left;
        if rw > 0 {
            if lw + rw < self.bw {
                spans.push(Span::raw(" ".repeat(self.bw - lw - rw)));
                spans.extend(right);
            } else {
                // The title truncates first; status stays whole.
                let room = self.bw.saturating_sub(rw + 1);
                let mut cut = Vec::new();
                let mut used = 0usize;
                for s in spans {
                    let text = s.content.to_string();
                    let w = width(&text);
                    if used + w <= room {
                        used += w;
                        cut.push(s);
                    } else {
                        let take = room.saturating_sub(used).saturating_sub(1);
                        cut.push(Span::styled(truncate(&text, take, self.ell()), s.style));
                        break;
                    }
                }
                spans = cut;
                spans.push(Span::raw(" "));
                spans.extend(right);
            }
        }
        self.line(spans);
    }

    fn run(&mut self) {
        let Some(view) = self
            .app
            .state
            .model
            .selected
            .as_ref()
            .filter(|v| self.app.viewed_id() == Some(v.session_id.as_str()))
        else {
            return;
        };
        let busy = self.app.is_busy(&view.session_id);

        // Tool results, by call id.
        let mut results: HashMap<String, Done> = HashMap::new();
        for m in &view.messages {
            for p in &m.parts {
                if p["type"] == "tool_result" {
                    results.insert(
                        json_str(&p["call_id"]),
                        Done {
                            output: json_str(&p["output"]),
                            is_error: p["is_error"].as_bool().unwrap_or(false),
                        },
                    );
                }
            }
        }
        // Tool timing from the durable stream.
        let mut traces: HashMap<String, ToolTrace> = HashMap::new();
        for e in &view.events {
            if e.kind == "tool_start" {
                let id = json_str(&e.data["call"]["id"]);
                traces.entry(id).or_default().start = Some(e.time);
            } else if e.kind == "tool_end" {
                let id = json_str(&e.data["call"]["id"]);
                let t = traces.entry(id).or_default();
                t.end = Some(e.time);
                if e.data["result"]["is_error"].as_bool().unwrap_or(false) {
                    t.error = Some(json_str(&e.data["result"]["output"]));
                }
            }
        }
        let waiting: HashMap<String, String> = self
            .app
            .state
            .model
            .approvals
            .values()
            .map(|a| (a.call_id.clone(), a.tool.clone()))
            .collect();

        // Where each message was announced, so diagnostics interleave with it.
        let mut added_at: HashMap<&str, i64> = HashMap::new();
        for e in &view.events {
            if e.kind == "message.added" {
                if let Some(id) = e.data["id"].as_str() {
                    added_at.insert(id, e.id);
                }
            }
        }

        let turns = self.turn_meta(view, busy);
        // Widest tool name in view, for the name column (max 9 cells).
        self.name_w = view
            .messages
            .iter()
            .flat_map(|m| m.parts.iter())
            .filter(|p| p["type"] == "tool_call")
            .map(|p| width(&json_str(&p["name"])))
            .max()
            .unwrap_or(5)
            .clamp(5, 9);

        self.context_section(view, turns.is_empty());
        if view.messages_more {
            self.block("older-messages", BlockKind::Older, None, false, |b| {
                b.line(vec![Span::styled(
                    format!("{} older messages are not loaded", b.ell()),
                    b.meta(),
                )]);
            });
        }
        if let Some(gap) = &view.events_gap {
            self.block("older-events", BlockKind::Older, None, false, |b| {
                b.line(vec![Span::styled(format!("{} {gap}", b.ell()), b.meta())]);
            });
        }

        enum Item<'a> {
            Msg(&'a crate::api::Message),
            Ev(&'a crate::api::Event),
        }
        let mut items: Vec<(i64, usize, Item)> = Vec::new();
        for (i, m) in view.messages.iter().enumerate() {
            items.push((
                added_at.get(m.id.as_str()).copied().unwrap_or(0),
                i,
                Item::Msg(m),
            ));
        }
        for (i, e) in view.events.iter().enumerate() {
            if matches!(
                e.kind.as_str(),
                "message.added"
                    | "text_delta"
                    | "reasoning_delta"
                    | "step_start"
                    | "session.status"
                    | "session.updated"
                    | "session.created"
                    | "session.archived"
                    | "session.unarchived"
                    | "project.updated"
                    | "project.created"
                    | "permission.asked"
                    | "permission.replied"
                    | "question.asked"
                    | "question.replied"
                    | "usage"
                    | "turn_end"
                    | "tool_start"
                    | "tool_end"
                    | "wire_request"
                    | "wire_response"
                    | "wire_event"
                    | "part_done"
                    | "done"
            ) {
                continue;
            }
            items.push((e.id, view.messages.len() + i, Item::Ev(e)));
        }
        items.sort_by_key(|(k, i, _)| (*k, *i));

        let mut turn_open = false;
        let mut turn_no = 0usize;
        let mut folded = false;
        for (_, _, item) in &items {
            match item {
                Item::Msg(m) => {
                    if m.role == "user" {
                        let text = parts_text(m);
                        if is_notice(&text) {
                            if !folded {
                                self.notice(m, &text);
                            }
                            continue;
                        }
                        if turn_open && !folded {
                            self.turn_footer(turn_no, &turns);
                            self.blank();
                        }
                        turn_no = turns
                            .iter()
                            .position(|t| t.start_msg == msg_index(view, &m.id))
                            .unwrap_or(turn_no);
                        self.turn = Some(turn_no);
                        turn_open = true;
                        folded =
                            !self.is_open(&format!("turn:{}:{turn_no}", view.session_id), true);
                        self.turn_header(turn_no, &text, &turns);
                        if !folded {
                            self.user_block(m, &text);
                        }
                        continue;
                    }
                    if !turn_open || folded {
                        continue;
                    }
                    for (pi, p) in m.parts.iter().enumerate() {
                        match p["type"].as_str().unwrap_or("") {
                            "text" => {
                                let text = json_str(&p["text"]);
                                if !text.trim().is_empty() {
                                    self.assistant_block(&format!("{}-{pi}", m.id), &text, false);
                                }
                            }
                            "reasoning" => self.reasoning_block(m, pi, p),
                            "tool_call" => {
                                let call_id = json_str(&p["id"]);
                                let name = json_str(&p["name"]);
                                if name == "session" {
                                    self.child_card(&call_id, &json_str(&p["arguments"]), &results);
                                } else {
                                    self.tool_block(
                                        &format!("tool:{call_id}"),
                                        Some(turn_no),
                                        &call_id,
                                        &name,
                                        &json_str(&p["arguments"]),
                                        &results,
                                        &traces,
                                        &waiting,
                                    );
                                }
                            }
                            _ => {}
                        }
                    }
                }
                Item::Ev(e) => {
                    if !folded && self.diagnostic(e) {
                        // Diagnostics belong to the turn they were produced in.
                        if turn_open {
                            self.blank();
                        }
                    }
                }
            }
        }
        if turn_open && !folded {
            self.turn_footer(turn_no, &turns);
        }

        // Live stream of the current step.
        if let Some(s) = self.app.state.model.streaming.get(&view.session_id) {
            if !s.reasoning.is_empty() {
                self.block(
                    "streaming-reasoning",
                    BlockKind::Reasoning,
                    self.turn,
                    false,
                    |b| {
                        let st = b.t.role_style("reasoning");
                        let rail = b.t.role_rail_style("reasoning");
                        b.line(vec![
                            Span::styled(format!("{} ", b.t.structure("rail")), rail),
                            Span::styled("Thinking", st),
                        ]);
                        for row in text::wrap(&s.reasoning, b.bw.saturating_sub(2)) {
                            b.line(vec![
                                Span::styled(format!("{} ", b.t.structure("rail")), rail),
                                Span::styled(row, st),
                            ]);
                        }
                    },
                );
            }
            if !s.text.is_empty() {
                self.block(
                    "streaming-text",
                    BlockKind::Assistant,
                    self.turn,
                    false,
                    |b| {
                        for row in text::wrap(&s.text, b.bw) {
                            b.line(vec![Span::styled(row, b.primary())]);
                        }
                    },
                );
            }
            if busy {
                self.line(vec![Span::styled(
                    format!("{} working{}", self.t.spinner(self.frame / 2), self.ell()),
                    self.meta(),
                )]);
            }
        }
        // Trim trailing blanks.
        while self.out.last().is_some_and(|l| l.spans.is_empty()) {
            self.out.pop();
        }
    }

    // -- context ---------------------------------------------------------------

    fn context_section(&mut self, view: &crate::reduce::SelectedView, no_turns: bool) {
        let Some(s) = &view.starting else { return };
        let key = format!("context:{}", view.session_id);
        // Expanded before the first turn; collapsed once the session has content.
        let open = self.is_open(&key, no_turns);
        let sys = s["system_prompt"].as_str().unwrap_or("");
        let tools = s["tools"].as_array().map_or(0, |v| v.len());
        let defs = s["tool_definitions"].as_object().map_or(0, |m| m.len());
        let file = &s["instruction_file"];
        let agent = &s["agent"];
        let est = sys.chars().count().div_ceil(4) as u64;
        let file_label = match file["status"].as_str() {
            Some("loaded") => format!(
                "AGENTS.md · {}",
                file["bytes"]
                    .as_u64()
                    .map_or(String::new(), |b| format!("{b} B"))
            ),
            Some(other) => format!("AGENTS.md · {other}"),
            None => "AGENTS.md".into(),
        };
        let items = 3 + usize::from(defs > 0);
        let mut header = vec![
            Span::styled(format!("{} ", self.t.fold(open)), self.st(MUTED, CANVAS)),
            Span::styled(
                "Context",
                self.st(SECONDARY, CANVAS).add_modifier(Modifier::BOLD),
            ),
            Span::styled(
                self.sep()
                    + &format!("{items} items")
                    + &self.sep()
                    + &format!("~{} tokens", fmt_count(est)),
                self.meta(),
            ),
        ];
        if s["origin"].as_str() == Some("reconstructed") {
            header.push(Span::styled(
                self.sep() + "reconstructed",
                self.st("status.warning", CANVAS),
            ));
        }
        self.block(key.clone(), BlockKind::Context, None, true, |b| {
            b.row(header, Vec::new());
            if !open {
                return;
            }
            let rail = b.st("border.default", CANVAS);
            let name = b.secondary();
            let toc = b.meta();
            let row = |b: &mut Self, label: &str, value: String| {
                let pad = b.bw.saturating_sub(2 + width(label) + width(&value));
                b.line(vec![
                    Span::styled(format!("  {} ", b.t.structure("rail")), rail),
                    Span::styled(label.to_string(), name),
                    Span::styled(" ".repeat(pad.max(1)), toc),
                    Span::styled(value, toc),
                ]);
            };
            row(
                b,
                "system prompt",
                format!("{} B · ~{} tokens", sys.len(), fmt_count(est)),
            );
            row(
                b,
                "agent",
                format!(
                    "{} · {}",
                    agent["name"].as_str().unwrap_or("?"),
                    agent["source"].as_str().unwrap_or("?")
                ),
            );
            row(b, &format!("tools ({tools})"), format!("{defs} schemas"));
            row(
                b,
                &file_label,
                file["path"].as_str().unwrap_or("").to_string(),
            );
            let children = s["child_agents"].as_array().map_or(0, Vec::len);
            if children > 0 {
                row(b, "child agents", children.to_string());
            }
            if s["workspace"]["active"].as_bool().unwrap_or(false) {
                row(b, "workspace tools", "active".into());
            }
        });
    }

    // -- turns -----------------------------------------------------------------

    fn turn_meta(&self, view: &crate::reduce::SelectedView, busy: bool) -> Vec<TurnMeta> {
        let mut starts: Vec<(usize, &crate::api::Message)> = Vec::new();
        for (i, m) in view.messages.iter().enumerate() {
            if m.role == "user" && !is_notice(&parts_text(m)) {
                starts.push((i, m));
            }
        }
        let mut out = Vec::new();
        for (n, (i, m)) in starts.iter().enumerate() {
            let end_msg = starts.get(n + 1).map_or(view.messages.len(), |x| x.0);
            let window: Vec<&crate::api::Message> = view.messages[*i..end_msg].iter().collect();
            let mut meta = TurnMeta {
                start_msg: *i,
                start_time: m.created,
                end_time: window.last().map_or(m.created, |x| x.created),
                running: busy && n + 1 == starts.len(),
                followed: n + 1 < starts.len(),
                ..Default::default()
            };
            if let Some(last) = window.last() {
                meta.end_time = last.created;
            }
            // Diagnostics in the same time window.
            for e in &view.events {
                if e.time < meta.start_time {
                    continue;
                }
                if n + 1 < starts.len() && e.time > starts[n + 1].1.created {
                    continue;
                }
                match e.kind.as_str() {
                    "usage" => {
                        meta.input += e.data["input_tokens"].as_u64().unwrap_or(0);
                        meta.cached += e.data["cached_input_tokens"].as_u64().unwrap_or(0);
                        meta.output += e.data["output_tokens"].as_u64().unwrap_or(0);
                        meta.reasoning += e.data["reasoning_tokens"].as_u64().unwrap_or(0);
                        meta.end_time = meta.end_time.max(e.time);
                    }
                    "step_start" => meta.steps += 1,
                    "turn_end" => {
                        meta.stop = e.data["stop"].as_str().map(str::to_owned);
                        meta.end_event = true;
                        if let Some(s) = e.data["steps"].as_u64() {
                            if s > meta.steps {
                                meta.steps = s;
                            }
                        }
                        meta.end_time = meta.end_time.max(e.time);
                    }
                    "wire_request" => {
                        if let Some(m) = e.data["body"]["model"].as_str() {
                            if !meta.models.iter().any(|x| x == m) {
                                meta.models.push(m.to_owned());
                            }
                        }
                    }
                    "session.error" => {
                        meta.error = Some((
                            e.data["kind"].as_str().unwrap_or("error").to_owned(),
                            e.data["message"].as_str().unwrap_or("").to_owned(),
                        ));
                        meta.end_time = meta.end_time.max(e.time);
                    }
                    _ => {}
                }
            }
            let mut seen = std::collections::HashSet::new();
            for m in &window {
                for p in &m.parts {
                    if p["type"] == "tool_call" {
                        if let Some(id) = p["id"].as_str() {
                            if seen.insert(id.to_owned()) {
                                meta.tools += 1;
                            }
                        }
                    }
                }
            }
            out.push(meta);
        }
        out
    }

    fn turn_header(&mut self, n: usize, text: &str, turns: &[TurnMeta]) {
        let key = format!("turn:{}:{n}", self.app.viewed_id().unwrap_or_default());
        let folded = !self.is_open(&key, true);
        let meta = turns.get(n).cloned().unwrap_or_default();
        let mut left = vec![
            Span::styled(format!("{} ", self.t.fold(!folded)), self.meta()),
            Span::styled(format!("#{}  ", n + 1), self.meta()),
            Span::styled(
                truncate(&first_line(text), self.bw.saturating_sub(20), self.ell()),
                self.primary().add_modifier(Modifier::BOLD),
            ),
        ];
        let mut right = Vec::new();
        if meta.tools > 0 {
            right.push(Span::styled(
                format!(
                    "{} tool{}{}",
                    meta.tools,
                    if meta.tools == 1 { "" } else { "s" },
                    self.sep()
                ),
                self.meta(),
            ));
        }
        if meta.duration() >= 0.5 {
            right.push(Span::styled(
                format!("{}{}", fmt_duration(meta.duration()), self.sep()),
                self.meta(),
            ));
        }
        right.extend(self.turn_status(&meta));
        let _ = &mut left;
        self.block(key, BlockKind::Turn, Some(n), true, |b| {
            b.row(left, right);
        });
    }

    fn turn_status(&self, meta: &TurnMeta) -> Vec<Span<'static>> {
        if meta.running {
            return vec![
                Span::styled(
                    format!("{} ", self.t.spinner(self.frame / 2)),
                    self.st(PRIMARY, CANVAS),
                ),
                Span::styled("running".to_string(), self.primary()),
            ];
        }
        if let Some((kind, _)) = &meta.error {
            if kind != "interrupted" {
                let (g, l, tok) = self.t.status("error");
                return vec![Span::styled(format!("{g} {l}"), self.status_style(tok))];
            }
        }
        match meta.stop.as_deref() {
            Some("max_steps") => {
                let (g, _, tok) = self.t.status("interrupted");
                vec![Span::styled(
                    format!("{g} step limit"),
                    self.status_style(tok),
                )]
            }
            Some(_) if meta.error.is_none() => {
                let (g, l, tok) = self.t.status("done");
                vec![Span::styled(format!("{g} {l}"), self.status_style(tok))]
            }
            _ if meta.end_event || meta.followed => {
                let (g, l, tok) = self.t.status("done");
                vec![Span::styled(format!("{g} {l}"), self.status_style(tok))]
            }
            _ => {
                let (g, l, tok) = self.t.status("interrupted");
                vec![Span::styled(format!("{g} {l}"), self.status_style(tok))]
            }
        }
    }

    fn turn_footer(&mut self, n: usize, turns: &[TurnMeta]) {
        let Some(meta) = turns.get(n) else { return };
        self.line(vec![Span::styled(
            self.t.structure("rule").repeat(self.bw),
            self.st("border.default", CANVAS),
        )]);
        let session = self.app.viewed();
        let mut bits: Vec<String> = Vec::new();
        if !meta.models.is_empty() {
            bits.push(meta.models.join(" / "));
        } else if let Some(m) = session.and_then(|s| s.model.clone()) {
            let short = m.rsplit('/').next().unwrap_or(&m).to_owned();
            if m != "default" && !m.is_empty() {
                // A ref is `provider/model`; a lone bare id stays as it is.
                bits.push(if short.len() > 1 { m } else { short });
            }
        }
        if let Some(effort) = session.and_then(|s| {
            s.extra
                .get("effort")
                .and_then(Value::as_str)
                .map(str::to_owned)
        }) {
            if !effort.is_empty() && effort != "none" && effort != "default" {
                bits.push(effort);
            }
        }
        bits.push(fmt_duration(meta.duration()));
        if meta.steps > 0 {
            bits.push(format!(
                "{} call{}",
                meta.steps,
                if meta.steps == 1 { "" } else { "s" }
            ));
        }
        if meta.tools > 0 {
            bits.push(format!(
                "{} tool{}",
                meta.tools,
                if meta.tools == 1 { "" } else { "s" }
            ));
        }
        if meta.has_usage() {
            let mut tok = format!("{} in", fmt_count(meta.input));
            if meta.cached > 0 {
                tok.push_str(&format!(" {} cached", fmt_count(meta.cached)));
            }
            tok.push_str(&format!(" {} out", fmt_count(meta.output)));
            if meta.reasoning > 0 {
                tok.push_str(&format!(" {} reasoning", fmt_count(meta.reasoning)));
            }
            bits.push(tok);
        }
        let text = bits.join(&self.sep());
        let mut rows = text::wrap(&text, self.bw);
        // The end status is never truncated: append it to the last row, or a
        // new one when it does not fit.
        let status = self.turn_status(meta);
        let sw: usize = status.iter().map(|s| width(s.content.as_ref())).sum();
        let last_w = rows.last().map_or(0, |r| width(r));
        if last_w + 1 + sw > self.bw {
            rows.push(String::new());
        }
        for (i, row) in rows.iter().enumerate() {
            let mut spans = vec![Span::styled(row.clone(), self.meta())];
            if i + 1 == rows.len() {
                let pad = self.bw.saturating_sub(width(row) + 1 + sw).max(1);
                spans.push(Span::raw(" ".repeat(pad)));
                spans.extend(status.clone());
            }
            self.line(spans);
        }
    }

    // -- items -----------------------------------------------------------------

    fn user_block(&mut self, m: &crate::api::Message, text: &str) {
        let key = format!("user:{}", m.id);
        let bg = self.t.role_style("user_text");
        let rail = self.t.role_rail_style("user_text");
        let bar = self.t.structure("focus_bar").to_owned();
        let lines = text.lines().count();
        let big = text.len() > 4096 || lines > 40;
        let open = self.is_open(&key, !big);
        self.block(key.clone(), BlockKind::User, self.turn, big, |b| {
            let body = if big && !open {
                let head: Vec<&str> = text.lines().take(6).collect();
                format!(
                    "{}\n{} {} more lines (sent whole)",
                    head.join("\n"),
                    b.ell(),
                    lines.saturating_sub(6)
                )
            } else {
                text.to_owned()
            };
            for row in text::wrap(&body, b.bw.saturating_sub(2)) {
                b.out.push(TLine {
                    spans: vec![
                        Span::styled(bar.clone(), rail),
                        Span::styled(format!(" {row}"), bg),
                    ],
                    bg: Some(bg),
                });
            }
        });
    }

    fn notice(&mut self, m: &crate::api::Message, text: &str) {
        let key = format!("notice:{}", m.id);
        let kind = if text.contains("recovery") {
            "diagnostic"
        } else if text.contains("inserted") {
            "context_insertion"
        } else {
            "context_notice"
        };
        let st = self.t.role_style(kind);
        let rail = self.t.role_rail_style(kind);
        let label = self.t.role(kind).label.clone();
        let heavy = self.t.structure("rail_heavy").to_owned();
        let bg = self.t.role(kind).bg.clone();
        let bgst = self.st(&bg, &bg);
        self.block(key, BlockKind::Notice, self.turn, false, |b| {
            b.out.push(TLine {
                spans: vec![
                    Span::styled(heavy.clone(), rail),
                    Span::styled(format!(" {label}"), st.add_modifier(Modifier::BOLD)),
                ],
                bg: Some(bgst),
            });
            for row in text::wrap(text, b.bw.saturating_sub(2)) {
                b.out.push(TLine {
                    spans: vec![
                        Span::styled(heavy.clone(), rail),
                        Span::styled(format!(" {row}"), st),
                    ],
                    bg: Some(bgst),
                });
            }
        });
    }

    fn assistant_block(&mut self, key: &str, text: &str, streaming: bool) {
        let k = format!("assistant:{key}");
        let open = self.is_open(&k, true);
        let body = if open {
            text.to_owned()
        } else {
            let head: Vec<&str> = text.lines().take(4).collect();
            let n = text.lines().count().saturating_sub(4);
            format!("{}\n{} {n} more lines", head.join("\n"), self.ell())
        };
        let _ = streaming;
        self.block(k.clone(), BlockKind::Assistant, self.turn, true, |b| {
            b.markdown(&body);
            if !open {
                b.line(vec![Span::styled(
                    format!("  show all ({} lines)", text.lines().count()),
                    b.meta(),
                )]);
            }
        });
    }

    fn reasoning_block(&mut self, m: &crate::api::Message, pi: usize, p: &Value) {
        let key = format!("reason:{}:{pi}", m.id);
        let summary = json_str(&p["summary"]);
        let encrypted = summary.trim().is_empty();
        let open = self.is_open(&key, false);
        let st = self.t.role_style("reasoning");
        let rail = self.t.role_rail_style("reasoning");
        let razor = self.t.structure("rail").to_owned();
        let label = self.t.role("reasoning").label.clone();
        self.block(key, BlockKind::Reasoning, self.turn, true, |b| {
            let head = if encrypted {
                format!("{label} · encrypted (replayed, not readable)")
            } else {
                label.clone()
            };
            b.line(vec![
                Span::styled(format!("{razor} {head}"), st),
                Span::styled(if open { String::new() } else { " …".into() }, st),
            ]);
            if open && !encrypted {
                for row in text::wrap(&summary, b.bw.saturating_sub(2)) {
                    b.line(vec![
                        Span::styled(format!("{razor} "), rail),
                        Span::styled(row, st),
                    ]);
                }
            }
        });
    }

    // One row needs the call, its result, timing and the approval state; the
    // alternative is a temporary struct used nowhere else.
    #[allow(clippy::too_many_arguments)]
    fn tool_block(
        &mut self,
        key: &str,
        turn: Option<usize>,
        call_id: &str,
        name: &str,
        args: &str,
        results: &HashMap<String, Done>,
        traces: &HashMap<String, ToolTrace>,
        waiting: &HashMap<String, String>,
    ) {
        let open = self.is_open(key, false);
        let raw = self.is_open(&format!("{key}:raw"), false);
        let done = results.get(call_id);
        let trace = traces.get(call_id);
        let is_waiting = waiting.contains_key(call_id);
        let (glyph, tok, label) = if is_waiting {
            (
                self.t.status("waiting").0,
                self.t.status("waiting").2,
                "waiting",
            )
        } else if done.is_some_and(|d| d.is_error) || trace.is_some_and(|t| t.error.is_some()) {
            (self.t.status("error").0, self.t.status("error").2, "error")
        } else if done.is_some() || trace.is_some_and(|t| t.end.is_some()) {
            (self.t.status("done").0, self.t.status("done").2, "done")
        } else {
            (self.t.spinner(self.frame / 2), PRIMARY, "running")
        };
        let dur = trace
            .and_then(|t| Some((t.start?, t.end?)))
            .map(|(s, e)| e - s);
        let (arg, summary) = tool_summary(name, args, done);
        let detail = self.tool_detail(name, args, done, trace, raw);
        let key = key.to_owned();
        let name = name.to_owned();
        let _ = label;
        self.block(key.clone(), BlockKind::Tool, turn, true, |b| {
            let mut left = vec![
                Span::styled(glyph.to_owned(), b.st(tok, CANVAS)),
                Span::raw(" "),
                Span::styled(
                    text::pad_right(&name, b.name_w),
                    b.primary().add_modifier(Modifier::BOLD),
                ),
                Span::raw(" "),
            ];
            let mut pieces: Vec<Vec<Span<'static>>> = Vec::new();
            if let Some(summary) = &summary {
                let mut spans = Vec::new();
                for (text, _) in summary {
                    let style = if text.starts_with('+') {
                        b.st("diff.add", CANVAS)
                    } else if text.starts_with('−') || text.starts_with('-') {
                        b.st("diff.del", CANVAS)
                    } else {
                        b.secondary()
                    };
                    spans.push(Span::styled(text.clone(), style));
                }
                pieces.push(spans);
            }
            if is_waiting {
                pieces.push(vec![Span::styled(
                    "waiting for approval",
                    b.status_style(tok),
                )]);
            } else if done.is_none() && trace.is_none() {
                pieces.push(vec![Span::styled("running…", b.meta())]);
            } else if let Some(d) = dur {
                pieces.push(vec![Span::styled(fmt_duration(d), b.meta())]);
            }
            let mut right: Vec<Span<'static>> = Vec::new();
            for (i, piece) in pieces.into_iter().enumerate() {
                if i > 0 {
                    right.push(Span::styled(b.sep(), b.meta()));
                }
                right.extend(piece);
            }
            let room =
                b.bw.saturating_sub(
                    2 + b.name_w
                        + 1
                        + right
                            .iter()
                            .map(|s| width(s.content.as_ref()))
                            .sum::<usize>()
                        + 1,
                )
                .max(4);
            left.push(Span::styled(
                truncate_middle(&arg, room, b.ell()),
                b.secondary(),
            ));
            b.row(left, right);
            if open && !detail.is_empty() {
                for (line, style) in detail.clone() {
                    b.line(vec![
                        Span::styled(
                            format!("  {} ", b.t.structure("rail")),
                            b.st("border.default", CANVAS),
                        ),
                        Span::styled(line, style),
                    ]);
                }
                b.line(vec![
                    Span::styled("   ", b.meta()),
                    Span::styled(
                        if raw { "[raw] " } else { "raw " },
                        if raw { b.st(ACCENT, CANVAS) } else { b.meta() },
                    ),
                    Span::styled("r", b.meta()),
                ]);
            }
        });
    }

    fn tool_detail(
        &self,
        name: &str,
        args: &str,
        done: Option<&Done>,
        trace: Option<&ToolTrace>,
        raw: bool,
    ) -> Vec<(String, Style)> {
        let mut out = Vec::new();
        if raw {
            for line in args.lines().take(60) {
                out.push((line.to_owned(), self.secondary()));
            }
            if let Some(d) = done {
                for line in d.output.lines().take(60) {
                    out.push((
                        line.to_owned(),
                        if d.is_error {
                            self.st("status.error", CANVAS)
                        } else {
                            self.secondary()
                        },
                    ));
                }
            }
            return out;
        }
        let a = arg_json(args).unwrap_or(Value::Null);
        match name {
            "edit" | "patch" => {
                if let Some(patch) = a["patch"].as_str() {
                    for line in patch.lines().take(200) {
                        let style = if line.starts_with('+') && !line.starts_with("+++") {
                            self.st("diff.add", CANVAS)
                        } else if line.starts_with('-') && !line.starts_with("---") {
                            self.st("diff.del", CANVAS)
                        } else if line.starts_with("@@") {
                            self.st("status.info", CANVAS)
                        } else {
                            self.secondary()
                        };
                        out.push((line.to_owned(), style));
                    }
                } else {
                    let old = a["old_string"]
                        .as_str()
                        .or_else(|| a["old"].as_str())
                        .unwrap_or("");
                    let new = a["new_string"]
                        .as_str()
                        .or_else(|| a["new"].as_str())
                        .unwrap_or("");
                    out.push((
                        format!("@@ {}", a["path"].as_str().unwrap_or("")),
                        self.st("status.info", CANVAS),
                    ));
                    for line in old.lines().take(100) {
                        out.push((format!("- {line}"), self.st("diff.del", CANVAS)));
                    }
                    for line in new.lines().take(100) {
                        out.push((format!("+ {line}"), self.st("diff.add", CANVAS)));
                    }
                }
            }
            "write" => {
                out.push((
                    format!("+ {}", a["path"].as_str().unwrap_or("")),
                    self.st("status.info", CANVAS),
                ));
                for line in a["content"].as_str().unwrap_or("").lines().take(200) {
                    out.push((format!("+ {line}"), self.st("diff.add", CANVAS)));
                }
            }
            _ => {
                if let Some(d) = done {
                    let lines: Vec<&str> = d.output.lines().collect();
                    let limit = 200;
                    for line in lines.iter().take(limit) {
                        out.push((
                            line.to_string(),
                            if d.is_error {
                                self.st("status.error", CANVAS)
                            } else {
                                self.secondary()
                            },
                        ));
                    }
                    if lines.len() > limit {
                        out.push((
                            format!(
                                "{} {} lines hidden · show all",
                                self.ell(),
                                lines.len() - limit
                            ),
                            self.meta(),
                        ));
                    }
                } else if let Some(err) = trace.and_then(|t| t.error.clone()) {
                    out.push((err, self.st("status.error", CANVAS)));
                } else {
                    out.push(("no output".into(), self.meta()));
                }
            }
        }
        out
    }

    fn child_card(&mut self, call_id: &str, args: &str, results: &HashMap<String, Done>) {
        let a = arg_json(args).unwrap_or(Value::Null);
        let agent = a["agent"].as_str().unwrap_or("child").to_owned();
        let title = a["title"]
            .as_str()
            .map(str::to_owned)
            .or_else(|| a["prompt"].as_str().map(first_line))
            .unwrap_or_default();
        let child = self
            .app
            .viewed_id()
            .and_then(|parent| {
                self.app.state.model.sessions.values().find(|s| {
                    s.parent_id.as_deref() == Some(parent)
                        && s.parent_call_id.as_deref() == Some(call_id)
                })
            })
            .cloned();
        let done = results.get(call_id);
        let waits = child.as_ref().is_some_and(|c| {
            self.app
                .state
                .model
                .approvals
                .values()
                .any(|w| w.session_id == c.id)
                || self
                    .app
                    .state
                    .model
                    .questions
                    .values()
                    .any(|w| w.session_id == c.id)
        });
        let busy = child.as_ref().is_some_and(|c| self.app.is_busy(&c.id));
        let (glyph, tok) = if waits {
            (self.t.status("waiting").0, "interactive.accent")
        } else if busy {
            (self.t.spinner(self.frame / 2), PRIMARY)
        } else if done.is_some_and(|d| d.is_error) {
            (self.t.status("error").0, "status.error")
        } else if done.is_some() || child.is_some() {
            (self.t.status("done").0, "status.success")
        } else {
            (self.t.spinner(self.frame / 2), PRIMARY)
        };
        let rail = self.t.role_rail_style("child_card");
        let st = self.t.role_style("child_card");
        let heavy = self.t.structure("rail_heavy").to_owned();
        let bg = self.t.role("child_card").bg.clone();
        let bgst = self.st(&bg, &bg);
        let key = format!("child:{call_id}");
        let child_id = child.as_ref().map(|c| c.id.clone());
        let status_word = if waits {
            "waiting for input".to_string()
        } else if busy {
            "running".into()
        } else if done.is_some_and(|d| d.is_error) {
            "failed".into()
        } else if done.is_some() {
            "done".into()
        } else {
            "starting…".into()
        };
        let title = if title.is_empty() {
            child.as_ref().map(|c| c.title.clone()).unwrap_or_default()
        } else {
            title
        };
        let quote = if title.is_empty() {
            String::new()
        } else {
            format!(" · \"{}\"", truncate(&title, 48, self.ell()))
        };
        let head = vec![
            Span::styled(heavy.clone(), rail),
            Span::styled(format!(" {glyph} "), self.st(tok, PANEL)),
            Span::styled(
                "child",
                self.st(SECONDARY, PANEL).add_modifier(Modifier::BOLD),
            ),
            Span::styled(format!(" · {agent}"), self.st(PRIMARY, PANEL)),
            Span::styled(quote, self.st(SECONDARY, PANEL)),
        ];
        let right = vec![Span::styled(status_word, self.st(tok, PANEL))];
        self.block(
            key.clone(),
            BlockKind::Child,
            self.turn,
            child_id.is_some(),
            |b| {
                b.out.push(TLine {
                    spans: {
                        let mut s = head.clone();
                        let lw: usize = s.iter().map(|x| width(x.content.as_ref())).sum();
                        let rw: usize = right.iter().map(|x| width(x.content.as_ref())).sum();
                        if lw + rw < b.bw {
                            s.push(Span::raw(" ".repeat(b.bw - lw - rw)));
                            s.extend(right.clone());
                        } else {
                            s.push(Span::raw(" "));
                            s.extend(right.clone());
                        }
                        s
                    },
                    bg: Some(bgst),
                });
                let body = if let Some(d) = done {
                    let lines: Vec<&str> =
                        d.output.lines().filter(|l| !l.trim().is_empty()).collect();
                    if d.is_error {
                        format!("✕ {}", lines.first().copied().unwrap_or("failed"))
                    } else {
                        lines.iter().take(3).cloned().collect::<Vec<_>>().join("\n")
                    }
                } else if waits {
                    "waiting for input".into()
                } else if busy {
                    "running…".into()
                } else {
                    "starting…".into()
                };
                if !body.is_empty() {
                    for row in text::wrap(&body, b.bw.saturating_sub(2))
                        .into_iter()
                        .take(4)
                    {
                        b.out.push(TLine {
                            spans: vec![
                                Span::styled(format!("{heavy} "), rail),
                                Span::styled(row, st),
                            ],
                            bg: Some(bgst),
                        });
                    }
                }
            },
        );
        if let Some(id) = child_id {
            if let Some(last) = self
                .blocks
                .last_mut()
                .filter(|b| b.kind == BlockKind::Child)
            {
                last.target = Some(id);
            }
        }
    }

    // -- diagnostics ------------------------------------------------------------

    fn diagnostic(&mut self, e: &crate::api::Event) -> bool {
        let d = &e.data;
        let (text, detail, level) = match e.kind.as_str() {
            "session.error" => (
                format!(
                    "{}: {}",
                    d["kind"].as_str().unwrap_or("error"),
                    d["message"].as_str().unwrap_or("")
                ),
                d["status"].as_i64().map(|s| format!("HTTP {s}")),
                "error",
            ),
            "retry_scheduled" => (
                format!(
                    "provider retry {}/{} in {}s",
                    d["attempt"].as_u64().unwrap_or(0),
                    d["max_attempts"].as_u64().unwrap_or(0),
                    d["delay"].as_f64().unwrap_or(0.0)
                ),
                d["reason"].as_str().map(str::to_owned),
                "warning",
            ),
            "retry_started" => (
                format!(
                    "provider retry started (attempt {})",
                    d["attempt"].as_u64().unwrap_or(0)
                ),
                None,
                "warning",
            ),
            "retry_exhausted" => (
                format!(
                    "provider retries exhausted after {}",
                    d["attempts"].as_u64().unwrap_or(0)
                ),
                d["reason"].as_str().map(str::to_owned),
                "error",
            ),
            "turn.recovered" => (
                format!(
                    "turn {}: {}",
                    d["outcome"].as_str().unwrap_or("interrupted"),
                    d["reason"].as_str().unwrap_or("daemon restarted")
                ),
                None,
                "warning",
            ),
            "session.directory_changed" => (
                format!(
                    "directory changed · {} → {}",
                    d["from"].as_str().unwrap_or("?"),
                    d["to"].as_str().unwrap_or("?")
                ),
                None,
                "info",
            ),
            "session.notified" => (
                format!("child finished: {}", d["from"].as_str().unwrap_or("child")),
                None,
                "info",
            ),
            "permission.replied" if d["outcome"].as_str() == Some("cancelled") => {
                ("approval cancelled: interrupted".into(), None, "warning")
            }
            "question.replied" if d["outcome"].as_str() == Some("cancelled") => {
                ("question cancelled: interrupted".into(), None, "warning")
            }
            _ => return false,
        };
        let key = format!("diag:{}", e.id);
        let (glyph, _label, token) = match level {
            "error" => self.t.status("error"),
            "warning" => self.t.status("interrupted"),
            _ => ("i", "info", "status.info"),
        };
        let st = self.t.role_style("diagnostic");
        let rail = self.t.role_rail_style("diagnostic");
        let heavy = self.t.structure("rail_heavy").to_owned();
        let bg = self.t.role("diagnostic").bg.clone();
        let bgst = self.st(&bg, &bg);
        let head = format!("{glyph} {text}");
        self.block(key, BlockKind::Diagnostic, self.turn, false, |b| {
            b.out.push(TLine {
                spans: vec![
                    Span::styled(heavy.clone(), rail),
                    Span::styled(format!(" {head}"), st.add_modifier(Modifier::BOLD)),
                ],
                bg: Some(bgst),
            });
            if let Some(detail) = detail {
                for row in text::wrap(&detail, b.bw.saturating_sub(2)) {
                    b.out.push(TLine {
                        spans: vec![
                            Span::styled(heavy.clone(), rail),
                            Span::styled(format!(" {row}"), st),
                        ],
                        bg: Some(bgst),
                    });
                }
            }
        });
        let _ = token;
        true
    }

    // -- markdown-lite ----------------------------------------------------------

    fn markdown(&mut self, text: &str) {
        let mut fence: Option<String> = None;
        let mut fence_lines: Vec<String> = Vec::new();
        for raw in text.lines() {
            if let Some(rest) = raw.trim_start().strip_prefix("```") {
                if fence.is_some() {
                    self.fenced(fence.take().unwrap_or_default(), &fence_lines);
                    fence_lines.clear();
                } else {
                    fence = Some(rest.trim().to_owned());
                }
                continue;
            }
            if fence.is_some() {
                fence_lines.push(raw.to_owned());
                continue;
            }
            self.md_line(raw);
        }
        if fence.is_some() {
            for line in fence_lines {
                self.line(vec![Span::styled(line, self.secondary())]);
            }
        }
    }

    fn fenced(&mut self, lang: String, lines: &[String]) {
        let label = if lang.is_empty() {
            "code".to_string()
        } else {
            lang
        };
        self.line(vec![Span::styled(format!("  {label}"), self.meta())]);
        for line in lines.iter().take(200) {
            for row in text::wrap_line(line, self.bw.saturating_sub(2)) {
                self.line(vec![
                    Span::styled(
                        format!("  {} ", self.t.structure("rail")),
                        self.st("border.default", CANVAS),
                    ),
                    Span::styled(row, self.secondary()),
                ]);
            }
        }
        if lines.len() > 200 {
            self.line(vec![Span::styled(
                format!("  {} {} lines hidden", self.ell(), lines.len() - 200),
                self.meta(),
            )]);
        }
    }

    fn md_line(&mut self, raw: &str) {
        if raw.trim().is_empty() {
            self.blank();
            return;
        }
        let t = raw.trim_end();
        if let Some(rest) = t
            .strip_prefix("###### ")
            .or_else(|| t.strip_prefix("##### "))
        {
            self.line(vec![Span::styled(rest.to_owned(), self.primary())]);
        } else if let Some(rest) = t
            .strip_prefix("# ")
            .or_else(|| t.strip_prefix("## "))
            .or_else(|| t.strip_prefix("### "))
            .or_else(|| t.strip_prefix("#### "))
        {
            for row in text::wrap(&strip_inline(rest), self.bw) {
                self.line(vec![Span::styled(
                    row,
                    self.primary().add_modifier(Modifier::BOLD),
                )]);
            }
        } else if let Some(rest) = t.strip_prefix("> ") {
            for row in text::wrap(&strip_inline(rest), self.bw.saturating_sub(2)) {
                self.line(vec![
                    Span::styled(
                        format!("  {} ", self.t.structure("rail")),
                        self.st("border.default", CANVAS),
                    ),
                    Span::styled(row, self.secondary()),
                ]);
            }
        } else if let Some(rest) = t
            .strip_prefix("- ")
            .or_else(|| t.strip_prefix("* "))
            .or_else(|| t.strip_prefix("+ "))
        {
            self.bullet("·", rest);
        } else if let Some((marker, rest)) = numbered(t) {
            self.bullet(&marker, rest);
        } else if t.starts_with('|') {
            // Tables stay readable as preformatted rows; no alignment guessing.
            for row in text::wrap(t, self.bw) {
                self.line(vec![Span::styled(row, self.secondary())]);
            }
        } else if t.starts_with("---") || t.starts_with("***") {
            self.line(vec![Span::styled(
                self.t.structure("rule").repeat(self.bw),
                self.st("border.default", CANVAS),
            )]);
        } else {
            for row in text::wrap(&strip_inline(t), self.bw) {
                self.line(vec![Span::styled(row, self.primary())]);
            }
        }
    }

    fn bullet(&mut self, marker: &str, rest: &str) {
        let text = strip_inline(rest);
        let mut first = true;
        for row in text::wrap(&text, self.bw.saturating_sub(2)) {
            if first {
                self.line(vec![
                    Span::styled(format!("{marker} "), self.meta()),
                    Span::styled(row, self.primary()),
                ]);
                first = false;
            } else {
                self.line(vec![Span::raw("  "), Span::styled(row, self.primary())]);
            }
        }
    }
}

fn msg_index(view: &crate::reduce::SelectedView, id: &str) -> usize {
    view.messages
        .iter()
        .position(|m| m.id == id)
        .unwrap_or(usize::MAX)
}

fn parts_text(m: &crate::api::Message) -> String {
    m.parts
        .iter()
        .filter(|p| p["type"] == "text")
        .map(|p| p["text"].as_str().unwrap_or(""))
        .collect::<Vec<_>>()
        .join("\n")
}

fn first_line(s: &str) -> String {
    s.lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or("")
        .to_owned()
}

fn numbered(s: &str) -> Option<(String, &str)> {
    let dot = s.find(['.', ')'])?;
    let (n, rest) = s.split_at(dot);
    if n.len() <= 3 && n.chars().all(|c| c.is_ascii_digit()) && rest[1..].starts_with(' ') {
        Some((format!("{n}{}", &rest[..1]), &rest[1..]))
    } else {
        None
    }
}

/// Remove the most common inline Markdown markers; text is what matters in a
/// terminal, the words stay byte-identical otherwise.
fn strip_inline(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '`' => {}
            '*' => {
                // Emphasis and strong markers come in pairs; strip a paired one
                // and keep a lone asterisk (multiplication, globs).
                if chars.clone().any(|x| x == '*') {
                    continue;
                }
                out.push(c);
            }
            '[' => {
                let mut label = String::new();
                let mut depth = 1;
                let mut closed = false;
                for x in chars.by_ref() {
                    match x {
                        '[' => depth += 1,
                        ']' => {
                            depth -= 1;
                            if depth == 0 {
                                closed = true;
                                break;
                            }
                        }
                        _ => {}
                    }
                    if depth > 0 {
                        label.push(x);
                    }
                }
                if closed && chars.peek() == Some(&'(') {
                    // drop the URL
                    let mut d = 0;
                    for x in chars.by_ref() {
                        if x == '(' {
                            d += 1;
                        } else if x == ')' {
                            d -= 1;
                            if d == 0 {
                                break;
                            }
                        }
                    }
                    out.push_str(&label);
                } else {
                    out.push('[');
                    out.push_str(&label);
                    if closed {
                        out.push(']');
                    }
                }
            }
            _ => out.push(c),
        }
    }
    out
}

fn diff_counts(args: &str) -> Option<(usize, usize)> {
    let a = arg_json(args)?;
    if let Some(patch) = a["patch"].as_str() {
        let mut add = 0;
        let mut del = 0;
        for line in patch.lines() {
            if line.starts_with('+') && !line.starts_with("+++") {
                add += 1;
            } else if line.starts_with('-') && !line.starts_with("---") {
                del += 1;
            }
        }
        return Some((add, del));
    }
    let old = a["old_string"]
        .as_str()
        .or_else(|| a["old"].as_str())
        .unwrap_or("");
    let new = a["new_string"]
        .as_str()
        .or_else(|| a["new"].as_str())
        .unwrap_or("");
    Some((new.lines().count(), old.lines().count()))
}

fn tool_summary(
    name: &str,
    args: &str,
    done: Option<&Done>,
) -> (String, Option<Vec<(String, Style)>>) {
    let a = arg_json(args).unwrap_or(Value::Null);
    let path = a["path"].as_str().unwrap_or("");
    let summary_style = Style::default();
    let summary = match name {
        "edit" | "patch" if done.is_some_and(|d| d.is_error) => {
            Some(vec![("failed".to_string(), summary_style)])
        }
        "edit" | "patch" => diff_counts(args).map(|(add, del)| {
            vec![
                (format!("+{add} "), summary_style),
                (format!("−{del}"), summary_style),
            ]
        }),
        "write" => a["content"].as_str().map(|c| {
            let n = c.lines().count();
            vec![(format!("+{n}"), summary_style)]
        }),
        "read" => done.map(|d| {
            let n = d.output.lines().count();
            vec![(format!("{n} lines"), summary_style)]
        }),
        "bash" => {
            let cmd = a["command"].as_str().unwrap_or("");
            let _ = cmd;
            done.map(|d| {
                let n = d.output.lines().count();
                vec![(format!("{n} lines"), summary_style)]
            })
        }
        "code" => Some(vec![("cell".to_string(), summary_style)]),
        "ask" if a["questions"].as_array().is_some_and(|q| !q.is_empty()) => {
            Some(vec![("question".to_string(), summary_style)])
        }
        _ => None,
    };
    let arg = match name {
        "edit" | "write" | "read" => path.to_owned(),
        "patch" => a["patch"]
            .as_str()
            .map(|p| p.lines().next().unwrap_or("patch").to_owned())
            .unwrap_or_default(),
        "bash" => a["command"]
            .as_str()
            .map(|c| c.lines().next().unwrap_or("").to_owned())
            .unwrap_or_default(),
        "code" => a["code"]
            .as_str()
            .map(|c| c.lines().next().unwrap_or("").to_owned())
            .unwrap_or_default(),
        "ask" => a["questions"]
            .as_array()
            .and_then(|q| q.first())
            .and_then(|q| q["question"].as_str())
            .map(str::to_owned)
            .unwrap_or_default(),
        _ => {
            let v = a["path"]
                .as_str()
                .or_else(|| a["query"].as_str())
                .or_else(|| a["url"].as_str())
                .or_else(|| a["name"].as_str());
            v.unwrap_or("").to_owned()
        }
    };
    // The diff summary needs the theme; the caller applies colours for these
    // three tools, so a neutral style here is replaced there.
    (arg, summary)
}

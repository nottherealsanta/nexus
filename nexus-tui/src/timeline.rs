//! The session timeline: every model-visible item in order, plus the execution
//! diagnostics Core keeps beside it.
//!
//! The builder is pure and shared by drawing and by keyboard/mouse interaction:
//! it produces both the styled lines and a list of `Block`s (the foldable,
//! selectable units). Folding changes how much is drawn, never what exists.
//! Nothing here talks to Core or to the terminal beyond `Theme`.

use std::collections::{HashMap, HashSet};

use ratatui::style::{Modifier, Style};
use ratatui::text::Span;
use serde_json::Value;

use crate::app::App;
use crate::text::{self, truncate, width};
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
    Code,
    Reasoning,
    Group,
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
    /// When set, this block's default depends on its live state.
    pub default_open: Option<bool>,
    /// Child session to open, or message ID to copy for a turn/user block.
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

enum Activity<'a> {
    Thought(&'a crate::api::Message, usize, &'a Value),
    Tool {
        id: String,
        name: String,
        args: String,
    },
}

#[derive(Default)]
struct ToolTrace {
    start: Option<f64>,
    end: Option<f64>,
    error: Option<String>,
}

pub fn build(app: &App, t: &Theme, frame: usize, width: usize) -> Timeline {
    let thoughts = app
        .state
        .model
        .selected
        .as_ref()
        .map(|view| reasoning_times(&view.events))
        .unwrap_or_default();
    let mut b = Builder {
        app,
        t,
        frame,
        bw: width.saturating_sub(2).max(8),
        out: Vec::new(),
        blocks: Vec::new(),
        turn: None,
        denied: HashSet::new(),
        thoughts,
    };
    b.run();
    Timeline {
        lines: b.out,
        blocks: b.blocks,
        turns: b.turn.map_or(0, |n| n + 1),
    }
}

/// Wall time per reasoning part, keyed by its summary text. Deltas are paired
/// with the `part_done` that carries the same summary; a part whose summary
/// arrived without deltas has no duration rather than an invented one.
fn reasoning_times(events: &[crate::api::Event]) -> HashMap<String, f64> {
    let mut out = HashMap::new();
    let mut start: Option<f64> = None;
    let mut text = String::new();
    for event in events {
        match event.kind.as_str() {
            "reasoning_delta" => {
                start.get_or_insert(event.time);
                if let Some(delta) = event.data["text"].as_str() {
                    text.push_str(delta);
                }
            }
            "part_done" => {
                let part = &event.data["part"];
                if part["type"] == "reasoning" {
                    let summary = json_str(&part["summary"]);
                    if let Some(start) = start.filter(|_| !summary.trim().is_empty()) {
                        if summary.trim() == text.trim() {
                            out.insert(summary, (event.time - start).max(0.0));
                        }
                    }
                }
                start = None;
                text.clear();
            }
            "text_delta" | "tool_start" | "step_start" => {
                start = None;
                text.clear();
            }
            _ => {}
        }
    }
    out
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
    denied: HashSet<String>,
    /// Reasoning duration per summary text.
    thoughts: HashMap<String, f64>,
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

pub(crate) fn is_notice(text: &str) -> bool {
    let t = text.trim_start();
    t.starts_with("[Nexus notice:")
        || t.starts_with("[Nexus recovery:")
        || t.starts_with("[Notification:")
}

fn is_diagnostic(e: &crate::api::Event) -> bool {
    matches!(
        e.kind.as_str(),
        "session.error"
            | "retry_scheduled"
            | "retry_started"
            | "retry_exhausted"
            | "turn.recovered"
            | "session.directory_changed"
            | "session.notified"
    ) || (matches!(e.kind.as_str(), "permission.replied" | "question.replied")
        && e.data["outcome"].as_str() == Some("cancelled"))
}

/// Format a stored timestamp in the terminal's local timezone.
#[cfg(unix)]
fn local_clock(timestamp: f64) -> Option<String> {
    if !timestamp.is_finite() || timestamp < 0.0 || timestamp > libc::time_t::MAX as f64 {
        return None;
    }
    let value = timestamp as libc::time_t;
    let mut out = std::mem::MaybeUninit::<libc::tm>::uninit();
    // localtime_r writes `out` on success and does not use shared static storage.
    if unsafe { libc::localtime_r(&value, out.as_mut_ptr()) }.is_null() {
        return None;
    }
    let time = unsafe { out.assume_init() };
    let hour = time.tm_hour.rem_euclid(12);
    Some(format!(
        "{}:{:02} {}",
        if hour == 0 { 12 } else { hour },
        time.tm_min,
        if time.tm_hour < 12 { "AM" } else { "PM" }
    ))
}
#[cfg(not(unix))]
fn local_clock(_timestamp: f64) -> Option<String> {
    None
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
            default_open: None,
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
            .chain(
                self.app
                    .state
                    .model
                    .questions
                    .values()
                    .map(|q| (q.call_id.clone(), "ask".into())),
            )
            .collect();

        // Approval replies refer to approval IDs, not call IDs. Join the durable
        // lifecycle records so a denial is not presented as a tool failure.
        let approval_calls: HashMap<&str, &str> = view
            .events
            .iter()
            .filter(|event| event.kind == "permission.asked")
            .filter_map(|event| Some((event.data["id"].as_str()?, event.data["call_id"].as_str()?)))
            .collect();
        self.denied = view
            .events
            .iter()
            .filter(|event| {
                event.kind == "permission.replied"
                    && event.data["outcome"].as_str() == Some("denied")
            })
            .filter_map(|event| approval_calls.get(event.data["id"].as_str()?).copied())
            .map(str::to_owned)
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
        let mut activity = Vec::new();
        for (_, _, item) in &items {
            match item {
                Item::Msg(m) => {
                    if m.role == "user" {
                        self.activity_group(&mut activity, &results, &traces, &waiting, busy);
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
                        let start = turns.get(turn_no).map_or(0, |t| t.start_msg);
                        let end = turns
                            .get(turn_no + 1)
                            .map_or(view.messages.len(), |t| t.start_msg);
                        let turn_waiting = view.messages[start..end]
                            .iter()
                            .flat_map(|message| message.parts.iter())
                            .any(|part| {
                                part["type"] == "tool_call"
                                    && part["id"]
                                        .as_str()
                                        .is_some_and(|id| waiting.contains_key(id))
                            });
                        folded = !turn_waiting
                            && !self.is_open(&format!("turn:{}:{turn_no}", view.session_id), true);
                        self.turn_block(turn_no, m, &text, &turns, folded);
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
                                    self.activity_group(
                                        &mut activity,
                                        &results,
                                        &traces,
                                        &waiting,
                                        busy,
                                    );
                                    self.assistant_block(&format!("{}-{pi}", m.id), &text, false);
                                }
                            }
                            "reasoning" => activity.push(Activity::Thought(m, pi, p)),
                            "tool_call" => {
                                let call_id = json_str(&p["id"]);
                                let name = json_str(&p["name"]);
                                if name == "session" {
                                    self.activity_group(
                                        &mut activity,
                                        &results,
                                        &traces,
                                        &waiting,
                                        busy,
                                    );
                                    self.child_card(&call_id, &json_str(&p["arguments"]), &results);
                                } else {
                                    activity.push(Activity::Tool {
                                        id: call_id,
                                        name,
                                        args: json_str(&p["arguments"]),
                                    });
                                }
                            }
                            _ => {}
                        }
                    }
                }
                Item::Ev(e) => {
                    if !folded && is_diagnostic(e) {
                        self.activity_group(&mut activity, &results, &traces, &waiting, busy);
                    }
                    if !folded && self.diagnostic(e) {
                        // Diagnostics belong to the turn they were produced in.
                        if turn_open {
                            self.blank();
                        }
                    }
                }
            }
        }
        self.activity_group(&mut activity, &results, &traces, &waiting, busy);
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

    fn turn_block(
        &mut self,
        n: usize,
        message: &crate::api::Message,
        text: &str,
        turns: &[TurnMeta],
        folded: bool,
    ) {
        let key = format!("turn:{}:{n}", self.app.viewed_id().unwrap_or_default());
        let message_key = format!("user:{}", message.id);
        let meta = turns.get(n).cloned().unwrap_or_default();
        let bg = self.t.role_style("user_text");
        let gutter = self.st(MUTED, CANVAS);
        let rail = self.st(MUTED, "surface.raised");
        let use_rail = matches!(
            self.t.opts.tier,
            crate::theme::Tier::Ansi16 | crate::theme::Tier::Mono
        );
        let big = text.len() > 4096 || text.lines().count() > 40;
        let full = self.is_open(&message_key, !big);
        let body = if folded {
            first_line(text)
        } else if big && !full {
            text.lines().take(6).collect::<Vec<_>>().join("\n")
        } else {
            text.to_owned()
        };
        let metadata = if folded {
            let mut parts = vec![format!("#{}", n + 1)];
            if meta.tools > 0 {
                parts.push(format!(
                    "{} tool{}",
                    meta.tools,
                    if meta.tools == 1 { "" } else { "s" }
                ));
            }
            if meta.duration() >= 0.5 {
                parts.push(fmt_duration(meta.duration()));
            }
            let status: String = self
                .turn_status(&meta)
                .iter()
                .map(|s| s.content.as_ref())
                .collect();
            parts.push(status);
            parts.join(&self.sep())
        } else {
            local_clock(message.created).map_or_else(
                || format!("#{}", n + 1),
                |clock| format!("#{}{}{}", n + 1, self.sep(), clock),
            )
        };
        let marker = self.t.fold(!folded).to_owned();
        let fallback_rail = self.t.structure("focus_bar").to_owned();
        self.block(key, BlockKind::Turn, Some(n), true, |b| {
            let pad = |prefix: String| TLine {
                spans: vec![Span::styled(prefix, gutter)],
                bg: Some(bg),
            };
            if !folded {
                b.out.push(pad(marker.clone()));
            }
            let available = b.bw.saturating_sub(3);
            let meta_width = width(&metadata);
            let body_width = available.saturating_sub(meta_width + 2).max(1);
            let mut rows = if folded {
                vec![truncate(&body, body_width, b.ell())]
            } else {
                text::wrap(&body, body_width)
            };
            if big && !full && !folded {
                rows.truncate(6);
            }
            for (i, row) in rows.iter().enumerate() {
                let prefix = if folded {
                    marker.as_str()
                } else if use_rail {
                    fallback_rail.as_str()
                } else {
                    " "
                };
                let mut spans = vec![
                    Span::styled(
                        prefix.to_owned(),
                        if !folded && use_rail { rail } else { gutter },
                    ),
                    Span::styled(format!("  {row}"), bg),
                ];
                if i == 0 && meta_width + 3 < b.bw {
                    let padding = b.bw.saturating_sub(3 + width(row) + meta_width).max(1);
                    spans.push(Span::styled(" ".repeat(padding), bg));
                    spans.push(Span::styled(
                        metadata.clone(),
                        b.st(MUTED, "surface.raised"),
                    ));
                }
                b.out.push(TLine {
                    spans,
                    bg: Some(bg),
                });
            }
            if !folded {
                b.out.push(pad(" ".into()));
            }
        });
        if let Some(block) = self.blocks.last_mut() {
            block.target = Some(message.id.clone());
        }
        if big && !folded {
            let label = if full {
                "collapse message"
            } else {
                "show full message (sent whole)"
            };
            self.block(message_key, BlockKind::User, Some(n), true, |b| {
                b.line(vec![Span::styled(
                    format!("{} {label}", b.t.fold(full)),
                    b.meta(),
                )]);
            });
            if let Some(block) = self.blocks.last_mut() {
                block.default_open = Some(false);
                block.target = Some(message.id.clone());
            }
        }
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
        let block_start = self.blocks.len();
        self.block(k.clone(), BlockKind::Assistant, self.turn, true, |b| {
            b.markdown(&body, &k, text);
            if !open {
                b.line(vec![Span::styled(
                    format!("  show all ({} lines)", text.lines().count()),
                    b.meta(),
                )]);
            }
        });
        if self.blocks.len() > block_start {
            let mut assistant = self.blocks.pop().unwrap();
            assistant.target = Some(text.to_owned());
            self.blocks.insert(block_start, assistant);
        }
    }

    fn activity_group(
        &mut self,
        activity: &mut Vec<Activity<'_>>,
        results: &HashMap<String, Done>,
        traces: &HashMap<String, ToolTrace>,
        waiting: &HashMap<String, String>,
        busy: bool,
    ) {
        if activity.is_empty() {
            return;
        }
        let first = match &activity[0] {
            Activity::Thought(m, i, _) => format!("reason:{}:{i}", m.id),
            Activity::Tool { id, .. } => format!("tool:{id}"),
        };
        let key = format!("group:{first}");
        let has_wait = activity
            .iter()
            .any(|a| matches!(a, Activity::Tool { id, .. } if waiting.contains_key(id)));
        let running = has_wait || (busy && activity.iter().any(|a| matches!(a, Activity::Tool { id, .. } if !results.contains_key(id) && !traces.get(id).is_some_and(|t| t.end.is_some()))));
        let default_open = running || has_wait;
        let open = has_wait || self.is_open(&key, default_open);
        let tree_mode = running
            && !self
                .app
                .ui
                .timeline_open
                .get(&key)
                .copied()
                .unwrap_or(false);
        let clip_live = running
            && !has_wait
            && !self
                .app
                .ui
                .timeline_open
                .get(&key)
                .copied()
                .unwrap_or(false);
        let mut starts = Vec::new();
        let mut ends = Vec::new();
        let mut counts = std::collections::BTreeMap::<&str, usize>::new();
        let mut thought_secs = 0.0_f64;
        let mut failed = 0;
        let mut denied_count = 0;
        for item in activity.iter() {
            let label = match item {
                Activity::Thought(_, _, p) => {
                    if let Some(secs) = self.thoughts.get(&json_str(&p["summary"])) {
                        thought_secs += *secs;
                    }
                    "Thought"
                }
                Activity::Tool { id, name, .. } => {
                    if let Some(trace) = traces.get(id) {
                        if let Some(start) = trace.start {
                            starts.push(start);
                        }
                        if let Some(end) = trace.end {
                            ends.push(end);
                        }
                    }
                    if self.denied.contains(id) {
                        denied_count += 1;
                    } else if results.get(id).is_some_and(|d| d.is_error)
                        || traces.get(id).is_some_and(|t| t.error.is_some())
                    {
                        failed += 1;
                    }
                    match name.as_str() {
                        "bash" => "Ran command",
                        "read" => "Read file",
                        "edit" => "Edited file",
                        "write" => "Wrote file",
                        "code" => "Ran cell",
                        "ask" => "Asked question",
                        _ => "Used tool",
                    }
                }
            };
            *counts.entry(label).or_default() += 1;
        }
        let summary = if running {
            "Working".to_owned()
        } else {
            counts
                .iter()
                .map(|(label, count)| {
                    if *label == "Thought" {
                        return if thought_secs > 0.0 {
                            format!("Thought for {thought_secs:.1}s")
                        } else {
                            "Thought".to_owned()
                        };
                    }
                    let (verb, noun) = label.rsplit_once(' ').unwrap_or((label, ""));
                    format!(
                        "{verb} {count} {noun}{}",
                        if *count == 1 { "" } else { "s" }
                    )
                })
                .collect::<Vec<_>>()
                .join(", ")
        };
        let duration = if !running && !starts.is_empty() && !ends.is_empty() {
            let start = starts.into_iter().fold(f64::INFINITY, f64::min);
            let end = ends.into_iter().fold(f64::NEG_INFINITY, f64::max);
            (end >= start).then_some(end - start)
        } else {
            None
        };
        self.block(key, BlockKind::Group, self.turn, !has_wait, |b| {
            let mut spans = vec![Span::styled(format!("{} ", b.t.fold(open)), b.meta())];
            if running && !b.t.opts.reduced_motion && !b.t.mono() {
                spans.extend(crate::theme::shimmer(
                    &summary,
                    b.frame,
                    true,
                    b.meta(),
                    b.primary(),
                ));
            } else {
                spans.push(Span::styled(
                    summary.clone(),
                    if running { b.primary() } else { b.meta() },
                ));
            }
            if let Some(duration) = duration {
                spans.push(Span::styled(
                    format!("{}{:0.1}s", b.sep(), duration),
                    b.meta(),
                ));
            }
            if denied_count > 0 {
                spans.push(Span::styled(
                    format!("{}{} denied", b.sep(), denied_count),
                    b.meta(),
                ));
            }
            if failed > 0 {
                spans.push(Span::styled(
                    format!(
                        " {} {} {failed} failed",
                        b.t.structure("separator"),
                        b.t.status("error").0
                    ),
                    b.st("status.error", CANVAS),
                ));
            }
            b.line(spans);
        });
        if let Some(block) = self.blocks.last_mut() {
            block.default_open = Some(default_open);
        }
        if open {
            // Keep the tree contiguous: block() normally adds a separating row.
            self.out.pop();
            let skipped = if clip_live {
                activity.len().saturating_sub(5)
            } else {
                0
            };
            if skipped > 0 {
                self.line(vec![Span::styled(
                    format!("{} {skipped} earlier", self.ell()),
                    self.meta(),
                )]);
            }
            for (index, item) in activity.iter().enumerate().skip(skipped) {
                let tree = tree_mode.then_some(index + 1 == activity.len());
                match item {
                    Activity::Thought(m, i, p) => {
                        let secs = self.thoughts.get(&json_str(&p["summary"])).copied();
                        self.reasoning_block(m, *i, p, tree, secs)
                    }
                    Activity::Tool { id, name, args } => self.tool_block(
                        &format!("tool:{id}"),
                        self.turn,
                        id,
                        name,
                        args,
                        results,
                        traces,
                        waiting,
                        tree,
                    ),
                }
                self.out.pop();
            }
            self.blank();
        }
        activity.clear();
    }

    fn reasoning_block(
        &mut self,
        m: &crate::api::Message,
        pi: usize,
        p: &Value,
        tree: Option<bool>,
        seconds: Option<f64>,
    ) {
        let key = format!("reason:{}:{pi}", m.id);
        let summary = json_str(&p["summary"]);
        let encrypted = summary.trim().is_empty();
        let open = self.is_open(&key, false);
        let st = self.t.role_style("reasoning");
        let rail = self.t.role_rail_style("reasoning");
        let razor = self.t.structure("rail").to_owned();
        let label = self.t.role("reasoning").label.clone();
        self.block(key, BlockKind::Reasoning, self.turn, true, |b| {
            let prefix = match tree {
                Some(true) => b.t.structure("tree_end"),
                Some(false) => b.t.structure("tree_mid"),
                None => b.t.fold(open),
            };
            let head = if encrypted {
                format!("{label} · encrypted (replayed, not readable)")
            } else if let Some(seconds) = seconds {
                format!("{label} for {seconds:.1}s")
            } else {
                label.clone()
            };
            b.line(vec![Span::styled(format!("{prefix} {head}"), st)]);
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
        tree: Option<bool>,
    ) {
        let open = self.is_open(key, false);
        let raw = self.is_open(&format!("{key}:raw"), false);
        let done = results.get(call_id);
        let trace = traces.get(call_id);
        let is_waiting = waiting.contains_key(call_id);
        let denied = self.denied.contains(call_id);
        let failed = !denied
            && (done.is_some_and(|d| d.is_error) || trace.is_some_and(|t| t.error.is_some()));
        let duration = trace.and_then(|t| {
            let start = t.start?;
            let end = t.end.or_else(|| {
                self.app
                    .viewed_id()
                    .filter(|id| self.app.is_busy(id))
                    .map(|_| crate::app::now_secs())
            })?;
            Some((end - start).max(0.0))
        });
        let (arg, _) = tool_summary(name, args, done);
        let detail = self.tool_detail(name, args, done, trace, raw);
        let key = key.to_owned();
        let name = name.to_owned();
        self.block(key.clone(), BlockKind::Tool, turn, true, |b| {
            let prefix = match tree {
                Some(true) => b.t.structure("tree_end"),
                Some(false) => b.t.structure("tree_mid"),
                None => b.t.fold(open),
            };
            let verb = match name.as_str() {
                "bash" => "Shell",
                "read" => "Read",
                "edit" | "patch" => "Edit",
                "write" => "Write",
                "code" => "Code",
                "ask" => "Ask",
                "web_search" => "Search",
                "web_fetch" => "Fetch",
                _ => &name,
            };
            let mut right = Vec::new();
            if is_waiting {
                right.push(Span::styled(
                    format!("{} waiting", b.t.status("waiting").0),
                    b.st("interactive.accent", CANVAS),
                ));
            } else if denied {
                right.push(Span::styled(format!("{}denied", b.sep()), b.meta()));
            } else if failed {
                right.push(Span::styled(format!("{}failed", b.sep()), b.meta()));
            } else if let Some(duration) = duration {
                right.push(Span::styled(format!("{duration:.1}s"), b.meta()));
            }
            let right_width: usize = right.iter().map(|s| width(s.content.as_ref())).sum();
            let label = if arg.is_empty() {
                format!("{prefix} {verb}")
            } else {
                format!("{prefix} {verb} {arg}")
            };
            let room =
                b.bw.saturating_sub(right_width + usize::from(!right.is_empty()));
            b.row(
                vec![Span::styled(truncate(&label, room, b.ell()), b.meta())],
                right,
            );
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
            Span::styled(" · ", self.st(SECONDARY, PANEL)),
            Span::styled(
                agent.clone(),
                self.st(PRIMARY, PANEL).patch(self.t.agent_style(&agent)),
            ),
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

    fn markdown(&mut self, text: &str, parent_key: &str, full_text: &str) {
        let sources = crate::markdown::fence_sources(full_text);
        let mut fences: std::collections::BTreeMap<usize, (usize, usize)> =
            std::collections::BTreeMap::new();
        let palette = crate::markdown::Palette::from_theme(self.t);
        let rows = crate::markdown::lines(text, &palette, self.bw);
        if rows.is_empty() && !text.trim().is_empty() {
            self.legacy_markdown(text);
            return;
        }
        for row in rows {
            let row_start = self.out.len();
            if row.blank {
                self.blank();
                continue;
            }
            let prefix_width: usize = row
                .prefix
                .iter()
                .map(|span| crate::text::width(&span.content))
                .sum();
            let width = self.bw.saturating_sub(prefix_width).max(1);
            let parts = crate::markdown::wrap_spans(&row.spans, width);
            for (index, part) in parts.into_iter().enumerate() {
                let mut spans = if index == 0 {
                    row.prefix.clone()
                } else {
                    vec![Span::raw(" ".repeat(prefix_width))]
                };
                spans.extend(part);
                self.line(spans);
                if let Some(background) = row.bg {
                    if let Some(line) = self.out.last_mut() {
                        line.bg = Some(Style::default().bg(background));
                    }
                }
            }
            if let Some(index) = row.fence {
                let range = fences.entry(index).or_insert((row_start, self.out.len()));
                range.1 = self.out.len();
            }
        }
        for (index, (start, end)) in fences {
            if let Some(source) = sources.get(index) {
                self.blocks.push(Block {
                    key: format!("code:{parent_key}:{index}"),
                    kind: BlockKind::Code,
                    start,
                    end,
                    turn: self.turn,
                    focusable: true,
                    toggle: false,
                    default_open: None,
                    target: Some(source.clone()),
                });
            }
        }
    }

    fn legacy_markdown(&mut self, text: &str) {
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

#[cfg(test)]
mod tests {
    use super::reasoning_times;
    use crate::api::Event;

    fn event(id: i64, time: f64, kind: &str, data: serde_json::Value) -> Event {
        Event {
            id,
            cursor: None,
            seq: None,
            time,
            kind: kind.into(),
            session_id: Some("ses_test".into()),
            data,
        }
    }

    #[test]
    fn reasoning_time_comes_from_its_own_deltas() {
        let events = vec![
            event(
                1,
                10.0,
                "reasoning_delta",
                serde_json::json!({"text": "Let me "}),
            ),
            event(
                2,
                11.0,
                "reasoning_delta",
                serde_json::json!({"text": "think."}),
            ),
            event(
                3,
                13.5,
                "part_done",
                serde_json::json!({"part": {"type": "reasoning", "summary": "Let me think."}}),
            ),
            // A summary with no visible deltas has no measured duration.
            event(
                4,
                14.0,
                "part_done",
                serde_json::json!({"part": {"type": "reasoning", "summary": "Opaque."}}),
            ),
        ];
        let times = reasoning_times(&events);
        assert_eq!(times.get("Let me think."), Some(&3.5));
        assert_eq!(times.get("Opaque."), None);
    }
}

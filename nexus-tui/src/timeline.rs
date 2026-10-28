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
    /// A full-width band row (the user message, DESIGN_PLAN.md D04): the
    /// background spans the whole page width and the rail `▌` is drawn at its
    /// left edge in this style.
    pub rail: Option<Style>,
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
    /// A tool whose detail is longer than the head and tail the UI shows.
    pub clipped: bool,
}

pub struct Timeline {
    pub lines: Vec<TLine>,
    pub blocks: Vec<Block>,
    pub turns: usize,
    /// Keys of the activity groups that are live (running or waiting) now.
    pub live_groups: Vec<String>,
}

/// Frames a finished group's glyph flashes the accent (DESIGN_PLAN.md D06).
pub const FLASH_FRAMES: usize = 6;

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
        live_groups: Vec::new(),
    };
    b.run();
    Timeline {
        lines: b.out,
        blocks: b.blocks,
        turns: b.turn.map_or(0, |n| n + 1),
        live_groups: b.live_groups,
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
    live_groups: Vec<String>,
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
pub(crate) fn local_clock(timestamp: f64) -> Option<String> {
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
pub(crate) fn local_clock(_timestamp: f64) -> Option<String> {
    None
}

fn fmt_count(n: u64) -> String {
    if n >= 1000 {
        let k = n as f64 / 1000.0;
        if k >= 100.0 {
            format!("{k:.0}K")
        } else {
            format!("{k:.1}K")
        }
    } else {
        n.to_string()
    }
}

/// Lines of a tool's detail shown from each end before the middle is folded.
const DETAIL_HEAD: usize = 100;
const DETAIL_TAIL: usize = 100;
/// The same, when every tool is open (high detail).
const HIGH_HEAD: usize = 30;
const HIGH_TAIL: usize = 20;

/// Output rows kept at the end of a tool's detail (the rest folds behind
/// `↑ N earlier lines`), and command rows kept at its start.
const OUTPUT_TAIL: usize = 8;
const COMMAND_ROWS: usize = 2;

/// A tool's detail rows. `head` leading rows (the command) are never cut;
/// `tail_cap` output keeps its last `OUTPUT_TAIL` rows, anything else (a diff,
/// the raw view) keeps a head and a tail.
struct ToolDetail {
    rows: Vec<Vec<Span<'static>>>,
    head: usize,
    tail_cap: bool,
}

fn plain_rows(rows: Vec<(String, Style)>) -> Vec<Vec<Span<'static>>> {
    rows.into_iter()
        .map(|(text, style)| vec![Span::styled(text, style)])
        .collect()
}

/// Tokens estimated from a character count (about four characters each).
fn estimate(chars: usize) -> u64 {
    (chars as u64).div_ceil(4)
}

/// `0.4s`, `4.2s`, `12s`, `1m 05s`, `1h 02m` (DESIGN_PLAN.md D05); never `<1 s`.
pub(crate) fn fmt_duration(secs: f64) -> String {
    let s = if secs.is_finite() { secs.max(0.0) } else { 0.0 };
    if s < 9.95 {
        format!("{s:.1}s")
    } else if s < 59.5 {
        format!("{s:.0}s")
    } else if s < 3599.5 {
        let t = s.round() as u64;
        format!("{}m {:02}s", t / 60, t % 60)
    } else {
        let t = s as u64;
        format!("{}h {:02}m", t / 3600, (t % 3600) / 60)
    }
}

/// The right block of a user band: tags (`steer`, `queued`, `via voice`,
/// `from parent`) before `#n`, then the local time.
fn band_meta(tags: &[&str], n: usize, clock: Option<&str>, sep: &str) -> String {
    let mut parts: Vec<String> = tags.iter().map(|t| (*t).to_owned()).collect();
    parts.push(format!("#{}", n + 1));
    parts.extend(clock.map(str::to_owned));
    parts.join(sep)
}

/// Tags a user message carries. Core records none yet (steer and queue wait on
/// Step 12a), so this is empty until it does.
fn user_tags(_message: &crate::api::Message) -> Vec<&'static str> {
    Vec::new()
}

/// How a turn ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum End {
    Running,
    Done,
    Error,
    Interrupted,
    StepLimit,
}

/// Seconds the model has been reasoning in the current step: from its first
/// reasoning delta since the last boundary (text, tool, step or part end).
fn live_reasoning_secs(events: &[crate::api::Event], now: f64) -> Option<f64> {
    let mut start = None;
    for event in events {
        match event.kind.as_str() {
            "reasoning_delta" => {
                start.get_or_insert(event.time);
            }
            "part_done" | "text_delta" | "tool_start" | "step_start" => start = None,
            _ => {}
        }
    }
    start.map(|t: f64| (now - t).max(0.0))
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
        self.out.push(TLine {
            spans,
            bg: None,
            rail: None,
        });
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
            clipped: false,
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
                    if !folded {
                        self.diagnostic(e);
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
                        // `▾ Thinking… 2.4s`, the last 5 rows behind `┃`, and
                        // `│ … N earlier lines` above them (D07).
                        let muted = b.meta();
                        let italic = muted.add_modifier(Modifier::ITALIC);
                        let strong = b.st("border.strong", CANVAS);
                        let weak = b.st("border.default", CANVAS);
                        let mut head = vec![
                            Span::styled(format!("{} ", b.t.fold(true)), muted),
                            Span::styled(
                                format!("Thinking{}", b.ell()),
                                muted.add_modifier(Modifier::BOLD),
                            ),
                        ];
                        if let Some(secs) =
                            live_reasoning_secs(&view.events, crate::app::now_secs())
                        {
                            head.push(Span::styled(format!(" {}", fmt_duration(secs)), muted));
                        }
                        b.line(head);
                        let rows = text::wrap(&s.reasoning, b.bw.saturating_sub(4).max(1));
                        let hidden = rows.len().saturating_sub(5);
                        if hidden > 0 {
                            b.line(vec![
                                Span::styled(format!("  {} ", b.t.structure("rail")), weak),
                                Span::styled(format!("{} {hidden} earlier lines", b.ell()), muted),
                            ]);
                        }
                        for row in rows.into_iter().skip(hidden) {
                            b.line(vec![
                                Span::styled(format!("  {} ", b.t.structure("rail_heavy")), strong),
                                Span::styled(row, italic),
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
        use crate::paint::{CtxBody, CtxLayout, CtxRole, CtxSection};
        let Some(s) = &view.starting else { return };
        let key = format!("context:{}", view.session_id);
        // Expanded before the first turn; folded to one line once the session has content.
        let open = self.is_open(&key, no_turns);
        let sys = s["system_prompt"].as_str().unwrap_or("");
        let names: Vec<&str> = s["tools"]
            .as_array()
            .map(|v| v.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        let off = crate::app::disabled_tools(self.app.viewed());
        let schemas = &s["tool_definitions"];
        let file = &s["instruction_file"];
        let agent = &s["agent"];
        // Estimates are characters / 4 and say so; the provider's own count
        // arrives with the first request (see the turn footer).
        let sys_tokens = estimate(sys.chars().count());
        let tool_tokens = if schemas.as_object().is_some_and(|m| !m.is_empty()) {
            estimate(schemas.to_string().chars().count())
        } else {
            0
        };
        let file_tokens = file["bytes"].as_u64().map(|b| estimate(b as usize));
        // The instruction file is part of the system prompt, so the total
        // counts the prompt and the tool schemas only.
        let total = sys_tokens + tool_tokens;
        let window = self.context_window();
        let sep = self.sep();
        let first_prompt_line = sys
            .lines()
            .map(str::trim)
            .find(|l| !l.is_empty() && !l.starts_with('#'))
            .unwrap_or("")
            .to_owned();
        let agent_name = agent["name"].as_str().unwrap_or("?");
        let file_path = file["path"].as_str().unwrap_or("");
        let file_title = std::path::Path::new(file_path).file_name().map_or_else(
            || "AGENTS.md".to_owned(),
            |n| n.to_string_lossy().into_owned(),
        );
        let file_status = match file["status"].as_str() {
            Some("loaded") => "loaded".to_owned(),
            Some("missing") => "not found".to_owned(),
            Some(other) => other.replace('_', " "),
            None => "not read".to_owned(),
        };
        let mut sections = vec![CtxSection {
            title: "System prompt".into(),
            tokens: Some(sys_tokens),
            meta: format!(
                "{agent_name}{sep}{} line{}",
                sys.lines().count(),
                if sys.lines().count() == 1 { "" } else { "s" }
            ),
            body: CtxBody::Preview(first_prompt_line),
        }];
        // Status and the one-line fact first, so a narrow page clips the path.
        let mut meta = file_status;
        if file["bytes"].as_u64().is_some_and(|b| b > 0) {
            meta.push_str(&format!("{sep}in the system prompt"));
        }
        if !file_path.is_empty() {
            meta.push_str(&format!("{sep}{file_path}"));
        }
        sections.push(CtxSection {
            title: file_title,
            tokens: file_tokens,
            meta,
            body: CtxBody::None,
        });
        sections.push(CtxSection {
            title: "Tools".into(),
            tokens: (tool_tokens > 0).then_some(tool_tokens),
            meta: format!(
                "{} tools{sep}{} on",
                names.len(),
                names
                    .iter()
                    .filter(|n| !off.iter().any(|o| o == *n))
                    .count()
            ),
            body: CtxBody::Grid(
                names
                    .iter()
                    .map(|n| ((*n).to_owned(), !off.iter().any(|o| o == n)))
                    .collect(),
            ),
        });
        let children: Vec<(String, bool)> = s["child_agents"]
            .as_array()
            .map(|v| {
                v.iter()
                    .filter_map(|c| c["name"].as_str())
                    .map(|n| (n.to_owned(), true))
                    .collect()
            })
            .unwrap_or_default();
        if !children.is_empty() {
            sections.push(CtxSection {
                title: "Child agents".into(),
                tokens: None,
                meta: format!("{} agents", children.len()),
                body: CtxBody::Grid(children),
            });
        }
        if s["workspace"]["active"].as_bool().unwrap_or(false) {
            sections.push(CtxSection {
                title: "Workspace tools".into(),
                tokens: None,
                meta: "active".into(),
                body: CtxBody::None,
            });
        }
        let (title, folded) = crate::paint::context_folded(sections.len(), total, true, &sep);
        let mut header = vec![
            Span::styled(format!("{} ", self.t.fold(open)), self.st(MUTED, CANVAS)),
            Span::styled(
                title,
                self.st(SECONDARY, CANVAS).add_modifier(Modifier::BOLD),
            ),
            Span::styled(folded, self.meta()),
        ];
        if s["origin"].as_str() == Some("reconstructed") {
            header.push(Span::styled(
                sep.clone() + "reconstructed",
                self.st("status.warning", CANVAS),
            ));
        }
        self.block(key.clone(), BlockKind::Context, None, true, |b| {
            b.row(header, Vec::new());
            if !open {
                return;
            }
            let layout = CtxLayout {
                width: b.bw.min(100).saturating_sub(2),
                sep: &sep,
                ell: b.ell(),
                ascii: b.t.opts.ascii,
                estimated: true,
                window,
            };
            let rows = crate::paint::context_rows(&sections, total, &layout);
            for row in rows {
                let mut spans = vec![Span::raw("  ".to_owned())];
                for (text, role) in row {
                    let style = match role {
                        CtxRole::Mark | CtxRole::Meta | CtxRole::Preview => b.meta(),
                        CtxRole::Title | CtxRole::TotalLabel => {
                            b.primary().add_modifier(Modifier::BOLD)
                        }
                        CtxRole::Tokens | CtxRole::Item => b.secondary(),
                        CtxRole::ItemOff => b.meta().add_modifier(Modifier::CROSSED_OUT),
                        CtxRole::TotalFigure => b.primary(),
                    };
                    spans.push(Span::styled(text, style));
                }
                b.line(if spans.len() == 1 { Vec::new() } else { spans });
            }
        });
    }

    /// The viewed session's context window from the catalog, when it reports one.
    fn context_window(&self) -> Option<u64> {
        let model = self.app.effective_model(self.app.viewed());
        self.app
            .state
            .models
            .iter()
            .find(|m| m["ref"].as_str() == Some(model.as_str()))
            .and_then(|m| m["context"].as_u64())
            .filter(|&n| n > 0)
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
        let raised = "surface.raised";
        let gutter = self.st(MUTED, raised);
        // The rail is muted, and the focus colour while the band is focused.
        let rail = if self.selected() {
            self.st("interactive.focus", raised)
        } else {
            self.st(MUTED, raised)
        };
        let big = text.len() > 4096 || text.lines().count() > 40;
        let full = self.is_open(&message_key, !big);
        let body = if folded {
            first_line(text)
        } else if big && !full {
            text.lines().take(6).collect::<Vec<_>>().join("\n")
        } else {
            text.to_owned()
        };
        let sep = self.sep();
        let tags = user_tags(message);
        let clock = local_clock(message.created);
        let metadata = if folded {
            // `#2 · 3 tools · 4.2s · ✓`: the turn summary sits in the right block.
            let mut parts: Vec<String> = tags.iter().map(|t| (*t).to_owned()).collect();
            parts.push(format!("#{}", n + 1));
            if meta.tools > 0 {
                parts.push(format!(
                    "{} tool{}",
                    meta.tools,
                    if meta.tools == 1 { "" } else { "s" }
                ));
            }
            if meta.duration() >= 0.05 {
                parts.push(fmt_duration(meta.duration()));
            }
            let status: String = self
                .turn_status(&meta)
                .iter()
                .map(|s| s.content.as_ref())
                .collect();
            parts.extend(status.split(' ').next().map(str::to_owned));
            parts.join(&sep)
        } else {
            band_meta(&tags, n, clock.as_deref(), &sep)
        };
        let marker = self.t.fold(!folded).to_owned();
        self.block(key, BlockKind::Turn, Some(n), true, |b| {
            let pad = || TLine {
                spans: vec![Span::styled(" ", bg)],
                bg: Some(bg),
                rail: Some(rail),
            };
            if !folded {
                b.out.push(pad());
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
            if rows.is_empty() {
                rows.push(String::new());
            }
            for (i, row) in rows.iter().enumerate() {
                // The fold marker sits inside the band; the text starts 2 cells after it.
                let mark = if i == 0 { marker.as_str() } else { " " };
                let mut spans = vec![
                    Span::styled(" ", bg),
                    Span::styled(mark.to_owned(), gutter),
                    Span::styled(format!(" {row}"), bg),
                ];
                if i == 0 && meta_width + 3 < b.bw {
                    let padding = b.bw.saturating_sub(3 + width(row) + meta_width).max(1);
                    spans.push(Span::styled(" ".repeat(padding), bg));
                    spans.push(Span::styled(metadata.clone(), b.st(MUTED, raised)));
                }
                b.out.push(TLine {
                    spans,
                    bg: Some(bg),
                    rail: Some(rail),
                });
            }
            if !folded {
                b.out.push(pad());
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

    fn turn_end(&self, meta: &TurnMeta) -> End {
        if meta.running {
            return End::Running;
        }
        if let Some((kind, _)) = &meta.error {
            if kind != "interrupted" {
                return End::Error;
            }
        }
        match meta.stop.as_deref() {
            Some("max_steps") => End::StepLimit,
            Some(_) if meta.error.is_none() => End::Done,
            _ if meta.end_event || meta.followed => End::Done,
            _ => End::Interrupted,
        }
    }

    /// The end status of a turn as glyph and word. Only the folded user band
    /// uses it (as the glyph); the footer never says how the turn ended (D05).
    fn turn_status(&self, meta: &TurnMeta) -> Vec<Span<'static>> {
        let status = |name: &str| {
            let (g, l, tok) = self.t.status(name);
            vec![Span::styled(format!("{g} {l}"), self.status_style(tok))]
        };
        match self.turn_end(meta) {
            End::Running => vec![
                Span::styled(
                    format!("{} ", self.t.spinner(self.frame / 2)),
                    self.st(PRIMARY, CANVAS),
                ),
                Span::styled("running".to_string(), self.primary()),
            ],
            End::Error => status("error"),
            End::StepLimit => {
                let (g, _, tok) = self.t.status("interrupted");
                vec![Span::styled(
                    format!("{g} step limit"),
                    self.status_style(tok),
                )]
            }
            End::Done => status("done"),
            End::Interrupted => status("interrupted"),
        }
    }

    /// The turn's price from the catalog (USD per million tokens), when the
    /// model is listed with prices: uncached input, cached input and output
    /// (reasoning tokens are billed as output and already counted there).
    fn turn_cost(&self, meta: &TurnMeta) -> Option<String> {
        let used = meta.models.last()?;
        let entry = crate::stats::catalog_entry(&self.app.state.models, used)?;
        let price = crate::stats::price_of(entry)?;
        Some(crate::stats::money(crate::stats::cost(
            price,
            meta.input,
            meta.cached,
            meta.output,
        )))
    }

    /// The turn footer (DESIGN_PLAN.md D05): right-aligned
    /// `▪ Agent · models · effort · tools · duration · tokens · cost`, with no
    /// end status. An interrupted turn adds the `/continue` hint after it, a
    /// failed turn puts the first line of its error on the row above.
    fn turn_footer(&mut self, n: usize, turns: &[TurnMeta]) {
        let Some(meta) = turns.get(n) else { return };
        let end = self.turn_end(meta);
        if end == End::Running {
            return;
        }
        let session = self.app.viewed();
        let mut bits: Vec<String> = Vec::new();
        if !meta.models.is_empty() {
            let name = |used: &String| {
                crate::stats::catalog_entry(&self.app.state.models, used)
                    .and_then(|m| m["name"].as_str())
                    .filter(|n| !n.is_empty())
                    .map_or_else(|| used.clone(), str::to_owned)
            };
            let arrow = format!(" {} ", self.t.structure("arrow"));
            bits.push(
                meta.models
                    .iter()
                    .map(name)
                    .collect::<Vec<_>>()
                    .join(&arrow),
            );
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
        if meta.tools > 0 {
            bits.push(format!(
                "{} tool{}",
                meta.tools,
                if meta.tools == 1 { "" } else { "s" }
            ));
        }
        bits.push(fmt_duration(meta.duration()));
        if meta.has_usage() {
            bits.push(format!("{} in", fmt_count(meta.input)));
            if meta.cached > 0 {
                bits.push(format!("{} cached", fmt_count(meta.cached)));
            }
            bits.push(format!("{} out", fmt_count(meta.output)));
            if meta.reasoning > 0 {
                bits.push(format!("{} reasoning", fmt_count(meta.reasoning)));
            }
            if let Some(cost) = self.turn_cost(meta) {
                bits.push(cost);
            }
        }
        if matches!(end, End::Interrupted | End::StepLimit) {
            bits.push(if self.app.term("composer.empty").unwrap_or(false) {
                "enter continue".to_owned()
            } else {
                "/continue".to_owned()
            });
        }
        let agent = session.map(|s| s.agent.clone()).unwrap_or_default();
        let label: String = agent
            .chars()
            .next()
            .map(|c| c.to_uppercase().collect::<String>() + &agent[c.len_utf8()..])
            .unwrap_or_default();
        let glyph = if self.t.opts.ascii { "*" } else { "\u{25aa}" };
        let sep = self.sep();
        let sep_w = width(&sep);

        // Break the segments from the left onto extra right-aligned rows.
        let mut rows: Vec<Vec<Span<'static>>> = vec![Vec::new()];
        let mut used = 0usize;
        if !label.is_empty() {
            rows[0].push(Span::styled(
                format!("{glyph} "),
                self.st(MUTED, CANVAS).patch(self.t.agent_style(&agent)),
            ));
            rows[0].push(Span::styled(
                label.clone(),
                self.primary().add_modifier(Modifier::BOLD),
            ));
            used = width(glyph) + 1 + width(&label);
        }
        for bit in bits {
            let w = width(&bit);
            let row = rows.last_mut().expect("a row");
            if row.is_empty() {
                row.push(Span::styled(bit, self.meta()));
                used = w;
            } else if used + sep_w + w <= self.bw {
                row.push(Span::styled(sep.clone(), self.meta()));
                row.push(Span::styled(bit, self.meta()));
                used += sep_w + w;
            } else {
                rows.push(vec![Span::styled(bit, self.meta())]);
                used = w;
            }
        }
        if end == End::Error {
            let msg = meta
                .error
                .as_ref()
                .map(|(kind, message)| {
                    let line = first_line(message);
                    if line.is_empty() {
                        kind.clone()
                    } else {
                        line
                    }
                })
                .unwrap_or_default();
            let text = format!("{} {msg}", self.t.status("error").0);
            let text = truncate(&text, self.bw, self.ell());
            let pad = self.bw.saturating_sub(width(&text));
            self.line(vec![
                Span::raw(" ".repeat(pad)),
                Span::styled(text, self.status_style("status.error")),
            ]);
        }
        for mut spans in rows {
            let used: usize = spans.iter().map(|s| width(s.content.as_ref())).sum();
            spans.insert(0, Span::raw(" ".repeat(self.bw.saturating_sub(used))));
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
                rail: None,
            });
            for row in text::wrap(text, b.bw.saturating_sub(2)) {
                b.out.push(TLine {
                    spans: vec![
                        Span::styled(heavy.clone(), rail),
                        Span::styled(format!(" {row}"), st),
                    ],
                    bg: Some(bgst),
                    rail: None,
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
        // A lone thought is its own `▸ Thought for 2.1s · preview` row (D07).
        if let [Activity::Thought(m, i, p)] = activity.as_slice() {
            let secs = self.thoughts.get(&json_str(&p["summary"])).copied();
            self.reasoning_block(m, *i, p, None, secs);
            activity.clear();
            return;
        }
        let first = match &activity[0] {
            Activity::Thought(m, i, _) => format!("reason:{}:{i}", m.id),
            Activity::Tool { id, .. } => format!("tool:{id}"),
        };
        let key = format!("group:{first}");
        let flash = !self.t.opts.reduced_motion
            && self
                .app
                .ui
                .group_flash
                .get(&key)
                .is_some_and(|at| self.frame.wrapping_sub(*at) < FLASH_FRAMES);
        let has_wait = activity
            .iter()
            .any(|a| matches!(a, Activity::Tool { id, .. } if waiting.contains_key(id)));
        let running = has_wait || (busy && activity.iter().any(|a| matches!(a, Activity::Tool { id, .. } if !results.contains_key(id) && !traces.get(id).is_some_and(|t| t.end.is_some()))));
        if running {
            self.live_groups.push(key.clone());
        }
        let default_open = running || has_wait || self.app.ui.detail != crate::ui::Detail::Low;
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
        // Verb labels in first-seen order, with their counts.
        let mut order: Vec<&str> = Vec::new();
        let mut counts = std::collections::BTreeMap::<&str, usize>::new();
        let mut unknown = std::collections::BTreeSet::<&str>::new();
        let mut thought_secs = 0.0_f64;
        let mut failed = 0;
        let mut denied_count = 0;
        let (mut added, mut removed, mut edits) = (0usize, 0usize, false);
        for item in activity.iter() {
            let label = match item {
                Activity::Thought(_, _, p) => {
                    if let Some(secs) = self.thoughts.get(&json_str(&p["summary"])) {
                        thought_secs += *secs;
                    }
                    "Thought"
                }
                Activity::Tool { id, name, args } => {
                    if let Some(trace) = traces.get(id) {
                        if let Some(start) = trace.start {
                            starts.push(start);
                        }
                        if let Some(end) = trace.end {
                            ends.push(end);
                        }
                    }
                    let failure = results.get(id).is_some_and(|d| d.is_error)
                        || traces.get(id).is_some_and(|t| t.error.is_some());
                    if self.denied.contains(id) {
                        denied_count += 1;
                    } else if failure {
                        failed += 1;
                    } else if matches!(name.as_str(), "edit" | "patch") {
                        if let Some((a, r)) = diff_counts(args) {
                            added += a;
                            removed += r;
                            edits = true;
                        }
                    }
                    match name.as_str() {
                        "bash" => "Ran command",
                        "read" => "Read file",
                        "edit" => "Edited file",
                        "write" => "Wrote file",
                        "code" => "Ran cell",
                        "ask" => "Asked question",
                        "web_search" => "Searched time",
                        "web_fetch" => "Fetched page",
                        "session" => "Started session",
                        _ => {
                            unknown.insert(name.as_str());
                            "Used tool"
                        }
                    }
                }
            };
            if !counts.contains_key(label) {
                order.push(label);
            }
            *counts.entry(label).or_default() += 1;
        }
        let summary = if running {
            "Working".to_owned()
        } else {
            order
                .iter()
                .map(|label| {
                    let count = counts[label];
                    if *label == "Thought" {
                        return if thought_secs > 0.0 {
                            format!("Thought for {}", fmt_duration(thought_secs))
                        } else {
                            "Thought".to_owned()
                        };
                    }
                    if *label == "Used tool" && unknown.len() == 1 {
                        return format!("Used {}", unknown.iter().next().unwrap());
                    }
                    let (verb, noun) = label.rsplit_once(' ').unwrap_or((label, ""));
                    format!("{verb} {count} {noun}{}", if count == 1 { "" } else { "s" })
                })
                .enumerate()
                // `Read 2 files, ran 1 command`: only the first verb is capitalised.
                .map(|(i, piece)| {
                    let mut chars = piece.chars();
                    match chars.next() {
                        Some(c) if i > 0 => c.to_lowercase().collect::<String>() + chars.as_str(),
                        _ => piece,
                    }
                })
                .collect::<Vec<_>>()
                .join(", ")
        };
        let duration = if running {
            starts
                .iter()
                .copied()
                .reduce(f64::min)
                .map(|start| (crate::app::now_secs() - start).max(0.0))
        } else if !starts.is_empty() && !ends.is_empty() {
            let start = starts.into_iter().fold(f64::INFINITY, f64::min);
            let end = ends.into_iter().fold(f64::NEG_INFINITY, f64::max);
            (end >= start).then_some(end - start)
        } else {
            None
        };
        let agent = self
            .app
            .viewed()
            .map(|s| s.agent.clone())
            .unwrap_or_default();
        self.block(key, BlockKind::Group, self.turn, !has_wait, |b| {
            let marker = Span::styled(
                format!("{} ", b.t.fold(open)),
                if flash {
                    b.st(ACCENT, CANVAS).add_modifier(Modifier::BOLD)
                } else {
                    b.meta()
                },
            );
            // Edit counts and failures stay whole: a narrow row shortens the
            // summary, never the `✕ 1 failed` that must be visible when folded.
            let mut tail: Vec<Span<'static>> = Vec::new();
            if edits {
                tail.push(Span::styled(
                    format!("  +{added}"),
                    b.st("diff.add", CANVAS),
                ));
                tail.push(Span::styled(
                    format!(
                        " {}{removed}",
                        if b.t.opts.ascii { "-" } else { "\u{2212}" }
                    ),
                    b.st("diff.del", CANVAS),
                ));
            }
            if denied_count > 0 {
                tail.push(Span::styled(
                    format!("{}{} denied", b.sep(), denied_count),
                    b.meta(),
                ));
            }
            if failed > 0 {
                tail.push(Span::styled(
                    format!("{}{} {failed} failed", b.sep(), b.t.status("error").0),
                    b.st("status.error", CANVAS),
                ));
            }
            let right = duration
                .map(|d| vec![Span::styled(fmt_duration(d), b.meta())])
                .unwrap_or_default();
            let fixed: usize = [&marker]
                .into_iter()
                .chain(tail.iter())
                .chain(right.iter())
                .map(|s| width(s.content.as_ref()))
                .sum();
            let room = b.bw.saturating_sub(fixed + 1);
            let shown = if width(&summary) > room && !running {
                truncate(&summary, room.max(8), b.ell())
            } else {
                summary.clone()
            };
            let mut left = vec![marker];
            if running && !b.t.opts.reduced_motion && !b.t.mono() {
                left.extend(crate::theme::shimmer(
                    &shown,
                    b.frame,
                    true,
                    b.meta(),
                    b.t.agent_style(&agent),
                ));
            } else {
                left.push(Span::styled(
                    shown,
                    if running { b.primary() } else { b.meta() },
                ));
            }
            left.extend(tail);
            b.row(left, right);
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
                    format!("  {} {skipped} earlier", self.ell()),
                    self.meta(),
                )]);
            }
            let _ = tree_mode;
            for (index, item) in activity.iter().enumerate().skip(skipped) {
                let tree = Some(index + 1 == activity.len());
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

    /// `▸ Thought for 2.1s · preview` (D07). Open: the summary in italics
    /// behind a `│` rail. Encrypted: labelled, with no expansion.
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
        let open = !encrypted && self.is_open(&key, false);
        let label = self.t.role("reasoning").label.clone();
        let muted = self.meta();
        let italic = muted.add_modifier(Modifier::ITALIC);
        let tree_style = self.st("border.default", CANVAS);
        let rail = self.st("border.default", CANVAS);
        let vbar = self.t.structure("rail").to_owned();
        self.block(key, BlockKind::Reasoning, self.turn, !encrypted, |b| {
            let prefix = match tree {
                Some(true) => format!("  {} ", b.t.structure("tree_end")),
                Some(false) => format!("  {} ", b.t.structure("tree_mid")),
                None => format!("{} ", b.t.fold(open)),
            };
            let mut head = vec![
                Span::styled(
                    prefix.clone(),
                    if tree.is_some() { tree_style } else { muted },
                ),
                Span::styled(label.clone(), muted.add_modifier(Modifier::BOLD)),
            ];
            if let Some(seconds) = seconds {
                head.push(Span::styled(
                    format!(" for {}", fmt_duration(seconds)),
                    muted,
                ));
            }
            if !open {
                let preview = if encrypted {
                    "encrypted, replayed to the provider".to_owned()
                } else {
                    first_line(&summary)
                };
                if !preview.is_empty() {
                    let used: usize = head.iter().map(|s| width(s.content.as_ref())).sum();
                    let sep = b.sep();
                    let room = b.bw.saturating_sub(used + width(&sep));
                    if room > 3 {
                        head.push(Span::styled(sep, muted));
                        head.push(Span::styled(truncate(&preview, room, b.ell()), italic));
                    }
                }
            }
            b.line(head);
            if open {
                let indent = if tree.is_some() { "    " } else { "  " };
                let room = b.bw.saturating_sub(indent.len() + 2).max(1);
                for row in text::wrap(&summary, room) {
                    b.line(vec![
                        Span::styled(format!("{indent}{vbar} "), rail),
                        Span::styled(row, italic),
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
        let open = self.is_open(key, self.app.ui.detail == crate::ui::Detail::High);
        let raw = self.is_open(&format!("{key}:raw"), false);
        let done = results.get(call_id);
        let trace = traces.get(call_id);
        let is_waiting = waiting.contains_key(call_id);
        let denied = self.denied.contains(call_id);
        let failed = !denied
            && (done.is_some_and(|d| d.is_error) || trace.is_some_and(|t| t.error.is_some()));
        let busy = self.app.viewed_id().is_some_and(|id| self.app.is_busy(id));
        let running = busy && done.is_none() && !trace.is_some_and(|t| t.end.is_some());
        let duration = trace.and_then(|t| {
            let start = t.start?;
            let end = t.end.or_else(|| busy.then(crate::app::now_secs))?;
            Some((end - start).max(0.0))
        });
        let (arg, summary) = tool_summary(name, args, done);
        let exit_code = (name == "bash")
            .then(|| done.and_then(|d| bash_exit_code(&d.output)))
            .flatten()
            .filter(|code| *code != 0);
        let mut detail = self.tool_detail(name, args, done, trace, raw);
        // Output keeps its command rows and its last rows; a diff or the raw
        // view keeps a head and a tail. The marker says how much it hides and
        // `o` (or Enter on the open row) brings it back. Clipping here is the
        // UI's, never the model's.
        let all = self.is_open(&format!("{key}:all"), false);
        let up = if self.t.opts.ascii { "^" } else { "\u{2191}" };
        let clipped;
        if detail.tail_cap {
            let out_rows = detail.rows.len() - detail.head;
            clipped = out_rows > OUTPUT_TAIL + 2;
            if clipped && !all {
                let hidden = out_rows - OUTPUT_TAIL;
                let tail = detail.rows.split_off(detail.rows.len() - OUTPUT_TAIL);
                detail.rows.truncate(detail.head);
                detail.rows.push(vec![Span::styled(
                    format!("{up} {hidden} earlier lines · enter show all"),
                    self.meta(),
                )]);
                detail.rows.extend(tail);
            }
        } else {
            // High detail opens every tool, so it keeps each one short.
            let (head, tail_rows) = if self.app.ui.detail == crate::ui::Detail::High {
                (HIGH_HEAD, HIGH_TAIL)
            } else {
                (DETAIL_HEAD, DETAIL_TAIL)
            };
            clipped = detail.rows.len() > head + tail_rows + 20;
            if clipped && !all {
                let hidden = detail.rows.len() - head - tail_rows;
                let tail = detail.rows.split_off(detail.rows.len() - tail_rows);
                detail.rows.truncate(head);
                detail.rows.push(vec![Span::styled(
                    format!("{} {hidden} lines hidden · show all (o)", self.ell()),
                    self.st(ACCENT, CANVAS),
                )]);
                detail.rows.extend(tail);
            }
        }
        if clipped && all {
            let n = detail.rows.len() - detail.head;
            detail.rows.insert(
                detail.head,
                vec![Span::styled(
                    format!("{up} showing all {n} lines · o show less"),
                    self.meta(),
                )],
            );
        }
        let rows = detail.rows;
        let key = key.to_owned();
        let name = name.to_owned();
        let denied_word = self.t.status("denied").0.to_owned();
        self.block(key.clone(), BlockKind::Tool, turn, true, |b| {
            let strong = b.st("border.strong", CANVAS);
            let ascii = b.t.opts.ascii;
            let box_open = open && !rows.is_empty();
            let (prefix, prefix_style) = if box_open {
                (if ascii { "+" } else { "\u{250c}" }.to_owned(), strong)
            } else {
                let glyph = match tree {
                    Some(true) => b.t.structure("tree_end"),
                    Some(false) => b.t.structure("tree_mid"),
                    None => b.t.fold(open),
                };
                (glyph.to_owned(), b.st("border.default", CANVAS))
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
            // The status glyph replaces the verb glyph for running, error,
            // denied and waiting.
            let (glyph, glyph_style) = if is_waiting {
                (
                    b.t.status("waiting").0.to_owned(),
                    b.st("interactive.accent", CANVAS),
                )
            } else if denied {
                (denied_word.clone(), b.st("status.error", CANVAS))
            } else if failed {
                (
                    b.t.status("error").0.to_owned(),
                    b.st("status.error", CANVAS),
                )
            } else if running {
                (b.t.spinner(b.frame / 2).to_owned(), b.st(PRIMARY, CANVAS))
            } else {
                (crate::paint::verb_glyph(&name, ascii).to_owned(), b.meta())
            };
            let mut left = vec![
                Span::styled(format!("  {prefix} "), prefix_style),
                Span::styled(format!("{glyph} "), glyph_style),
                Span::styled(verb.to_owned(), b.primary().add_modifier(Modifier::BOLD)),
            ];
            if !arg.is_empty() {
                left.push(Span::styled(format!("  {arg}"), b.secondary()));
            }
            if failed {
                if let Some(line) = done
                    .map(|d| first_line(&d.output))
                    .filter(|l| !l.is_empty())
                {
                    left.push(Span::styled(
                        format!("  {line}"),
                        b.st("status.error", CANVAS),
                    ));
                }
            } else if let Some(parts) = summary.filter(|_| !is_waiting && !denied && name != "bash")
            {
                for (i, (text, _)) in parts.iter().enumerate() {
                    let token = match (name.as_str(), i) {
                        ("edit" | "patch", 0) => "diff.add",
                        ("edit" | "patch", _) => "diff.del",
                        ("write", _) => "diff.add",
                        _ => MUTED,
                    };
                    let text = if ascii {
                        text.replace('\u{2212}', "-")
                    } else {
                        text.clone()
                    };
                    left.push(Span::styled(
                        format!("{}{text}", if i == 0 { "  " } else { "" }),
                        b.st(token, CANVAS),
                    ));
                }
            }
            let mut right = Vec::new();
            if open {
                right.push(Span::styled(
                    "raw",
                    if raw {
                        b.st(ACCENT, CANVAS).add_modifier(Modifier::BOLD)
                    } else {
                        b.meta()
                    },
                ));
            }
            let mut tail = Vec::new();
            if is_waiting {
                tail.push(Span::styled("waiting", b.st("interactive.accent", CANVAS)));
            } else if denied {
                tail.push(Span::styled("denied", b.st("status.error", CANVAS)));
            } else if let Some(duration) = duration {
                tail.push(Span::styled(fmt_duration(duration), b.meta()));
            }
            if let Some(code) = exit_code {
                if !tail.is_empty() {
                    tail.insert(0, Span::styled(b.sep(), b.meta()));
                }
                tail.insert(
                    0,
                    Span::styled(format!("exit {code}"), b.st("status.error", CANVAS)),
                );
            }
            if !right.is_empty() && !tail.is_empty() {
                right.push(Span::raw("  "));
            }
            right.extend(tail);
            b.row(left, right);
            if box_open {
                let vbar = b.t.structure("rail").to_owned();
                let corner = if ascii {
                    "`".to_owned()
                } else {
                    "\u{2570}".to_owned()
                };
                let last = rows.len() - 1;
                for (i, row) in rows.iter().enumerate() {
                    let rail = if i == last { &corner } else { &vbar };
                    let mut spans = vec![Span::styled(format!("  {rail} "), strong)];
                    spans.extend(row.iter().cloned());
                    b.line(spans);
                }
            }
        });
        if clipped {
            if let Some(block) = self.blocks.last_mut() {
                block.clipped = true;
            }
        }
    }

    fn tool_detail(
        &self,
        name: &str,
        args: &str,
        done: Option<&Done>,
        trace: Option<&ToolTrace>,
        raw: bool,
    ) -> ToolDetail {
        let mut out = Vec::new();
        let mut head = 0;
        let mut tail_cap = false;
        if raw {
            for line in args.lines() {
                out.push((line.to_owned(), self.secondary()));
            }
            if let Some(d) = done {
                for line in d.output.lines() {
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
            return ToolDetail {
                rows: plain_rows(out),
                head,
                tail_cap,
            };
        }
        let a = arg_json(args).unwrap_or(Value::Null);
        match name {
            "edit" | "patch" => {
                let diff = if let Some(patch) = a["patch"].as_str() {
                    crate::diff::Diff::from_patch(patch)
                } else {
                    let old = a["old_string"]
                        .as_str()
                        .or_else(|| a["old"].as_str())
                        .unwrap_or("");
                    let new = a["new_string"]
                        .as_str()
                        .or_else(|| a["new"].as_str())
                        .unwrap_or("");
                    crate::diff::Diff::from_replace(old, new)
                };
                return self.diff_detail(&diff);
            }
            "write" => {
                let diff = crate::diff::Diff::from_new(a["content"].as_str().unwrap_or(""));
                return self.diff_detail(&diff);
            }
            _ => {
                tail_cap = true;
                // A command shows its first two rows (`$ cmd`, then a
                // continuation) ahead of its output.
                let command = match name {
                    "bash" => a["command"].as_str(),
                    "code" => a["code"].as_str(),
                    _ => None,
                };
                if let Some(command) = command {
                    let lines: Vec<&str> = command.lines().collect();
                    for (i, line) in lines.iter().take(COMMAND_ROWS).enumerate() {
                        let more = i + 1 == COMMAND_ROWS && lines.len() > COMMAND_ROWS;
                        let lead = if i == 0 && name == "bash" { "$ " } else { "  " };
                        out.push((
                            format!("{lead}{line}{}", if more { " …" } else { "" }),
                            self.primary(),
                        ));
                    }
                    head = out.len();
                }
                if let Some(d) = done {
                    for line in d.output.lines() {
                        out.push((
                            line.to_string(),
                            if d.is_error {
                                self.st("status.error", CANVAS)
                            } else {
                                self.secondary()
                            },
                        ));
                    }
                } else if let Some(err) = trace.and_then(|t| t.error.clone()) {
                    out.push((err, self.st("status.error", CANVAS)));
                } else {
                    out.push(("no output".into(), self.meta()));
                }
            }
        }
        ToolDetail {
            rows: plain_rows(out),
            head,
            tail_cap,
        }
    }

    /// A diff as detail rows: split at 110 cells and wider, unified below
    /// (D09). The rail and its gap take 4 cells of the block.
    fn diff_detail(&self, diff: &crate::diff::Diff) -> ToolDetail {
        let look = crate::diff::Look::from_theme(self.t);
        ToolDetail {
            rows: diff.render(self.bw.saturating_sub(4).max(8), &look),
            head: 0,
            tail_cap: false,
        }
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
                    rail: None,
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
                            rail: None,
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
                    "provider retry {}/{} in {:.1}s",
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
        // Detail the head already says (`codex: HTTP 429` / `HTTP 429`) is noise.
        let detail = detail.filter(|d| !text.contains(d.as_str()));
        // A run of diagnostics (a retry sequence) reads as one stack: no
        // blank line between them.
        if self
            .blocks
            .last()
            .is_some_and(|b| b.kind == BlockKind::Diagnostic && b.end + 1 == self.out.len())
            && self.out.last().is_some_and(|l| l.spans.is_empty())
        {
            self.out.pop();
        }
        let key = format!("diag:{}", e.id);
        let (glyph, _label, token) = match level {
            "error" => self.t.status("error"),
            "warning" => self.t.status("interrupted"),
            _ => ("i", "info", "status.info"),
        };
        // Old Nexus look: one plain line, indented like body text, the glyph
        // in the level's colour; errors read in the error colour, retries stay
        // quiet. The detail follows on the same line.
        let text_style = if level == "error" {
            self.st(token, CANVAS)
        } else {
            self.secondary()
        };
        let glyph_style = self.st(token, CANVAS);
        let meta = self.meta();
        let sep = self.sep();
        self.block(key, BlockKind::Diagnostic, self.turn, false, |b| {
            let mut spans = vec![
                Span::styled(format!("  {glyph} "), glyph_style),
                Span::styled(text.clone(), text_style),
            ];
            if let Some(detail) = &detail {
                spans.push(Span::styled(format!("{sep}{detail}"), meta));
            }
            let parts = crate::markdown::wrap_spans(&spans, b.bw.max(8));
            for (i, mut part) in parts.into_iter().enumerate() {
                if i > 0 {
                    part.insert(0, Span::raw("    "));
                }
                b.line(part);
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
                    clipped: false,
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

/// The code from a bash result's `[exit code N]` status line (after an optional cwd line).
fn bash_exit_code(output: &str) -> Option<i32> {
    output.lines().take(2).find_map(|line| {
        line.strip_prefix("[exit code ")?
            .split(']')
            .next()?
            .parse()
            .ok()
    })
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
    use super::{band_meta, fmt_duration, live_reasoning_secs, reasoning_times};
    use crate::api::Event;

    #[test]
    fn durations_are_never_below_a_tenth_and_never_say_less_than_one() {
        for (secs, want) in [
            (0.0, "0.0s"),
            (0.4, "0.4s"),
            (0.96, "1.0s"),
            (4.2, "4.2s"),
            (9.94, "9.9s"),
            (9.96, "10s"),
            (12.0, "12s"),
            (59.4, "59s"),
            (59.6, "1m 00s"),
            (65.0, "1m 05s"),
            (125.4, "2m 05s"),
            (3599.0, "59m 59s"),
            (3720.0, "1h 02m"),
            (36000.0, "10h 00m"),
            (-3.0, "0.0s"),
            (f64::NAN, "0.0s"),
        ] {
            assert_eq!(fmt_duration(secs), want, "{secs}");
        }
        assert!(!fmt_duration(0.01).contains('<'));
    }

    #[test]
    fn tags_go_before_the_turn_number_and_time() {
        let sep = " · ";
        assert_eq!(band_meta(&[], 1, Some("3:44 PM"), sep), "#2 · 3:44 PM");
        assert_eq!(
            band_meta(&["steer"], 2, Some("3:46 PM"), sep),
            "steer · #3 · 3:46 PM"
        );
        assert_eq!(band_meta(&["queued"], 3, None, sep), "queued · #4");
    }

    #[test]
    fn live_thinking_time_starts_at_the_first_delta_of_the_step() {
        let ev = |id, time, kind: &str| Event {
            id,
            cursor: None,
            seq: None,
            time,
            kind: kind.into(),
            session_id: None,
            data: serde_json::Value::Null,
        };
        let events = vec![
            ev(1, 10.0, "reasoning_delta"),
            ev(2, 11.0, "part_done"),
            ev(3, 20.0, "reasoning_delta"),
            ev(4, 21.0, "reasoning_delta"),
        ];
        let secs = live_reasoning_secs(&events, 22.4).unwrap();
        assert!((secs - 2.4).abs() < 1e-9, "{secs}");
        assert_eq!(live_reasoning_secs(&events[..2], 30.0), None);
    }

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

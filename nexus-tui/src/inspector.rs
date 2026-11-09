//! The native session inspector (read-only), Inspector API v2.
//!
//! State is kept per (inspected session, view) so selection, expanded details,
//! scroll, zoom, filters and the cursor stack survive refreshes and a trip to
//! another view or session. Every request carries a sequence number
//! (`generation`); a response that is not the latest is ignored.
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

pub const VIEWS: [&str; 6] = [
    "Timing",
    "Conversation",
    "Steps",
    "Requests",
    "Raw",
    "Questions",
];
pub const LIMIT: usize = 50;
/// Rows drawn at once; the rest scroll in as the selection moves.
pub const WINDOW: usize = 60;
const DETAIL_LINES: usize = 120;
static GENERATION: AtomicU64 = AtomicU64::new(0);
pub fn generation() -> u64 {
    GENERATION.fetch_add(1, Ordering::Relaxed) + 1
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Field {
    Q,
    Type,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Filters {
    pub q: String,
    pub kind: String,
    pub status: String,
    pub types: String,
}
impl Filters {
    pub fn any(&self) -> bool {
        !(self.q.is_empty()
            && self.kind.is_empty()
            && self.status.is_empty()
            && self.types.is_empty())
    }
}

/// Values the `kind` filter cycles through for a view.
pub fn kind_options(view: usize) -> &'static [&'static str] {
    match view {
        0 => &["turn", "model", "tool", "attempt", "terminal"],
        2 => &["turn", "model", "tool", "attempt"],
        1 => &["user", "assistant"],
        _ => &[],
    }
}
/// Values the `status` filter cycles through for a view.
pub fn status_options(view: usize) -> &'static [&'static str] {
    match view {
        0 => &[
            "running",
            "completed",
            "failed",
            "interrupted",
            "exited",
            "closed",
            "ended",
        ],
        2 => &["running", "completed", "failed", "interrupted"],
        5 => &["pending", "replied"],
        _ => &[],
    }
}
pub fn supports_type(view: usize) -> bool {
    matches!(view, 3 | 4)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Page {
    pub filters: Filters,
    /// Cursors of the older pages walked so far; empty = the newest page.
    pub stack: Vec<String>,
    pub selected: Option<String>,
    pub expanded: BTreeSet<String>,
    /// Full records loaded on demand, pretty JSON by row id.
    pub full: BTreeMap<String, String>,
    pub top: usize,
    pub zoom: u32,
    /// Pan in thousandths of the scrollable range.
    pub pan: u32,
    pub data: Value,
    /// Client clock when the data arrived, in milliseconds.
    pub fetched: Option<u64>,
}
impl Default for Page {
    fn default() -> Self {
        Self {
            filters: Filters::default(),
            stack: Vec::new(),
            selected: None,
            expanded: BTreeSet::new(),
            full: BTreeMap::new(),
            top: 0,
            zoom: 1,
            pan: 0,
            data: Value::Null,
            fetched: None,
        }
    }
}

/// What the app must do after an inspector key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Act {
    None,
    Fetch,
    Record {
        kind: String,
        id: String,
        row: String,
    },
    Copy(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Inspector {
    /// The chat session the inspector was opened from.
    pub session: String,
    /// The tree member being inspected (equals `session` at first).
    pub target: String,
    pub view: usize,
    pub pages: BTreeMap<(String, usize), Page>,
    pub generation: u64,
    pub loading: bool,
    pub loading_full: Option<String>,
    pub error: String,
    pub paused: bool,
    pub connected: bool,
    pub head_inflight: bool,
    /// Head moved while an older page was shown or the view was paused.
    pub newer: bool,
    pub info: bool,
    pub edit: Option<(Field, String)>,
    pub polled: Option<Instant>,
    pub dirty_at: Option<Instant>,
}

fn n_text(v: &Value) -> String {
    v.as_u64().map_or_else(
        || "unknown".into(),
        |n| {
            let digits = n.to_string();
            let mut out = String::new();
            for (i, ch) in digits.chars().enumerate() {
                if i > 0 && (digits.len() - i).is_multiple_of(3) {
                    out.push(',');
                }
                out.push(ch);
            }
            out
        },
    )
}
fn pct(v: &Value) -> String {
    v.as_f64().map_or_else(
        || "unknown".into(),
        |v| format!("{}%", format!("{v:.1}").trim_end_matches(".0")),
    )
}
pub fn age(seconds: f64) -> String {
    let s = seconds.max(0.0) as u64;
    match s {
        0..=59 => format!("{s}s ago"),
        60..=3599 => format!("{}m ago", s / 60),
        _ => format!("{}h ago", s / 3600),
    }
}
fn id_of(v: &Value) -> String {
    match &v["id"] {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}
fn clip(text: &str, width: usize) -> String {
    if text.chars().count() <= width {
        text.to_owned()
    } else {
        let mut out: String = text.chars().take(width.saturating_sub(1)).collect();
        out.push('…');
        out
    }
}
fn bytes(n: u64) -> String {
    if n >= 1024 * 1024 {
        format!("{:.1} MB", n as f64 / 1048576.0)
    } else if n >= 1024 {
        format!("{:.1} KB", n as f64 / 1024.0)
    } else {
        format!("{n} B")
    }
}

/// The rows to draw out of `count`, centred on `index`.
pub fn window(index: usize, count: usize) -> std::ops::Range<usize> {
    if count <= WINDOW {
        return 0..count;
    }
    let start = index.saturating_sub(WINDOW / 2).min(count - WINDOW);
    start..start + WINDOW
}

/// The visible time range for a zoom and pan over `[origin, origin + total]`.
pub fn time_window(origin: f64, total: f64, zoom: u32, pan: u32) -> (f64, f64) {
    let span = total / zoom.max(1) as f64;
    (origin + (total - span) * pan as f64 / 1000.0, span)
}

impl Inspector {
    pub fn new(session: String) -> Self {
        Self {
            target: session.clone(),
            session,
            view: 0,
            pages: BTreeMap::new(),
            generation: generation(),
            loading: true,
            loading_full: None,
            error: String::new(),
            paused: false,
            connected: true,
            head_inflight: false,
            newer: false,
            info: false,
            edit: None,
            polled: None,
            dirty_at: None,
        }
    }
    pub fn page(&self) -> Page {
        self.pages
            .get(&(self.target.clone(), self.view))
            .cloned()
            .unwrap_or_default()
    }
    pub fn page_mut(&mut self) -> &mut Page {
        self.pages
            .entry((self.target.clone(), self.view))
            .or_default()
    }
    pub fn data(&self) -> &Value {
        self.pages
            .get(&(self.target.clone(), self.view))
            .map_or(&Value::Null, |p| &p.data)
    }
    pub fn set_data(&mut self, data: Value, now: f64) {
        let p = self.page_mut();
        p.data = data;
        p.fetched = Some((now * 1000.0) as u64);
    }
    /// A new request begins: later responses of older sequence numbers are ignored.
    pub fn begin(&mut self) -> u64 {
        self.generation = generation();
        self.loading = true;
        self.error.clear();
        self.generation
    }
    pub fn query(&self) -> Vec<(String, String)> {
        let page = self.page();
        let mut q = vec![
            ("view".to_owned(), VIEWS[self.view].to_owned()),
            ("limit".to_owned(), LIMIT.to_string()),
        ];
        if let Some(before) = page.stack.last() {
            q.push(("before".into(), before.clone()));
        }
        if self.target != self.session {
            q.push(("session".into(), self.target.clone()));
        }
        for (key, value) in [
            ("q", &page.filters.q),
            ("kind", &page.filters.kind),
            ("status", &page.filters.status),
            ("type", &page.filters.types),
        ] {
            if !value.is_empty() {
                q.push((key.into(), value.clone()));
            }
        }
        q
    }
    #[doc(hidden)]
    pub fn items_len_for_test(&self) -> usize {
        self.items().len()
    }
    fn items(&self) -> Vec<Value> {
        let data = self.data();
        let key = if self.view == 0 { "lanes" } else { "records" };
        data[key].as_array().cloned().unwrap_or_default()
    }
    pub fn index(&self) -> usize {
        let page = self.page();
        let items = self.items();
        page.selected
            .and_then(|id| items.iter().position(|v| id_of(v) == id))
            .unwrap_or(0)
    }
    fn select_at(&mut self, index: usize) {
        let items = self.items();
        if let Some(item) = items.get(index.min(items.len().saturating_sub(1))) {
            let id = id_of(item);
            self.page_mut().selected = Some(id);
        }
    }
    fn selected_item(&self) -> Option<Value> {
        self.items().get(self.index()).cloned()
    }

    // ---- state changes ------------------------------------------------------------------
    pub fn set_view(&mut self, view: usize) -> Act {
        self.view = view;
        self.edit = None;
        self.newer = false;
        Act::Fetch
    }
    pub fn set_target(&mut self, target: String) -> Act {
        self.target = target;
        self.edit = None;
        self.newer = false;
        Act::Fetch
    }
    /// Move to the previous (`-1`) or next (`1`) member of the session tree.
    pub fn step_session(&mut self, delta: isize) -> Act {
        let tree: Vec<String> = self.data()["tree"]
            .as_array()
            .map(|t| t.iter().map(id_of).collect())
            .unwrap_or_default();
        if tree.is_empty() {
            return Act::None;
        }
        // The root is the first entry; a target missing from the list counts as the root.
        let at = tree.iter().position(|id| *id == self.target).unwrap_or(0);
        let next = (at as isize + delta).clamp(0, tree.len() as isize - 1) as usize;
        if next == at {
            return Act::None;
        }
        self.set_target(tree[next].clone())
    }
    pub fn older(&mut self) -> Act {
        let Some(cursor) = self.data()["next_before"].as_str().map(str::to_owned) else {
            return Act::None;
        };
        let p = self.page_mut();
        p.stack.push(cursor);
        p.top = 0;
        Act::Fetch
    }
    pub fn newer_page(&mut self) -> Act {
        let p = self.page_mut();
        if p.stack.pop().is_none() {
            return Act::None;
        }
        p.top = 0;
        self.newer = false;
        Act::Fetch
    }
    fn reset_page(&mut self) {
        let p = self.page_mut();
        p.stack.clear();
        p.selected = None;
        p.top = 0;
    }
    pub fn cycle(&mut self, field: &str) -> Act {
        let options = if field == "kind" {
            kind_options(self.view)
        } else {
            status_options(self.view)
        };
        if options.is_empty() {
            return Act::None;
        }
        let p = self.page_mut();
        let slot = if field == "kind" {
            &mut p.filters.kind
        } else {
            &mut p.filters.status
        };
        *slot = match options.iter().position(|o| o == slot) {
            None => options[0].to_owned(),
            Some(i) if i + 1 < options.len() => options[i + 1].to_owned(),
            Some(_) => String::new(),
        };
        self.reset_page();
        Act::Fetch
    }
    pub fn begin_edit(&mut self, field: Field) {
        if field == Field::Type && !supports_type(self.view) {
            return;
        }
        let current = match field {
            Field::Q => self.page().filters.q,
            Field::Type => self.page().filters.types,
        };
        self.edit = Some((field, current));
    }
    pub fn finish_edit(&mut self) -> Act {
        let Some((field, text)) = self.edit.take() else {
            return Act::None;
        };
        let text = text.trim().to_owned();
        let p = self.page_mut();
        let slot = match field {
            Field::Q => &mut p.filters.q,
            Field::Type => &mut p.filters.types,
        };
        if *slot == text {
            return Act::None;
        }
        *slot = text;
        self.reset_page();
        Act::Fetch
    }
    pub fn clear_filters(&mut self) -> Act {
        if !self.page().filters.any() {
            return Act::None;
        }
        self.page_mut().filters = Filters::default();
        self.reset_page();
        Act::Fetch
    }
    pub fn toggle_live(&mut self) -> Act {
        self.paused = !self.paused;
        if self.paused {
            Act::None
        } else {
            self.polled = None;
            Act::Fetch
        }
    }
    pub fn select(&mut self, delta: isize) {
        let n = self.items().len();
        if n == 0 {
            return;
        }
        let next = (self.index() as isize + delta).clamp(0, n as isize - 1) as usize;
        self.select_at(next);
    }
    /// A click on a row selects it and toggles its details.
    pub fn pick(&mut self, id: &str) {
        if self.items().iter().any(|v| id_of(v) == id) {
            self.page_mut().selected = Some(id.to_owned());
            self.expand();
        }
    }
    pub fn expand(&mut self) {
        if let Some(item) = self.selected_item() {
            let id = id_of(&item);
            self.page_mut().selected = Some(id.clone());
            let set = &mut self.page_mut().expanded;
            if !set.remove(&id) {
                set.insert(id);
            }
        }
    }
    pub fn zoom(&mut self, factor_up: bool) {
        let p = self.page_mut();
        p.zoom = if factor_up {
            (p.zoom * 2).min(64)
        } else {
            (p.zoom / 2).max(1)
        };
    }
    pub fn pan(&mut self, right: bool) {
        let p = self.page_mut();
        p.pan = if right {
            (p.pan + 100).min(1000)
        } else {
            p.pan.saturating_sub(100)
        };
    }
    fn axis(&self) -> Option<(f64, f64)> {
        let lanes = self.items();
        if self.view != 0 || lanes.is_empty() {
            return None;
        }
        let origin = lanes
            .iter()
            .filter_map(|l| l["start"].as_f64())
            .fold(f64::INFINITY, f64::min);
        let end = lanes
            .iter()
            .filter_map(|l| l["end"].as_f64())
            .fold(origin, f64::max);
        Some((origin, (end - origin).max(0.001)))
    }
    /// Fit the selected operation, or the whole range when it is already fitted.
    pub fn fit(&mut self) {
        let Some((origin, total)) = self.axis() else {
            return;
        };
        let lane = self.selected_item();
        let p = self.page_mut();
        let range = lane.as_ref().and_then(|l| {
            let a = l["start"].as_f64()?;
            let b = l["end"].as_f64()?;
            Some((a, b))
        });
        match range {
            Some((a, b)) if p.zoom == 1 && b - a > 0.0 && total / (b - a) >= 2.0 => {
                let zoom = ((total / ((b - a) * 1.2)).floor() as u32).clamp(1, 64);
                let span = total / zoom as f64;
                let start = ((a + b) / 2.0 - span / 2.0).clamp(origin, origin + total - span);
                p.zoom = zoom;
                p.pan = if total > span {
                    (((start - origin) / (total - span)) * 1000.0).round() as u32
                } else {
                    0
                };
            }
            _ => {
                p.zoom = 1;
                p.pan = 0;
            }
        }
    }
    /// Ask for the full record of the selected row when its detail was clipped.
    pub fn load_full(&mut self) -> Act {
        let Some(item) = self.selected_item() else {
            return Act::None;
        };
        let id = id_of(&item);
        if item["detail_truncated"] != true || self.page().full.contains_key(&id) {
            return Act::None;
        }
        let (kind, rid) = if self.view == 0 {
            (
                item["ref"]["kind"].as_str().unwrap_or("").to_owned(),
                item["ref"]["id"].as_str().unwrap_or("").to_owned(),
            )
        } else {
            (item["kind"].as_str().unwrap_or("").to_owned(), id.clone())
        };
        if ![
            "message", "event", "turn", "request", "tool", "attempt", "terminal",
        ]
        .contains(&kind.as_str())
        {
            return Act::None;
        }
        self.loading_full = Some(id.clone());
        Act::Record {
            kind,
            id: rid,
            row: id,
        }
    }
    pub fn accept_record(&mut self, target: &str, view: usize, row: &str, body: &Value) {
        if let Some(p) = self.pages.get_mut(&(target.to_owned(), view)) {
            p.full.insert(
                row.to_owned(),
                serde_json::to_string_pretty(&body["record"]).unwrap_or_default(),
            );
        }
        if self.loading_full.as_deref() == Some(row) {
            self.loading_full = None;
        }
    }
    pub fn copy_text(&self) -> String {
        if let Some(item) = self.selected_item() {
            let id = id_of(&item);
            if let Some(full) = self.page().full.get(&id) {
                return full.clone();
            }
            if let Some(detail) = item["detail"].as_str() {
                return detail.to_owned();
            }
        }
        if self.view == 0 {
            serde_json::to_string_pretty(self.data()).unwrap_or_default()
        } else {
            self.data()["text"].as_str().unwrap_or("").to_owned()
        }
    }
    pub fn newest(&self) -> bool {
        self.page().stack.is_empty()
    }
    pub fn live(&self) -> bool {
        self.connected && !self.paused && self.newest()
    }
    /// Coalesce durable event bursts before checking the inspection watermark.
    pub fn invalidate(&mut self, now: Instant) {
        if self.dirty_at.is_none() {
            self.dirty_at = Some(now);
        }
    }
    pub fn should_poll(&self, now: Instant) -> bool {
        !self.paused
            && !self.loading
            && !self.head_inflight
            && self
                .dirty_at
                .is_some_and(|at| now.duration_since(at).as_millis() >= 200)
    }
    /// Apply a head response. Returns true when the newest page must be refetched.
    pub fn on_head(&mut self, head: &Value) -> bool {
        self.connected = true;
        let data = self.data().clone();
        if data.is_null() {
            return !self.loading;
        }
        let history_changed = head["generation"] != data["generation"]
            || head["floor"].as_i64().unwrap_or(0) > data["floor"].as_i64().unwrap_or(0);
        if history_changed {
            for ((target, _), page) in self.pages.iter_mut() {
                if *target == self.target {
                    page.stack.clear();
                    page.selected = None;
                    page.top = 0;
                }
            }
            return true;
        }
        let moved = head["head"] != data["head"];
        if moved && !self.newest() {
            self.newer = true;
            return false;
        }
        if !moved {
            self.error.clear();
        }
        moved
    }

    // ---- text ---------------------------------------------------------------------------
    fn cache_lines(&self, out: &mut Vec<String>) {
        let data = self.data();
        let cache = &data["cache"];
        let write = if cache["write_known"] == true {
            n_text(&cache["write"])
        } else {
            "unknown".into()
        };
        out.push(format!(
            "Session lifetime · Input {} · Cached {} · Uncached {}",
            n_text(&cache["input"]),
            n_text(&cache["cached"]),
            n_text(&cache["uncached"]),
        ));
        out.push(format!(
            "  Hit {} of {} · Writes {write}",
            pct(&cache["hit_percent"]),
            cache["denominator"]
                .as_str()
                .unwrap_or("input tokens reported by provider"),
        ));
        let cov = &cache["coverage"];
        if cov.is_object() {
            let complete = if cov["complete"] == true {
                ""
            } else {
                " (partial)"
            };
            out.push(format!(
                "  {} of {} requests reported usage{complete}",
                n_text(&cov["requests_with_usage"]),
                n_text(&cov["requests_total"]),
            ));
        }
        if let Some(models) = cache["models"].as_object() {
            for (name, m) in models {
                out.push(format!(
                    "  {name} · {} requests · input {} · cached {} · hit {}",
                    n_text(&m["requests"]),
                    n_text(&m["input"]),
                    n_text(&m["cached"]),
                    pct(&m["hit_percent"]),
                ));
            }
        }
        let range = &data["cache_range"];
        if range.is_object() {
            let cov = &range["coverage"];
            out.push(format!(
                "Displayed page · Input {} · Cached {} · Uncached {}",
                n_text(&range["input"]),
                n_text(&range["cached"]),
                n_text(&range["uncached"]),
            ));
            out.push(format!(
                "  Hit {} · {} of {} requests reported usage",
                pct(&range["hit_percent"]),
                n_text(&cov["requests_with_usage"]),
                n_text(&cov["requests_total"]),
            ));
            if let Some(models) = range["models"].as_object() {
                for (name, m) in models {
                    out.push(format!(
                        "  {name} · {} requests · input {} · cached {} · hit {}",
                        n_text(&m["requests"]),
                        n_text(&m["input"]),
                        n_text(&m["cached"]),
                        pct(&m["hit_percent"]),
                    ));
                }
            }
        }
    }

    pub fn status_line(&self, now: f64) -> String {
        let page = self.page();
        let when = page.fetched.map(|f| age(now - f as f64 / 1000.0));
        let updated = when.clone().unwrap_or_else(|| "never".into());
        let mut line = if !self.connected {
            format!("○ Disconnected · data from {updated} · not current · r retry")
        } else if self.paused {
            format!("‖ Paused · updated {updated} · w resume")
        } else if !self.newest() {
            format!("‖ Paused · viewing older records · updated {updated} · p newer")
        } else if page.fetched.is_none() {
            "● Live · waiting for first data".into()
        } else {
            format!("● Live · updated {updated}")
        };
        if self.newer {
            line.push_str(" · newer records available");
        }
        if self.loading {
            line.push_str(" · refreshing…");
        }
        line
    }

    pub fn text(&self, width: usize) -> String {
        let parts = self.parts(width, 0.0);
        format!("{}\n{}", parts.fixed.join("\n"), parts.body.join("\n"))
    }

    /// Fixed header lines (one row each) and the scrolling body with its selected line.
    pub fn parts(&self, width: usize, now: f64) -> Parts {
        let mut fixed: Vec<String> = Vec::new();
        let mut out: Vec<String> = Vec::new();
        let data = self.data();
        let page = self.page();
        let title = |id: &str| {
            data["tree"]
                .as_array()
                .and_then(|t| t.iter().find(|n| id_of(n) == id))
                .and_then(|n| n["title"].as_str())
                .map(str::to_owned)
        };
        let mut head = self.session.clone();
        if self.target != self.session {
            head.push_str(&format!(
                " → inspecting {} ({})",
                title(&self.target).unwrap_or_else(|| self.target.clone()),
                self.target
            ));
        }
        fixed.push(head);
        fixed.push(
            VIEWS
                .iter()
                .enumerate()
                .map(|(i, v)| {
                    if i == self.view {
                        format!("[{}] {v}", i + 1)
                    } else {
                        format!("{} {v}", i + 1)
                    }
                })
                .collect::<Vec<_>>()
                .join(" · "),
        );
        fixed.push(self.status_line(now));
        if let Some((field, buffer)) = &self.edit {
            let name = if *field == Field::Q {
                "Search"
            } else {
                "Event types"
            };
            fixed.push(format!("{name}: {buffer}▏ (enter apply · esc cancel)"));
        }
        let f = &page.filters;
        let show = |s: &str| {
            if s.is_empty() {
                "any".to_owned()
            } else {
                s.to_owned()
            }
        };
        let mut filters = format!(
            "Filters · search {}",
            if f.q.is_empty() {
                "none".into()
            } else {
                format!("\"{}\"", f.q)
            }
        );
        if !kind_options(self.view).is_empty() {
            filters.push_str(&format!(" · kind {}", show(&f.kind)));
        }
        if !status_options(self.view).is_empty() {
            filters.push_str(&format!(" · status {}", show(&f.status)));
        }
        if supports_type(self.view) {
            filters.push_str(&format!(" · type {}", show(&f.types)));
        }
        fixed.push(filters);
        let mut tree_hits = Vec::new();
        if let Some(tree) = data["tree"].as_array().filter(|t| t.len() > 1) {
            let at = tree
                .iter()
                .position(|n| id_of(n) == self.target)
                .unwrap_or(0);
            let start = at.saturating_sub(2).min(tree.len().saturating_sub(6));
            let range = start..(start + 6).min(tree.len());
            // Entries wrap onto further lines; each remembers where it is so a click can pick it.
            let mut tokens: Vec<(String, Option<String>)> =
                vec![(format!("Sessions ({}) [ ]", tree.len()), None)];
            if range.start > 0 {
                tokens.push((format!("… {} before", range.start), None));
            }
            for n in &tree[range.clone()] {
                tokens.push((
                    format!(
                        "{}{} {}",
                        if id_of(n) == self.target { "›" } else { "" },
                        n["title"].as_str().unwrap_or(&id_of(n)),
                        n["status"].as_str().unwrap_or(""),
                    ),
                    Some(id_of(n)),
                ));
            }
            if range.end < tree.len() {
                tokens.push((format!("… {} after", tree.len() - range.end), None));
            }
            let mut line = String::new();
            let mut col = 0usize;
            for (k, (text, id)) in tokens.into_iter().enumerate() {
                let w = text.chars().count();
                if k > 0 && col + 3 + w > width {
                    fixed.push(std::mem::take(&mut line));
                    line = "  ".into();
                    col = 2;
                } else if k > 0 {
                    line.push_str(" · ");
                    col += 3;
                }
                if let Some(id) = id {
                    tree_hits.push((fixed.len(), col, w.min(width.saturating_sub(col)), id));
                }
                line.push_str(&text);
                col += w;
            }
            fixed.push(line);
        }
        let items = self.items();
        let gantt = (self.view == 0 && !items.is_empty()).then(|| self.gantt_geometry(width));
        if let Some(g) = &gantt {
            // The ruler stays put while the lanes scroll under it.
            fixed.extend(g.header.iter().cloned());
        }
        let fixed: Vec<String> = fixed.iter().map(|l| clip(l, width)).collect();
        self.cache_lines(&mut out);
        if let Some(omitted) = data["omitted"].as_array() {
            for o in omitted {
                let mut line = format!("Omitted · {}", o["kind"].as_str().unwrap_or(""));
                if let Some(c) = o["count"].as_u64() {
                    line.push_str(&format!(" ×{c}"));
                }
                if let Some(b) = o["bytes"].as_u64() {
                    line.push_str(&format!(" · {}", bytes(b)));
                }
                line.push_str(&format!(" · {}", o["reason"].as_str().unwrap_or("")));
                out.push(line);
            }
        }
        if let Some(notes) = data["notes"].as_array() {
            for note in notes.iter().filter_map(Value::as_str) {
                if !self.info && note.starts_with("Cache") {
                    continue;
                }
                out.push(note.to_owned());
            }
        }
        if self.loading && data.is_null() {
            out.push("Loading inspector…".into());
        }
        if !self.error.is_empty() {
            out.push(format!("Error · {}", self.error));
        }
        out.push(if page.stack.is_empty() {
            if data["more"] == true {
                "Newest records · n older".into()
            } else {
                "Newest records".into()
            }
        } else if data["more"] == true {
            "Older records · p newer · n older".into()
        } else {
            "Older records · p newer · oldest retained".into()
        });
        out.push(String::new());
        let mut cursor_line = None;
        let mut row_hits_out: Vec<(usize, String)> = Vec::new();
        if items.is_empty() {
            out.push(if self.view == 0 {
                if f.any() {
                    "No timings match the filters.".into()
                } else {
                    "No retained execution timings.".into()
                }
            } else if f.any() {
                "No records match the filters.".into()
            } else {
                data["text"].as_str().unwrap_or("").to_owned()
            });
        } else {
            let index = self.index();
            let range = window(index, items.len());
            if range.start > 0 {
                out.push(format!("… {} rows above", range.start));
            }
            let mut last_session = String::new();
            for i in range.clone() {
                let item = &items[i];
                let id = id_of(item);
                let open = page.expanded.contains(&id);
                let mark = if i == index { "›" } else { " " };
                let fold = if open { "▾" } else { "▸" };
                if self.view == 0 {
                    let sid = item["session_id"].as_str().unwrap_or("");
                    if sid != last_session {
                        last_session = sid.to_owned();
                        let count = items
                            .iter()
                            .filter(|l| l["session_id"] == item["session_id"])
                            .count();
                        out.push(format!(
                            "{} · {} operation{}",
                            item["session"].as_str().unwrap_or(sid),
                            count,
                            if count == 1 { "" } else { "s" }
                        ));
                    }
                    if i == index {
                        cursor_line = Some(out.len());
                    }
                    row_hits_out.push((out.len(), id.clone()));
                    out.push(gantt.as_ref().unwrap().row(item, mark, fold));
                } else {
                    if i == index {
                        cursor_line = Some(out.len());
                    }
                    row_hits_out.push((out.len(), id.clone()));
                    out.push(clip(
                        &format!("{mark} {fold} {}", item["summary"].as_str().unwrap_or("")),
                        width,
                    ));
                }
                if open {
                    self.detail_lines(item, &page, width, &mut out);
                }
            }
            if range.end < items.len() {
                out.push(format!("… {} rows below", items.len() - range.end));
            }
        }
        // Hard-wrap long lines here so the overlay's rows map one to one onto these lines.
        let mut lines: Vec<String> = Vec::new();
        let mut selected = None;
        let mut starts = Vec::with_capacity(out.len());
        for (i, line) in out.iter().enumerate() {
            starts.push(lines.len());
            if Some(i) == cursor_line {
                selected = Some(lines.len());
            }
            let chars: Vec<char> = line.chars().collect();
            if chars.len() <= width.max(1) {
                lines.push(line.clone());
            } else {
                for chunk in chars.chunks(width.max(1)) {
                    lines.push(chunk.iter().collect());
                }
            }
        }
        Parts {
            fixed,
            body: lines,
            cursor: selected,
            tree_hits,
            row_hits: row_hits_out
                .into_iter()
                .map(|(at, id)| (starts[at], id))
                .collect(),
        }
    }

    fn detail_lines(&self, item: &Value, page: &Page, width: usize, out: &mut Vec<String>) {
        let id = id_of(item);
        let full = page.full.get(&id);
        let text = full
            .map(String::as_str)
            .or(item["detail"].as_str())
            .unwrap_or("");
        if let Some(links) = item["links"].as_object() {
            let parts: Vec<String> = links
                .iter()
                .filter_map(|(k, v)| v.as_str().map(|v| format!("{k} {v}")))
                .collect();
            if !parts.is_empty() {
                out.push(format!("    Links · {}", parts.join(" · ")));
            }
        }
        let lines: Vec<&str> = text.lines().collect();
        for line in lines.iter().take(DETAIL_LINES) {
            out.push(clip(&format!("    {line}"), width));
        }
        if lines.len() > DETAIL_LINES {
            out.push(format!(
                "    … {} more lines (c copies all)",
                lines.len() - DETAIL_LINES
            ));
        }
        if full.is_some() {
            out.push(format!(
                "    Full record loaded · {} bytes",
                item["detail_bytes"].as_u64().unwrap_or(text.len() as u64)
            ));
        } else if item["detail_truncated"] == true {
            let size = item["detail_bytes"].as_u64().unwrap_or(0);
            if self.loading_full.as_deref() == Some(id.as_str()) {
                out.push("    Loading full record…".into());
            } else {
                out.push(format!(
                    "    Clipped · showing the first part of {} · e loads the full record",
                    bytes(size)
                ));
            }
        }
    }

    fn gantt_geometry(&self, width: usize) -> Gantt {
        let page = self.page();
        let (origin, total) = self.axis().unwrap_or((0.0, 1.0));
        let (start, span) = time_window(origin, total, page.zoom, page.pan);
        let label_width = (width / 3).clamp(10, 28);
        let columns = width.saturating_sub(label_width + 22).max(8);
        let lanes = self.items();
        let ttfb = lanes.iter().any(|l| l["ttfb"].is_number());
        let mut header = Vec::new();
        header.push(format!(
            "{}× · view +{:.2}s to +{:.2}s of {:.2}s · z fit",
            page.zoom,
            start - origin,
            start + span - origin,
            total
        ));
        // Ruler: labelled ticks at 0, 25, 50, 75 and 100 percent of the visible range.
        let stamp = |f: f64| format!("+{:.2}s", start + span * f - origin);
        let mut labels = vec![' '; columns + 2];
        let mut ticks = vec!['─'; columns + 2];
        // The ends and the middle always get a label; the quarter marks only when they fit.
        for k in [0usize, 4, 2, 1, 3] {
            let at = (columns * k / 4).min(columns);
            ticks[at + 1] = '┼';
            let chars: Vec<char> = stamp(k as f64 / 4.0).chars().collect();
            let from = if k == 4 {
                (at + 2).saturating_sub(chars.len())
            } else {
                at + 1
            };
            let lo = from.saturating_sub(1);
            let hi = (from + chars.len() + 1).min(labels.len());
            if from + chars.len() > labels.len() || labels[lo..hi].iter().any(|c| *c != ' ') {
                continue;
            }
            for (j, c) in chars.iter().enumerate() {
                labels[from + j] = *c;
            }
        }
        let pad = " ".repeat(label_width + 3);
        header.push(format!(
            "{pad}{}",
            labels.iter().collect::<String>().trim_end()
        ));
        header.push(format!("{pad}{}", ticks.iter().collect::<String>()));
        let mut legend =
            "# model · = tool · ~ terminal · - turn/attempt · > live · ! failed · ? end unknown"
                .to_owned();
        if ttfb {
            legend.push_str(" · | first byte");
        }
        header.push(legend);
        Gantt {
            start,
            span,
            label_width,
            columns,
            header,
        }
    }
}

/// The inspector as drawn: a fixed header, a scrolling body and the body's selected line.
pub struct Parts {
    pub fixed: Vec<String>,
    pub body: Vec<String>,
    pub cursor: Option<usize>,
    /// Clickable session-tree entries: fixed row, column, width, session id.
    pub tree_hits: Vec<(usize, usize, usize, String)>,
    /// Clickable rows: body line and record id.
    pub row_hits: Vec<(usize, String)>,
}

struct Gantt {
    start: f64,
    span: f64,
    label_width: usize,
    columns: usize,
    header: Vec<String>,
}
impl Gantt {
    fn cell(&self, t: f64) -> isize {
        ((t - self.start) / self.span * self.columns as f64).floor() as isize
    }
    fn row(&self, lane: &Value, mark: &str, fold: &str) -> String {
        let a = lane["start"].as_f64().unwrap_or(self.start);
        let b = lane["end"].as_f64().unwrap_or(a);
        let columns = self.columns;
        let status = lane["status"].as_str().unwrap_or("");
        let unknown = lane["end_unknown"] == true;
        let glyph = if unknown {
            '?'
        } else if status.contains("fail") || status.contains("error") || status == "interrupted" {
            '!'
        } else if lane["running"] == true {
            '>'
        } else if lane["kind"] == "model" {
            '#'
        } else if lane["kind"] == "tool" {
            '='
        } else if lane["kind"] == "terminal" {
            '~'
        } else {
            '-'
        };
        let left = self.cell(a).clamp(0, columns as isize - 1) as usize;
        let right = (((b - self.start) / self.span * columns as f64).ceil() as isize)
            .clamp(0, columns as isize) as usize;
        let mut bar = vec![' '; columns];
        if b >= self.start && a <= self.start + self.span {
            let end = if unknown {
                left + 1
            } else {
                right.max(left + 1)
            };
            for cell in bar.iter_mut().take(end).skip(left) {
                *cell = glyph;
            }
            if let Some(t) = lane["ttfb"].as_f64() {
                let at = self.cell(a + t);
                if at >= 0 && (at as usize) < columns {
                    bar[at as usize] = '|';
                }
            }
        }
        let label = format!(
            "{} {}",
            lane["kind"].as_str().unwrap_or(""),
            lane["label"].as_str().unwrap_or("")
        );
        let label: String = label.chars().take(self.label_width).collect();
        let duration = if unknown {
            "end unknown".to_owned()
        } else if lane["running"] == true {
            format!("{:.2}s live", b - a)
        } else {
            format!("{:.2}s", b - a)
        };
        format!(
            "{mark} {fold} {label:lw$} |{}| {duration}",
            bar.iter().collect::<String>(),
            lw = self.label_width
        )
    }
}

#[cfg(test)]
mod invalidation_tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn events_are_coalesced_and_never_poll_without_invalidation() {
        let mut inspector = Inspector::new("session".into());
        inspector.loading = false;
        let now = Instant::now();
        assert!(!inspector.should_poll(now + Duration::from_secs(10)));
        inspector.invalidate(now);
        inspector.invalidate(now + Duration::from_millis(50));
        assert!(!inspector.should_poll(now + Duration::from_millis(199)));
        assert!(inspector.should_poll(now + Duration::from_millis(200)));
        inspector.head_inflight = true;
        assert!(!inspector.should_poll(now + Duration::from_secs(1)));
    }
}

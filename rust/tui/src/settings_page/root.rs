//! The Settings root list (plan GROK_TUI_PLAN WS-C): one scrolling list of section
//! headers, local toggles and drill rows (`▸ Label … ›`) inside the modal frame, with a
//! search field, footer tip and key hints. Presentation and keys only; a drill row asks the
//! caller to send `nav_select`, a toggle asks it to flip a local appearance flag.
use crate::render::Palette;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};
use unicode_width::UnicodeWidthStr;

/// One row of the flattened root list.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RootRow {
    Header(String),
    Toggle {
        key: &'static str,
        label: String,
        desc: &'static str,
        on: bool,
        default: bool,
    },
    /// A host-owned row inlined from the Appearance/Layout pages: Enter flips or cycles it
    /// and sends the page's own operation (same handler as the drilled page).
    Host {
        label: String,
        desc: String,
        value: String,
        /// Operation to send for each direction: `(forward, backward)`.
        next: Option<serde_json::Value>,
        prev: Option<serde_json::Value>,
    },
    /// Opens the Settings area at `index` in the host's nav list.
    Drill {
        index: usize,
        label: String,
        desc: &'static str,
        /// Cheap current value from the snapshot (`Dark`, the model name); may be empty.
        summary: String,
    },
}

impl RootRow {
    fn selectable(&self) -> bool {
        !matches!(self, RootRow::Header(_))
    }
    fn matches(&self, query: &str) -> bool {
        let q = query.to_lowercase();
        match self {
            RootRow::Header(_) => false,
            RootRow::Toggle { label, desc, .. } | RootRow::Drill { label, desc, .. } => {
                label.to_lowercase().contains(&q) || desc.to_lowercase().contains(&q)
            }
            RootRow::Host { label, desc, .. } => {
                label.to_lowercase().contains(&q) || desc.to_lowercase().contains(&q)
            }
        }
    }
}

/// Short description of a Settings area, shown under the selected row.
fn area_desc(key: &str) -> &'static str {
    match key {
        "appearance" => "Theme and how this terminal looks.",
        "layout" => "Which panels, sidebars and rows are shown.",
        "keys" => "Keyboard shortcuts and chat commands.",
        "providers" => "Sign in to model providers and see their usage.",
        "models" => "Default models, tiers and fallbacks.",
        "voice" => "Local speech and dictation.",
        "agents" => "Build, advisor, task and quick agents and their models.",
        "tools" => "Python tools the agent can call.",
        "mcp" => "External tools connected through MCP servers.",
        "skills" => "Reusable workflows the agent can follow.",
        _ => "",
    }
}

/// A short current value for a drill row, from data already in the snapshot.
fn summary(key: &str, s: &crate::bridge::Snapshot) -> String {
    match key {
        "appearance" => if s.theme == "nexus-light" {
            "Light"
        } else {
            "Dark"
        }
        .to_string(),
        "models" => s.model.clone(),
        "agents" => s.agent.clone(),
        _ => String::new(),
    }
}

/// Rows of one host-built row block: a toggle flips, a segmented control cycles.
fn host_row(r: &crate::settings_page::model::Row) -> Option<RootRow> {
    use crate::settings_page::model::Control;
    use serde_json::{json, Value};
    let with = |op: &Value, v: Value| {
        let mut out = op.clone();
        if let Value::Object(m) = &mut out {
            m.insert("value".into(), v);
        }
        out
    };
    let (value, next, prev) = match &r.control {
        Control::Toggle { on, locked, op } if locked.is_empty() => {
            let flip = Some(with(op, json!(!on)));
            (
                if *on { "on" } else { "off" }.to_string(),
                flip.clone(),
                flip,
            )
        }
        Control::Segmented {
            options,
            values,
            active,
            op,
        } if !options.is_empty() => {
            let at = |i: usize| {
                with(
                    op,
                    values.get(i).cloned().unwrap_or_else(|| json!(options[i])),
                )
            };
            let n = options.len();
            (
                format!("{} ›", options[(*active).min(n - 1)]),
                Some(at((active + 1) % n)),
                Some(at((active + n - 1) % n)),
            )
        }
        _ => return None,
    };
    Some(RootRow::Host {
        label: r.label.clone(),
        desc: r.description.clone(),
        value,
        next,
        prev,
    })
}

/// The unfiltered root rows: the host's inlined Appearance and Layout rows (when the snapshot
/// carries `settings_overview`) with the local appearance toggles, then one drill row per
/// remaining area. Without an overview the General areas are plain drill rows.
pub fn build(
    nav: &crate::bridge::Nav,
    toggles: &[(&'static str, &'static str, bool)],
    query: &str,
    snap: &crate::bridge::Snapshot,
) -> Vec<RootRow> {
    use crate::settings_page::model::Block;
    let overview = snap
        .settings_overview
        .as_ref()
        .map(crate::settings_page::Page::from_value)
        .filter(|p| !p.blocks.is_empty());
    let toggle_rows = || -> Vec<RootRow> {
        toggles
            .iter()
            .map(|(key, label, on)| RootRow::Toggle {
                key,
                label: (*label).into(),
                desc: match *key {
                    "animate_activity" => "Animate spinners and live activity.",
                    _ => "Drop all colour; keep bold and dim.",
                },
                on: *on,
                default: *key == "animate_activity",
            })
            .collect()
    };
    let host_summary = |k: &str| {
        snap.settings_overview
            .as_ref()
            .and_then(|v| v.get("summaries")?.get(k)?.as_str().map(String::from))
            .unwrap_or_default()
    };
    let mut all: Vec<RootRow> = Vec::new();
    let mut first_section = true;
    for (index, (label, key, heading)) in nav.items.iter().enumerate() {
        if *heading {
            if label.is_empty() {
                continue;
            }
            if first_section {
                first_section = false;
                if let Some(page) = &overview {
                    // Inlined sections: each `heading` block opens one; Appearance also
                    // carries the local toggles.
                    let mut open: Option<String> = None;
                    let mut flush = |all: &mut Vec<RootRow>, open: &mut Option<String>| {
                        if open.as_deref() == Some("Appearance") {
                            all.extend(toggle_rows());
                        }
                        *open = None;
                    };
                    for block in &page.blocks {
                        match block {
                            Block::Heading(h) => {
                                flush(&mut all, &mut open);
                                all.push(RootRow::Header(h.clone()));
                                open = Some(h.clone());
                            }
                            Block::Row(r) => all.extend(host_row(r)),
                            _ => {}
                        }
                    }
                    flush(&mut all, &mut open);
                    all.push(RootRow::Header("Keyboard".into()));
                } else {
                    all.push(RootRow::Header("General".into()));
                    all.extend(toggle_rows());
                }
                continue;
            }
            let mut title = label.to_lowercase();
            if let Some(c) = title.get_mut(0..1) {
                c.make_ascii_uppercase();
            }
            all.push(RootRow::Header(title));
        } else {
            if overview.is_some() && matches!(key.as_str(), "appearance" | "layout") {
                continue;
            }
            all.push(RootRow::Drill {
                index,
                label: label.clone(),
                desc: area_desc(key),
                summary: if matches!(key.as_str(), "keys" | "providers") {
                    host_summary(key)
                } else {
                    summary(key, snap)
                },
            });
        }
    }
    if query.is_empty() {
        return all;
    }
    // Filtered: keep matching rows, plus the header of any section that still has one.
    let mut out: Vec<RootRow> = Vec::new();
    let mut header: Option<RootRow> = None;
    for row in all {
        match row {
            RootRow::Header(_) => header = Some(row),
            _ if row.matches(query) => {
                if let Some(h) = header.take() {
                    out.push(h);
                }
                out.push(row);
            }
            _ => {}
        }
    }
    out
}

#[derive(Default)]
pub struct RootState {
    /// The root list is showing (otherwise a drilled page is).
    pub active: bool,
    /// Set once a Settings modal is open; cleared when it closes, so a new open starts at the root.
    pub seen: bool,
    /// Index into the visible rows; always on a selectable row after `settle`.
    pub cursor: usize,
    pub query: String,
    pub searching: bool,
    pub scroll: usize,
    /// Row hit rectangles of the last draw: `(rect, row index)`.
    pub hits: Vec<(Rect, usize)>,
}

impl RootState {
    /// Track the modal opening and closing so every open starts at the root.
    pub fn track(&mut self, open: bool) {
        if open && !self.seen {
            *self = RootState {
                active: true,
                seen: true,
                ..Default::default()
            };
        } else if !open && self.seen {
            *self = RootState::default();
        }
    }
    fn settle(&mut self, rows: &[RootRow]) {
        if rows.get(self.cursor).is_some_and(RootRow::selectable) {
            return;
        }
        self.cursor = rows.iter().position(RootRow::selectable).unwrap_or(0);
    }
    fn step(&mut self, rows: &[RootRow], delta: i64) {
        let mut at = self.cursor as i64;
        loop {
            let next = at + delta.signum();
            if next < 0 || next >= rows.len() as i64 {
                return;
            }
            at = next;
            if rows[at as usize].selectable() {
                self.cursor = at as usize;
                return;
            }
        }
    }
}

/// What the caller should do after a root key or click.
#[derive(Debug, PartialEq, Eq)]
pub enum RootAct {
    /// Not ours; let the caller continue (Alt shortcuts, Ctrl+C handled by caller).
    Pass,
    /// Handled; redraw.
    Done,
    Close,
    /// Open the area at this nav index.
    Drill(usize),
    /// Flip the local appearance flag with this key.
    Toggle(&'static str),
    /// Send this host operation (an inlined Appearance/Layout row).
    Send(serde_json::Value),
}

fn activate(row: Option<&RootRow>) -> RootAct {
    match row {
        Some(RootRow::Drill { index, .. }) => RootAct::Drill(*index),
        Some(RootRow::Toggle { key, .. }) => RootAct::Toggle(key),
        Some(RootRow::Host { next: Some(op), .. }) => RootAct::Send(op.clone()),
        _ => RootAct::Done,
    }
}

pub fn key(st: &mut RootState, rows: &[RootRow], key: KeyEvent) -> RootAct {
    use KeyCode::*;
    st.settle(rows);
    if key.modifiers.contains(KeyModifiers::ALT) {
        return RootAct::Pass;
    }
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    if ctrl && key.code == Char('c') {
        return RootAct::Close;
    }
    if st.searching {
        match key.code {
            Esc => {
                st.query.clear();
                st.searching = false;
            }
            Enter | Down | Up => st.searching = false,
            Backspace => {
                st.query.pop();
            }
            Char(c) if !ctrl => st.query.push(c),
            _ => {}
        }
        // Rows change with the query; land on the first match.
        st.cursor = 0;
        st.scroll = 0;
        return RootAct::Done;
    }
    let page = 8;
    match key.code {
        Esc => {
            if st.query.is_empty() {
                return RootAct::Close;
            }
            st.query.clear();
            st.cursor = 0;
            st.scroll = 0;
        }
        Char('/') => st.searching = true,
        Up | Char('k') => st.step(rows, -1),
        Down | Char('j') => st.step(rows, 1),
        PageUp => (0..page).for_each(|_| st.step(rows, -1)),
        PageDown => (0..page).for_each(|_| st.step(rows, 1)),
        Home | Char('g') => {
            st.cursor = 0;
            st.settle(rows);
        }
        End | Char('G') => {
            st.cursor = rows.len().saturating_sub(1);
            if !rows.get(st.cursor).is_some_and(RootRow::selectable) {
                st.step(rows, -1);
            }
        }
        Enter | Char(' ') => return activate(rows.get(st.cursor)),
        Right | Char('l') => {
            if matches!(
                rows.get(st.cursor),
                Some(RootRow::Drill { .. } | RootRow::Host { .. })
            ) {
                return activate(rows.get(st.cursor));
            }
        }
        Left | Char('h') => {
            if let Some(RootRow::Host { prev: Some(op), .. }) = rows.get(st.cursor) {
                return RootAct::Send(op.clone());
            }
        }
        Char('d') => {
            if let Some(RootRow::Toggle { on, default, .. }) = rows.get(st.cursor) {
                if on != default {
                    return activate(rows.get(st.cursor));
                }
            }
        }
        _ => {}
    }
    RootAct::Done
}

/// A left click: select and activate the row under the pointer.
pub fn click(st: &mut RootState, rows: &[RootRow], x: u16, y: u16) -> RootAct {
    let hit = st
        .hits
        .iter()
        .find(|(r, _)| r.contains((x, y).into()))
        .map(|(_, i)| *i);
    match hit {
        Some(i) => {
            st.cursor = i;
            st.searching = false;
            activate(rows.get(i))
        }
        None => RootAct::Done,
    }
}

pub fn wheel(st: &mut RootState, rows: &[RootRow], delta: i64) {
    st.settle(rows);
    for _ in 0..3 {
        st.step(rows, delta);
    }
}

const HINTS: [(&str, &str); 8] = [
    ("↑/↓/j/k", "nav"),
    ("g/G", "top/btm"),
    ("Space", "toggle"),
    ("Enter", "edit"),
    ("→", "expand"),
    ("/", "search"),
    ("d", "reset"),
    ("Esc", "close"),
];

/// Hints wrapped to `width`, each line centred (bold keys, quiet labels and `|`).
pub fn hint_lines(hints: &[(&str, &str)], width: usize, p: &Palette) -> Vec<Line<'static>> {
    let key = Style::default().fg(p.text).add_modifier(Modifier::BOLD);
    let quiet = Style::default().fg(p.quiet);
    let mut lines: Vec<Vec<Span<'static>>> = vec![vec![]];
    let mut used = 0usize;
    for &(k, label) in hints {
        let w = k.width() + 1 + label.width();
        let sep = if used == 0 { 0 } else { 3 };
        if used > 0 && used + sep + w > width {
            lines.push(vec![]);
            used = 0;
        }
        let cur = lines.last_mut().unwrap();
        if used > 0 {
            cur.push(Span::styled(" | ", quiet));
            used += 3;
        }
        cur.push(Span::styled(k.to_string(), key));
        cur.push(Span::styled(format!(" {label}"), quiet));
        used += w;
    }
    lines
        .into_iter()
        .map(|spans| {
            let w: usize = spans.iter().map(|s| s.content.width()).sum();
            let mut line = Line::from(spans);
            line.alignment = Some(ratatui::layout::Alignment::Center);
            let _ = w;
            line
        })
        .collect()
}

pub fn draw(frame: &mut Frame, inner: Rect, st: &mut RootState, rows: &[RootRow], p: &Palette) {
    st.settle(rows);
    let bg = Style::default().bg(p.dialog).fg(p.text);
    let quiet = Style::default().fg(p.quiet);
    let width = usize::from(inner.width);
    // Search field and the rule under it.
    let search = if st.searching {
        Line::from(vec![
            Span::styled("/ ", quiet),
            Span::styled(format!("{}█", st.query), Style::default().fg(p.text)),
        ])
    } else if !st.query.is_empty() {
        Line::from(vec![
            Span::styled("/ ", quiet),
            Span::styled(st.query.clone(), Style::default().fg(p.text)),
        ])
    } else {
        Line::from(Span::styled("/ to search", quiet))
    };
    frame.render_widget(
        Paragraph::new(vec![
            search,
            Line::styled("─".repeat(width), Style::default().fg(p.border)),
        ])
        .style(bg),
        Rect::new(inner.x, inner.y, inner.width, 2.min(inner.height)),
    );
    // Footer: tip, then the wrapped key hints.
    let hints = hint_lines(&HINTS, width, p);
    let footer_h = (1 + hints.len() as u16 + 1).min(inner.height.saturating_sub(2));
    let footer = Rect::new(
        inner.x,
        inner.bottom().saturating_sub(footer_h),
        inner.width,
        footer_h,
    );
    let mut foot = vec![Line::from(Span::styled(
        "Tip · Saved in ~/.config/nexus/tui.json · applies at once",
        quiet.add_modifier(Modifier::ITALIC),
    ))];
    foot.push(Line::default());
    foot.extend(hints);
    frame.render_widget(Paragraph::new(foot).style(bg), footer);
    // Body: the flattened rows (the selected row's description sits under it).
    let body = Rect::new(
        inner.x,
        inner.y + 2,
        inner.width,
        inner.height.saturating_sub(2 + footer_h),
    );
    st.hits.clear();
    if rows.is_empty() {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled("No settings match", quiet))).style(bg),
            body,
        );
        return;
    }
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut owner: Vec<Option<usize>> = Vec::new();
    let mut selected_line = 0;
    for (i, row) in rows.iter().enumerate() {
        match row {
            RootRow::Header(title) => {
                if !lines.is_empty() {
                    lines.push(Line::default());
                    owner.push(None);
                }
                let used = title.width() + 1;
                lines.push(Line::from(vec![
                    Span::styled(
                        title.clone(),
                        Style::default().fg(p.muted).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(
                        format!(" {}", "─".repeat(width.saturating_sub(used))),
                        Style::default().fg(p.border),
                    ),
                ]));
                owner.push(None);
            }
            RootRow::Toggle {
                label, on, desc, ..
            } => {
                let (value, vstyle) = if *on {
                    ("on".to_string(), Style::default().fg(p.text))
                } else {
                    ("off".to_string(), quiet)
                };
                if i == st.cursor {
                    selected_line = lines.len();
                }
                lines.push(row_line(label, &value, vstyle, i == st.cursor, width, p));
                owner.push(Some(i));
                if i == st.cursor {
                    lines.push(desc_line(desc, p));
                    owner.push(Some(i));
                }
            }
            RootRow::Host {
                label, desc, value, ..
            } => {
                if i == st.cursor {
                    selected_line = lines.len();
                }
                let vstyle = if value == "off" {
                    quiet
                } else {
                    Style::default().fg(p.text)
                };
                lines.push(row_line(label, value, vstyle, i == st.cursor, width, p));
                owner.push(Some(i));
                if i == st.cursor && !desc.is_empty() {
                    lines.push(Line::from(Span::styled(
                        format!("    {desc}"),
                        Style::default().fg(p.quiet).bg(p.dialog),
                    )));
                    owner.push(Some(i));
                }
            }
            RootRow::Drill {
                label,
                desc,
                summary,
                ..
            } => {
                if i == st.cursor {
                    selected_line = lines.len();
                }
                let value = if summary.is_empty() {
                    "›".to_string()
                } else {
                    format!("{summary} ›")
                };
                lines.push(row_line(label, &value, quiet, i == st.cursor, width, p));
                owner.push(Some(i));
                if i == st.cursor && !desc.is_empty() {
                    lines.push(desc_line(desc, p));
                    owner.push(Some(i));
                }
            }
        }
    }
    let room = usize::from(body.height);
    if selected_line < st.scroll {
        st.scroll = selected_line;
    } else if selected_line + 2 >= st.scroll + room {
        st.scroll = (selected_line + 2).saturating_sub(room.saturating_sub(1));
    }
    st.scroll = st.scroll.min(lines.len().saturating_sub(room));
    let visible: Vec<Line<'static>> = lines.iter().skip(st.scroll).take(room).cloned().collect();
    for (n, who) in owner.iter().skip(st.scroll).take(room).enumerate() {
        if let Some(i) = who {
            st.hits
                .push((Rect::new(body.x, body.y + n as u16, body.width, 1), *i));
        }
    }
    frame.render_widget(Paragraph::new(visible).style(bg), body);
}

fn row_line(
    label: &str,
    value: &str,
    vstyle: Style,
    selected: bool,
    width: usize,
    p: &Palette,
) -> Line<'static> {
    let base = if selected {
        Style::default().bg(p.element).fg(p.text)
    } else {
        Style::default().bg(p.dialog).fg(p.text)
    };
    let marker = "▸ ";
    let label_style = if selected {
        base.add_modifier(Modifier::BOLD)
    } else {
        base
    };
    let tail = format!("{value} ");
    let used = marker.width() + label.width() + tail.width();
    let gap = " ".repeat(width.saturating_sub(used).max(1));
    Line::from(vec![
        Span::styled(marker, base.fg(p.quiet)),
        Span::styled(label.to_string(), label_style),
        Span::styled(gap, base),
        Span::styled(tail, vstyle.bg(base.bg.unwrap_or(p.dialog))),
    ])
}

fn desc_line(desc: &str, p: &Palette) -> Line<'static> {
    Line::from(Span::styled(
        format!("    {desc}"),
        Style::default().fg(p.quiet).bg(p.dialog),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::{KeyEventKind, KeyEventState};

    fn nav() -> crate::bridge::Nav {
        let items = [
            ("GENERAL", "", true),
            ("Appearance", "appearance", false),
            ("Layout", "layout", false),
            ("", "", true),
            ("CONFIGURE", "", true),
            ("Providers", "providers", false),
        ];
        crate::bridge::Nav {
            items: items
                .iter()
                .map(|(a, b, c)| (a.to_string(), b.to_string(), *c))
                .collect(),
            selected: 1,
        }
    }
    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent {
            code,
            modifiers: KeyModifiers::NONE,
            kind: KeyEventKind::Press,
            state: KeyEventState::NONE,
        }
    }
    fn toggles() -> [(&'static str, &'static str, bool); 2] {
        [
            ("animate_activity", "Animate activity", true),
            ("mono", "Monochrome", false),
        ]
    }

    #[test]
    fn rows_group_by_heading_and_inline_the_toggles() {
        let rows = build(&nav(), &toggles(), "", &Default::default());
        assert!(matches!(&rows[0], RootRow::Header(h) if h == "General"));
        assert!(matches!(
            &rows[1],
            RootRow::Toggle {
                key: "animate_activity",
                ..
            }
        ));
        assert!(rows
            .iter()
            .any(|r| matches!(r, RootRow::Drill { index: 5, .. })));
        let found = build(&nav(), &toggles(), "provid", &Default::default());
        assert_eq!(found.len(), 2);
        assert!(matches!(&found[0], RootRow::Header(h) if h == "Configure"));
    }

    #[test]
    fn keys_walk_open_search_and_escape_clears_then_closes() {
        let rows = build(&nav(), &toggles(), "", &Default::default());
        let mut st = RootState::default();
        st.track(true);
        assert_eq!(key(&mut st, &rows, press(KeyCode::Down)), RootAct::Done);
        assert_eq!(st.cursor, 2);
        assert_eq!(
            key(&mut st, &rows, press(KeyCode::Enter)),
            RootAct::Toggle("mono")
        );
        key(&mut st, &rows, press(KeyCode::Char('G')));
        assert_eq!(
            key(&mut st, &rows, press(KeyCode::Enter)),
            RootAct::Drill(5)
        );
        key(&mut st, &rows, press(KeyCode::Char('/')));
        assert!(st.searching);
        key(&mut st, &rows, press(KeyCode::Char('x')));
        assert_eq!(st.query, "x");
        key(&mut st, &rows, press(KeyCode::Esc));
        assert!(!st.searching && st.query.is_empty());
        assert_eq!(key(&mut st, &rows, press(KeyCode::Esc)), RootAct::Close);
    }

    #[test]
    fn host_overview_rows_are_inlined_cycle_and_are_searchable() {
        let mut snap = crate::bridge::Snapshot::default();
        snap.settings_overview = Some(serde_json::json!({
            "area": "overview", "title": "Settings", "blocks": [
                {"t": "heading", "text": "Appearance"},
                {"t": "row", "id": "theme", "label": "Theme", "description": "How it looks.", "control":
                    {"c": "segmented", "options": ["Dark", "Light"], "values": ["d", "l"], "active": 0,
                     "operation": {"kind": "sp_appearance", "area": "appearance", "key": "theme"}}},
                {"t": "heading", "text": "Layout"},
                {"t": "row", "id": "x", "label": "Details sidebar", "control":
                    {"c": "toggle", "on": true, "locked": "", "operation": {"kind": "sp_layout", "area": "layout", "key": "toggle"}}}],
            "summaries": {"keys": "24 shortcuts", "providers": "2 of 6 connected"}}));
        let mut nav = nav();
        nav.items.push(("Keyboard".into(), "keys".into(), false));
        let rows = build(&nav, &toggles(), "", &snap);
        assert!(!rows
            .iter()
            .any(|r| matches!(r, RootRow::Drill { label, .. } if label == "Appearance")));
        assert!(rows
            .iter()
            .any(|r| matches!(r, RootRow::Host { value, .. } if value == "Dark ›")));
        assert!(rows
            .iter()
            .any(|r| matches!(r, RootRow::Drill { summary, .. } if summary == "24 shortcuts")));
        assert!(rows
            .iter()
            .any(|r| matches!(r, RootRow::Drill { summary, .. } if summary == "2 of 6 connected")));
        let found = build(&nav, &toggles(), "sidebar", &snap);
        assert_eq!(found.len(), 2, "{found:?}");
        let mut st = RootState::default();
        st.track(true);
        st.cursor = rows
            .iter()
            .position(|r| matches!(r, RootRow::Host { .. }))
            .unwrap();
        match key(&mut st, &rows, press(KeyCode::Enter)) {
            RootAct::Send(op) => assert_eq!(op["value"], "l"),
            other => panic!("{other:?}"),
        }
    }
}

//! Chat shell, gallery, toasts and the overlay screens.
use crate::ctx::*;
use crate::fixture::*;
use crate::{sessions, settings};
use nexus_widgets::layout::{centered, inset};
use nexus_widgets::lists::*;
use nexus_widgets::theme::Level;
use nexus_widgets::*;
use ratatui::{layout::Rect, style::{Modifier, Style}};

pub struct ScreenDef {
    pub key: &'static str,
    pub title: &'static str,
    pub states: &'static [&'static str],
    pub draw: fn(&mut Ctx),
}

pub fn registry() -> Vec<ScreenDef> {
    let mut v = vec![
        ScreenDef { key: "gallery", title: "Component gallery", states: &["controls", "inputs & lists", "feedback"], draw: gallery },
        ScreenDef { key: "chat", title: "Chat", states: &["idle", "streaming", "permission", "question", "recording", "empty", "disconnected"], draw: chat },
        ScreenDef { key: "toasts", title: "Toasts", states: &["one of each", "stacked + more", "dedup ×2", "with action"], draw: toasts },
        ScreenDef { key: "sessions", title: "Sessions (docked)", states: &["default", "searching", "archived", "rename", "multi-select", "empty"], draw: sessions_docked },
        ScreenDef { key: "sessions-drawer", title: "Sessions (narrow drawer)", states: &["default", "searching"], draw: sessions_drawer },
    ];
    for (key, label) in AREAS {
        let k: &'static str = Box::leak(format!("settings-{key}").into_boxed_str());
        let t: &'static str = Box::leak(format!("Settings · {label}").into_boxed_str());
        let states: &'static [&'static str] = match *key {
            "providers" => &["default", "browser sign-in", "api key entry"],
            "models" => &["low tab", "medium tab", "high tab", "refreshing"],
            "agents" => &["tier mode", "specific model", "custom agent"],
            "keyboard" => &["default", "filtered"],
            _ => &["default"],
        };
        v.push(ScreenDef { key: k, title: t, states, draw: settings_screen });
    }
    v.push(ScreenDef { key: "settings-search", title: "Settings · search", states: &["voice", "model"], draw: settings_search });
    v.push(ScreenDef { key: "model-picker", title: "Model picker", states: &["default", "searching", "details"], draw: model_picker });
    v.push(ScreenDef { key: "palette", title: "Command palette", states: &["default", "settings"], draw: palette });
    v.push(ScreenDef { key: "confirm", title: "Confirm", states: &["danger"], draw: confirm });
    v.push(ScreenDef { key: "notifications", title: "Notifications", states: &["history"], draw: notifications });
    v.push(ScreenDef { key: "help", title: "Key sheet (?)", states: &["composer", "sessions", "settings"], draw: help });
    v
}

// ---------------------------------------------------------------- chat shell

fn top_bar(c: &mut Ctx, area: Rect) {
    let t = c.theme();
    let tabs_s = [("New Session", Dot::Ok), ("Fix token refresh", Dot::Work), ("Docs pass", Dot::Idle)];
    let mut x = area.x + 1;
    c.text(x, area.y, "▌ ", Style::default().fg(t.accent), 2);
    x += 3;
    for (i, (name, d)) in tabs_s.iter().enumerate() {
        let (g, col) = dot(c.ui, *d);
        let id = format!("tab:{i}");
        let w = (name.len() + 5) as u16;
        let r = c.ui.stop(&id, Rect::new(x, area.y, w, 1));
        let st = if i == 1 { t.text_style().add_modifier(Modifier::BOLD) } else { t.dim() };
        let st = if r.focused { st.add_modifier(Modifier::UNDERLINED) } else { st };
        c.text(x, area.y, g, Style::default().fg(col), 1);
        c.text(x + 2, area.y, name, st, w);
        x += w + 2;
    }
    c.text(x, area.y, "+", t.dim(), 1);
    let sid = c.ui.stop("top:sessions", Rect::new(area.x + area.width.saturating_sub(5), area.y, 4, 1));
    c.text(area.x + area.width.saturating_sub(5), area.y, "[≡]", if sid.focused { t.text_style().add_modifier(Modifier::REVERSED) } else { t.dim() }, 3);
    c.text(area.x + 1, area.y + 1, "~/repos/nexus  ·  main  ·  no worktree", t.dim(), area.width / 2);
    let status = if c.v.chat_state == 6 { "disconnected".to_string() } else { "build · sonnet-5.5 · 41%".to_string() };
    put_right(c.buf, area.x + area.width - 1, area.y + 1, &status, if c.v.chat_state == 6 { Style::default().fg(t.error) } else { t.dim() });
    c.text(area.x, area.y + 2, &c.ui.glyphs.rule.repeat(area.width as usize), Style::default().fg(t.border), area.width);
}

fn transcript(c: &mut Ctx, area: Rect) {
    let t = c.theme();
    let ctx_rows = [("System prompt", "2,140"), ("AGENTS.md", "3,880"), ("Tools · 24", "6,010"), ("Skills · 7", "1,120"), ("MCP · 3", "4,100")];
    let mut y = area.y;
    if c.v.chat_state == 5 {
        let m = centered(area, 56, 8);
        c.text(m.x, m.y, "Nexus", t.strong(Style::default().fg(t.accent)).add_modifier(Modifier::BOLD), 10);
        c.text(m.x, m.y + 2, "Ask anything, or try:", t.dim(), 40);
        for (i, (k, l)) in [("/model", "choose a model"), ("/sessions", "open the sessions sidebar"), ("/settings", "everything configurable"), ("Ctrl+P", "command palette")].iter().enumerate() {
            c.text(m.x + 2, m.y + 3 + i as u16, k, t.text_style().add_modifier(Modifier::BOLD), 12);
            c.text(m.x + 14, m.y + 3 + i as u16, l, t.dim(), 40);
        }
        return;
    }
    for (name, tok) in ctx_rows {
        c.text(area.x + 4, y, "◈", Style::default().fg(t.purple), 1);
        c.text(area.x + 6, y, name, t.text_style().add_modifier(Modifier::BOLD), 24);
        let dots = (area.width as usize).saturating_sub(40).min(40);
        c.text(area.x + 6 + name.len() as u16 + 1, y, &"·".repeat(dots.saturating_sub(name.len())), Style::default().fg(t.border_strong), dots as u16);
        put_right(c.buf, area.x + area.width - 3, y, &format!("{tok} tok"), t.dim());
        y += 1;
    }
    put_right(c.buf, area.x + area.width - 3, y, "Context total · ~17,250 tokens", t.dim());
    y += 2;
    let lines: Vec<(String, Style)> = vec![
        ("┃ Fix the token refresh race in auth/session.py".into(), Style::default().fg(t.accent)),
        ("".into(), t.text_style()),
        ("✓ Read 3 files · Ran 2 commands · Edited 1 file · Thought 2 times".into(), t.dim()),
        ("".into(), t.text_style()),
        ("The race happens because two refreshes can start before the first".into(), t.text_style()),
        ("stores its result. I serialised them with a lock and added a test.".into(), t.text_style()),
        ("".into(), t.text_style()),
        ("  ✓ pytest tests/test_auth.py::test_refresh_race   1 passed".into(), Style::default().fg(t.success)),
        ("  ✕ pytest tests/test_auth.py::test_refresh_expired  1 failed".into(), Style::default().fg(t.error)),
    ];
    for (l, st) in lines {
        if y >= area.y + area.height {
            break;
        }
        c.text(area.x + 4, y, &l, st, area.width.saturating_sub(6));
        y += 1;
    }
    if c.v.chat_state == 1 && y < area.y + area.height {
        spinner_row(c.buf, c.ui, Rect::new(area.x + 4, y + 1, 40, 1), "Thinking · editing auth/session.py · 12s");
    }
}

fn composer(c: &mut Ctx, area: Rect) {
    let t = c.theme();
    let st = c.v.chat_state;
    if st == 2 || st == 3 {
        // Inline prompt docked above the composer, focus lands on it.
        let h = if st == 2 { 7 } else { 7 };
        let p = Rect::new(area.x + 2, area.y.saturating_sub(h), area.width.saturating_sub(4), h);
        fill(c.buf, p, Style::default().bg(t.surface).fg(t.text));
        c.text(p.x + 1, p.y, "▌", Style::default().fg(t.warning), 1);
        if st == 2 {
            c.text(p.x + 3, p.y, "Permission · bash", t.text_style().add_modifier(Modifier::BOLD), 30);
            kv(c.buf, c.ui, Rect::new(p.x + 3, p.y + 1, p.width - 4, 1), "command", "pytest tests/test_auth.py -q", 12);
            kv(c.buf, c.ui, Rect::new(p.x + 3, p.y + 2, p.width - 4, 1), "cwd", "~/repos/nexus", 12);
            kv(c.buf, c.ui, Rect::new(p.x + 3, p.y + 3, p.width - 4, 1), "policy", "ask (writes outside workspace denied)", 12);
            let mut x = p.x + 3;
            for (i, (l, k)) in [("Allow once", ButtonKind::Primary), ("Allow for session", ButtonKind::Secondary), ("Deny", ButtonKind::Danger)].iter().enumerate() {
                button(c.buf, c.ui, x, p.y + 5, &Button::new(&format!("perm:{i}"), l, *k));
                x += button_width(l) + 1;
            }
        } else {
            c.text(p.x + 3, p.y, "Question · which test runner?", t.text_style().add_modifier(Modifier::BOLD), 40);
            let o = [RadioOption { label: "pytest", detail: "the repo's suite" }, RadioOption { label: "pytest -x", detail: "stop at the first failure" }, RadioOption { label: "ruff only", detail: "lint, no tests" }];
            radio_group(c.buf, c.ui, Rect::new(p.x + 3, p.y + 2, p.width - 4, 3), "question", &o, 0);
            c.text(p.x + 3, p.y + 6, "1–3 select · Enter confirm", t.dim(), 40);
        }
    }
    let box_ = Rect::new(area.x + 1, area.y, area.width - 2, 4);
    fill(c.buf, box_, Style::default().bg(t.surface));
    for k in 0..4 {
        c.text(box_.x, box_.y + k, "▎", Style::default().fg(t.accent).bg(t.surface), 1);
    }
    let r = c.ui.stop("composer", Rect::new(box_.x, box_.y, box_.width, 2));
    let placeholder = match st {
        6 => "Daemon unreachable — /reconnect to replay and reattach",
        4 => "● Listening…  Esc finishes · Enter sends",
        _ => "Ask anything…",
    };
    c.text(box_.x + 2, box_.y + 1, placeholder, if st == 6 { Style::default().fg(t.error).bg(t.surface) } else { t.dim().bg(t.surface) }, box_.width - 4);
    if r.focused {
        c.text(box_.x + 2, box_.y + 1, " ", Style::default().add_modifier(Modifier::REVERSED), 1);
    }
    let y = box_.y + 3;
    let mut x = box_.x + 2;
    for (i, (l, w)) in [("build", 7u16), ("sonnet-5.5", 12), ("medium", 8)].iter().enumerate() {
        let id = format!("composer:ctl{i}");
        select(c.buf, c.ui, x, y, *w + 4, &id, l);
        x += *w + 5;
    }
    let ctxl = "41% · 82k/200k";
    put_right(c.buf, box_.x + box_.width - 12, y, ctxl, t.dim().bg(t.surface));
    meter(c.buf, c.ui, box_.x + box_.width - 10, y, 8, 0.41, &[0.7, 0.9]);
    if st == 4 {
        c.text(box_.x + box_.width - 26, box_.y, "● rec 00:07 ▂▃▅▆▃▂", Style::default().fg(t.error).bg(t.surface), 24);
    }
}

fn chat(c: &mut Ctx) {
    let a = c.area;
    c.base(a);
    top_bar(c, Rect::new(a.x, a.y, a.width, 3));
    let narrow = a.width < 90;
    let side_w = if c.v.sidebar && !narrow { 40u16.min(a.width / 3) } else { 0 };
    let body = Rect::new(a.x + side_w, a.y + 3, a.width - side_w, a.height.saturating_sub(3 + 5 + c.w_hint_rows()));
    if side_w > 0 {
        sessions::panel(c, Rect::new(a.x, a.y + 3, side_w, a.height.saturating_sub(3 + c.w_hint_rows())));
    }
    transcript(c, body);
    composer(c, Rect::new(a.x + side_w, a.y + a.height - 5 - c.w_hint_rows(), a.width - side_w, 5));
    if c.w.key_hints {
        let y = a.y + a.height - 1;
        let hs: &[(&str, &str)] = &[("Enter", "send"), ("Shift+Enter", "newline"), ("Ctrl+B", "sessions"), ("Ctrl+L", "details"), ("F6", "next pane"), ("Ctrl+P", "palette"), ("?", "keys")];
        key_hints(c.buf, c.ui, Rect::new(a.x, y, a.width, 1), hs);
    }
    if c.v.sidebar && narrow {
        let w = 44.min(a.width * 9 / 10);
        sessions::panel(c, Rect::new(a.x, a.y + 3, w, a.height.saturating_sub(4)));
    }
    toast_layer(c);
}

impl<'a, 't> Ctx<'a, 't> {
    pub fn w_hint_rows(&self) -> u16 {
        self.w.key_hints as u16
    }
}

/// Toasts float at the top-right of the conversation area, under the top bar.
pub fn toast_layer(c: &mut Ctx) {
    let a = c.area;
    let area = Rect::new(a.x, a.y + 3, a.width, a.height.saturating_sub(8));
    let area = if c.w.toast_top == 1 { Rect::new(a.x, a.y + a.height.saturating_sub(16), a.width, 10) } else { area };
    toast_stack(c.buf, c.ui, area, c.toasts);
}

fn sessions_docked(c: &mut Ctx) {
    c.v.sidebar = true;
    chat(c);
}
fn sessions_drawer(c: &mut Ctx) {
    c.v.sidebar = true;
    let a = c.area;
    c.base(a);
    let narrow = Rect::new(a.x, a.y, a.width.min(80), a.height.min(24));
    let saved = c.area;
    c.area = narrow;
    chat(c);
    c.area = saved;
}

// ---------------------------------------------------------------- toasts

fn toasts(c: &mut Ctx) {
    c.v.chat_state = 0;
    chat(c);
}

// ---------------------------------------------------------------- settings

fn settings_screen(c: &mut Ctx) {
    let a = c.area;
    c.base(a);
    let w = (a.width.saturating_sub(4)).min(120);
    let h = a.height.saturating_sub(2);
    let r = centered(a, w, h);
    settings::draw(c, r);
    toast_layer(c);
}
fn settings_search(c: &mut Ctx) {
    let a = c.area;
    c.base(a);
    let r = centered(a, a.width.saturating_sub(4).min(120), a.height.saturating_sub(2));
    let t = c.theme();
    let (inner, hint) = modal(c.buf, c.ui, r, &Modal { title: "Settings", id: "settings:close" });
    let sf = c.v.settings_search.clone();
    search_field(c.buf, c.ui, Rect::new(inner.x + 1, inner.y, 48.min(inner.width - 2), 1), "settings:search", &sf, None);
    c.text(inner.x, inner.y + 1, &c.ui.glyphs.rule.repeat(inner.width as usize), Style::default().fg(t.border), inner.width);
    settings::search_page(c, Rect::new(inner.x, inner.y + 2, inner.width, inner.height.saturating_sub(3)));
    key_hints(c.buf, c.ui, hint, &[("↑↓", "move"), ("Enter", "jump to setting"), ("Esc", "clear search")]);
}

// ---------------------------------------------------------------- overlays

fn dim_backdrop(c: &mut Ctx) {
    let a = c.area;
    c.base(a);
    let saved = c.v.chat_state;
    c.v.chat_state = 0;
    chat(c);
    c.v.chat_state = saved;
}

fn model_picker(c: &mut Ctx) {
    dim_backdrop(c);
    let t = c.theme();
    let r = centered(c.area, 76.min(c.area.width - 2), 22.min(c.area.height - 2));
    let (inner, hint) = modal(c.buf, c.ui, r, &Modal { title: "Select model", id: "picker:close" });
    let q = c.v.picker_q.clone();
    search_field(c.buf, c.ui, Rect::new(inner.x, inner.y, inner.width, 1), "picker:search", &q, None);
    let needle = c.v.picker_q.value.to_lowercase();
    let mut items = vec![];
    let mut last = String::new();
    for m in &c.w.models {
        if !needle.is_empty() && !m.contains(&needle) {
            continue;
        }
        let (prov, name) = m.split_once('/').unwrap();
        let group = if prov != last { last = prov.to_string(); prov.to_string() } else { prov.to_string() };
        items.push(ListItem { id: format!("model:{m}"), group, title: name.to_string(), sub: String::new(), meta: if m.ends_with("5-5") { "● current".into() } else { String::new() }, dot: None, active: m.ends_with("5-5"), actions: vec![] });
    }
    let mut ls = ListState { selected: c.v.sess_sel.min(items.len().saturating_sub(1)), scroll: 0 };
    let la = Rect::new(inner.x, inner.y + 2, inner.width, inner.height.saturating_sub(if c.state == 2 { 9 } else { 3 }));
    list(c.buf, c.ui, la, &items, &mut ls);
    if c.state == 2 {
        let d = Rect::new(inner.x + 1, inner.y + inner.height - 7, inner.width - 2, 7);
        c.text(d.x, d.y, &c.ui.glyphs.rule.repeat(d.width as usize), Style::default().fg(t.border), d.width);
        for (i, (k, v)) in [("Model", "anthropic/claude-sonnet-5-5"), ("Context", "200,000 tokens · output 64,000"), ("Pricing", "$3.00 in · $15.00 out per 1M tokens"), ("Reasoning", "low · medium · high"), ("Source", "models.dev · refreshed 3h ago")].iter().enumerate() {
            kv(c.buf, c.ui, Rect::new(d.x, d.y + 1 + i as u16, d.width, 1), k, v, 12);
        }
    }
    key_hints(c.buf, c.ui, hint, &[("↑↓", "move"), ("Enter", "select"), ("Ctrl+I", "details"), ("Ctrl+F", "favourite"), ("Ctrl+S", "sort"), ("Ctrl+R", "refresh"), ("Esc", "close")]);
}

fn palette(c: &mut Ctx) {
    dim_backdrop(c);
    let r = centered(c.area, 70.min(c.area.width - 2), 20.min(c.area.height - 2));
    let (inner, hint) = modal(c.buf, c.ui, r, &Modal { title: "Command palette", id: "palette:close" });
    let q = if c.state == 1 { TextState::new("voice") } else { c.v.palette_q.clone() };
    search_field(c.buf, c.ui, Rect::new(inner.x, inner.y, inner.width, 1), "palette:search", &q, None);
    let rows: Vec<(&str, &str, &str, &str)> = if c.state == 1 {
        vec![("Settings", "Voice input", "Settings › Voice & speech", ""), ("Settings", "Send transcript automatically", "Settings › Voice & speech", ""), ("Commands", "/voice", "dictation status", ""), ("Settings", "Recording limit", "Settings › Voice & speech", "")]
    } else {
        vec![("Commands", "/model", "choose a model", "Ctrl+X M"), ("Commands", "/sessions", "open the sessions sidebar", "Ctrl+B"), ("Commands", "/settings", "open Settings", ""), ("Commands", "/context", "assembled prompt and accounting", "Ctrl+I"), ("Sessions", "Fix token refresh race", "~/repos/nexus · running", ""), ("Agents", "build", "root agent", "Shift+Tab")]
    };
    let items: Vec<ListItem> = rows.iter().map(|r| ListItem { id: format!("pal:{}", r.1), group: r.0.into(), title: r.1.into(), sub: String::new(), meta: if r.3.is_empty() { r.2.into() } else { r.3.into() }, dot: None, active: false, actions: vec![] }).collect();
    let mut ls = ListState::default();
    list(c.buf, c.ui, Rect::new(inner.x, inner.y + 2, inner.width, inner.height.saturating_sub(3)), &items, &mut ls);
    key_hints(c.buf, c.ui, hint, &[("↑↓", "move"), ("Enter", "run"), ("Esc", "close")]);
}

fn confirm(c: &mut Ctx) {
    dim_backdrop(c);
    let r = centered(c.area, 58.min(c.area.width - 2), 11);
    let t = c.theme();
    let (inner, hint) = modal(c.buf, c.ui, r, &Modal { title: "Delete archived session?", id: "confirm:close" });
    c.text(inner.x + 2, inner.y + 1, "“Archived experiment 7” and its 14 turns will be removed.", t.text_style(), inner.width - 4);
    c.text(inner.x + 2, inner.y + 3, "This cannot be undone. Exported copies are not affected.", t.dim(), inner.width - 4);
    button(c.buf, c.ui, inner.x + 2, inner.y + 5, &Button::new("confirm:cancel", "Cancel", ButtonKind::Secondary));
    button(c.buf, c.ui, inner.x + 14, inner.y + 5, &Button::new("confirm:ok", "Delete", ButtonKind::Danger));
    key_hints(c.buf, c.ui, hint, &[("y", "confirm"), ("n", "cancel"), ("Esc", "cancel")]);
}

fn notifications(c: &mut Ctx) {
    dim_backdrop(c);
    let t = c.theme();
    let r = centered(c.area, 84.min(c.area.width - 2), 22.min(c.area.height - 2));
    let (inner, hint) = modal(c.buf, c.ui, r, &Modal { title: "Notifications · this window", id: "notif:close" });
    let hist: Vec<_> = c.toasts.history.iter().rev().collect();
    if hist.is_empty() {
        empty_state(c.buf, c.ui, inner, "notif:empty", "No notifications yet.", "");
    }
    for (i, h) in hist.iter().take(inner.height as usize).enumerate() {
        let y = inner.y + i as u16;
        let col = h.level.color(t);
        c.text(inner.x + 1, y, "▌", Style::default().fg(col), 1);
        c.text(inner.x + 3, y, h.level.label(), Style::default().fg(col), 8);
        c.text(inner.x + 12, y, &h.title, t.text_style(), inner.width / 2);
        put_right(c.buf, inner.x + inner.width - 2, y, &truncate(&h.body, (inner.width / 2 - 4) as usize, c.ui.glyphs.ellipsis), t.dim());
    }
    key_hints(c.buf, c.ui, hint, &[("↑↓", "move"), ("Enter", "run action"), ("c", "copy"), ("Esc", "close")]);
}

fn help(c: &mut Ctx) {
    dim_backdrop(c);
    let t = c.theme();
    let r = centered(c.area, 76.min(c.area.width - 2), 22.min(c.area.height - 2));
    let scope = ["Composer", "Sessions", "Settings"][c.state];
    let (inner, hint) = modal(c.buf, c.ui, r, &Modal { title: &format!("Keys · {scope}"), id: "help:close" });
    let rows: Vec<Vec<String>> = settings::KEYMAP.iter().filter(|k| k.2 == scope || k.2 == "Global" || k.2 == "Lists" || k.2 == "Overlays").map(|k| vec![k.0.into(), k.1.into(), k.2.into()]).collect();
    table(c.buf, c.ui, Rect::new(inner.x, inner.y, inner.width, inner.height.saturating_sub(1)), "help", &[("Action", 0), ("Keys", 24), ("Where", 14)], &rows, usize::MAX, 0);
    c.text(inner.x + 2, inner.y + inner.height - 1, "All shortcuts → Settings › Keyboard", t.dim(), inner.width);
    key_hints(c.buf, c.ui, hint, &[("Esc", "close")]);
}

// ---------------------------------------------------------------- gallery

fn gallery(c: &mut Ctx) {
    let a = c.area;
    c.base(a);
    let t = c.theme();
    let mut sc = c.v.page_scroll;
    let state = c.state;
    let scroll_area = Rect::new(a.x, a.y, a.width, a.height.saturating_sub(1));
    scrolled(c.buf, c.ui, scroll_area, &mut sc, |buf, ui, r| {
        let mut y = 0u16;
        let x = 2u16;
        let head = |buf: &mut ratatui::buffer::Buffer, ui: &Ui, y: &mut u16, s: &str| {
            *y += 1;
            put(buf, x, *y, s, ui.theme.strong(Style::default().fg(ui.theme.muted).add_modifier(Modifier::BOLD)), 80);
            *y += 1;
        };
        let _ = (inset, r);
        match state {
            0 => {
                head(buf, ui, &mut y, "BUTTON · primary · secondary · danger · ghost · disabled · loading · focused");
                let mut bx = x;
                for (i, (l, k)) in [("Primary", ButtonKind::Primary), ("Secondary", ButtonKind::Secondary), ("Danger", ButtonKind::Danger), ("Ghost", ButtonKind::Ghost)].iter().enumerate() {
                    button(buf, ui, bx, y, &Button::new(&format!("g:b{i}"), l, *k));
                    bx += button_width(l) + 1;
                }
                let mut d = Button::new("g:dis", "Disabled", ButtonKind::Secondary);
                d.disabled = true;
                button(buf, ui, bx, y, &d);
                bx += 13;
                let mut l = Button::new("g:load", "Loading", ButtonKind::Primary);
                l.loading = true;
                button(buf, ui, bx, y, &l);
                bx += 11;
                icon_button(buf, ui, bx, y, "g:ic1", ui.glyphs.close, false);
                icon_button(buf, ui, bx + 4, y, "g:ic2", ui.glyphs.up, false);
                y += 2;
                head(buf, ui, &mut y, "TOGGLE · on · off · locked   (state in text, not only colour)");
                for (i, (on, lock)) in [(true, false), (false, false), (true, true)].iter().enumerate() {
                    toggle(buf, ui, Rect::new(x, y, 50, 1), &Toggle { id: &format!("g:t{i}"), label: ["Voice input", "Auto-send", "Tool: bash"][i], on: *on, locked: lock.then_some("locked") });
                    y += 1;
                }
                head(buf, ui, &mut y, "CHECKBOX · off · on · mixed        RADIO");
                for (i, s) in [Check::Off, Check::On, Check::Mixed].iter().enumerate() {
                    checkbox(buf, ui, Rect::new(x, y, 28, 1), &format!("g:c{i}"), ["Connected", "Not connected", "Some tools"][i], *s);
                    y += 1;
                }
                let ro = [RadioOption { label: "Tier", detail: "automatic" }, RadioOption { label: "Specific model", detail: "ordered" }];
                radio_group(buf, ui, Rect::new(34, y - 3, 44, 2), "g:r", &ro, 0);
                head(buf, ui, &mut y, "SEGMENTED · TABS · STEPPER");
                segmented(buf, ui, x, y, "g:seg", &["Dark", "Light", "System"], 1);
                stepper(buf, ui, 34, y, "g:step", "60 s");
                y += 2;
                tabs(buf, ui, Rect::new(x, y, 50, 2), "g:tabs", &[Tab { label: "Low", badge: "" }, Tab { label: "Medium", badge: "" }, Tab { label: "High", badge: "!" }], 1);
                y += 3;
            }
            1 => {
                head(buf, ui, &mut y, "TEXT INPUT · plain · secret · placeholder");
                let a1 = TextState::new("~/repos/nexus");
                let a2 = TextState::new("sk-ant-secret");
                let a3 = TextState::default();
                text_input(buf, ui, x, y, 32, &TextInput { id: "g:i1", state: &a1, placeholder: "", secret: false, error: "", editing: false });
                text_input(buf, ui, x + 34, y, 24, &TextInput { id: "g:i2", state: &a2, placeholder: "", secret: true, error: "", editing: false });
                text_input(buf, ui, x + 60, y, 24, &TextInput { id: "g:i3", state: &a3, placeholder: "paste key", secret: false, error: "", editing: false });
                y += 2;
                head(buf, ui, &mut y, "SELECT · closed          SEARCH FIELD");
                select(buf, ui, x, y, 26, "g:sel", "auto");
                search_field(buf, ui, Rect::new(34, y, 40, 1), "g:search", &TextState::new("refr"), Some((3, 41)));
                y += 2;
                head(buf, ui, &mut y, "ORDERED LIST · in use · fallback · not connected");
                let items = [OrderedItem { label: "anthropic/claude-sonnet-5-5", note: "" }, OrderedItem { label: "openai/gpt-6-mini", note: "" }, OrderedItem { label: "google/gemini-3-pro", note: "not connected" }];
                ordered_list(buf, ui, Rect::new(x, y, 84, 4), "g:ol", &items, "Add model…");
                y += 5;
                head(buf, ui, &mut y, "SECTION · open · closed · error");
                section(buf, ui, Rect::new(x, y, 84, 1), "g:s1", "OpenAI", "● connected · API key ••••3f2a · 34 models", Some(ui.theme.success), true);
                section(buf, ui, Rect::new(x, y + 1, 84, 1), "g:s2", "Google", "○ not connected", None, false);
                section(buf, ui, Rect::new(x, y + 2, 84, 1), "g:s3", "OpenCode Go", "✕ error · token expired", Some(ui.theme.error), false);
                y += 4;
                head(buf, ui, &mut y, "SETTING ROW · label · control · scope · description");
                setting_row(buf, ui, Rect::new(x, y, 84, 2), "g:row", "Send transcript automatically", "Off lets you review the transcript first.", "global", TOGGLE_W, |b, ui, cr, f| toggle_view(b, ui, cr.x + cr.width - TOGGLE_W, cr.y, false, false, f));
                y += 3;
                head(buf, ui, &mut y, "KEY / VALUE · TABLE");
                kv(buf, ui, Rect::new(x, y, 60, 1), "Account", "you@example.com · Max plan", 14);
                y += 2;
                table(buf, ui, Rect::new(x, y, 84, 5), "g:tbl", &[("Action", 0), ("Keys", 20)], &[vec!["Sessions".into(), "Ctrl+B".into()], vec!["Details".into(), "Ctrl+L".into()], vec!["Palette".into(), "Ctrl+P".into()]], 1, 0);
                y += 6;
            }
            _ => {
                head(buf, ui, &mut y, "METER · PROGRESS · SPINNER · STATUS DOTS");
                meter(buf, ui, x, y, 30, 0.41, &[0.7, 0.9]);
                meter(buf, ui, x + 34, y, 30, 0.93, &[0.7, 0.9]);
                y += 1;
                progress(buf, ui, Rect::new(x, y, 70, 1), 0.46, "82/179 MB");
                y += 1;
                spinner_row(buf, ui, Rect::new(x, y, 50, 1), "Waiting for the browser…");
                y += 1;
                let mut sx = x;
                for (d, l) in [(Dot::Ok, "ok"), (Dot::Work, "working"), (Dot::Idle, "idle"), (Dot::Err, "error")] {
                    let (g, c) = dot(ui, d);
                    put(buf, sx, y, &format!("{g} {l}"), Style::default().fg(c), 12);
                    sx += 12;
                }
                y += 2;
                head(buf, ui, &mut y, "CALLOUT · persistent conditions");
                for (i, (lv, tx, ac)) in [(Level::Info, "Updates are checked daily.", ""), (Level::Warning, "Google is not connected — models from it are skipped.", "Connect"), (Level::Error, "postgres failed to start (exit 1).", "Logs")].iter().enumerate() {
                    callout(buf, ui, Rect::new(x, y, 84, 1), &format!("g:co{i}"), *lv, tx, ac);
                    y += 1;
                }
                head(buf, ui, &mut y, "BADGES · scope colours");
                let mut bx = x + 10;
                for s in ["global", "project", "session", "built-in", "edited"] {
                    bx += width(s) as u16 + 2;
                    badge(buf, ui, bx, y, s, scope_color(ui, s));
                }
                y += 2;
                head(buf, ui, &mut y, "EMPTY STATE · KEY HINTS");
                empty_state(buf, ui, Rect::new(x, y, 84, 5), "g:empty", "No archived sessions.", "Show active");
                y += 6;
                key_hints(buf, ui, Rect::new(x, y, 84, 1), &[("↑↓", "move"), ("←→", "tier"), ("Space", "toggle"), ("Alt+↑↓", "reorder"), ("Enter", "open"), ("/", "search"), ("?", "keys"), ("Esc", "close")]);
                y += 2;
                head(buf, ui, &mut y, "TOASTS: see the Toasts screen · MODAL: see Confirm · SCROLLBAR: this page");
            }
        }
        y + 2
    });
    c.v.page_scroll = sc;
    let _ = t;
    let hint = Rect::new(a.x, a.y + a.height - 1, a.width, 1);
    key_hints(c.buf, c.ui, hint, &[("↑↓", "move focus"), ("Tab", "next stop"), ("F3", "next group"), ("F4", "theme"), ("F5", "size"), ("F7", "ASCII")]);
}

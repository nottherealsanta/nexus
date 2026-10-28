//! Ratatui drawing of the shell. Everything clickable is registered in the
//! `HitMap` at the moment it is drawn, from the same rect (see `layout`).
//! Colours and glyphs come from `theme`; labels, keys and reasons from the
//! manifest. The look follows INTERFACE_PLAN.md "Design language"; the old
//! Nexus TUI (`rust/tui/src/render/`) is the visual reference.

use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::Line;
use ratatui::Frame;
use serde_json::Value;

use crate::app::{now_secs, App, WaitKind};
use crate::layout::{contains, Flags, Heights, HitMap, Regions};
use crate::state::ConnectionState;
use crate::text::{self, truncate, truncate_middle, truncate_start, width};
use crate::theme::Theme;
use crate::ui::*;

const CANVAS: &str = "surface.canvas";
const PANEL: &str = "surface.panel";
const RAISED: &str = "surface.raised";
const SIDEBAR: &str = "surface.sidebar";
const PRIMARY: &str = "text.primary";
const SECONDARY: &str = "text.secondary";
const MUTED: &str = "text.muted";
const ACCENT: &str = "interactive.accent";

struct Cx<'a> {
    app: &'a App,
    t: &'a Theme,
}

impl Cx<'_> {
    fn st(&self, fg: &str, bg: &str) -> Style {
        self.t.style(fg, bg)
    }
    fn ell(&self) -> &str {
        self.t.structure("ellipsis")
    }
    fn sep(&self) -> String {
        format!(" {} ", self.t.structure("separator"))
    }
    fn hovered(&self, r: Rect) -> bool {
        self.app.mouse.is_some_and(|(x, y)| contains(r, x, y))
    }
    fn level(&self, l: Level) -> (&str, &str) {
        match l {
            Level::Info => ("i", "status.info"),
            Level::Success => (self.t.status("done").0, "status.success"),
            Level::Warning => ("!", "status.warning"),
            Level::Error => (self.t.status("error").0, "status.error"),
        }
    }
}

fn clear(buf: &mut Buffer, r: Rect, style: Style) {
    let r = r.intersection(buf.area);
    for y in r.y..r.y + r.height {
        for x in r.x..r.x + r.width {
            let c = &mut buf[(x, y)];
            c.reset();
            c.set_style(style);
        }
    }
}

fn put(buf: &mut Buffer, x: u16, y: u16, s: &str, style: Style, max_w: u16) -> u16 {
    if max_w == 0 || y >= buf.area.y + buf.area.height {
        return x;
    }
    let (nx, _) = buf.set_stringn(x, y, s, max_w as usize, style);
    nx
}

fn json_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// `$HOME` shortened to `~` for paths shown to the user.
fn short_home(path: &str) -> String {
    let home = std::env::var("HOME").ok();
    home.as_deref()
        .filter(|home| {
            path == *home
                || path
                    .strip_prefix(*home)
                    .is_some_and(|rest| rest.starts_with('/'))
        })
        .map(|home| format!("~{}", &path[home.len()..]))
        .unwrap_or_else(|| path.to_owned())
}

// ============================================================================
// entry point
// ============================================================================

/// Draw one frame and rebuild the hit map from it.
pub fn render(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    app.size = (area.width, area.height);
    let flags = Flags {
        left_open: app.ui.left_open,
        right_open: app.ui.right_open,
        drawer: app.ui.drawer,
        left_rail: app.ui.left_rail,
        left_width: app.ui.left_width,
    };
    let regions = {
        let a: &App = app;
        Regions::compute(area, flags, |w| heights(a, w))
    };
    let mut hits = HitMap::default();
    let mut metrics = (0usize, 0usize);
    let timeline = {
        let a: &App = app;
        Some(crate::timeline::build(
            a,
            &a.theme,
            a.frame,
            regions.timeline.width.saturating_sub(2) as usize,
        ))
    };
    {
        let a: &App = app;
        let cx = Cx {
            app: a,
            t: &a.theme,
        };
        let buf = frame.buffer_mut();
        clear(buf, area, cx.st(PRIMARY, CANVAS));
        draw_header(&cx, buf, &regions, &mut hits);
        if let Some(l) = regions.left {
            draw_left(
                &cx,
                buf,
                Rect::new(l.x, l.y, l.width.saturating_sub(1), l.height),
                &mut hits,
            );
            draw_sidebar_edge(&cx, buf, l.right().saturating_sub(1), l);
        }
        if let Some(r) = regions.right {
            draw_right(
                &cx,
                buf,
                Rect::new(r.x + 1, r.y, r.width.saturating_sub(1), r.height),
                &mut hits,
            );
            draw_sidebar_edge(&cx, buf, r.x, r);
        }
        draw_banner(&cx, buf, regions.banner, &mut hits);
        // The session stays drawn under the settings modal.
        if a.ui.file_open {
            draw_file_view(&cx, buf, regions.timeline);
        } else if let Some(t) = &timeline {
            metrics = draw_timeline(&cx, buf, regions.timeline, &mut hits, t);
        }
        draw_waiting(&cx, buf, regions.waiting, &mut hits);
        draw_composer(&cx, buf, regions.composer, &mut hits);
        if let Some(l) = regions
            .left
            .filter(|l| l.width <= crate::layout::RAIL_WIDTH)
        {
            draw_rail_popup(&cx, buf, l, regions.centre);
        }
        if a.ui.view == View::Settings {
            draw_settings_modal(&cx, buf, area, &mut hits);
        }
        draw_toasts(&cx, buf, &regions, &mut hits);
        if let Some((side, r)) = regions.drawer {
            // scrim: clicks outside the drawer close it
            hits.add(regions.centre, Target::Scrim);
            match side {
                Side::Left => draw_left(&cx, buf, r, &mut hits),
                Side::Right => draw_right(&cx, buf, r, &mut hits),
            }
            // a hairline marks the drawer's inner edge over the centre column
            let ex = match side {
                Side::Left => r.right().saturating_sub(1),
                Side::Right => r.x,
            };
            for y in r.y..r.bottom() {
                put(buf, ex, y, cx.t.structure("rail"), cx.st(MUTED, PANEL), 1);
            }
        }
        if let Some(o) = &a.ui.overlay {
            draw_overlay(&cx, buf, area, o, &mut hits);
        }
        if let Some(d) = &a.ui.dialog {
            draw_dialog(
                &cx,
                buf,
                area,
                &d.title,
                &d.body,
                Some((&d.cancel, &d.confirm, d.danger, d.button)),
                &mut hits,
            );
        } else if let Some((title, body)) = a.blocking() {
            draw_dialog(&cx, buf, area, &title, &body, None, &mut hits);
        }
    }
    // The terminal cursor shows where typing goes.
    if let Some(p) = cursor_position(app, &regions) {
        frame.set_cursor_position(p);
    }
    app.hits = hits;
    app.regions = regions;
    app.ui.timeline_total = metrics.0;
    app.ui.timeline_height = metrics.1;
    if let Some(t) = timeline {
        app.ui.timeline_blocks = t.blocks;
        let n = app.ui.timeline_blocks.len();
        // A fold changes line ranges and can add or remove blocks; keep the
        // selection on the same key rather than the same index.
        if let Some(key) = app.ui.timeline_sel_key.clone() {
            if let Some(i) = app.ui.timeline_blocks.iter().position(|b| b.key == key) {
                app.ui.timeline_sel = Some(i);
            }
        }
        app.ui.timeline_sel = app.ui.timeline_sel.map(|i| i.min(n.saturating_sub(1)));
    }
    // keep the stored scroll honest after clamping
    if !app.ui.scroll.follow {
        let max_top = metrics.0.saturating_sub(metrics.1);
        if app.ui.scroll.top >= max_top {
            app.ui.scroll = Scroll::default();
        }
    }
}

/// Render to a text grid (one string per row), for tests and frame dumps.
pub fn render_text(app: &mut App, w: u16, h: u16) -> Vec<String> {
    use ratatui::backend::TestBackend;
    let mut term = ratatui::Terminal::new(TestBackend::new(w, h)).expect("test terminal");
    term.draw(|f| render(f, app)).expect("draw");
    let buf = term.backend().buffer().clone();
    buffer_rows(&buf)
}

pub fn buffer_rows(buf: &Buffer) -> Vec<String> {
    (0..buf.area.height)
        .map(|y| {
            let mut s = String::new();
            let mut x = 0;
            while x < buf.area.width {
                let c = &buf[(x, y)];
                let sym = c.symbol();
                s.push_str(sym);
                x += (text::width(sym) as u16).max(1);
            }
            s.trim_end().to_owned()
        })
        .collect()
}

fn cursor_position(app: &App, g: &Regions) -> Option<Position> {
    if app.ui.dialog.is_some() || app.blocking().is_some() {
        return None;
    }
    if app.ui.view == View::Settings {
        if app.ui.overlay.is_some() {
            return None;
        }
        let b = settings_box(g.area);
        let col = text::width(&app.ui.settings_search.text()) as u16;
        return Some(Position::new(
            b.x + 3 + col.min(b.width.saturating_sub(7)),
            b.y + 3,
        ));
    }
    if let Some(Overlay {
        kind: OverlayKind::Palette(p),
        ..
    }) = &app.ui.overlay
    {
        let b = palette_box(g.area);
        let (_, (_, c)) = p.query.layout(b.width.saturating_sub(6) as usize);
        return Some(Position::new(b.x + 4 + c as u16, b.y + 1));
    }
    if app.ui.overlay.is_some() {
        return None;
    }
    if app.ui.edit.is_some() {
        return None;
    }
    if app.ui.focus != Focus::Composer || !app.writable() {
        return None;
    }
    let inner = composer_inner(g.composer);
    let (rows, (cr, cc)) = app.composer_ref().layout(inner.width as usize);
    let first = app.ui.composer_drag.map_or_else(
        || visible_first(rows.len(), cr, inner.height as usize),
        |(_, top)| top,
    );
    if cr < first || cr - first >= inner.height as usize {
        return None;
    }
    Some(Position::new(
        inner.x + cc as u16,
        inner.y + (cr - first) as u16,
    ))
}

pub(crate) fn visible_first(total: usize, cursor_row: usize, h: usize) -> usize {
    if total <= h {
        0
    } else {
        (cursor_row + 1).saturating_sub(h).min(total - h)
    }
}

// ============================================================================
// heights
// ============================================================================

pub(crate) fn composer_inner(r: Rect) -> Rect {
    Rect::new(
        r.x + 3,
        r.y + 1,
        r.width.saturating_sub(6),
        r.height.saturating_sub(5),
    )
}

fn heights(app: &App, w: u16) -> Heights {
    let banner = u16::from(!app.banners().is_empty());
    let inner_w = w.saturating_sub(6) as usize;
    let rows = app.composer_ref().layout(inner_w).0.len().clamp(1, 8) as u16;
    let (waiting, composer) = (waiting_height(app), rows + 5);
    Heights {
        banner,
        waiting,
        composer,
    }
}

/// Option rows the question card shows at most; the cursor scrolls the rest.
const ASK_ROWS: usize = 5;

/// Rows between the question text and the hint line: the options, or the
/// one-line prompt of a free-text question.
fn question_rows(app: &App) -> usize {
    app.ask_current()
        .map_or(1, |c| c.options().len().clamp(1, ASK_ROWS))
}

fn waiting_height(app: &App) -> u16 {
    match app.wait_focused() {
        None => 0,
        Some(w) => match w.kind {
            WaitKind::Approval => 6,
            WaitKind::Question => 4 + question_rows(app) as u16,
        },
    }
}

// ============================================================================
// header
// ============================================================================

fn toggle_glyph(cx: &Cx, side: Side) -> String {
    let open = match side {
        Side::Left => {
            if cx.app.narrow() {
                cx.app.ui.drawer == Some(Side::Left)
            } else {
                cx.app.ui.left_open
            }
        }
        Side::Right => {
            if cx.app.narrow() {
                cx.app.ui.drawer == Some(Side::Right)
            } else {
                cx.app.ui.right_open
            }
        }
    };
    cx.t.sidebar_glyph(match (side, open) {
        (Side::Left, true) => "left_open",
        (Side::Left, false) => "left_closed",
        (Side::Right, true) => "right_open",
        (Side::Right, false) => "right_closed",
    })
    .to_owned()
}

fn draw_header(cx: &Cx, buf: &mut Buffer, g: &Regions, hits: &mut HitMap) {
    let base = cx.st(SECONDARY, CANVAS);
    clear(buf, g.header, base);
    let focus_bar = cx.t.structure("focus_bar");
    for (side, rect, focus) in [
        (Side::Left, g.toggle_left, Focus::ToggleLeft),
        (Side::Right, g.toggle_right, Focus::ToggleRight),
    ] {
        let focused = cx.app.ui.focus == focus;
        let mut style = cx.st(PRIMARY, CANVAS);
        if cx.hovered(rect) {
            style = style.patch(cx.t.bg("interactive.hover"));
        }
        let glyph = toggle_glyph(cx, side);
        let gw = width(&glyph) as u16;
        // leading toggle: bar, glyph; trailing toggle: glyph, bar (so the glyph
        // sits at the very edge)
        match side {
            Side::Left => {
                if focused {
                    put(
                        buf,
                        rect.x,
                        rect.y,
                        focus_bar,
                        cx.st("interactive.focus", CANVAS)
                            .add_modifier(Modifier::BOLD),
                        2,
                    );
                }
                put(buf, rect.x + 1, rect.y, &glyph, style, rect.width - 1);
            }
            Side::Right => {
                let gx = rect.x + rect.width.saturating_sub(gw + 1);
                put(buf, gx, rect.y, &glyph, style, gw);
                if focused {
                    put(
                        buf,
                        gx + gw,
                        rect.y,
                        focus_bar,
                        cx.st("interactive.focus", CANVAS)
                            .add_modifier(Modifier::BOLD),
                        1,
                    );
                }
            }
        }
        hits.add(
            rect,
            if side == Side::Left {
                Target::ToggleLeft
            } else {
                Target::ToggleRight
            },
        );
    }

    // bell: only with unread notices
    let unread = cx.app.ui.alerts.unread();
    if unread > 0 {
        let s = format!("{} {unread}", cx.t.attention("unread").0);
        let x = g.bell.x + g.bell.width.saturating_sub(width(&s) as u16 + 1);
        put(
            buf,
            x,
            g.bell.y,
            &s,
            cx.st("status.info", CANVAS),
            g.bell.width,
        );
        hits.add(g.bell, Target::Notifications);
    }

    // Sidebar tabs, in manifest order, right after the leading toggle.
    let tabs_end = draw_header_tabs(cx, buf, g, hits);

    // title, breadcrumbs and status, centred over the centre column
    let tx0 = g.title.x.max(tabs_end + 2).max(g.centre.x);
    let tr = Rect::new(
        tx0,
        g.title.y,
        g.title.right().saturating_sub(tx0),
        g.title.height,
    );
    if tr.width < 3 {
        return;
    }
    if let Some(e) = &cx.app.ui.edit {
        let label = format!("{} ", e.label);
        let lx = put(buf, tr.x + 1, tr.y, &label, cx.st(MUTED, CANVAS), tr.width);
        let (rows, _) = e.buf.layout(tr.width.saturating_sub(10) as usize);
        let line = rows.first().cloned().unwrap_or_default();
        let ex = put(
            buf,
            lx,
            tr.y,
            &line,
            cx.st(PRIMARY, CANVAS).add_modifier(Modifier::UNDERLINED),
            tr.width,
        );
        put(
            buf,
            ex + 1,
            tr.y,
            "enter save - esc cancel",
            cx.st(MUTED, CANVAS),
            tr.right().saturating_sub(ex + 1),
        );
        buf.set_style(Rect::new(lx, tr.y, 0, 0), Style::default());
        return;
    }
    let sep = format!(" {} ", cx.t.structure("breadcrumb"));
    let (crumbs, last): (Vec<String>, String) = match (cx.app.ui.view, cx.app.viewed_id()) {
        (View::Settings, _) => (vec![], "Settings".into()),
        (_, Some(id)) => {
            let chain = cx.app.ancestors_of(id);
            let mut titles: Vec<String> = chain.iter().map(|s| s.title.clone()).collect();
            let last = titles.pop().unwrap_or_default();
            if let Some(p) = chain
                .first()
                .and_then(|s| cx.app.state.model.projects.get(&s.project_id))
            {
                titles.insert(0, p.name.clone());
            }
            (titles, last)
        }
        _ => (vec![], "New session".into()),
    };
    // status block
    let status = header_status(cx);
    let status_w = status.as_ref().map_or(0, |(s, _)| width(s) + 2);
    let max_title = (tr.width as usize).saturating_sub(status_w + 2);
    let mut prefix = String::new();
    for c in &crumbs {
        prefix.push_str(c);
        prefix.push_str(&sep);
    }
    let full = format!("{prefix}{last}");
    let shown = truncate_start(&full, max_title.max(1), cx.ell());
    let total_w = width(&shown) + status_w;
    let x0 = tr.x + (tr.width as usize).saturating_sub(total_w) as u16 / 2;
    // split `shown` into muted prefix and primary last crumb
    let last_w = width(&last).min(width(&shown));
    let split_at = shown
        .char_indices()
        .rev()
        .scan(0usize, |acc, (i, c)| {
            *acc += text::cw(c);
            Some((i, *acc))
        })
        .find(|(_, acc)| *acc >= last_w)
        .map_or(0, |(i, _)| i);
    let (muted, main) = shown.split_at(split_at.min(shown.len()));
    let mut x = put(buf, x0, tr.y, muted, cx.st(MUTED, CANVAS), tr.width);
    let title_style = cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD);
    let title_style = if cx.hovered(tr) && cx.app.viewed().is_some() {
        title_style.add_modifier(Modifier::UNDERLINED)
    } else {
        title_style
    };
    let tx = x;
    x = put(
        buf,
        x,
        tr.y,
        main,
        title_style,
        tr.right().saturating_sub(x),
    );
    if cx.app.viewed().is_some() && cx.app.ui.view == View::Session {
        hits.add(Rect::new(tx, tr.y, x.saturating_sub(tx), 1), Target::Title);
    }
    if let Some((s, style)) = status {
        put(
            buf,
            x + 2,
            tr.y,
            &s,
            style,
            tr.right().saturating_sub(x + 2),
        );
    }
}

/// The Sessions / Files / Projects tabs in the header row; returns the column
/// after the last tab.
fn draw_header_tabs(cx: &Cx, buf: &mut Buffer, g: &Regions, hits: &mut HitMap) -> u16 {
    let a = cx.app;
    let y = g.header.y;
    let mut x = g.toggle_left.right() + 1;
    let limit = g.bell.x.saturating_sub(8);
    for t in &a.manifest.navigation.left_sidebar.tab {
        let Some(tab) = Tab::from_id(&t.id) else {
            continue;
        };
        let w = width(&t.label) as u16 + 2;
        if x + w > limit {
            break;
        }
        let rect = Rect::new(x, y, w, 1);
        let active = a.ui.tab == tab && a.ui.view == View::Session;
        let mut style = if active {
            cx.st(PRIMARY, CANVAS)
                .add_modifier(Modifier::BOLD)
                .patch(cx.t.bg("interactive.hover"))
        } else {
            cx.st(MUTED, CANVAS)
        };
        if cx.hovered(rect) && !active {
            style = cx.st(SECONDARY, CANVAS).patch(cx.t.bg("interactive.hover"));
        }
        clear(buf, rect, style);
        put(buf, x + 1, y, &t.label, style, w - 1);
        if active && cx.t.mono() {
            buf.set_style(rect, Style::default().add_modifier(Modifier::UNDERLINED));
        }
        hits.add(rect, Target::Tab(tab));
        x += w;
    }
    x
}

fn header_status(cx: &Cx) -> Option<(String, Style)> {
    let a = cx.app;
    match &a.state.connection {
        ConnectionState::Live => {
            let id = a.viewed_id()?;
            if a.is_busy(id) {
                let (_, label, _) = cx.t.status("running");
                Some((
                    format!("{} {label}", cx.t.spinner(a.frame / 2)),
                    cx.st(PRIMARY, CANVAS),
                ))
            } else {
                None
            }
        }
        ConnectionState::Starting | ConnectionState::Connecting => Some((
            format!(
                "{} {}",
                cx.t.spinner(a.frame / 2),
                a.state.connection.label()
            ),
            cx.st(SECONDARY, CANVAS),
        )),
        c => {
            let (g, _, tok) = cx.t.status("error");
            Some((
                format!(
                    "{g} {}",
                    if matches!(c, ConnectionState::Reconnecting { .. }) {
                        "reconnecting"
                    } else {
                        c.label()
                    }
                ),
                cx.st(tok, CANVAS),
            ))
        }
    }
}

// ============================================================================
// banner and toasts
// ============================================================================

fn draw_banner(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.height == 0 {
        return;
    }
    let banners = cx.app.banners();
    let Some(b) = banners.first() else { return };
    let row = Rect::new(r.x, r.y, r.width, 1);
    clear(buf, row, cx.st(SECONDARY, PANEL));
    let (glyph, tok) = if b.loading {
        (cx.t.spinner(cx.app.frame / 2).to_owned(), "text.primary")
    } else {
        let (g, t) = cx.level(b.level);
        (g.to_owned(), t)
    };
    // right block: +N, action, hide
    let mut right: Vec<(String, Style, Option<Target>)> = Vec::new();
    if banners.len() > 1 {
        right.push((format!("+{}", banners.len() - 1), cx.st(MUTED, PANEL), None));
    }
    if let Some((key, label, _)) = &b.action {
        let t = if key.is_empty() {
            label.clone()
        } else {
            format!("{key} {label}")
        };
        right.push((t, cx.st(ACCENT, PANEL), Some(Target::BannerAction(0))));
    }
    if let Some(i) = b.hide {
        right.push((
            format!("{} hide", cx.t.structure("close")),
            cx.st(MUTED, PANEL),
            Some(Target::BannerHide(i)),
        ));
    }
    let gap = cx.sep();
    let total: usize = right.iter().map(|(s, _, _)| width(s)).sum::<usize>()
        + gap.chars().count() * right.len().saturating_sub(1)
        + 1;
    let mut rx = r.right().saturating_sub(total as u16 + 1);
    let edge = rx;
    for (i, (s, style, target)) in right.iter().enumerate() {
        if i > 0 {
            rx = put(buf, rx, r.y, &gap, cx.st(MUTED, PANEL), r.right() - rx);
        }
        let x1 = put(buf, rx, r.y, s, *style, r.right().saturating_sub(rx));
        if let Some(t) = target {
            hits.add(Rect::new(rx, r.y, x1 - rx, 1), t.clone());
        }
        rx = x1;
    }
    put(buf, r.x + 1, r.y, &glyph, cx.st(tok, PANEL), 2);
    let room = (edge.saturating_sub(r.x + 4)) as usize;
    let text = truncate(&b.text, room.saturating_sub(2), cx.ell());
    put(
        buf,
        r.x + 3,
        r.y,
        &text,
        cx.st(SECONDARY, PANEL),
        room as u16,
    );
}

fn draw_toasts(cx: &Cx, buf: &mut Buffer, g: &Regions, hits: &mut HitMap) {
    let toasts = &cx.app.ui.alerts.toasts;
    if toasts.is_empty() {
        return;
    }
    let bottom = g.timeline.y + g.timeline.height;
    let mut y = bottom;
    for t in toasts.iter().rev() {
        if y <= g.timeline.y {
            break;
        }
        let (glyph, tok) = cx.level(t.level);
        let max_w = g.centre.width.saturating_sub(4).min(60) as usize;
        let body = truncate(&t.text, max_w.saturating_sub(4), cx.ell());
        let w = (width(&body) + 6).min(g.centre.width as usize) as u16;
        y -= 1;
        let r = Rect::new(g.centre.x + g.centre.width - w - 1, y, w, 1);
        clear(buf, r, cx.st(PRIMARY, RAISED));
        put(buf, r.x + 1, r.y, glyph, cx.st(tok, RAISED), 2);
        put(
            buf,
            r.x + 3,
            r.y,
            &body,
            cx.st(PRIMARY, RAISED),
            r.width - 5,
        );
        let hov = cx.hovered(r);
        put(
            buf,
            r.right() - 2,
            r.y,
            cx.t.structure("close"),
            cx.st(if hov { PRIMARY } else { MUTED }, RAISED),
            1,
        );
        hits.add(r, Target::ToastDismiss(t.id));
    }
}

// ============================================================================
// left sidebar
// ============================================================================

fn draw_sidebar_edge(cx: &Cx, buf: &mut Buffer, x: u16, r: Rect) {
    for y in r.y..r.bottom() {
        put(
            buf,
            x,
            y,
            cx.t.structure("rail"),
            cx.st("border.default", CANVAS),
            1,
        );
    }
}

fn draw_left(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.width < 4 || r.height < 4 {
        return;
    }
    let a = cx.app;
    clear(buf, r, cx.st(SECONDARY, SIDEBAR));
    hits.add(r, Target::Swallow);
    if r.width <= crate::layout::RAIL_WIDTH {
        return draw_rail(cx, buf, r, hits);
    }
    let focused = a.ui.focus == Focus::Sidebar;

    // fixed bottom: settings entry
    let settings_y = r.bottom() - 1;
    let sr = Rect::new(r.x, settings_y, r.width, 1);
    let mut s_style = cx.st(SECONDARY, SIDEBAR);
    if a.ui.view == View::Settings {
        s_style = cx
            .st(PRIMARY, SIDEBAR)
            .patch(cx.t.bg("interactive.selection"));
        clear(buf, sr, s_style);
    } else if cx.hovered(sr) {
        s_style = s_style.patch(cx.t.bg("interactive.hover"));
        clear(buf, sr, s_style);
    }
    let label = a
        .manifest
        .command(&a.manifest.navigation.left_sidebar.settings_entry)
        .map_or("Settings", |c| c.title.as_str());
    let label = label.strip_prefix("Open ").map_or(label.to_owned(), |l| {
        let mut c = l.chars();
        c.next()
            .map(|f| f.to_uppercase().collect::<String>() + c.as_str())
            .unwrap_or_default()
    });
    put(
        buf,
        r.x + 2,
        settings_y,
        &label,
        s_style,
        r.width.saturating_sub(2),
    );
    if let Some(k) = a.manifest.command("app.settings").and_then(|c| c.tui_key()) {
        let kx = r.right().saturating_sub(width(k) as u16 + 1);
        put(
            buf,
            kx,
            settings_y,
            k,
            s_style
                .patch(cx.st(MUTED, SIDEBAR))
                .patch(if a.ui.view == View::Settings {
                    cx.t.bg("interactive.selection")
                } else if cx.hovered(sr) {
                    cx.t.bg("interactive.hover")
                } else {
                    Style::default()
                }),
            r.right() - kx,
        );
    }
    hits.add(sr, Target::SettingsEntry);

    let body = Rect::new(r.x, r.y + 1, r.width, r.height.saturating_sub(3));
    match a.ui.tab {
        Tab::Sessions => draw_sessions(cx, buf, body, focused, hits),
        Tab::Files => draw_files(cx, buf, body, focused, hits),
        Tab::Projects => draw_projects(cx, buf, body, focused, hits),
    }
}

/// One rail entry: a session, or the search and new-session controls.
#[derive(Clone, Copy, PartialEq, Eq)]
enum RailItem {
    Search,
    Session(usize),
    New,
}

/// Rail rows: three per item, the glyph on the middle row.
const RAIL_ITEM: u16 = 3;

fn rail_layout(cx: &Cx, r: Rect) -> Vec<(RailItem, Rect)> {
    let a = cx.app;
    let entries = a.side_entries();
    let room = (r.height.saturating_sub(1) / RAIL_ITEM) as usize;
    let mut items = vec![RailItem::Search];
    let sessions = room.saturating_sub(2);
    let cursor = a.cursor(Tab::Sessions).min(entries.len().saturating_sub(1));
    let top = (cursor + 1).saturating_sub(sessions);
    items.extend((top..entries.len()).take(sessions).map(RailItem::Session));
    if items.len() < room {
        items.push(RailItem::New);
    }
    items
        .into_iter()
        .enumerate()
        .map(|(i, item)| {
            (
                item,
                Rect::new(r.x, r.y + 1 + i as u16 * RAIL_ITEM, r.width, RAIL_ITEM),
            )
        })
        .collect()
}

fn rail_initial(title: &str) -> String {
    title
        .chars()
        .find(|c| c.is_alphanumeric())
        .map_or("·".into(), |c| c.to_uppercase().collect())
}

fn draw_rail(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    let a = cx.app;
    let entries = a.side_entries();
    let viewed = a.viewed_id();
    let focused = a.ui.focus == Focus::Sidebar;
    let cursor = a.cursor(Tab::Sessions);
    let mid = r.x + r.width.saturating_sub(1) / 2;
    for (item, band) in rail_layout(cx, r) {
        let hovered = cx.hovered(band);
        let (glyph, mut style, target) = match item {
            RailItem::Search => (
                if cx.t.opts.ascii { "/" } else { "⌕" }.to_owned(),
                cx.st(MUTED, SIDEBAR),
                Target::RailSearch,
            ),
            RailItem::New => (
                "+".to_owned(),
                cx.st(SECONDARY, SIDEBAR),
                Target::NewSession,
            ),
            RailItem::Session(i) => {
                let Some(s) = entries
                    .get(i)
                    .and_then(|e| a.state.model.sessions.get(&e.id))
                else {
                    continue;
                };
                let selected = viewed == Some(s.id.as_str());
                let (glyph, token) = if a.session_waiting(&s.id) {
                    let (g, t) = cx.t.attention("waiting");
                    (g.to_owned(), t)
                } else if a.ui.failed_sessions.contains(&s.id) {
                    let (g, t) = cx.t.attention("error");
                    (g.to_owned(), t)
                } else if a.is_busy(&s.id) && !cx.t.opts.reduced_motion {
                    (cx.t.spinner(a.frame / 2).to_owned(), PRIMARY)
                } else {
                    (
                        rail_initial(&s.title),
                        if selected || a.ui.unread_sessions.contains(&s.id) {
                            PRIMARY
                        } else {
                            MUTED
                        },
                    )
                };
                let mut style = cx.st(token, SIDEBAR);
                if selected {
                    style = style
                        .add_modifier(Modifier::BOLD)
                        .patch(cx.t.bg("interactive.hover"));
                }
                if focused && i == cursor {
                    put(
                        buf,
                        band.x,
                        band.y + 1,
                        cx.t.structure("focus_bar"),
                        cx.st("interactive.focus", SIDEBAR),
                        1,
                    );
                }
                (glyph, style, Target::SideRow(i))
            }
        };
        if hovered {
            style = style
                .patch(cx.st(PRIMARY, SIDEBAR))
                .patch(cx.t.bg("interactive.hover"));
        }
        if style.bg.is_some() && style.bg != cx.st(PRIMARY, SIDEBAR).bg {
            let fill = Rect::new(
                band.x + 1,
                band.y,
                band.width.saturating_sub(2),
                band.height,
            );
            clear(buf, fill, Style::default().bg(style.bg.unwrap_or_default()));
        }
        put(buf, mid, band.y + 1, &glyph, style, 2);
        hits.add(band, target);
    }
}

/// The hovered rail session's title and place, drawn beside the rail over
/// the centre column (after everything else, so it is on top).
fn draw_rail_popup(cx: &Cx, buf: &mut Buffer, r: Rect, limit: Rect) {
    let a = cx.app;
    let entries = a.side_entries();
    let Some((RailItem::Session(i), band)) = rail_layout(cx, r)
        .into_iter()
        .find(|(item, band)| matches!(item, RailItem::Session(_)) && cx.hovered(*band))
    else {
        return;
    };
    let Some(s) = entries
        .get(i)
        .and_then(|e| a.state.model.sessions.get(&e.id))
    else {
        return;
    };
    let title = if s.title.is_empty() {
        "Untitled".to_owned()
    } else {
        s.title.clone()
    };
    let detail = session_place(cx, s);
    let max = limit.right().saturating_sub(r.right() + 1).min(60) as usize;
    if max < 10 {
        return;
    }
    let w = (width(&title).max(width(&detail)) + 4).min(max) as u16;
    let pop = Rect::new(r.right(), band.y, w, RAIL_ITEM + 1).intersection(limit);
    let bg = "interactive.hover";
    clear(buf, pop, cx.st(PRIMARY, bg));
    put(
        buf,
        pop.x + 2,
        pop.y + 1,
        &truncate(&title, (w - 3) as usize, cx.ell()),
        cx.st(PRIMARY, bg).add_modifier(Modifier::BOLD),
        w - 3,
    );
    put(
        buf,
        pop.x + 2,
        pop.y + 2,
        &truncate(&detail, (w - 3) as usize, cx.ell()),
        cx.st(MUTED, bg),
        w - 3,
    );
}

/// `project ⌥ branch` for a session row.
fn session_place(cx: &Cx, s: &crate::api::Session) -> String {
    let a = cx.app;
    let project = a
        .state
        .model
        .projects
        .get(&s.project_id)
        .map_or(s.project_id.as_str(), |p| p.name.as_str());
    let branch = a
        .session_worktree(s)
        .and_then(|tree| tree["branch"].as_str())
        .filter(|branch| !branch.is_empty());
    let separator = if cx.t.opts.ascii { "~" } else { "⌥" };
    branch.map_or_else(
        || project.to_owned(),
        |b| format!("{project} {separator} {b}"),
    )
}

fn draw_files(cx: &Cx, buf: &mut Buffer, r: Rect, focused: bool, hits: &mut HitMap) {
    let a = cx.app;
    let root = a.files_root_label();
    let mut top = r.y;
    if let Some(label) = &root {
        let row = Rect::new(r.x, r.y, r.width, 1);
        clear(buf, row, cx.st(MUTED, SIDEBAR));
        put(
            buf,
            r.x + 1,
            r.y,
            &format!("{} {label}", cx.t.fold(true)),
            cx.st(MUTED, SIDEBAR),
            r.width.saturating_sub(1),
        );
        hits.add(row, Target::FilesRoot);
        top += 1;
    }
    let height = r.bottom().saturating_sub(top);
    let rows = a.file_rows();
    if rows.is_empty() {
        let message = a
            .state
            .file_dirs
            .get("")
            .and_then(|result| result.as_ref().err())
            .map_or(
                if cx.t.opts.ascii {
                    "No files (or loading...)"
                } else {
                    "No files (or loading…)"
                },
                String::as_str,
            );
        put(
            buf,
            r.x + 1,
            top,
            message,
            cx.st(MUTED, SIDEBAR),
            r.width.saturating_sub(1),
        );
        return;
    }
    let cursor = a.cursor(Tab::Files);
    let visible = height as usize;
    let first = (cursor + 1).saturating_sub(visible);
    for (index, entry) in rows.iter().enumerate().skip(first).take(visible) {
        let y = top + (index - first) as u16;
        let row = Rect::new(r.x, y, r.width, 1);
        let selected = focused && index == cursor;
        let mut style = row_style(cx, selected, cx.hovered(row));
        if entry.ignored {
            if let Some(color) = cx.t.color(MUTED) {
                style = style.fg(color);
            }
        } else if entry.kind == "directory" {
            if let Some(color) = cx.t.color(PRIMARY) {
                style = style.fg(color).add_modifier(Modifier::BOLD);
            }
        } else if entry.kind == "error" {
            if let Some(color) = cx.t.color("status.error") {
                style = style.fg(color);
            }
        }
        clear(buf, row, style);
        if selected {
            put(
                buf,
                r.x,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", SIDEBAR),
                1,
            );
        }
        let indent = 1 + entry.depth * 2;
        let marker = match entry.kind.as_str() {
            "directory" => cx.t.fold(entry.expanded),
            "error" => cx.t.status("error").0,
            _ => " ",
        };
        let marker = if entry.kind == "file" || entry.kind == "symlink" {
            " ".to_owned()
        } else {
            marker.to_owned()
        };
        let letter = entry
            .git_status
            .as_deref()
            .and_then(git_marker)
            .filter(|_| entry.kind != "directory" && height > 0 && r.width > 8);
        let room = r
            .width
            .saturating_sub(indent as u16)
            .saturating_sub(if letter.is_some() { 3 } else { 1 });
        let label = format!(
            "{marker} {}",
            truncate(&entry.name, room as usize, cx.ell())
        );
        put(
            buf,
            r.x + indent as u16,
            y,
            &label,
            style,
            room.saturating_add(2),
        );
        if let Some((glyph, token)) = letter {
            put(
                buf,
                r.right().saturating_sub(2),
                y,
                &glyph.to_string(),
                cx.st(token, SIDEBAR),
                1,
            );
        }
        hits.add(row, Target::SideRow(index));
    }
}

/// A compact Git marker for a porcelain status code.
fn git_marker(code: &str) -> Option<(char, &'static str)> {
    let code = code.trim();
    if code.is_empty() {
        return None;
    }
    if code.contains('D') {
        Some(('D', "status.error"))
    } else if code.contains('M') {
        Some(('M', "status.warning"))
    } else if code.contains('?') || code.starts_with("A") || code.starts_with('R') {
        Some((if code.contains('?') { '?' } else { 'A' }, "status.success"))
    } else {
        None
    }
}

fn draw_file_view(cx: &Cx, buf: &mut Buffer, r: Rect) {
    clear(buf, r, cx.st(PRIMARY, SIDEBAR));
    let Some((project, session, path, result)) = &cx.app.state.file else {
        put(
            buf,
            r.x + 1,
            r.y,
            if cx.t.opts.ascii {
                "Loading file..."
            } else {
                "Loading file…"
            },
            cx.st(MUTED, SIDEBAR),
            r.width.saturating_sub(1),
        );
        return;
    };
    if Some(project.clone()) != cx.app.current_project() || session.as_deref() != cx.app.viewed_id()
    {
        return;
    }
    let display = cx
        .app
        .files_root()
        .map(|root| short_home(&std::path::Path::new(&root).join(path).to_string_lossy()))
        .unwrap_or_else(|| path.clone());
    put(
        buf,
        r.x + 1,
        r.y,
        &truncate_middle(&display, r.width.saturating_sub(2) as usize, cx.ell()),
        cx.st(PRIMARY, SIDEBAR).add_modifier(Modifier::BOLD),
        r.width.saturating_sub(1),
    );
    match result {
        Ok(data) => {
            let content = data["content"].as_str().unwrap_or("");
            let page = r.height.saturating_sub(2) as usize;
            for (i, line) in content
                .lines()
                .enumerate()
                .skip(cx.app.ui.file_scroll)
                .take(page)
            {
                let label = format!("{:>4}  {line}", i + 1);
                let label = truncate(&label, r.width.saturating_sub(1) as usize, cx.ell());
                put(
                    buf,
                    r.x + 1,
                    r.y + 1 + (i - cx.app.ui.file_scroll) as u16,
                    &label,
                    cx.st(SECONDARY, SIDEBAR),
                    r.width.saturating_sub(1),
                );
            }
            let remaining = content
                .lines()
                .count()
                .saturating_sub(cx.app.ui.file_scroll + page);
            if remaining > 0 && r.height >= 2 {
                let notice = format!("{remaining} more lines · scroll");
                put(
                    buf,
                    r.x + 1,
                    r.bottom() - 1,
                    &notice,
                    cx.st(MUTED, SIDEBAR),
                    r.width.saturating_sub(1),
                );
            }
        }
        Err(error) => {
            put(
                buf,
                r.x + 1,
                r.y + 1,
                error,
                cx.st("status.error", SIDEBAR),
                r.width.saturating_sub(1),
            );
        }
    }
}

fn row_style(cx: &Cx, selected: bool, hover: bool) -> Style {
    if selected {
        cx.st(PRIMARY, SIDEBAR)
            .patch(cx.t.bg("interactive.selection"))
    } else if hover {
        cx.st(PRIMARY, SIDEBAR).patch(cx.t.bg("interactive.hover"))
    } else {
        cx.st(SECONDARY, SIDEBAR)
    }
}

fn draw_sessions(cx: &Cx, buf: &mut Buffer, r: Rect, focused: bool, hits: &mut HitMap) {
    let a = cx.app;
    if r.height < 3 {
        return;
    }
    let list = r;
    let entries = a.side_entries();
    if entries.is_empty() {
        let msg = if a.state.model.projects.is_empty() {
            "No project yet. Add one with the Nexus CLI."
        } else if a.ui.show_archived || a.ui.scope_all {
            "No sessions here. Type in the composer to start one."
        } else {
            "No sessions yet. Type in the composer to start one."
        };
        for (i, l) in text::wrap(msg, list.width.saturating_sub(3) as usize)
            .iter()
            .enumerate()
            .take(list.height as usize)
        {
            put(
                buf,
                list.x + 1,
                list.y + 1 + i as u16,
                l,
                cx.st(MUTED, SIDEBAR),
                list.width - 2,
            );
        }
        return;
    }
    // Two selectable lines and a spacer per session; headings scroll with them.
    struct VRow {
        entry: usize,
        line: u8,
    }
    let mut rows = Vec::new();
    for (i, e) in entries.iter().enumerate() {
        if e.heading.is_some() {
            rows.push(VRow { entry: i, line: 3 });
        }
        for line in 0..3 {
            rows.push(VRow { entry: i, line });
        }
    }
    let cursor = a.cursor(Tab::Sessions);
    let cur_row = rows
        .iter()
        .position(|v| v.line == 0 && v.entry == cursor)
        .unwrap_or(0);
    // The new-session row closes the list (`line` 4).
    rows.push(VRow {
        entry: usize::MAX,
        line: 4,
    });
    let h = list.height as usize;
    let top = (cur_row + 3).saturating_sub(h);
    let viewed = a.viewed_id();
    for (vi, v) in rows.iter().enumerate().skip(top).take(h) {
        let y = list.y + (vi - top) as u16;
        if v.line == 4 {
            let rr = Rect::new(list.x, y, list.width, 1);
            let style = if cx.hovered(rr) {
                cx.st(PRIMARY, SIDEBAR).patch(cx.t.bg("interactive.hover"))
            } else {
                cx.st(MUTED, SIDEBAR)
            };
            clear(buf, rr, style);
            put(
                buf,
                rr.x + 1,
                y,
                "+ New session",
                style,
                rr.width.saturating_sub(1),
            );
            hits.add(rr, Target::NewSession);
            continue;
        }
        let e = &entries[v.entry];
        if v.line == 3 {
            let name = truncate(
                e.heading.as_deref().unwrap_or(""),
                list.width.saturating_sub(3) as usize,
                cx.ell(),
            );
            put(
                buf,
                list.x + 1,
                y,
                &name,
                cx.st(MUTED, SIDEBAR),
                list.width - 1,
            );
            continue;
        }
        if v.line == 2 {
            continue;
        }
        let Some(s) = a.state.model.sessions.get(&e.id) else {
            continue;
        };
        let rr = Rect::new(list.x, y, list.width, 1);
        let hovered = cx.hovered(rr)
            || (v.line == 0 && cx.hovered(Rect::new(list.x, y + 1, list.width, 1)))
            || (v.line == 1 && y > list.y && cx.hovered(Rect::new(list.x, y - 1, list.width, 1)));
        let selected = viewed == Some(s.id.as_str());
        let bg = if hovered {
            cx.t.bg("interactive.hover")
        } else {
            Style::default()
        };
        clear(buf, rr, cx.st(SECONDARY, SIDEBAR).patch(bg));
        if v.line == 0 {
            if focused && v.entry == cursor {
                put(
                    buf,
                    rr.x + 1,
                    y,
                    cx.t.structure("focus_bar"),
                    cx.st("interactive.focus", SIDEBAR).patch(bg),
                    1,
                );
            } else if a.session_waiting(&s.id) {
                let (glyph, token) = cx.t.attention("waiting");
                put(buf, rr.x + 1, y, glyph, cx.st(token, SIDEBAR).patch(bg), 1);
            } else if a.ui.failed_sessions.contains(&s.id) {
                let (glyph, token) = cx.t.attention("error");
                put(buf, rr.x + 1, y, glyph, cx.st(token, SIDEBAR).patch(bg), 1);
            } else if a.ui.unread_sessions.contains(&s.id) {
                let (glyph, token) = cx.t.attention("unread");
                put(buf, rr.x + 1, y, glyph, cx.st(token, SIDEBAR).patch(bg), 1);
            }
            let title = if s.title.is_empty() {
                "Untitled"
            } else {
                &s.title
            };
            let token = if s.archived {
                MUTED
            } else if selected || a.ui.unread_sessions.contains(&s.id) {
                PRIMARY
            } else {
                SECONDARY
            };
            let mut style = cx.st(token, SIDEBAR).patch(bg);
            if selected {
                style = style.add_modifier(Modifier::BOLD);
            }
            let tw = rr.width.saturating_sub(4);
            let clipped = width(title) > tw as usize;
            let fade = clipped && cx.t.opts.tier == crate::theme::Tier::TrueColor;
            let shown = if fade {
                use unicode_segmentation::UnicodeSegmentation;
                let mut used = 0;
                title
                    .graphemes(true)
                    .take_while(|g| {
                        let next = used + width(g);
                        if next > tw as usize {
                            false
                        } else {
                            used = next;
                            true
                        }
                    })
                    .collect::<String>()
            } else {
                truncate(title, tw as usize, cx.ell())
            };
            let animate = a.is_busy(&s.id) && !cx.t.opts.reduced_motion && !cx.t.mono();
            let spans = crate::theme::shimmer(
                &shown,
                a.frame,
                animate,
                style,
                cx.st(PRIMARY, SIDEBAR).patch(bg),
            );
            let fade_bg = cx.t.color(if hovered {
                "interactive.hover"
            } else {
                SIDEBAR
            });
            let total = width(&shown);
            let mut offset = 0;
            let mut x = rr.x + 3;
            for span in spans {
                use unicode_segmentation::UnicodeSegmentation;
                for grapheme in span.content.graphemes(true) {
                    let cells = width(grapheme);
                    let remaining = total.saturating_sub(offset);
                    let mut text_style = span.style;
                    if fade && remaining <= 3 {
                        if let (
                            Some(ratatui::style::Color::Rgb(r, g, b)),
                            Some(ratatui::style::Color::Rgb(br, bg, bb)),
                        ) = (text_style.fg, fade_bg)
                        {
                            let amount = remaining as f32 / 4.0;
                            let mix = |fg: u8, bg: u8| {
                                (f32::from(bg) + (f32::from(fg) - f32::from(bg)) * amount).round()
                                    as u8
                            };
                            text_style = text_style.fg(ratatui::style::Color::Rgb(
                                mix(r, br),
                                mix(g, bg),
                                mix(b, bb),
                            ));
                        }
                    }
                    x = put(
                        buf,
                        x,
                        y,
                        grapheme,
                        text_style,
                        rr.right().saturating_sub(x),
                    );
                    offset += cells;
                }
            }
        } else {
            let detail = session_place(cx, s);
            put(
                buf,
                rr.x + 3,
                y,
                &truncate(&detail, rr.width.saturating_sub(4) as usize, cx.ell()),
                cx.st(MUTED, SIDEBAR).patch(bg),
                rr.width.saturating_sub(4),
            );
        }
        hits.add(rr, Target::SideRow(v.entry));
    }
}

fn draw_projects(cx: &Cx, buf: &mut Buffer, r: Rect, focused: bool, hits: &mut HitMap) {
    let a = cx.app;
    let ids = a.project_ids();
    if ids.is_empty() {
        put(
            buf,
            r.x + 1,
            r.y + 1,
            "No projects yet.",
            cx.st(MUTED, SIDEBAR),
            r.width - 1,
        );
        return;
    }
    let cursor = a.cursor(Tab::Projects);
    let cur_proj = a.current_project();
    let per = 2u16;
    let h = (r.height / per) as usize;
    let top = if cursor < h { 0 } else { cursor + 1 - h };
    let now = now_secs();
    for (i, id) in ids.iter().enumerate().skip(top).take(h) {
        let p = &a.state.model.projects[id];
        let y = r.y + (i - top) as u16 * per;
        let rr = Rect::new(r.x, y, r.width, per);
        let selected = cur_proj.as_deref() == Some(id.as_str());
        let style = row_style(cx, selected, cx.hovered(rr));
        clear(buf, rr, style);
        if focused && i == cursor {
            put(
                buf,
                rr.x,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", SIDEBAR)
                    .patch(style)
                    .add_modifier(Modifier::BOLD),
                1,
            );
        }
        let n = a
            .state
            .model
            .sessions
            .values()
            .filter(|s| &s.project_id == id)
            .count();
        let right = format!("{n}  {}", text::age(now, p.updated));
        let rw = width(&right) as u16;
        let rx = rr.right().saturating_sub(rw + 1);
        put(
            buf,
            rx,
            y,
            &right,
            cx.st(MUTED, SIDEBAR).patch(if selected {
                cx.t.bg("interactive.selection")
            } else {
                Style::default()
            }),
            rw,
        );
        let nw = rx.saturating_sub(rr.x + 4) as usize;
        put(
            buf,
            rr.x + 3,
            y,
            &truncate(&p.name, nw, cx.ell()),
            if selected {
                style.add_modifier(Modifier::BOLD)
            } else {
                style
            },
            nw as u16,
        );
        let path = truncate_middle(&p.path, r.width.saturating_sub(5) as usize, cx.ell());
        put(
            buf,
            rr.x + 3,
            y + 1,
            &path,
            cx.st(MUTED, SIDEBAR).patch(if selected {
                cx.t.bg("interactive.selection")
            } else {
                Style::default()
            }),
            r.width - 4,
        );
        hits.add(rr, Target::SideRow(i));
    }
}

// ============================================================================
// right sidebar
// ============================================================================

fn kv(cx: &Cx, buf: &mut Buffer, r: Rect, y: &mut u16, key: &str, value: &str) {
    if *y >= r.bottom() {
        return;
    }
    put(buf, r.x + 1, *y, key, cx.st(MUTED, CANVAS), 9);
    let vw = r.width.saturating_sub(12) as usize;
    put(
        buf,
        r.x + 11,
        *y,
        &truncate(value, vw, cx.ell()),
        if key == "agent" {
            cx.st(SECONDARY, CANVAS).patch(cx.t.agent_style(value))
        } else {
            cx.st(SECONDARY, CANVAS)
        },
        vw as u16,
    );
    *y += 1;
}

fn draw_right(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.width < 8 || r.height < 4 {
        return;
    }
    let a = cx.app;
    clear(buf, r, cx.st(SECONDARY, CANVAS));
    hits.add(r, Target::Swallow);
    let focused = a.ui.focus == Focus::Right;
    let mut y = r.y;
    for (i, sec) in a
        .manifest
        .navigation
        .right_sidebar
        .section
        .iter()
        .enumerate()
    {
        if y >= r.bottom() {
            break;
        }
        let head = Rect::new(r.x, y, r.width, 1);
        let mut hs = cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD);
        if cx.hovered(head) {
            hs = hs.patch(cx.t.bg("interactive.hover"));
        }
        if focused && i == 0 {
            put(
                buf,
                r.x,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", CANVAS)
                    .add_modifier(Modifier::BOLD),
                1,
            );
        }
        put(buf, r.x + 1, y, cx.t.fold(true), cx.st(MUTED, CANVAS), 1);
        put(buf, r.x + 3, y, &sec.label, hs, r.width.saturating_sub(4));
        hits.add(head, Target::RightSection(i));
        y += 1;
        match sec.id.as_str() {
            "session_info" => session_info(cx, buf, r, &mut y),
            "git_status" => git_status(cx, buf, r, &mut y),
            _ => {
                let reason = sec
                    .unavailable_reason
                    .clone()
                    .unwrap_or_else(|| "Not available.".into());
                for l in text::wrap(&reason, r.width.saturating_sub(4) as usize) {
                    if y >= r.bottom() {
                        break;
                    }
                    put(buf, r.x + 3, y, &l, cx.st(MUTED, CANVAS), r.width - 4);
                    y += 1;
                }
            }
        }
        y += 1;
    }
}

fn git_status(cx: &Cx, buf: &mut Buffer, r: Rect, y: &mut u16) {
    let Some(id) = cx.app.viewed_id() else {
        put(
            buf,
            r.x + 3,
            *y,
            "Open a session",
            cx.st(MUTED, CANVAS),
            r.width.saturating_sub(4),
        );
        *y += 1;
        return;
    };
    let Some((_, result)) = cx
        .app
        .state
        .git
        .as_ref()
        .filter(|(session, _)| session == id)
    else {
        put(
            buf,
            r.x + 3,
            *y,
            if cx.t.opts.ascii {
                "Loading Git status..."
            } else {
                "Loading Git status…"
            },
            cx.st(MUTED, CANVAS),
            r.width.saturating_sub(4),
        );
        *y += 1;
        return;
    };
    match result {
        Err(error) => {
            let message = if error.contains("workspace is not a Git repository") {
                "No Git repository"
            } else {
                error.as_str()
            };
            let lines = text::wrap(message, r.width.saturating_sub(4) as usize);
            for line in lines {
                if *y >= r.bottom() {
                    break;
                }
                put(
                    buf,
                    r.x + 3,
                    *y,
                    &line,
                    cx.st(MUTED, CANVAS),
                    r.width.saturating_sub(4),
                );
                *y += 1;
            }
        }
        Ok(data) => {
            let branch = data["branch"].as_str().unwrap_or("—");
            let (up, down) = if cx.t.opts.ascii {
                ("+", "-")
            } else {
                ("↑", "↓")
            };
            let header = format!(
                "{}  {up}{} {down}{}",
                branch,
                data["ahead"].as_u64().unwrap_or(0),
                data["behind"].as_u64().unwrap_or(0)
            );
            put(
                buf,
                r.x + 3,
                *y,
                &truncate(&header, r.width.saturating_sub(4) as usize, cx.ell()),
                cx.st(SECONDARY, CANVAS),
                r.width.saturating_sub(4),
            );
            *y += 1;
            if let Some(changes) = data["changes"].as_array() {
                if changes.is_empty() && *y < r.bottom() {
                    put(
                        buf,
                        r.x + 3,
                        *y,
                        "Clean",
                        cx.st(MUTED, CANVAS),
                        r.width.saturating_sub(4),
                    );
                    *y += 1;
                }
                for change in changes {
                    if *y >= r.bottom() {
                        break;
                    }
                    let label = format!(
                        "{} +{} -{}",
                        change["path"].as_str().unwrap_or(""),
                        change["added"].as_u64().unwrap_or(0),
                        change["removed"].as_u64().unwrap_or(0)
                    );
                    put(
                        buf,
                        r.x + 3,
                        *y,
                        &truncate(&label, r.width.saturating_sub(4) as usize, cx.ell()),
                        cx.st(SECONDARY, CANVAS),
                        r.width.saturating_sub(4),
                    );
                    *y += 1;
                }
            }
        }
    }
}

fn session_info(cx: &Cx, buf: &mut Buffer, r: Rect, y: &mut u16) {
    let a = cx.app;
    let Some(s) = a.viewed() else {
        put(
            buf,
            r.x + 3,
            *y,
            "No session open",
            cx.st(MUTED, CANVAS),
            r.width - 4,
        );
        *y += 1;
        return;
    };
    let now = now_secs();
    kv(
        cx,
        buf,
        r,
        y,
        "title",
        if s.title.is_empty() {
            "Untitled"
        } else {
            &s.title
        },
    );
    kv(
        cx,
        buf,
        r,
        y,
        "id",
        &truncate_middle(&s.id, r.width.saturating_sub(13) as usize, cx.ell()),
    );
    kv(cx, buf, r, y, "agent", &s.agent);
    kv(
        cx,
        buf,
        r,
        y,
        "model",
        s.model
            .as_deref()
            .filter(|m| !m.is_empty())
            .unwrap_or("default"),
    );
    kv(cx, buf, r, y, "perm", &s.permission);
    kv(
        cx,
        buf,
        r,
        y,
        "dir",
        &truncate_middle(&s.directory, r.width.saturating_sub(13) as usize, cx.ell()),
    );
    kv(
        cx,
        buf,
        r,
        y,
        "status",
        &if a.is_busy(&s.id) {
            "running".to_owned()
        } else if s.archived {
            "archived".into()
        } else {
            "idle".into()
        },
    );
    kv(cx, buf, r, y, "started", &text::ago(now, s.created));
    kv(cx, buf, r, y, "active", &text::ago(now, s.updated));
    let more = a
        .state
        .model
        .selected
        .as_ref()
        .is_some_and(|v| v.messages_more);
    kv(
        cx,
        buf,
        r,
        y,
        "messages",
        &format!(
            "{}{} loaded",
            a.messages().len(),
            if more { "+" } else { "" }
        ),
    );
    let kids = a.descendants(&s.id);
    let running = a
        .state
        .model
        .running
        .keys()
        .filter(|k| a.ancestors_of(k).iter().any(|x| x.id == s.id) && **k != s.id)
        .count();
    kv(
        cx,
        buf,
        r,
        y,
        "children",
        &if kids == 0 {
            "none".to_owned()
        } else {
            format!("{kids}, {running} running")
        },
    );
    kv(cx, buf, r, y, "tokens", "not reported");
}

// ============================================================================
// timeline
// ============================================================================

fn empty_state(cx: &Cx) -> (String, Vec<String>) {
    let a = cx.app;
    match &a.state.connection {
        ConnectionState::Starting => (
            "Starting Nexus Core".into(),
            vec!["This takes a moment the first time.".into()],
        ),
        ConnectionState::Connecting => ("Connecting to Nexus Core".into(), vec![]),
        _ if a.state.model.projects.is_empty() => (
            "Welcome to Nexus".into(),
            vec![
                "No project yet. Add one with the Nexus CLI (nexus project open <path>),".into(),
                "then it appears here. Sign in to a provider with `nexus login`.".into(),
            ],
        ),
        _ => {
            let name = a
                .current_project()
                .and_then(|p| a.state.model.projects.get(&p).map(|p| p.name.clone()))
                .unwrap_or_default();
            if a.viewed().is_some() {
                (
                    "No messages yet".into(),
                    vec!["Type below to start the conversation.".into()],
                )
            } else {
                (
                    "New session".into(),
                    vec![
                        format!("Project {name}. Type a message below to start a session."),
                        "ctrl+p commands · f1 keys".into(),
                    ],
                )
            }
        }
    }
}

fn draw_timeline(
    cx: &Cx,
    buf: &mut Buffer,
    r: Rect,
    hits: &mut HitMap,
    timeline: &crate::timeline::Timeline,
) -> (usize, usize) {
    if r.height == 0 || r.width < 4 {
        return (0, 0);
    }
    let a = cx.app;
    hits.add(r, Target::Timeline);
    let inner = Rect::new(r.x + 1, r.y, r.width - 2, r.height);
    let lines = &timeline.lines;
    if a.viewed().is_none() || (lines.is_empty() && a.messages().is_empty()) {
        let (title, body) = empty_state(cx);
        let busy = matches!(
            a.state.connection,
            ConnectionState::Starting | ConnectionState::Connecting
        );
        let head = if busy {
            format!("{} {title}", cx.t.spinner(a.frame / 2))
        } else {
            title
        };
        let wrap_w = (inner.width as usize).saturating_sub(4).max(10);
        let body: Vec<String> = body.iter().flat_map(|l| text::wrap(l, wrap_w)).collect();
        let y0 = r.y + r.height.saturating_sub(2 + body.len() as u16) / 3;
        let x0 = |s: &str| inner.x + (inner.width as usize).saturating_sub(width(s)) as u16 / 2;
        put(
            buf,
            x0(&head),
            y0,
            &truncate(&head, inner.width as usize, cx.ell()),
            cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
            inner.width,
        );
        for (i, l) in body.iter().enumerate() {
            let l = truncate(l, inner.width as usize, cx.ell());
            put(
                buf,
                x0(&l),
                y0 + 2 + i as u16,
                &l,
                cx.st(MUTED, CANVAS),
                inner.width,
            );
        }
        if a.ui.focus == Focus::Timeline {
            put(
                buf,
                r.x,
                r.y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", CANVAS)
                    .add_modifier(Modifier::BOLD),
                1,
            );
        }
        return (0, r.height as usize);
    }
    let total = lines.len();
    let h = r.height as usize;
    let max_top = total.saturating_sub(h);
    let top = if a.ui.scroll.follow {
        max_top
    } else {
        a.ui.scroll.top.min(max_top)
    };
    for (i, line) in lines.iter().skip(top).take(h).enumerate() {
        let y = r.y + i as u16;
        if let Some(bg) = line.bg {
            clear(buf, Rect::new(inner.x, y, inner.width, 1), bg);
        }
        buf.set_line(inner.x, y, &Line::from(line.spans.clone()), inner.width);
    }
    // Every visible block is clickable; a click selects and toggles it.
    for (bi, b) in timeline.blocks.iter().enumerate() {
        if b.end <= top || b.start >= top + h {
            continue;
        }
        let y0 = r.y + (b.start.saturating_sub(top)) as u16;
        let y1 = r.y + (b.end.min(top + h).saturating_sub(top)) as u16;
        let rect = Rect::new(inner.x, y0, inner.width, y1 - y0);
        hits.add(rect, Target::Block(bi));
        // Old Nexus exposes a fence's copy action only when its first row has room.
        if b.kind == crate::timeline::BlockKind::Code
            && b.start >= top
            && inner.width >= 8
            && (cx.hovered(rect)
                || (a.ui.focus == Focus::Timeline && a.ui.timeline_sel == Some(bi)))
        {
            let button = Rect::new(inner.right() - 6, y0, 5, 1);
            let empty = (button.x..button.right())
                .all(|x| matches!(buf[(x, y0)].symbol(), " " | "─" | "-"));
            if empty {
                put(
                    buf,
                    button.x,
                    button.y,
                    "Copy",
                    cx.st(MUTED, PANEL),
                    button.width,
                );
                hits.add(button, Target::CopyBlock(bi));
            }
        }
        // Open turns have a padding row for the copy action. Hide it when
        // the beginning of the block has scrolled away.
        if b.kind == crate::timeline::BlockKind::Turn
            && b.start >= top
            && (cx.hovered(rect)
                || (a.ui.focus == Focus::Timeline && a.ui.timeline_sel == Some(bi)))
            && inner.width >= 8
        {
            let button = Rect::new(inner.right() - 6, y0, 5, 1);
            put(
                buf,
                button.x,
                button.y,
                "Copy",
                cx.st(MUTED, RAISED),
                button.width,
            );
            hits.add(button, Target::CopyBlock(bi));
        }
    }
    // The focus location: the selected block's first row, else the top.
    let bar_y = if a.ui.focus == Focus::Timeline {
        a.ui.timeline_sel
            .and_then(|i| timeline.blocks.get(i))
            .filter(|b| b.end > top && b.start < top + h)
            .map_or(r.y, |b| r.y + (b.start.saturating_sub(top)) as u16)
    } else {
        r.y
    };
    if a.ui.focus == Focus::Timeline {
        put(
            buf,
            r.x,
            bar_y,
            cx.t.structure("focus_bar"),
            cx.st("interactive.focus", CANVAS)
                .add_modifier(Modifier::BOLD),
            1,
        );
    }
    if top < max_top {
        let key = a
            .manifest
            .command("session.jump_latest")
            .and_then(|c| c.tui_key())
            .unwrap_or("end");
        let s = format!(
            " {} jump to latest {} {key} ",
            cx.t.status("running").0.replace('◐', "v"),
            cx.t.structure("separator")
        );
        let s = if cx.t.opts.ascii {
            s
        } else {
            s.replace('v', "↓")
        };
        let w = width(&s) as u16;
        let rr = Rect::new(r.right().saturating_sub(w + 1), r.bottom() - 1, w, 1);
        let mut st = cx.st(ACCENT, RAISED);
        if cx.hovered(rr) {
            st = st.add_modifier(Modifier::UNDERLINED);
        }
        clear(buf, rr, st);
        put(buf, rr.x, rr.y, &s, st, w);
        hits.add(rr, Target::JumpLatest);
    }
    (total, h)
}

// ============================================================================
// waiting card and composer
// ============================================================================

/// (enabled, bold, focused)
type BtnState = (bool, bool, bool);

fn button(cx: &Cx, buf: &mut Buffer, pos: (u16, u16), label: &str, state: BtnState) -> Rect {
    let (enabled, bold, focused) = state;
    let s = format!("[ {label} ]");
    let mut style = if enabled {
        cx.st(PRIMARY, PANEL)
    } else {
        cx.st(MUTED, PANEL)
    };
    if bold && enabled {
        style = style.add_modifier(Modifier::BOLD);
    }
    if focused && enabled {
        style = style.add_modifier(Modifier::REVERSED);
    }
    let x1 = put(buf, pos.0, pos.1, &s, style, 40);
    Rect::new(pos.0, pos.1, x1 - pos.0, 1)
}

fn draw_waiting(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.height < 3 {
        return;
    }
    let a = cx.app;
    let Some(w) = a.wait_focused() else { return };
    let items = a.wait_items();
    let at = a.ui.wait_idx.min(items.len().saturating_sub(1));
    let focused = a.ui.focus == Focus::Waiting;
    clear(buf, r, cx.st(SECONDARY, PANEL));
    let rail_style = if focused {
        cx.st(ACCENT, PANEL).add_modifier(Modifier::BOLD)
    } else {
        cx.st(ACCENT, PANEL)
    };
    for y in r.y..r.bottom() {
        put(buf, r.x, y, cx.t.structure("rail_heavy"), rail_style, 1);
    }
    let x = r.x + 2;
    let inner_w = r.width.saturating_sub(4);
    let (wg, wl, wt) = cx.t.status("waiting");
    let child = w
        .child
        .as_ref()
        .map(|c| format!("{}{} {c}", cx.sep().trim_start(), "child"))
        .unwrap_or_default();
    let _ = child;
    // title row
    let head_right = if items.len() > 1 {
        format!(
            "{} of {}  {}",
            at + 1,
            items.len(),
            a.manifest
                .command("waiting.next")
                .and_then(|c| c.tui_key())
                .map(|k| format!("{k} next"))
                .unwrap_or_default()
        )
    } else {
        String::new()
    };
    let hr_w = width(&head_right) as u16;
    let mut tx = put(buf, x, r.y, wg, cx.st(wt, PANEL), 2);
    tx = put(
        buf,
        tx + 1,
        r.y,
        wl,
        cx.st(PRIMARY, PANEL).add_modifier(Modifier::BOLD),
        10,
    );
    let (what, detail): (String, Vec<String>) = match w.kind {
        WaitKind::Approval => {
            let ap = a.state.model.approvals.get(&w.id);
            let tool = ap.map(|p| p.tool.clone()).unwrap_or_default();
            let mut lines = Vec::new();
            if let Some(p) = ap {
                match &p.args {
                    Value::Object(m) => {
                        for (k, v) in m.iter().take(2) {
                            lines.push(format!("{k}: {}", json_str(v).replace('\n', " ")));
                        }
                    }
                    other => lines.push(json_str(other)),
                }
            }
            (tool, lines)
        }
        WaitKind::Question => {
            let mut lines = Vec::new();
            if let Some(c) = a.ask_current() {
                lines.push(
                    json_str(&c.question["question"])
                        .trim_matches('"')
                        .replace('\n', " "),
                );
            }
            ("question".into(), lines)
        }
    };
    let who = match &w.child {
        Some(c) => format!("{}{what} in child: {c}", cx.sep()),
        None => format!("{}{what}", cx.sep()),
    };
    let room = (inner_w.saturating_sub(hr_w + 2)).saturating_sub(tx - x) as usize;
    put(
        buf,
        tx,
        r.y,
        &truncate(&who, room, cx.ell()),
        cx.st(SECONDARY, PANEL),
        room as u16,
    );
    if hr_w > 0 {
        let hx = r.right().saturating_sub(hr_w + 1);
        put(buf, hx, r.y, &head_right, cx.st(MUTED, PANEL), hr_w);
        hits.add(Rect::new(hx, r.y, hr_w, 1), Target::Wait(WaitButton::Next));
    }
    for (i, l) in detail.iter().enumerate().take(2) {
        put(
            buf,
            x + 2,
            r.y + 1 + i as u16,
            &truncate(l, inner_w.saturating_sub(2) as usize, cx.ell()),
            cx.st(SECONDARY, PANEL),
            inner_w,
        );
    }
    // buttons: choices Core does not offer are shown disabled with the reason
    let by = r.y + 3;
    let writable = a.writable();
    match w.kind {
        WaitKind::Approval => {
            let mut bx = x + 2;
            let defs = [
                ("allow once", "waiting.allow_once", Some(WaitButton::Allow)),
                ("allow session", "waiting.allow_session", None),
                ("deny", "waiting.deny", Some(WaitButton::Deny)),
                ("deny with note", "waiting.deny_note", None),
            ];
            for (label, id, target) in defs {
                let enabled = target.is_some() && writable;
                let rect = button(
                    cx,
                    buf,
                    (bx, by),
                    label,
                    (
                        enabled,
                        id == "waiting.allow_once",
                        focused && id == "waiting.allow_once",
                    ),
                );
                if let (Some(t), true) = (target, enabled) {
                    hits.add(rect, Target::Wait(t));
                }
                bx = rect.right() + 2;
            }
            let hint = defs
                .iter()
                .filter(|d| d.2.is_some())
                .filter_map(|d| {
                    a.manifest
                        .command(d.1)
                        .and_then(|c| c.tui_key().map(|k| format!("{k} {}", d.0)))
                })
                .collect::<Vec<_>>()
                .join(&cx.sep());
            put(
                buf,
                x + 2,
                by + 1,
                &truncate(&hint, inner_w as usize, cx.ell()),
                cx.st(MUTED, PANEL),
                inner_w,
            );
            let reason = a
                .manifest
                .command("waiting.allow_session")
                .and_then(|c| c.unavailable_reason.clone())
                .unwrap_or_default();
            let note = if writable {
                "allow session, deny with note: not offered by Core yet.".to_owned()
            } else {
                reason
            };
            put(
                buf,
                x + 2,
                by + 2,
                &truncate(&note, inner_w as usize, cx.ell()),
                cx.st(MUTED, PANEL),
                inner_w,
            );
        }
        WaitKind::Question => draw_question_body(cx, buf, r, (x, inner_w), writable, hits),
    }
}

/// The options (or the free-text prompt), a key hint and the submit button of
/// the focused question. Each option is a mouse target.
fn draw_question_body(
    cx: &Cx,
    buf: &mut Buffer,
    r: Rect,
    (x, inner_w): (u16, u16),
    writable: bool,
    hits: &mut HitMap,
) {
    let a = cx.app;
    let Some(c) = a.ask_current() else { return };
    let focused = a.ui.focus == Focus::Waiting;
    let opts = c.options();
    let mut y = r.y + 2;
    if opts.is_empty() {
        put(
            buf,
            x + 2,
            y,
            "Type the answer in the composer below, then press enter.",
            cx.st(SECONDARY, PANEL),
            inner_w,
        );
    } else {
        let shown = opts.len().min(ASK_ROWS);
        let top = c
            .draft
            .cursor
            .saturating_sub(shown - 1)
            .min(opts.len() - shown);
        for (i, o) in opts.iter().enumerate().skip(top).take(shown) {
            let on = c.draft.chosen.contains(&i);
            let mark = match (c.multiple(), on) {
                (true, true) => "[x]",
                (true, false) => "[ ]",
                (false, true) => "(*)",
                (false, false) => "( )",
            };
            let num = (i + 1) % 10;
            let label = json_str(&o["label"]);
            let line = format!("{mark} {num} {}", label.trim_matches('"'));
            let at = focused && i == c.draft.cursor;
            let mut st = cx.st(if at { PRIMARY } else { SECONDARY }, PANEL);
            if at {
                st = st.add_modifier(Modifier::REVERSED);
            }
            let w = (inner_w.saturating_sub(2) as usize).min(width(&line));
            put(
                buf,
                x + 2,
                y,
                &truncate(&line, inner_w.saturating_sub(2) as usize, cx.ell()),
                st,
                inner_w,
            );
            if writable {
                hits.add(Rect::new(x + 2, y, w as u16, 1), Target::WaitOption(i));
            }
            y += 1;
        }
    }
    let by = r.bottom().saturating_sub(2);
    let step = if c.total > 1 {
        format!("question {} of {}  ", c.index + 1, c.total)
    } else {
        String::new()
    };
    let hint = if c.draft.sent {
        "sending the answers...".to_owned()
    } else if opts.is_empty() {
        format!("{step}enter in the composer sends the answer")
    } else {
        let pick = if c.multiple() {
            "space/number tick, enter confirms"
        } else {
            "number or up/down+enter picks"
        };
        let text = if c.allows_text() {
            ", or type a note"
        } else {
            ""
        };
        format!("{step}{pick}{text}")
    };
    put(
        buf,
        x + 2,
        by,
        &truncate(&hint, inner_w.saturating_sub(2) as usize, cx.ell()),
        cx.st(MUTED, PANEL),
        inner_w,
    );
    let last = c.index + 1 >= c.total;
    let label = if last {
        "submit answers"
    } else {
        "next question"
    };
    let enabled = writable && !c.draft.sent;
    let rect = button(cx, buf, (x + 2, by + 1), label, (enabled, true, false));
    if enabled {
        hits.add(rect, Target::Wait(WaitButton::Submit));
    }
    let key = a
        .manifest
        .command("waiting.submit_answers")
        .and_then(|c| c.tui_key())
        .map(|k| format!("{k} {label}"))
        .unwrap_or_default();
    put(
        buf,
        rect.right() + 2,
        by + 1,
        &truncate(
            &format!("{key}{}esc stops the turn", cx.sep()),
            inner_w.saturating_sub(rect.width + 4) as usize,
            cx.ell(),
        ),
        cx.st(MUTED, PANEL),
        inner_w,
    );
}

/// Display parts of a `provider/id` model reference. The catalog name wins over
/// the id; the id is the fallback for a model the catalog no longer lists.
fn model_parts(app: &App, reference: &str) -> (String, String, String) {
    let (provider, id) = reference
        .split_once('/')
        .map_or(("codex", reference), |(p, i)| (p, i));
    let name = app
        .state
        .models
        .iter()
        .find(|m| m["ref"].as_str() == Some(reference))
        .and_then(|m| m["name"].as_str())
        .filter(|n| !n.is_empty())
        .unwrap_or(id);
    (provider.to_owned(), name.to_owned(), id.to_owned())
}

fn model_name_for_id(app: &App, id: &str) -> String {
    app.state
        .models
        .iter()
        .find(|m| {
            m["ref"]
                .as_str()
                .and_then(|r| r.rsplit('/').next())
                .is_some_and(|listed| listed == id)
        })
        .and_then(|m| m["name"].as_str())
        .filter(|n| !n.is_empty())
        .unwrap_or(id)
        .to_owned()
}

fn percent(used: u64, limit: u64) -> String {
    format!("{:0.1}%", 100.0 * used as f64 / limit as f64)
}

/// A pending model change keeps `(next call)`; names shorten first so the
/// marker is never what falls off the row.
fn pending_model_label(old: &str, new: &str, arrow: &str, room: usize, ell: &str) -> String {
    const SUFFIX: &str = "(next call)";
    let fixed = width(SUFFIX) + width(arrow) + 3; // three single spaces
    if room > fixed + 2 {
        let names = room - fixed;
        let left = names / 2;
        return format!(
            "{} {arrow} {} {SUFFIX}",
            truncate(old, left, ell),
            truncate(new, names - left, ell)
        );
    }
    truncate(&format!("{old} {arrow} {new} {SUFFIX}"), room, ell)
}

/// One row of context accounting: what to print and what a hover explains.
struct ComposerUsage {
    numbers: Vec<String>,
    lines: Vec<String>,
}

/// The latest request's input is an estimate of occupied context. Occupancy is
/// shown against each price tier boundary and the window, as old Nexus did.
fn composer_usage(cx: &Cx, session: &crate::api::Session) -> Option<ComposerUsage> {
    let events = cx.app.state.model.selected.as_ref()?.events.as_slice();
    let input = events
        .iter()
        .rev()
        .find(|event| event.kind == "usage")
        .and_then(|event| event.data.get("input_tokens"))
        .and_then(Value::as_u64)?;
    let model = session.model.as_deref().unwrap_or_default();
    let entry = cx
        .app
        .state
        .models
        .iter()
        .find(|m| m["ref"].as_str() == Some(model));
    let window = entry.and_then(|m| m["context"].as_u64()).filter(|&n| n > 0);
    let mut tiers: Vec<u64> = entry
        .and_then(|m| m["tiers"].as_array())
        .map(|tiers| tiers.iter().filter_map(Value::as_u64).collect())
        .unwrap_or_default();
    tiers.retain(|&tier| tier > 0 && Some(tier) != window);
    tiers.sort_unstable();
    tiers.dedup();
    let mut numbers = vec![text::count(input)];
    numbers.extend(tiers.iter().map(|tier| percent(input, *tier)));
    if let Some(window) = window {
        numbers.push(percent(input, window));
    }
    let mut lines = vec![match window {
        Some(window) => format!(
            "Context: {} of {} tokens",
            text::count(input),
            text::count(window)
        ),
        None => format!("Context: {} tokens (window unknown)", text::count(input)),
    }];
    let mut detail: Vec<String> = tiers
        .iter()
        .map(|tier| format!("{} at {}", percent(input, *tier), text::count(*tier)))
        .collect();
    if let Some(window) = window {
        detail.push(format!("{} of window", percent(input, window)));
    }
    if !detail.is_empty() {
        lines.push(detail.join(" · "));
    }
    lines.push("Estimated from the latest request's input tokens.".to_owned());
    Some(ComposerUsage { numbers, lines })
}

fn draw_composer(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.height < 6 || r.width < 8 {
        return;
    }
    let a = cx.app;
    let focused = a.ui.focus == Focus::Composer;
    let base = cx.st(PRIMARY, PANEL);
    let panel = Rect::new(r.x + 1, r.y, r.width.saturating_sub(2), r.height - 2);
    clear(buf, panel, base);
    hits.add(panel, Target::Composer);
    if focused
        && matches!(
            cx.t.opts.tier,
            crate::theme::Tier::Ansi16 | crate::theme::Tier::Mono
        )
    {
        let inner = composer_inner(r);
        for y in inner.y..inner.bottom() {
            put(
                buf,
                r.x + 1,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", PANEL),
                1,
            );
        }
    }

    // input
    let inner = composer_inner(r);
    let comp = a.composer_ref();
    if comp.is_empty() {
        let ph = if !a.writable() {
            format!("Disconnected {} read-only", cx.t.structure("separator"))
        } else if a.viewed().is_none() {
            "Message to start a new session".to_owned()
        } else {
            "Type a message\u{2026}".replace('\u{2026}', cx.ell())
        };
        put(buf, inner.x, inner.y, &ph, cx.st(MUTED, PANEL), inner.width);
    } else {
        let (rows, (cr, _)) = comp.display_rows(inner.width as usize);
        let first = a.ui.composer_drag.map_or_else(
            || visible_first(rows.len(), cr, inner.height as usize),
            |(_, top)| top,
        );
        for (i, row) in rows
            .iter()
            .skip(first)
            .take(inner.height as usize)
            .enumerate()
        {
            let mut x = inner.x;
            for (text, selected) in row {
                let style = if *selected {
                    base.add_modifier(Modifier::REVERSED)
                } else {
                    base
                };
                x = put(
                    buf,
                    x,
                    inner.y + i as u16,
                    text,
                    style,
                    inner.right().saturating_sub(x),
                );
            }
        }
        if rows.len() > inner.height as usize {
            let more = format!("{}/{}", cr + 1, rows.len());
            put(
                buf,
                r.right().saturating_sub(width(&more) as u16 + 2),
                r.y,
                &more,
                cx.st(MUTED, PANEL),
                8,
            );
        }
    }

    // Selectors and actions share the control row; each has its own hit target.
    // The right block is computed first: the left side gives way in the order
    // effort → provider → extra percentages → truncated names (A2).
    let cy = r.bottom() - 3;
    let avail = r.width.saturating_sub(6) as usize;
    let busy = a.viewed_id().is_some_and(|i| a.is_busy(i));
    let interrupted = a.term("session.interrupted").unwrap_or(false);
    let session = a.viewed();
    let usage = session.and_then(|s| composer_usage(cx, s));
    let mut numbers = usage
        .as_ref()
        .map(|usage| usage.numbers.clone())
        .unwrap_or_default();
    let request_model = a
        .state
        .model
        .selected
        .as_ref()
        .and_then(|view| {
            view.events
                .iter()
                .rev()
                .find(|event| event.kind == "wire_request")
        })
        .and_then(|event| event.data["body"]["model"].as_str())
        .map(str::to_owned);
    let mut effort_on = true;
    let mut provider_on = true;
    let mut agent_label = session
        .map(|s| text::capitalize(&s.agent))
        .unwrap_or_default();
    let (provider, model_name, model_id) = session
        .and_then(|s| s.model.as_deref().filter(|m| !m.is_empty()))
        .map_or_else(
            || {
                (
                    "codex".to_owned(),
                    "default model".to_owned(),
                    String::new(),
                )
            },
            |reference| model_parts(a, reference),
        );
    let pending = busy
        .then_some(request_model)
        .flatten()
        .filter(|used| !model_id.is_empty() && used.rsplit('/').next() != Some(model_id.as_str()));
    let mut model_label = match &pending {
        Some(used) => format!(
            "{} {} {} (next call)",
            model_name_for_id(a, used),
            cx.t.structure("arrow"),
            model_name
        ),
        None if provider_on && !model_id.is_empty() => format!("{model_name} {provider}"),
        None => model_name.clone(),
    };
    let effort = session.map_or("default", |s| {
        s.extra
            .get("effort")
            .and_then(Value::as_str)
            .unwrap_or("default")
    });
    loop {
        let text = numbers.join(&cx.sep());
        let left_width = width(&agent_label)
            + 3
            + width(&model_label)
            + if effort_on { 3 + width(effort) } else { 0 }
            + if busy || interrupted { 11 } else { 0 };
        let right_width = width(&text);
        if left_width + right_width + 2 <= avail {
            break;
        }
        if effort_on {
            effort_on = false;
        } else if provider_on && pending.is_none() && !model_id.is_empty() {
            provider_on = false;
            model_label = model_name.clone();
        } else if numbers.len() > 2 || (numbers.len() > 1 && avail < 40) {
            numbers.pop();
        } else {
            // Last resort: keep the agent and one context number, shorten the model.
            let room = avail.saturating_sub(width(&agent_label) + 3 + width(&text) + 2);
            model_label = match pending.as_deref() {
                Some(used) => pending_model_label(
                    &model_name_for_id(a, used),
                    &model_name,
                    cx.t.structure("arrow"),
                    room,
                    cx.ell(),
                ),
                None => truncate(&model_label, room, cx.ell()),
            };
            if width(&agent_label) + 3 + width(&model_label) + 2 + width(&text) > avail {
                let room = avail.saturating_sub(width(&model_label) + 3 + width(&text) + 2);
                agent_label = truncate(&agent_label, room.max(1), cx.ell());
            }
            break;
        }
    }
    let text = numbers.join(&cx.sep());
    let right_width = width(&text) as u16;
    let end = r.right().saturating_sub(5 + right_width);
    let mut context_rect = None;
    if !text.is_empty() {
        let rx = r.right().saturating_sub(3 + right_width);
        put(buf, rx, cy, &text, cx.st(MUTED, PANEL), right_width);
        context_rect = Some(Rect::new(rx, cy, right_width, 1));
        hits.add(context_rect.unwrap(), Target::ComposerContext);
    }
    let selector_end = if busy || interrupted {
        end.saturating_sub(11).max(r.x + 3)
    } else {
        end
    };
    let mut x = r.x + 3;
    if let Some(session) = session {
        let fields: Vec<(String, Target, &str)> = vec![
            (agent_label, Target::ComposerAgent, PRIMARY),
            (model_label, Target::ComposerModel, PRIMARY),
            (effort.to_owned(), Target::ComposerEffort, MUTED),
        ];
        let mut first = true;
        for (index, (label, target, token)) in fields.into_iter().enumerate() {
            if index == 2 && !effort_on {
                continue;
            }
            if x >= selector_end {
                break;
            }
            if !first {
                x = put(buf, x, cy, &cx.sep(), cx.st(MUTED, PANEL), selector_end - x);
            }
            first = false;
            let start = x;
            let room = selector_end.saturating_sub(x);
            let label = if index == 1 {
                if let (Some(used), true) = (pending.as_deref(), width(&label) > room as usize) {
                    pending_model_label(
                        &model_name_for_id(a, used),
                        &model_name,
                        cx.t.structure("arrow"),
                        room as usize,
                        cx.ell(),
                    )
                } else {
                    truncate(&label, room as usize, cx.ell())
                }
            } else {
                truncate(&label, room as usize, cx.ell())
            };
            let style = if target == Target::ComposerAgent {
                cx.st(token, PANEL).patch(cx.t.agent_style(&session.agent))
            } else {
                cx.st(token, PANEL)
            };
            x = put(buf, x, cy, &label, style, room);
            if x > start {
                hits.add(Rect::new(start, cy, x - start, 1), target);
            }
        }
    }
    if busy || interrupted {
        if x < end {
            x = put(buf, x, cy, &cx.sep(), cx.st(MUTED, PANEL), end - x);
        }
        let start = x;
        let (label, target) = if busy {
            ("esc stop", Target::Stop)
        } else {
            ("continue", Target::Continue)
        };
        x = put(
            buf,
            x,
            cy,
            label,
            cx.st(MUTED, PANEL),
            end.saturating_sub(x),
        );
        if x > start {
            hits.add(Rect::new(start, cy, x - start, 1), target);
        }
    }
    let project = a.current_project();
    let directory = a.viewed().map(|s| s.directory.clone()).or_else(|| {
        project
            .as_ref()
            .and_then(|p| a.state.model.projects.get(p).map(|p| p.path.clone()))
    });
    let listing = a
        .viewed()
        .and_then(|s| a.state.worktrees.get(&s.id))
        .or_else(|| {
            project
                .as_ref()
                .and_then(|p| a.state.project_worktrees.get(p))
        });
    if let Some(directory) = directory {
        let directory = directory.as_str();
        let path = short_home(directory);
        let worktree = listing
            .filter(|listing| {
                listing["session_directory"]
                    .as_str()
                    .is_none_or(|root| root == directory)
                    && listing["project_directory"]
                        .as_str()
                        .is_none_or(|root| std::path::Path::new(directory).starts_with(root))
            })
            .and_then(|listing| listing["worktrees"].as_array())
            .and_then(|trees| {
                trees
                    .iter()
                    .filter(|tree| {
                        tree["path"]
                            .as_str()
                            .is_some_and(|root| std::path::Path::new(directory).starts_with(root))
                    })
                    .max_by_key(|tree| tree["path"].as_str().map_or(0, str::len))
            });
        let branch = worktree
            .and_then(|tree| tree["branch"].as_str())
            .filter(|b| !b.is_empty());
        let linked = worktree.is_some_and(|tree| tree["main"].as_bool() == Some(false));
        // Location strip: project name (underlined by the accent rule
        // below), directory, branch and checkout kind.
        let y = r.bottom() - 2;
        let start = r.x + 2;
        let end = r.right().saturating_sub(2);
        let name = project
            .as_ref()
            .and_then(|p| a.state.model.projects.get(p))
            .map(|p| p.name.clone())
            .unwrap_or_default();
        let mut x = start;
        if !name.is_empty() {
            x = put(
                buf,
                x,
                y,
                &name,
                cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
                end.saturating_sub(x),
            );
        }
        let name_end = x;
        let kind = if linked { "worktree" } else { "checkout" };
        let tail = branch.map_or(0, |b| width(b) + width(kind) + 4);
        let room = (end.saturating_sub(x + 1) as usize).saturating_sub(tail);
        if room > 3 {
            x = put(buf, x, y, " ", cx.st(MUTED, CANVAS), 1);
            let begin = x;
            x = put(
                buf,
                x,
                y,
                &truncate_middle(&path, room, cx.ell()),
                cx.st(MUTED, CANVAS),
                end.saturating_sub(x),
            );
            if a.viewed().is_some() && x > begin {
                hits.add(Rect::new(begin, y, x - begin, 1), Target::ComposerDirectory);
            }
        }
        if let Some(branch) = branch {
            x = put(buf, x, y, "  ", cx.st(MUTED, CANVAS), end.saturating_sub(x));
            let begin = x;
            x = put(
                buf,
                x,
                y,
                branch,
                cx.st(SECONDARY, CANVAS),
                end.saturating_sub(x),
            );
            if x > begin {
                hits.add(Rect::new(begin, y, x - begin, 1), Target::ComposerWorktree);
            }
            let token = if linked { "agent.violet" } else { MUTED };
            put(
                buf,
                x,
                y,
                &format!("  {kind}"),
                cx.st(token, CANVAS),
                end.saturating_sub(x),
            );
        }
        // the rule closing the strip; the accent marks the project name
        let rule = cx.t.structure("rule");
        for rx in r.x + 1..r.right().saturating_sub(1) {
            let accent = rx >= start && rx < name_end;
            put(
                buf,
                rx,
                r.bottom() - 1,
                rule,
                if accent {
                    cx.st(ACCENT, CANVAS).add_modifier(Modifier::BOLD)
                } else {
                    cx.st("border.default", CANVAS)
                },
                1,
            );
        }
    }
    if let (Some(usage), Some(rect)) = (&usage, context_rect) {
        if cx.hovered(rect) {
            draw_usage_tooltip(cx, buf, r, usage);
        }
    }
}

/// A toast-sized explanation above the composer for the context numbers.
fn draw_usage_tooltip(cx: &Cx, buf: &mut Buffer, composer: Rect, usage: &ComposerUsage) {
    let widest = usage
        .lines
        .iter()
        .map(|line| width(line))
        .max()
        .unwrap_or(0);
    let w = (widest + 4).min(composer.width.saturating_sub(2) as usize) as u16;
    let h = usage.lines.len() as u16 + 2;
    if composer.y < h || w < 4 {
        return;
    }
    let area = Rect::new(composer.right().saturating_sub(w + 1), composer.y - h, w, h);
    boxed(cx, buf, area, "", RAISED);
    for (index, line) in usage.lines.iter().enumerate() {
        let style = if index == 0 {
            cx.st(PRIMARY, RAISED)
        } else {
            cx.st(MUTED, RAISED)
        };
        put(
            buf,
            area.x + 2,
            area.y + 1 + index as u16,
            &truncate(line, w.saturating_sub(4) as usize, cx.ell()),
            style,
            w.saturating_sub(4),
        );
    }
}

// ============================================================================
// settings view
// ============================================================================

fn on_off(on: bool) -> String {
    if on { "on" } else { "off" }.into()
}

/// The value shown for a settings row, and whether it is live (wired).
fn settings_value(cx: &Cx, row: &crate::manifest::Row) -> (String, bool) {
    let a = cx.app;
    let live = match row.id.as_str() {
        "appearance.theme" => Some(match a.theme.opts.mode {
            crate::theme::Mode::Dark => "dark".into(),
            crate::theme::Mode::Light => "light".into(),
        }),
        "appearance.monochrome" => Some(on_off(a.theme.mono())),
        "appearance.animations" => Some(on_off(!a.theme.opts.reduced_motion)),
        "appearance.sidebar" => Some(
            match (a.ui.left_open, a.ui.left_rail) {
                (false, _) => "hidden",
                (true, true) => "rail",
                (true, false) => "full",
            }
            .into(),
        ),
        "appearance.details" => Some(on_off(a.ui.right_open)),
        "appearance.session_scope" => Some(
            if a.ui.scope_all {
                "all projects"
            } else {
                "this project"
            }
            .into(),
        ),
        "appearance.show_archived" => Some(
            if a.ui.show_archived {
                "shown"
            } else {
                "hidden"
            }
            .into(),
        ),
        "about.versions" => {
            let core = a.state.core.as_ref();
            Some(format!(
                "client {} {sep} Core {} {sep} API {}",
                env!("CARGO_PKG_VERSION"),
                core.map_or("-".into(), |c| c.version.clone()),
                core.and_then(|c| c.api_version)
                    .map_or("unreported".into(), |v| format!("{}.{}", v.major, v.minor)),
                sep = cx.t.structure("separator"),
            ))
        }
        "about.daemon" => Some(a.state.core.as_ref().map_or("-".into(), |c| {
            format!("pid {}{}{}", c.pid, cx.sep(), c.home)
        })),
        "keybindings.list" => Some("enter".into()),
        _ => None,
    };
    match live {
        Some(v) => (v, true),
        None => {
            let v = match &row.default {
                Some(toml::Value::String(s)) => s.clone(),
                Some(toml::Value::Boolean(b)) => on_off(*b),
                Some(v) => v.to_string(),
                None => row.options.first().cloned().unwrap_or_default(),
            };
            (v, false)
        }
    }
}

/// Settings: a centred modal over the dimmed session (old Nexus layout):
/// title and `esc`, a search line, then every section's rows with values
/// right-aligned; left/right change the selected row.
fn draw_settings_modal(cx: &Cx, buf: &mut Buffer, area: Rect, hits: &mut HitMap) {
    let a = cx.app;
    // dim everything under the modal: muted text (or the dim attribute
    // without colour)
    let muted = cx.t.color(MUTED);
    for y in area.y..area.bottom() {
        for x in area.x..area.right() {
            let cell = &mut buf[(x, y)];
            match muted {
                Some(c) => {
                    cell.set_fg(c);
                }
                None => {
                    cell.set_style(cell.style().add_modifier(Modifier::DIM));
                }
            }
        }
    }
    hits.add(area, Target::Scrim);
    let r = settings_box(area);
    if r.width < 20 || r.height < 8 {
        return;
    }
    let bg = PANEL;
    clear(buf, r, cx.st(PRIMARY, bg));
    hits.add(r, Target::Swallow);
    let x = r.x + 4;
    let w = r.width.saturating_sub(8);
    put(
        buf,
        x,
        r.y + 1,
        "Settings",
        cx.st(PRIMARY, bg).add_modifier(Modifier::BOLD),
        w,
    );
    let close = Rect::new(r.right().saturating_sub(6), r.y + 1, 3, 1);
    put(buf, close.x, close.y, "esc", cx.st(MUTED, bg), 3);
    hits.add(close, Target::SettingsEntry);
    let query = a.ui.settings_search.text();
    if query.is_empty() {
        put(buf, x, r.y + 3, "Search", cx.st(MUTED, bg), w);
    } else {
        put(buf, x, r.y + 3, &query, cx.st(PRIMARY, bg), w);
    }

    let items = a.settings_items();
    let rows = a.settings_rows();
    let sel = a.ui.settings_row.min(rows.len().saturating_sub(1));
    let sel_item = rows.get(sel).copied();
    // footer: the selected row's note or reason, then the key hints
    let foot_y = r.bottom().saturating_sub(2);
    let note_y = foot_y.saturating_sub(1);
    let list = Rect::new(r.x, r.y + 5, r.width, note_y.saturating_sub(r.y + 6));
    if items.is_empty() {
        put(buf, x, list.y, "No settings match.", cx.st(MUTED, bg), w);
    }
    // a blank line before every heading but the first
    let mut lines: Vec<Option<usize>> = Vec::new();
    for (i, item) in items.iter().enumerate() {
        if matches!(item, crate::app::SettingsItem::Heading(_)) && i > 0 {
            lines.push(None);
        }
        lines.push(Some(i));
    }
    let h = list.height as usize;
    let sel_line = sel_item
        .and_then(|i| lines.iter().position(|l| *l == Some(i)))
        .unwrap_or(0);
    // keep the selection's heading in view when scrolling up to it
    let top = (sel_line + 1).saturating_sub(h);
    for (li, line) in lines.iter().enumerate().skip(top).take(h) {
        let y = list.y + (li - top) as u16;
        let Some(i) = line else { continue };
        match &items[*i] {
            crate::app::SettingsItem::Heading(label) => {
                put(
                    buf,
                    x,
                    y,
                    label,
                    cx.st("agent.violet", bg).add_modifier(Modifier::BOLD),
                    w,
                );
            }
            crate::app::SettingsItem::Row {
                row,
                section_reason,
            } => {
                let position = rows.iter().position(|p| p == i).unwrap_or(0);
                let selected = Some(*i) == sel_item;
                let disabled = row.unavailable_reason.is_some() || section_reason.is_some();
                let (value, wired) = settings_value(cx, row);
                let band = Rect::new(r.x + 1, y, r.width.saturating_sub(2), 1);
                let mut style = cx.st(if disabled || !wired { MUTED } else { PRIMARY }, bg);
                let mut vstyle = cx.st(if wired && !disabled { SECONDARY } else { MUTED }, bg);
                if selected {
                    style = cx
                        .st("interactive.on_accent", "interactive.accent")
                        .add_modifier(Modifier::BOLD);
                    vstyle = cx.st("interactive.on_accent", "interactive.accent");
                    clear(buf, band, style);
                    put(
                        buf,
                        r.x + 2,
                        y,
                        if cx.t.opts.ascii { "*" } else { "●" },
                        style,
                        1,
                    );
                } else if cx.hovered(band) {
                    style = style.patch(cx.t.bg("interactive.hover"));
                    vstyle = vstyle.patch(cx.t.bg("interactive.hover"));
                    clear(buf, band, style);
                }
                let value = if disabled {
                    "unavailable".to_owned()
                } else {
                    value
                };
                let vw = (width(&value) as u16).min(w / 2);
                let vx = x + w - vw;
                put(
                    buf,
                    x,
                    y,
                    &truncate(&row.label, (w - vw).saturating_sub(2) as usize, cx.ell()),
                    style,
                    (w - vw).saturating_sub(2),
                );
                put(
                    buf,
                    vx,
                    y,
                    &truncate(&value, vw as usize, cx.ell()),
                    vstyle,
                    vw,
                );
                hits.add(band, Target::SettingsRow(position));
            }
        }
    }
    if let Some(crate::app::SettingsItem::Row {
        row,
        section_reason,
    }) = sel_item.and_then(|i| items.get(i))
    {
        let note = row
            .unavailable_reason
            .clone()
            .or(section_reason.clone())
            .or_else(|| (!App::settings_wired(&row.id)).then(|| "Not available yet.".to_owned()))
            .unwrap_or_else(|| row.note.clone());
        put(
            buf,
            x,
            note_y,
            &truncate(&note, w as usize, cx.ell()),
            cx.st(MUTED, bg),
            w,
        );
    }
    let arrows = if cx.t.opts.ascii { "</>" } else { "←/→" };
    put(
        buf,
        x,
        foot_y,
        arrows,
        cx.st(PRIMARY, bg).add_modifier(Modifier::BOLD),
        w,
    );
    put(
        buf,
        x + width(arrows) as u16 + 1,
        foot_y,
        "change  ·  enter select  ·  type to search",
        cx.st(MUTED, bg),
        w.saturating_sub(width(arrows) as u16 + 1),
    );
}

pub(crate) fn settings_box(area: Rect) -> Rect {
    let w = area.width.saturating_sub(4).clamp(20, 72);
    let h = area.height.saturating_sub(4).clamp(8, 44);
    Rect::new(
        area.x + area.width.saturating_sub(w) / 2,
        area.y + area.height.saturating_sub(h) / 2,
        w,
        h,
    )
}

// ============================================================================
// overlays and dialogs
// ============================================================================

fn palette_box(area: Rect) -> Rect {
    let w = area.width.saturating_sub(4).clamp(20, 72);
    let h = area.height.saturating_sub(4).clamp(8, 18);
    Rect::new(
        area.x + (area.width - w) / 2,
        area.y + 1.min(area.height.saturating_sub(h)) + area.height.saturating_sub(h + 2) / 4,
        w,
        h,
    )
}

fn boxed(cx: &Cx, buf: &mut Buffer, r: Rect, title: &str, bg: &str) {
    let base = cx.st(PRIMARY, bg);
    clear(buf, r, base);
    let set = cx.t.border_set();
    let b = cx.st(MUTED, bg);
    for x in r.x + 1..r.right() - 1 {
        put(buf, x, r.y, set.horizontal_top, b, 1);
        put(buf, x, r.bottom() - 1, set.horizontal_bottom, b, 1);
    }
    for y in r.y + 1..r.bottom() - 1 {
        put(buf, r.x, y, set.vertical_left, b, 1);
        put(buf, r.right() - 1, y, set.vertical_right, b, 1);
    }
    put(buf, r.x, r.y, set.top_left, b, 1);
    put(buf, r.right() - 1, r.y, set.top_right, b, 1);
    put(buf, r.x, r.bottom() - 1, set.bottom_left, b, 1);
    put(buf, r.right() - 1, r.bottom() - 1, set.bottom_right, b, 1);
    if !title.is_empty() {
        put(
            buf,
            r.x + 2,
            r.y,
            &format!(" {title} "),
            base.add_modifier(Modifier::BOLD),
            r.width.saturating_sub(4),
        );
    }
}

fn draw_overlay(cx: &Cx, buf: &mut Buffer, area: Rect, o: &Overlay, hits: &mut HitMap) {
    hits.add(area, Target::Scrim);
    match &o.kind {
        OverlayKind::Palette(p) => draw_palette(cx, buf, area, p, hits),
        OverlayKind::Notifications { selected } => {
            draw_notifications(cx, buf, area, *selected, hits)
        }
        OverlayKind::Help { top } => draw_help(cx, buf, area, *top, hits),
    }
}

fn draw_palette(cx: &Cx, buf: &mut Buffer, area: Rect, p: &Palette, hits: &mut HitMap) {
    let b = palette_box(area);
    let title = match p.mode {
        PaletteMode::Commands => "Command palette",
        PaletteMode::Sessions => "Switch session",
        PaletteMode::Projects => "Switch project",
        PaletteMode::Models => "Change model",
        PaletteMode::Agents => "Change agent",
        PaletteMode::Efforts => "Reasoning effort",
        PaletteMode::Export => "Export session",
        PaletteMode::Turns => "Go to turn",
    };
    boxed(cx, buf, b, title, RAISED);
    hits.add(b, Target::Swallow);
    let base = cx.st(PRIMARY, RAISED);
    // query row
    put(
        buf,
        b.x + 2,
        b.y + 1,
        ">",
        cx.st(ACCENT, RAISED).add_modifier(Modifier::BOLD),
        1,
    );
    let q = p.query.text();
    if q.is_empty() {
        let ph = match p.mode {
            PaletteMode::Commands => "type to filter commands",
            PaletteMode::Sessions => "type to find a session",
            PaletteMode::Projects => "type to find a project",
            PaletteMode::Models => "type to filter models",
            PaletteMode::Agents => "type to filter agents",
            PaletteMode::Efforts => "type to filter efforts",
            PaletteMode::Export => "choose a format and scope",
            PaletteMode::Turns => "type to find a turn",
        };
        put(buf, b.x + 4, b.y + 1, ph, cx.st(MUTED, RAISED), b.width - 6);
    } else {
        put(
            buf,
            b.x + 4,
            b.y + 1,
            &q.replace('\n', " "),
            base,
            b.width - 6,
        );
    }
    for x in b.x + 1..b.right() - 1 {
        put(
            buf,
            x,
            b.y + 2,
            cx.t.structure("rule"),
            cx.st("border.default", RAISED),
            1,
        );
    }
    let items = cx.app.palette_items();
    let list = Rect::new(b.x + 1, b.y + 3, b.width - 2, b.height.saturating_sub(5));
    if items.is_empty() {
        put(
            buf,
            list.x + 2,
            list.y,
            "Nothing matches.",
            cx.st(MUTED, RAISED),
            list.width - 2,
        );
    }
    let h = list.height as usize;
    let top = if p.selected < h {
        0
    } else {
        p.selected + 1 - h
    };
    for (i, it) in items.iter().enumerate().skip(top).take(h) {
        let y = list.y + (i - top) as u16;
        let rr = Rect::new(list.x, y, list.width, 1);
        let selected = i == p.selected;
        let mut style = if it.reason.is_some() {
            cx.st(MUTED, RAISED)
        } else {
            base
        };
        if selected {
            style = style
                .patch(cx.t.bg("interactive.selection"))
                .patch(if it.reason.is_some() {
                    Style::default()
                } else {
                    cx.st(PRIMARY, "interactive.selection")
                });
            clear(buf, rr, style);
            put(
                buf,
                rr.x,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", "interactive.selection")
                    .add_modifier(Modifier::BOLD),
                1,
            );
        } else if cx.hovered(rr) {
            style = style.patch(cx.t.bg("interactive.hover"));
            clear(buf, rr, style);
        }
        let hint = it.hint.clone();
        let hw = width(&hint) as u16;
        let hx = rr.right().saturating_sub(hw + 1);
        if hw > 0 {
            put(
                buf,
                hx,
                y,
                &hint,
                style.patch(cx.st(MUTED, RAISED)).patch(if selected {
                    cx.t.bg("interactive.selection")
                } else {
                    Style::default()
                }),
                hw,
            );
        }
        let room = hx.saturating_sub(rr.x + 3 + 1) as usize;
        let lw = width(&it.label).min(room);
        let mut label_style = if selected {
            style.add_modifier(Modifier::BOLD)
        } else {
            style
        };
        if let PaletteKind::Agent(name) = &it.kind {
            label_style = label_style.patch(cx.t.agent_style(name));
        }
        let x1 = put(
            buf,
            rr.x + 2,
            y,
            &truncate(&it.label, room, cx.ell()),
            label_style,
            room as u16,
        );
        if let Some(reason) = &it.reason {
            let rrm = (hx.saturating_sub(x1 + 3)) as usize;
            if rrm > 8 {
                put(
                    buf,
                    x1 + 2,
                    y,
                    &truncate(
                        &format!("{} {reason}", cx.t.structure("separator")),
                        rrm,
                        cx.ell(),
                    ),
                    style.patch(cx.st(MUTED, RAISED)).patch(if selected {
                        cx.t.bg("interactive.selection")
                    } else {
                        Style::default()
                    }),
                    rrm as u16,
                );
            }
        }
        let _ = lw;
        hits.add(rr, Target::PaletteItem(i));
    }
    let foot = if items.get(p.selected).is_some_and(|i| i.reason.is_some()) {
        "enter explains why it is off - esc close"
    } else {
        "enter run - esc close"
    };
    let foot = foot.replace(" - ", &cx.sep());
    put(
        buf,
        b.x + 2,
        b.bottom() - 2,
        &truncate(&foot, b.width as usize - 4, cx.ell()),
        cx.st(MUTED, RAISED),
        b.width - 4,
    );
    let count = format!(
        "{}/{}",
        if items.is_empty() { 0 } else { p.selected + 1 },
        items.len()
    );
    put(
        buf,
        b.right().saturating_sub(width(&count) as u16 + 2),
        b.bottom() - 2,
        &count,
        cx.st(MUTED, RAISED),
        10,
    );
}

fn draw_notifications(cx: &Cx, buf: &mut Buffer, area: Rect, selected: usize, hits: &mut HitMap) {
    let w = 46u16.min(area.width.saturating_sub(2));
    let b = Rect::new(
        area.right() - w,
        area.y + 1,
        w,
        area.height.saturating_sub(2),
    );
    boxed(cx, buf, b, "Notifications", RAISED);
    hits.add(b, Target::Swallow);
    let hist = &cx.app.ui.alerts.history;
    if hist.is_empty() {
        put(
            buf,
            b.x + 2,
            b.y + 2,
            "Nothing yet. Toasts and alerts are kept here.",
            cx.st(MUTED, RAISED),
            b.width - 4,
        );
        return;
    }
    let mut y = b.y + 1;
    for (i, n) in hist.iter().enumerate() {
        let rows = text::wrap(&n.text, b.width.saturating_sub(7) as usize);
        let h = rows.len().min(2) as u16;
        if y + h > b.bottom() - 1 {
            break;
        }
        let rr = Rect::new(b.x + 1, y, b.width - 2, h);
        let mut style = cx.st(PRIMARY, RAISED);
        if i == selected {
            style = style.patch(cx.t.bg("interactive.selection"));
            clear(buf, rr, style);
        }
        let (g, tok) = cx.level(n.level);
        put(
            buf,
            rr.x + 1,
            y,
            g,
            cx.st(tok, RAISED).patch(if i == selected {
                cx.t.bg("interactive.selection")
            } else {
                Style::default()
            }),
            2,
        );
        for (j, l) in rows.iter().take(2).enumerate() {
            put(buf, rr.x + 4, y + j as u16, l, style, rr.width - 5);
        }
        hits.add(rr, Target::NoticeItem(i));
        y += h;
    }
}

fn draw_help(cx: &Cx, buf: &mut Buffer, area: Rect, top: usize, hits: &mut HitMap) {
    let b = palette_box(area);
    let title = cx
        .app
        .state
        .core
        .as_ref()
        .filter(|c| !c.version.is_empty())
        .map(|c| format!("Keyboard shortcuts · Core {}", c.version))
        .unwrap_or_else(|| "Keyboard shortcuts".into());
    boxed(cx, buf, b, &title, RAISED);
    hits.add(b, Target::Swallow);
    let mut rows: Vec<(String, String)> = cx
        .app
        .manifest
        .command
        .iter()
        .filter(|c| c.for_tui() && c.tui_key().is_some())
        .map(|c| (c.tui_key().unwrap_or("").to_owned(), c.title.clone()))
        .collect();
    rows.push(("f6 / shift+f6".into(), "Cycle focus between panes".into()));
    rows.push((
        "ctrl+j".into(),
        "Insert newline (where shift+enter is not reported)".into(),
    ));
    rows.push(("ctrl+z".into(), "Suspend to the shell".into()));
    rows.push(("ctrl+c".into(), "Quit".into()));
    let h = b.height.saturating_sub(4) as usize;
    let top = top.min(rows.len().saturating_sub(h));
    for (i, (k, t)) in rows.iter().skip(top).take(h).enumerate() {
        let y = b.y + 2 + i as u16;
        put(buf, b.x + 2, y, k, cx.st(ACCENT, RAISED), 16);
        put(
            buf,
            b.x + 19,
            y,
            &truncate(t, b.width.saturating_sub(21) as usize, cx.ell()),
            cx.st(PRIMARY, RAISED),
            b.width - 21,
        );
    }
    put(
        buf,
        b.x + 2,
        b.bottom() - 2,
        &format!("up/down scroll{}esc close", cx.sep()),
        cx.st(MUTED, RAISED),
        b.width - 4,
    );
}

type Buttons<'a> = Option<(&'a str, &'a str, bool, usize)>;

fn draw_dialog(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    title: &str,
    body: &[String],
    buttons: Buttons,
    hits: &mut HitMap,
) {
    // modal: swallow everything underneath
    hits.add(area, Target::Swallow);
    let w = area.width.saturating_sub(4).clamp(20, 64).min(72);
    let inner = w.saturating_sub(4) as usize;
    let rows: Vec<String> = body.iter().flat_map(|l| text::wrap(l, inner)).collect();
    let h = (rows.len() as u16 + 6).min(area.height);
    let b = Rect::new(
        area.x + (area.width - w) / 2,
        area.y + area.height.saturating_sub(h) / 2,
        w,
        h,
    );
    boxed(cx, buf, b, title, RAISED);
    hits.add(b, Target::Swallow);
    for (i, l) in rows.iter().enumerate() {
        if (i as u16) + 2 >= b.height.saturating_sub(3) {
            break;
        }
        put(
            buf,
            b.x + 2,
            b.y + 1 + i as u16,
            l,
            cx.st(PRIMARY, RAISED),
            b.width - 4,
        );
    }
    let by = b.bottom() - 3;
    match buttons {
        Some((cancel, confirm, danger, focus)) => {
            let cw_ = width(cancel) as u16 + 4;
            let fw = width(confirm) as u16 + 4;
            let total = cw_ + 2 + fw;
            let mut x = b.x + (b.width.saturating_sub(total)) / 2;
            for (i, (label, is_danger)) in
                [(cancel, false), (confirm, danger)].into_iter().enumerate()
            {
                let mut style = cx.st(if is_danger { "status.error" } else { PRIMARY }, RAISED);
                if focus == i {
                    style = style.add_modifier(Modifier::REVERSED | Modifier::BOLD);
                }
                let s = format!("[ {label} ]");
                let x1 = put(buf, x, by, &s, style, 40);
                hits.add(Rect::new(x, by, x1 - x, 1), Target::DialogButton(i));
                x = x1 + 2;
            }
            put(
                buf,
                b.x + 2,
                b.bottom() - 2,
                &format!("enter confirm{}esc cancel", cx.sep()),
                cx.st(MUTED, RAISED),
                b.width - 4,
            );
        }
        None => {
            put(
                buf,
                b.x + 2,
                b.bottom() - 2,
                "ctrl+q quit",
                cx.st(MUTED, RAISED),
                b.width - 4,
            );
        }
    }
}

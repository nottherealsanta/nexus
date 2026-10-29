//! Ratatui drawing of the shell. Everything clickable is registered in the
//! `HitMap` at the moment it is drawn, from the same rect (see `layout`).
//! Colours and glyphs come from `theme`; labels, keys and reasons from the
//! manifest. The look follows INTERFACE_PLAN.md "Design language"; the old
//! Nexus TUI (`rust/tui/src/render/`) is the visual reference.

use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Line;
use ratatui::Frame;
use serde_json::Value;

use crate::app::{now_secs, App, GitGroup, WaitKind};
use crate::layout::{contains, Flags, Heights, HitMap, Regions};
use crate::paint;
use crate::state::ConnectionState;
use crate::text::{self, truncate, truncate_middle, width};
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
    let mut metrics = (0usize, 0usize, None::<usize>);
    let timeline = {
        let a: &App = app;
        Some(crate::timeline::build(
            a,
            &a.theme,
            a.frame,
            regions.timeline.width.saturating_sub(4) as usize,
        ))
    };
    if let Some(t) = &timeline {
        app.note_live_groups(t);
    }
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
            let ex = l.right().saturating_sub(1);
            draw_sidebar_edge(&cx, buf, ex, l);
            // Dragging the edge resizes the sidebar; hovering it shows the handle.
            let edge = Rect::new(ex, l.y, 1, l.height);
            hits.add(edge, Target::SidebarEdge);
            if cx.hovered(edge) || a.ui.left_drag {
                for y in edge.y..edge.bottom() {
                    put(
                        buf,
                        ex,
                        y,
                        cx.t.structure("rail_heavy"),
                        cx.st(ACCENT, CANVAS),
                        1,
                    );
                }
            }
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
        if a.ui.diff_open.is_some() {
            draw_diff_view(&cx, buf, regions.timeline);
        } else if a.ui.file_open {
            draw_file_view(&cx, buf, regions.timeline);
        } else if let Some(t) = &timeline {
            metrics = draw_timeline(&cx, buf, regions.timeline, &mut hits, t);
        }
        draw_waiting(&cx, buf, regions.waiting, &mut hits);
        draw_composer(&cx, buf, regions.composer, &mut hits);
        if let Some(behind) = metrics.2 {
            draw_jump(&cx, buf, regions.composer, behind, &mut hits);
        }
        draw_completion(&cx, buf, &regions, &mut hits);
        if let Some(l) = regions
            .left
            .filter(|l| l.width <= crate::layout::RAIL_WIDTH)
        {
            draw_rail_popup(&cx, buf, l, regions.centre);
        }
        if let Some(l) = regions.left {
            draw_tab_tooltip(&cx, buf, l, area);
        }
        if a.ui.view == View::Settings {
            draw_settings_modal(&cx, buf, area, &mut hits);
        }
        draw_toasts(&cx, buf, &regions, &mut hits);
        if let Some((side, r)) = regions.drawer {
            // The scrim dims the page under the drawer (the header keeps its
            // toggles); clicks outside the drawer close it.
            paint::scrim(cx.t, buf, regions.centre);
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
        if let Some(e) = &a.ui.edit {
            let title = match &e.target {
                crate::ui::EditTarget::RenameSession { .. } => "Rename session",
                _ => &e.label,
            };
            let m = modal(&cx, buf, area, modal_box(area, 60, 8), title, &mut hits);
            let (rows, _) = e.buf.layout(m.w.max(1) as usize);
            let masked = "•".repeat(e.buf.text().chars().count());
            put(
                buf,
                m.x,
                m.body.y,
                if matches!(e.target, crate::ui::EditTarget::ProviderKey) {
                    &masked
                } else {
                    rows.first().map(String::as_str).unwrap_or("")
                },
                cx.st(PRIMARY, PANEL).add_modifier(Modifier::UNDERLINED),
                m.w,
            );
            put(
                buf,
                m.x,
                m.hint_y,
                "enter save · esc cancel",
                cx.st(MUTED, RAISED),
                m.w,
            );
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
    buffer_rows(&render_buffer(app, w, h))
}

/// Render to a cell buffer (styles included), for frame dumps and SVG.
pub fn render_buffer(app: &mut App, w: u16, h: u16) -> Buffer {
    use ratatui::backend::TestBackend;
    let mut term = ratatui::Terminal::new(TestBackend::new(w, h)).expect("test terminal");
    term.draw(|f| render(f, app)).expect("draw");
    term.backend().buffer().clone()
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
        let m = Modal::of(settings_box(g.area));
        let col = text::width(&app.ui.settings_search.text()) as u16;
        return Some(Position::new(
            m.x + 2 + col.min(m.w.saturating_sub(3)),
            m.body.y,
        ));
    }
    if let Some(Overlay {
        kind: OverlayKind::Palette(p),
        ..
    }) = &app.ui.overlay
    {
        let m = Modal::of(palette_box(g.area));
        let (_, (_, c)) = p.query.layout(m.w.saturating_sub(2) as usize);
        return Some(Position::new(m.x + 2 + c as u16, m.body.y));
    }
    if app.ui.overlay.is_some() {
        return None;
    }
    if let Some(e) = &app.ui.edit {
        let m = Modal::of(modal_box(g.area, 60, 8));
        let (_, (_, col)) = e.buf.layout(m.w.max(1) as usize);
        return Some(Position::new(
            m.x + (col as u16).min(m.w.saturating_sub(1)),
            m.body.y,
        ));
    }
    if app.ui.focus != Focus::Composer || !app.writable() {
        return None;
    }
    let inner = composer_inner(g.composer, !app.composer_ref().images.is_empty());
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

/// The text area: inside the card (inset two cells from the column), three
/// cells in and two rows down, below the top padding row.
pub(crate) fn composer_inner(r: Rect, images: bool) -> Rect {
    let r = composer_card(composer_content(r), images);
    Rect::new(
        r.x + 5,
        r.y + 2,
        r.width.saturating_sub(10),
        r.height.saturating_sub(6),
    )
}

fn composer_content(r: Rect) -> Rect {
    Rect::new(r.x, r.y, r.width, r.height.saturating_sub(1))
}

fn composer_card(r: Rect, images: bool) -> Rect {
    let offset = if images {
        2.min(r.height.saturating_sub(7))
    } else {
        0
    };
    Rect::new(r.x, r.y + offset, r.width, r.height - offset)
}

fn heights(app: &App, w: u16) -> Heights {
    let banner = u16::from(!app.banners().is_empty());
    let inner_w = w.saturating_sub(10) as usize;
    let rows = app.composer_ref().layout(inner_w).0.len().clamp(1, 8) as u16;
    let (waiting, composer) = (
        waiting_height(app, w),
        rows + 7
            + if app.pending_inputs().is_empty() {
                0
            } else {
                app.pending_inputs().len().min(4) as u16 + 1
            }
            + if app.composer_ref().images.is_empty() {
                0
            } else {
                2
            },
    );
    Heights {
        banner,
        waiting,
        composer,
    }
}

/// Option rows the question card shows at most; the cursor scrolls the rest.
const ASK_ROWS: usize = 5;

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
    let base = cx.st(SECONDARY, SIDEBAR);
    clear(buf, g.header, base);
    let focus_bar = cx.t.structure("focus_bar");
    for (side, rect, focus) in [
        (Side::Left, g.toggle_left, Focus::ToggleLeft),
        (Side::Right, g.toggle_right, Focus::ToggleRight),
    ] {
        let focused = cx.app.ui.focus == focus;
        let style = cx.st(SECONDARY, SIDEBAR);
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
                        cx.st("interactive.focus", SIDEBAR)
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
                        cx.st("interactive.focus", SIDEBAR)
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

    // The sidebar icons live in the band over the docked left sidebar.
    if let Some(l) = g
        .left
        .filter(|l| l.width.saturating_sub(1) > crate::layout::RAIL_WIDTH)
    {
        draw_tab_icons(cx, buf, l, hits);
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
            cx.st("status.info", SIDEBAR),
            g.bell.width,
        );
        hits.add(g.bell, Target::Notifications);
    }

    // title, breadcrumbs and status, centred over the centre column
    let tx0 = g.title.x.max(g.centre.x);
    let tr = Rect::new(
        tx0,
        g.title.y,
        g.title.right().min(g.centre.right()).saturating_sub(tx0),
        g.title.height,
    );
    if tr.width < 3 {
        return;
    }
    if cx.app.narrow() {
        draw_crumb(cx, buf, tr);
    } else {
        draw_tabs(cx, buf, tr, hits);
    }
}

/// The narrow header (DESIGN_PLAN.md D17): the toggles stay at the corners and
/// the tab strip gives way to the breadcrumb of the viewed session, cut from
/// the left so the current session stays readable. The run status keeps the
/// right edge.
fn draw_crumb(cx: &Cx, buf: &mut Buffer, tr: Rect) {
    let a = cx.app;
    let status = header_status(cx);
    let status_w = status.as_ref().map_or(0, |(s, _)| width(s) + 2);
    if let Some((s, style)) = &status {
        let sx = tr.right().saturating_sub(width(s) as u16 + 1);
        put(buf, sx, tr.y, s, *style, width(s) as u16);
    }
    let parts: Vec<String> = if a.ui.view == View::Settings {
        vec!["Settings".to_owned()]
    } else if let Some(id) = a.viewed_id() {
        let chain = a.ancestors_of(id);
        if chain.len() > 1 {
            chain.iter().map(|s| s.title.clone()).collect()
        } else {
            vec![a
                .state
                .model
                .sessions
                .get(id)
                .map_or_else(|| id.to_owned(), |s| s.title.clone())]
        }
    } else {
        vec!["New session".to_owned()]
    };
    let sep = format!(" {} ", cx.t.structure("breadcrumb"));
    let room = (tr.width as usize).saturating_sub(status_w + 2);
    let shown = paint::crumb_left(&parts, &sep, cx.ell(), room);
    let (head, last) = match shown.rfind(&sep) {
        Some(i) => shown.split_at(i + sep.len()),
        None => ("", shown.as_str()),
    };
    let x = put(buf, tr.x + 1, tr.y, head, cx.st(MUTED, SIDEBAR), tr.width);
    put(
        buf,
        x,
        tr.y,
        last,
        cx.st(PRIMARY, SIDEBAR).add_modifier(Modifier::BOLD),
        tr.right().saturating_sub(x),
    );
}

/// The session tabs (DESIGN_PLAN.md D02): `{glyph} {title} ×` per open
/// session. The viewed tab is bold primary on the page surface so it reads as
/// joined to the page below; the others are muted on the chrome band. `+` ends
/// the strip. A viewed child session shows `root › child`. The run status sits
/// at the centre column's right edge.
fn draw_tabs(cx: &Cx, buf: &mut Buffer, tr: Rect, hits: &mut HitMap) {
    let a = cx.app;
    let status = header_status(cx);
    let status_w = status.as_ref().map_or(0, |(s, _)| width(s) + 2);
    if let Some((s, style)) = &status {
        let sx = tr.right().saturating_sub(width(s) as u16 + 1);
        put(buf, sx, tr.y, s, *style, width(s) as u16);
    }
    let ids = a.tab_ids();
    let viewed = a.viewed_id().map(str::to_owned);
    let settings = a.ui.view == View::Settings;
    // A tab for what is on screen without a session: the new-session screen or Settings.
    let extra = if settings {
        Some("Settings")
    } else if viewed.is_none() {
        Some("New session")
    } else {
        None
    };
    let ascii = cx.t.opts.ascii;
    let glyph_of = |id: &str| -> (String, Style) {
        if a.is_busy(id) {
            (
                cx.t.spinner(a.frame / 2).to_owned(),
                cx.st(PRIMARY, SIDEBAR),
            )
        } else if a.ui.failed_sessions.contains(id) {
            (
                if ascii { "o" } else { "\u{25cf}" }.to_owned(),
                cx.st("status.error", SIDEBAR),
            )
        } else if a.session_waiting(id) {
            let (g, tok) = cx.t.attention("waiting");
            (g.to_owned(), cx.st(tok, SIDEBAR))
        } else if a.ui.unread_sessions.contains(id) {
            (
                if ascii { "*" } else { "\u{25cf}" }.to_owned(),
                cx.st("status.info", SIDEBAR),
            )
        } else {
            (
                if ascii { "." } else { "\u{b7}" }.to_owned(),
                cx.st(MUTED, SIDEBAR),
            )
        }
    };
    struct Tab {
        index: Option<usize>,
        /// Breadcrumb parts (root first); one part for a plain title.
        parts: Vec<String>,
        active: bool,
        id: Option<String>,
    }
    let title_of = |id: &str| {
        a.state
            .model
            .sessions
            .get(id)
            .map_or_else(|| id.to_owned(), |s| s.title.clone())
    };
    let mut tabs: Vec<Tab> = ids
        .iter()
        .enumerate()
        .map(|(i, id)| {
            let active = !settings && viewed.as_deref() == Some(id.as_str());
            let parts = if active {
                let chain = a.ancestors_of(id);
                if chain.len() > 1 {
                    chain.iter().map(|s| s.title.clone()).collect()
                } else {
                    vec![title_of(id)]
                }
            } else {
                vec![title_of(id)]
            };
            Tab {
                index: Some(i),
                parts,
                active,
                id: Some(id.clone()),
            }
        })
        .collect();
    if let Some(label) = extra {
        tabs.push(Tab {
            index: None,
            parts: vec![label.to_owned()],
            active: true,
            id: None,
        });
    }
    let sep = format!(" {} ", cx.t.structure("breadcrumb"));
    // The shown text of each part within `max` cells: earlier crumbs shrink
    // first, the last part keeps at least six.
    let shown = |t: &Tab, max: usize| -> Vec<String> {
        let n = t.parts.len();
        if n == 1 {
            return vec![truncate(&t.parts[0], max, cx.ell())];
        }
        let seps = width(&sep) * (n - 1);
        let early = (max.saturating_sub(seps + 6) / (n - 1)).clamp(3, 10);
        let mut out: Vec<String> = t.parts[..n - 1]
            .iter()
            .map(|p| truncate(p, early, cx.ell()))
            .collect();
        let used: usize = out.iter().map(|p| width(p)).sum::<usize>() + seps;
        out.push(truncate(
            &t.parts[n - 1],
            max.saturating_sub(used).max(6),
            cx.ell(),
        ));
        out
    };
    let title_w = |t: &Tab, max: usize| {
        let p = shown(t, max);
        p.iter().map(|s| width(s)).sum::<usize>() + width(&sep) * (p.len() - 1)
    };
    // The room: leave the `+` and the status.
    let avail = (tr.width as usize).saturating_sub(status_w + 4);
    let cell = |t: &Tab, max: usize| 5 + title_w(t, max) + usize::from(t.id.is_some()) * 2;
    let mut max_title = 28usize;
    let total = |tabs: &[Tab], max: usize| tabs.iter().map(|t| cell(t, max) + 1).sum::<usize>();
    while max_title > 8 && total(&tabs, max_title) > avail {
        max_title -= 2;
    }
    // Still too many: drop the tabs farthest from the active one.
    let mut dropped_left = false;
    while tabs.len() > 1 && total(&tabs, max_title) > avail {
        let active = tabs.iter().position(|t| t.active).unwrap_or(0);
        if active > 0 {
            tabs.remove(0);
            dropped_left = true;
        } else {
            tabs.pop();
        }
    }
    let mut x = tr.x + 1;
    if dropped_left {
        x = put(buf, x, tr.y, cx.ell(), cx.st(MUTED, SIDEBAR), 1) + 1;
    }
    for t in &tabs {
        let w = cell(t, max_title) as u16;
        if x + w > tr.right() {
            break;
        }
        let rect = Rect::new(x, tr.y, w, 1);
        let bg = if t.active { CANVAS } else { SIDEBAR };
        let base = cx.st(if t.active { PRIMARY } else { MUTED }, bg);
        clear(buf, rect, base);
        let mut cx_x = x + 1;
        if let Some(id) = &t.id {
            let (g, gs) = glyph_of(id);
            let gs = gs.patch(cx.t.bg(bg));
            cx_x = put(buf, cx_x, tr.y, &g, gs, 1) + 1;
        }
        let parts = shown(t, max_title);
        let last = parts.len() - 1;
        if let (Some(i), Some(_)) = (t.index, &t.id) {
            hits.add(rect, Target::SessionTab(i));
        }
        for (pi, part) in parts.iter().enumerate() {
            let style = if pi == last && t.active {
                base.add_modifier(Modifier::BOLD)
            } else {
                base
            };
            let start = cx_x;
            cx_x = put(
                buf,
                cx_x,
                tr.y,
                part,
                style,
                rect.right().saturating_sub(cx_x),
            );
            if pi < last {
                hits.add(rect, Target::SessionTab(t.index.unwrap_or(0)));
                hits.add(
                    Rect::new(start, tr.y, cx_x - start, 1),
                    Target::SessionCrumb(pi),
                );
                cx_x = put(
                    buf,
                    cx_x,
                    tr.y,
                    &sep,
                    base.patch(cx.t.fg(MUTED)),
                    rect.right().saturating_sub(cx_x),
                );
            }
        }
        if let (Some(i), Some(_)) = (t.index, &t.id) {
            let close = Rect::new(rect.right().saturating_sub(3), tr.y, 2, 1);
            let hot = cx.hovered(close);
            put(
                buf,
                close.x,
                tr.y,
                if ascii { "x" } else { "\u{00d7}" },
                if hot {
                    base.patch(cx.t.fg(PRIMARY)).add_modifier(Modifier::BOLD)
                } else {
                    base.patch(cx.t.fg(MUTED))
                },
                1,
            );
            hits.add(close, Target::SessionTabClose(i));
        }
        x += w + 1;
    }
    // new session
    if x + 2 <= tr.right() {
        let rect = Rect::new(x, tr.y, 3, 1);
        let style = if cx.hovered(rect) {
            cx.st(PRIMARY, SIDEBAR)
        } else {
            cx.st(MUTED, SIDEBAR)
        };
        clear(buf, rect, style);
        put(buf, x + 1, tr.y, "+", style, 1);
        hits.add(rect, Target::NewSession);
    }
}

fn header_status(cx: &Cx) -> Option<(String, Style)> {
    let a = cx.app;
    match &a.state.connection {
        ConnectionState::Live => {
            a.viewed_id()?;
            if !a.wait_items().is_empty() {
                // `◆ waiting for you`: a wait is pending in this session or a child.
                let (g, _, tok) = cx.t.status("waiting");
                Some((format!("{g} waiting for you"), cx.st(tok, SIDEBAR)))
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
            cx.st(SECONDARY, SIDEBAR),
        )),
        c => {
            let (g, _, tok) = cx.t.status("error");
            Some((
                format!(
                    "{g} {}",
                    // A dropped stream reads as offline too (DESIGN_PLAN D18):
                    // the banner says it is retrying.
                    if matches!(c, ConnectionState::Reconnecting { .. }) {
                        "offline"
                    } else {
                        c.label()
                    }
                ),
                cx.st(tok, SIDEBAR),
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
        right.push((t, cx.st(SECONDARY, PANEL), Some(Target::BannerAction(0))));
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
    // The first words in the level colour, the rest secondary.
    let room = (edge.saturating_sub(r.x + 4)) as usize;
    let text = truncate(&b.text, room.saturating_sub(2), cx.ell());
    let cut = banner_head(&text);
    let (head, rest) = text.split_at(cut);
    let hx = put(
        buf,
        r.x + 3,
        r.y,
        head,
        cx.st(tok, PANEL).add_modifier(Modifier::BOLD),
        room as u16,
    );
    put(
        buf,
        hx,
        r.y,
        rest,
        cx.st(SECONDARY, PANEL),
        (r.x + 3 + room as u16).saturating_sub(hx),
    );
}

/// Where a banner's lead phrase ends: its first ` · ` or `. `, else its first
/// four words.
fn banner_head(text: &str) -> usize {
    for sep in [" \u{b7} ", ". "] {
        if let Some(i) = text.find(sep) {
            return i;
        }
    }
    let mut words = 0;
    for (i, c) in text.char_indices() {
        if c == ' ' {
            words += 1;
            if words == 4 {
                return i;
            }
        }
    }
    text.len()
}

/// Toasts (D15): top-right under the header (and the banner), the oldest on
/// top, three drawn and `+N more` for the queue. Each is a raised block with
/// the level `▌`, the level glyph, a bold title and `×`; a body line when the
/// text has one. No lifetime hairline. A click opens the full text.
fn draw_toasts(cx: &Cx, buf: &mut Buffer, g: &Regions, hits: &mut HitMap) {
    let toasts = &cx.app.ui.alerts.toasts;
    if toasts.is_empty() {
        return;
    }
    let parts: Vec<(String, String)> = toasts
        .iter()
        .take(crate::ui::SHOWN)
        .map(|t| crate::ui::split_title(&t.text))
        .collect();
    let longest = parts
        .iter()
        .map(|(t, b)| width(t).max(width(b)) + 8)
        .max()
        .unwrap_or(0);
    let w = paint::toast_width(longest, g.centre.width);
    let x = g.centre.right().saturating_sub(w + 1);
    let bottom = g.timeline.bottom();
    let mut y = g.timeline.y;
    let mut shown = 0;
    for (t, (title, body)) in toasts.iter().zip(&parts) {
        let h = if body.is_empty() { 1 } else { 2 };
        if y + h > bottom {
            break;
        }
        let r = Rect::new(x, y, w, h);
        let bg = RAISED;
        let (glyph, tok) = cx.level(t.level);
        clear(buf, r, cx.st(PRIMARY, bg));
        for row in r.y..r.bottom() {
            put(
                buf,
                r.x,
                row,
                cx.t.structure("focus_bar"),
                cx.st(tok, bg),
                1,
            );
        }
        hits.add(r, Target::ToastOpen(t.id));
        put(buf, r.x + 2, r.y, glyph, cx.st(tok, bg), 2);
        let room = w.saturating_sub(8) as usize;
        put(
            buf,
            r.x + 4,
            r.y,
            &truncate(title, room, cx.ell()),
            cx.st(PRIMARY, bg).add_modifier(Modifier::BOLD),
            room as u16,
        );
        if !body.is_empty() {
            put(
                buf,
                r.x + 4,
                r.y + 1,
                &truncate(body, room, cx.ell()),
                cx.st(SECONDARY, bg),
                room as u16,
            );
        }
        let close = Rect::new(r.right() - 3, r.y, 3, 1);
        hits.add(close, Target::ToastDismiss(t.id));
        put(
            buf,
            r.right() - 2,
            r.y,
            cx.t.structure("close"),
            cx.st(if cx.hovered(close) { PRIMARY } else { MUTED }, bg),
            1,
        );
        y += h + 1;
        shown += 1;
    }
    let hidden = toasts.len() - shown;
    // `y` is one row past the last toast's gap; the tag sits on that gap row.
    if hidden > 0 && shown > 0 && y > g.timeline.y && y - 1 < bottom {
        let tag = format!(" +{hidden} more ");
        let tw = width(&tag) as u16;
        let r = Rect::new((x + w).saturating_sub(tw), y - 1, tw, 1);
        clear(buf, r, cx.st(MUTED, RAISED));
        put(buf, r.x, r.y, &tag, cx.st(MUTED, RAISED), tw);
    }
}

// ============================================================================
// left sidebar
// ============================================================================

/// The sidebar's edge column. The surface step is the edge; only where the
/// surfaces collapse (ANSI-16, monochrome) is a `│` drawn.
fn draw_sidebar_edge(cx: &Cx, buf: &mut Buffer, x: u16, r: Rect) {
    clear(
        buf,
        Rect::new(x, r.y, 1, r.height),
        cx.st(SECONDARY, SIDEBAR),
    );
    if !matches!(
        cx.t.opts.tier,
        crate::theme::Tier::Ansi16 | crate::theme::Tier::Mono
    ) {
        return;
    }
    for y in r.y..r.bottom() {
        put(
            buf,
            x,
            y,
            cx.t.structure("rail"),
            cx.st("border.default", SIDEBAR),
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
        s_style = cx.st(PRIMARY, SIDEBAR);
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
            s_style.patch(cx.st(MUTED, SIDEBAR)),
            r.right() - kx,
        );
    }
    hits.add(sr, Target::SettingsEntry);

    // Docked: the icons are in the header band and the body starts on the
    // first row. A drawer keeps its own icon row.
    let top = if cx.app.narrow() {
        draw_tab_icons(cx, buf, r, hits);
        2
    } else {
        1
    };
    let body = Rect::new(r.x, r.y + top, r.width, r.height.saturating_sub(top + 2));
    match a.ui.tab {
        Tab::Sessions => draw_sessions(cx, buf, body, focused, hits),
        Tab::Files => draw_files(cx, buf, body, focused, hits),
        Tab::Projects => draw_projects(cx, buf, body, focused, hits),
    }
}

/// The sidebar tab icons, in manifest order: a row at the top of the full
/// sidebar, or a column at the top of the rail. Labels show on hover.
fn tab_icons(cx: &Cx, r: Rect) -> Vec<(Tab, Rect, String, &'static str)> {
    let rail = r.width <= crate::layout::RAIL_WIDTH;
    let mut out = Vec::new();
    if rail {
        return out;
    }
    for (i, t) in cx
        .app
        .manifest
        .navigation
        .left_sidebar
        .tab
        .iter()
        .enumerate()
    {
        let Some(tab) = Tab::from_id(&t.id) else {
            continue;
        };
        let glyph = match tab {
            Tab::Sessions => "tab_sessions",
            Tab::Files => "tab_files",
            Tab::Projects => "tab_projects",
        };
        let i = i as u16;
        let rect = if rail {
            Rect::new(r.x, r.y + 1 + i, r.width, 1)
        } else {
            // in the header band, over the docked sidebar
            Rect::new(r.x + 3 + i * 3, r.y.saturating_sub(1), 3, 1)
        };
        let rect = if !rail && cx.app.narrow() {
            Rect::new(r.x + 1 + i * 4, r.y, 3, 1)
        } else {
            rect
        };
        if rect.right() <= r.right() && rect.bottom() <= r.bottom() {
            out.push((tab, rect, t.label.clone(), glyph));
        }
    }
    out
}

fn draw_tab_icons(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    let a = cx.app;
    for (tab, rect, _, glyph) in tab_icons(cx, r) {
        let active = a.ui.tab == tab;
        let mut style = if active {
            cx.st(PRIMARY, SIDEBAR).add_modifier(Modifier::BOLD)
        } else {
            cx.st(MUTED, SIDEBAR)
        };
        if cx.hovered(rect) && !active {
            style = cx.st(SECONDARY, SIDEBAR);
        }
        if active || cx.hovered(rect) {
            clear(buf, rect, style);
        }
        let g = cx.t.sidebar_glyph(glyph);
        let gx = rect.x + rect.width.saturating_sub(width(g) as u16) / 2;
        put(buf, gx, rect.y, g, style, rect.width);
        if active && cx.t.mono() {
            buf.set_style(rect, Style::default().add_modifier(Modifier::UNDERLINED));
        }
        hits.add(rect, Target::Tab(tab));
    }
}

/// The hovered tab icon's word label, drawn on top of everything.
fn draw_tab_tooltip(cx: &Cx, buf: &mut Buffer, r: Rect, limit: Rect) {
    let Some((_, rect, label, _)) = tab_icons(cx, r)
        .into_iter()
        .find(|(_, rect, _, _)| cx.hovered(*rect))
    else {
        return;
    };
    let w = width(&label) as u16 + 2;
    let (x, y) = if r.width <= crate::layout::RAIL_WIDTH {
        (rect.right(), rect.y)
    } else {
        (rect.x, rect.y + 1)
    };
    let tip = Rect::new(x, y, w, 1).intersection(limit);
    clear(buf, tip, cx.st(PRIMARY, RAISED));
    put(
        buf,
        tip.x + 1,
        tip.y,
        &label,
        cx.st(PRIMARY, RAISED),
        tip.width.saturating_sub(1),
    );
}

/// One rail entry: a session, or the search and new-session controls.
#[derive(Clone, Copy, PartialEq, Eq)]
enum RailItem {
    Search,
    Session(usize),
    New,
}

/// Rail rows: two per item, with one row between initials.
const RAIL_ITEM: u16 = 2;

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
                Rect::new(
                    r.x,
                    r.y + i as u16 * RAIL_ITEM + u16::from(i > 0),
                    r.width,
                    RAIL_ITEM,
                ),
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
                    style = style.add_modifier(Modifier::BOLD);
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
            style = style.patch(cx.st(PRIMARY, SIDEBAR));
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
    let bg = RAISED;
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
    let mut top = r.y;
    // D16: the root header, `SIDE-PROJECT` bold and `⌥ main` muted at the
    // right. With several checkouts it switches between them.
    if let Some(h) = a.files_header() {
        let row = Rect::new(r.x, r.y, r.width, 1);
        clear(buf, row, cx.st(MUTED, SIDEBAR));
        let lead = if h.switchable {
            format!("{} ", cx.t.fold(true))
        } else {
            String::new()
        };
        let branch = h
            .branch
            .as_ref()
            .map(|b| format!("{} {b}", if cx.t.opts.ascii { "~" } else { "\u{2325}" }));
        let branch_w = branch.as_ref().map_or(0, |b| width(b) + 1);
        let room = (r.width as usize).saturating_sub(2 + width(&lead) + branch_w);
        let x = put(
            buf,
            r.x + 1,
            r.y,
            &lead,
            cx.st(MUTED, SIDEBAR),
            r.width.saturating_sub(1),
        );
        put(
            buf,
            x,
            r.y,
            &truncate(&h.name.to_uppercase(), room, cx.ell()),
            cx.st(PRIMARY, SIDEBAR).add_modifier(Modifier::BOLD),
            room as u16,
        );
        if let Some(b) = branch.filter(|_| room > 6) {
            let bx = r.right().saturating_sub(width(&b) as u16 + 1);
            put(
                buf,
                bx,
                r.y,
                &truncate(&b, branch_w, cx.ell()),
                cx.st(MUTED, SIDEBAR),
                width(&b) as u16,
            );
        }
        if h.switchable {
            hits.add(row, Target::FilesRoot);
        }
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
        // One `│` guide per ancestor level, under that folder's marker.
        for level in 0..entry.depth {
            put(
                buf,
                r.x + 1 + level as u16 * 2,
                y,
                cx.t.structure("rail"),
                style.patch(cx.t.fg("border.default")),
                1,
            );
        }
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
                style.patch(cx.t.fg(token)),
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

/// The read-only file view (DESIGN_PLAN.md D16), on the page surface: a header
/// row with the `~` path (the basename bold) and the shown line range at the
/// right, a blank row, the numbered text behind a `│` gutter, and a bottom
/// row that holds the search. The text is drawn up to the row above it.
fn draw_file_view(cx: &Cx, buf: &mut Buffer, r: Rect) {
    clear(buf, r, cx.st(PRIMARY, CANVAS));
    let Some((project, session, path, result)) = &cx.app.state.file else {
        put(
            buf,
            r.x + 1,
            r.y,
            if cx.t.opts.ascii {
                "Loading file..."
            } else {
                "Loading file\u{2026}"
            },
            cx.st(MUTED, CANVAS),
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
    let content = result
        .as_ref()
        .ok()
        .and_then(|data| data["content"].as_str())
        .unwrap_or("");
    let total = content.lines().count();
    let page = paint::file_page(r.height);
    let scroll = cx.app.ui.file_scroll;
    // `lines 1-38 of 38`, right-aligned and muted.
    let range = match result {
        Ok(_) if total == 0 => "empty file".to_owned(),
        Ok(_) => format!(
            "lines {}-{} of {total}",
            scroll + 1,
            (scroll + page).min(total)
        ),
        Err(_) => String::new(),
    };
    let range_w = width(&range) as u16;
    if range_w > 0 && r.width > range_w + 12 {
        put(
            buf,
            r.right().saturating_sub(range_w + 1),
            r.y,
            &range,
            cx.st(MUTED, CANVAS),
            range_w,
        );
    }
    // The path in secondary, the basename bold; a long directory is cut in the
    // middle and the basename is kept.
    let room = (r.width as usize)
        .saturating_sub(range_w as usize + 4)
        .max(8);
    let (dir, base) = match display.rfind('/') {
        Some(i) => display.split_at(i + 1),
        None => ("", display.as_str()),
    };
    let base = truncate(base, room, cx.ell());
    let dir = truncate_middle(dir, room.saturating_sub(width(&base)), cx.ell());
    let x = put(
        buf,
        r.x + 1,
        r.y,
        &dir,
        cx.st(SECONDARY, CANVAS),
        r.width.saturating_sub(1),
    );
    put(
        buf,
        x,
        r.y,
        &base,
        cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
        r.right().saturating_sub(x),
    );
    match result {
        Ok(_) => {
            let query = cx
                .app
                .ui
                .file_search
                .as_ref()
                .map(|search| search.query.to_ascii_lowercase())
                .filter(|query| !query.is_empty());
            let matches = cx.app.file_matches();
            let current = cx
                .app
                .ui
                .file_search
                .as_ref()
                .and_then(|search| search.hit)
                .and_then(|hit| matches.get(hit).copied());
            // line numbers right-aligned in a gutter of at least 3 digits
            let digits = total.to_string().len().max(3);
            let top = r.y + if r.height >= 8 { 2 } else { 1 };
            let gutter = format!("{} ", cx.t.structure("rail"));
            let base_x = r.x + 1 + digits as u16 + 1 + width(&gutter) as u16;
            for (i, line) in content.lines().enumerate().skip(scroll).take(page) {
                let y = top + (i - scroll) as u16;
                let number = format!("{:>digits$} ", i + 1);
                let nx = put(
                    buf,
                    r.x + 1,
                    y,
                    &number,
                    cx.st(MUTED, CANVAS),
                    r.width.saturating_sub(1),
                );
                let tx = put(
                    buf,
                    nx,
                    y,
                    &gutter,
                    cx.st("border.default", CANVAS),
                    r.right().saturating_sub(nx),
                );
                debug_assert_eq!(tx, base_x);
                let room = r.right().saturating_sub(base_x + 1);
                put(
                    buf,
                    base_x,
                    y,
                    &truncate(line, room as usize, cx.ell()),
                    cx.st(PRIMARY, CANVAS),
                    room,
                );
                if let Some(query) = &query {
                    for (k, (at, _)) in line
                        .to_ascii_lowercase()
                        .match_indices(query.as_str())
                        .enumerate()
                    {
                        let x = base_x + width(&line[..at]) as u16;
                        if x >= r.right() {
                            break;
                        }
                        let len = (width(query) as u16).min(r.right() - x);
                        let mut style = Style::default().add_modifier(Modifier::UNDERLINED);
                        // the current match (the first on its line) is reversed
                        if current == Some(i) && k == 0 {
                            style = style.add_modifier(Modifier::REVERSED);
                        }
                        buf.set_style(Rect::new(x, y, len, 1), style);
                    }
                }
            }
            // `/ query  3 of 12   n next · N prev · esc end` on the bottom row.
            if let (Some(search), true) = (&cx.app.ui.file_search, r.height >= 3) {
                let y = r.bottom() - 1;
                let found = matches.len();
                let status = match (search.query.is_empty(), found, search.hit) {
                    (true, _, _) => String::new(),
                    (false, 0, _) => "no matches".to_owned(),
                    (false, n, Some(h)) => format!("{} of {n}", h + 1),
                    (false, n, None) => format!("{n} matches"),
                };
                let hints: Vec<(String, String)> = if search.typing {
                    vec![
                        ("enter".into(), "next".into()),
                        ("esc".into(), "end".into()),
                    ]
                } else {
                    vec![
                        ("n".into(), "next".into()),
                        ("N".into(), "prev".into()),
                        ("esc".into(), "end".into()),
                    ]
                };
                let hint_w: usize = hints
                    .iter()
                    .map(|(k, l)| width(k) + 1 + width(l))
                    .sum::<usize>()
                    + width(&cx.sep()) * (hints.len() - 1);
                let x = put(
                    buf,
                    r.x + 1,
                    y,
                    "/ ",
                    cx.st(MUTED, CANVAS),
                    r.width.saturating_sub(1),
                );
                let x = put(
                    buf,
                    x,
                    y,
                    &search.query,
                    cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
                    r.right().saturating_sub(x + 1),
                );
                let status_x = x + 2;
                if status_x + width(&status) as u16 + hint_w as u16 + 4 < r.right() {
                    put(
                        buf,
                        status_x,
                        y,
                        &status,
                        cx.st(SECONDARY, CANVAS),
                        r.right().saturating_sub(status_x),
                    );
                    let mut hx = r.right().saturating_sub(hint_w as u16 + 1);
                    for (i, (k, l)) in hints.iter().enumerate() {
                        if i > 0 {
                            hx = put(
                                buf,
                                hx,
                                y,
                                &cx.sep(),
                                cx.st(MUTED, CANVAS),
                                r.right().saturating_sub(hx),
                            );
                        }
                        hx = put(
                            buf,
                            hx,
                            y,
                            k,
                            cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
                            r.right().saturating_sub(hx),
                        );
                        hx = put(
                            buf,
                            hx,
                            y,
                            &format!(" {l}"),
                            cx.st(MUTED, CANVAS),
                            r.right().saturating_sub(hx),
                        );
                    }
                }
            }
        }
        Err(error) => {
            put(
                buf,
                r.x + 1,
                r.y + 2.min(r.height.saturating_sub(1)),
                error,
                cx.st("status.error", CANVAS),
                r.width.saturating_sub(1),
            );
        }
    }
}

/// A read-only Git diff in the centre column, like the file view, drawn by
/// `diff.rs`: split at 110 cells and wider, unified below.
fn draw_diff_view(cx: &Cx, buf: &mut Buffer, r: Rect) {
    clear(buf, r, cx.st(PRIMARY, SIDEBAR));
    let Some((path, group)) = &cx.app.ui.diff_open else {
        return;
    };
    let title = format!("{path} {} {}", cx.t.structure("separator"), group.label());
    put(
        buf,
        r.x + 1,
        r.y,
        &truncate_middle(&title, r.width.saturating_sub(2) as usize, cx.ell()),
        cx.st(PRIMARY, SIDEBAR).add_modifier(Modifier::BOLD),
        r.width.saturating_sub(1),
    );
    let note = |buf: &mut Buffer, text: &str, token: &str| {
        put(
            buf,
            r.x + 1,
            r.y + 1,
            text,
            cx.st(token, SIDEBAR),
            r.width.saturating_sub(1),
        );
    };
    if *group == GitGroup::Untracked {
        return note(buf, "Untracked file: Git has no diff for it yet.", MUTED);
    }
    let loaded = cx
        .app
        .state
        .diff
        .as_ref()
        .filter(|(session, p, _)| Some(session.as_str()) == cx.app.viewed_id() && p == path);
    match loaded {
        None => {
            let text = if cx.t.opts.ascii {
                "Loading diff..."
            } else {
                "Loading diff…"
            };
            return note(buf, text, MUTED);
        }
        Some((_, _, Err(error))) => return note(buf, error, "status.error"),
        Some(_) => {}
    }
    let lines = cx.app.diff_lines();
    if lines.is_empty() {
        return note(buf, "No changes in this part.", MUTED);
    }
    let page = r.height.saturating_sub(2) as usize;
    let width = r.width.saturating_sub(2) as usize;
    let look = crate::diff::Look::from_theme(cx.t);
    let rows = crate::diff::Diff::from_patch(&lines.join("\n")).render(width, &look);
    if rows.is_empty() {
        return note(buf, "No changes in this part.", MUTED);
    }
    let scroll = cx.app.ui.diff_scroll.min(rows.len().saturating_sub(page));
    for (i, row) in rows.iter().enumerate().skip(scroll).take(page) {
        let y = r.y + 1 + (i - scroll) as u16;
        buf.set_line(r.x + 1, y, &Line::from(row.clone()), width as u16);
    }
    let remaining = rows.len().saturating_sub(scroll + page);
    if remaining > 0 && r.height >= 2 {
        put(
            buf,
            r.x + 1,
            r.bottom() - 1,
            &format!("{remaining} more lines · scroll"),
            cx.st(MUTED, SIDEBAR),
            r.width.saturating_sub(1),
        );
    }
}

fn row_style(cx: &Cx, selected: bool, hover: bool) -> Style {
    if selected || hover {
        cx.st(PRIMARY, SIDEBAR)
    } else {
        cx.st(SECONDARY, SIDEBAR)
    }
}

/// `~/…/leaf` for a path that does not fit: the home-shortened path, else its
/// first and last parts around an ellipsis, else middle-truncated.
fn short_path(path: &str, max: usize, ell: &str) -> String {
    let home = short_home(path);
    if width(&home) <= max {
        return home;
    }
    let leaf = home.rsplit('/').next().unwrap_or("");
    let first = if home.starts_with("~/") { "~" } else { "" };
    let compact = format!("{first}/{ell}/{leaf}");
    if width(&compact) <= max {
        compact
    } else {
        truncate_middle(&compact, max, ell)
    }
}

/// One line of the sessions list.
#[derive(Clone, Copy, PartialEq, Debug)]
enum SRow {
    Project(usize),
    Tree(usize),
    Title(usize),
    Detail(usize),
    Spacer,
    New,
}

/// The title of a session row: the shimmer while it runs, an ellipsis when it
/// is clipped.
fn draw_session_title(
    cx: &Cx,
    buf: &mut Buffer,
    pos: (u16, u16),
    room: u16,
    s: &crate::api::Session,
    style: Style,
    bg: Style,
) {
    let a = cx.app;
    let title = if s.title.is_empty() {
        "Untitled"
    } else {
        &s.title
    };
    let shown = truncate(title, room as usize, cx.ell());
    let animate = a.is_busy(&s.id) && !cx.t.opts.reduced_motion && !cx.t.mono();
    let spans = crate::theme::shimmer(
        &shown,
        a.frame,
        animate,
        style,
        cx.st(PRIMARY, SIDEBAR).patch(bg),
    );
    let mut x = pos.0;
    let end = pos.0 + room;
    for span in spans {
        x = put(
            buf,
            x,
            pos.1,
            &span.content,
            span.style,
            end.saturating_sub(x),
        );
    }
}

fn draw_sessions(cx: &Cx, buf: &mut Buffer, r: Rect, focused: bool, hits: &mut HitMap) {
    let a = cx.app;
    if r.height < 3 {
        return;
    }
    // The search row opens the switcher; a blank row follows it.
    let search = Rect::new(r.x, r.y, r.width, 1);
    let mut style = cx.st(MUTED, SIDEBAR);
    if cx.hovered(search) {
        style = style.patch(cx.st(PRIMARY, SIDEBAR));
        clear(buf, search, style);
    }
    let glyph = if cx.t.opts.ascii { "/" } else { "⌕" };
    put(buf, r.x + 2, r.y, glyph, style, 1);
    let key = a
        .manifest
        .command("session.switch")
        .and_then(|c| c.tui_key())
        .unwrap_or("");
    let kw = width(key) as u16;
    let kx = r.right().saturating_sub(kw + 1);
    // `Search sessions`, or `Search` when the key hint leaves no room for it.
    let room = kx.saturating_sub(r.x + 6) as usize;
    let label = if room >= 15 {
        "Search sessions"
    } else {
        "Search"
    };
    put(
        buf,
        r.x + 5,
        r.y,
        &truncate(label, room, cx.ell()),
        style,
        room as u16,
    );
    put(buf, kx, r.y, key, style, kw);
    hits.add(search, Target::RailSearch);
    let list = Rect::new(r.x, r.y + 2, r.width, r.height.saturating_sub(2));
    if list.height == 0 {
        return;
    }
    let entries = a.side_entries();
    if entries.is_empty() {
        let msg = if a.state.model.projects.is_empty() {
            "No project yet. Open the Projects tab to add one."
        } else if a.ui.show_archived || a.ui.scope_all {
            "No sessions here. Type in the composer to start one."
        } else {
            "No sessions yet. Type in the composer to start one."
        };
        for (i, l) in text::wrap(msg, list.width.saturating_sub(5) as usize)
            .iter()
            .enumerate()
            .take(list.height as usize)
        {
            put(
                buf,
                list.x + 2,
                list.y + i as u16,
                l,
                cx.st(MUTED, SIDEBAR),
                list.width - 3,
            );
        }
        return;
    }
    // Headers scroll with their rows; each session is two lines and a spacer.
    let mut rows = Vec::new();
    for (i, e) in entries.iter().enumerate() {
        if e.project.is_some() {
            rows.push(SRow::Project(i));
        }
        if e.tree.is_some() {
            rows.push(SRow::Tree(i));
        }
        rows.extend([SRow::Title(i), SRow::Detail(i), SRow::Spacer]);
    }
    rows.push(SRow::New);
    let cursor = a.cursor(Tab::Sessions);
    let cur_row = rows
        .iter()
        .position(|v| *v == SRow::Title(cursor))
        .unwrap_or(0);
    let h = list.height as usize;
    let top = (cur_row + 3).saturating_sub(h);
    let viewed = a.viewed_id();
    let muted = cx.st(MUTED, SIDEBAR);
    let inner_right = list.right().saturating_sub(1);
    for (vi, v) in rows.iter().enumerate().skip(top).take(h) {
        let y = list.y + (vi - top) as u16;
        let rr = Rect::new(list.x, y, list.width, 1);
        match *v {
            SRow::Spacer => {}
            SRow::New => {
                let style = if cx.hovered(rr) {
                    clear(buf, rr, cx.st(PRIMARY, SIDEBAR));
                    cx.st(PRIMARY, SIDEBAR)
                } else {
                    muted
                };
                put(
                    buf,
                    rr.x + 4,
                    y,
                    "+ New session",
                    style,
                    rr.width.saturating_sub(4),
                );
                hits.add(rr, Target::NewSession);
            }
            SRow::Project(i) => {
                let name = entries[i].project.as_deref().unwrap_or("").to_uppercase();
                put(
                    buf,
                    rr.x + 2,
                    y,
                    &truncate(&name, rr.width.saturating_sub(4) as usize, cx.ell()),
                    muted.add_modifier(Modifier::BOLD),
                    rr.width.saturating_sub(3),
                );
            }
            SRow::Tree(i) => {
                let Some((label, path)) = entries[i].tree.as_ref() else {
                    continue;
                };
                let branch = if cx.t.opts.ascii { "~" } else { "⌥" };
                let inner = rr.width.saturating_sub(4) as usize;
                let label = format!("{branch} {label}");
                let lw = width(&label).min(inner);
                let left = truncate(&label, inner, cx.ell());
                put(
                    buf,
                    rr.x + 2,
                    y,
                    &left,
                    cx.st(SECONDARY, SIDEBAR),
                    inner as u16,
                );
                let room = inner.saturating_sub(lw + 2);
                if room >= 8 {
                    let p = short_path(path, room, cx.ell());
                    put(
                        buf,
                        inner_right.saturating_sub(width(&p) as u16),
                        y,
                        &p,
                        muted,
                        room as u16,
                    );
                }
            }
            SRow::Title(i) | SRow::Detail(i) => {
                let e = &entries[i];
                let Some(s) = a.state.model.sessions.get(&e.id) else {
                    continue;
                };
                let first = matches!(v, SRow::Title(_));
                let selected = viewed == Some(s.id.as_str());
                let bg = Style::default();
                clear(buf, rr, cx.st(SECONDARY, SIDEBAR).patch(bg));
                if focused && i == cursor {
                    put(
                        buf,
                        rr.x,
                        y,
                        cx.t.structure("focus_bar"),
                        cx.st("interactive.focus", SIDEBAR)
                            .patch(bg)
                            .add_modifier(Modifier::BOLD),
                        1,
                    );
                }
                if first {
                    if let Some((glyph, token)) = session_mark(cx, &s.id) {
                        put(buf, rr.x + 2, y, &glyph, cx.st(token, SIDEBAR).patch(bg), 1);
                    }
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
                    let age = text::age(now_secs(), s.updated);
                    let aw = width(&age) as u16;
                    let inner = rr.width.saturating_sub(5);
                    // The time yields when the title would be squeezed too far.
                    let show_age = inner >= aw + 1 + 6;
                    let room = if show_age { inner - aw - 1 } else { inner };
                    draw_session_title(cx, buf, (rr.x + 4, y), room, s, style, bg);
                    if show_age {
                        put(buf, inner_right - aw, y, &age, muted.patch(bg), aw);
                    }
                } else {
                    let (left, meta) = session_detail(cx, s, a.descendants(&s.id));
                    let inner = rr.width.saturating_sub(5) as usize;
                    let mw = width(&meta);
                    let show_meta = mw > 0 && inner >= mw + 1 + 6;
                    let room = if show_meta { inner - mw - 1 } else { inner };
                    let mut x = rr.x + 4;
                    let end = rr.x + 4 + room as u16;
                    let mut budget = room;
                    for (text, st) in left {
                        let t = truncate(&text, budget, cx.ell());
                        budget = budget.saturating_sub(width(&t));
                        x = put(buf, x, y, &t, st.patch(bg), end.saturating_sub(x));
                    }
                    if show_meta {
                        put(
                            buf,
                            inner_right - mw as u16,
                            y,
                            &meta,
                            muted.patch(bg),
                            mw as u16,
                        );
                    }
                }
                hits.add(rr, Target::SideRow(i));
            }
        }
    }
}

/// The mark slot of a session row from its attention data: waiting, error,
/// running (the spinner) or unread, in that order of precedence.
fn session_mark<'t>(cx: &Cx<'t>, id: &str) -> Option<(String, &'t str)> {
    let a = cx.app;
    match a.session_mark(id)? {
        "running" => Some((cx.t.spinner(a.frame / 2).to_owned(), PRIMARY)),
        name => {
            let (glyph, token) = cx.t.attention(name);
            Some((glyph.to_owned(), token))
        }
    }
}

/// Line 2 of a session row: the agent in its colour, `·`, `⌥ branch`, and on
/// the right the child count (`2 ↳`).
fn session_detail(
    cx: &Cx,
    s: &crate::api::Session,
    children: usize,
) -> (Vec<(String, Style)>, String) {
    let a = cx.app;
    let muted = cx.st(MUTED, SIDEBAR);
    let mut left = Vec::new();
    if !s.agent.is_empty() {
        left.push((
            s.agent.clone(),
            cx.st(SECONDARY, SIDEBAR).patch(cx.t.agent_style(&s.agent)),
        ));
    }
    let branch = a
        .session_worktree(s)
        .and_then(|tree| tree["branch"].as_str())
        .filter(|branch| !branch.is_empty());
    if let Some(branch) = branch {
        let mark = if cx.t.opts.ascii { "~" } else { "⌥" };
        let sep = if left.is_empty() {
            String::new()
        } else {
            cx.sep()
        };
        left.push((format!("{sep}{mark} {branch}"), muted));
    }
    let meta = if children == 0 {
        String::new()
    } else {
        format!("{children} {}", if cx.t.opts.ascii { ">" } else { "↳" })
    };
    (left, meta)
}

fn draw_projects(cx: &Cx, buf: &mut Buffer, r: Rect, focused: bool, hits: &mut HitMap) {
    let a = cx.app;
    let ids = a.project_ids();
    let add_row = |buf: &mut Buffer, y: u16, hits: &mut HitMap| {
        if y >= r.bottom() {
            return;
        }
        let rr = Rect::new(r.x, y, r.width, 1);
        let style = if cx.hovered(rr) {
            cx.st(PRIMARY, SIDEBAR)
        } else {
            cx.st(MUTED, SIDEBAR)
        };
        clear(buf, rr, style);
        put(
            buf,
            rr.x + 1,
            y,
            "+ Add project",
            style,
            rr.width.saturating_sub(1),
        );
        hits.add(rr, Target::AddProject);
    };
    if ids.is_empty() {
        put(
            buf,
            r.x + 1,
            r.y + 1,
            "No projects yet.",
            cx.st(MUTED, SIDEBAR),
            r.width - 1,
        );
        add_row(buf, r.y + 3, hits);
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
        put(buf, rx, y, &right, cx.st(MUTED, SIDEBAR), rw);
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
            cx.st(MUTED, SIDEBAR),
            r.width - 4,
        );
        hits.add(rr, Target::SideRow(i));
    }
    // The closing row follows the last visible project.
    let shown = ids.len().saturating_sub(top).min(h) as u16;
    add_row(buf, r.y + shown * per, hits);
}

// ============================================================================
// right sidebar
// ============================================================================

/// Width of the key column of the right sidebar (D12).
const KEY_W: u16 = 9;

/// Where the right sidebar's values start, relative to its left edge.
const VALUE_X: u16 = 1 + KEY_W;

/// An UPPERCASE bold muted section label.
fn section_label(cx: &Cx, buf: &mut Buffer, r: Rect, y: u16, label: &str) {
    put(
        buf,
        r.x + 1,
        y,
        &truncate(
            &label.to_uppercase(),
            r.width.saturating_sub(2) as usize,
            cx.ell(),
        ),
        cx.st(MUTED, SIDEBAR).add_modifier(Modifier::BOLD),
        r.width.saturating_sub(2),
    );
}

/// `Key      value`: a muted key in the 9-cell column and a value truncated in
/// the middle.
fn kv(cx: &Cx, buf: &mut Buffer, r: Rect, y: &mut u16, key: &str, value: &str, style: Style) {
    if *y >= r.bottom() {
        return;
    }
    put(buf, r.x + 1, *y, key, cx.st(MUTED, SIDEBAR), KEY_W);
    let vw = r.width.saturating_sub(VALUE_X + 1) as usize;
    put(
        buf,
        r.x + VALUE_X,
        *y,
        &truncate_middle(value, vw, cx.ell()),
        style,
        vw as u16,
    );
    *y += 1;
}

fn draw_right(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.width < 8 || r.height < 4 {
        return;
    }
    let a = cx.app;
    clear(buf, r, cx.st(SECONDARY, SIDEBAR));
    hits.add(r, Target::Swallow);
    let focused = a.ui.focus == Focus::Right;
    // One blank row under the chrome band, as above the sidebar's search row.
    let mut y = r.y + 1;
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
        if cx.hovered(head) {
            clear(buf, head, cx.st(MUTED, SIDEBAR));
        }
        if focused && i == 0 {
            put(
                buf,
                r.x,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", SIDEBAR)
                    .add_modifier(Modifier::BOLD),
                1,
            );
        }
        let label = match sec.id.as_str() {
            "session_info" => "Session",
            "git_status" => "Git",
            _ => sec.label.as_str(),
        };
        section_label(cx, buf, r, y, label);
        hits.add(head, Target::RightSection(i));
        y += 1;
        match sec.id.as_str() {
            "session_info" => session_info(cx, buf, r, &mut y),
            "git_status" => git_status(cx, buf, r, &mut y, hits),
            _ => {
                let reason = sec
                    .unavailable_reason
                    .clone()
                    .unwrap_or_else(|| "Not available.".into());
                for l in text::wrap(&reason, r.width.saturating_sub(3) as usize) {
                    if y >= r.bottom() {
                        break;
                    }
                    put(buf, r.x + 1, y, &l, cx.st(MUTED, SIDEBAR), r.width - 2);
                    y += 1;
                }
            }
        }
        y += 1;
    }
}

/// A muted one-line note under a section label.
fn note(cx: &Cx, buf: &mut Buffer, r: Rect, y: &mut u16, msg: &str) {
    for line in text::wrap(msg, r.width.saturating_sub(3) as usize) {
        if *y >= r.bottom() {
            break;
        }
        put(buf, r.x + 1, *y, &line, cx.st(MUTED, SIDEBAR), r.width - 2);
        *y += 1;
    }
}

fn git_status(cx: &Cx, buf: &mut Buffer, r: Rect, y: &mut u16, hits: &mut HitMap) {
    let Some(id) = cx.app.viewed_id() else {
        return note(cx, buf, r, y, "Open a session");
    };
    let Some((_, result)) = cx
        .app
        .state
        .git
        .as_ref()
        .filter(|(session, _)| session == id)
    else {
        let msg = if cx.t.opts.ascii {
            "Loading Git status..."
        } else {
            "Loading Git status…"
        };
        return note(cx, buf, r, y, msg);
    };
    let data = match result {
        Err(error) => {
            let message = if error.contains("workspace is not a Git repository") {
                "No Git repository"
            } else {
                error.as_str()
            };
            return note(cx, buf, r, y, message);
        }
        Ok(data) => data,
    };
    // `⌥ main  ↑1`: the branch, then what is ahead or behind when it is not 0.
    let branch = data["branch"].as_str().unwrap_or("—");
    let (up, down) = if cx.t.opts.ascii {
        ("+", "-")
    } else {
        ("↑", "↓")
    };
    let mark = if cx.t.opts.ascii { "~" } else { "⌥" };
    let mut header = format!("{mark} {branch}");
    for (glyph, key) in [(up, "ahead"), (down, "behind")] {
        let n = data[key].as_u64().unwrap_or(0);
        if n > 0 {
            header.push_str(&format!("  {glyph}{n}"));
        }
    }
    put(
        buf,
        r.x + 1,
        *y,
        &truncate(&header, r.width.saturating_sub(2) as usize, cx.ell()),
        cx.st(PRIMARY, SIDEBAR),
        r.width.saturating_sub(2),
    );
    *y += 1;
    let rows = cx.app.git_rows();
    if rows.is_empty() && *y < r.bottom() {
        put(
            buf,
            r.x + 1,
            *y,
            "Clean",
            cx.st(MUTED, SIDEBAR),
            r.width - 2,
        );
        *y += 1;
    }
    let focused = cx.app.ui.focus == Focus::Right;
    let right = r.right().saturating_sub(1);
    let minus = if cx.t.opts.ascii { "-" } else { "−" };
    let mut group = None;
    for (index, row) in rows.iter().enumerate() {
        if group != Some(row.group) {
            group = Some(row.group);
            // A blank row, then `Unstaged` with its count on the right.
            *y += 1;
            if *y >= r.bottom() {
                break;
            }
            let count = rows
                .iter()
                .filter(|other| other.group == row.group)
                .count()
                .to_string();
            put(
                buf,
                r.x + 1,
                *y,
                row.group.label(),
                cx.st(SECONDARY, SIDEBAR),
                r.width.saturating_sub(2),
            );
            put(
                buf,
                right.saturating_sub(width(&count) as u16),
                *y,
                &count,
                cx.st(MUTED, SIDEBAR),
                width(&count) as u16,
            );
            *y += 1;
        }
        if *y >= r.bottom() {
            break;
        }
        let line = Rect::new(r.x, *y, r.width, 1);
        let selected = index == cx.app.ui.git_cursor;
        let style = cx.st(SECONDARY, SIDEBAR);
        let bg = Style::default();
        clear(buf, line, style);
        if selected && focused {
            put(
                buf,
                r.x,
                *y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", SIDEBAR).patch(bg),
                1,
            );
        }
        // Staged rows read the index column, the others the worktree one.
        let code: String = match row.group {
            GitGroup::Staged => row.status.chars().take(1).collect(),
            GitGroup::Unstaged => row.status.chars().skip(1).take(1).collect(),
            GitGroup::Untracked => "?".into(),
        };
        if let Some((glyph, token)) = git_marker(&code) {
            put(
                buf,
                r.x + 1,
                *y,
                &glyph.to_string(),
                cx.st(token, SIDEBAR).patch(bg),
                1,
            );
        }
        // `+N −M` right-aligned; a side that is 0 is left out.
        let mut stats: Vec<(String, &str)> = Vec::new();
        if row.group != GitGroup::Untracked {
            if row.added > 0 {
                stats.push((format!("+{}", row.added), "status.success"));
            }
            if row.removed > 0 {
                stats.push((format!("{minus}{}", row.removed), "status.error"));
            }
        }
        let sw: usize =
            stats.iter().map(|(t, _)| width(t)).sum::<usize>() + stats.len().saturating_sub(1);
        let room = (right.saturating_sub(r.x + 3) as usize).saturating_sub(if sw > 0 {
            sw + 1
        } else {
            0
        });
        put(
            buf,
            r.x + 3,
            *y,
            &truncate_middle(&row.path, room, cx.ell()),
            style,
            room as u16,
        );
        let mut x = right.saturating_sub(sw as u16);
        for (i, (t, token)) in stats.iter().enumerate() {
            if i > 0 {
                x += 1;
            }
            x = put(
                buf,
                x,
                *y,
                t,
                cx.st(token, SIDEBAR).patch(bg),
                width(t) as u16,
            );
        }
        hits.add(line, Target::GitRow(index));
        *y += 1;
    }
}

/// `82k of 200k` style: tokens as whole thousands from 10k, one decimal below.
fn compact(n: u64) -> String {
    if n >= 1_000_000 {
        let m = format!("{:.1}", n as f64 / 1e6);
        format!("{}M", m.trim_end_matches(".0"))
    } else if n >= 10_000 {
        format!("{}k", (n as f64 / 1000.0).round() as u64)
    } else if n >= 1000 {
        let k = format!("{:.1}", n as f64 / 1000.0);
        format!("{}k", k.trim_end_matches(".0"))
    } else {
        n.to_string()
    }
}

fn session_info(cx: &Cx, buf: &mut Buffer, r: Rect, y: &mut u16) {
    let a = cx.app;
    let Some(s) = a.viewed() else {
        return note(cx, buf, r, y, "No session open");
    };
    let now = now_secs();
    let primary = cx.st(PRIMARY, SIDEBAR);
    kv(
        cx,
        buf,
        r,
        y,
        "Agent",
        &s.agent,
        primary.patch(cx.t.agent_style(&s.agent)),
    );
    kv(cx, buf, r, y, "Model", &a_model_label(cx, s), primary);
    kv(
        cx,
        buf,
        r,
        y,
        "Effort",
        s.extra
            .get("effort")
            .and_then(Value::as_str)
            .filter(|e| !e.is_empty())
            .unwrap_or("default"),
        primary,
    );
    // `✓ idle`: the glyph carries the status colour.
    let (state, name) = if a.is_busy(&s.id) {
        ("running", "running")
    } else if s.archived {
        ("archived", "done")
    } else {
        ("idle", "done")
    };
    if *y < r.bottom() {
        let (glyph, _, token) = cx.t.status(name);
        put(buf, r.x + 1, *y, "Status", cx.st(MUTED, SIDEBAR), KEY_W);
        let glyph = if name == "running" {
            cx.t.spinner(a.frame / 2)
        } else {
            glyph
        };
        let x = put(
            buf,
            r.x + VALUE_X,
            *y,
            glyph,
            cx.st(if name == "running" { PRIMARY } else { token }, SIDEBAR),
            2,
        );
        put(
            buf,
            x + 1,
            *y,
            state,
            primary,
            r.right().saturating_sub(x + 2),
        );
        *y += 1;
    }
    let stats = a
        .state
        .model
        .selected
        .as_ref()
        .filter(|v| v.session_id == s.id)
        .map(|v| crate::stats::compute(v, &a.state.models, &a.effective_model(Some(s))));
    if let Some(stats) = &stats {
        let calls = if stats.calls > 0 {
            format!("{}{} tools", cx.sep(), stats.calls)
        } else {
            String::new()
        };
        kv(
            cx,
            buf,
            r,
            y,
            "Turns",
            &format!("{}{calls}", stats.turns),
            primary,
        );
        if !stats.tools.is_empty() {
            let shown: Vec<String> = stats
                .tools
                .iter()
                .take(2)
                .map(|(name, n)| format!("{name} {n}"))
                .collect();
            kv(cx, buf, r, y, "Tools", &shown.join(&cx.sep()), primary);
        }
    }
    let started = crate::timeline::local_clock(s.created)
        .map(|c| format!("{c}{}{}", cx.sep(), text::age(now, s.created)))
        .unwrap_or_else(|| text::ago(now, s.created));
    kv(cx, buf, r, y, "Started", &started, primary);
    kv(cx, buf, r, y, "Id", &s.id, primary);

    // CONTEXT: the meter, `82k of 200k`, tokens and cost; `not reported`
    // wherever Core gives nothing.
    *y += 1;
    if *y >= r.bottom() {
        return;
    }
    section_label(cx, buf, r, *y, "Context");
    *y += 1;
    let none = "not reported";
    let muted = cx.st(MUTED, SIDEBAR);
    let tokens = stats.as_ref().filter(|st| st.has_tokens());
    let budget = a.state.context.get(&s.id);
    let window = match budget {
        Some(c) => c["window"].as_u64(),
        None => a
            .state
            .models
            .iter()
            .find(|m| m["ref"].as_str() == Some(a.effective_model(Some(s)).as_str()))
            .and_then(|m| m["context"].as_u64()),
    }
    .filter(|&n| n > 0);
    let used = budget
        .and_then(|c| c["used"].as_u64())
        .or_else(|| tokens.and_then(|st| st.last_input));
    if let (Some(used), Some(window)) = (used, window) {
        if *y < r.bottom() {
            let fraction = used as f64 / window as f64;
            let label = format!("{:.0}%", fraction * 100.0);
            let lw = width(&label) as u16;
            let cells = r.width.saturating_sub(3 + lw);
            if cells >= 4 {
                let token = if fraction >= 0.95 {
                    "status.error"
                } else if fraction >= 0.80 {
                    "status.warning"
                } else {
                    PRIMARY
                };
                let filled = cx.st(token, SIDEBAR).patch(if token == PRIMARY {
                    cx.t.agent_style(&s.agent)
                } else {
                    Style::default()
                });
                crate::paint::meter(
                    cx.t,
                    buf,
                    (r.x + 1, *y),
                    cells,
                    fraction,
                    filled,
                    cx.st("border.default", SIDEBAR),
                );
            }
            put(
                buf,
                r.right().saturating_sub(lw + 1),
                *y,
                &label,
                cx.st(SECONDARY, SIDEBAR),
                lw,
            );
            *y += 1;
        }
    }
    let used_text = match (used, window) {
        (Some(u), Some(w)) => Some(format!("{} of {}", compact(u), compact(w))),
        (Some(u), None) => Some(compact(u)),
        _ => None,
    };
    for (key, value) in [
        ("Used", used_text),
        (
            "Tokens",
            tokens.map(|st| {
                format!(
                    "{} in{}{} out",
                    compact(st.input),
                    cx.sep(),
                    compact(st.output)
                )
            }),
        ),
        (
            "Cost",
            tokens.and_then(|st| st.cost).map(|usd| {
                format!(
                    "{}{}",
                    crate::stats::money(usd),
                    if tokens.is_some_and(|st| st.partial_cost) {
                        "+"
                    } else {
                        ""
                    }
                )
            }),
        ),
    ] {
        match value {
            Some(v) => kv(cx, buf, r, y, key, &v, primary),
            None => kv(cx, buf, r, y, key, none, muted),
        }
    }
    // The ledger began after the session did (it is older than turn records):
    // the totals count from then, and say so.
    if let Some(since) = tokens
        .and_then(|st| st.since)
        .filter(|since| *since > s.created + 60.0)
    {
        let date = crate::timeline::local_date(since).unwrap_or_else(|| text::ago(now, since));
        kv(cx, buf, r, y, "Since", &date, muted);
    }
}

// ============================================================================
// timeline
// ============================================================================

/// What the page shows when there is no timeline: an optional splash logo, a
/// bold title, muted sentences and key hints.
struct EmptyState {
    /// The splash logo (`paint::LOGO`) above the text: the connecting screen
    /// and the empty workspace.
    logo: bool,
    title: String,
    body: Vec<String>,
    /// `key label` pairs on one row, the key bold.
    hints: Vec<(String, String)>,
}

fn empty_state(cx: &Cx) -> EmptyState {
    let a = cx.app;
    let key = |id: &str, fallback: &str| {
        a.manifest
            .command(id)
            .and_then(|c| c.tui_key())
            .unwrap_or(fallback)
            .to_owned()
    };
    let plain = |title: &str, body: Vec<String>| EmptyState {
        logo: false,
        title: title.to_owned(),
        body,
        hints: Vec::new(),
    };
    match &a.state.connection {
        ConnectionState::Starting => EmptyState {
            logo: true,
            ..plain(
                "Starting Nexus Core",
                vec!["This takes a moment the first time.".into()],
            )
        },
        ConnectionState::Connecting => EmptyState {
            logo: true,
            ..plain("Connecting to Nexus Core", vec![])
        },
        ConnectionState::Reconnecting { .. }
        | ConnectionState::Offline { .. }
        | ConnectionState::AuthFailed { .. }
            if a.state.model.projects.is_empty() =>
        {
            // Nothing was loaded, so "no projects" would be a guess.
            EmptyState {
                logo: true,
                ..plain(
                    "Not connected to Core",
                    vec!["Your projects load when Core is back.".into()],
                )
            }
        }
        _ if a.state.model.projects.is_empty() => EmptyState {
            logo: true,
            hints: vec![
                (key("sidebar.tab.projects", "alt+3"), "add a project".into()),
                (key("palette.open", "ctrl+p"), "commands".into()),
            ],
            ..plain("No projects yet", vec![])
        },
        _ => {
            let name = a
                .current_project()
                .and_then(|p| a.state.model.projects.get(&p).map(|p| p.name.clone()))
                .unwrap_or_default();
            if a.viewed().is_some() {
                plain(
                    "No messages yet",
                    vec!["Type below to start the conversation.".into()],
                )
            } else {
                plain(
                    "New session",
                    vec![
                        format!("Project {name}. Type a message below to start a session."),
                        "f1 keys".into(),
                    ],
                )
            }
        }
    }
}

/// Tokens the generated system prompt costs before the agent body and the
/// instruction file are added. The preview is client-side until Core session
/// drafts land (PLAN.md Step 12a), so every figure is an estimate.
const PROMPT_BASE_TOKENS: u64 = 900;
/// Tokens one tool's schema costs in the preview.
const TOOL_SCHEMA_TOKENS: u64 = 450;
/// The project's `AGENTS.md` as the preview sees it: size in bytes and its
/// first prose line. Read once per path (the preview redraws every frame);
/// `None` when the file is absent or unreadable (a remote Core's project).
fn instruction_file(path: &str) -> Option<(u64, String)> {
    use std::cell::RefCell;
    use std::collections::HashMap;
    thread_local! {
        static CACHE: RefCell<HashMap<String, Option<(u64, String)>>> =
            RefCell::new(HashMap::new());
    }
    CACHE.with(|cache| {
        cache
            .borrow_mut()
            .entry(path.to_owned())
            .or_insert_with(|| {
                let text = std::fs::read_to_string(path).ok()?;
                let line = text
                    .lines()
                    .map(str::trim)
                    .find(|l| !l.is_empty() && !l.starts_with('#') && !l.starts_with("<!--"))
                    .unwrap_or("")
                    .to_owned();
                Some((text.len() as u64, line))
            })
            .clone()
    })
}

/// The sections the next session will start with, estimated on the client.
fn preview_sections(cx: &Cx) -> (Vec<paint::CtxSection>, u64) {
    use paint::{CtxBody, CtxSection};
    let a = cx.app;
    let sep = cx.sep();
    let agent_name = a.current_agent();
    let agent = a
        .state
        .agents
        .iter()
        .find(|ag| ag["name"].as_str() == Some(agent_name.as_str()));
    // Only what the model receives: the agent's prompt body is the system
    // prompt. The description is for the pickers and is never sent.
    let prompt = agent
        .and_then(|ag| ag["prompt"].as_str())
        .unwrap_or("")
        .to_owned();
    // An agent with no `tools` list is offered every default tool; every new
    // session also gets the workspace entry point.
    let mut tools: Vec<String> = match agent.and_then(|ag| ag["tools"].as_array()) {
        Some(list) => list
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect(),
        None => crate::app::DEFAULT_TOOLS
            .iter()
            .map(|t| (*t).to_owned())
            .collect(),
    };
    if !tools.iter().any(|t| t == "expose_workspace_tools") {
        tools.push("expose_workspace_tools".into());
    }
    let project = a
        .current_project()
        .and_then(|p| a.state.model.projects.get(&p));
    let file_path = project.map(|p| format!("{}/AGENTS.md", p.path.trim_end_matches('/')));
    let file = file_path.as_deref().and_then(instruction_file);
    let file_tokens = file.as_ref().map_or(0, |(bytes, _)| bytes.div_ceil(4));
    let prompt_tokens =
        PROMPT_BASE_TOKENS + (prompt.chars().count() as u64).div_ceil(4) + file_tokens;
    let tool_tokens = tools.len() as u64 * TOOL_SCHEMA_TOKENS;
    let mut sections = vec![CtxSection {
        title: "System prompt".into(),
        tokens: Some(prompt_tokens),
        meta: agent_name.clone(),
        body: CtxBody::None,
    }];
    sections.push(match (&file, &file_path) {
        (Some((_bytes, _line)), Some(path)) => CtxSection {
            title: "AGENTS.md".into(),
            tokens: Some(file_tokens),
            meta: format!("in the system prompt{sep}{}", short_home(path)),
            body: CtxBody::None,
        },
        _ => CtxSection {
            title: "AGENTS.md".into(),
            tokens: None,
            meta: "unavailable or not read".into(),
            body: CtxBody::None,
        },
    });
    sections.push(CtxSection {
        title: "Tools".into(),
        tokens: Some(tool_tokens),
        meta: String::new(),
        body: CtxBody::Inventory(a.context_items_for("Tools")),
    });
    for title in ["Skills", "MCP"] {
        sections.push(CtxSection {
            title: title.into(),
            tokens: None,
            meta: a.context_inventory_meta(title),
            body: CtxBody::Inventory(a.context_items_for(title)),
        });
    }
    // The instruction file is part of the system prompt: the total counts the
    // prompt and the tool schemas, as the timeline does after the first turn.
    (sections, prompt_tokens + tool_tokens)
}

fn draw_new_session_context(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) -> usize {
    let a = cx.app;
    if r.height < 4 || r.width < 24 {
        return 0;
    }
    let sep = cx.sep();
    let bold = |fg| cx.st(fg, CANVAS).add_modifier(Modifier::BOLD);
    // The sections.
    let (sections, total) = preview_sections(cx);
    let r = Rect::new(r.x + 2, r.y, r.width.saturating_sub(2), r.height);
    let end = r.right();

    let window = a
        .state
        .models
        .iter()
        .find(|m| m["ref"].as_str() == Some(a.effective_model(None).as_str()))
        .and_then(|m| m["context"].as_u64());
    let layout = paint::CtxLayout {
        width: r.width as usize,
        sep: &sep,
        ell: cx.ell(),
        ascii: cx.t.opts.ascii,
        estimated: true,
        window,
    };
    let mut tip: Option<(u16, u16)> = None;
    let agent_prompt = a
        .state
        .agents
        .iter()
        .find(|ag| ag["name"].as_str() == Some(a.current_agent().as_str()))
        .and_then(|ag| ag["prompt"].as_str())
        .unwrap_or("Agent prompt unavailable.");
    let rows = paint::context_rows(&sections, total, &layout);
    let total_rows = rows.len();
    let top =
        a.ui.scroll
            .top
            .min(total_rows.saturating_sub(r.height as usize));
    for (visible_row, row) in rows.iter().skip(top).take(r.height as usize).enumerate() {
        let y = r.y + visible_row as u16;
        let mut x = r.x;
        let mut hovered_header = false;
        if let Some((title, _)) = row.iter().find(|(_, role)| *role == paint::CtxRole::Title) {
            let content_width: usize = row.iter().map(|(text, _)| width(text)).sum();
            let start = r.x.saturating_sub(1);
            let header = Rect::new(
                start,
                y,
                (content_width as u16 + 2).min(end.saturating_sub(start)),
                1,
            );
            hovered_header = cx.hovered(header);
            if hovered_header {
                clear(buf, header, cx.st(PRIMARY, RAISED));
            }
            let title = title.trim().to_owned();
            let detail = match title.as_str() {
                "System prompt" => format!("Agent prompt preview; the complete system prompt is frozen when the session starts.\n\n{agent_prompt}"),
                "Skills" | "MCP" | "Tools" => a
                    .context_items_for(&title)
                    .iter()
                    .map(|item| {
                        format!(
                            "[{}] {}{}",
                            if item.on { "x" } else { " " },
                            item.label,
                            if item.locked { " · new session" } else { "" }
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n"),
                _ => sections.iter().find(|s| s.title == title).map(|s| match &s.body { paint::CtxBody::Preview(line) => format!("Available first-line preview:\n{line}"), _ => "Instruction file unavailable or not read by this client.".to_owned() }).unwrap_or_default(),
            };
            hits.add(
                header,
                Target::ContextPreview {
                    title,
                    text: detail,
                },
            );
        }
        let toggle = row.iter().find_map(|(_, role)| match role {
            paint::CtxRole::ToggleOn(index)
            | paint::CtxRole::ToggleOff(index)
            | paint::CtxRole::ToggleLocked(index) => Some(*index),
            _ => None,
        });
        let row_rect = Rect::new(r.x, y, r.width, 1);
        let selected = toggle
            .is_some_and(|index| a.ui.focus == Focus::Timeline && a.ui.context_cursor == index);
        let hovered = toggle.is_some_and(|_| cx.hovered(row_rect));
        if selected || hovered {
            clear(buf, row_rect, cx.st(PRIMARY, RAISED));
        }
        for (text, role) in row {
            let mut style = match role {
                paint::CtxRole::Mark | paint::CtxRole::Meta | paint::CtxRole::Preview => {
                    cx.st(MUTED, CANVAS)
                }
                paint::CtxRole::Title | paint::CtxRole::TotalLabel => bold(SECONDARY),
                paint::CtxRole::Tokens => cx.st(SECONDARY, CANVAS),
                paint::CtxRole::ToggleOn(_) => {
                    cx.st(SECONDARY, if selected || hovered { RAISED } else { CANVAS })
                }
                paint::CtxRole::ToggleOff(_) => cx
                    .st(MUTED, if selected || hovered { RAISED } else { CANVAS })
                    .add_modifier(Modifier::CROSSED_OUT),
                paint::CtxRole::ToggleLocked(_) => {
                    cx.st(MUTED, if selected || hovered { RAISED } else { CANVAS })
                }
                paint::CtxRole::TotalFigure => cx.st(SECONDARY, CANVAS),
            };
            if hovered_header || selected || hovered {
                style = style.patch(cx.t.bg(RAISED));
            }
            let start = x;
            x = put(buf, x, y, text, style, end.saturating_sub(x));
            let counts = matches!(role, paint::CtxRole::Tokens | paint::CtxRole::TotalFigure)
                && text.contains('~');
            if counts && cx.hovered(Rect::new(start, y, x - start, 1)) {
                tip = Some((start, y));
            }
        }
        if let Some(index) = toggle {
            hits.add(row_rect, Target::ContextToggle(index));
        }
    }
    // Hovering an estimated count says so.
    if let Some((tx, ty)) = tip {
        let label = " estimated: Core counts when the session starts ";
        let w = (width(label) as u16).min(r.width);
        let tr = Rect::new(tx.min(end.saturating_sub(w)), ty + 1, w, 1);
        if tr.bottom() <= buf.area.bottom() {
            clear(buf, tr, cx.st(SECONDARY, RAISED));
            put(buf, tr.x, tr.y, label, cx.st(SECONDARY, RAISED), tr.width);
        }
    }
    total_rows
}

fn draw_timeline(
    cx: &Cx,
    buf: &mut Buffer,
    r: Rect,
    hits: &mut HitMap,
    timeline: &crate::timeline::Timeline,
) -> (usize, usize, Option<usize>) {
    if r.height == 0 || r.width < 4 {
        return (0, 0, None);
    }
    let a = cx.app;
    hits.add(r, Target::Timeline);
    let inner = Rect::new(r.x + 2, r.y, r.width - 4, r.height);
    let lines = &timeline.lines;
    if a.viewed().is_none() || (lines.is_empty() && a.messages().is_empty()) {
        let empty = empty_state(cx);
        let busy = matches!(
            a.state.connection,
            ConnectionState::Starting | ConnectionState::Connecting
        );
        let head = if busy {
            format!("{} {}", cx.t.spinner(a.frame / 2), empty.title)
        } else {
            empty.title.clone()
        };
        let preview = a.viewed().is_none() && !busy && !a.state.model.projects.is_empty();
        if preview {
            // D10: the context is the view's content; no centred text.
            let total = draw_new_session_context(
                cx,
                buf,
                Rect::new(
                    inner.x,
                    inner.y + 1,
                    inner.width,
                    inner.height.saturating_sub(1),
                ),
                hits,
            );
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
            return (total, r.height.saturating_sub(1) as usize, None);
        }
        let wrap_w = (inner.width as usize).saturating_sub(4).max(10);
        let body: Vec<String> = empty
            .body
            .iter()
            .flat_map(|l| text::wrap(l, wrap_w))
            .collect();
        // logo, a blank row, the title, a blank row, then the sentences and hints
        let text_rows = 2 + body.len() as u16 + if empty.hints.is_empty() { 0 } else { 2 };
        let logo_rows = paint::LOGO.len() as u16 + 1;
        let logo = empty.logo && r.height >= logo_rows + text_rows;
        let total = text_rows + if logo { logo_rows } else { 0 };
        let mut y = r.y + r.height.saturating_sub(total) / if logo { 2 } else { 3 };
        if logo {
            // The accent colour; bold is what carries it in monochrome.
            let mut style = cx.st(ACCENT, CANVAS);
            if cx.t.opts.tier == crate::theme::Tier::Mono {
                style = style.add_modifier(Modifier::BOLD);
            }
            let x0 = inner.x + paint::logo_x(inner.width as usize) as u16;
            for row in paint::LOGO {
                put(buf, x0, y, row, style, inner.right().saturating_sub(x0));
                y += 1;
            }
            y += 1;
        }
        let x0 = |s: &str| inner.x + (inner.width as usize).saturating_sub(width(s)) as u16 / 2;
        put(
            buf,
            x0(&head),
            y,
            &truncate(&head, inner.width as usize, cx.ell()),
            cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
            inner.width,
        );
        y += 2;
        for l in &body {
            let l = truncate(l, inner.width as usize, cx.ell());
            put(buf, x0(&l), y, &l, cx.st(MUTED, CANVAS), inner.width);
            y += 1;
        }
        if !empty.hints.is_empty() {
            let sep = cx.sep();
            let total: usize = empty
                .hints
                .iter()
                .map(|(k, l)| width(k) + 1 + width(l))
                .sum::<usize>()
                + width(&sep) * (empty.hints.len() - 1);
            let mut x = inner.x + (inner.width as usize).saturating_sub(total) as u16 / 2;
            for (i, (k, l)) in empty.hints.iter().enumerate() {
                if i > 0 {
                    x = put(buf, x, y, &sep, cx.st(MUTED, CANVAS), inner.right() - x);
                }
                x = put(
                    buf,
                    x,
                    y,
                    k,
                    cx.st(PRIMARY, CANVAS).add_modifier(Modifier::BOLD),
                    inner.right().saturating_sub(x),
                );
                x = put(
                    buf,
                    x,
                    y,
                    &format!(" {l}"),
                    cx.st(MUTED, CANVAS),
                    inner.right().saturating_sub(x),
                );
            }
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
        return (0, r.height as usize, None);
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
        draw_tline(cx, buf, r, inner, r.y + i as u16, line);
        if a.ui.focus == Focus::Timeline && line.context_item == Some(a.ui.context_cursor) {
            if let Some(bg) = cx.t.color(RAISED) {
                for x in inner.x..inner.right() {
                    buf[(x, r.y + i as u16)].set_bg(bg);
                }
            }
        }
        let is_header = line.context_section.is_some()
            || timeline
                .blocks
                .iter()
                .any(|b| b.kind == crate::timeline::BlockKind::Context && b.start == top + i);
        let content_width: usize = line.spans.iter().map(|span| width(&span.content)).sum();
        let start = inner.x + 1;
        let header = Rect::new(
            start,
            r.y + i as u16,
            (content_width.saturating_sub(2) as u16 + 2).min(inner.right().saturating_sub(start)),
            1,
        );
        if is_header && cx.hovered(header) {
            if let Some(bg) = cx.t.color(RAISED) {
                for x in header.x..header.right() {
                    buf[(x, header.y)].set_bg(bg);
                }
            }
        }
    }
    // Every visible block is clickable; a click selects and toggles it.
    for (bi, b) in timeline.blocks.iter().enumerate() {
        if b.end <= top || b.start >= top + h {
            continue;
        }
        let y0 = r.y + (b.start.saturating_sub(top)) as u16;
        let y1 = r.y + (b.end.min(top + h).saturating_sub(top)) as u16;
        let rect = Rect::new(inner.x, y0, inner.width, y1 - y0);
        if b.kind == crate::timeline::BlockKind::Context {
            if b.start >= top && b.start < top + h {
                hits.add(
                    Rect::new(inner.x, y0, inner.width.min(2), 1),
                    Target::Block(bi),
                );
            }
        } else {
            hits.add(rect, Target::Block(bi));
        }
        // A fence's `copy` sits at the right end of its header row, on focus or hover, when the row has room.
        if b.kind == crate::timeline::BlockKind::Code
            && b.start >= top
            && inner.width >= 8
            && (cx.hovered(rect)
                || (a.ui.focus == Focus::Timeline && a.ui.timeline_sel == Some(bi)))
        {
            let button = Rect::new(inner.right() - 6, y0, 5, 1);
            let empty = (button.x..button.right()).all(|x| buf[(x, y0)].symbol() == " ");
            if empty {
                put(
                    buf,
                    button.x,
                    button.y,
                    "copy",
                    cx.st(MUTED, PANEL),
                    button.width,
                );
                hits.add(button, Target::CopyBlock(bi));
            }
        }
        // An open user band shows `copy` on its last text row (its bottom
        // padding row when the message is a single row, where the first row
        // holds `#n · time`) while hovered or focused.
        let focused = a.ui.focus == Focus::Timeline && a.ui.timeline_sel == Some(bi);
        if b.kind == crate::timeline::BlockKind::Turn
            && b.end - b.start >= 3
            && (cx.hovered(rect) || focused)
            && inner.width >= 8
        {
            let rows = b.end - b.start - 2;
            let row = if rows >= 2 { b.end - 2 } else { b.end - 1 };
            if row >= top && row < top + h {
                let y = r.y + (row - top) as u16;
                let label = "copy";
                let button =
                    Rect::new(inner.right() - label.len() as u16, y, label.len() as u16, 1);
                put(
                    buf,
                    button.x,
                    button.y,
                    label,
                    cx.st(MUTED, RAISED),
                    button.width,
                );
                hits.add(button, Target::CopyBlock(bi));
            }
        }
    }
    // Section headings open the exact frozen snapshot.
    for b in timeline
        .blocks
        .iter()
        .filter(|b| b.kind == crate::timeline::BlockKind::Context)
    {
        for (n, line) in lines
            .iter()
            .enumerate()
            .take(b.end.min(top + h))
            .skip(b.start.max(top))
        {
            if let Some(title) = &line.context_section {
                hits.add(
                    Rect::new(
                        inner.x + 1,
                        r.y + (n - top) as u16,
                        ((line
                            .spans
                            .iter()
                            .map(|span| width(&span.content))
                            .sum::<usize>()) as u16)
                            .min(inner.width.saturating_sub(1)),
                        1,
                    ),
                    Target::ContextSection(title.clone()),
                );
            }
        }
    }
    for (n, line) in lines.iter().enumerate().take(top + h).skip(top) {
        if let Some(index) = line.context_item {
            hits.add(
                Rect::new(inner.x, r.y + (n - top) as u16, inner.width, 1),
                Target::ContextToggle(index),
            );
        }
    }
    draw_sticky(cx, buf, r, inner, hits, timeline, top);
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
    // Blocks wholly below the viewport count for the jump pill, which is
    // drawn on the composer's live row, never over the timeline.
    let behind = (top < max_top).then(|| {
        timeline
            .blocks
            .iter()
            .filter(|b| b.start >= top + h)
            .count()
    });
    (total, h, behind)
}

/// The jump-to-latest pill (D15): raised, right-aligned on the live row above
/// the composer card, `↓ 3 new · end`. `behind` counts the blocks below the
/// viewport.
fn draw_jump(cx: &Cx, buf: &mut Buffer, composer: Rect, behind: usize, hits: &mut HitMap) {
    if composer.height < 7 {
        return;
    }
    let key = cx
        .app
        .manifest
        .command("session.jump_latest")
        .and_then(|c| c.tui_key())
        .unwrap_or("end");
    let arrow = if cx.t.opts.ascii { "v" } else { "\u{2193}" };
    let count = if behind > 0 {
        format!(" {behind} new")
    } else {
        String::new()
    };
    let s = format!(" {arrow}{count} {} {key} ", cx.t.structure("separator"));
    let rr = paint::pill_rect(composer, width(&s) as u16);
    let mut st = cx.st(SECONDARY, RAISED);
    if cx.hovered(rr) {
        st = st.add_modifier(Modifier::UNDERLINED);
    }
    clear(buf, rr, st);
    put(buf, rr.x, rr.y, &s, st, rr.width);
    put(buf, rr.x + 1, rr.y, arrow, cx.st(ACCENT, RAISED), 1);
    hits.add(rr, Target::JumpLatest);
}

/// One timeline row. User bands and their rails stay inside the page gutters.
fn draw_tline(
    cx: &Cx,
    buf: &mut Buffer,
    r: Rect,
    inner: Rect,
    y: u16,
    line: &crate::timeline::TLine,
) {
    if let Some(bg) = line.bg {
        clear(buf, Rect::new(inner.x, y, inner.width, 1), bg);
    }
    buf.set_line(inner.x, y, &Line::from(line.spans.clone()), inner.width);
    if line.focus && line.rail.is_none() {
        let glyph = if cx.t.opts.ascii {
            cx.t.structure("rail")
        } else {
            cx.t.structure("focus_bar")
        };
        put(buf, r.x, y, glyph, cx.st("interactive.focus", CANVAS), 1);
    }
    if let Some(rail) = line.rail {
        put(buf, inner.x, y, cx.t.structure("rail"), rail, 1);
    }
}

/// The sticky user header (D04): the pinned copy of the user band whose text
/// has scrolled off the top, with a shade row and the count of this turn's rows
/// above. The next band pushes it up; a click scrolls back to the band.
fn draw_sticky(
    cx: &Cx,
    buf: &mut Buffer,
    r: Rect,
    inner: Rect,
    hits: &mut HitMap,
    timeline: &crate::timeline::Timeline,
    top: usize,
) {
    let bands: Vec<(usize, &crate::timeline::Block)> = timeline
        .blocks
        .iter()
        .enumerate()
        .filter(|(_, b)| b.kind == crate::timeline::BlockKind::Turn && b.end - b.start >= 3)
        .collect();
    let cards: Vec<(usize, usize)> = bands
        .iter()
        .map(|(_, b)| (b.start, b.end - b.start))
        .collect();
    let Some(pin) = crate::sticky::select(&cards, top, timeline.lines.len()) else {
        return;
    };
    let (block_index, _) = bands[pin.card];
    let skip = pin.rows - pin.shown;
    let ascii = cx.t.opts.ascii;
    for k in skip..pin.rows {
        let y = r.y + (k - skip) as u16;
        if y >= r.bottom() {
            break;
        }
        if k < pin.body {
            let mut line = crate::timeline::TLine {
                spans: timeline.lines[pin.first + 1 + k].spans.clone(),
                bg: timeline.lines[pin.first + 1 + k].bg,
                rail: timeline.lines[pin.first + 1 + k].rail,
                focus: false,
                context_section: None,
                context_item: None,
            };
            if pin.clipped && k + 1 == pin.body {
                crate::sticky::ellipsize(&mut line.spans, cx.ell());
            }
            draw_tline(cx, buf, r, inner, y, &line);
        } else {
            let row = Rect::new(inner.x, y, inner.width, 1);
            clear(buf, row, cx.st(MUTED, RAISED));
            let (shade, label) =
                crate::sticky::shade(crate::sticky::above(&pin, top), inner.width as usize, ascii);
            let x = put(
                buf,
                inner.x,
                y,
                &shade,
                cx.st("border.strong", RAISED),
                inner.width,
            );
            put(buf, x, y, &label, cx.st(MUTED, RAISED), inner.width);
            put(
                buf,
                inner.x,
                y,
                cx.t.structure("rail"),
                cx.st(MUTED, RAISED),
                1,
            );
        }
    }
    let shown = Rect::new(
        inner.x,
        r.y,
        inner.width,
        pin.shown.min(r.height as usize) as u16,
    );
    hits.add(shown, Target::StickyUser(block_index));
}

// ============================================================================
// waiting card and composer
// ============================================================================

/// Diff rows the waiting card shows at most (D14).
const WAIT_DIFF_ROWS: usize = 12;
/// Parameter-table rows the card shows at most.
const WAIT_TABLE_ROWS: usize = 6;

/// One row of the waiting card, in order. Rows are listed whole, then dropped
/// by priority (diff, blanks, pads, table, options) until the card fits.
enum WRow {
    Pad,
    Blank,
    Head,
    Sub,
    Table(String, String),
    Diff(Vec<ratatui::text::Span<'static>>),
    DiffNote(String),
    Ask(String),
    Opt(usize),
    Field(String),
    Choice(usize),
    Hint,
}

type ApprovalParts = (String, Vec<(String, String)>, Option<crate::diff::Diff>);

/// The approval's tool, its parameters and, for edit, write and patch, the diff.
fn approval_parts(app: &App, id: &str) -> ApprovalParts {
    use crate::diff::Diff;
    let Some(p) = app.state.model.approvals.get(id) else {
        return (String::new(), Vec::new(), None);
    };
    let args = match &p.args {
        Value::String(s) => serde_json::from_str(s).unwrap_or(Value::Null),
        other => other.clone(),
    };
    let str_of = |keys: &[&str]| {
        keys.iter()
            .find_map(|k| args[*k].as_str())
            .unwrap_or("")
            .to_owned()
    };
    let (diff, skip): (Option<Diff>, &[&str]) = match p.tool.as_str() {
        "edit" | "patch" => {
            let d = match args["patch"].as_str() {
                Some(patch) => Diff::from_patch(patch),
                None => Diff::from_replace(
                    &str_of(&["old_string", "old"]),
                    &str_of(&["new_string", "new"]),
                ),
            };
            (
                Some(d),
                &["patch", "old_string", "new_string", "old", "new"],
            )
        }
        "write" => (Some(Diff::from_new(&str_of(&["content"]))), &["content"]),
        _ => (None, &[]),
    };
    let diff = diff.filter(|d| !d.is_empty());
    let mut table = Vec::new();
    match &args {
        Value::Object(m) => {
            for (k, v) in m {
                if diff.is_some() && skip.contains(&k.as_str()) {
                    continue;
                }
                let text = json_str(v);
                let mut lines = text.lines();
                let first = lines.next().unwrap_or("").to_owned();
                let more = lines.next().is_some();
                table.push((k.clone(), if more { format!("{first} …") } else { first }));
            }
        }
        Value::Null => {}
        other => table.push(("args".into(), json_str(other))),
    }
    (p.tool.clone(), table, diff)
}

/// The rows of the focused waiting card for a region `w` cells wide, fitted to
/// `limit` rows (`usize::MAX` for the natural height).
fn wait_rows(app: &App, w: u16, limit: usize) -> Vec<WRow> {
    let Some(item) = app.wait_focused() else {
        return Vec::new();
    };
    let inner = w.saturating_sub(8) as usize;
    let mut rows = vec![WRow::Pad, WRow::Head, WRow::Sub, WRow::Blank];
    match item.kind {
        WaitKind::Approval => {
            let (_, table, diff) = approval_parts(app, &item.id);
            for (k, v) in table.into_iter().take(WAIT_TABLE_ROWS) {
                rows.push(WRow::Table(k, v));
            }
            if let Some(diff) = diff {
                let look = crate::diff::Look::from_theme(&app.theme);
                let all = diff.render(inner, &look);
                rows.push(WRow::Blank);
                let cap = if all.len() > WAIT_DIFF_ROWS {
                    WAIT_DIFF_ROWS - 1
                } else {
                    all.len()
                };
                let more = all.len() - cap;
                rows.extend(all.into_iter().take(cap).map(WRow::Diff));
                if more > 0 {
                    rows.push(WRow::DiffNote(format!("{more} more rows")));
                }
            }
            rows.push(WRow::Blank);
            rows.extend((0..4).map(WRow::Choice));
            rows.push(WRow::Blank);
        }
        WaitKind::Question => {
            if let Some(c) = app.ask_current() {
                rows.push(WRow::Ask(
                    json_str(&c.question["question"]).replace('\n', " "),
                ));
                let opts = c.options().len();
                let shown = opts.min(ASK_ROWS);
                let top = c
                    .draft
                    .cursor
                    .saturating_sub(shown.saturating_sub(1))
                    .min(opts - shown);
                rows.extend((top..top + shown).map(WRow::Opt));
                if opts == 0 {
                    rows.push(WRow::Field(
                        "Type the answer in the composer below, then press enter.".into(),
                    ));
                } else if c.allows_text() {
                    rows.push(WRow::Blank);
                    rows.push(WRow::Field(
                        "Or type your own answer in the composer below.".into(),
                    ));
                }
            }
            rows.push(WRow::Blank);
        }
    }
    rows.push(WRow::Hint);
    rows.push(WRow::Pad);
    // Fit: diff first, then blanks, pads, the field, table rows, option rows.
    fn drop_last(rows: &mut Vec<WRow>, pick: fn(&WRow) -> bool, keep: usize) -> bool {
        if rows.iter().filter(|r| pick(r)).count() > keep {
            if let Some(i) = rows.iter().rposition(pick) {
                rows.remove(i);
                return true;
            }
        }
        false
    }
    while rows.len() > limit {
        let done = drop_last(
            &mut rows,
            |r| matches!(r, WRow::Diff(_) | WRow::DiffNote(_)),
            0,
        ) || drop_last(&mut rows, |r| matches!(r, WRow::Blank), 0)
            || drop_last(&mut rows, |r| matches!(r, WRow::Pad), 0)
            || drop_last(&mut rows, |r| matches!(r, WRow::Field(_)), 0)
            || drop_last(&mut rows, |r| matches!(r, WRow::Table(..)), 1)
            || drop_last(&mut rows, |r| matches!(r, WRow::Opt(_)), 1);
        if !done {
            break;
        }
    }
    rows
}

fn waiting_height(app: &App, w: u16) -> u16 {
    wait_rows(app, w, usize::MAX).len() as u16
}

/// Key hints as `key label` pairs: the key bold, the label muted. Returns the
/// rect of each pair.
fn hints_at(
    cx: &Cx,
    buf: &mut Buffer,
    (x, y): (u16, u16),
    end: u16,
    hints: &[(String, String)],
) -> Vec<Rect> {
    let key = cx.st(PRIMARY, PANEL).add_modifier(Modifier::BOLD);
    let muted = cx.st(MUTED, PANEL);
    let mut at = x;
    let mut out = Vec::new();
    for (i, (k, label)) in hints.iter().enumerate() {
        if i > 0 {
            at = put(buf, at, y, &cx.sep(), muted, end.saturating_sub(at));
        }
        let start = at;
        at = put(buf, at, y, k, key, end.saturating_sub(at));
        let label = if k.is_empty() {
            label.clone()
        } else {
            format!(" {label}")
        };
        at = put(buf, at, y, &label, muted, end.saturating_sub(at));
        out.push(Rect::new(start, y, at - start, 1));
    }
    out
}

fn mmss(secs: u64) -> String {
    format!("{}:{:02}", secs / 60, secs % 60)
}

const READ_ONLY_CARD: &str = "read-only until Core is connected";

/// The waiting card (D14), docked above the composer on `surface.panel` with a
/// heavy accent rail: the header, the parameters (and the diff, capped at 12
/// rows) or the question, the key-chip choices and the key hints.
fn draw_waiting(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.height < 3 || r.width < 12 {
        return;
    }
    let a = cx.app;
    let Some(w) = a.wait_focused() else { return };
    let items = a.wait_items();
    let at = a.ui.wait_idx.min(items.len().saturating_sub(1));
    let focused = a.ui.focus == Focus::Waiting;
    let writable = a.writable();
    let card = Rect::new(r.x + 2, r.y, r.width.saturating_sub(4), r.height);
    clear(buf, card, cx.st(SECONDARY, PANEL));
    let rail_style = if focused {
        cx.st(ACCENT, PANEL).add_modifier(Modifier::BOLD)
    } else {
        cx.st(ACCENT, PANEL)
    };
    for y in card.y..card.bottom() {
        put(buf, card.x, y, cx.t.structure("rail_heavy"), rail_style, 1);
    }
    let x = card.x + 3;
    let end = card.right().saturating_sub(2);
    let inner_w = end.saturating_sub(x);
    let rows = wait_rows(a, r.width, r.height as usize);
    let (wg, _, wt) = cx.t.status("waiting");
    let tool = if w.kind == WaitKind::Approval {
        approval_parts(a, &w.id).0
    } else {
        String::new()
    };
    let marker = if cx.t.opts.ascii { ">" } else { "\u{25b8}" };
    let table_w = rows
        .iter()
        .filter_map(|row| match row {
            WRow::Table(k, _) => Some(width(k)),
            _ => None,
        })
        .max()
        .unwrap_or(0)
        .min(14);
    // The selected choice: the `▸` marker and bold text, no fill.
    let band_row = |buf: &mut Buffer, y: u16, _fg: &str| {
        put(
            buf,
            card.x + 1,
            y,
            marker,
            cx.st(ACCENT, PANEL).add_modifier(Modifier::BOLD),
            1,
        );
    };
    for (i, row) in rows.iter().enumerate() {
        let y = card.y + i as u16;
        match row {
            WRow::Pad | WRow::Blank => {}
            WRow::Head => {
                let title = match w.kind {
                    WaitKind::Approval => format!("Allow {tool}?"),
                    WaitKind::Question => "Nexus asks".to_owned(),
                };
                let tx = put(buf, x, y, wg, cx.st(wt, PANEL), 2);
                let right = if items.len() > 1 {
                    let key = a
                        .manifest
                        .command("waiting.next")
                        .and_then(|c| c.tui_key())
                        .map(|k| format!("{}{k} next", cx.sep()))
                        .unwrap_or_default();
                    format!("{} of {}{key}", at + 1, items.len())
                } else {
                    String::new()
                };
                let rw = width(&right) as u16;
                let room = inner_w.saturating_sub(rw + 3) as usize;
                put(
                    buf,
                    tx + 1,
                    y,
                    &truncate(&title, room, cx.ell()),
                    cx.st(PRIMARY, PANEL).add_modifier(Modifier::BOLD),
                    room as u16,
                );
                if rw > 0 {
                    let rx = end.saturating_sub(rw);
                    put(buf, rx, y, &right, cx.st(MUTED, PANEL), rw);
                    hits.add(Rect::new(rx, y, rw, 1), Target::Wait(WaitButton::Next));
                }
            }
            WRow::Sub => {
                let mut sub = format!("waiting on you{}{}", cx.sep(), mmss(a.wait_secs(&w.id)));
                if let Some(c) = a.ask_current().filter(|c| c.total > 1) {
                    sub.push_str(&format!(
                        "{}question {} of {}",
                        cx.sep(),
                        c.index + 1,
                        c.total
                    ));
                }
                if let Some(child) = &w.child {
                    sub.push_str(&format!("{}in child: {child}", cx.sep()));
                }
                put(
                    buf,
                    x,
                    y,
                    &truncate(&sub, inner_w as usize, cx.ell()),
                    cx.st(MUTED, PANEL),
                    inner_w,
                );
            }
            WRow::Table(k, v) => {
                put(
                    buf,
                    x,
                    y,
                    &truncate(k, table_w, cx.ell()),
                    cx.st(MUTED, PANEL),
                    table_w as u16,
                );
                let vx = x + table_w as u16 + 2;
                let room = end.saturating_sub(vx) as usize;
                put(
                    buf,
                    vx,
                    y,
                    &truncate(v, room, cx.ell()),
                    cx.st(PRIMARY, PANEL),
                    room as u16,
                );
            }
            WRow::Diff(spans) => {
                buf.set_line(x, y, &Line::from(spans.clone()), inner_w);
            }
            WRow::DiffNote(note) => {
                put(buf, x, y, note, cx.st(MUTED, PANEL), inner_w);
            }
            WRow::Ask(q) => {
                put(
                    buf,
                    x,
                    y,
                    &truncate(q, inner_w as usize, cx.ell()),
                    cx.st(PRIMARY, PANEL).add_modifier(Modifier::BOLD),
                    inner_w,
                );
            }
            WRow::Opt(i) => {
                let Some(c) = a.ask_current() else { continue };
                let Some(o) = c.options().get(*i) else {
                    continue;
                };
                let on = c.draft.chosen.contains(i);
                let mark = match (c.multiple(), on) {
                    (true, true) => "[x]",
                    (true, false) => "[ ]",
                    (false, true) => "(*)",
                    (false, false) => "( )",
                };
                let label = json_str(&o["label"]);
                let line = format!("{mark} {} {}", (i + 1) % 10, label.trim_matches('"'));
                let mut st = cx.st(SECONDARY, PANEL);
                if *i == c.draft.cursor {
                    band_row(buf, y, PRIMARY);
                    st = cx.st(PRIMARY, PANEL).add_modifier(Modifier::BOLD);
                }
                put(
                    buf,
                    x,
                    y,
                    &truncate(&line, inner_w as usize, cx.ell()),
                    st,
                    inner_w,
                );
                if writable {
                    let lw = width(&line).min(inner_w as usize) as u16;
                    hits.add(Rect::new(x, y, lw, 1), Target::WaitOption(*i));
                }
            }
            WRow::Field(text) => {
                clear(buf, Rect::new(x, y, inner_w, 1), cx.st(MUTED, RAISED));
                put(
                    buf,
                    x + 1,
                    y,
                    &truncate(text, inner_w.saturating_sub(2) as usize, cx.ell()),
                    cx.st(MUTED, RAISED),
                    inner_w.saturating_sub(2),
                );
            }
            WRow::Choice(n) => {
                let defs: [(&str, &str, Option<WaitButton>, &str); 4] = [
                    (
                        "Allow once",
                        "waiting.allow_once",
                        Some(WaitButton::Allow),
                        "",
                    ),
                    (
                        "Allow for session",
                        "waiting.allow_session",
                        None,
                        "not offered by Core yet",
                    ),
                    ("Deny", "waiting.deny", Some(WaitButton::Deny), ""),
                    (
                        "Deny with a note",
                        "waiting.deny_note",
                        None,
                        "Core does not support notes yet",
                    ),
                ];
                let (label, cmd, target, why) = defs[*n];
                let offered = target.is_some();
                let enabled = offered && writable;
                let selected = enabled && a.ui.wait_choice == *n;
                let bg = if selected {
                    band_row(buf, y, PRIMARY);
                    PANEL
                } else {
                    PANEL
                };
                let key = a
                    .manifest
                    .command(cmd)
                    .and_then(|c| c.tui_key())
                    .unwrap_or("");
                let chip = format!(" {key} ");
                let chip_style = if enabled {
                    cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD)
                } else {
                    cx.st(MUTED, RAISED)
                };
                let cw = (width(&chip) as u16).max(3);
                put(buf, x, y, &chip, chip_style, cw);
                let lx = x + cw + 1;
                let mut label_style = if enabled {
                    cx.st(PRIMARY, bg)
                } else {
                    cx.st(MUTED, bg)
                };
                if selected {
                    label_style = label_style.add_modifier(Modifier::BOLD);
                }
                let lend = put(buf, lx, y, label, label_style, end.saturating_sub(lx));
                let reason = if !offered {
                    why
                } else if !writable {
                    READ_ONLY_CARD
                } else {
                    ""
                };
                if !reason.is_empty() {
                    let rx = lend + 2;
                    put(
                        buf,
                        rx,
                        y,
                        &truncate(reason, end.saturating_sub(rx) as usize, cx.ell()),
                        cx.st(MUTED, bg),
                        end.saturating_sub(rx),
                    );
                }
                if let (Some(t), true) = (target, enabled) {
                    hits.add(
                        Rect::new(card.x + 1, y, card.width.saturating_sub(1), 1),
                        Target::Wait(t),
                    );
                }
            }
            WRow::Hint => {
                let key = |id: &str, fallback: &str| {
                    a.manifest
                        .command(id)
                        .and_then(|c| c.tui_key().map(str::to_owned))
                        .unwrap_or_else(|| fallback.to_owned())
                };
                let mut hints: Vec<(String, String)> = Vec::new();
                let mut submit = None;
                match (&w.kind, a.ask_current()) {
                    (WaitKind::Question, Some(c)) if c.draft.sent => {
                        hints.push((String::new(), "sending the answers...".into()));
                    }
                    (WaitKind::Question, Some(c)) if focused => {
                        if !c.options().is_empty() {
                            hints.push((updown(cx).to_owned(), "move".into()));
                        }
                        if c.multiple() {
                            hints.push(("space".into(), "tick".into()));
                        }
                        let last = c.index + 1 >= c.total;
                        submit = Some(hints.len());
                        hints.push((
                            "enter".into(),
                            if last { "submit" } else { "next question" }.into(),
                        ));
                        hints.push(("esc".into(), "stops the turn".into()));
                    }
                    (_, _) if !focused => {
                        hints.push((key("waiting.focus", "alt+w"), "answer".into()));
                    }
                    _ => {
                        hints.push((updown(cx).to_owned(), "choose".into()));
                        hints.push(("enter".into(), "confirm".into()));
                    }
                }
                let rects = hints_at(cx, buf, (x, y), end, &hints);
                if let (Some(i), true) = (submit, writable) {
                    if let Some(rc) = rects.get(i) {
                        hits.add(*rc, Target::Wait(WaitButton::Submit));
                    }
                }
            }
        }
    }
}

/// Display parts of a `provider/id` model reference. The catalog name wins over
/// the id; the id is the fallback for a model the catalog no longer lists.
/// The details sidebar's model value: the effective `provider/id`.
fn a_model_label(cx: &Cx, s: &crate::api::Session) -> String {
    let reference = cx.app.effective_model(Some(s));
    if reference.is_empty() {
        cx.ell().to_owned()
    } else {
        reference
    }
}

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
    /// `11.1K (4.1%)`: the context in use and its share of the window.
    summary: String,
    /// Fill against the first tier until it is reached, then against the window.
    fraction: Option<f64>,
    lines: Vec<String>,
}

/// The latest request's input is an estimate of occupied context. Occupancy is
/// shown against each price tier boundary and the window, as old Nexus did.
fn composer_usage(cx: &Cx, session: &crate::api::Session) -> Option<ComposerUsage> {
    let budget = cx.app.state.context.get(&session.id);
    let input = budget
        .and_then(|b| b["used"].as_u64())
        .or_else(|| cx.app.state.model.selected.as_ref()?.latest_input())?;
    let exact = budget.and_then(|b| b["exact"].as_bool()).unwrap_or(true);
    let model = cx.app.effective_model(Some(session));
    let entry = cx
        .app
        .state
        .models
        .iter()
        .find(|m| m["ref"].as_str() == Some(model.as_str()));
    let window = match budget {
        Some(b) => b["window"].as_u64(),
        None => entry.and_then(|m| m["context"].as_u64()),
    }
    .filter(|&n| n > 0);
    let mut tiers: Vec<u64> = entry
        .and_then(|m| m["tiers"].as_array())
        .map(|tiers| tiers.iter().filter_map(Value::as_u64).collect())
        .unwrap_or_default();
    tiers.retain(|&tier| tier > 0 && Some(tier) != window);
    tiers.sort_unstable();
    tiers.dedup();
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
    lines.push(
        if budget.is_none() {
            "Estimated from the latest request's input tokens."
        } else if exact {
            "Provider usage."
        } else {
            "Estimated context."
        }
        .to_owned(),
    );
    let fraction = tiers
        .first()
        .filter(|&&tier| input < tier)
        .map(|&tier| input as f64 / tier as f64)
        .or_else(|| window.map(|window| input as f64 / window as f64));
    let mut figures = vec![format!(
        "{}{}",
        if exact { "" } else { "~" },
        text::count(input)
    )];
    figures.extend(tiers.iter().map(|tier| percent(input, *tier)));
    if let Some(window) = window {
        figures.push(percent(input, window));
    }
    let summary = figures.join(&cx.sep());
    Some(ComposerUsage {
        summary,
        fraction,
        lines,
    })
}

/// The card's half-row edge (`▄` above, `▀` below, in the panel colour over
/// the canvas); mono and ASCII draw a `─` rule instead (DESIGN_PLAN.md D11).
fn card_edge(cx: &Cx, buf: &mut Buffer, panel: Rect, y: u16, glyph: &str) {
    let plain = cx.t.opts.ascii || cx.t.mono();
    let style = match (cx.t.color(PANEL), cx.t.color(CANVAS)) {
        (Some(fg), Some(bg)) if !plain => Style::default().fg(fg).bg(bg),
        _ => cx.st("border.default", CANVAS),
    };
    let glyph = if style.bg.is_some() && !plain {
        glyph
    } else {
        cx.t.structure("rule")
    };
    for x in panel.x..panel.right() {
        put(buf, x, y, glyph, style, 1);
    }
}

/// The right-aligned activity on the location strip, with Working shimmering.
fn draw_working(cx: &Cx, buf: &mut Buffer, start: u16, y: u16, end: u16) {
    let a = cx.app;
    let started = a.state.model.selected.as_ref().and_then(|view| {
        view.messages
            .iter()
            .rev()
            .find(|m| m.role == "user")
            .map(|m| m.created)
    });
    let mut tail = String::new();
    if let Some(started) = started {
        tail.push_str(&format!(
            "{}{}",
            cx.sep(),
            crate::timeline::fmt_duration(now_secs() - started)
        ));
    }
    let mut x = end
        .saturating_sub((width("Working") + width(&tail)) as u16)
        .max(start);
    clear(
        buf,
        Rect::new(
            x.saturating_sub(1).max(start),
            y,
            end.saturating_sub(x.saturating_sub(1).max(start)),
            1,
        ),
        cx.st(MUTED, CANVAS),
    );
    let animate = !cx.t.opts.reduced_motion && !cx.t.mono();
    let peak = cx.st(PRIMARY, CANVAS);
    let spans = crate::theme::shimmer(
        "Working",
        a.frame,
        animate,
        if animate { cx.st(MUTED, CANVAS) } else { peak },
        cx.st(SECONDARY, CANVAS),
    );
    for span in spans {
        x = put(buf, x, y, &span.content, span.style, end.saturating_sub(x));
    }
    put(
        buf,
        x,
        y,
        &tail,
        cx.st(MUTED, CANVAS),
        end.saturating_sub(x),
    );
}

fn draw_composer(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    let inputs = cx.app.pending_inputs();
    let count = inputs.len().min(4);
    let extra = if count == 0 { 0 } else { (count + 1) as u16 }.min(r.height.saturating_sub(8));
    if extra > 0 {
        let visible = (extra - 1) as usize;
        let selected = cx
            .app
            .ui
            .pending_selected
            .unwrap_or(inputs.len() - 1)
            .min(inputs.len() - 1);
        let start = selected
            .saturating_add(1)
            .saturating_sub(visible)
            .min(inputs.len().saturating_sub(visible));
        let held = cx
            .app
            .viewed_id()
            .and_then(|id| cx.app.state.inputs.get(id))
            .is_some_and(|v| v["held"] == true);
        let heading = if held {
            "Queue held · enter steer · tab queue · n send next"
        } else {
            "Pending · enter steer · tab queue"
        };
        put(
            buf,
            r.x + 2,
            r.y,
            heading,
            cx.st(MUTED, PANEL),
            r.width.saturating_sub(4),
        );
        for (line, (idx, item)) in inputs
            .iter()
            .enumerate()
            .skip(start)
            .take(visible)
            .enumerate()
        {
            let text = item["parts"]
                .as_array()
                .and_then(|parts| parts.iter().find(|p| p["type"] == "text"))
                .and_then(|p| p["text"].as_str())
                .unwrap_or("[image]")
                .lines()
                .next()
                .unwrap_or("");
            let mode = if item["mode"] == "steer" {
                "steer"
            } else {
                "queued"
            };
            let selected = cx.app.ui.pending_selected == Some(idx);
            let label = format!(
                "{} {} · {}{}",
                if selected { "▸" } else { " " },
                mode,
                text,
                if selected {
                    "  enter edit · backspace remove · s steer · shift+↑/↓ move"
                } else {
                    ""
                }
            );
            put(
                buf,
                r.x + 2,
                r.y + 1 + line as u16,
                &label,
                cx.st(if selected { PRIMARY } else { MUTED }, PANEL),
                r.width.saturating_sub(4),
            );
        }
    }
    let r = Rect::new(r.x, r.y + extra, r.width, r.height.saturating_sub(extra));
    if r.height < 8 || r.width < 8 {
        return;
    }
    draw_activity_line(cx, buf, r);
    let full_r = r;
    let r = composer_content(r);
    let a = cx.app;
    let base = cx.st(PRIMARY, PANEL);
    let comp = a.composer_ref();
    let card = composer_card(r, !comp.images.is_empty());
    // Rows: the pill/attachment row, the top padding, the input, a blank row,
    // the control row, the bottom edge and the project strip.
    let panel = Rect::new(
        card.x + 2,
        card.y + 1,
        card.width.saturating_sub(4),
        card.height - 2,
    );
    clear(buf, panel, base);
    hits.add(panel, Target::Composer);
    card_edge(cx, buf, panel, panel.bottom() - 1, "\u{2580}");
    let inner = composer_inner(full_r, !comp.images.is_empty());
    let cy = r.bottom() - 3;
    // The focus bar at the card's left edge, only where colour cannot carry
    // focus (ANSI-16 and mono; DESIGN_PLAN D11).
    if a.ui.focus == Focus::Composer
        && matches!(
            cx.t.opts.tier,
            crate::theme::Tier::Ansi16 | crate::theme::Tier::Mono
        )
    {
        for y in inner.y..=cy {
            put(
                buf,
                panel.x,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", PANEL),
                1,
            );
        }
    }

    // input
    if !comp.images.is_empty() && card.y >= r.y + 2 {
        let shown = comp.images.len();
        put(
            buf,
            inner.x,
            r.y + 1,
            &format!(
                "{shown} attached: {}",
                comp.images
                    .iter()
                    .map(|image| image.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            cx.st(MUTED, CANVAS),
            inner.width,
        );
    }
    if comp.is_empty() {
        let ph = if !a.writable() {
            match a.state.connection {
                // The draft is kept and goes out once Core is back (D18).
                ConnectionState::Reconnecting { .. } | ConnectionState::Offline { .. } => {
                    "Reconnecting to Core\u{2026} your draft is kept".replace('\u{2026}', cx.ell())
                }
                ConnectionState::Starting | ConnectionState::Connecting => {
                    "Connecting to Core\u{2026} your draft is kept".replace('\u{2026}', cx.ell())
                }
                _ => format!("Disconnected {} read-only", cx.t.structure("separator")),
            }
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
                panel.right().saturating_sub(width(&more) as u16 + 2),
                inner.y,
                &more,
                cx.st(MUTED, PANEL),
                8,
            );
        }
    }

    // Selectors and the meter share the control row; each has its own hit
    // target. The right block is sized first: the left side gives way in the
    // order effort → provider → meter → truncated names.
    let avail = panel.width.saturating_sub(6) as usize;
    let busy = a.viewed_id().is_some_and(|i| a.is_busy(i));
    let interrupted = a.term("session.interrupted").unwrap_or(false);
    let session = a.viewed();
    let usage = session.and_then(|s| composer_usage(cx, s));
    let request_model = a
        .state
        .model
        .selected
        .as_ref()
        .and_then(|view| view.latest_request_model())
        .map(|m| m.rsplit('/').next().unwrap_or(&m).to_owned());
    let mut effort_on = true;
    let mut provider_on = true;
    // A new session starts with the build agent until one is chosen.
    let agent_owned = a.current_agent();
    let agent_name = agent_owned.as_str();
    let mut agent_label = text::capitalize(agent_name);
    let reference = a.effective_model(session);
    // Until the catalog reports Core's default, the model is not known yet.
    let (provider, model_name, model_id) = if reference.is_empty() {
        (String::new(), cx.ell().to_owned(), String::new())
    } else {
        model_parts(a, &reference)
    };
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
    let effort_owned = a.current_effort();
    let effort = if effort_owned.is_empty() {
        "default"
    } else {
        effort_owned.as_str()
    };
    // Right block: `continue`, else the meter and the context
    // figure (`0 · 0%` before anything is sent).
    let (summary, fraction) = usage.as_ref().map_or_else(
        || {
            let window = a
                .state
                .models
                .iter()
                .find(|m| m["ref"].as_str() == Some(a.effective_model(session).as_str()))
                .and_then(|m| m["context"].as_u64())
                .filter(|&n| n > 0);
            match window {
                Some(_) => (format!("0{}0%", cx.sep()), Some(0.0)),
                None => ("0".to_owned(), None),
            }
        },
        |u| (u.summary.clone(), u.fraction),
    );
    let action = if interrupted && !busy {
        Some(("continue", Target::Continue))
    } else {
        None
    };
    let mut meter_on = false;
    let right_width = |meter_on: bool| match action {
        Some((label, _)) => width(label),
        None => {
            width(&summary)
                + if meter_on {
                    METER_CELLS as usize + 2
                } else {
                    0
                }
        }
    };
    loop {
        let left_width = width(&agent_label)
            + 1
            + width(&model_label)
            + if effort_on { 1 + width(effort) } else { 0 };
        if left_width + 2 + right_width(meter_on) <= avail {
            break;
        }
        if effort_on {
            effort_on = false;
        } else if provider_on && pending.is_none() && !model_id.is_empty() {
            provider_on = false;
            model_label = model_name.clone();
        } else if meter_on {
            meter_on = false;
        } else {
            // Last resort: keep the agent and the figure, shorten the model.
            let room = avail.saturating_sub(width(&agent_label) + 1 + 2 + right_width(false));
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
            if width(&agent_label) + 1 + width(&model_label) + 2 + right_width(false) > avail {
                let room = avail.saturating_sub(width(&model_label) + 1 + 2 + right_width(false));
                agent_label = truncate(&agent_label, room.max(1), cx.ell());
            }
            break;
        }
    }
    let end = panel.right().saturating_sub(3);
    let rw = right_width(meter_on) as u16;
    let right_x = end.saturating_sub(rw);
    let mut context_rect = None;
    if let Some((label, target)) = action {
        put(buf, right_x, cy, label, cx.st(MUTED, PANEL), rw);
        hits.add(Rect::new(right_x, cy, rw, 1), target);
    } else {
        let mut x = right_x;
        if let (true, Some(fraction)) = (meter_on, fraction) {
            let filled = cx.st(PRIMARY, PANEL).patch(cx.t.agent_style(agent_name));
            crate::paint::meter(
                cx.t,
                buf,
                (x, cy),
                METER_CELLS,
                fraction,
                filled,
                cx.st("border.default", PANEL),
            );
            x += METER_CELLS + 2;
        }
        put(
            buf,
            x,
            cy,
            &summary,
            cx.st(SECONDARY, PANEL),
            end.saturating_sub(x),
        );
        if usage.is_some() {
            let rect = Rect::new(right_x, cy, rw, 1);
            context_rect = Some(rect);
            hits.add(rect, Target::ComposerContext);
        }
    }
    let selector_end = right_x.saturating_sub(2).max(panel.x + 3);
    let mut x = panel.x + 3;
    {
        let fields: Vec<(String, Target, Modifier, &str)> = vec![
            (
                agent_label,
                Target::ComposerAgent,
                Modifier::empty(),
                PRIMARY,
            ),
            (
                model_label,
                Target::ComposerModel,
                Modifier::empty(),
                PRIMARY,
            ),
            (
                effort.to_owned(),
                Target::ComposerEffort,
                Modifier::empty(),
                SECONDARY,
            ),
        ];
        let mut first = true;
        for (index, (label, target, modifier, token)) in fields.into_iter().enumerate() {
            if index == 2 && !effort_on {
                continue;
            }
            if x >= selector_end {
                break;
            }
            if !first {
                x = put(buf, x, cy, " ", cx.st(MUTED, PANEL), selector_end - x);
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
                cx.st(token, PANEL)
                    .patch(cx.t.agent_style(agent_name))
                    .add_modifier(modifier)
            } else {
                cx.st(token, PANEL).add_modifier(modifier)
            };
            x = put(buf, x, cy, &label, style, room);
            if index == 1 && provider_on && pending.is_none() && !model_id.is_empty() {
                // The provider trails the model name, muted.
                let name_end = start + width(&model_name).min((x - start) as usize) as u16;
                for px in name_end..x {
                    buf[(px, cy)].set_fg(cx.t.fg(MUTED).fg.unwrap_or_default());
                }
            }
            if x > start {
                hits.add(Rect::new(start, cy, x - start, 1), target);
            }
        }
    }
    draw_strip(cx, buf, r, hits);
    if busy {
        draw_working(
            cx,
            buf,
            r.x + 2,
            r.bottom() - 1,
            panel.right().saturating_sub(3),
        );
    }
    if let (Some(usage), Some(rect)) = (&usage, context_rect) {
        if cx.hovered(rect) {
            draw_usage_tooltip(
                cx,
                buf,
                Rect::new(r.x, r.y + 1, r.width, r.height - 1),
                usage,
            );
        }
    }
}

/// The old Nexus bottom rule shows context occupancy at rest and activity while running.
fn draw_activity_line(cx: &Cx, buf: &mut Buffer, r: Rect) {
    let start = r.x + 2;
    let width = r.width.saturating_sub(4);
    let busy = cx.app.viewed_id().is_some_and(|id| cx.app.is_busy(id));
    let usage = cx
        .app
        .viewed()
        .and_then(|session| composer_usage(cx, session));
    let fill = usage
        .and_then(|usage| usage.fraction)
        .unwrap_or(0.0)
        .clamp(0.0, 1.0);
    let length = (width as usize / 5).max(1);
    let travel = (width as usize).saturating_sub(length);
    let tick = if cx.t.opts.reduced_motion {
        45
    } else {
        cx.app.frame % 90
    };
    let phase = tick as f64 / 90.0;
    // Fractional head position with a soft-edged (smoothstep) pulse, so the
    // sweep glides between cells instead of stepping a whole cell per frame.
    let pos = travel as f64 * (1.0 - (phase * std::f64::consts::TAU).cos()) / 2.0;
    let accent = cx
        .st(PRIMARY, CANVAS)
        .patch(cx.t.agent_style(&cx.app.current_agent()));
    let rest = cx.st("border.default", CANVAS);
    let soft = !cx.t.mono();
    let half = length as f64 / 2.0;
    let centre = pos + half;
    for i in 0..width as usize {
        let weight = if busy {
            let near = (1.0 - ((i as f64 + 0.5 - centre).abs() / (half + 1.5))).clamp(0.0, 1.0);
            if soft {
                near * near * (3.0 - 2.0 * near)
            } else if near > 0.0 && (i as f64 + 0.5 - centre).abs() <= half {
                1.0
            } else {
                0.0
            }
        } else if (i as f64) < fill * width as f64 {
            1.0
        } else {
            0.0
        };
        let active = weight > 0.5;
        let glyph = if cx.t.opts.ascii {
            if active && cx.t.mono() {
                "="
            } else {
                "-"
            }
        } else if active && cx.t.mono() {
            "━"
        } else {
            "─"
        };
        put(
            buf,
            start + i as u16,
            r.bottom() - 1,
            glyph,
            match (rest.fg, accent.fg) {
                (Some(Color::Rgb(r0, g0, b0)), Some(Color::Rgb(r1, g1, b1))) if busy => {
                    let mix = |a: u8, b: u8| {
                        (f64::from(a) + (f64::from(b) - f64::from(a)) * weight).round() as u8
                    };
                    accent.fg(Color::Rgb(mix(r0, r1), mix(g0, g1), mix(b0, b1)))
                }
                _ if active => accent,
                _ => rest,
            },
            1,
        );
    }
}

/// Cells in the context meter (DESIGN_PLAN.md D11).
const METER_CELLS: u16 = 10;

/// The location strip under the card: path, branch, and a muted icon only
/// for a linked worktree (DESIGN_PLAN.md screenshot refinements).
fn draw_strip(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    let a = cx.app;
    let project = a.current_project();
    let directory = a.viewed().map(|s| s.directory.clone()).or_else(|| {
        project
            .as_ref()
            .and_then(|p| a.state.model.projects.get(p).map(|p| p.path.clone()))
    });
    let Some(directory) = directory else {
        return;
    };
    let listing = a
        .viewed()
        .and_then(|s| a.state.worktrees.get(&s.id))
        .or_else(|| {
            project
                .as_ref()
                .and_then(|p| a.state.project_worktrees.get(p))
        });
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
        .filter(|b| !b.is_empty())
        .map(|b| truncate(b, 24, cx.ell()));
    let linked = worktree.is_some_and(|tree| tree["main"].as_bool() == Some(false));
    let y = r.bottom() - 1;
    let start = r.x + 2;
    let end = r.right().saturating_sub(2);
    let icon = if cx.t.opts.ascii { "*" } else { "⑂" };
    let reserved = branch.as_ref().map_or(0, |b| width(b) + 4) + if linked { 3 } else { 0 };
    let room = (end.saturating_sub(start) as usize).saturating_sub(reserved);
    let mut x = put(
        buf,
        start,
        y,
        &truncate_middle(&path, room, cx.ell()),
        cx.st(MUTED, CANVAS),
        end.saturating_sub(start),
    );
    if a.viewed().is_some() && x > start {
        hits.add(Rect::new(start, y, x - start, 1), Target::ComposerDirectory);
    }
    if let Some(branch) = &branch {
        let begin = x;
        let mark = if cx.t.opts.ascii { "@" } else { "⌥" };
        x = put(
            buf,
            x,
            y,
            &format!("  {mark} {branch}"),
            cx.st(SECONDARY, CANVAS),
            end.saturating_sub(x),
        );
        if x > begin {
            hits.add(Rect::new(begin, y, x - begin, 1), Target::ComposerWorktree);
        }
    }
    if linked {
        put(
            buf,
            x,
            y,
            &format!("  {icon}"),
            cx.st(MUTED, CANVAS),
            end.saturating_sub(x),
        );
    }
}

/// The completion popup: a borderless raised block exactly as wide as the
/// composer card, sitting on its top edge and drawn after it so it covers the
/// timeline (DESIGN_PLAN.md D11). Names are padded to the widest (at most 24)
/// plus three columns, followed by muted help. A thin rail runs down the left
/// edge, like the composer's; the selected row is raised.
fn draw_completion(cx: &Cx, buf: &mut Buffer, g: &Regions, hits: &mut HitMap) {
    let Some(rows) = cx.app.completion_rows() else {
        return;
    };
    let r = g.composer;
    if r.height < 7 || r.width < 8 {
        return;
    }
    // The block ends where the card's top edge row begins.
    let bottom = r.y + 1;
    let room = bottom.saturating_sub(g.centre.y);
    let shown = (rows.len().min(crate::completion::MENU_LIMIT) as u16).min(room.saturating_sub(1));
    if shown == 0 {
        return;
    }
    let height = shown;
    let area = Rect::new(r.x + 2, bottom - height, r.width.saturating_sub(4), height);
    clear(buf, area, cx.st(PRIMARY, PANEL));
    let selected = cx.app.ui.completion.index.min(rows.len() - 1);
    let name_width = rows
        .iter()
        .map(|row| width(&row.value))
        .max()
        .unwrap_or(0)
        .min(24)
        + 3;
    let start = selected.saturating_sub(shown as usize - 1);
    for (offset, (i, row)) in rows
        .iter()
        .enumerate()
        .skip(start)
        .take(shown as usize)
        .enumerate()
    {
        let y = area.y + offset as u16;
        let line = Rect::new(area.x, y, area.width, 1);
        let bg = if i == selected { RAISED } else { PANEL };
        let base = cx.st(PRIMARY, bg);
        clear(buf, line, base);
        put(
            buf,
            area.x,
            y,
            cx.t.structure("rail"),
            cx.st("border.default", bg),
            1,
        );
        let muted = base.patch(cx.t.fg(MUTED));
        let name_style = if row.reason.is_some() {
            muted
        } else if i == selected {
            base.add_modifier(Modifier::BOLD)
        } else {
            base
        };
        let x = put(
            buf,
            area.x + 3,
            y,
            &truncate(&row.value, name_width - 3, cx.ell()),
            name_style,
            area.width.saturating_sub(3),
        );
        let help_x = area.x + 3 + name_width as u16;
        let help = match &row.reason {
            Some(reason) => format!("{}{}{reason}", row.help, cx.sep()),
            None => row.help.clone(),
        };
        let room = area.right().saturating_sub(help_x + 2) as usize;
        if x < help_x && room > 0 {
            put(
                buf,
                help_x,
                y,
                &truncate(&help, room, cx.ell()),
                muted,
                room as u16,
            );
        }
        hits.add(line, Target::CompletionRow(i));
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
        id if id.starts_with("tools.default_") && id != "tools.default_permission" => {
            let name = id.trim_start_matches("tools.default_");
            let disabled = a
                .state
                .config
                .as_ref()
                .and_then(|c| c["values"]["tools"]["disabled"].as_array())
                .is_some_and(|v| v.iter().any(|x| x.as_str() == Some(name)));
            Some(on_off(!disabled))
        }
        "appearance.theme" => Some(match a.theme.opts.mode {
            crate::theme::Mode::Dark => "dark".into(),
            crate::theme::Mode::Light => "light".into(),
        }),
        "appearance.monochrome" => Some(on_off(a.theme.mono())),
        "appearance.animations" => Some(on_off(!a.theme.opts.reduced_motion)),
        "notifications.terminal" => Some(a.ui.notify.label().into()),
        "appearance.sidebar" => Some(
            match (a.ui.left_open, a.ui.left_rail) {
                (false, _) => "hidden",
                (true, true) => "rail",
                (true, false) => "full",
            }
            .into(),
        ),
        "appearance.details" => Some(on_off(a.ui.right_open)),
        "appearance.timeline_detail" => Some(a.ui.detail.label().into()),
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
        "mcp.full_definitions" => a
            .state
            .config
            .as_ref()
            .and_then(|c| c["values"]["mcp"]["full_definitions"].as_bool())
            .map(on_off),
        "mcp.max_listed_tools" => a
            .state
            .config
            .as_ref()
            .and_then(|c| c["values"]["mcp"]["max_listed_tools"].as_u64())
            .map(|value| value.to_string()),
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
        "models.default"
        | "providers.default_model"
        | "models.default_effort"
        | "agents.default_root"
        | "tools.default_permission" => {
            let key = match row.id.as_str() {
                "models.default_effort" => "effort",
                "agents.default_root" => "agent",
                "tools.default_permission" => "permission",
                _ => "model",
            };
            Some(
                a.state
                    .config
                    .as_ref()
                    .and_then(|c| c["values"]["defaults"][key].as_str())
                    .filter(|v| !v.is_empty())
                    .unwrap_or("automatic")
                    .into(),
            )
        }
        "models.catalog" => Some(format!(
            "{} models{}",
            a.state.models.len(),
            if a.state.catalog_source.is_empty() {
                String::new()
            } else {
                format!("{}{}", cx.sep(), a.state.catalog_source)
            }
        )),
        "agents.list" => Some(
            a.state
                .agents
                .iter()
                .filter_map(|agent| agent["name"].as_str())
                .collect::<Vec<_>>()
                .join(", "),
        ),
        "models.refresh" | "providers.sign_in" | "providers.sign_out" => Some("enter".into()),
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

/// Settings (DESIGN_PLAN.md D13): the modal recipe over the scrimmed session.
/// A search row, then every section's rows (UPPERCASE muted headings, values
/// right-aligned, the selected row on the selection band with the focus bar);
/// the selected row's note or reason is the line above the key hints.
fn draw_settings_modal(cx: &Cx, buf: &mut Buffer, area: Rect, hits: &mut HitMap) {
    let a = cx.app;
    hits.add(area, Target::Scrim);
    let r = settings_box(area);
    if r.width < 20 || r.height < 8 {
        return;
    }
    let m = modal(cx, buf, area, r, "Settings", hits);
    let (x, w) = (m.x, m.w);
    let close = Rect::new(r.right().saturating_sub(5), r.y + 1, 3, 1);
    hits.add(close, Target::SettingsEntry);
    let query = a.ui.settings_search.text();
    let sy = m.body.y;
    put(buf, x, sy, "\u{2315}", cx.st(MUTED, RAISED), 1);
    if query.is_empty() {
        put(
            buf,
            x + 2,
            sy,
            "Filter settings\u{2026}",
            cx.st(MUTED, RAISED),
            w,
        );
    } else {
        put(buf, x + 2, sy, &query, cx.st(PRIMARY, RAISED), w);
    }
    for hx in x..x + w {
        put(
            buf,
            hx,
            sy + 1,
            cx.t.structure("rule"),
            cx.st("border.default", RAISED),
            1,
        );
    }

    let items = a.settings_items();
    let rows = a.settings_rows();
    let sel = a.ui.settings_row.min(rows.len().saturating_sub(1));
    let sel_item = rows.get(sel).copied();
    // the note or reason sits on the blank row above the hints
    let note_y = m.hint_y.saturating_sub(1);
    let list = Rect::new(r.x, sy + 2, r.width, note_y.saturating_sub(sy + 2));
    if items.is_empty() {
        put(
            buf,
            x,
            list.y,
            "No settings match.",
            cx.st(MUTED, RAISED),
            w,
        );
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
            crate::app::SettingsItem::Heading(label) => heading(cx, buf, &m, y, label),
            crate::app::SettingsItem::Row {
                row,
                section_reason,
            } => {
                let position = rows.iter().position(|p| p == i).unwrap_or(0);
                let selected = Some(*i) == sel_item;
                let disabled = (row.unavailable_reason.is_some() || section_reason.is_some())
                    && !matches!(row.id.as_str(), "providers.sign_in" | "providers.sign_out");
                let (value, wired) = settings_value(cx, row);
                let rr = Rect::new(r.x + 1, y, r.width.saturating_sub(2), 1);
                let mut style = cx.st(if disabled || !wired { MUTED } else { PRIMARY }, RAISED);
                let mut vstyle = cx.st(if wired && !disabled { SECONDARY } else { MUTED }, RAISED);
                if selected {
                    style = band(
                        cx,
                        buf,
                        &m,
                        y,
                        if disabled || !wired { MUTED } else { PRIMARY },
                    );
                    vstyle = cx
                        .st(if wired && !disabled { SECONDARY } else { MUTED }, RAISED)
                        .add_modifier(Modifier::BOLD);
                }
                let value = if disabled {
                    "unavailable".to_owned()
                } else {
                    value
                };
                let lx = x + 2;
                let lw = w.saturating_sub(2);
                let vw = (width(&value) as u16).min(lw / 2);
                let vx = x + w - vw;
                put(
                    buf,
                    lx,
                    y,
                    &truncate(&row.label, (lw - vw).saturating_sub(2) as usize, cx.ell()),
                    style,
                    (lw - vw).saturating_sub(2),
                );
                put(
                    buf,
                    vx,
                    y,
                    &truncate(&value, vw as usize, cx.ell()),
                    vstyle,
                    vw,
                );
                hits.add(rr, Target::SettingsRow(position));
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
            .filter(|_| !matches!(row.id.as_str(), "providers.sign_in" | "providers.sign_out"))
            .or_else(|| (!App::settings_wired(&row.id)).then(|| "Not available yet.".to_owned()))
            .unwrap_or_else(|| row.note.clone());
        put(
            buf,
            x,
            note_y,
            &truncate(&note, w as usize, cx.ell()),
            cx.st(MUTED, RAISED),
            w,
        );
    }
    let arrows = if cx.t.opts.ascii {
        "</>"
    } else {
        "\u{2190}/\u{2192}"
    };
    hint_row(
        cx,
        buf,
        &m,
        &[(arrows, "change"), ("enter", "select"), ("esc", "close")],
    );
    let sections = items
        .iter()
        .filter(|i| matches!(i, crate::app::SettingsItem::Heading(_)))
        .count();
    let count = format!("{sections} sections");
    put(
        buf,
        r.right().saturating_sub(width(&count) as u16 + 2),
        m.hint_y,
        &count,
        cx.st(MUTED, RAISED),
        width(&count) as u16,
    );
}

/// One modal: the scrim over everything drawn so far, then a borderless
/// `surface.raised` block (DESIGN_PLAN.md D13) with 2 cells of padding left
/// and right and 1 row top and bottom. Inside: the title row (bold, `esc`
/// right-aligned), a blank row, the body, a blank row, the key-hint row.
struct Modal {
    /// The whole block, padding included.
    r: Rect,
    /// Left edge and width of the content inside the padding.
    x: u16,
    w: u16,
    /// Body rows: after the title and its blank row, before the hint row's
    /// blank row.
    body: Rect,
    /// The key-hint row (the last row inside).
    hint_y: u16,
}

impl Modal {
    fn of(r: Rect) -> Self {
        Self {
            r,
            x: r.x + 2,
            w: r.width.saturating_sub(4),
            body: Rect::new(
                r.x + 2,
                r.y + 3,
                r.width.saturating_sub(4),
                r.height.saturating_sub(6),
            ),
            hint_y: r.bottom().saturating_sub(2),
        }
    }
}

fn modal(cx: &Cx, buf: &mut Buffer, area: Rect, r: Rect, title: &str, hits: &mut HitMap) -> Modal {
    paint::scrim(cx.t, buf, area);
    clear(buf, r, cx.st(PRIMARY, RAISED));
    hits.add(r, Target::Swallow);
    let m = Modal::of(r);
    let tw = m.w.saturating_sub(5);
    put(
        buf,
        m.x,
        r.y + 1,
        &truncate(title, tw as usize, cx.ell()),
        cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD),
        tw,
    );
    put(
        buf,
        r.right().saturating_sub(5),
        r.y + 1,
        "esc",
        cx.st(MUTED, RAISED),
        3,
    );
    m
}

/// A selected modal row uses a focus bar and bold text without a fill.
fn band(cx: &Cx, buf: &mut Buffer, m: &Modal, y: u16, token: &str) -> Style {
    let rr = Rect::new(m.r.x + 1, y, m.r.width.saturating_sub(2), 1);
    let style = cx.st(token, RAISED).add_modifier(Modifier::BOLD);
    put(
        buf,
        rr.x,
        y,
        cx.t.structure("focus_bar"),
        cx.st("interactive.focus", RAISED),
        1,
    );
    style
}

/// A section heading inside a modal: UPPERCASE, bold, muted.
fn heading(cx: &Cx, buf: &mut Buffer, m: &Modal, y: u16, label: &str) {
    put(
        buf,
        m.x,
        y,
        &truncate(&label.to_uppercase(), m.w as usize, cx.ell()),
        cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD),
        m.w,
    );
}

/// Key hints on the modal's last row: bold key, muted label, `·` between.
fn hint_row(cx: &Cx, buf: &mut Buffer, m: &Modal, hints: &[(&str, &str)]) -> u16 {
    let muted = cx.st(MUTED, RAISED);
    let key = cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD);
    let mut x = m.x;
    for (i, (k, label)) in hints.iter().enumerate() {
        if i > 0 {
            x = put(
                buf,
                x,
                m.hint_y,
                &cx.sep(),
                muted,
                m.r.right().saturating_sub(x + 2),
            );
        }
        x = put(buf, x, m.hint_y, k, key, m.r.right().saturating_sub(x + 2));
        x = put(
            buf,
            x,
            m.hint_y,
            &format!(" {label}"),
            muted,
            m.r.right().saturating_sub(x + 2),
        );
    }
    x
}

fn updown(cx: &Cx) -> &'static str {
    if cx.t.opts.ascii {
        "up/down"
    } else {
        "\u{2191}\u{2193}"
    }
}

/// The block for a modal of `width` and `height` cells: centred, within 2
/// cells of each edge and at most 70% of the frame high.
fn modal_box(area: Rect, width: u16, height: u16) -> Rect {
    paint::modal_rect(area, width, height, false)
}

pub(crate) fn settings_box(area: Rect) -> Rect {
    modal_box(area, 88, 40)
}

fn palette_box(area: Rect) -> Rect {
    paint::modal_rect(area, 64, 22, true)
}

// ============================================================================
// overlays and dialogs
// ============================================================================

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
        OverlayKind::Palette(p) => {
            let anchor = (p.mode == PaletteMode::RowMenu)
                .then(|| hits.rect_of(&Target::SideRow(cx.app.cursor(cx.app.ui.tab))))
                .flatten();
            match anchor {
                Some(row) => draw_row_menu(cx, buf, area, row, p, hits),
                None => draw_palette(cx, buf, area, p, hits),
            }
        }
        OverlayKind::LearnChecklist(checklist) => {
            draw_learn_checklist(cx, buf, area, checklist, hits)
        }
        OverlayKind::Notifications { selected } => {
            draw_notifications(cx, buf, area, *selected, hits)
        }
        OverlayKind::Help { top } => draw_help(cx, buf, area, *top, hits),
        OverlayKind::ContextDetail { title, text, top } => {
            draw_context_detail(cx, buf, area, title, text, *top, hits)
        }
        OverlayKind::AlertDetail { id, top, .. } => {
            draw_alert_detail(cx, buf, area, *id, *top, hits)
        }
    }
}

/// `/learn`'s candidate checklist. It uses the shared borderless modal and keeps
/// one row per skill, with installed candidates visibly marked for replacement.
fn draw_learn_checklist(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    checklist: &crate::ui::LearnChecklist,
    hits: &mut HitMap,
) {
    let visible = checklist
        .skills
        .len()
        .min(area.height.saturating_sub(12) as usize)
        .max(1);
    let r = modal_box(area, 76, visible.saturating_add(8) as u16);
    if r.width < 24 || r.height < 9 {
        return;
    }
    let title = format!("Choose skills · {}", checklist.source);
    let m = modal(cx, buf, area, r, &title, hits);
    let source = format!("From {}/{}", checklist.scope, checklist.source);
    put(
        buf,
        m.x,
        m.body.y,
        &truncate(&source, m.w as usize, cx.ell()),
        cx.st(MUTED, RAISED),
        m.w,
    );
    let list_y = m.body.y + 1;
    let list_height = m.hint_y.saturating_sub(1).saturating_sub(list_y) as usize;
    let top = checklist
        .cursor
        .saturating_add(1)
        .saturating_sub(list_height.max(1));
    let selected_new = checklist
        .skills
        .iter()
        .zip(&checklist.selected)
        .filter(|(skill, selected)| **selected && !skill.installed)
        .count();
    let replacing = checklist
        .skills
        .iter()
        .zip(&checklist.selected)
        .filter(|(skill, selected)| **selected && skill.installed)
        .count();
    let footer = format!("{selected_new} new, {replacing} will be replaced");
    put(
        buf,
        m.x,
        m.hint_y.saturating_sub(1),
        &truncate(&footer, m.w as usize, cx.ell()),
        cx.st(MUTED, RAISED),
        m.w,
    );
    for (i, skill) in checklist
        .skills
        .iter()
        .enumerate()
        .skip(top)
        .take(list_height)
    {
        let y = list_y + (i - top) as u16;
        let rr = Rect::new(r.x + 1, y, r.width.saturating_sub(2), 1);
        let active = checklist.cursor == i;
        let selected = checklist.selected.get(i).copied().unwrap_or(false);
        let base = if active {
            band(cx, buf, &m, y, PRIMARY)
        } else {
            cx.st(PRIMARY, RAISED)
        };
        let marker = if selected { "[x]" } else { "[ ]" };
        put(buf, m.x, y, marker, base, 3);
        let installed = if skill.installed { "installed" } else { "" };
        let tag_width = width(installed) as u16;
        if tag_width > 0 {
            put(
                buf,
                rr.right().saturating_sub(tag_width + 1),
                y,
                installed,
                cx.st(MUTED, RAISED),
                tag_width,
            );
        }
        let label_x = m.x + 5;
        let label_w = rr
            .right()
            .saturating_sub(label_x + tag_width + u16::from(tag_width > 0));
        let name_w = (width(&skill.name) as u16).min(label_w / 2).max(1);
        put(
            buf,
            label_x,
            y,
            &truncate(&skill.name, name_w as usize, cx.ell()),
            base.add_modifier(Modifier::BOLD),
            name_w,
        );
        let desc_x = label_x + name_w + 1;
        let desc_w = rr
            .right()
            .saturating_sub(desc_x + tag_width + u16::from(tag_width > 0));
        put(
            buf,
            desc_x,
            y,
            &truncate(&skill.description, desc_w as usize, cx.ell()),
            cx.st(MUTED, RAISED),
            desc_w,
        );
        hits.add(rr, Target::LearnSkill(i));
    }
    hint_row(
        cx,
        buf,
        &m,
        &[
            ("↑/↓", "move"),
            ("space", "toggle"),
            ("a", "all"),
            ("n", "none"),
            ("i", "invert"),
            ("enter", "install"),
            ("esc", "cancel"),
        ],
    );
}

/// The `.` row menu: a small raised popover beside the focused sidebar row,
/// one item per row with its key on the right; Delete is in `status.error`.
fn draw_row_menu(cx: &Cx, buf: &mut Buffer, area: Rect, row: Rect, p: &Palette, hits: &mut HitMap) {
    let items = cx.app.palette_items();
    let want = items
        .iter()
        .map(|it| width(&it.label) + width(&it.hint) + 6)
        .max()
        .unwrap_or(0)
        .max(24) as u16;
    let w = want.min(area.width);
    let h = (items.len() as u16).max(1).min(area.height);
    let x = row
        .right()
        .saturating_sub(5)
        .min(area.right().saturating_sub(w));
    let y = (row.y + 1).min(area.bottom().saturating_sub(h));
    let b = Rect::new(x, y, w, h);
    clear(buf, b, cx.st(PRIMARY, RAISED));
    hits.add(b, Target::Swallow);
    if items.is_empty() {
        put(
            buf,
            b.x + 2,
            b.y,
            "Nothing matches.",
            cx.st(MUTED, RAISED),
            w - 2,
        );
    }
    for (i, it) in items.iter().enumerate().take(h as usize) {
        let rr = Rect::new(b.x, b.y + i as u16, w, 1);
        let selected = i == p.selected;
        let danger = matches!(&it.kind, PaletteKind::Command(id) if id.ends_with(".delete"));
        let token = if it.reason.is_some() {
            MUTED
        } else if danger {
            "status.error"
        } else {
            PRIMARY
        };
        let mut style = cx.st(token, RAISED);
        let hint = cx.st(MUTED, RAISED);
        if selected {
            style = style.add_modifier(Modifier::BOLD);
            put(
                buf,
                rr.x,
                rr.y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", RAISED),
                1,
            );
        }
        let hw = width(&it.hint) as u16;
        put(
            buf,
            rr.right().saturating_sub(hw + 2),
            rr.y,
            &it.hint,
            hint,
            hw,
        );
        let room = w.saturating_sub(hw + 6) as usize;
        put(
            buf,
            rr.x + 2,
            rr.y,
            &truncate(&it.label, room, cx.ell()),
            style,
            room as u16,
        );
        hits.add(rr, Target::PaletteItem(i));
    }
}

fn draw_palette(cx: &Cx, buf: &mut Buffer, area: Rect, p: &Palette, hits: &mut HitMap) {
    let mut b = palette_box(area);
    if p.mode == PaletteMode::Models && p.model_info && area.height >= 12 {
        b.height = b.height.min(area.height.saturating_sub(6));
        b.y = area.y + area.height.saturating_sub(b.height + 5) / 2;
    }
    let title = match p.mode {
        PaletteMode::Commands => "Command palette",
        PaletteMode::ProviderLogin => "Sign in to a provider",
        PaletteMode::ProviderLogout => "Sign out of a provider",
        PaletteMode::Sessions if cx.app.ui.scope_all => "Search sessions · Global",
        PaletteMode::Sessions => "Search sessions · Project",
        PaletteMode::Projects => "Switch project",
        PaletteMode::Models => "Change model",
        PaletteMode::Agents => "Change agent",
        PaletteMode::Efforts => "Reasoning effort",
        PaletteMode::Export => "Export session",
        PaletteMode::LearnScope => "Install skills for",
        PaletteMode::Turns => "Go to turn",
        PaletteMode::RowMenu => "Actions",
    };
    let m = modal(cx, buf, area, b, title, hits);
    let base = cx.st(PRIMARY, RAISED);
    // search row: the glyph, then the query or its placeholder
    let sy = m.body.y;
    put(buf, m.x, sy, "\u{2315}", cx.st(MUTED, RAISED), 1);
    let q = p.query.text();
    if q.is_empty() {
        let ph = match p.mode {
            PaletteMode::Commands => "type to filter commands",
            PaletteMode::ProviderLogin | PaletteMode::ProviderLogout => "choose a provider",
            PaletteMode::Sessions => "type to find a session",
            PaletteMode::Projects => "type to find a project",
            PaletteMode::Models => "type to filter models",
            PaletteMode::Agents => "type to filter agents",
            PaletteMode::Efforts => "type to filter efforts",
            PaletteMode::Export => "choose a format and scope",
            PaletteMode::LearnScope => "choose where the skills go",
            PaletteMode::Turns => "type to find a turn",
            PaletteMode::RowMenu => "choose an action",
        };
        put(
            buf,
            m.x + 2,
            sy,
            ph,
            cx.st(MUTED, RAISED),
            m.w.saturating_sub(2),
        );
    } else {
        put(
            buf,
            m.x + 2,
            sy,
            &q.replace('\n', " "),
            base,
            m.w.saturating_sub(2),
        );
    }
    for x in m.x..m.x + m.w {
        put(
            buf,
            x,
            sy + 1,
            cx.t.structure("rule"),
            cx.st("border.default", RAISED),
            1,
        );
    }
    let items = cx.app.palette_items();
    let list = Rect::new(
        b.x + 1,
        sy + 2,
        b.width - 2,
        m.body.height.saturating_sub(2),
    );
    if items.is_empty() {
        put(
            buf,
            m.x + 2,
            list.y,
            "Nothing matches.",
            cx.st(MUTED, RAISED),
            m.w.saturating_sub(2),
        );
    }
    let h = list.height as usize;
    // Display rows: the command palette, unfiltered, is grouped (D13) with an
    // UPPERCASE muted heading before each group and a blank row between them.
    let grouped = p.mode == PaletteMode::Commands && q.is_empty();
    let mut rows: Vec<(Option<String>, usize)> = Vec::new();
    let mut last: Option<String> = None;
    for (i, it) in items.iter().enumerate() {
        if grouped {
            let g = paint::palette_group(&it.category);
            if last.as_ref() != Some(&g) {
                if last.is_some() {
                    rows.push((None, usize::MAX));
                }
                rows.push((Some(g.clone()), usize::MAX));
                last = Some(g);
            }
        }
        rows.push((None, i));
    }
    let at = rows.iter().position(|r| r.1 == p.selected).unwrap_or(0);
    let top = if at < h { 0 } else { at + 1 - h };
    for (di, (head, i)) in rows.iter().enumerate().skip(top).take(h) {
        let (head, i) = (head, *i);
        let y = list.y + (di - top) as u16;
        if i == usize::MAX {
            if let Some(g) = head {
                heading(cx, buf, &m, y, g);
            }
            continue;
        }
        let it = &items[i];
        let rr = Rect::new(list.x, y, list.width, 1);
        let selected = i == p.selected;
        let token = if it.reason.is_some() { MUTED } else { PRIMARY };
        let mut style = cx.st(token, RAISED);
        let mut quiet = cx.st(MUTED, RAISED);
        if selected {
            style = band(cx, buf, &m, y, token);
            quiet = cx.st(MUTED, RAISED);
        } else if cx.hovered(rr) {
            let hover = Style::default();
            style = style.patch(hover);
            quiet = quiet.patch(hover);
            clear(buf, rr, style);
        }
        let hw = width(&it.hint) as u16;
        let hx = m.x + m.w - hw;
        if hw > 0 {
            put(buf, hx, y, &it.hint, quiet, hw);
        }
        let room = hx.saturating_sub(m.x + 2 + 1) as usize;
        let mut label_style = style;
        if let PaletteKind::Agent(name) = &it.kind {
            label_style = label_style.patch(cx.t.agent_style(name));
        }
        let x1 = put(
            buf,
            m.x + 2,
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
                    quiet,
                    rrm as u16,
                );
            }
        }
        hits.add(rr, Target::PaletteItem(i));
    }
    let explain = items.get(p.selected).is_some_and(|i| i.reason.is_some());
    if p.mode == PaletteMode::Models && p.model_info && b.bottom() + 4 <= area.bottom() {
        let panel = Rect::new(b.x, b.bottom(), b.width, 4);
        clear(buf, panel, cx.st(PRIMARY, RAISED));
        if let Some(PaletteItem {
            kind: PaletteKind::Model(reference),
            ..
        }) = items.get(p.selected)
        {
            if let Some(model) = cx
                .app
                .state
                .models
                .iter()
                .find(|m| m["ref"].as_str() == Some(reference.as_str()))
            {
                let context = model["context"]
                    .as_u64()
                    .map(|n| format!("{n} tokens"))
                    .unwrap_or_else(|| "unknown".into());
                let efforts = model["efforts"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str())
                            .collect::<Vec<_>>()
                            .join(" / ")
                    })
                    .filter(|s| !s.is_empty())
                    .unwrap_or_else(|| "none listed".into());
                put(
                    buf,
                    panel.x + 2,
                    panel.y,
                    &truncate(reference, panel.width.saturating_sub(4) as usize, cx.ell()),
                    cx.st(PRIMARY, RAISED),
                    panel.width.saturating_sub(4),
                );
                put(
                    buf,
                    panel.x + 2,
                    panel.y + 1,
                    &format!("Context: {context}"),
                    cx.st(MUTED, RAISED),
                    panel.width.saturating_sub(4),
                );
                put(
                    buf,
                    panel.x + 2,
                    panel.y + 2,
                    &format!("Efforts: {efforts}"),
                    cx.st(MUTED, RAISED),
                    panel.width.saturating_sub(4),
                );
            }
        }
    }
    let mut hints = vec![];
    if p.mode == PaletteMode::Models {
        hints.push(("m", "info"));
    }
    if p.mode == PaletteMode::Sessions {
        hints.push(("tab", "scope"));
    }
    hints.extend_from_slice(&[
        (updown(cx), "move"),
        (
            "enter",
            if explain {
                "explain why it is off"
            } else {
                "run"
            },
        ),
        ("esc", "close"),
    ]);
    hint_row(cx, buf, &m, &hints);
    let count = format!(
        "{} of {}",
        if items.is_empty() { 0 } else { p.selected + 1 },
        items.len()
    );
    put(
        buf,
        m.x + m.w - width(&count) as u16,
        m.hint_y,
        &count,
        cx.st(MUTED, RAISED),
        width(&count) as u16,
    );
}

/// The notification history: the modal recipe, one entry per two rows at most.
fn draw_notifications(cx: &Cx, buf: &mut Buffer, area: Rect, selected: usize, hits: &mut HitMap) {
    let b = modal_box(area, 64, 30);
    let m = modal(cx, buf, area, b, "Notifications", hits);
    hint_row(
        cx,
        buf,
        &m,
        &[(updown(cx), "move"), ("enter", "open"), ("esc", "close")],
    );
    let hist = &cx.app.ui.alerts.history;
    if hist.is_empty() {
        put(
            buf,
            m.x,
            m.body.y,
            "Nothing yet. Toasts and alerts are kept here.",
            cx.st(MUTED, RAISED),
            m.w,
        );
        return;
    }
    let mut y = m.body.y;
    for (i, n) in hist.iter().enumerate() {
        let rows = text::wrap(&n.text, m.w.saturating_sub(3) as usize);
        let h = rows.len().min(2) as u16;
        if y + h > m.body.bottom() {
            break;
        }
        let rr = Rect::new(b.x + 1, y, b.width - 2, h);
        let mut style = cx.st(PRIMARY, RAISED);
        let mut bg = Style::default();
        if i == selected {
            band(cx, buf, &m, y, PRIMARY);
            style = cx.st(PRIMARY, RAISED);
            bg = Style::default();
        }
        let (g, tok) = cx.level(n.level);
        put(buf, m.x, y, g, cx.st(tok, RAISED).patch(bg), 2);
        for (j, l) in rows.iter().take(2).enumerate() {
            put(buf, m.x + 3, y + j as u16, l, style, m.w.saturating_sub(3));
        }
        hits.add(rr, Target::NoticeItem(i));
        y += h;
    }
}

/// The full text of one alert: the modal recipe, the title from the alert,
/// the whole text wrapped and scrollable (D15).
fn context_detail_layout(area: Rect, text: &str) -> (Rect, Vec<String>) {
    let wide = (area.width as usize * 6 / 10).clamp(48, 92) as u16;
    let actual_width = modal_box(area, wide, 10).width.saturating_sub(4) as usize;
    let rows: Vec<String> = text
        .split('\n')
        .flat_map(|line| {
            if line.is_empty() {
                vec![String::new()]
            } else {
                text::wrap(line, actual_width.max(1))
            }
        })
        .collect();
    (
        modal_box(area, wide, (rows.len() + 6).clamp(10, 40) as u16),
        rows,
    )
}

pub(crate) fn context_detail_max(area: Rect, text: &str) -> usize {
    let (rect, rows) = context_detail_layout(area, text);
    rows.len()
        .saturating_sub(Modal::of(rect).body.height as usize)
}

fn draw_context_detail(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    title: &str,
    text: &str,
    top: usize,
    hits: &mut HitMap,
) {
    let (b, rows) = context_detail_layout(area, text);
    let m = modal(cx, buf, area, b, title, hits);
    let h = m.body.height as usize;
    let top = top.min(rows.len().saturating_sub(h));
    for (i, row) in rows.iter().skip(top).take(h).enumerate() {
        put(
            buf,
            m.x,
            m.body.y + i as u16,
            row,
            cx.st(PRIMARY, RAISED),
            m.w,
        );
    }
    hint_row(cx, buf, &m, &[(updown(cx), "scroll"), ("esc", "close")]);
}

fn draw_alert_detail(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    id: u64,
    top: usize,
    hits: &mut HitMap,
) {
    let Some((level, full)) = cx.app.ui.alerts.full(id) else {
        return;
    };
    let (title, _) = crate::ui::split_title(full);
    let wide = (area.width as usize * 6 / 10).clamp(48, 92) as u16;
    let inner = wide.saturating_sub(4) as usize;
    // The body repeats the title only when the text is one line.
    let text = match full.split_once('\n') {
        Some((_, rest)) => rest.trim().to_owned(),
        None => full.to_owned(),
    };
    let mut rows: Vec<String> = Vec::new();
    for para in text.split('\n') {
        if para.trim().is_empty() {
            rows.push(String::new());
        } else {
            rows.extend(text::wrap(para, inner));
        }
    }
    let want = (rows.len() + 6).clamp(10, 40) as u16;
    let b = modal_box(area, wide, want);
    let (glyph, tok) = cx.level(level);
    let m = modal(cx, buf, area, b, &format!("{glyph} {title}"), hits);
    let _ = tok;
    let h = m.body.height as usize;
    let top = top.min(rows.len().saturating_sub(h));
    for (i, row) in rows.iter().skip(top).take(h).enumerate() {
        put(
            buf,
            m.x,
            m.body.y + i as u16,
            row,
            cx.st(PRIMARY, RAISED),
            m.w,
        );
    }
    let has_session = cx
        .app
        .ui
        .alerts
        .history
        .iter()
        .any(|n| n.id == id && n.session.is_some());
    let mut hints = vec![("c", "copy")];
    if has_session {
        hints.push(("enter", "go to session"));
    }
    if rows.len() > h {
        hints.push((updown(cx), "scroll"));
    }
    hints.push(("esc", "close"));
    hint_row(cx, buf, &m, &hints);
    if rows.len() > h {
        let more = format!(
            "{}-{} of {}",
            top + 1,
            (top + h).min(rows.len()),
            rows.len()
        );
        put(
            buf,
            m.x + m.w - width(&more) as u16,
            m.hint_y,
            &more,
            cx.st(MUTED, RAISED),
            width(&more) as u16,
        );
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
    let m = modal(cx, buf, area, b, &title, hits);
    let mut rows: Vec<(String, String)> = cx
        .app
        .manifest
        .command
        .iter()
        .filter(|c| c.for_tui() && c.tui_key().is_some())
        .map(|c| (c.tui_key().unwrap_or("").to_owned(), c.title.clone()))
        .collect();
    rows.extend([
        ("ctrl+o / ctrl+s".into(), "Switch session".into()),
        ("ctrl+l".into(), "Toggle right sidebar".into()),
        ("ctrl+t".into(), "Cycle effort".into()),
        (
            "ctrl+x m/n/o/g".into(),
            "Model, new, sessions, agent".into(),
        ),
        (
            "ctrl+x s/l/t/?".into(),
            "Settings, sidebar, effort, shortcuts".into(),
        ),
        ("ctrl+x shift+n".into(), "Notifications".into()),
        (
            "alt+left/right".into(),
            "Move by word; Shift selects".into(),
        ),
    ]);
    rows.push(("f6 / shift+f6".into(), "Cycle focus between panes".into()));
    rows.push((
        "ctrl+j".into(),
        "Insert newline (where shift+enter is not reported)".into(),
    ));
    rows.push(("ctrl+z".into(), "Suspend to the shell".into()));
    rows.push(("ctrl+c".into(), "Quit".into()));
    let h = m.body.height as usize;
    let top = top.min(rows.len().saturating_sub(h));
    for (i, (k, t)) in rows.iter().skip(top).take(h).enumerate() {
        let y = m.body.y + i as u16;
        put(
            buf,
            m.x,
            y,
            k,
            cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD),
            16,
        );
        put(
            buf,
            m.x + 17,
            y,
            &truncate(t, m.w.saturating_sub(17) as usize, cx.ell()),
            cx.st(PRIMARY, RAISED),
            m.w.saturating_sub(17),
        );
    }
    hint_row(cx, buf, &m, &[(updown(cx), "scroll"), ("esc", "close")]);
}

type Buttons<'a> = Option<(&'a str, &'a str, bool, usize)>;

/// A confirm or blocking dialog: title, body, and `[ Cancel ]` (the safe
/// default, on the selection band when focused) beside the confirm button
/// (`[ Delete ]` in `status.error` when `danger`).
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
    let w = area.width.saturating_sub(4).clamp(20, 60);
    let inner = w.saturating_sub(4) as usize;
    let rows: Vec<String> = body.iter().flat_map(|l| text::wrap(l, inner)).collect();
    // padding 2 + title + blank + body + blank + buttons + blank + hints
    let extra = if buttons.is_some() { 8 } else { 6 };
    let want = (rows.len() as u16 + extra).max(8);
    let b = modal_box(area, w, want.min(area.height.saturating_sub(4).max(8)));
    let b = if want > b.height {
        // a long body: take the room it needs (up to 70% was already tried)
        let h = want.min(area.height);
        Rect::new(b.x, area.y + area.height.saturating_sub(h) / 2, b.width, h)
    } else {
        b
    };
    let m = modal(cx, buf, area, b, title, hits);
    for (i, l) in rows.iter().enumerate() {
        if i as u16
            >= m.body
                .height
                .saturating_sub(if buttons.is_some() { 2 } else { 0 })
        {
            break;
        }
        put(
            buf,
            m.x,
            m.body.y + i as u16,
            l,
            cx.st(PRIMARY, RAISED),
            m.w,
        );
    }
    match buttons {
        Some((cancel, confirm, danger, focus)) => {
            let by = m.body.bottom().saturating_sub(1);
            let labels = [(cancel, false), (confirm, danger)];
            let total: u16 = labels
                .iter()
                .map(|(l, _)| width(l) as u16 + 4 + 2)
                .sum::<u16>()
                .saturating_sub(2);
            // right-aligned on the inner edge; each button is `[ label ]`
            let mut x = m.x + m.w.saturating_sub(total);
            for (i, (label, is_danger)) in labels.into_iter().enumerate() {
                let token = if is_danger { "status.error" } else { PRIMARY };
                let s = format!("[ {label} ]");
                let sw = width(&s) as u16;
                let style = if focus == i {
                    let mut st = cx.st(token, RAISED).add_modifier(Modifier::BOLD);
                    if cx.t.mono() {
                        st = st.add_modifier(Modifier::REVERSED);
                    }
                    put(
                        buf,
                        x.saturating_sub(1),
                        by,
                        cx.t.structure("focus_bar"),
                        cx.st("interactive.focus", RAISED),
                        1,
                    );
                    st
                } else {
                    cx.st(token, RAISED)
                };
                let x1 = put(buf, x, by, &s, style, sw);
                hits.add(Rect::new(x, by, x1 - x, 1), Target::DialogButton(i));
                x = x1 + 2;
            }
            let lr = if cx.t.opts.ascii {
                "</>"
            } else {
                "\u{2190}\u{2192}"
            };
            hint_row(
                cx,
                buf,
                &m,
                &[(lr, "choose"), ("enter", "confirm"), ("esc", "cancel")],
            );
        }
        None => {
            // Esc does nothing on a blocking dialog: drop the title's `esc`
            put(
                buf,
                b.right().saturating_sub(5),
                b.y + 1,
                "   ",
                cx.st(MUTED, RAISED),
                3,
            );
            hint_row(cx, buf, &m, &[("ctrl+q", "quit")]);
        }
    }
}

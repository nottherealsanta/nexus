//! Ratatui drawing of the shell. Everything clickable is registered in the
//! `HitMap` at the moment it is drawn, from the same rect (see `layout`).
//! Colours and glyphs come from `theme`; labels, keys and reasons from the
//! manifest. The look follows INTERFACE_PLAN.md "Design language"; the old
//! Nexus TUI (`rust/tui/src/render/`) is the visual reference.

use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Line;
use ratatui::text::Span;
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
const MULTI: &str = "interactive.multi";
/// The active sidebar block (selected project tile, selected session row) and its left bar.
const ACTIVE: &str = "interactive.active";
const ACTIVE_BAR: &str = "interactive.active_bar";
const HIGHLIGHT: &str = "text.highlight";

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
    render_frame(frame, app);
    // Every way out of the modal ends here: its pictures and pixel mouse go too.
    let annotating = matches!(
        app.ui.overlay,
        Some(Overlay {
            kind: OverlayKind::Annotate(_),
            ..
        })
    );
    if !annotating {
        app.annotate_live.borrow_mut().teardown();
    }
}

fn render_frame(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    app.size = (area.width, area.height);
    let now = std::time::Instant::now();
    observe_splash(app, now);
    app.manager_frame(now);
    if matches!(
        app.ui.splash.phase(app.ui.splash.frame(now), area.width),
        crate::splash::Phase::Loading | crate::splash::Phase::Flash
    ) {
        // Core is starting or connecting: the splash alone, no chrome.
        app.hits = HitMap::default();
        app.timeline_screen = None;
        app.timeline_drag = None;
        let clear_style = app.theme.style(PRIMARY, CANVAS);
        let palette = splash_palette(app);
        let splash_frame = app.ui.splash.frame(now);
        let buf = frame.buffer_mut();
        clear(buf, area, clear_style);
        app.ui.splash.render(buf, area, splash_frame, &palette);
        return;
    }
    let flags = Flags {
        left_open: app.ui.left_open,
        right_open: app.ui.right_open,
        drawer: app.ui.drawer,
        left_width: app.ui.left_width,
        panel_open: app.terminal_open || app.ui.file_open || app.ui.diff_open.is_some(),
    };
    let regions = {
        let a: &App = app;
        Regions::compute(area, flags, |w| heights(a, w))
    };
    let mut hits = HitMap::default();
    let mut timeline_screen = None;
    let mut metrics = (0usize, 0usize, None::<usize>);
    let timeline = {
        let a: &App = app;
        Some(a.timeline_for(regions.timeline.width.saturating_sub(4) as usize))
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
                Rect::new(
                    l.x + u16::from(regions.rail.is_some()),
                    l.y,
                    l.width
                        .saturating_sub(1 + u16::from(regions.rail.is_some())),
                    l.height,
                ),
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
        if let Some(rail) = regions.rail {
            draw_rail(&cx, buf, rail, &mut hits);
            if let Some(left) = regions.left {
                for y in left.y..left.bottom() {
                    put(
                        buf,
                        left.x,
                        y,
                        cx.t.structure("rail"),
                        cx.st("border.default", SIDEBAR),
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
        draw_heading(&cx, buf, regions.heading);
        draw_subagents(&cx, buf, regions.strip, &mut hits);
        draw_banner(&cx, buf, regions.banner, &mut hits);
        // The session stays drawn under the settings modal and under a narrow panel.
        if let Some(t) = &timeline {
            metrics = draw_timeline(&cx, buf, regions.timeline, &mut hits, t);
            let mut screen = Buffer::empty(regions.timeline);
            for y in regions.timeline.y..regions.timeline.bottom() {
                for x in regions.timeline.x..regions.timeline.right() {
                    screen[(x, y)] = buf[(x, y)].clone();
                }
            }
            timeline_screen = Some(screen);
            if let Some((selection, _)) = &a.timeline_drag {
                if selection.dragged() && selection.area == regions.timeline {
                    selection.highlight(buf);
                }
            }
        }
        draw_waiting(&cx, buf, regions.waiting, &mut hits);
        draw_composer(&cx, buf, regions.composer, &mut hits);
        if let Some(behind) = metrics.2 {
            draw_jump(&cx, buf, regions.composer, behind, &mut hits);
        }
        draw_completion(&cx, buf, &regions, &mut hits);
        if let Some(rail) = regions.rail {
            // Beside the rail, over the sidebar when it is open.
            let limit = Rect::new(
                rail.right(),
                rail.y,
                regions.centre.right().saturating_sub(rail.right()),
                rail.height,
            );
            draw_rail_popup(&cx, buf, rail, limit);
        }
        if a.ui.view == View::Settings {
            draw_settings(&cx, buf, area, &mut hits);
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
        if let Some(p) = regions.panel {
            // A modal: the page dims, and a click outside it closes it.
            paint::scrim(cx.t, buf, area);
            hits.add(area, Target::PanelBack);
            draw_panel(&cx, buf, p, area, &mut hits);
        }
        if let Some(e) = &a.ui.edit {
            let title = match &e.target {
                crate::ui::EditTarget::RenameSession { .. } => "Rename session",
                _ => &e.label,
            };
            let m = modal(
                &cx,
                buf,
                area,
                modal_box(area, MODAL_SMALL, 8),
                title,
                &mut hits,
            );
            let (rows, _) = e.buf.layout(m.w.max(1) as usize);
            let masked = "•".repeat(e.buf.text().chars().count());
            put(
                buf,
                m.x,
                m.body.y,
                if matches!(e.target, crate::ui::EditTarget::ProviderKey { .. }) {
                    &masked
                } else {
                    rows.first().map(String::as_str).unwrap_or("")
                },
                cx.st(PRIMARY, PANEL).add_modifier(Modifier::UNDERLINED),
                m.w,
            );
            hint_row(&cx, buf, &m, &[("enter", "save"), ("esc", "cancel")]);
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
        // The Manager's indicator, caption and card are painted over everything else.
        crate::stage::draw(a, buf, area, regions.composer, regions.manager, &mut hits);
        // The Manager's debug context is a modal above even the call stage.
        if let Some(context) = &a.ui.manager.context {
            draw_context_detail(
                &cx,
                buf,
                area,
                &context.title,
                &context.text,
                context.top,
                &mut hits,
            );
        }
    }
    // The splash wipes into the first live frame: cells right of the curtain
    // still show the splash.
    if app.ui.splash.phase(app.ui.splash.frame(now), area.width) == crate::splash::Phase::Curtain {
        let palette = splash_palette(app);
        app.ui.splash.overlay(frame.buffer_mut(), now, &palette);
    }
    // The terminal cursor shows where typing goes.
    if let Some(p) = cursor_position(app, &regions) {
        frame.set_cursor_position(p);
    }
    app.hits = hits;
    app.timeline_screen = timeline_screen;
    app.regions = regions;
    app.ui.timeline_total = metrics.0;
    app.ui.timeline_height = metrics.1;
    if let Some(t) = &timeline {
        app.ui.timeline_blocks = t.blocks.clone();
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
    // The first-send pin holds the scroll at the first message until the reader moves it.
    let pin_top = timeline.as_ref().and_then(|t| app.first_pin_top(&t.blocks));
    if let Some(top) = pin_top {
        let held = Scroll { follow: false, top };
        let applied = app.ui.first_pin.as_ref().and_then(|p| p.applied);
        match applied {
            Some(was) if was != app.ui.scroll => app.ui.first_pin = None,
            _ => {
                app.ui.scroll = held;
                if let Some(pin) = app.ui.first_pin.as_mut() {
                    pin.applied = Some(held);
                }
            }
        }
    } else if app.ui.first_pin.is_some() && timeline.is_some() {
        app.ui.first_pin = None;
    }
    // keep the stored scroll honest after clamping
    if pin_top.is_none() && !app.ui.scroll.follow {
        let max_top = metrics.0.saturating_sub(metrics.1);
        if app.ui.scroll.top >= max_top {
            app.ui.scroll = Scroll::default();
        }
    }
}

/// Feed the splash the milestones the client can observe. Time never moves the
/// bar; a failed start stops covering the screen so the error can be read.
fn observe_splash(app: &mut App, now: std::time::Instant) {
    app.ui
        .splash
        .set_animate(!app.theme.opts.reduced_motion && !app.theme.mono());
    let failed = matches!(
        &app.state.connection,
        ConnectionState::Offline { .. }
            | ConnectionState::AuthFailed { .. }
            | ConnectionState::Incompatible { .. }
    ) || matches!(
        &app.state.connection,
        ConnectionState::Reconnecting { attempt, .. } if *attempt >= 2
    );
    if failed {
        app.ui.splash.dismiss();
        return;
    }
    // A client that already has Core data before its first frame (a warm local
    // Core, or a headless test) has nothing to wait for; do not flash a splash
    // over it.
    if app.ui.splash.reached() == 0
        && (app.state.connection.is_live()
            || app.state.snapshots_applied > 0
            || app.state.config.is_some()
            || app.state.core.is_some())
    {
        app.ui.splash.dismiss();
        return;
    }
    let stage = if app.state.connection.is_live() && app.state.snapshots_applied > 0 {
        5
    } else if app.state.connection.is_live() {
        4
    } else if app.state.config.is_some() {
        3
    } else if app.state.snapshots_applied > 0 {
        2
    } else if app.state.core.is_some() {
        1
    } else {
        0
    };
    app.ui.splash.reach(stage, now);
}

fn splash_palette(app: &App) -> crate::splash::Palette {
    let ink = app.theme.style(PRIMARY, CANVAS);
    let logo = ink.add_modifier(Modifier::BOLD);
    // The flash brightens white ink toward pure white on a truecolor dark
    // terminal; elsewhere bold is the only extra emphasis there is.
    let peak = if app.theme.opts.tier == crate::theme::Tier::TrueColor
        && app.theme.opts.mode == crate::theme::Mode::Dark
    {
        ink.add_modifier(Modifier::BOLD)
            .fg(Color::Rgb(255, 255, 255))
    } else {
        logo
    };
    crate::splash::Palette {
        logo,
        ink,
        dim: app.theme.style("text.faint", CANVAS),
        muted: app.theme.style(MUTED, CANVAS),
        border: app.theme.style("border.strong", CANVAS),
        peak,
        ascii: app.theme.opts.ascii,
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

/// Where the editor's text sits in the file view: the wrapped rows, the cursor among
/// them, the gutter width, the first text row and column, the rows shown and the first
/// row shown, and the line number each row starts, if it starts one.
struct EditGeometry {
    rows: Vec<String>,
    numbers: Vec<Option<usize>>,
    cursor: (usize, usize),
    digits: usize,
    top_y: u16,
    base_x: u16,
    page: usize,
    first: usize,
}

fn edit_geometry(edit: &FileEdit, r: Rect) -> EditGeometry {
    let lines = edit.buf.lines();
    let room = paint::file_text_room(r.width, lines.len());
    let (rows, cursor) = edit.buf.layout(room);
    let digits = lines.len().to_string().len().max(3);
    let page = paint::file_page(r.height).max(1);
    let first = visible_first(rows.len(), cursor.0, page);
    // A row starts a line when the rows before it used up the previous line.
    let mut numbers = Vec::with_capacity(rows.len());
    let (mut line, mut used) = (0, 0);
    for row in &rows {
        numbers.push((used == 0 && line < lines.len()).then_some(line + 1));
        used += row.len();
        if lines.get(line).is_some_and(|text| used >= text.len()) {
            line += 1;
            used = 0;
        }
    }
    EditGeometry {
        rows,
        numbers,
        cursor,
        digits,
        top_y: r.y + if r.height >= 8 { 2 } else { 1 },
        base_x: r.x + 1 + digits as u16 + 1 + 2,
        page,
        first,
    }
}

/// `modified`, `saved`, or why a save would be refused, right-aligned in the header.
fn edit_status(edit: &FileEdit) -> String {
    match (edit.saving, edit.stale, edit.dirty()) {
        (true, _, _) => "saving…".into(),
        (_, true, _) => "changed on disk · reload".into(),
        (_, _, true) => "● modified".into(),
        _ => "editing".into(),
    }
}

/// The editor: the same gutter as the read-only view, the cursor line, and a hint row.
fn draw_file_edit(cx: &Cx, buf: &mut Buffer, r: Rect, edit: &FileEdit) {
    let g = edit_geometry(edit, r);
    let gutter = format!("{} ", cx.t.structure("rail"));
    let room = r.right().saturating_sub(g.base_x + 1);
    for (i, line) in g.rows.iter().enumerate().skip(g.first).take(g.page) {
        let y = g.top_y + (i - g.first) as u16;
        let number_style = if i == g.cursor.0 {
            cx.st(PRIMARY, CANVAS)
        } else {
            cx.st(MUTED, CANVAS)
        };
        let number = g.numbers[i].map_or_else(
            || " ".repeat(g.digits + 1),
            |n| format!("{n:>digits$} ", digits = g.digits),
        );
        let nx = put(
            buf,
            r.x + 1,
            y,
            &number,
            number_style,
            r.width.saturating_sub(1),
        );
        put(
            buf,
            nx,
            y,
            &gutter,
            cx.st("border.default", CANVAS),
            r.right().saturating_sub(nx),
        );
        put(buf, g.base_x, y, line, cx.st(PRIMARY, CANVAS), room);
    }
    if r.height >= 3 {
        let y = r.bottom() - 1;
        let hint = if edit.dirty() {
            "ctrl+s save · esc discard"
        } else {
            "ctrl+s save · esc leave"
        };
        put(
            buf,
            r.x + 1,
            y,
            hint,
            cx.st(MUTED, CANVAS),
            r.width.saturating_sub(2),
        );
    }
}

fn cursor_position(app: &App, g: &Regions) -> Option<Position> {
    if app.ui.dialog.is_some() || app.blocking().is_some() {
        return None;
    }
    // The Manager's typed field takes the caret while it is open.
    if app.ui.manager.field.is_some() {
        if let Some((x, y)) = app.ui.manager.cursor.get() {
            return Some(Position::new(x, y));
        }
    }
    if let (Some(edit), true, None, View::Session) = (
        &app.ui.file_edit,
        app.ui.file_open,
        &app.ui.overlay,
        app.ui.view,
    ) {
        let geo = edit_geometry(edit, g.panel.unwrap_or(g.timeline));
        return Some(Position::new(
            geo.base_x + geo.cursor.1 as u16,
            geo.top_y + (geo.cursor.0 - geo.first) as u16,
        ));
    }
    // The file modal hides the page underneath, including the composer's
    // caret, unless the editor above is the one taking input.
    if app.ui.file_open || app.ui.diff_open.is_some() {
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
        let m = Modal::of(modal_box(g.area, MODAL_SMALL, 8));
        let (_, (_, col)) = e.buf.layout(m.w.max(1) as usize);
        return Some(Position::new(
            m.x + (col as u16).min(m.w.saturating_sub(1)),
            m.body.y,
        ));
    }
    if app.ui.focus == Focus::Waiting {
        return ask_input_cursor(app, g);
    }
    if app.ui.focus != Focus::Composer || !app.writable() {
        return None;
    }
    let inner = composer_inner_with(
        composer_without_pending(g.composer, app.pending_inputs().len()),
        app.composer_ref().images.len(),
        app.composer_ref().pastes().len(),
    );
    let (rows, (cr, cc)) = app.composer_view().layout(inner.width as usize);
    let input_height = inner.height as usize;
    let first = app.ui.composer_drag.map_or_else(
        || visible_first(rows.len(), cr, input_height),
        |(_, top)| top,
    );
    if cr < first || cr - first >= input_height {
        return None;
    }
    Some(Position::new(
        inner.x + cc as u16,
        inner.y + (cr - first) as u16,
    ))
}

/// The terminal cursor in the question card's custom answer field.
fn ask_input_cursor(app: &App, g: &Regions) -> Option<Position> {
    if !app.writable() {
        return None;
    }
    let c = app.ask_current()?;
    if c.sent || c.review() || !c.field() {
        return None;
    }
    let r = g.waiting;
    if r.height < 3 || r.width < 12 {
        return None;
    }
    let card = Rect::new(r.x + 2, r.y, r.width.saturating_sub(4), r.height);
    let x = card.x + 3;
    let end = card.right().saturating_sub(2);
    let inner_w = end.saturating_sub(x);
    let (_, (row, col)) = c.pick()?.input.layout(inner_w.saturating_sub(2) as usize);
    let at = wait_rows(app, r.width, r.height as usize, "\u{2026}")
        .iter()
        .position(|line| matches!(line, WRow::Input(i, _) if *i == row))?;
    Some(Position::new(x + 2 + col as u16, card.y + at as u16))
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

/// The text area inside the card, below collapsed-paste cards and thumbnails.
/// The mouse path must pass the draft's paste count to line up.
pub(crate) fn composer_inner_with(r: Rect, images: usize, pastes: usize) -> Rect {
    let r = composer_card(composer_content(r), images, pastes);
    Rect::new(
        r.x + 5,
        r.y + 2,
        r.width.saturating_sub(10),
        r.height.saturating_sub(6),
    )
}

pub(crate) fn composer_without_pending(r: Rect, count: usize) -> Rect {
    let extra = if count == 0 {
        0
    } else {
        (count.min(4) + 2) as u16
    }
    .min(r.height.saturating_sub(8));
    let extra = if extra < 3 { 0 } else { extra };
    Rect::new(r.x, r.y + extra, r.width, r.height.saturating_sub(extra))
}

#[cfg(test)]
mod composer_geometry_tests {
    use super::*;

    #[test]
    fn pending_cards_shift_the_input_and_respect_small_terminals() {
        let r = Rect::new(10, 20, 80, 14);
        assert_eq!(
            composer_inner_with(composer_without_pending(r, 0), 0, 0).y,
            22
        );
        assert_eq!(
            composer_inner_with(composer_without_pending(r, 1), 0, 0).y,
            25
        );
        assert_eq!(
            composer_inner_with(composer_without_pending(r, 8), 0, 0).y,
            28
        );
        assert_eq!(
            composer_inner_with(composer_without_pending(r, 1), 1, 0).y,
            28
        );
        let short = Rect::new(0, 0, 80, 10);
        assert_eq!(composer_without_pending(short, 1), short);
    }

    #[test]
    fn attachment_thumbnails_wrap_by_width_and_are_square() {
        assert_eq!(composer_image_rows(80, 0), 0);
        assert_eq!(composer_image_rows(80, 1), 4, "name + 3 rows");
        assert_eq!(composer_image_rows(80, 9), 4, "nine fit one line");
        assert_eq!(composer_image_rows(80, 10), 8, "the tenth wraps");
        assert_eq!(composer_image_rows(40, 8), 8, "four per line");
    }
}

fn composer_content(r: Rect) -> Rect {
    Rect::new(r.x, r.y, r.width, r.height.saturating_sub(1))
}

/// Rows the composer's attachment strip needs above the card: each thumbnail
/// is a square with its muted name on the row above, and each collapsed paste
/// is a title row (`Pasted text · N lines · Show`) over a clipped first line.
fn composer_image_rows(width: u16, images: usize) -> u16 {
    if images == 0 {
        return 0;
    }
    let gap = 2u16;
    let available = width.saturating_sub(6);
    let per_line = (available + gap)
        .checked_div(crate::images::COMPOSER_THUMB_COLS as u16 + gap)
        .unwrap_or(0)
        .max(1) as usize;
    let lines = images.div_ceil(per_line);
    (lines * (1 + crate::images::COMPOSER_THUMB_ROWS)) as u16
}

fn composer_paste_rows(pastes: usize) -> u16 {
    (pastes as u16) * 2
}

fn composer_strip_rows(width: u16, images: usize, pastes: usize) -> u16 {
    composer_paste_rows(pastes) + composer_image_rows(width, images)
}

fn composer_card(r: Rect, images: usize, pastes: usize) -> Rect {
    let offset = composer_strip_rows(r.width, images, pastes).min(r.height.saturating_sub(7));
    Rect::new(r.x, r.y + offset, r.width, r.height - offset)
}

fn heights(app: &App, w: u16) -> Heights {
    let banner = u16::from(!app.banners().is_empty());
    let inner_w = w.saturating_sub(10) as usize;
    let rows = app.composer_view().layout(inner_w).0.len().clamp(1, 8) as u16;
    let (waiting, composer) = (
        waiting_height(app, w),
        rows + 7
            + if app.pending_inputs().is_empty() {
                0
            } else {
                app.pending_inputs().len().min(4) as u16 + 2
            }
            + composer_strip_rows(
                w,
                app.composer_ref().images.len(),
                app.composer_ref().pastes().len(),
            ),
    );
    Heights {
        heading: u16::from(
            !app.narrow() && app.ui.view != View::Settings && app.viewed_root().is_some(),
        ),
        banner,
        waiting,
        composer,
        strip: u16::from(
            app.viewed_id()
                .is_some_and(|id| app.viewed_root().is_some_and(|r| r != id)),
        ),
    }
}

/// Option rows the question card shows at most; the cursor scrolls the rest.
const ASK_ROWS: usize = 5;
/// Rows the custom answer field shows at most; it scrolls with the cursor.
const ASK_INPUT_ROWS: usize = 3;

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
                let x = if g.rail.is_some() {
                    rect.x + rect.width.saturating_sub(gw) / 2
                } else {
                    rect.x + 1
                };
                put(
                    buf,
                    x,
                    rect.y,
                    &glyph,
                    style,
                    rect.right().saturating_sub(x),
                );
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

    // The Manager button, left of the right toggle: the hidden context modal.
    if g.manager.width > 0
        && cx.app.manager_setting_value("manager.enabled").as_deref() == Some("on")
    {
        let m = &cx.app.ui.manager;
        let label = if m.dropdown_open || m.call.is_some() || m.starting || m.indicator.visible() {
            ""
        } else {
            "Manager"
        };
        let live = cx.app.ui.manager.call.is_some();
        let style = if live {
            cx.st("status.info", SIDEBAR)
        } else {
            cx.st(SECONDARY, SIDEBAR)
        };
        let x = g.manager.x;
        put(buf, x, g.manager.y, label, style, g.manager.width);
        hits.add(g.manager, Target::ManagerSession);
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
/// the strip. A viewed sub agent shows `root › child`. The run status sits
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
    let viewed = a.viewed_root();
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
            let parts = vec![title_of(id)];
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
    // One width for every tab — roots, children and New session — so the strip
    // keeps the same rhythm whatever the titles say.
    let tab_width = |tabs: &[Tab], max: usize| tabs.iter().map(|t| cell(t, max)).max().unwrap_or(0);
    let mut max_title = 28usize;
    let total = |tabs: &[Tab], max: usize| tabs.len() * (tab_width(tabs, max) + 1);
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
    let w = tab_width(&tabs, max_title) as u16;
    for t in &tabs {
        if x + w > tr.right() {
            break;
        }
        let rect = Rect::new(x, tr.y, w, 1);
        let bg = if t.active { CANVAS } else { SIDEBAR };
        let base = cx.st(if t.active { PRIMARY } else { MUTED }, bg);
        clear(buf, rect, base);
        let mut cx_x = x + 1;
        if let Some(id) = &t.id {
            let (g, gs) = session_glyph(cx, id, bg);
            cx_x = put(buf, cx_x, tr.y, &g, gs, 1) + 1;
        }
        let title_max = max_title
            .min(w.saturating_sub(5 + u16::from(t.id.is_some()) * 2) as usize)
            .max(6);
        let parts = shown(t, title_max);
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
            cx_x = put(
                buf,
                cx_x,
                tr.y,
                part,
                style,
                rect.right().saturating_sub(cx_x),
            );
            if pi < last {
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

/// The status mark in front of a session tab, also used by the session
/// switcher so both carry the same icon.
fn session_glyph(cx: &Cx, id: &str, bg: &str) -> (String, Style) {
    let a = cx.app;
    let ascii = cx.t.opts.ascii;
    if a.is_busy(id) {
        (cx.t.spinner(a.frame).to_owned(), cx.st(PRIMARY, bg))
    } else if a.ui.failed_sessions.contains(id) {
        (
            if ascii { "o" } else { "\u{25cf}" }.to_owned(),
            cx.st("status.error", bg),
        )
    } else if a.session_waiting(id) {
        let (g, tok) = cx.t.attention("waiting");
        (g.to_owned(), cx.st(tok, bg))
    } else if a.ui.unread_sessions.contains(id) {
        (
            if ascii { "*" } else { "\u{25cf}" }.to_owned(),
            cx.st("status.info", bg),
        )
    } else {
        (
            if ascii { "." } else { "\u{b7}" }.to_owned(),
            cx.st(MUTED, bg),
        )
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
            format!("{} {}", cx.t.spinner(a.frame), a.state.connection.label()),
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

/// The first row of an open session's page: the full title of its tab on the
/// black page surface, ellipsised only when the page is too narrow for it.
fn draw_heading(cx: &Cx, buf: &mut Buffer, r: Rect) {
    if r.height == 0 {
        return;
    }
    let Some(root) = cx.app.viewed_root() else {
        return;
    };
    let title = cx
        .app
        .state
        .model
        .sessions
        .get(&root)
        .map_or("", |s| s.title.as_str());
    let title = if title.is_empty() { "Untitled" } else { title };
    let style = cx.st(SECONDARY, CANVAS);
    clear(buf, r, style);
    let room = r.width.saturating_sub(2) as usize;
    put(
        buf,
        r.x + 1,
        r.y,
        &truncate(title, room, cx.ell()),
        style,
        r.width.saturating_sub(1),
    );
}

/// The subagent strip under the tabs (shown only while a child is viewed): the
/// path above it as equal parent tabs, a rail, then the root's other sessions
/// as equal tabs with their state glyph and agent. The viewed child is the one
/// selected tab, so its title appears there and not again beside the strip.
fn draw_subagents(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.height == 0 {
        return;
    }
    let a = cx.app;
    let Some(viewed) = a.viewed_id() else {
        return;
    };
    let chain = a.ancestors_of(viewed);
    if chain.len() < 2 {
        return;
    }
    let root = chain[0];
    let mut ancestors: Vec<&crate::api::Session> = chain[..chain.len() - 1].to_vec();
    let mut children: Vec<&crate::api::Session> = a.subagents_of(&root.id);
    let above: Vec<&str> = ancestors.iter().map(|s| s.id.as_str()).collect();
    children.retain(|s| !above.contains(&s.id.as_str()));
    if children.is_empty() {
        children.push(chain[chain.len() - 1]);
    }
    let title_w = |s: &&crate::api::Session| width(&s.title).min(28);
    let natural = |s: &&crate::api::Session, child: bool| {
        if child {
            4 + width(&crate::text::capitalize(&s.agent)) + title_w(s)
        } else {
            title_w(s)
        }
    };
    let widest = ancestors
        .iter()
        .map(|s| natural(s, false))
        .chain(children.iter().map(|s| natural(s, true)))
        .max()
        .unwrap_or(10);
    let mut cell = (widest + 2).clamp(10, 30) as u16;
    let total = |cell: u16, count: usize| count as u16 * (cell + 1) + 2;
    let room = r.width.saturating_sub(3);
    let mut dropped_left = false;
    while total(cell, ancestors.len() + children.len()) + if dropped_left { 2 } else { 0 } > room {
        if ancestors.len() > 1 {
            ancestors.remove(0);
            dropped_left = true;
        } else if children.len() > 1 {
            // Drop the tab farthest from the viewed child so it keeps its selected tab.
            let at = children
                .iter()
                .position(|s| s.id.as_str() == viewed)
                .unwrap_or(0);
            if at + 1 < children.len() {
                children.remove(children.len() - 1);
            } else {
                children.remove(0);
            }
        } else if cell > 8 {
            cell -= 1;
        } else {
            break;
        }
    }
    let row = Rect::new(r.x, r.y, r.width, 1);
    clear(buf, row, cx.st(MUTED, CANVAS));
    let mut x = r.x + 1;
    if dropped_left {
        x = put(buf, x, r.y, cx.ell(), cx.st(MUTED, CANVAS), 1) + 1;
    }
    for s in &ancestors {
        if x + cell > row.right() {
            return;
        }
        let rect = Rect::new(x, r.y, cell, 1);
        let hot = cx.hovered(rect);
        let bg = if hot { RAISED } else { CANVAS };
        clear(buf, rect, cx.st(SECONDARY, bg));
        put(
            buf,
            x + 1,
            r.y,
            &truncate(&s.title, cell.saturating_sub(2) as usize, cx.ell()),
            cx.st(if hot { PRIMARY } else { SECONDARY }, bg),
            cell.saturating_sub(1),
        );
        hits.add(rect, Target::Subagent(s.id.clone()));
        x += cell + 1;
    }
    x = put(buf, x, r.y, cx.t.structure("rail"), cx.st(MUTED, CANVAS), 1) + 1;
    for s in &children {
        if x + cell > row.right() {
            break;
        }
        let open = s.id.as_str() == viewed;
        let rect = Rect::new(x, r.y, cell, 1);
        let bg = if open { RAISED } else { CANVAS };
        let base = cx.st(if open { PRIMARY } else { SECONDARY }, bg);
        clear(buf, rect, base);
        let (glyph, style) = session_glyph(cx, &s.id, bg);
        let mut tx = put(buf, x + 1, r.y, &glyph, style, 1) + 1;
        let agent = crate::text::capitalize(&s.agent);
        tx = put(
            buf,
            tx,
            r.y,
            &agent,
            cx.st(PRIMARY, bg).patch(cx.t.agent_style(&s.agent)),
            rect.right().saturating_sub(tx),
        ) + 1;
        put(
            buf,
            tx,
            r.y,
            &truncate(&s.title, rect.right().saturating_sub(tx) as usize, cx.ell()),
            base,
            rect.right().saturating_sub(tx),
        );
        hits.add(rect, Target::Subagent(s.id.clone()));
        x += cell + 1;
    }
}

fn draw_banner(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if r.height == 0 {
        return;
    }
    let banners = cx.app.banners();
    let Some(b) = banners.first() else { return };
    let row = Rect::new(r.x, r.y, r.width, 1);
    clear(buf, row, cx.st(SECONDARY, PANEL));
    let (glyph, tok) = if b.loading {
        (cx.t.spinner(cx.app.frame).to_owned(), "text.primary")
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
    if r.width <= crate::layout::RAIL_WIDTH {
        let canvas = cx.st(PRIMARY, CANVAS).bg.unwrap_or(Color::Reset);
        for y in r.y..r.bottom() {
            let from = buf[(x.saturating_sub(1), y)].bg;
            let fade = match (from, canvas) {
                (Color::Rgb(r, g, b), Color::Rgb(cr, cg, cb)) => Color::Rgb(
                    ((r as u16 + cr as u16) / 2) as u8,
                    ((g as u16 + cg as u16) / 2) as u8,
                    ((b as u16 + cb as u16) / 2) as u8,
                ),
                _ => canvas,
            };
            put(buf, x, y, " ", Style::default().bg(fade), 1);
        }
        return;
    }
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
    if false && a.ui.tab == Tab::Sessions && body.width >= 8 && body.height >= 3 {
        let scope = Rect::new(body.right() - 3, body.y, 2, 1);
        if cx.hovered(scope) {
            let label = if a.ui.scope_all {
                "All projects · switch scope"
            } else {
                "Current project · switch scope"
            };
            let w = (width(label) as u16 + 2).min(body.width);
            let tip = Rect::new(body.right() - w, body.y + 1, w, 1);
            clear(buf, tip, cx.st(PRIMARY, RAISED));
            put(
                buf,
                tip.x + 1,
                tip.y,
                label,
                cx.st(PRIMARY, RAISED),
                w.saturating_sub(2),
            );
        }
    }
}

/// The sidebar tabs, in manifest order.
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
            Rect::new(
                r.x + 4 + i * 11,
                r.y.saturating_sub(1),
                if i == 0 { 10 } else { 7 },
                1,
            )
        };
        let rect = if !rail && cx.app.narrow() {
            Rect::new(r.x + 3 + i * 11, r.y, if i == 0 { 10 } else { 7 }, 1)
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
    for (tab, rect, _, _) in tab_icons(cx, r) {
        let active = a.ui.tab == tab;
        let mut style = if active {
            cx.st(PRIMARY, SIDEBAR)
        } else {
            cx.st(MUTED, SIDEBAR)
        };
        if cx.hovered(rect) && !active {
            style = cx.st(SECONDARY, SIDEBAR);
        }
        if active || cx.hovered(rect) {
            clear(buf, rect, style);
        }
        let label = match tab {
            Tab::Sessions => "[Sessions]",
            Tab::Projects => "[Projects]",
            Tab::Files => "[Files]",
        };
        put(buf, rect.x, rect.y, label, style, rect.width);
        if active && cx.t.mono() {
            buf.set_style(rect, Style::default().add_modifier(Modifier::UNDERLINED));
        }
        hits.add(rect, Target::Tab(tab));
    }
}

/// One rail entry: a project, the add-project control, or Settings while the
/// sidebar with its own Settings row is closed.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum RailItem {
    Project(usize),
    Add,
    Settings,
}

/// The corner counters and the label between them, and a separating row.
const RAIL_ITEM: u16 = 4;

pub(crate) fn rail_layout(a: &App, r: Rect) -> Vec<(RailItem, Rect)> {
    let entries = a.rail_project_ids();
    let settings = !a.ui.left_open;
    let room = r.height.saturating_sub(4 + if settings { 4 } else { 0 });
    let shown = (room / RAIL_ITEM) as usize;
    // The current project stays in view when the rail overflows.
    let current = a
        .current_project()
        .and_then(|id| entries.iter().position(|e| *e == id))
        .unwrap_or(0);
    let top = (current + 1).saturating_sub(shown);
    let mut out = Vec::new();
    let mut y = r.y.saturating_add(1);
    for i in (top..entries.len()).take(shown) {
        out.push((RailItem::Project(i), Rect::new(r.x, y, r.width, RAIL_ITEM)));
        y += RAIL_ITEM;
    }
    if y + 3 <= r.bottom() {
        out.push((RailItem::Add, Rect::new(r.x, y, r.width, 3)));
    }
    if settings && r.height >= 4 {
        out.push((
            RailItem::Settings,
            Rect::new(r.x, r.bottom() - 4, r.width, 3),
        ));
    }
    out
}

pub(crate) fn rail_labels(names: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut used = std::collections::HashSet::new();
    names
        .into_iter()
        .map(|name| {
            let chars: Vec<_> = name.trim().to_uppercase().chars().collect();
            let first = chars.first().copied().unwrap_or('·').to_string();
            let mut label = first.clone();
            if used.contains(&label) {
                label = chars
                    .iter()
                    .skip(1)
                    .filter(|c| c.is_alphabetic() && !"AEIOU".contains(**c))
                    .map(|c| format!("{first}{c}"))
                    .find(|candidate| !used.contains(candidate))
                    .unwrap_or_else(|| {
                        let mut n = 2;
                        while used.contains(&format!("{first}{n}")) {
                            n += 1;
                        }
                        format!("{first}{n}")
                    });
            }
            used.insert(label.clone());
            label
        })
        .collect()
}

fn rail_activity(a: &App, project: &str) -> (usize, f64) {
    let mut running = 0;
    let mut latest: f64 = 0.0;
    for s in a
        .state
        .model
        .sessions
        .values()
        .filter(|s| s.project_id == project)
    {
        if a.is_busy(&s.id) {
            running += 1;
        }
        latest = latest.max(s.updated);
    }
    (running, latest)
}

fn rail_counts(a: &App, project: &str) -> [usize; 4] {
    let mut counts = [0; 4];
    for s in a
        .state
        .model
        .sessions
        .values()
        .filter(|s| s.project_id == project && s.parent_id.is_none() && !s.archived)
    {
        let index = match a.session_state(&s.id) {
            "needs input" => 0,
            "finished" => 1,
            "running" => 2,
            _ => 3,
        };
        counts[index] += 1;
    }
    counts
}

fn rail_count(n: usize) -> String {
    if n > 9 {
        "*".to_owned()
    } else {
        n.to_string()
    }
}

fn draw_rail(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    let a = cx.app;
    clear(buf, r, cx.st(SECONDARY, SIDEBAR));
    hits.add(r, Target::Swallow);
    let entries = a.rail_project_ids();
    let labels = a.rail_label_map();
    let inner = r.width;
    let centre = r.x + inner.saturating_sub(1) / 2;
    for (item, band) in rail_layout(a, r) {
        let hovered = cx.hovered(band);
        match item {
            RailItem::Add => {
                let style = cx.st(if hovered { PRIMARY } else { MUTED }, SIDEBAR);
                put(buf, centre, band.y + 1, "+", style, 1);
                hits.add(band, Target::AddProject);
            }
            RailItem::Settings => {
                let style = cx.st(if hovered { PRIMARY } else { MUTED }, SIDEBAR);
                let glyph = if cx.t.opts.ascii { "*" } else { "⚙" };
                put(buf, centre, band.y + 1, glyph, style, 1);
                hits.add(band, Target::SettingsEntry);
            }
            RailItem::Project(i) => {
                let Some(p) = entries.get(i).and_then(|id| a.state.model.projects.get(id)) else {
                    continue;
                };
                let selected = a.current_project().as_deref() == Some(p.id.as_str());
                let dragging = a.ui.rail_drag;
                let is_source = dragging.is_some_and(|d| d.active && d.from == i);
                let is_target = dragging.is_some_and(|d| d.active && !d.cancelled && d.to == i);
                let passive = a.project_status(&p.id) == crate::api::project_status::PASSIVE;
                let (running, latest) = rail_activity(a, &p.id);
                let token = if running > 0 || (latest > 0.0 && now_secs() - latest < 3600.0) {
                    SECONDARY
                } else {
                    MUTED
                };
                let lifted = selected || hovered;
                let bg = if lifted { ACTIVE } else { SIDEBAR };
                let tile = Rect::new(band.x, band.y, inner, 3).intersection(r);
                if lifted {
                    clear(buf, tile, cx.st(PRIMARY, ACTIVE));
                }
                let mut style = cx.st(token, bg);
                if token == MUTED || passive || is_source {
                    style = style.add_modifier(Modifier::DIM);
                }
                if is_target {
                    style = cx
                        .st("interactive.focus", RAISED)
                        .add_modifier(Modifier::BOLD);
                }
                if selected {
                    style = cx.st(PRIMARY, ACTIVE).add_modifier(Modifier::BOLD);
                }
                let label = labels.get(&p.id).cloned().unwrap_or_default();
                let label = &label;
                let label_width = width(label) as u16;
                let label_x = tile.x + tile.width.saturating_sub(label_width) / 2;
                put(buf, label_x, band.y + 1, label, style, label_width);
                if selected {
                    for y in tile.y..tile.bottom() {
                        put(
                            buf,
                            band.x,
                            y,
                            if cx.t.opts.ascii { "|" } else { "▏" },
                            cx.st(ACTIVE_BAR, bg),
                            1,
                        );
                    }
                }
                let counts = rail_counts(a, &p.id);
                if counts.iter().all(|n| *n == 0) {
                    let glyph = if cx.t.opts.ascii { "-" } else { "─" };
                    let len = 3.min(tile.width);
                    put(
                        buf,
                        tile.x + (tile.width - len) / 2,
                        band.y + 2,
                        &glyph.repeat(len as usize),
                        cx.st("border.default", bg).add_modifier(Modifier::DIM),
                        len,
                    );
                } else {
                    // One counter in each corner: attention and unread on top,
                    // running and idle below.
                    let tokens = [MULTI, "status.info", "status.success", PRIMARY];
                    let left = tile.x + 1;
                    let right = tile.right().saturating_sub(2);
                    let corners = [
                        (left, band.y),
                        (right, band.y),
                        (left, band.y + 2),
                        (right, band.y + 2),
                    ];
                    for (index, (x, y)) in corners.into_iter().enumerate() {
                        if counts[index] > 0 {
                            put(
                                buf,
                                x,
                                y,
                                &rail_count(counts[index]),
                                cx.st(tokens[index], bg),
                                1,
                            );
                        }
                    }
                }
                if is_target && dragging.is_some_and(|d| d.from != d.to) {
                    let glyph = if cx.t.opts.ascii { "-" } else { "▔" };
                    put(
                        buf,
                        band.x,
                        band.y,
                        &glyph.repeat(band.width as usize),
                        cx.st("interactive.focus", SIDEBAR),
                        band.width,
                    );
                }
                hits.add(band, Target::RailProject(i));
            }
        }
    }
}

/// The hovered rail project's name and activity, drawn right beside the rail
/// (after everything else, so it is on top).
fn draw_rail_popup(cx: &Cx, buf: &mut Buffer, r: Rect, limit: Rect) {
    let a = cx.app;
    let entries = a.rail_project_ids();
    let Some((RailItem::Project(i), band)) = rail_layout(a, r)
        .into_iter()
        .find(|(item, band)| matches!(item, RailItem::Project(_)) && cx.hovered(*band))
    else {
        return;
    };
    let Some(p) = entries.get(i).and_then(|id| a.state.model.projects.get(id)) else {
        return;
    };
    let title = p.name.clone();
    let counts = rail_counts(a, &p.id);
    let details = [
        format!("{} attention", counts[0]),
        format!("{} unread", counts[1]),
        format!("{} running", counts[2]),
        format!("{} idle", counts[3]),
    ];
    let max = limit.right().saturating_sub(r.right() + 1).min(60) as usize;
    if max < 10 {
        return;
    }
    let w = (width(&title).max(details.iter().map(|s| width(s)).max().unwrap_or(0)) + 4).min(max)
        as u16;
    let height = 7.min(limit.height);
    let pop = Rect::new(
        r.right(),
        band.y.min(limit.bottom().saturating_sub(height)),
        w,
        height,
    )
    .intersection(limit);
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
    for (i, detail) in details
        .iter()
        .enumerate()
        .take(pop.height.saturating_sub(2) as usize)
    {
        let token = [MULTI, "status.info", "status.success", PRIMARY][i];
        put(
            buf,
            pop.x + 2,
            pop.y + 2 + i as u16,
            &truncate(detail, (w - 3) as usize, cx.ell()),
            cx.st(token, bg),
            w - 3,
        );
    }
}

fn draw_files(cx: &Cx, buf: &mut Buffer, r: Rect, focused: bool, hits: &mut HitMap) {
    let a = cx.app;
    let mut top = r.y;
    // The root header has the project name in bold and `⌥ main` muted at the
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
            &truncate(&h.name, room, cx.ell()),
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

/// The file/diff panel. Wide, it sits beside the timeline; narrow, it covers
/// the whole window (the composer included). When it hides a live session's
/// approvals or question, the header says so.
fn draw_panel(cx: &Cx, buf: &mut Buffer, r: Rect, area: Rect, hits: &mut HitMap) {
    // A cell of frame on the panel surface, when the window leaves room for it.
    let frame = Rect::new(
        r.x.saturating_sub(1),
        r.y.saturating_sub(1),
        r.width + 2,
        r.height + 2,
    )
    .intersection(area);
    clear(buf, frame, cx.st(PRIMARY, PANEL));
    hits.add(frame, Target::Swallow);
    clear(buf, r, cx.st(PRIMARY, CANVAS));
    if cx.app.terminal_open {
        put(buf, r.x, r.y, "Terminal", cx.st(PRIMARY, CANVAS), r.width);
        if r.height > 1 {
            put(
                buf,
                r.x,
                r.y + 1,
                if cx.app.terminal_panel.cursor == 0 {
                    "> New terminal"
                } else {
                    "  New terminal"
                },
                cx.st(ACCENT, CANVAS),
                r.width,
            );
        }
        for (i, terminal) in cx.app.terminal_panel.terminals.iter().enumerate() {
            let y = r.y.saturating_add(2).saturating_add(i as u16);
            if y >= r.bottom() {
                break;
            }
            put(
                buf,
                r.x,
                y,
                &format!(
                    "{} {}  {}  {}  {}",
                    if cx.app.terminal_panel.cursor == i + 1 {
                        ">"
                    } else {
                        " "
                    },
                    terminal.name,
                    terminal.workspace,
                    terminal.status,
                    terminal.controller.as_deref().unwrap_or("No controller")
                ),
                cx.st(PRIMARY, CANVAS),
                r.width,
            );
        }
    } else if cx.app.ui.diff_open.is_some() {
        draw_diff_view(cx, buf, r, true, hits);
    } else if cx.app.ui.file_open {
        draw_file_view(cx, buf, r, true, hits);
    }
    // The modal must not hide a request that needs the user.
    let waiting = cx.app.wait_items();
    if !waiting.is_empty() {
        let label = if cx.t.opts.ascii {
            format!("! {} waiting", waiting.len())
        } else {
            format!("\u{26a0} {} waiting", waiting.len())
        };
        let w = width(&label) as u16;
        let x = r.right().saturating_sub(w + 1);
        // Clear what the file view put at the right of its title row (the range).
        put(
            buf,
            x,
            r.y,
            &" ".repeat(w as usize + 1),
            cx.st(PRIMARY, CANVAS),
            w + 1,
        );
        put(
            buf,
            x,
            r.y,
            &label,
            cx.st("status.warning", CANVAS).add_modifier(Modifier::BOLD),
            w,
        );
    }
}

/// The read-only file view (DESIGN_PLAN.md D16), on the page surface: a header
/// row with the `~` path (the basename bold) and the shown line range at the
/// right, a blank row, the numbered text behind a `│` gutter, and a bottom
/// row that holds the search. The text is drawn up to the row above it.
fn draw_file_view(cx: &Cx, buf: &mut Buffer, r: Rect, back: bool, hits: &mut HitMap) {
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
    if Some(project.clone()) != cx.app.current_project() || session != &cx.app.files_session() {
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
    let text_room = paint::file_text_room(r.width, total);
    let preview = cx.app.file_preview();
    // A Markdown file offers `p source`, or `p preview` back from the source.
    let toggle = (cx.app.file_is_markdown() && cx.app.ui.file_edit.is_none()).then(|| {
        let key = cx
            .app
            .manifest
            .command("editor.preview")
            .and_then(|c| c.tui_key())
            .unwrap_or("p");
        (
            if preview { "source" } else { "preview" }.to_owned(),
            key.to_owned(),
        )
    });
    let toggle_w = toggle
        .as_ref()
        .map_or(0, |(label, key)| width(key) + 1 + width(label)) as u16;
    let mut right = r.right().saturating_sub(1);
    if let Some((label, key)) = &toggle {
        let tw = width(key) as u16 + 1 + width(label) as u16;
        if right > tw + 2 {
            let x = right.saturating_sub(tw);
            let kx = put(
                buf,
                x,
                r.y,
                key,
                cx.st(ACCENT, CANVAS),
                r.right().saturating_sub(x),
            );
            let lx = put(
                buf,
                kx,
                r.y,
                " ",
                cx.st(MUTED, CANVAS),
                r.right().saturating_sub(kx),
            );
            put(
                buf,
                lx,
                r.y,
                label,
                cx.st(MUTED, CANVAS),
                r.right().saturating_sub(lx),
            );
            hits.add(Rect::new(x, r.y, tw, 1), Target::FilePreview);
            right = x.saturating_sub(2);
        }
    }
    // `e edit`, so the read-only view says how to start typing.
    let edit_key = cx
        .app
        .manifest
        .command("editor.edit")
        .and_then(|c| c.tui_key())
        .unwrap_or("e")
        .to_owned();
    let edit_w = width(&edit_key) as u16 + 1 + width("edit") as u16;
    if right > edit_w + 2 {
        let x = right.saturating_sub(edit_w);
        let kx = put(
            buf,
            x,
            r.y,
            &edit_key,
            cx.st(ACCENT, CANVAS),
            r.right().saturating_sub(x),
        );
        let lx = put(
            buf,
            kx,
            r.y,
            " ",
            cx.st(MUTED, CANVAS),
            r.right().saturating_sub(kx),
        );
        put(
            buf,
            lx,
            r.y,
            "edit",
            cx.st(MUTED, CANVAS),
            r.right().saturating_sub(lx),
        );
        hits.add(Rect::new(x, r.y, edit_w, 1), Target::FileEdit);
        right = x.saturating_sub(2);
    }
    // `lines 1-38 of 38`, right-aligned and muted.
    let range = match result {
        Ok(_) if total == 0 => "empty file".to_owned(),
        Ok(_) if preview => String::new(),
        Ok(_) => format!(
            "lines {}-{} of {total}",
            scroll + 1,
            scroll + paint::file_lines_shown(content, scroll, text_room, page)
        ),
        Err(_) => String::new(),
    };
    let range = match &cx.app.ui.file_edit {
        Some(edit) => edit_status(edit),
        None => range,
    };
    let range_w = width(&range) as u16;
    if range_w > 0 && right > range_w + 2 {
        put(
            buf,
            right.saturating_sub(range_w),
            r.y,
            &range,
            cx.st(MUTED, CANVAS),
            range_w,
        );
    }
    // The panel's back control sits at the start of the title row; the path
    // follows it, in secondary with the basename bold.
    let mut x = r.x + 1;
    if back {
        let label = if cx.t.opts.ascii {
            "< Back"
        } else {
            "\u{2039} Back"
        };
        let w = width(label) as u16;
        x = put(
            buf,
            x,
            r.y,
            label,
            cx.st(ACCENT, CANVAS),
            r.width.saturating_sub(1),
        );
        hits.add(Rect::new(r.x + 1, r.y, w, 1), Target::PanelBack);
        x = put(
            buf,
            x,
            r.y,
            "  ",
            cx.st(MUTED, CANVAS),
            r.right().saturating_sub(x),
        );
    }
    // A long directory is cut in the middle and the basename is kept.
    let room = (r.width as usize)
        .saturating_sub(
            range_w as usize + toggle_w as usize + edit_w as usize + (x - r.x) as usize + 4,
        )
        .max(8);
    let (dir, base) = match display.rfind('/') {
        Some(i) => display.split_at(i + 1),
        None => ("", display.as_str()),
    };
    let base = truncate(base, room, cx.ell());
    let dir = truncate_middle(dir, room.saturating_sub(width(&base)), cx.ell());
    let x = put(
        buf,
        x,
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
    if let Some(edit) = &cx.app.ui.file_edit {
        draw_file_edit(cx, buf, r, edit);
        return;
    }
    match result {
        Ok(_) => {
            if preview {
                let rows = cx.app.file_preview_rows(r.width.saturating_sub(2) as usize);
                let top = r.y + if r.height >= 8 { 2 } else { 1 };
                let scroll = scroll.min(rows.len().saturating_sub(page));
                for (i, line) in rows.iter().skip(scroll).take(page).enumerate() {
                    buf.set_line(r.x + 1, top + i as u16, line, r.width.saturating_sub(2));
                }
                return;
            }
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
            let mut drawn = 0usize;
            'lines: for (i, line) in content.lines().enumerate().skip(scroll) {
                let lower = line.to_ascii_lowercase();
                let found: Vec<usize> = query
                    .as_ref()
                    .map(|query| {
                        lower
                            .match_indices(query.as_str())
                            .map(|(at, _)| at)
                            .collect()
                    })
                    .unwrap_or_default();
                for (row, range) in paint::wrap_ranges(line, text_room).into_iter().enumerate() {
                    if drawn >= page {
                        break 'lines;
                    }
                    let y = top + drawn as u16;
                    drawn += 1;
                    // The number marks a line's first row; its other rows keep the rail.
                    let number = if row == 0 {
                        format!("{:>digits$} ", i + 1)
                    } else {
                        " ".repeat(digits + 1)
                    };
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
                    put(
                        buf,
                        base_x,
                        y,
                        &line[range.clone()],
                        cx.st(PRIMARY, CANVAS),
                        text_room as u16,
                    );
                    if let Some(query) = &query {
                        for (k, &at) in found.iter().enumerate() {
                            // The part of this match on this row.
                            let from = at.max(range.start);
                            let to = (at + query.len()).min(range.end);
                            if from >= to
                                || !line.is_char_boundary(from)
                                || !line.is_char_boundary(to)
                            {
                                continue;
                            }
                            let x = base_x + width(&line[range.start..from]) as u16;
                            if x >= r.right() {
                                continue;
                            }
                            let len = (width(&line[from..to]) as u16).min(r.right() - x);
                            let mut style = Style::default().add_modifier(Modifier::UNDERLINED);
                            // the current match (the first on its line) is reversed
                            if current == Some(i) && k == 0 {
                                style = style.add_modifier(Modifier::REVERSED);
                            }
                            buf.set_style(Rect::new(x, y, len, 1), style);
                        }
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
fn draw_diff_view(cx: &Cx, buf: &mut Buffer, r: Rect, back: bool, hits: &mut HitMap) {
    clear(buf, r, cx.st(PRIMARY, SIDEBAR));
    let Some((path, group)) = &cx.app.ui.diff_open else {
        return;
    };
    let mut x = r.x + 1;
    if back {
        let label = if cx.t.opts.ascii {
            "< Back"
        } else {
            "\u{2039} Back"
        };
        let w = width(label) as u16;
        x = put(
            buf,
            x,
            r.y,
            label,
            cx.st(ACCENT, SIDEBAR),
            r.width.saturating_sub(1),
        );
        hits.add(Rect::new(r.x + 1, r.y, w, 1), Target::PanelBack);
        x = put(
            buf,
            x,
            r.y,
            "  ",
            cx.st(MUTED, SIDEBAR),
            r.right().saturating_sub(x),
        );
    }
    let title = format!("{path} {} {}", cx.t.structure("separator"), group.label());
    put(
        buf,
        x,
        r.y,
        &truncate_middle(&title, r.right().saturating_sub(x) as usize, cx.ell()),
        cx.st(PRIMARY, SIDEBAR).add_modifier(Modifier::BOLD),
        r.right().saturating_sub(x),
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
    let toggle = Rect::new(r.right().saturating_sub(3).max(r.x), r.y, r.width.min(3), 1);
    let toggle_style = if a.ui.show_archived {
        cx.st(MULTI, SIDEBAR).add_modifier(Modifier::BOLD)
    } else {
        cx.st(MUTED, SIDEBAR)
    };
    let icon = cx.t.control("archive");
    put(
        buf,
        toggle.x + toggle.width / 2,
        toggle.y,
        icon,
        toggle_style,
        1,
    );
    hits.add(toggle, Target::ArchivedFilter);
    let search = Rect::new(r.x, r.y + 1, r.width, 1);
    let mut style = cx.st(MUTED, SIDEBAR);
    if cx.hovered(search) {
        style = style.patch(cx.st(PRIMARY, SIDEBAR));
        clear(buf, search, style);
    }
    let glyph = cx.t.control("search");
    put(buf, r.x + 1, search.y, glyph, style, 1);
    let key = a
        .manifest
        .command("session.switch")
        .and_then(|c| c.tui_key())
        .unwrap_or("");
    let kw = width(key) as u16;
    let kx = search.right().saturating_sub(kw + 1);
    // `Search sessions`, or `Search` when the key hint leaves no room for it.
    let room = kx.saturating_sub(r.x + 4) as usize;
    let label = if room >= 15 {
        "Search sessions"
    } else {
        "Search"
    };
    put(
        buf,
        r.x + 3,
        search.y,
        &truncate(label, room, cx.ell()),
        style,
        room as u16,
    );
    put(buf, kx, search.y, key, style, kw);
    hits.add(search, Target::RailSearch);
    let list = Rect::new(r.x, r.y + 3, r.width, r.height.saturating_sub(3));
    if list.height == 0 {
        return;
    }
    let entries = a.side_entries();
    if entries.is_empty() {
        let msg = if a.state.model.projects.is_empty() {
            "No project yet. Add one with the + on the project rail."
        } else if a.ui.show_archived {
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
                put(buf, rr.x + 1, y, "+", style, 1);
                put(
                    buf,
                    rr.x + 3,
                    y,
                    "New session",
                    style,
                    rr.width.saturating_sub(3),
                );
                hits.add(rr, Target::NewSession);
            }
            SRow::Project(i) => {
                let name = entries[i].project.as_deref().unwrap_or("");
                put(
                    buf,
                    rr.x + 2,
                    y,
                    &truncate(name, rr.width.saturating_sub(4) as usize, cx.ell()),
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
                // The selected session takes the project rail's active treatment: the
                // lighter block and the left bar (`interactive.active`, `active_bar`).
                let bg = if selected {
                    Style::default().bg(cx.st(PRIMARY, ACTIVE).bg.unwrap_or_default())
                } else {
                    Style::default()
                };
                clear(buf, rr, cx.st(SECONDARY, SIDEBAR).patch(bg));
                if selected {
                    put(
                        buf,
                        rr.x,
                        y,
                        if cx.t.opts.ascii { "|" } else { "▏" },
                        cx.st(ACTIVE_BAR, SIDEBAR).patch(bg),
                        1,
                    );
                }
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
                let mut close_hit: Option<(Rect, String)> = None;
                if first {
                    if let Some((glyph, token)) = session_mark(cx, &s.id) {
                        put(buf, rr.x + 1, y, &glyph, cx.st(token, SIDEBAR).patch(bg), 1);
                    }
                    let token = if s.archived {
                        MUTED
                    } else if selected {
                        HIGHLIGHT
                    } else if a.ui.unread_sessions.contains(&s.id) {
                        PRIMARY
                    } else {
                        SECONDARY
                    };
                    let style = cx.st(token, SIDEBAR).patch(bg);
                    let age = text::age(now_secs(), s.updated);
                    let aw = width(&age) as u16;
                    // Hovering the two rows of a session offers `×` where the time is.
                    let close = (!s.archived && cx.hovered(Rect::new(rr.x, y, rr.width, 2)))
                        .then(|| Rect::new(inner_right.saturating_sub(2), y, 3, 1));
                    let inner = rr.width.saturating_sub(4);
                    // The time yields when the title would be squeezed too far.
                    let show_age = inner >= aw + 1 + 6;
                    let room = if show_age { inner - aw - 1 } else { inner };
                    draw_session_title(cx, buf, (rr.x + 3, y), room, s, style, bg);
                    if let Some(x) = close {
                        let hot = cx.hovered(x);
                        clear(
                            buf,
                            Rect::new(inner_right - aw.max(1), y, aw.max(1), 1),
                            cx.st(MUTED, SIDEBAR).patch(bg),
                        );
                        put(
                            buf,
                            x.x,
                            y,
                            if cx.t.opts.ascii { "x" } else { "\u{00d7}" },
                            cx.st(if hot { PRIMARY } else { MUTED }, SIDEBAR).patch(bg),
                            1,
                        );
                        close_hit = Some((x, s.id.clone()));
                    } else if show_age {
                        put(buf, inner_right - aw, y, &age, muted.patch(bg), aw);
                    }
                } else {
                    let (left, meta) = session_detail(cx, s, a.descendants(&s.id));
                    let inner = rr.width.saturating_sub(4) as usize;
                    let mw = width(&meta);
                    let show_meta = mw > 0 && inner >= mw + 1 + 6;
                    let room = if show_meta { inner - mw - 1 } else { inner };
                    let mut x = rr.x + 3;
                    let end = rr.x + 3 + room as u16;
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
                if let Some((x, id)) = close_hit {
                    hits.add(x, Target::ArchiveSession(id));
                }
            }
        }
    }
}

/// The mark slot of a session row from its attention data: waiting, error,
/// running (the spinner) or unread, in that order of precedence.
fn session_mark<'t>(cx: &Cx<'t>, id: &str) -> Option<(String, &'t str)> {
    let a = cx.app;
    match a.session_mark(id)? {
        "running" => Some((cx.t.spinner(a.frame).to_owned(), PRIMARY)),
        name => {
            let (glyph, token) = cx.t.attention(name);
            Some((glyph.to_owned(), token))
        }
    }
}

/// Line 2 of a session row: `⌥ branch` and the child count (`2 ↳`).
fn session_detail(
    cx: &Cx,
    s: &crate::api::Session,
    children: usize,
) -> (Vec<(String, Style)>, String) {
    let a = cx.app;
    let muted = cx.st(MUTED, SIDEBAR);
    let mut left = Vec::new();
    let branch = a
        .session_worktree(s)
        .and_then(|tree| tree["branch"].as_str())
        .filter(|branch| !branch.is_empty());
    if let Some(branch) = branch {
        let mark = if cx.t.opts.ascii { "~" } else { "⌥" };
        left.push((format!("{mark} {branch}"), muted));
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
        let hot_row = cx.hovered(rr);
        let right = format!("{n}  {}", text::age(now, p.updated));
        let rw = width(&right) as u16;
        let rx = rr.right().saturating_sub(rw + 1);
        // Hovering a project offers `×` where the count and time are.
        let close = hot_row.then(|| Rect::new(rr.right().saturating_sub(4), y, 3, 1));
        if let Some(x) = close {
            put(
                buf,
                x.x + 1,
                y,
                if cx.t.opts.ascii { "x" } else { "\u{00d7}" },
                cx.st(if cx.hovered(x) { PRIMARY } else { MUTED }, SIDEBAR)
                    .patch(style),
                1,
            );
        } else {
            put(buf, rx, y, &right, cx.st(MUTED, SIDEBAR), rw);
        }
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
        if let Some(x) = close {
            hits.add(x, Target::RemoveProject(id.clone()));
        }
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
    // `◆ needs input`: the glyph carries the state colour.
    let state = a.session_state(&s.id);
    if *y < r.bottom() {
        let (glyph, token) = match state {
            "needs input" => cx.t.attention("waiting"),
            "failed" => cx.t.attention("error"),
            "finished" => cx.t.attention("unread"),
            "running" => (cx.t.spinner(a.frame), PRIMARY),
            "archived" => {
                let (glyph, _, token) = cx.t.status("cancelled");
                (glyph, token)
            }
            _ => {
                let (glyph, _, token) = cx.t.status("done");
                (glyph, token)
            }
        };
        put(buf, r.x + 1, *y, "Status", cx.st(MUTED, SIDEBAR), KEY_W);
        let x = put(buf, r.x + VALUE_X, *y, glyph, cx.st(token, SIDEBAR), 2);
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
/// instruction file are added. Only the fallback: Core builds the real starting
/// context (`POST /api/draft/context`, PLAN K4), and these figures show while
/// it has not answered the current draft.
const PROMPT_BASE_TOKENS: u64 = 900;
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

/// The instruction file Core loaded for the draft, for its preview modal: the
/// text (read once per path, bounded) or why it is not available.
fn instruction_preview(a: &App) -> String {
    use std::cell::RefCell;
    use std::collections::HashMap;
    thread_local! {
        static CACHE: RefCell<HashMap<String, String>> = RefCell::new(HashMap::new());
    }
    let info = a.draft_context().map(|d| &d["snapshot"]["instructions"]);
    // Until Core answers the draft, look the way Core does: the project folder,
    // then its parents up to the Git root.
    let local = || {
        let project = a
            .current_project()
            .and_then(|p| a.state.model.projects.get(&p))?;
        let start = std::path::Path::new(&project.path);
        for dir in start.ancestors() {
            let file = dir.join("AGENTS.md");
            if file.is_file() {
                return Some(file.to_string_lossy().into_owned());
            }
            if dir.join(".git").exists() {
                break;
            }
        }
        None
    };
    let path = match info.and_then(|i| i["path"].as_str()) {
        Some(path) => path.to_owned(),
        None => match local() {
            Some(path) => path,
            None => return "No AGENTS.md found in the project folder or its parents up to the Git root.".to_owned(),
        },
    };
    let path = path.as_str();
    if info.is_some_and(|i| i["status"].as_str() != Some("loaded")) {
        return format!(
            "No instructions file was loaded ({}).\nLooked for {path} and its parents up to the Git root.",
            info.and_then(|i| i["status"].as_str()).unwrap_or("missing")
        );
    }
    CACHE.with(|cache| {
        cache
            .borrow_mut()
            .entry(path.to_owned())
            .or_insert_with(|| match std::fs::read_to_string(path) {
                Ok(text) => format!(
                    "{path}\n\n{}",
                    text.lines().take(400).collect::<Vec<_>>().join("\n")
                ),
                Err(e) => format!(
                    "{path}\n\nCore loaded this file, but this client cannot read it ({e})."
                ),
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
    let project = a
        .current_project()
        .and_then(|p| a.state.model.projects.get(&p));
    let core = a.draft_context();
    // Core says which file it loaded (the nearest AGENTS.md up to the Git root);
    // the client's own read of the project folder is only the fallback.
    let loaded_by_core = core.map(|d| &d["snapshot"]["instructions"]);
    let file_path = loaded_by_core
        .and_then(|i| i["path"].as_str().map(str::to_owned))
        .or_else(|| project.map(|p| format!("{}/AGENTS.md", p.path.trim_end_matches('/'))));
    let file = match loaded_by_core {
        Some(i) => (i["status"].as_str() == Some("loaded"))
            .then(|| (i["bytes"].as_u64().unwrap_or(0), String::new())),
        None => file_path.as_deref().and_then(instruction_file),
    };
    let file_tokens = file.as_ref().map_or(0, |(bytes, _)| bytes.div_ceil(4));
    let part = |name: &str| core.and_then(|d| d["parts"][name].as_u64());
    let file_tokens = part("instruction_file").unwrap_or(file_tokens);
    let prompt_tokens = part("instructions")
        .unwrap_or(PROMPT_BASE_TOKENS + (prompt.chars().count() as u64).div_ceil(4) + file_tokens);
    let tool_tokens = part("tools");
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
        tokens: tool_tokens,
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
    let total = core
        .and_then(|d| d["used"].as_u64())
        .unwrap_or(prompt_tokens + tool_tokens.unwrap_or(0));
    (sections, total)
}

/// The starting-context rows for the session that does not exist yet, memoized.
///
/// The rows are a pure function of the preview state (Core's draft plan, the
/// catalog, the agent file, the project inventories and the local switches) and
/// the layout; [`App::preview_key`] names exactly those inputs. Assembling them
/// re-cloned all of that on every frame, and this screen redraws on every
/// keystroke and animation tick.
fn preview_rows(
    cx: &Cx,
    layout: &paint::CtxLayout,
    window: Option<u64>,
) -> std::rc::Rc<Vec<paint::CtxRow>> {
    let a = cx.app;
    let key = a.preview_key(layout.width, layout.sep, layout.ell, layout.ascii, window);
    if let Some((cached, rows)) = a.preview_cache.borrow().as_ref() {
        if *cached == key {
            return std::rc::Rc::clone(rows);
        }
    }
    let (sections, total) = preview_sections(cx);
    let rows = std::rc::Rc::new(paint::context_rows(&sections, total, layout));
    *a.preview_cache.borrow_mut() = Some((key, std::rc::Rc::clone(&rows)));
    a.preview_builds.set(a.preview_builds.get() + 1);
    rows
}

fn draw_new_session_context(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) -> usize {
    let a = cx.app;
    if r.height < 4 || r.width < 24 {
        return 0;
    }
    let sep = cx.sep();
    let bold = |fg| cx.st(fg, PANEL).add_modifier(Modifier::BOLD);
    // The same box as the header after the first message: panel surface, one
    // padding row above and below the rows.
    let boxed = r;
    let r = Rect::new(
        r.x + 2,
        r.y + 1,
        r.width.saturating_sub(2),
        r.height.saturating_sub(2),
    );
    let end = r.right();

    let window = a
        .draft_context()
        .and_then(|d| d["window"].as_u64())
        .or_else(|| {
            a.state
                .models
                .iter()
                .find(|m| m["ref"].as_str() == Some(a.effective_model(None).as_str()))
                .and_then(|m| m["context"].as_u64())
        });
    let layout = paint::CtxLayout {
        width: (r.width as usize).min(98),
        sep: &sep,
        ell: cx.ell(),
        ascii: cx.t.opts.ascii,
        estimated: true,
        preview: true,
        window,
        columns: true,
    };
    let mut tip: Option<(u16, u16)> = None;
    let agent_prompt = a
        .state
        .agents
        .iter()
        .find(|ag| ag["name"].as_str() == Some(a.current_agent().as_str()))
        .and_then(|ag| ag["prompt"].as_str())
        .unwrap_or("Agent prompt unavailable.");
    let rows = preview_rows(cx, &layout, window);
    let total_rows = rows.len();
    let box_rows = (total_rows + 2).min(boxed.height as usize) as u16;
    clear(
        buf,
        Rect::new(boxed.x, boxed.y, boxed.width, box_rows),
        cx.st(MUTED, PANEL),
    );
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
                    .map(|item| format!("[{}] {}", if item.on { "x" } else { " " }, item.label))
                    .collect::<Vec<_>>()
                    .join("\n"),
                _ => instruction_preview(a),
            };
            hits.add(
                header,
                Target::ContextPreview {
                    title,
                    text: detail,
                },
            );
        }
        // Each switch is a cell: its `●` marker and name. Only the hovered or
        // selected cell is lifted, not the row.
        let mut cells: Vec<(usize, Rect, Rect)> = Vec::new();
        let mut cx_x = r.x;
        let mut marker_x = r.x;
        for (text, role) in row {
            match role {
                paint::CtxRole::ToggleOn(i)
                | paint::CtxRole::ToggleOff(i)
                | paint::CtxRole::ToggleLocked(i) => cells.push((
                    *i,
                    Rect::new(
                        marker_x,
                        y,
                        ((cx_x - marker_x) as usize + width(text) + 2) as u16,
                        1,
                    ),
                    // The name opens its info; the marker before it switches.
                    Rect::new(cx_x, y, (width(text) + 2) as u16, 1),
                )),
                _ => marker_x = cx_x,
            }
            cx_x += width(text) as u16;
        }
        let lifted = |index: usize, rect: Rect| {
            (a.ui.focus == Focus::Timeline && a.ui.context_cursor == index) || cx.hovered(rect)
        };
        for (_, rect, _) in cells.iter().filter(|(i, rect, _)| lifted(*i, *rect)) {
            clear(buf, *rect, cx.st(PRIMARY, RAISED));
        }
        let lifted_index = |index: Option<usize>| {
            index.is_some_and(|i| cells.iter().any(|(c, rect, _)| *c == i && lifted(i, *rect)))
        };
        for (text, role) in row {
            let own = match role {
                paint::CtxRole::ToggleOn(i)
                | paint::CtxRole::ToggleOff(i)
                | paint::CtxRole::ToggleLocked(i) => Some(*i),
                _ => None,
            };
            let on_cell = if own.is_some() {
                lifted_index(own)
            } else {
                // The marker before a switch belongs to its cell.
                cells
                    .iter()
                    .any(|(i, rect, _)| x >= rect.x && x < rect.right() && lifted(*i, *rect))
            };
            let bg = if on_cell { RAISED } else { PANEL };
            let mut style = match role {
                paint::CtxRole::Mark | paint::CtxRole::Meta | paint::CtxRole::Preview => {
                    cx.st(MUTED, PANEL)
                }
                paint::CtxRole::Title | paint::CtxRole::TotalLabel => bold(SECONDARY),
                paint::CtxRole::Tokens => cx.st(SECONDARY, PANEL),
                paint::CtxRole::ToggleOn(_) => cx.st(SECONDARY, bg),
                paint::CtxRole::ToggleOff(_) => {
                    cx.st(MUTED, bg).add_modifier(Modifier::CROSSED_OUT)
                }
                paint::CtxRole::ToggleLocked(_) => cx.st(MUTED, bg),
                paint::CtxRole::TotalFigure => cx.st(SECONDARY, PANEL),
            };
            if hovered_header || on_cell {
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
        for (index, rect, name) in &cells {
            hits.add(*rect, Target::ContextToggle(*index));
            hits.add(*name, Target::ContextInfo(*index));
        }
    }
    // Hovering an estimated count says so.
    if let Some((tx, ty)) = tip {
        let label = " estimated: characters ÷ 4 of Core's starting context ";
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
    if a.pending_first_prompt().is_some()
        || a.viewed().is_none()
        || (lines.is_empty() && a.messages().is_empty())
    {
        let empty = empty_state(cx);
        let busy = matches!(
            a.state.connection,
            ConnectionState::Starting | ConnectionState::Connecting
        );
        let head = if busy {
            format!("{} {}", cx.t.spinner(a.frame), empty.title)
        } else {
            empty.title.clone()
        };
        // An empty session whose starting context has not loaded shows the same preview.
        let preview = !busy && !a.state.model.projects.is_empty();
        if preview {
            let pending = a.pending_first_prompt();
            let mut sent = Vec::new();
            if let Some(p) = pending {
                sent.push("You · Sending…".to_owned());
                sent.extend(text::wrap(&p.text, inner.width.saturating_sub(2) as usize));
                if p.images > 0 {
                    sent.push(format!(
                        "[{} image{}]",
                        p.images,
                        if p.images == 1 { "" } else { "s" }
                    ));
                }
            }
            let reserved = if sent.is_empty() {
                0
            } else {
                (sent.len() + 1).min(inner.height.saturating_sub(4) as usize) as u16
            };
            // D10: the context is the view's content; no centred text.
            let total = draw_new_session_context(
                cx,
                buf,
                Rect::new(
                    inner.x,
                    inner.y + 1,
                    inner.width,
                    inner.height.saturating_sub(1 + reserved),
                ),
                hits,
            );
            if !sent.is_empty() {
                let y = (inner.y + total as u16 + 4).min(inner.bottom().saturating_sub(reserved));
                for (i, line) in sent
                    .iter()
                    .take(reserved.saturating_sub(1) as usize)
                    .enumerate()
                {
                    put(
                        buf,
                        inner.x + 2,
                        y + i as u16,
                        line,
                        cx.st(if i == 0 { SECONDARY } else { PRIMARY }, CANVAS),
                        inner.width.saturating_sub(2),
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
    let h = r.height as usize;
    // The first message sits at the top; a spacer keeps the page from scrolling short of it.
    let pin = a.first_pin_top(&timeline.blocks);
    let total = pin.map_or(lines.len(), |s| lines.len().max(s + h));
    let max_top = total.saturating_sub(h);
    let top = if let Some(s) = pin {
        s.min(max_top)
    } else if a.ui.scroll.follow {
        max_top
    } else {
        a.ui.scroll.top.min(max_top)
    };
    let mut paste_hits: Vec<(Rect, Target)> = Vec::new();
    for (i, line) in lines.iter().skip(top).take(h).enumerate() {
        draw_tline(cx, buf, r, inner, r.y + i as u16, line);
        if let Some(paste) = &line.paste {
            let content_width: usize = line.spans.iter().map(|span| width(&span.content)).sum();
            let rect = Rect::new(
                inner.x,
                r.y + i as u16,
                (content_width as u16).min(inner.width),
                1,
            );
            paste_hits.push((rect, Target::TimelinePaste(std::rc::Rc::clone(paste))));
        }
        for &(index, start, _, end) in &line.context_items {
            let start = inner.x.saturating_add(start as u16).min(inner.right());
            let end = inner.x.saturating_add((end + 2) as u16).min(inner.right());
            let cell = Rect::new(start, r.y + i as u16, end.saturating_sub(start), 1);
            if (a.ui.focus == Focus::Timeline && index == a.ui.context_cursor) || cx.hovered(cell) {
                if let Some(bg) = cx.t.color(RAISED) {
                    for x in cell.x..cell.right() {
                        buf[(x, cell.y)].set_bg(bg);
                    }
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
    // Image previews draw over the blank band rows reserved by the builder.
    // Their clicks are registered after the block targets so a preview wins.
    let mut preview_hits: Vec<(Rect, Target)> = Vec::new();
    if !timeline.previews.is_empty() {
        let mut viewer = cx.app.image_viewer.borrow_mut();
        for preview in &timeline.previews {
            if preview.start >= top + h || preview.start + preview.rows <= top {
                continue;
            }
            let x = inner.x.saturating_add(3 + preview.column as u16);
            let width = (preview.width as u16).min(inner.right().saturating_sub(x));
            if width == 0 {
                continue;
            }
            let y = r.y + preview.start.saturating_sub(top) as u16;
            let y0 = y.max(r.y);
            let y1 = (y + preview.rows as u16).min(r.bottom());
            if y1 <= y0 {
                continue;
            }
            if let Some(state) = viewer.protocol(&preview.key, &preview.data_url) {
                let image =
                    ratatui_image::StatefulImage::<ratatui_image::protocol::StatefulProtocol>::new(
                    )
                    .resize(ratatui_image::Resize::Fit(None));
                ratatui::widgets::StatefulWidget::render(
                    image,
                    Rect::new(x, y0, width, y1 - y0),
                    buf,
                    state,
                );
            }
            preview_hits.push((
                Rect::new(x, y0, width, y1 - y0),
                Target::ImagePreview {
                    name: preview.name.clone(),
                    data_url: preview.data_url.clone(),
                },
            ));
        }
    }
    // Every visible block is clickable; a click selects it and (except for a
    // user band's body) toggles it.
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
        // The left edge of a user band is its fold control; hovering it lifts
        // the strip and brightens the rail.
        if b.kind == crate::timeline::BlockKind::Turn {
            let toggle = Rect::new(inner.x, y0, 3.min(inner.width), y1 - y0);
            if cx.hovered(toggle) {
                if let Some(bg) = cx.t.color("interactive.hover") {
                    for y in toggle.y..toggle.bottom() {
                        for x in toggle.x..toggle.right() {
                            buf[(x, y)].set_bg(bg);
                        }
                    }
                }
                let rail = cx.t.structure("rail");
                let style = cx
                    .st("interactive.focus", "interactive.hover")
                    .add_modifier(Modifier::BOLD);
                for y in toggle.y..toggle.bottom() {
                    put(buf, toggle.x, y, rail, style, 1);
                }
            }
            hits.add(toggle, Target::BandToggle(bi));
        }
        if b.kind == crate::timeline::BlockKind::Code && b.end > 0 {
            let row = b.end - 1;
            if row >= top && row < top + h {
                let text: String = lines[row]
                    .spans
                    .iter()
                    .map(|s| s.content.as_ref())
                    .collect();
                if text.contains("◇ mermaid · copy source") {
                    hits.add(
                        Rect::new(inner.x, r.y + (row - top) as u16, inner.width, 1),
                        Target::CopyBlock(bi),
                    );
                }
            }
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
        // An open user band shows `copy` at the top-right of its top padding
        // row while hovered or focused; the text row below keeps `#n · time`.
        let focused = a.ui.focus == Focus::Timeline && a.ui.timeline_sel == Some(bi);
        if b.kind == crate::timeline::BlockKind::Turn
            && b.end - b.start >= 3
            && (cx.hovered(rect) || focused)
            && inner.width >= 8
        {
            let row = b.start;
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
    for (rect, target) in preview_hits {
        hits.add(rect, target);
    }
    for (rect, target) in paste_hits {
        hits.add(rect, target);
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
        for &(index, start, name, end) in &line.context_items {
            let start = inner.x.saturating_add(start as u16).min(inner.right());
            let name = inner.x.saturating_add(name as u16).min(inner.right());
            let end = inner.x.saturating_add((end + 2) as u16).min(inner.right());
            let y = r.y + (n - top) as u16;
            hits.add(
                Rect::new(start, y, end.saturating_sub(start), 1),
                Target::ContextToggle(index),
            );
            hits.add(
                Rect::new(name, y, end.saturating_sub(name), 1),
                Target::ContextInfo(index),
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
    draw_scroll_bar(cx, buf, r, top, total, h);
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

/// The timeline's scroll bar: one quiet cell in the right gutter, shown only
/// while the reader scrolls and then fading out. It never takes a column from
/// the content or a click from the page.
fn draw_scroll_bar(cx: &Cx, buf: &mut Buffer, r: Rect, top: usize, total: usize, h: usize) {
    let a = cx.app;
    if !a.ui.scrollbar || total <= h || r.width == 0 || r.height == 0 {
        return;
    }
    let Some(fading) = a.scrollbar_phase(a.clock) else {
        return;
    };
    let track = h as u32;
    let thumb = (track * track).div_ceil(total as u32).max(1).min(track) as u16;
    let max_top = (total - h).max(1) as u32;
    let travel = track.saturating_sub(thumb as u32);
    let offset = ((top as u32) * travel / max_top) as u16;
    let mut style = cx.st("interactive.scrollbar", CANVAS);
    if fading {
        style = style.add_modifier(Modifier::DIM);
    }
    for y in r.y + offset..r.y + offset + thumb {
        put(
            buf,
            r.right() - 1,
            y,
            cx.t.structure("rail_heavy"),
            style,
            1,
        );
    }
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
    let cards: Vec<(usize, usize, usize, usize)> = bands
        .iter()
        .map(|(_, b)| {
            // Attachments sit above the text; the pin starts after their labels
            // and preview rows, so no image is ever copied into the header.
            let text_start = timeline
                .previews
                .iter()
                .filter(|p| p.start > b.start && p.start < b.end)
                .map(|p| p.start + p.rows)
                .max()
                .unwrap_or(b.start + 1);
            (
                b.start,
                b.end - b.start,
                b.end.saturating_sub(text_start + 1).max(1),
                text_start - b.start,
            )
        })
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
                spans: timeline.lines[pin.first + k].spans.clone(),
                bg: timeline.lines[pin.first + k].bg,
                rail: timeline.lines[pin.first + k].rail,
                focus: false,
                context_section: None,
                context_items: Vec::new(),
                paste: timeline.lines[pin.first + k].paste.clone(),
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
    /// A review row's answer and whether the question was answered.
    Answer(String, bool),
    Opt(usize),
    /// One visible row of the custom answer field: its row in the input and
    /// the text on that row.
    Input(usize, String),
    Choice(usize),
    Hint,
}

type ApprovalParts = (String, Vec<(String, String)>, Option<crate::diff::Diff>);

/// A question in at most two rows; the second ends with the ellipsis when more
/// text follows, instead of clipping the whole question to one line.
fn ask_lines(text: &str, w: usize, ell: &str) -> Vec<String> {
    let mut lines = text::wrap(&text.replace('\n', " "), w.max(1));
    if lines.len() > 2 {
        lines.truncate(2);
        if let Some(last) = lines.last_mut() {
            let room = w.max(1).saturating_sub(width(ell));
            *last = format!("{}{}", truncate(last, room, ""), ell);
        }
    }
    lines
}

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
fn wait_rows(app: &App, w: u16, limit: usize, ell: &str) -> Vec<WRow> {
    let Some(item) = app.wait_focused() else {
        return Vec::new();
    };
    // The card's text column: `draw_waiting` starts three cells into the card
    // and stops two cells before its right edge.
    let inner = w.saturating_sub(9) as usize;
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
                if c.review() {
                    for (i, question) in c.questions.iter().enumerate() {
                        let text = format!("{}. {}", i + 1, json_str(&question["question"]));
                        rows.extend(ask_lines(&text, inner, ell).into_iter().map(WRow::Ask));
                        let answer = c.answer(i);
                        rows.push(WRow::Answer(
                            answer.clone().unwrap_or_else(|| "not answered".into()),
                            answer.is_some(),
                        ));
                    }
                } else if let Some(question) = c.question() {
                    rows.extend(
                        ask_lines(&json_str(&question["question"]), inner, ell)
                            .into_iter()
                            .map(WRow::Ask),
                    );
                    let opts = c.options().len();
                    let shown = opts.min(ASK_ROWS);
                    let cursor = c.pick().map_or(0, |p| p.cursor);
                    let top = cursor
                        .saturating_sub(shown.saturating_sub(1))
                        .min(opts.saturating_sub(shown));
                    rows.extend((top..top + shown).map(WRow::Opt));
                    if opts > 0 {
                        rows.push(WRow::Blank);
                    }
                    let field_w = inner.saturating_sub(2);
                    let (lines, (row, _)) = c
                        .pick()
                        .map(|p| p.input.layout(field_w))
                        .unwrap_or_else(|| (vec![String::new()], (0, 0)));
                    let first = visible_first(lines.len(), row, ASK_INPUT_ROWS);
                    let last = (first + ASK_INPUT_ROWS).min(lines.len());
                    for (i, line) in lines.iter().enumerate().take(last).skip(first) {
                        rows.push(WRow::Input(i, line.clone()));
                    }
                }
            }
            rows.push(WRow::Blank);
        }
    }
    rows.push(WRow::Hint);
    rows.push(WRow::Pad);
    // Fit: diff first, then blanks, pads, extra answer rows, table rows, option
    // rows, then review answers one at a time. Question headings stay.
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
            || drop_last(&mut rows, |r| matches!(r, WRow::Input(..)), 1)
            || drop_last(&mut rows, |r| matches!(r, WRow::Table(..)), 1)
            || drop_last(&mut rows, |r| matches!(r, WRow::Opt(_)), 1)
            || {
                let n = rows
                    .iter()
                    .filter(|r| matches!(r, WRow::Answer(..)))
                    .count();
                n > 0 && drop_last(&mut rows, |r| matches!(r, WRow::Answer(..)), n - 1)
            };
        if !done {
            break;
        }
    }
    rows
}

fn waiting_height(app: &App, w: u16) -> u16 {
    wait_rows(app, w, usize::MAX, "\u{2026}").len() as u16
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
    let ask = a.ask_current();
    let review = ask.as_ref().is_some_and(|c| c.review());
    let multi = ask.as_ref().is_some_and(|c| !c.review() && c.multiple());
    // A multi-select question uses the violet mark; the review page uses the
    // strongest text colour (white in the dark theme) for its rail.
    let mark = match (&w.kind, review, multi) {
        (WaitKind::Question, true, _) => PRIMARY,
        (WaitKind::Question, false, true) => MULTI,
        _ => ACCENT,
    };
    let card = Rect::new(r.x + 2, r.y, r.width.saturating_sub(4), r.height);
    clear(buf, card, cx.st(SECONDARY, PANEL));
    let rail_style = if focused {
        cx.st(mark, PANEL).add_modifier(Modifier::BOLD)
    } else {
        cx.st(mark, PANEL)
    };
    for y in card.y..card.bottom() {
        put(buf, card.x, y, cx.t.structure("rail_heavy"), rail_style, 1);
    }
    let x = card.x + 3;
    let end = card.right().saturating_sub(2);
    let inner_w = end.saturating_sub(x);
    let rows = wait_rows(a, r.width, r.height as usize, cx.ell());
    let (wg, _, wt) = cx.t.status("waiting");
    let head = if w.kind == WaitKind::Question {
        mark
    } else {
        wt
    };
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
            cx.st(mark, PANEL).add_modifier(Modifier::BOLD),
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
                let tx = put(buf, x, y, wg, cx.st(head, PANEL), 2);
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
                let mut sub = if a.ask_current().is_some() {
                    "waiting on you".to_string()
                } else {
                    format!("waiting on you{}{}", cx.sep(), mmss(a.wait_secs(&w.id)))
                };
                if let Some(c) = &ask {
                    if c.review() {
                        sub.push_str(&format!(
                            "{}review{}{} question{}",
                            cx.sep(),
                            cx.sep(),
                            c.total,
                            if c.total == 1 { "" } else { "s" }
                        ));
                    } else if c.total > 1 {
                        sub.push_str(&format!(
                            "{}question {} of {}",
                            cx.sep(),
                            c.page + 1,
                            c.total
                        ));
                    }
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
            WRow::Answer(text, answered) => {
                let st = if *answered {
                    cx.st(SECONDARY, PANEL)
                } else {
                    cx.st("status.warning", PANEL)
                };
                let head = if *answered {
                    review_arrow(cx)
                } else if cx.t.opts.ascii {
                    "-"
                } else {
                    "\u{00b7}"
                };
                let line = format!("  {head} {text}");
                put(
                    buf,
                    x,
                    y,
                    &truncate(&line, inner_w as usize, cx.ell()),
                    st,
                    inner_w,
                );
            }
            WRow::Opt(i) => {
                let Some(c) = a.ask_current() else { continue };
                let Some(o) = c.options().get(*i) else {
                    continue;
                };
                let on = c.pick().is_some_and(|p| p.chosen.contains(i));
                let label = json_str(&o["label"]);
                let tick = if c.multiple() {
                    if on {
                        "[x] "
                    } else {
                        "[ ] "
                    }
                } else {
                    ""
                };
                let line = format!("{tick}{} {}", (i + 1) % 10, label.trim_matches('"'));
                let mut st = if c.multiple() && on {
                    cx.st(MULTI, PANEL)
                } else {
                    cx.st(SECONDARY, PANEL)
                };
                if *i == c.pick().map_or(0, |p| p.cursor) {
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
            WRow::Input(_, text) => {
                let active = ask
                    .as_ref()
                    .is_some_and(|c| !c.sent && !c.review() && c.field());
                clear(buf, Rect::new(x, y, inner_w, 1), cx.st(MUTED, RAISED));
                put(
                    buf,
                    x,
                    y,
                    marker,
                    cx.st(if active { mark } else { MUTED }, RAISED),
                    1,
                );
                let tx = x + 2;
                let tw = inner_w.saturating_sub(2);
                if text.is_empty() {
                    let placeholder = if ask.as_ref().is_some_and(|c| c.options().is_empty()) {
                        "Type your answer\u{2026}".replace('\u{2026}', cx.ell())
                    } else {
                        "Add context, or type your own answer\u{2026}".replace('\u{2026}', cx.ell())
                    };
                    put(
                        buf,
                        tx,
                        y,
                        &truncate(&placeholder, tw as usize, cx.ell()),
                        cx.st(MUTED, RAISED),
                        tw,
                    );
                } else {
                    put(
                        buf,
                        tx,
                        y,
                        text,
                        cx.st(if active { PRIMARY } else { SECONDARY }, RAISED),
                        tw,
                    );
                }
                if writable && !ask.as_ref().is_some_and(|c| c.sent) {
                    hits.add(Rect::new(x, y, inner_w, 1), Target::WaitInput);
                }
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
                        Some(WaitButton::AllowSession),
                        "",
                    ),
                    ("Deny", "waiting.deny", Some(WaitButton::Deny), ""),
                    (
                        "Deny with a note",
                        "waiting.deny_note",
                        Some(WaitButton::DenyNote),
                        "",
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
                match (&w.kind, ask.as_ref()) {
                    (WaitKind::Question, Some(c)) if c.sent => {
                        hints.push((String::new(), "sending the answers...".into()));
                    }
                    (WaitKind::Question, Some(c)) if focused && c.review() => {
                        if c.total > 0 {
                            hints.push((leftright(cx).to_owned(), "pages".into()));
                        }
                        submit = Some(hints.len());
                        hints.push(("enter".into(), "submit".into()));
                        hints.push(("esc".into(), "stop".into()));
                    }
                    (WaitKind::Question, Some(c)) if focused => {
                        let field = c.field();
                        let has_text = c
                            .pick()
                            .is_some_and(|p| !p.input.expanded_text().trim().is_empty());
                        if c.total > 1 {
                            hints.push((format!("alt+{}", leftright(cx)), "pages".into()));
                        }
                        if field && has_text {
                            hints.push((leftright(cx).to_owned(), "caret".into()));
                        }
                        if !c.options().is_empty() {
                            hints.push((
                                key("waiting.custom", "alt+a"),
                                if field { "options" } else { "text" }.into(),
                            ));
                            if !field {
                                hints.push((updown(cx).to_owned(), "move".into()));
                            }
                        }
                        if c.multiple() && !field {
                            hints.push(("space".into(), "tick".into()));
                        }
                        hints.push((
                            "enter".into(),
                            if c.page + 1 < c.total {
                                "next"
                            } else {
                                "review"
                            }
                            .into(),
                        ));
                        // Keep the action hints whole at narrow widths; Esc is
                        // the manifest's global chain and is documented elsewhere.
                        if inner_w >= 60 {
                            hints.push(("esc".into(), "stop".into()));
                        }
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
    let spans = crate::theme::shimmer(
        "Working",
        a.frame,
        animate,
        if animate {
            cx.st(SECONDARY, CANVAS)
        } else {
            cx.st(PRIMARY, CANVAS)
        },
        cx.st("text.highlight", CANVAS),
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

/// The composer's collapsed pastes: one raised row each, with the text's size
/// and an explicit `Show` that opens the full paste.
fn draw_composer_pastes(
    cx: &Cx,
    buf: &mut Buffer,
    strip: Rect,
    pastes: &[(&str, &str)],
    hits: &mut HitMap,
) {
    if pastes.is_empty() || strip.width < 16 {
        return;
    }
    for (i, (marker, text)) in pastes.iter().enumerate() {
        let y = strip.y + i as u16 * 2;
        if y >= strip.bottom() {
            break;
        }
        let width = strip.width.saturating_sub(6);
        if width < 16 {
            break;
        }
        let height = if y + 1 < strip.bottom() { 2 } else { 1 };
        let rect = Rect::new(strip.x + 3, y, width, height);
        let hot = cx.hovered(rect);
        clear(buf, Rect::new(rect.x, y, width, 1), cx.st(PRIMARY, RAISED));
        let show_x = rect.right().saturating_sub(5);
        let x = put(
            buf,
            rect.x + 1,
            y,
            "Pasted text",
            cx.st(PRIMARY, RAISED),
            show_x.saturating_sub(rect.x + 1),
        );
        let lines = text.lines().count().max(1);
        let tokens = (text.chars().count() / 4).max(1);
        let detail = format!(" {} {lines} lines {} ~{tokens} tokens", cx.sep(), cx.sep());
        put(
            buf,
            x,
            y,
            &detail,
            cx.st(MUTED, RAISED),
            show_x.saturating_sub(x),
        );
        put(
            buf,
            show_x,
            y,
            "Show",
            if hot {
                cx.st(ACCENT, RAISED).add_modifier(Modifier::BOLD)
            } else {
                cx.st(ACCENT, RAISED)
            },
            4,
        );
        if height > 1 {
            let preview = text
                .lines()
                .find(|line| !line.trim().is_empty())
                .map(|line| line.trim_start())
                .unwrap_or("");
            let row = Rect::new(rect.x, y + 1, width, 1);
            clear(buf, row, cx.st(MUTED, RAISED));
            put(
                buf,
                row.x + 1,
                row.y,
                &truncate(preview, width.saturating_sub(2) as usize, cx.ell()),
                cx.st(MUTED, RAISED),
                width.saturating_sub(1),
            );
        }
        hits.add(
            rect,
            Target::TimelinePaste(std::rc::Rc::new(crate::timeline::PasteText {
                marker: (*marker).to_owned(),
                text: (*text).to_owned(),
            })),
        );
    }
}

/// The composer's attachment strip: a muted name row over a square thumbnail
/// for every image, wrapping when the width runs out.
fn draw_composer_images(
    cx: &Cx,
    buf: &mut Buffer,
    strip: Rect,
    images: &[crate::images::Image],
    hits: &mut HitMap,
) {
    if images.is_empty() || strip.height == 0 || strip.width < 8 {
        return;
    }
    let gap = 2u16;
    let cols = crate::images::COMPOSER_THUMB_COLS as u16;
    let rows = crate::images::COMPOSER_THUMB_ROWS as u16;
    let available = strip.width.saturating_sub(6);
    let per_line = (available + gap)
        .checked_div(cols + gap)
        .unwrap_or(0)
        .max(1) as usize;
    let mut viewer = cx.app.image_viewer.borrow_mut();
    for (i, image) in images.iter().enumerate() {
        let x = strip.x + 3 + (i % per_line) as u16 * (cols + gap);
        let y = strip.y + (i / per_line) as u16 * (1 + rows);
        if y + 1 + rows > strip.bottom() {
            break;
        }
        put(
            buf,
            x,
            y,
            &truncate(&image.name, (cols + gap - 1) as usize, cx.ell()),
            cx.st(MUTED, CANVAS),
            cols + gap - 1,
        );
        let rect = Rect::new(x, y + 1, cols, rows);
        if let Some(state) = viewer.protocol(&format!("composer:{i}"), &image.data_url) {
            let picture =
                ratatui_image::StatefulImage::<ratatui_image::protocol::StatefulProtocol>::new()
                    .resize(ratatui_image::Resize::Fit(None));
            ratatui::widgets::StatefulWidget::render(picture, rect, buf, state);
        }
        hits.add(rect, Target::ComposerImage(i));
    }
}

fn draw_composer(cx: &Cx, buf: &mut Buffer, r: Rect, hits: &mut HitMap) {
    if cx.app.manager_debug_view() {
        put(
            buf,
            r.x + 2,
            r.y + 1,
            "Manager session · read-only debug view",
            cx.st(SECONDARY, PANEL),
            r.width.saturating_sub(4),
        );
        return;
    }
    let inputs = cx.app.pending_inputs();
    let composer = composer_without_pending(r, inputs.len());
    let extra = composer.y - r.y;
    if extra > 0 {
        let visible = (extra - 2) as usize;
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
        // One filled card, aligned with the composer panel below it.
        let card = Rect::new(r.x + 2, r.y, r.width.saturating_sub(4), extra);
        clear(buf, card, cx.st(MUTED, PANEL));
        // The message text starts where the composer's input starts; the
        // `Q`/`S` marker sits two cells left of it, one cell of air between.
        let input_x = composer_inner_with(
            composer,
            cx.app.composer_ref().images.len(),
            cx.app.composer_ref().pastes().len(),
        )
        .x;
        let mark_x = input_x.saturating_sub(2);
        let end = card.right().saturating_sub(2);
        for (line, (idx, item)) in inputs
            .iter()
            .enumerate()
            .skip(start)
            .take(visible)
            .enumerate()
        {
            let text: String = item["parts"]
                .as_array()
                .and_then(|parts| parts.iter().find(|p| p["type"] == "text"))
                .map(|p| {
                    if p["collapsed"] == true {
                        crate::timeline::paste_marker(p["text"].as_str().unwrap_or(""))
                    } else {
                        p["text"].as_str().unwrap_or("").to_owned()
                    }
                })
                .unwrap_or_else(|| "[image]".to_owned());
            let text = text.lines().next().unwrap_or("");
            let mode = if item["mode"] == "steer" { "S" } else { "Q" };
            let selected = cx.app.ui.pending_selected == Some(idx);
            let y = r.y + 1 + line as u16;
            put(
                buf,
                mark_x,
                y,
                mode,
                cx.st(ACCENT, PANEL).add_modifier(Modifier::BOLD),
                1,
            );
            let held_suffix = if held { " held" } else { "" };
            let hint = if selected {
                "  enter edit · backspace remove · s steer · shift+↑/↓ move"
            } else {
                ""
            };
            let text_end = end.saturating_sub(width(held_suffix) as u16).max(input_x);
            let mut x = put(
                buf,
                input_x,
                y,
                text,
                cx.st(if selected { PRIMARY } else { MUTED }, PANEL),
                text_end.saturating_sub(input_x),
            );
            x = put(
                buf,
                x,
                y,
                held_suffix,
                cx.st(MUTED, PANEL),
                end.saturating_sub(x),
            );
            put(buf, x, y, hint, cx.st(MUTED, PANEL), end.saturating_sub(x));
        }
    }
    let r = composer;
    if r.height < 8 || r.width < 8 {
        return;
    }
    draw_activity_line(cx, buf, r);
    let full_r = r;
    let r = composer_content(r);
    let a = cx.app;
    let base = cx.st(PRIMARY, PANEL);
    let comp_view = a.composer_view();
    let comp = &*comp_view;
    let pastes = comp.pastes();
    let card = composer_card(r, comp.images.len(), pastes.len());
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
    card_edge(
        cx,
        buf,
        panel,
        panel.bottom() - 1,
        cx.t.structure("card_bottom"),
    );
    let inner = composer_inner_with(full_r, comp.images.len(), pastes.len());
    let input_height = inner.height as usize;
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

    // Collapsed pastes first, then the square attachment thumbnails with their names above.
    let paste_rows = composer_paste_rows(pastes.len()).min(card.y.saturating_sub(r.y));
    draw_composer_pastes(
        cx,
        buf,
        Rect::new(r.x, r.y, r.width, paste_rows),
        &pastes,
        hits,
    );
    let images_y = r.y + paste_rows;
    draw_composer_images(
        cx,
        buf,
        Rect::new(r.x, images_y, r.width, card.y.saturating_sub(images_y)),
        &comp.images,
        hits,
    );
    // input
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
            || visible_first(rows.len(), cr, input_height),
            |(_, top)| top,
        );
        for (i, row) in rows.iter().skip(first).take(input_height).enumerate() {
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
        if rows.len() > input_height {
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
    let shell_mode = comp.text().starts_with('!');
    let mut agent_label = if shell_mode {
        "Shell".to_owned()
    } else {
        text::capitalize(agent_name)
    };
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
            + width(&cx.sep())
            + width(&model_label)
            + if effort_on {
                width(&cx.sep()) + width(effort)
            } else {
                0
            }
            + if provider_on && pending.is_none() && !model_id.is_empty() {
                1 + width(&provider)
            } else {
                0
            };
        if left_width + 2 + right_width(meter_on) <= avail {
            break;
        }
        if effort_on {
            effort_on = false;
        } else if provider_on && pending.is_none() && !model_id.is_empty() {
            provider_on = false;
        } else if meter_on {
            meter_on = false;
        } else {
            // Last resort: keep the agent and the figure, shorten the model.
            let room = avail
                .saturating_sub(width(&agent_label) + width(&cx.sep()) + 2 + right_width(false));
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
            if width(&agent_label) + width(&cx.sep()) + width(&model_label) + 2 + right_width(false)
                > avail
            {
                let room = avail.saturating_sub(
                    width(&model_label) + width(&cx.sep()) + 2 + right_width(false),
                );
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
    // Recording: a red square blinks left of the agent name, which does not move.
    if matches!(
        a.ui.dictation.phase,
        crate::dictation::Phase::Recording | crate::dictation::Phase::Stopping
    ) {
        let lit = cx.t.opts.reduced_motion || (a.frame / 4).is_multiple_of(2);
        let style = cx.st("status.error", PANEL);
        let style = if lit {
            style
        } else {
            style.add_modifier(Modifier::DIM)
        };
        let mark = if cx.t.opts.ascii { "#" } else { "■" };
        put(buf, panel.x + 1, cy, mark, style, 1);
    }
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
                provider.clone(),
                Target::ComposerModel,
                Modifier::empty(),
                MUTED,
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
            if shell_mode && index != 0 {
                continue;
            }
            if index == 3 && !effort_on {
                continue;
            }
            if index == 2 && (!provider_on || pending.is_some() || model_id.is_empty()) {
                continue;
            }
            if x >= selector_end {
                break;
            }
            if !first {
                let separator = if index == 1 || index == 3 {
                    cx.sep()
                } else {
                    " ".to_owned()
                };
                x = put(
                    buf,
                    x,
                    cy,
                    &separator,
                    cx.st(MUTED, PANEL),
                    selector_end - x,
                );
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
            let style = if shell_mode {
                cx.st("status.warning", PANEL).add_modifier(Modifier::BOLD)
            } else if target == Target::ComposerAgent {
                cx.st(token, PANEL)
                    .patch(cx.t.agent_style(agent_name))
                    .add_modifier(modifier)
            } else {
                cx.st(token, PANEL).add_modifier(modifier)
            };
            x = put(buf, x, cy, &label, style, room);
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
    let y = r.bottom() - 1;
    if let Some(progress) = &cx.app.ui.dictation.download {
        let percent = progress
            .bytes_done
            .saturating_mul(100)
            .checked_div(progress.bytes_total)
            .map(|value| format!("{}%", value.min(100)));
        let line = match percent {
            Some(percent) => format!(
                "Downloading speech model · {percent} · {} / {}",
                human_bytes(progress.bytes_done),
                human_bytes(progress.bytes_total)
            ),
            None => "Downloading speech model …".into(),
        };
        put(buf, start, y, &line, cx.st(ACCENT, CANVAS), width);
        return;
    }
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
            y,
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

fn human_bytes(bytes: u64) -> String {
    if bytes >= 1_000_000 {
        format!("{:.0} MB", bytes as f64 / 1_000_000.0)
    } else if bytes >= 1_000 {
        format!("{:.0} KB", bytes as f64 / 1_000.0)
    } else {
        format!("{bytes} B")
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
    // Live kernels and running background shells, centred in the strip. A
    // kernel chip is absent when no kernel exists; shells appear only while one
    // is running. Clicking the chip opens the full list.
    let (kernels, shells) = a.resource_counts();
    let mut parts = Vec::new();
    if kernels > 0 {
        parts.push(format!(
            "{kernels} {}",
            if kernels == 1 { "kernel" } else { "kernels" }
        ));
    }
    if shells > 0 {
        parts.push(format!(
            "{shells} {}",
            if shells == 1 { "shell" } else { "shells" }
        ));
    }
    let chip = if parts.is_empty() {
        String::new()
    } else {
        format!(" {} ", parts.join(&cx.sep()))
    };
    let chip_w = u16::try_from(width(&chip)).unwrap_or(u16::MAX);
    let available = end.saturating_sub(start);
    let show_chip = chip_w > 2 && available > chip_w.saturating_add(8);
    let chip_x = start + available.saturating_sub(chip_w) / 2;
    let left_end = if show_chip {
        chip_x.saturating_sub(2)
    } else {
        end
    };
    let reserved = branch.as_ref().map_or(0, |b| width(b) + 4) + if linked { 3 } else { 0 };
    let room = (left_end.saturating_sub(start) as usize).saturating_sub(reserved);
    let mut x = put(
        buf,
        start,
        y,
        &truncate_middle(&path, room, cx.ell()),
        cx.st(MUTED, CANVAS),
        left_end.saturating_sub(start),
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
            left_end.saturating_sub(x),
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
            left_end.saturating_sub(x),
        );
    }
    if show_chip {
        put(
            buf,
            chip_x,
            y,
            &chip,
            cx.st(ACCENT, "interactive.selection")
                .add_modifier(Modifier::BOLD),
            chip_w,
        );
        hits.add(Rect::new(chip_x, y, chip_w, 1), Target::ComposerResources);
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

/// One segmented Settings value, fitted to the cells left of the row's label.
struct Segments {
    labels: Vec<String>,
    selected: usize,
    total: u16,
}

/// The row's segmented control, or `None` when it is not a choice/switch or the
/// options cannot fit. Padded options are preferred; bare ones keep small
/// terminals usable.
fn segment_view(cx: &Cx, row: &crate::manifest::Row, room: u16) -> Option<Segments> {
    let (options, current) = cx.app.settings_segments(row)?;
    if options.len() < 2 {
        return None;
    }
    let selected = options
        .iter()
        .position(|option| *option == current)
        .unwrap_or(0);
    for padded in [true, false] {
        let labels: Vec<String> = options
            .iter()
            .map(|option| {
                if padded {
                    format!(" {option} ")
                } else {
                    (*option).to_owned()
                }
            })
            .collect();
        let total: usize = labels.iter().map(|label| width(label)).sum();
        if total <= room as usize {
            return Some(Segments {
                labels,
                selected,
                total: total as u16,
            });
        }
    }
    None
}

/// Draw the segments, selected one on the NSH selection band, one click per option.
fn draw_segments(
    cx: &Cx,
    buf: &mut Buffer,
    x: u16,
    y: u16,
    seg: &Segments,
    row: usize,
    hits: &mut HitMap,
) {
    let mut x = x;
    for (option, label) in seg.labels.iter().enumerate() {
        let w = width(label) as u16;
        let (token, bg) = if option == seg.selected {
            (PRIMARY, "interactive.selection")
        } else {
            (SECONDARY, PANEL)
        };
        let mut style = cx.st(token, bg);
        if option == seg.selected {
            style = style.add_modifier(Modifier::BOLD);
        }
        put(buf, x, y, label, style, w);
        hits.add(
            Rect::new(x, y, w, 1),
            Target::SettingsSegment { row, option },
        );
        x = x.saturating_add(w);
    }
}

/// The value shown for a settings row, and whether it is live (wired).
fn settings_value(cx: &Cx, row: &crate::manifest::Row) -> (String, bool) {
    let a = cx.app;
    let live = match row.id.as_str() {
        "tools.defaults" => {
            let disabled: Vec<&str> = a
                .state
                .config
                .as_ref()
                .and_then(|c| c["values"]["tools"]["disabled"].as_array())
                .map(|list| list.iter().filter_map(|v| v.as_str()).collect())
                .unwrap_or_default();
            let off = crate::app::TOOLS
                .iter()
                .filter(|name| disabled.contains(name))
                .count();
            Some(if off == 0 {
                "all on".to_owned()
            } else {
                format!("{off} off")
            })
        }
        "tools.search_status" => a.search_status_lines().into_iter().next(),
        "tools.search_start" => Some("enter".into()),
        "manager.enabled" | "manager.mode" | "manager.model" | "manager.proactive" => {
            a.manager_setting_value(&row.id)
        }
        "manager.privacy" => Some("enter".into()),
        "dictation.push_to_talk" => Some("enter".into()),
        "about.inspector" => Some("enter".into()),
        "appearance.theme" => Some(match a.theme.opts.mode {
            crate::theme::Mode::Dark => "dark".into(),
            crate::theme::Mode::Light => "light".into(),
        }),
        "appearance.monochrome" => Some(on_off(a.theme.mono())),
        "appearance.animations" => Some(on_off(!a.theme.opts.reduced_motion)),
        "notifications.sound" => Some(on_off(a.ui.sound)),
        "notifications.terminal" => Some(a.ui.notify.label().into()),
        "appearance.sidebar" => Some(if a.ui.left_open { "full" } else { "rail" }.into()),
        "appearance.scrollbar" => Some(on_off(a.ui.scrollbar)),
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
        "context.title_models" => a.state.config.as_ref().map(|c| {
            let list: Vec<&str> = c["values"]["context"]["title_models"]
                .as_array()
                .map(|l| l.iter().filter_map(|v| v.as_str()).collect())
                .unwrap_or_default();
            if list.is_empty() {
                "none · first words".to_owned()
            } else {
                list.join(", ")
            }
        }),
        "context.usage_refresh_minutes" => a
            .state
            .config
            .as_ref()
            .and_then(|c| c["values"]["usage"]["refresh_minutes"].as_u64())
            .map(|value| value.to_string()),
        "context.handoff_percent" => a
            .state
            .config
            .as_ref()
            .and_then(|c| c["values"]["context"]["handoff_percent"].as_u64())
            .map(|value| {
                if value == 0 {
                    "off".to_owned()
                } else {
                    format!("{value}%")
                }
            }),
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
        "providers.page" => Some(a.page_summary("providers")),
        "agents.page" => Some(a.page_summary("agents")),
        "agents.default_effort" | "agents.default_root" | "tools.default_permission" => {
            let key = match row.id.as_str() {
                "agents.default_root" => "agent",
                "tools.default_permission" => "permission",
                _ => "effort",
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
        "dictation.stt_model" => Some(a.voice_info_value(crate::actions::VoiceTarget::Stt)),
        "dictation.tts_model" => Some(a.voice_info_value(crate::actions::VoiceTarget::Tts)),
        "dictation.stt_download"
        | "dictation.stt_remove"
        | "dictation.tts_download"
        | "dictation.tts_remove" => Some("enter".into()),
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

/// Selected Settings rows fill their entire hit area with a neutral gray band.
fn row_band(cx: &Cx, buf: &mut Buffer, r: Rect, token: &str, focused: bool) -> Style {
    let style = cx
        .st(token, "interactive.settings_selection")
        .add_modifier(Modifier::BOLD);
    clear(buf, r, style);
    if focused {
        put(
            buf,
            r.x,
            r.y,
            cx.t.structure("focus_bar"),
            cx.st(
                "interactive.settings_focus",
                "interactive.settings_selection",
            ),
            1,
        );
    }
    style
}

/// Settings: a search row, a section rail on the left and the selected
/// section, provider list or agent page on the right. Both clients use the
/// same two-pane structure.
fn draw_settings(cx: &Cx, buf: &mut Buffer, area: Rect, hits: &mut HitMap) {
    use crate::app::{PageLine, SettingsItem, Tone};
    let a = cx.app;
    hits.add(area, Target::Scrim);
    let r = settings_box(area);
    if r.width < 20 || r.height < 8 {
        return;
    }
    let page = a.settings_open_page();
    let title = match page.as_deref() {
        Some(p) => match p.strip_prefix("agents/") {
            Some(name) => format!("Settings \u{203a} Agents \u{203a} {name}"),
            None => {
                let label = a
                    .manifest
                    .settings
                    .section
                    .iter()
                    .find(|s| s.id == p)
                    .map_or(p.to_owned(), |s| s.label.clone());
                format!("Settings \u{203a} {label}")
            }
        },
        None => "Settings".into(),
    };
    let m = modal(cx, buf, area, r, &title, hits);
    let (x, w) = (m.x, m.w);
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

    let main_top = sy + 2;
    let nav_w = 22u16.min(m.w.saturating_sub(24));
    let content_x = m.x + nav_w + 2;
    let content_w = m.w.saturating_sub(nav_w + 2);
    let page_lines = page.as_deref().map(|_| a.settings_page_lines());
    let page_items = page.as_deref().map(|_| a.settings_page_items());

    // The selected row's note (or the page's detail) sits above the key hints.
    let details: Vec<String> = match (&page, &page_lines) {
        (Some(_), Some(_)) => a
            .settings_page_detail()
            .map(|d| vec![d])
            .unwrap_or_default(),
        _ => {
            let items = a.settings_items();
            let rows = a.settings_rows();
            let sel = a.ui.settings_row.min(rows.len().saturating_sub(1));
            rows.get(sel)
                .and_then(|index| items.get(*index))
                .and_then(|item| match item {
                    SettingsItem::Row {
                        row,
                        section_reason,
                    } => Some(a.settings_details(row, section_reason.as_deref())),
                    SettingsItem::Heading(_) | SettingsItem::Note(_) => None,
                })
                .unwrap_or_default()
        }
    }
    .into_iter()
    .flat_map(|line| text::wrap(&line, content_w as usize))
    .collect::<Vec<_>>();
    let mut details = details;
    // Voice status and a long note can run past the space above the hints;
    // keep every line up to a readable bound and say how many were left out.
    if details.len() > 8 {
        let hidden = details.len() - 7;
        details.truncate(7);
        details.push(format!("\u{2026} {hidden} more lines"));
    }
    let detail_count = details.len().max(1);
    let detail_start = m.hint_y.saturating_sub(detail_count as u16);
    let list = Rect::new(m.x, main_top, m.w, detail_start.saturating_sub(main_top));
    let list_h = list.height as usize;

    // Left: the section rail.
    let sections = a.settings_sections();
    let nav_sel = a
        .settings_section_index()
        .min(sections.len().saturating_sub(1));
    let nav_visible = list_h.max(1);
    let nav_top = (nav_sel + 1).saturating_sub(nav_visible);
    if sections.is_empty() {
        put(buf, x, list.y, "No sections", cx.st(MUTED, RAISED), nav_w);
    }
    for (i, section) in sections.iter().enumerate().skip(nav_top).take(nav_visible) {
        let y = list.y + (i - nav_top) as u16;
        let selected = i == nav_sel;
        let style = if selected {
            row_band(
                cx,
                buf,
                Rect::new(x, y, nav_w, 1),
                PRIMARY,
                a.ui.settings_nav_focus,
            )
        } else {
            cx.st(SECONDARY, RAISED)
        };
        put(
            buf,
            x + 2,
            y,
            &truncate(&section.label, nav_w.saturating_sub(4) as usize, cx.ell()),
            style,
            nav_w.saturating_sub(2),
        );
        hits.add(Rect::new(x, y, nav_w, 1), Target::SettingsSection(i));
    }
    let below = sections
        .len()
        .saturating_sub(nav_top.saturating_add(nav_visible));
    if below > 0 && list_h > 0 {
        put(
            buf,
            x + 2,
            list.y + list.height.saturating_sub(1),
            &format!("\u{2193} {below} more"),
            cx.st(MUTED, RAISED),
            nav_w.saturating_sub(2),
        );
    }
    // A rule between the rail and the pane.
    let border = cx.t.border_set();
    for y in main_top..m.hint_y.saturating_sub(1) {
        put(
            buf,
            m.x + nav_w,
            y,
            border.vertical_left,
            cx.st("border.default", RAISED),
            1,
        );
    }

    // Right: the selected section or page.
    if let (Some(_), Some(lines), Some(items)) = (&page, &page_lines, &page_items) {
        let sel = items
            .get(a.ui.settings_page_row.min(items.len().saturating_sub(1)))
            .copied();
        let mut display: Vec<Option<usize>> = Vec::new();
        for (i, line) in lines.iter().enumerate() {
            if matches!(line, PageLine::Heading(_) | PageLine::Title(..)) && i > 0 {
                display.push(None);
            }
            display.push(Some(i));
        }
        let sel_row = sel
            .and_then(|i| display.iter().position(|l| *l == Some(i)))
            .unwrap_or(0);
        let top = (sel_row + 1).saturating_sub(list_h);
        let tone = |t: Tone| match t {
            Tone::Muted => MUTED,
            Tone::Secondary => SECONDARY,
            Tone::Warning => "status.warning",
            Tone::Error => "status.error",
        };
        for (li, row) in display.iter().enumerate().skip(top).take(list_h) {
            let y = list.y + (li - top) as u16;
            let Some(i) = row else { continue };
            match &lines[*i] {
                PageLine::Heading(label) => {
                    put(
                        buf,
                        content_x,
                        y,
                        &truncate(&label.to_uppercase(), content_w as usize, cx.ell()),
                        cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD),
                        content_w,
                    );
                }
                PageLine::Title(label, detail) => {
                    put(
                        buf,
                        content_x,
                        y,
                        &truncate(&label.to_uppercase(), content_w as usize, cx.ell()),
                        cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD),
                        content_w,
                    );
                    let dw = (width(detail) as u16).min(content_w / 2);
                    put(
                        buf,
                        content_x + content_w - dw,
                        y,
                        &truncate(detail, dw as usize, cx.ell()),
                        cx.st(MUTED, RAISED),
                        dw,
                    );
                }
                PageLine::Text(text, t) => {
                    put(
                        buf,
                        content_x,
                        y,
                        &truncate(text, content_w as usize, cx.ell()),
                        cx.st(tone(*t), RAISED),
                        content_w,
                    );
                }
                PageLine::Item {
                    label,
                    value,
                    tone: t,
                    action,
                } => {
                    let selected = Some(*i) == sel;
                    let position = items.iter().position(|p| p == i).unwrap_or(0);
                    let (value, disabled) = match action {
                        crate::app::PageAction::Setting(row) => {
                            let reason = a.settings_reason(row, None);
                            let shown = if reason.is_some() {
                                "unavailable".to_owned()
                            } else {
                                settings_value(cx, row).0
                            };
                            (shown, reason.is_some())
                        }
                        _ => (value.clone(), false),
                    };
                    let is_link = matches!(action, crate::app::PageAction::OpenUrl(_));
                    let token = if disabled { MUTED } else { PRIMARY };
                    let mut style = if selected {
                        row_band(
                            cx,
                            buf,
                            Rect::new(content_x, y, content_w, 1),
                            token,
                            !a.ui.settings_nav_focus,
                        )
                    } else {
                        cx.st(token, RAISED)
                    };
                    if is_link {
                        style = style.add_modifier(Modifier::UNDERLINED);
                    }
                    let mut vstyle = cx.st(
                        if disabled { MUTED } else { tone(*t) },
                        if selected {
                            "interactive.settings_selection"
                        } else {
                            RAISED
                        },
                    );
                    if is_link {
                        vstyle = vstyle.add_modifier(Modifier::UNDERLINED);
                    }
                    if selected {
                        vstyle = vstyle.add_modifier(Modifier::BOLD);
                    }
                    let lw = content_w.saturating_sub(2);
                    let vw = (width(&value) as u16).min(lw.saturating_sub(12));
                    put(
                        buf,
                        content_x + 2,
                        y,
                        &truncate(label, (lw - vw).saturating_sub(2) as usize, cx.ell()),
                        style,
                        (lw - vw).saturating_sub(2),
                    );
                    put(
                        buf,
                        content_x + content_w - vw,
                        y,
                        &truncate(&value, vw as usize, cx.ell()),
                        vstyle,
                        vw,
                    );
                    hits.add(
                        Rect::new(content_x, y, content_w, 1),
                        Target::SettingsPageRow(position),
                    );
                }
            }
        }
    } else {
        let items = a.settings_items();
        let rows = a.settings_rows();
        let sel = a.ui.settings_row.min(rows.len().saturating_sub(1));
        let sel_item = rows.get(sel).copied();
        let mut display: Vec<Option<usize>> = Vec::new();
        for (i, item) in items.iter().enumerate() {
            if matches!(item, SettingsItem::Heading(_)) && i > 0 {
                display.push(None);
            }
            display.push(Some(i));
        }
        let sel_row = sel_item
            .and_then(|i| display.iter().position(|l| *l == Some(i)))
            .unwrap_or(0);
        let top = (sel_row + 1).saturating_sub(list_h);
        if items.is_empty() {
            put(
                buf,
                content_x,
                list.y,
                if query.is_empty() {
                    "This section has no settings."
                } else {
                    "No settings match."
                },
                cx.st(MUTED, RAISED),
                content_w,
            );
        }
        for (li, line) in display.iter().enumerate().skip(top).take(list_h) {
            let y = list.y + (li - top) as u16;
            let Some(i) = line else { continue };
            match &items[*i] {
                SettingsItem::Heading(label) => {
                    put(
                        buf,
                        content_x,
                        y,
                        &truncate(&label.to_uppercase(), content_w as usize, cx.ell()),
                        cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD),
                        content_w,
                    );
                }
                SettingsItem::Note(text) => {
                    put(
                        buf,
                        content_x,
                        y,
                        &truncate(text, content_w as usize, cx.ell()),
                        cx.st("status.warning", RAISED),
                        content_w,
                    );
                }
                SettingsItem::Row {
                    row,
                    section_reason,
                } => {
                    let position = rows.iter().position(|p| p == i).unwrap_or(0);
                    let selected = Some(*i) == sel_item;
                    let disabled = a.settings_reason(row, section_reason.as_deref()).is_some();
                    let (value, wired) = settings_value(cx, row);
                    let token = if disabled || !wired { MUTED } else { PRIMARY };
                    let style = if selected {
                        row_band(
                            cx,
                            buf,
                            Rect::new(content_x, y, content_w, 1),
                            token,
                            !a.ui.settings_nav_focus,
                        )
                    } else {
                        cx.st(token, RAISED)
                    };
                    let lw = content_w.saturating_sub(2);
                    // The whole row takes a click first; a segment drawn after
                    // it wins over its own cells.
                    hits.add(
                        Rect::new(content_x, y, content_w, 1),
                        Target::SettingsRow(position),
                    );
                    let segments = if disabled {
                        None
                    } else {
                        segment_view(cx, row, lw)
                    };
                    if let Some(seg) = segments {
                        let label_room = lw.saturating_sub(seg.total + 2);
                        put(
                            buf,
                            content_x + 2,
                            y,
                            &truncate(&row.label, label_room as usize, cx.ell()),
                            style,
                            label_room,
                        );
                        draw_segments(
                            cx,
                            buf,
                            content_x + content_w - seg.total,
                            y,
                            &seg,
                            position,
                            hits,
                        );
                    } else {
                        let mut vstyle = cx.st(
                            if wired && !disabled { SECONDARY } else { MUTED },
                            if selected {
                                "interactive.settings_selection"
                            } else {
                                RAISED
                            },
                        );
                        if selected {
                            vstyle = vstyle.add_modifier(Modifier::BOLD);
                        }
                        let value = if disabled {
                            "unavailable".to_owned()
                        } else {
                            value
                        };
                        let vw = (width(&value) as u16).min(lw.saturating_sub(12));
                        put(
                            buf,
                            content_x + 2,
                            y,
                            &truncate(&row.label, (lw - vw).saturating_sub(2) as usize, cx.ell()),
                            style,
                            (lw - vw).saturating_sub(2),
                        );
                        put(
                            buf,
                            content_x + content_w - vw,
                            y,
                            &truncate(&value, vw as usize, cx.ell()),
                            vstyle,
                            vw,
                        );
                    }
                }
            }
        }
    }

    for (offset, line) in details.iter().take(detail_count).enumerate() {
        put(
            buf,
            content_x,
            detail_start + offset as u16,
            &truncate(line, content_w as usize, cx.ell()),
            cx.st(MUTED, RAISED),
            content_w,
        );
    }
    let arrows = if cx.t.opts.ascii { ">" } else { "\u{2192}" };
    let both = if cx.t.opts.ascii {
        "<>"
    } else {
        "\u{2190}\u{2192}"
    };
    let left = if cx.t.opts.ascii { "<" } else { "\u{2190}" };
    let hints: Vec<(&str, &str)> = if a.ui.settings_nav_focus {
        vec![(updown(cx), "section"), ("enter", "open"), ("esc", "close")]
    } else if let Some(page) = page.as_deref() {
        if page.starts_with("agents/") {
            vec![
                ("enter", "select"),
                (arrows, "effort"),
                ("d", "remove"),
                ("K/J", "reorder"),
                (left, "sections"),
                ("esc", "back"),
            ]
        } else if page == "agents" || page == "providers" {
            vec![("enter", "open"), ("esc", "back")]
        } else {
            vec![("enter", "select"), ("esc", "back")]
        }
    } else {
        vec![("enter", "select"), (both, "value"), ("esc", "back")]
    };
    hint_row(cx, buf, &m, &hints);
    let count = format!("{} sections", sections.len());
    put(
        buf,
        r.right().saturating_sub(width(&count) as u16 + 2),
        m.hint_y,
        &count,
        cx.st(MUTED, RAISED),
        width(&count) as u16,
    );
    if let Some(id) = a.ui.provider_detail.as_deref() {
        draw_provider_detail(cx, buf, area, hits, id);
    }
}

/// One provider's connection and sign-in actions, over Settings. Each provider
/// row on the Providers page opens this modal; the page itself stays a list.
fn draw_provider_detail(cx: &Cx, buf: &mut Buffer, area: Rect, hits: &mut HitMap, id: &str) {
    use crate::app::{PageLine, Tone};
    let a = cx.app;
    let label = a
        .state
        .providers
        .iter()
        .find(|p| p["id"].as_str() == Some(id))
        .and_then(|p| p["label"].as_str())
        .unwrap_or(id);
    let lines = a.provider_detail_lines(id);
    let items = a.provider_detail_items(id);
    let height = (lines.len() as u16 + 6).clamp(10, 22);
    let r = modal_box(area, MODAL_MEDIUM, height);
    if r.width < 20 || r.height < 8 {
        return;
    }
    hits.add(area, Target::Scrim);
    let m = modal(cx, buf, area, r, label, hits);
    let sel = items
        .get(a.ui.provider_detail_row.min(items.len().saturating_sub(1)))
        .copied();
    let mut display: Vec<Option<usize>> = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        if matches!(line, PageLine::Heading(_) | PageLine::Title(..)) && i > 0 {
            display.push(None);
        }
        display.push(Some(i));
    }
    let list_h = m.hint_y.saturating_sub(m.body.y).saturating_sub(1) as usize;
    let sel_row = sel
        .and_then(|i| display.iter().position(|l| *l == Some(i)))
        .unwrap_or(0);
    let top = (sel_row + 1).saturating_sub(list_h.max(1));
    let tone = |t: Tone| match t {
        Tone::Muted => MUTED,
        Tone::Secondary => SECONDARY,
        Tone::Warning => "status.warning",
        Tone::Error => "status.error",
    };
    for (li, row) in display.iter().enumerate().skip(top).take(list_h) {
        let y = m.body.y + (li - top) as u16;
        let Some(i) = row else { continue };
        match &lines[*i] {
            PageLine::Heading(text) => {
                put(
                    buf,
                    m.x,
                    y,
                    &truncate(&text.to_uppercase(), m.w as usize, cx.ell()),
                    cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD),
                    m.w,
                );
            }
            PageLine::Title(text, detail) => {
                put(
                    buf,
                    m.x,
                    y,
                    &truncate(&text.to_uppercase(), m.w as usize, cx.ell()),
                    cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD),
                    m.w,
                );
                let dw = (width(detail) as u16).min(m.w / 2);
                put(
                    buf,
                    m.x + m.w - dw,
                    y,
                    &truncate(detail, dw as usize, cx.ell()),
                    cx.st(MUTED, RAISED),
                    dw,
                );
            }
            PageLine::Text(text, t) => {
                put(
                    buf,
                    m.x,
                    y,
                    &truncate(text, m.w as usize, cx.ell()),
                    cx.st(tone(*t), RAISED),
                    m.w,
                );
            }
            PageLine::Item {
                label,
                value,
                tone: t,
                action,
            } => {
                let selected = Some(*i) == sel;
                let position = items.iter().position(|p| p == i).unwrap_or(0);
                let is_link = matches!(action, crate::app::PageAction::OpenUrl(_));
                let mut style = if selected {
                    row_band(cx, buf, Rect::new(m.x, y, m.w, 1), PRIMARY, true)
                } else {
                    cx.st(PRIMARY, RAISED)
                };
                if is_link {
                    style = style.add_modifier(Modifier::UNDERLINED);
                }
                let mut vstyle = cx.st(
                    tone(*t),
                    if selected {
                        "interactive.settings_selection"
                    } else {
                        RAISED
                    },
                );
                if is_link {
                    vstyle = vstyle.add_modifier(Modifier::UNDERLINED);
                }
                if selected {
                    vstyle = vstyle.add_modifier(Modifier::BOLD);
                }
                let lw = m.w.saturating_sub(2);
                let vw = (width(value) as u16).min(lw.saturating_sub(12));
                put(
                    buf,
                    m.x + 2,
                    y,
                    &truncate(label, (lw - vw).saturating_sub(2) as usize, cx.ell()),
                    style,
                    (lw - vw).saturating_sub(2),
                );
                put(
                    buf,
                    m.x + m.w - vw,
                    y,
                    &truncate(value, vw as usize, cx.ell()),
                    vstyle,
                    vw,
                );
                hits.add(
                    Rect::new(m.x, y, m.w, 1),
                    Target::ProviderDetailRow(position),
                );
            }
        }
    }
    hint_row(cx, buf, &m, &[("enter", "select"), ("esc", "close")]);
}

/// One modal: the scrim over everything drawn so far, then a borderless
/// `surface.raised` block (DESIGN_PLAN.md D13) with 2 cells of padding left
/// and right and 1 row top and bottom. Inside: the bold title row, a blank
/// row, the body, a blank row, and the key-hint row (including Esc).
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
    let tw = m.w;
    put(
        buf,
        m.x,
        r.y + 1,
        &truncate(title, tw as usize, cx.ell()),
        cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD),
        tw,
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

fn leftright(cx: &Cx) -> &'static str {
    if cx.t.opts.ascii {
        "left/right"
    } else {
        "\u{2190}\u{2192}"
    }
}

fn review_arrow(cx: &Cx) -> &'static str {
    if cx.t.opts.ascii {
        "->"
    } else {
        "\u{2192}"
    }
}

/// The block for a modal of `width` and `height` cells: centred, within 2
/// cells of each edge and at most 70% of the frame high.
const MODAL_SMALL: u16 = 60;
const MODAL_MEDIUM: u16 = 72;
const MODAL_LARGE: u16 = 88;

fn modal_box(area: Rect, width: u16, height: u16) -> Rect {
    paint::modal_rect(area, width, height)
}

pub(crate) fn settings_box(area: Rect) -> Rect {
    modal_box(area, MODAL_LARGE, 40)
}

fn palette_box(area: Rect) -> Rect {
    modal_box(area, MODAL_MEDIUM, 22)
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
        OverlayKind::Inspector(i) => draw_inspector(cx, buf, area, i, hits),
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
        OverlayKind::ContextDoc { title, text, top } => {
            draw_context_doc(cx, buf, area, title, text, *top, hits)
        }
        OverlayKind::ContextEntries {
            title,
            entries,
            cursor,
            open,
            top,
        } => draw_context_entries(cx, buf, area, title, entries, *cursor, *open, *top, hits),
        OverlayKind::ImagePreview { name, data_url } => {
            draw_image_preview(cx, buf, area, name, data_url, hits)
        }
        OverlayKind::Annotate(annotation) => draw_annotate(cx, buf, area, annotation, hits),
        OverlayKind::AlertDetail { id, top, .. } => {
            draw_alert_detail(cx, buf, area, *id, *top, hits)
        }
        OverlayKind::Usage { rows } => draw_usage(cx, buf, area, rows, hits),
        OverlayKind::Resources { top } => draw_resources(cx, buf, area, *top, hits),
        OverlayKind::Projects(o) => draw_projects_overview(cx, buf, area, o, hits),
    }
}

/// The projects overview: every project in its saved order, with its active
/// sessions as a tree, an archived toggle per project, and inactive projects
/// in their own group.
fn draw_projects_overview(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    o: &crate::ui::ProjectsOverview,
    hits: &mut HitMap,
) {
    use crate::app::OverviewRow;
    let a = cx.app;
    let rows = a.overview_rows(o);
    let b = modal_box(area, MODAL_LARGE, area.height.saturating_sub(4).min(44));
    let m = modal(cx, buf, area, b, "Projects", hits);
    let height = m.body.height as usize;
    let top = o.top.min(rows.len().saturating_sub(height));
    let muted = cx.st(MUTED, RAISED);
    let secondary = cx.st(SECONDARY, RAISED);
    let bold = cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD);
    let branch = if cx.t.opts.ascii { "|-" } else { "├" };
    let last = if cx.t.opts.ascii { "`-" } else { "└" };
    let open = if cx.t.opts.ascii { "v" } else { "▾" };
    let closed = if cx.t.opts.ascii { ">" } else { "▸" };
    for (offset, row) in rows.iter().skip(top).take(height).enumerate() {
        let index = top + offset;
        let y = m.body.y + offset as u16;
        let rect = Rect::new(m.r.x + 1, y, m.r.width.saturating_sub(2), 1);
        let selected = index == o.cursor;
        let mut line: Vec<Span<'static>> = Vec::new();
        match row {
            OverviewRow::Group(title) => {
                line.push(Span::styled(
                    title.to_string(),
                    cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD),
                ));
            }
            OverviewRow::Project(id) => {
                let name = a
                    .state
                    .model
                    .projects
                    .get(id)
                    .map_or(id.as_str(), |p| p.name.as_str())
                    .to_owned();
                let status = a.project_status(id);
                let label = match status {
                    crate::api::project_status::PASSIVE => "Passive",
                    crate::api::project_status::INACTIVE => "Inactive",
                    _ => "Current",
                };
                let tone = if status == crate::api::project_status::PASSIVE {
                    muted
                } else {
                    bold
                };
                line.push(Span::styled(name, tone));
                line.push(Span::styled(format!("  · {label}"), muted));
                if status == crate::api::project_status::INACTIVE {
                    line.push(Span::styled("  · r restore", secondary));
                }
            }
            OverviewRow::Empty(_) => {
                line.push(Span::styled(format!("  {last} No active sessions"), muted));
            }
            OverviewRow::Session(id, archived) => {
                let is_last = rows
                    .iter()
                    .skip(index + 1)
                    .take_while(|r| !matches!(r, OverviewRow::Project(_) | OverviewRow::Group(_)))
                    .next()
                    .is_none();
                let title = a
                    .state
                    .model
                    .sessions
                    .get(id)
                    .map_or(id.as_str(), |s| s.title.as_str())
                    .to_owned();
                let state = if *archived {
                    "archived"
                } else {
                    a.session_state(id)
                };
                let state_tone = match state {
                    "running" => cx.st("status.info", RAISED),
                    "needs input" => cx.st(MULTI, RAISED),
                    "failed" => cx.st("status.error", RAISED),
                    "finished" => cx.st("status.success", RAISED),
                    "archived" => muted,
                    _ => secondary,
                };
                line.push(Span::styled(
                    if is_last {
                        format!("  {last} ")
                    } else {
                        format!("  {branch} ")
                    },
                    muted,
                ));
                line.push(Span::styled(title, if *archived { muted } else { bold }));
                line.push(Span::styled(format!(" · {state}"), state_tone));
            }
            OverviewRow::ArchivedToggle(id, count) => {
                let expanded = o.archived.contains(id);
                line.push(Span::styled(
                    format!(
                        "  {} {} {count} archived",
                        if expanded { branch } else { last },
                        if expanded { open } else { closed }
                    ),
                    muted,
                ));
            }
        }
        if selected {
            clear(buf, rect, cx.st(PRIMARY, RAISED));
            put(
                buf,
                rect.x,
                y,
                cx.t.structure("focus_bar"),
                cx.st("interactive.focus", RAISED),
                1,
            );
            for span in line.iter_mut() {
                span.style = span.style.add_modifier(Modifier::BOLD);
            }
        }
        draw_spans(cx, buf, m.x, y, &line, m.w);
        hits.add(rect, Target::OverviewRow(index));
    }
    if rows.is_empty() {
        put(buf, m.x, m.body.y, "No projects yet.", muted, m.w);
    }
    let hints = [
        (updown(cx), "select"),
        ("a", "archived"),
        ("1/2/3", "current/passive/inactive"),
        ("r", "restore"),
        ("enter", "open"),
        ("esc", "back"),
    ];
    hint_row(cx, buf, &m, &hints);
}

/// `/usage`: a heading per connected provider, then one row per limit window as
/// `label  ━━━━────  42%  resets in 3h 10m`. Only the filled part of the bar takes the tone.
fn draw_usage(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    rows: &[crate::usage::Row],
    hits: &mut HitMap,
) {
    use crate::usage::Row;
    let b = modal_box(area, MODAL_LARGE, (rows.len() + 6).clamp(9, 30) as u16);
    let m = modal(cx, buf, area, b, "Provider usage", hits);
    let (filled, track) = if cx.t.opts.ascii {
        ("#", "-")
    } else {
        ("━", "─")
    };
    let label_w = m.w.saturating_sub(34).clamp(8, 22);
    let bar_w = (m.w as usize)
        .saturating_sub(label_w as usize + 26)
        .clamp(8, 24);
    for (i, row) in rows.iter().take(m.body.height as usize).enumerate() {
        let y = m.body.y + i as u16;
        match row {
            Row::Gap => {}
            Row::Provider { name, plan } => {
                let x = put(
                    buf,
                    m.x,
                    y,
                    name,
                    cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD),
                    m.w,
                );
                if !plan.is_empty() {
                    put(
                        buf,
                        x,
                        y,
                        &format!("  {plan}"),
                        cx.st(MUTED, RAISED),
                        m.x + m.w - x,
                    );
                }
            }
            Row::Note(text) => {
                put(
                    buf,
                    m.x,
                    y,
                    &truncate(text, m.w as usize, cx.ell()),
                    cx.st(MUTED, RAISED),
                    m.w,
                );
            }
            Row::Window { label, used, reset } => {
                let end = m.x + m.w;
                let x = put(
                    buf,
                    m.x + 2,
                    y,
                    &truncate(label, label_w as usize, cx.ell()),
                    cx.st(SECONDARY, RAISED),
                    label_w,
                );
                let _ = x;
                let bx = m.x + label_w + 4;
                let n = used.map_or(0, |u| (usize::from(u) * bar_w).div_ceil(100));
                let tone = match used {
                    Some(90..) => "status.error",
                    Some(70..) => "status.warning",
                    Some(_) => "status.success",
                    None => MUTED,
                };
                let bar = format!("{}{}", filled.repeat(n), track.repeat(bar_w - n));
                put(
                    buf,
                    bx,
                    y,
                    &track.repeat(bar_w),
                    cx.st(MUTED, RAISED),
                    bar_w as u16,
                );
                put(
                    buf,
                    bx,
                    y,
                    &bar[..bar.char_indices().nth(n).map_or(bar.len(), |(i, _)| i)],
                    cx.st(tone, RAISED),
                    n as u16,
                );
                let tx = bx + bar_w as u16 + 2;
                let pct = used.map_or("  ?".to_owned(), |u| format!("{u:>3}%"));
                let x = put(
                    buf,
                    tx,
                    y,
                    &pct,
                    cx.st(PRIMARY, RAISED),
                    end.saturating_sub(tx),
                );
                put(
                    buf,
                    x + 2,
                    y,
                    &truncate(reset, end.saturating_sub(x + 2) as usize, cx.ell()),
                    cx.st(MUTED, RAISED),
                    end.saturating_sub(x + 2),
                );
            }
        }
    }
    hint_row(cx, buf, &m, &[("r", "refresh"), ("esc", "close")]);
    hits.add(
        Rect::new(m.x, m.hint_y, m.w.min(9), 1),
        Target::UsageRefresh,
    );
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
    let r = modal_box(area, MODAL_LARGE, visible.saturating_add(8) as u16);
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
    let b = palette_box(area);
    let title = match p.mode {
        PaletteMode::Commands => "Command palette",
        PaletteMode::ProviderLogin => "Sign in to a provider",
        PaletteMode::ProviderLogout => "Sign out of a provider",
        PaletteMode::Sessions if cx.app.ui.scope_all => "Search sessions · Global",
        PaletteMode::Sessions => "Search sessions · Project",
        PaletteMode::Projects => "Switch project",
        PaletteMode::Models => "Change model",
        PaletteMode::PoolModels => "Add a model",
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
            PaletteMode::Models | PaletteMode::PoolModels => "type to filter models",
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
    let info_height = if p.mode == PaletteMode::Models && p.model_info {
        4
    } else {
        0
    };
    let list = Rect::new(
        b.x + 1,
        sy + 2,
        b.width - 2,
        m.body.height.saturating_sub(2 + info_height),
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
        let mark = match &it.kind {
            PaletteKind::Session(id) => Some(session_glyph(cx, id, RAISED)),
            _ => None,
        };
        let hw = width(&it.hint) as u16;
        let mark_w = mark.as_ref().map_or(0, |(g, _)| width(g) as u16 + 1);
        let hx = (m.x + m.w).saturating_sub(hw);
        if let Some((g, gs)) = &mark {
            put(buf, m.x + 2, y, g, *gs, width(g) as u16);
        }
        if hw > 0 {
            put(buf, hx, y, &it.hint, quiet, hw);
        }
        let label_x = m.x + 2 + mark_w;
        let room = hx.saturating_sub(label_x + 1) as usize;
        let mut label_style = style;
        if let PaletteKind::Agent(name) = &it.kind {
            label_style = label_style.patch(cx.t.agent_style(name));
        }
        let x1 = put(
            buf,
            label_x,
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
    if info_height > 0 && m.body.height >= 6 {
        let panel = Rect::new(
            b.x,
            m.body.bottom().saturating_sub(info_height),
            b.width,
            info_height,
        );
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
        hints.push(("tab", "favorite"));
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
    let b = modal_box(area, MODAL_MEDIUM, 30);
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
/// Text width and visible rows of the inspector overlay.
pub fn inspector_metrics(area: Rect) -> (usize, usize) {
    let b = modal_box(area, MODAL_LARGE, 40);
    (
        b.width.saturating_sub(4) as usize,
        (b.height as usize).saturating_sub(6).max(4),
    )
}

/// The native inspector: a fixed header (views, live state, filters, sessions), a
/// scrolling body whose selected row is bold, and the key hints.
fn draw_inspector(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    i: &crate::inspector::Inspector,
    hits: &mut HitMap,
) {
    let (width, _) = inspector_metrics(area);
    let parts = i.parts(width, crate::app::now_secs());
    let b = modal_box(area, MODAL_LARGE, 40);
    let m = modal(cx, buf, area, b, "Session inspector", hits);
    let mut y = m.body.y;
    for (n, line) in parts.fixed.iter().enumerate() {
        let style = if n == 2 {
            if line.starts_with('○') {
                cx.st("status.error", RAISED)
            } else if line.starts_with('‖') {
                cx.st("status.warning", RAISED)
            } else {
                cx.st("status.success", RAISED)
            }
        } else if n == 1 {
            cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD)
        } else {
            cx.st(SECONDARY, RAISED)
        };
        put(buf, m.x, y, line, style, m.w);
        y += 1;
    }
    for (row, col, w, id) in &parts.tree_hits {
        let at = m.body.y + *row as u16;
        if at < y && *col < m.w as usize {
            hits.add(
                Rect::new(m.x + *col as u16, at, (*w as u16).min(m.w), 1),
                Target::InspectorSession(id.clone()),
            );
        }
    }
    let h = (m.body.y + m.body.height).saturating_sub(y) as usize;
    let top = i.page().top.min(parts.body.len().saturating_sub(h));
    let bold = cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD);
    for (k, row) in parts.body.iter().skip(top).take(h).enumerate() {
        let style = if Some(top + k) == parts.cursor {
            bold
        } else if row.starts_with("Omitted") || row.starts_with("Error") {
            cx.st("status.warning", RAISED)
        } else if row.starts_with(' ') || row.starts_with("…") {
            cx.st(SECONDARY, RAISED)
        } else {
            cx.st(PRIMARY, RAISED)
        };
        put(buf, m.x, y + k as u16, row, style, m.w);
    }
    for (line, id) in &parts.row_hits {
        if *line >= top && *line < top + h {
            hits.add(
                Rect::new(m.x, y + (*line - top) as u16, m.w, 1),
                Target::InspectorRow(id.clone()),
            );
        }
    }
    hint_row(
        cx,
        buf,
        &m,
        &[
            ("1-6", "view"),
            ("j/k", "select"),
            ("enter", "expand"),
            ("/", "search"),
            ("esc", "close"),
        ],
    );
}

fn context_detail_layout(area: Rect, text: &str) -> (Rect, Vec<String>) {
    let wide = MODAL_LARGE;
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

fn draw_image_preview(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    name: &str,
    data_url: &str,
    hits: &mut HitMap,
) {
    let wide = MODAL_LARGE;
    let high = (area.height as u32 * 80 / 100)
        .clamp(10, area.height.saturating_sub(4).max(10) as u32) as u16;
    let b = modal_box(area, wide, high);
    let m = modal(cx, buf, area, b, name, hits);
    if m.body.width == 0 || m.body.height == 0 {
        return;
    }
    let key = format!("full:{name}");
    if let Some(state) = cx.app.image_viewer.borrow_mut().protocol(&key, data_url) {
        let image =
            ratatui_image::StatefulImage::<ratatui_image::protocol::StatefulProtocol>::new()
                .resize(ratatui_image::Resize::Fit(None));
        ratatui::widgets::StatefulWidget::render(image, m.body, buf, state);
    }
    hint_row(cx, buf, &m, &[("esc", "close")]);
}

/// The drawing modal: the picture, the pen colours and actions, then the keys.
fn draw_annotate(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    annotation: &crate::annotate::Annotation,
    hits: &mut HitMap,
) {
    use crate::annotate::{fit, PixelRect};
    let wide = MODAL_LARGE;
    let high = (area.height as u32 * 80 / 100)
        .clamp(12, area.height.saturating_sub(4).max(12) as u32) as u16;
    let b = modal_box(area, wide, high);
    let m = modal(
        cx,
        buf,
        area,
        b,
        &format!("Mark up {}", annotation.name),
        hits,
    );
    if m.body.width == 0 || m.body.height < 3 {
        return;
    }
    let tools_y = m.body.bottom() - 1;
    let picture = Rect::new(m.body.x, m.body.y, m.body.width, m.body.height - 2);
    let live = cx.app.annotate_live.borrow().supported();
    if live {
        let mut state = cx.app.annotate_live.borrow_mut();
        // Zooming the terminal changes the cell size; the kernel knows it.
        if let Ok(size) = crossterm::terminal::window_size() {
            if size.columns > 0 && size.rows > 0 && size.width > 0 && size.height > 0 {
                state.set_font((size.width / size.columns, size.height / size.rows));
            }
        }
        let (cell_w, cell_h) = state.font();
        let (cell_w, cell_h) = (u32::from(cell_w), u32::from(cell_h));
        let room = PixelRect {
            x: u32::from(picture.x) * cell_w,
            y: u32::from(picture.y) * cell_h,
            width: u32::from(picture.width) * cell_w,
            height: u32::from(picture.height) * cell_h,
        };
        if let Some(natural) = annotation.natural {
            let rect = fit(natural, room);
            annotation.canvas.set(rect);
            state.sync(annotation, rect);
        }
    } else {
        let url = annotation
            .flattened
            .as_deref()
            .unwrap_or(&annotation.data_url);
        let (cell_w, cell_h) = cx.app.annotate_live.borrow().font();
        let (cell_w, cell_h) = (u32::from(cell_w), u32::from(cell_h));
        let mut viewer = cx.app.image_viewer.borrow_mut();
        if let Some(state) = viewer.protocol("annotate", url) {
            let resize = ratatui_image::Resize::Fit(None);
            let shown = state.size_for(resize.clone(), picture);
            annotation.canvas.set(PixelRect {
                x: u32::from(picture.x) * cell_w,
                y: u32::from(picture.y) * cell_h,
                width: u32::from(shown.width) * cell_w,
                height: u32::from(shown.height) * cell_h,
            });
            let image =
                ratatui_image::StatefulImage::<ratatui_image::protocol::StatefulProtocol>::new()
                    .resize(resize);
            ratatui::widgets::StatefulWidget::render(image, picture, buf, state);
            if annotation.drawing {
                draw_annotate_cells(annotation, picture, shown, buf);
            }
        }
    }
    draw_annotate_tools(cx, buf, &m, tools_y, annotation, hits);
    hint_row(
        cx,
        buf,
        &m,
        &[
            ("1-4", "colour"),
            ("u", "undo"),
            ("x", "clear"),
            ("enter", "done"),
            ("esc", "cancel"),
        ],
    );
}

/// The stroke in progress where the terminal cannot draw it live.
fn draw_annotate_cells(
    annotation: &crate::annotate::Annotation,
    picture: Rect,
    shown: Rect,
    buf: &mut Buffer,
) {
    let Some(stroke) = annotation.strokes.last() else {
        return;
    };
    let ink = Color::Rgb(stroke.color[0], stroke.color[1], stroke.color[2]);
    let cell = |point: &[f32; 2]| {
        (
            f32::from(picture.x) + point[0] * f32::from(shown.width.saturating_sub(1)),
            f32::from(picture.y) + point[1] * f32::from(shown.height.saturating_sub(1)),
        )
    };
    let mut mark = |x: f32, y: f32| {
        if let Some(cell) = buf.cell_mut((x.round() as u16, y.round() as u16)) {
            cell.set_skip(false);
            cell.set_symbol(" ");
            cell.set_bg(ink);
        }
    };
    let mut previous: Option<(f32, f32)> = None;
    for point in &stroke.points {
        let (x, y) = cell(point);
        if let Some((from_x, from_y)) = previous {
            // Sample so a fast drag still leaves a connected line of cells.
            let steps = (x - from_x).abs().max((y - from_y).abs()).ceil().max(1.0);
            for step in 1..=steps as u32 {
                let t = step as f32 / steps;
                mark(from_x + (x - from_x) * t, from_y + (y - from_y) * t);
            }
        } else {
            mark(x, y);
        }
        previous = Some((x, y));
    }
}

/// The row under the picture: pen colours on the left, actions on the right.
fn draw_annotate_tools(
    cx: &Cx,
    buf: &mut Buffer,
    m: &Modal,
    y: u16,
    annotation: &crate::annotate::Annotation,
    hits: &mut HitMap,
) {
    let inks = crate::theme::annotation_inks();
    let mut x = m.x;
    for (i, ink) in inks.iter().enumerate() {
        let selected = annotation.ink == i;
        let bracket = cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD);
        put(buf, x, y, if selected { "[" } else { " " }, bracket, 1);
        put(
            buf,
            x + 1,
            y,
            "  ",
            Style::default().bg(Color::Rgb(ink[0], ink[1], ink[2])),
            2,
        );
        put(buf, x + 3, y, if selected { "]" } else { " " }, bracket, 1);
        hits.add(Rect::new(x, y, 4, 1), Target::AnnotateInk(i));
        x += 5;
    }
    let has_strokes = !annotation.strokes.is_empty();
    let actions = [
        ("Undo", Target::AnnotateUndo, has_strokes),
        ("Clear", Target::AnnotateClear, has_strokes),
        ("Done", Target::AnnotateDone, true),
    ];
    let width: u16 = actions
        .iter()
        .map(|(label, _, _)| label.len() as u16 + 2)
        .sum();
    let mut action_x = (m.x + m.w).saturating_sub(width).max(x);
    for (label, target, enabled) in actions {
        let style = if enabled {
            cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD)
        } else {
            cx.st(MUTED, RAISED)
        };
        let end = put(buf, action_x, y, label, style, label.len() as u16);
        if enabled {
            hits.add(Rect::new(action_x, y, label.len() as u16, 1), target);
        }
        action_x = end + 2;
    }
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

/// Live kernels and background shells of the viewed session tree, newest
/// state first. One line per fact, scrollable like an alert's full text.
fn draw_resources(cx: &Cx, buf: &mut Buffer, area: Rect, top: usize, hits: &mut HitMap) {
    let wide = MODAL_LARGE;
    let inner = modal_box(area, wide, 10).width.saturating_sub(4) as usize;
    let kernels = cx.app.view_kernels();
    let jobs = cx.app.view_jobs();
    let running = jobs
        .iter()
        .filter(|job| job["status"].as_str() == Some("running"))
        .count();
    let quiet = cx.st(MUTED, RAISED);
    let secondary = cx.st(SECONDARY, RAISED);
    let bold = cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD);
    let section = cx.st(MUTED, RAISED).add_modifier(Modifier::BOLD);
    let mut rows: Vec<Vec<Span<'static>>> = Vec::new();
    if let Some(error) = &cx.app.state.resources_error {
        rows.push(vec![Span::styled(
            truncate(&format!("Could not refresh: {error}"), inner, cx.ell()),
            cx.st("status.warning", RAISED),
        )]);
        rows.push(Vec::new());
    }
    rows.push(vec![Span::styled(
        format!("Kernels ({})", kernels.len()),
        section,
    )]);
    if kernels.is_empty() {
        rows.push(vec![Span::styled("No live kernels.", quiet)]);
    }
    for kernel in &kernels {
        let id = kernel["id"].as_str().unwrap_or("kernel");
        let busy = kernel["busy"].as_bool() == Some(true);
        let cells = kernel["cell_count"].as_u64().unwrap_or(0);
        let mut head = vec![
            Span::styled(id.to_owned(), bold),
            Span::styled(" · ", quiet),
        ];
        head.push(Span::styled(
            if busy { "busy" } else { "idle" }.to_owned(),
            if busy {
                cx.st("status.warning", RAISED)
            } else {
                quiet
            },
        ));
        head.push(Span::styled(
            format!(" · {cells} {}", if cells == 1 { "cell" } else { "cells" }),
            quiet,
        ));
        rows.push(head);
        let owner = kernel["created_by"]
            .as_str()
            .map(|id| session_label(cx.app, id))
            .unwrap_or_else(|| "unknown session".to_owned());
        let users = kernel["users"].as_array().map_or(0, Vec::len);
        let age = now_secs() - kernel["created"].as_f64().unwrap_or(0.0);
        rows.push(vec![Span::styled(
            truncate(
                &format!(
                    "  created by {owner} · {users} {} · {} ago",
                    if users == 1 { "user" } else { "users" },
                    crate::timeline::fmt_duration(age)
                ),
                inner,
                cx.ell(),
            ),
            secondary,
        )]);
        let variables = kernel["variables"].as_array();
        if let Some(variables) = variables.filter(|variables| !variables.is_empty()) {
            let text = variables
                .iter()
                .map(|variable| {
                    let name = variable["name"].as_str().unwrap_or("?");
                    let kind = variable["type"].as_str().unwrap_or("?");
                    let repr = variable["repr"].as_str().unwrap_or("");
                    format!("{name}: {kind} = {repr}")
                })
                .collect::<Vec<_>>()
                .join(" · ");
            rows.push(vec![Span::styled(
                truncate(&format!("  {text}"), inner, cx.ell()),
                secondary,
            )]);
        }
    }
    rows.push(Vec::new());
    rows.push(vec![Span::styled(
        format!("Background shells ({} live, {} kept)", running, jobs.len()),
        section,
    )]);
    if jobs.is_empty() {
        rows.push(vec![Span::styled("No background shells.", quiet)]);
    }
    for job in &jobs {
        let id = job["id"].as_str().unwrap_or("job");
        let status = job["status"].as_str().unwrap_or("unknown");
        let token = match status {
            "running" => "status.info",
            "done" => "status.success",
            "timeout" | "killed" => "status.warning",
            "error" => "status.error",
            _ => MUTED,
        };
        let mut head = vec![
            Span::styled(id.to_owned(), bold),
            Span::styled(" · ", quiet),
            Span::styled(status.to_owned(), cx.st(token, RAISED)),
        ];
        if let Some(exit) = job["exit_code"].as_i64() {
            head.push(Span::styled(format!(" · exit {exit}"), quiet));
        }
        let duration = job["duration_s"].as_f64().unwrap_or(0.0);
        head.push(Span::styled(
            format!(" · {}", crate::timeline::fmt_duration(duration)),
            quiet,
        ));
        rows.push(head);
        let command = job["command"].as_str().unwrap_or("");
        rows.push(vec![Span::styled(
            truncate(&format!("  $ {command}"), inner, cx.ell()),
            secondary,
        )]);
        let owner = job["session_id"]
            .as_str()
            .map(|id| session_label(cx.app, id))
            .unwrap_or_else(|| "unknown session".to_owned());
        let chars = job["output_chars"].as_u64().unwrap_or(0);
        let dropped = job["dropped_chars"].as_u64().unwrap_or(0);
        let mut meta = format!(
            "  {owner} · {chars} {}",
            if chars == 1 { "char" } else { "chars" }
        );
        if dropped > 0 {
            meta.push_str(&format!(" · {dropped} dropped"));
        }
        if let Some(timeout) = job["timeout"].as_f64() {
            meta.push_str(&format!(
                " · timeout {}",
                crate::timeline::fmt_duration(timeout)
            ));
        }
        rows.push(vec![Span::styled(truncate(&meta, inner, cx.ell()), quiet)]);
    }
    let b = modal_box(area, wide, (rows.len() + 6).clamp(10, 40) as u16);
    let m = modal(cx, buf, area, b, "Kernels and shells", hits);
    let h = m.body.height as usize;
    let top = top.min(rows.len().saturating_sub(h));
    for (i, row) in rows.iter().skip(top).take(h).enumerate() {
        draw_spans(cx, buf, m.x, m.body.y + i as u16, row, m.w);
    }
    hint_row(cx, buf, &m, &[(updown(cx), "scroll"), ("esc", "close")]);
}

/// A session's title, falling back to its id when the row is not loaded.
fn session_label(app: &App, id: &str) -> String {
    app.state
        .model
        .sessions
        .get(id)
        .map(|session| session.title.clone())
        .filter(|title| !title.trim().is_empty())
        .unwrap_or_else(|| id.to_owned())
}

/// The modal's width and the Markdown rows of a document at that width.
fn context_doc_rows(area: Rect, theme: &Theme, text: &str) -> (u16, Vec<Vec<Span<'static>>>) {
    let wide = MODAL_LARGE;
    let inner = modal_box(area, wide, 10).width.saturating_sub(4) as usize;
    let palette = crate::markdown::Palette::from_theme(theme);
    let rows = crate::markdown::lines(text, &palette, inner.max(1))
        .into_iter()
        .flat_map(|row| {
            let prefix_width: usize = row
                .prefix
                .iter()
                .map(|span| text::width(&span.content))
                .sum();
            crate::markdown::wrap_spans(&row.spans, inner.saturating_sub(prefix_width).max(1))
                .into_iter()
                .enumerate()
                .map(move |(index, spans)| {
                    let mut line = if index == 0 {
                        row.prefix.clone()
                    } else {
                        vec![Span::raw(" ".repeat(prefix_width))]
                    };
                    line.extend(spans);
                    line
                })
        })
        .collect();
    (wide, rows)
}

fn modal_rows_height(area: Rect, wide: u16, rows: usize) -> usize {
    modal_box(area, wide, (rows + 6).clamp(10, 40) as u16)
        .height
        .saturating_sub(6) as usize
}

pub(crate) fn context_doc_max(area: Rect, theme: &Theme, text: &str) -> usize {
    let (wide, rows) = context_doc_rows(area, theme, text);
    rows.len()
        .saturating_sub(modal_rows_height(area, wide, rows.len()))
}

/// Spans on the raised surface, cut at `room` cells.
fn draw_spans(cx: &Cx, buf: &mut Buffer, x: u16, y: u16, spans: &[Span<'static>], room: u16) {
    let surface = cx.st(PRIMARY, RAISED);
    let mut at = x;
    for span in spans {
        if at >= x + room {
            break;
        }
        let style = surface.patch(span.style);
        at = put(buf, at, y, span.content.as_ref(), style, x + room - at);
    }
}

fn draw_context_doc(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    title: &str,
    text: &str,
    top: usize,
    hits: &mut HitMap,
) {
    draw_context_doc_hint(cx, buf, area, title, text, top, "close", hits);
}

#[allow(clippy::too_many_arguments)]
fn draw_context_doc_hint(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    title: &str,
    text: &str,
    top: usize,
    escape_hint: &str,
    hits: &mut HitMap,
) {
    let (wide, rows) = context_doc_rows(area, cx.t, text);
    let b = modal_box(area, wide, (rows.len() + 6).clamp(10, 40) as u16);
    let m = modal(cx, buf, area, b, title, hits);
    let h = m.body.height as usize;
    let top = top.min(rows.len().saturating_sub(h));
    for (i, row) in rows.iter().skip(top).take(h).enumerate() {
        draw_spans(cx, buf, m.x, m.body.y + i as u16, row, m.w);
    }
    hint_row(cx, buf, &m, &[(updown(cx), "scroll"), ("esc", escape_hint)]);
}

/// A context entry's rows at `inner` cells: the open entry's definition, as Markdown
/// for a skill and verbatim for a schema.
fn context_entry_body(
    theme: &Theme,
    entry: &crate::ui::ContextEntry,
    inner: usize,
) -> Vec<Vec<Span<'static>>> {
    let Some(full) = &entry.full else {
        return vec![vec![Span::raw("Loading…")]];
    };
    if entry.markdown {
        let palette = crate::markdown::Palette::from_theme(theme);
        return crate::markdown::lines(full, &palette, inner.max(1))
            .into_iter()
            .map(|row| row.prefix.into_iter().chain(row.spans).collect())
            .collect();
    }
    full.lines()
        .flat_map(|line| {
            paint::wrap_ranges(line, inner.max(1))
                .into_iter()
                .map(|range| vec![Span::raw(line[range].to_owned())])
                .collect::<Vec<_>>()
        })
        .collect()
}

fn context_entries_box(area: Rect) -> (u16, usize) {
    let wide = MODAL_LARGE;
    (
        wide,
        modal_box(area, wide, 10).width.saturating_sub(8) as usize,
    )
}

/// How far the arrows scroll inside an open entry before moving to the next one.
pub(crate) fn context_entry_overflow(
    area: Rect,
    theme: &Theme,
    entries: &[crate::ui::ContextEntry],
    cursor: usize,
) -> usize {
    let (_, inner) = context_entries_box(area);
    let page = (area.height as usize).clamp(10, 40).saturating_sub(10);
    entries.get(cursor).map_or(0, |entry| {
        context_entry_body(theme, entry, inner)
            .len()
            .saturating_sub(page)
    })
}

#[allow(clippy::too_many_arguments)]
fn draw_context_entries(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    title: &str,
    entries: &[crate::ui::ContextEntry],
    cursor: usize,
    open: Option<usize>,
    top: usize,
    hits: &mut HitMap,
) {
    let (wide, inner) = context_entries_box(area);
    if title == "Skills" {
        if let Some(entry) = open.and_then(|index| entries.get(index)) {
            draw_context_doc_hint(
                cx,
                buf,
                area,
                &entry.name,
                entry.full.as_deref().unwrap_or("Loading…"),
                top,
                "back to skills",
                hits,
            );
        } else {
            draw_skill_picker(cx, buf, area, entries, cursor, hits);
        }
        return;
    }
    let body = open
        .and_then(|i| entries.get(i))
        .map(|entry| context_entry_body(cx.t, entry, inner))
        .unwrap_or_default();
    // Each entry is its name, its description when it has one, then its open body.
    let mut rows: Vec<(Option<usize>, Vec<Span<'static>>)> = Vec::new();
    let mut cursor_row = 0;
    let quiet = cx.st(MUTED, RAISED);
    for (i, entry) in entries.iter().enumerate() {
        if i == cursor {
            cursor_row = rows.len();
        }
        let style = if i == cursor {
            cx.st(PRIMARY, RAISED).add_modifier(Modifier::BOLD)
        } else {
            cx.st(PRIMARY, RAISED)
        };
        rows.push((
            Some(i),
            vec![Span::styled(
                format!("{} {}", cx.t.fold(open == Some(i)), entry.name),
                style,
            )],
        ));
        if !entry.description.is_empty() {
            rows.push((
                Some(i),
                vec![Span::styled(
                    format!("  {}", truncate(&entry.description, inner, cx.ell())),
                    quiet,
                )],
            ));
        }
        if open == Some(i) {
            let rail = cx.t.structure("rail").to_owned();
            for row in body.iter().skip(top) {
                let mut spans = vec![Span::styled(format!("  {rail} "), quiet)];
                spans.extend(row.iter().cloned());
                rows.push((None, spans));
            }
        }
    }
    if entries.is_empty() {
        rows.push((None, vec![Span::styled("Nothing here.", quiet)]));
    }
    let b = modal_box(area, wide, (rows.len() + 6).clamp(10, 40) as u16);
    let m = modal(cx, buf, area, b, title, hits);
    let h = m.body.height as usize;
    let first = (cursor_row + 1)
        .saturating_sub(h.saturating_sub(1).max(1))
        .min(cursor_row);
    for (i, (entry, row)) in rows.iter().skip(first).take(h).enumerate() {
        let y = m.body.y + i as u16;
        draw_spans(cx, buf, m.x, y, row, m.w);
        if let Some(entry) = entry {
            hits.add(Rect::new(m.x, y, m.w, 1), Target::ContextEntry(*entry));
        }
    }
    hint_row(
        cx,
        buf,
        &m,
        &[(updown(cx), "move"), ("enter", "details"), ("esc", "close")],
    );
}

fn draw_skill_picker(
    cx: &Cx,
    buf: &mut Buffer,
    area: Rect,
    entries: &[crate::ui::ContextEntry],
    cursor: usize,
    hits: &mut HitMap,
) {
    let b = modal_box(area, MODAL_LARGE, 40);
    let m = modal(cx, buf, area, b, "Skills", hits);
    let column_len = entries.len().div_ceil(2).max(1);
    let page = (m.body.height as usize / 7).max(1);
    let row = cursor % column_len;
    let first = row.saturating_sub(page - 1);
    let width = m.w.saturating_sub(2) / 2;
    let items = cx.app.context_items_for("Skills");
    for (index, entry) in entries.iter().enumerate() {
        let row = index % column_len;
        if row < first || row >= first + page {
            continue;
        }
        let x = m.x + (index / column_len) as u16 * (width + 2);
        let y = m.body.y + ((row - first) * 7) as u16;
        if y + 5 >= m.body.bottom() {
            continue;
        }
        let selected = index == cursor;
        let role = if selected {
            "skill_card_focus"
        } else {
            "skill_card"
        };
        let background = &cx.t.role(role).bg;
        clear(buf, Rect::new(x, y, width, 6), cx.t.role_style(role));
        put(
            buf,
            x + 1,
            y + 1,
            &truncate(&entry.name, width.saturating_sub(2) as usize, cx.ell()),
            cx.st(PRIMARY, background).add_modifier(if selected {
                Modifier::BOLD | Modifier::UNDERLINED
            } else {
                Modifier::BOLD
            }),
            width.saturating_sub(2),
        );
        hits.add(Rect::new(x, y + 1, width, 1), Target::ContextEntry(index));
        let description = entry
            .description
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        let lines = text::wrap(&description, width.saturating_sub(2).max(1) as usize);
        for (i, line) in lines.iter().take(2).enumerate() {
            put(
                buf,
                x + 1,
                y + 2 + i as u16,
                line,
                cx.st(SECONDARY, background),
                width.saturating_sub(2),
            );
        }
        if let Some(item) = items.get(index) {
            let label = if item.locked {
                "Locked · new session"
            } else if item.on {
                "● Enabled"
            } else {
                "○ Disabled"
            };
            put(
                buf,
                x + 1,
                y + 4,
                label,
                cx.st(SECONDARY, background),
                width.saturating_sub(2),
            );
            if !item.locked {
                hits.add(
                    Rect::new(x + 1, y + 4, width.saturating_sub(2), 1),
                    Target::ContextToggle(item.index),
                );
            }
        }
    }
    hint_row(
        cx,
        buf,
        &m,
        &[
            (updown(cx), "move"),
            ("enter", "details"),
            ("space", "toggle"),
            ("esc", "close"),
        ],
    );
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
    let wide = MODAL_MEDIUM;
    let inner = modal_box(area, wide, 10).width.saturating_sub(4) as usize;
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
            "alt/ctrl+left/right".into(),
            "Move by word; Shift selects".into(),
        ),
        (
            "cmd+left/right".into(),
            "Move to line start/end; Shift selects".into(),
        ),
    ]);
    rows.push(("f6 / shift+f6".into(), "Cycle focus between panes".into()));
    rows.push((
        "ctrl+j".into(),
        "Insert newline (where shift+enter is not reported)".into(),
    ));
    rows.push(("ctrl+z".into(), "Suspend to the shell".into()));
    rows.push(("ctrl+c".into(), "Clear the draft; twice to quit".into()));
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
    let w = MODAL_SMALL.min(area.width.saturating_sub(4));
    let inner = w.saturating_sub(4) as usize;
    let rows: Vec<String> = body.iter().flat_map(|l| text::wrap(l, inner)).collect();
    // padding 2 + title + blank + body + blank + buttons + blank + hints
    let extra = if buttons.is_some() { 8 } else { 6 };
    let want = (rows.len() as u16 + extra).max(8);
    let b = modal_box(area, w, want.min(area.height.saturating_sub(4).max(8)));
    let b = if want > b.height {
        // Confirmation text must remain visible until these dialogs have a scrollable body.
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
            hint_row(cx, buf, &m, &[("ctrl+q", "quit")]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{AppState, ConnectionState};
    use crate::sync::SyncMsg;
    use crate::theme::{Mode, Options, Theme, Tier};

    #[test]
    fn the_file_modal_hides_the_composer_caret() {
        let mut state = AppState::new();
        state.apply(SyncMsg::Connection(ConnectionState::Live));
        let options = Options {
            mode: Mode::Dark,
            tier: Tier::TrueColor,
            ascii: false,
            reduced_motion: true,
        };
        let mut app = App::new(state, Theme::new(options), (120, 36));
        app.ui.focus = Focus::Composer;
        let _ = render_text(&mut app, 120, 36);
        assert!(
            cursor_position(&app, &app.regions).is_some(),
            "the composer shows its caret"
        );
        app.ui.file_open = true;
        assert!(
            cursor_position(&app, &app.regions).is_none(),
            "the file modal hides the page's caret"
        );
    }
}

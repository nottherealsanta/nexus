//! Group strips and bodies (plan 45): the centre as a tree of groups of tabs.
//!
//! Every visible group draws a one-row strip (tabs, `+`, `⋯`) over its body.
//! The focused group's file, diff, terminal or session page is the live one;
//! another group shows what this client last saw (see `app::tabs`).

use super::*;
use crate::app::tabs::{Drag, Over};
use crate::groups::{self, Dir, GroupId, Kind};

/// What `draw_groups` hands back: where each strip tab sits (for drops) and
/// the tooltips to paint last.
pub(super) struct Drawn {
    pub tab_rects: Vec<(GroupId, usize, Rect)>,
    pub tips: Vec<(Rect, String)>,
}

const TITLE_MAX: usize = 24;
/// ` g title x `: padding, glyph, space, title, space, close, padding (and a
/// one-cell gap between tabs).
const CELL_EXTRA: usize = 6;

pub(super) fn draw_groups(cx: &Cx, buf: &mut Buffer, g: &Regions, hits: &mut HitMap) -> Drawn {
    let a = cx.app;
    let ui = &a.ui.groups;
    let focused = ui.layout.focused;
    let mut out = Drawn {
        tab_rects: Vec::new(),
        tips: Vec::new(),
    };
    for (id, rect) in &ui.rects {
        clear(buf, *rect, cx.st(PRIMARY, CANVAS));
        hits.add(*rect, Target::GroupBody(*id));
    }
    for (i, d) in ui.dividers.iter().enumerate() {
        let hot = cx.hovered(d.rect)
            || matches!(&ui.drag, Some(Drag::Divider { divider }) if divider == d);
        let (glyph, style) = if d.dir == Dir::Row {
            (cx.t.structure("rail"), "border.default")
        } else {
            (
                if cx.t.opts.ascii { "-" } else { "\u{2500}" },
                "border.default",
            )
        };
        let style = cx.st(if hot { ACCENT } else { style }, CANVAS);
        for y in d.rect.y..d.rect.bottom() {
            for x in d.rect.x..d.rect.right() {
                put(buf, x, y, glyph, style, 1);
            }
        }
        hits.add(d.rect, Target::GroupDivider(i));
    }
    for (id, rect) in &ui.rects {
        let Some(group) = ui.layout.group(*id) else {
            continue;
        };
        let is_focus = *id == focused;
        draw_strip(cx, buf, g, *id, *rect, group, is_focus, hits, &mut out);
        let body = crate::layout::group_body(*rect);
        let Some(tab) = group.active_tab() else {
            continue;
        };
        match tab.kind {
            Kind::Session => {
                if !is_focus {
                    draw_session_stub(cx, buf, body, &tab.id);
                }
            }
            Kind::File => {
                if is_focus {
                    if let Some(panel) = g.panel {
                        draw_file_view(cx, buf, panel, false, hits);
                    }
                } else {
                    draw_file_mirror(cx, buf, body, &tab.id);
                }
            }
            Kind::Diff => {
                if is_focus {
                    if let Some(panel) = g.panel {
                        draw_diff_view(cx, buf, panel, false, hits);
                    }
                } else {
                    let name = a.tab_title(tab);
                    put(
                        buf,
                        body.x + 2,
                        body.y + 1,
                        &truncate(&name, body.width.saturating_sub(4) as usize, cx.ell()),
                        cx.st(SECONDARY, CANVAS).add_modifier(Modifier::BOLD),
                        body.width.saturating_sub(4),
                    );
                }
            }
            Kind::Terminal => {
                let live = g.terminal == Some(body);
                draw_terminal_body(cx, buf, body, &tab.id, live, hits, &mut out.tips);
            }
            Kind::Browser => {}
        }
    }
    draw_drag(cx, buf, &out.tab_rects);
    out
}

fn tab_glyph(cx: &Cx, tab: &groups::Tab, bg: &str) -> (String, Style) {
    let a = cx.app;
    let ascii = cx.t.opts.ascii;
    match tab.kind {
        Kind::Session if tab.id.is_empty() => ("+".to_owned(), cx.st(MUTED, bg)),
        Kind::Session => session_glyph(cx, &tab.id, bg),
        Kind::File => (
            if ascii { "-" } else { "\u{b7}" }.to_owned(),
            cx.st(MUTED, bg),
        ),
        Kind::Diff => ("\u{b1}".to_owned(), cx.st("status.info", bg)),
        Kind::Terminal => {
            let running = a
                .terminal_panel
                .terminals
                .iter()
                .find(|t| t.id == tab.id)
                .is_some_and(|t| t.status == "running");
            if running {
                (
                    if ascii { "*" } else { "\u{25cf}" }.to_owned(),
                    cx.st(PRIMARY, bg),
                )
            } else {
                (
                    if ascii { "o" } else { "\u{25cb}" }.to_owned(),
                    cx.st(MUTED, bg),
                )
            }
        }
        Kind::Browser => ("\u{b7}".to_owned(), cx.st(MUTED, bg)),
    }
}

#[allow(clippy::too_many_arguments)]
fn draw_strip(
    cx: &Cx,
    buf: &mut Buffer,
    g: &Regions,
    id: GroupId,
    rect: Rect,
    group: &groups::Group,
    focused: bool,
    hits: &mut HitMap,
    out: &mut Drawn,
) {
    let a = cx.app;
    let strip = Rect::new(rect.x, rect.y, rect.width, rect.height.min(1));
    if strip.height == 0 {
        return;
    }
    clear(buf, strip, cx.st(SECONDARY, SIDEBAR));
    // Controls at the right end: [hidden marker] [waiting] [+] [⋯].
    let ascii = cx.t.opts.ascii;
    let menu = Rect::new(
        strip.right().saturating_sub(3),
        strip.y,
        3.min(strip.width),
        1,
    );
    let plus = Rect::new(
        menu.x.saturating_sub(3),
        strip.y,
        3.min(menu.x - strip.x),
        1,
    );
    let mut right = plus.x;
    let hover_style = |hot: bool| {
        if hot {
            cx.st(PRIMARY, SIDEBAR)
        } else {
            cx.st(MUTED, SIDEBAR)
        }
    };
    put(
        buf,
        menu.x + 1,
        menu.y,
        if ascii { "..." } else { "\u{22ef}" },
        hover_style(cx.hovered(menu)),
        menu.width.saturating_sub(1),
    );
    hits.add(menu, Target::GroupMenu(id));
    if cx.hovered(menu) && a.ui.overlay.is_none() {
        out.tips
            .push((menu, a.manifest.ui_text("layout.menu.tooltip", &[])));
    }
    put(
        buf,
        plus.x + 1,
        plus.y,
        "+",
        hover_style(cx.hovered(plus)),
        1,
    );
    hits.add(plus, Target::GroupNew(id));
    if focused {
        let hidden = a.ui.groups.hidden;
        let mut label = String::new();
        if hidden == 1 {
            label = a.manifest.ui_text("layout.hidden_groups.one", &[]);
        } else if hidden > 1 {
            label = a.manifest.ui_text(
                "layout.hidden_groups.many",
                &[("count", &hidden.to_string())],
            );
        } else if !a.session_focused() {
            let waiting = a.wait_items().len();
            if waiting > 0 {
                label = format!("{} {waiting} waiting", if ascii { "!" } else { "\u{26a0}" });
            }
        }
        if !label.is_empty() {
            let w = (width(&label) as u16).min(right.saturating_sub(strip.x + 1));
            let x = right.saturating_sub(w + 1);
            let r = Rect::new(x, strip.y, w, 1);
            put(
                buf,
                x,
                strip.y,
                &truncate(&label, w as usize, cx.ell()),
                cx.st("status.warning", SIDEBAR)
                    .add_modifier(Modifier::BOLD),
                w,
            );
            if hidden > 0 && cx.hovered(r) && a.ui.overlay.is_none() {
                out.tips
                    .push((r, a.manifest.ui_text("layout.hidden_groups.hint", &[])));
            }
            right = x.saturating_sub(1);
        }
    }
    let avail = right.saturating_sub(strip.x) as usize;
    // Titles shrink together before tabs scroll off.
    struct Item<'t> {
        index: usize,
        tab: &'t groups::Tab,
        title: String,
    }
    let mut items: Vec<Item> = group
        .tabs
        .iter()
        .enumerate()
        .map(|(index, tab)| Item {
            index,
            tab,
            title: a.tab_title(tab),
        })
        .collect();
    let cell = |it: &Item, max: usize| width(&truncate(&it.title, max, cx.ell())) + CELL_EXTRA;
    let total =
        |items: &[Item], max: usize| -> usize { items.iter().map(|it| cell(it, max) + 1).sum() };
    let mut max = TITLE_MAX;
    while max > 6 && total(&items, max) > avail {
        max -= 2;
    }
    let mut dropped_left = false;
    while items.len() > 1 && total(&items, max) > avail {
        let active = items
            .iter()
            .position(|i| i.index == group.active)
            .unwrap_or(0);
        if active > 0 {
            items.remove(0);
            dropped_left = true;
        } else {
            items.pop();
        }
    }
    let mut x = strip.x;
    if dropped_left {
        x = put(buf, x, strip.y, cx.ell(), cx.st(MUTED, SIDEBAR), 1) + 1;
    }
    for it in &items {
        let title = truncate(&it.title, max, cx.ell());
        let w = (width(&title) + CELL_EXTRA) as u16;
        if x + w > right {
            break;
        }
        let r = Rect::new(x, strip.y, w, 1);
        let active = it.index == group.active;
        let bg = if active { CANVAS } else { SIDEBAR };
        let mut base = cx.st(
            if active && focused {
                PRIMARY
            } else if active {
                SECONDARY
            } else {
                MUTED
            },
            bg,
        );
        if active && focused {
            base = base.add_modifier(Modifier::BOLD);
        }
        clear(buf, r, base);
        let (glyph, gs) = tab_glyph(cx, it.tab, bg);
        put(buf, x + 1, strip.y, &glyph, gs, 1);
        put(
            buf,
            x + 3,
            strip.y,
            &title,
            base,
            w.saturating_sub(CELL_EXTRA as u16 - 4),
        );
        let close = Rect::new(r.right().saturating_sub(3), strip.y, 3, 1);
        let dirty = it.tab.kind == Kind::File && a.file_dirty(&it.tab.id);
        let hot = cx.hovered(close);
        let (mark, mstyle) = if dirty && !hot {
            (if ascii { "*" } else { "\u{25cf}" }, cx.st(ACCENT, bg))
        } else {
            (
                if ascii { "x" } else { "\u{d7}" },
                if hot {
                    base.patch(cx.t.fg(PRIMARY)).add_modifier(Modifier::BOLD)
                } else {
                    base.patch(cx.t.fg(MUTED))
                },
            )
        };
        put(buf, close.x + 1, strip.y, mark, mstyle, 1);
        hits.add(
            Rect::new(r.x, r.y, r.width.saturating_sub(3), 1),
            Target::GroupTab(id, it.index),
        );
        hits.add(close, Target::GroupTabClose(id, it.index));
        out.tab_rects.push((id, it.index, r));
        x += w + 1;
    }
    let _ = g;
}

/// An unfocused group's session: its own timeline, following the latest
/// message, read-only (no hits, no composer). Its title stands in until the
/// history has loaded.
fn draw_session_stub(cx: &Cx, buf: &mut Buffer, body: Rect, id: &str) {
    let key = (id.to_owned(), body.width.saturating_sub(2));
    if let Some((_, timeline)) = cx.app.ui.groups.bg.get(&key) {
        if !timeline.lines.is_empty() && body.width >= 4 {
            let inner = Rect::new(body.x + 1, body.y, body.width - 2, body.height);
            let h = body.height as usize;
            let top = timeline.lines.len().saturating_sub(h);
            for (i, line) in timeline.lines.iter().skip(top).take(h).enumerate() {
                draw_tline(cx, buf, body, inner, body.y + i as u16, line);
            }
            return;
        }
    }
    let title = cx.app.tab_title(&groups::Tab::session(id.to_owned()));
    put(
        buf,
        body.x + 2,
        body.y + 1,
        &truncate(&title, body.width.saturating_sub(4) as usize, cx.ell()),
        cx.st(SECONDARY, CANVAS).add_modifier(Modifier::BOLD),
        body.width.saturating_sub(4),
    );
}

/// An unfocused group's file: the text this client last saw, plain.
fn draw_file_mirror(cx: &Cx, buf: &mut Buffer, body: Rect, path: &str) {
    let a = cx.app;
    let dirty = a.file_dirty(path);
    let mut title = path.to_owned();
    if dirty {
        title.push_str(if cx.t.opts.ascii { " *" } else { " \u{25cf}" });
    }
    put(
        buf,
        body.x + 1,
        body.y,
        &truncate(&title, body.width.saturating_sub(2) as usize, cx.ell()),
        cx.st(SECONDARY, CANVAS),
        body.width.saturating_sub(2),
    );
    let Some(text) = a.file_tab_text(path) else {
        return;
    };
    let lines: Vec<&str> = text.lines().collect();
    let digits = lines.len().max(1).to_string().len() as u16;
    let gutter = digits + 2;
    let room = body.width.saturating_sub(gutter + 1) as usize;
    for (i, line) in lines
        .iter()
        .enumerate()
        .take(body.height.saturating_sub(2) as usize)
    {
        let y = body.y + 2 + i as u16;
        let n = format!("{:>w$} ", i + 1, w = digits as usize);
        let x = put(buf, body.x + 1, y, &n, cx.st(MUTED, CANVAS), gutter);
        let x = put(buf, x, y, cx.t.structure("rail"), cx.st(MUTED, CANVAS), 1);
        let shown: String = line
            .chars()
            .map(|c| if c == '\t' { ' ' } else { c })
            .collect();
        put(
            buf,
            x + 1,
            y,
            &truncate(&shown, room, ""),
            cx.st(PRIMARY, CANVAS),
            room as u16,
        );
    }
}

/// A terminal tab's body: the toolbar and, for the attached terminal, its screen.
fn draw_terminal_body(
    cx: &Cx,
    buf: &mut Buffer,
    r: Rect,
    id: &str,
    live: bool,
    hits: &mut HitMap,
    tips: &mut Vec<(Rect, String)>,
) {
    clear(buf, r, cx.st(PRIMARY, CANVAS));
    let a = cx.app;
    let ascii = cx.t.opts.ascii;
    if r.height == 0 {
        return;
    }
    let terminal = a.terminal_panel.terminals.iter().find(|t| t.id == id);
    let selected = live && terminal.is_some();
    if !live {
        // Another group's terminal streams into its own screen, read-only.
        if let Some(p) = a.terminal_panel.parked.iter().find(|p| p.id == id) {
            let screen = crate::terminal_panel::screen_area(r, false, p.screen.gap);
            p.screen.render(screen, buf);
            let name = terminal.map_or("terminal", |t| t.name.as_str());
            put(
                buf,
                r.x + 1,
                r.y,
                &truncate(name, r.width.saturating_sub(2) as usize, cx.ell()),
                cx.st(MUTED, CANVAS),
                r.width.saturating_sub(2),
            );
            return;
        }
        let label = if a.terminal_panel.loading {
            "Loading terminals\u{2026}"
        } else {
            "Terminal not attached"
        };
        put(
            buf,
            r.x + 1,
            r.y,
            label,
            cx.st(MUTED, CANVAS),
            r.width.saturating_sub(1),
        );
        return;
    }
    let control = if a.terminal_controlled {
        (
            "[Release control]",
            "Release control",
            Target::TerminalReleaseControl,
        )
    } else {
        (
            "[Take control]",
            "Take control",
            Target::TerminalTakeControl,
        )
    };
    let (rename, copy) = if ascii {
        ("[r]", "=")
    } else {
        ("\u{270e}", cx.t.control("copy"))
    };
    // The shared toolbar order: Rename, Take/Release control, Attach, Copy.
    let actions: Vec<(String, &str, Target, bool)> = vec![
        (
            rename.to_owned(),
            "Rename terminal",
            Target::TerminalRename,
            selected,
        ),
        (control.0.to_owned(), control.1, control.2, selected),
        (
            "[Attach]".to_owned(),
            "Attach selected output",
            Target::TerminalAttach,
            selected,
        ),
        (
            copy.to_owned(),
            "Copy output",
            Target::TerminalCopy,
            selected,
        ),
    ];
    let widths: Vec<u16> = actions
        .iter()
        .map(|(label, ..)| width(label) as u16 + 2)
        .collect();
    let total: u16 = widths.iter().sum();
    let icons_start = r.right().saturating_sub(total).max(r.x);
    let hint = if a.terminal_focused {
        "Ctrl+] tabs"
    } else {
        "Enter open  c take control  r release"
    };
    let hint_room = icons_start.saturating_sub(r.x + 1);
    if hint_room >= 8 {
        put(
            buf,
            r.x + 1,
            r.y,
            &truncate(hint, hint_room as usize, cx.ell()),
            cx.st(MUTED, CANVAS),
            hint_room,
        );
    }
    let mut x = icons_start;
    for ((label, tip, target, enabled), width) in actions.into_iter().zip(widths) {
        if x >= r.right() {
            break;
        }
        let width = width.min(r.right().saturating_sub(x));
        let rect = Rect::new(x, r.y, width, 1);
        let hot = cx.hovered(rect);
        let style = if !enabled {
            cx.st(MUTED, CANVAS)
        } else if hot {
            cx.st(PRIMARY, RAISED)
        } else {
            cx.st(ACCENT, CANVAS)
        };
        if hot {
            clear(buf, rect, style);
        }
        put(buf, x, r.y, &format!(" {label} "), style, width);
        hits.add(rect, target);
        if hot {
            tips.push((rect, tip.to_owned()));
        }
        x = x.saturating_add(width);
    }
    let read_only = selected && !a.terminal_controlled;
    if selected && r.height > 1 {
        let screen = crate::terminal_panel::screen_area(r, read_only, a.terminal_panel.screen.gap);
        a.terminal_panel.screen.render(screen, buf);
        if let Some(selection) = &a.terminal_panel.selection {
            if selection.dragged() && selection.area == screen {
                selection.highlight(buf);
            }
        }
        if read_only {
            let label = "Read-only \u{2014} [Take control] to type.";
            put(
                buf,
                r.x,
                r.y + 1,
                label,
                cx.st("status.warning", CANVAS),
                screen.width,
            );
            let rect = Rect::new(r.x, r.y + 1, (width(label) as u16).min(r.width), 1);
            hits.add(rect, Target::TerminalTakeControl);
        }
        if a.terminal_panel.screen.gap {
            put(
                buf,
                r.x,
                r.y + 1 + u16::from(read_only),
                crate::terminal_screen::GAP_NOTICE,
                cx.st("status.warning", CANVAS),
                screen.width,
            );
        }
    }
}

/// Reverse the cells of `r`: the drop highlight.
fn highlight(buf: &mut Buffer, r: Rect) {
    for y in r.y..r.bottom() {
        for x in r.x..r.right() {
            let style = buf[(x, y)].style().add_modifier(Modifier::REVERSED);
            buf[(x, y)].set_style(style);
        }
    }
}

/// The drop target of a tab or divider drag in progress.
fn draw_drag(cx: &Cx, buf: &mut Buffer, tabs: &[(GroupId, usize, Rect)]) {
    let ui = &cx.app.ui.groups;
    let Some(Drag::Tab {
        moved: true,
        over: Some(over),
        ..
    }) = &ui.drag
    else {
        return;
    };
    match *over {
        Over::Strip { group, at } => {
            let mine: Vec<_> = tabs.iter().filter(|(g, _, _)| *g == group).collect();
            let rect = ui.rects.iter().find(|(g, _)| *g == group).map(|(_, r)| *r);
            let x = mine
                .iter()
                .find(|(_, i, _)| *i == at)
                .map(|(_, _, r)| r.x)
                .or_else(|| mine.last().map(|(_, _, r)| r.right()))
                .or_else(|| rect.map(|r| r.x));
            if let (Some(x), Some(rect)) = (x, rect) {
                highlight(
                    buf,
                    Rect::new(x.min(rect.right().saturating_sub(1)), rect.y, 1, 1),
                );
            }
        }
        Over::Body { group, zone } => {
            if let Some((_, rect)) = ui.rects.iter().find(|(g, _)| *g == group) {
                let body = crate::layout::group_body(*rect);
                highlight(buf, groups::zone_rect(body, zone));
            }
        }
    }
}

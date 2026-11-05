//! Manager header indicator and notification dropdown, painted over the finished frame.

use std::time::Instant;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;
use serde_json::Value;

use crate::app::App;
use crate::indicator::{self, blend, Anchor};
use crate::layout::HitMap;
use crate::manager::{RowKind, RowView, PLACEHOLDER};
use crate::markdown::{self, wrap_spans};
use crate::paint;
use crate::text::{truncate, width};
use crate::theme::{Theme, Tier};
use crate::ui::Target;

const RAISED: &str = "surface.raised";
const PANEL: &str = "surface.panel";
const PRIMARY: &str = "text.primary";
const SECONDARY: &str = "text.secondary";
const MUTED: &str = "text.muted";

/// The scrim under a focused card is far lighter than a normal modal's.
pub const SCRIM_PERCENT: u8 = 25;
/// An open Manager dropdown dims everything outside it, so the panel owns the screen.
pub const DROPDOWN_SCRIM_PERCENT: u8 = 40;
/// How much of the raised surface shows over what is underneath.
pub const PANEL_OPACITY: f32 = 0.8;

type Line = Vec<Span<'static>>;

struct Look<'a> {
    t: &'a Theme,
}

impl Look<'_> {
    fn st(&self, fg: &str) -> Style {
        self.t.style(fg, RAISED)
    }
    fn bold(&self, fg: &str) -> Style {
        self.st(fg).add_modifier(Modifier::BOLD)
    }
    fn ell(&self) -> &str {
        self.t.structure("ellipsis")
    }
    fn sep(&self) -> String {
        format!(" {} ", self.t.structure("separator"))
    }
    fn blended(&self) -> bool {
        self.t.opts.tier == Tier::TrueColor
    }
}

fn put(buf: &mut Buffer, x: u16, y: u16, text: &str, style: Style, max: u16) -> u16 {
    if max == 0 || y >= buf.area.bottom() || x >= buf.area.right() {
        return x;
    }
    buf.set_stringn(x, y, text, max as usize, style).0
}

/// Draw `line` at `x`, `y` within `max` cells.
fn put_line(buf: &mut Buffer, x: u16, y: u16, line: &Line, max: u16) {
    let mut at = x;
    let end = x.saturating_add(max);
    for span in line {
        if at >= end {
            break;
        }
        at = put(buf, at, y, &span.content, span.style, end - at);
    }
}

fn line_width(line: &Line) -> usize {
    line.iter().map(|s| width(&s.content)).sum()
}

fn plain(text: &str, style: Style) -> Line {
    vec![Span::styled(text.to_owned(), style)]
}

/// Wrap `text` to `w` cells in one style.
fn wrapped(text: &str, w: usize, style: Style) -> Vec<Line> {
    text.lines()
        .flat_map(|line| wrap_spans(&[Span::styled(line.to_owned(), style)], w.max(1)))
        .collect()
}

/// Paint the stage over a finished frame. Cheap when there is nothing to show.
pub fn draw(
    app: &App,
    buf: &mut Buffer,
    area: Rect,
    composer: Rect,
    header: Rect,
    hits: &mut HitMap,
) {
    let m = &app.ui.manager;
    m.cursor.set(None);
    if !m.live() {
        return;
    }
    let now = Instant::now();
    let t = &app.theme;
    let look = Look { t };

    if let Some(consent) = m.consent.as_ref() {
        paint::scrim_by(t, buf, area, SCRIM_PERCENT);
        hits.add(area, Target::Swallow);
        let card = crate::manager::CardState {
            id: String::new(),
            value: serde_json::json!({
                "type": "consent",
                "title": "Before the Manager starts",
                "notice": consent.notice,
                "provider": consent.provider,
            }),
            focused: true,
            shown: now,
            collapsed: false,
            scroll: 0,
        };
        let (_, bottom, bounds) = if composer.height > 0 && composer.width > 0 {
            (composer.x + composer.width / 2, composer.y, composer)
        } else {
            (
                area.x + area.width / 2,
                area.bottom().saturating_sub(1),
                area,
            )
        };
        draw_card(
            app, &look, buf, area, bounds, bottom, &card, 1.0, hits, false,
        );
        return;
    }
    // The header owns audio; the dropdown owns text, cards and debug navigation.
    let strip = Rect::new(header.x, header.y, header.width, 1).intersection(area);
    if strip.width > 0 {
        hits.add(strip, Target::ManagerIndicator);
        draw_indicator(app, buf, area, strip);
    }
    if m.dropdown_open {
        // Everything outside the panel recedes; the header indicator stays clickable.
        paint::scrim_by(t, buf, area, DROPDOWN_SCRIM_PERCENT);
    }
    // A hovered indicator that can stop the live voice says so, over the scrim.
    if strip.width > 0 && m.live_call && hovered(app, strip) {
        draw_stop_label(buf, strip, t);
    }
    if m.dropdown_open {
        draw_dropdown(app, &look, buf, area, header, hits);
    }
}

fn hovered(app: &App, strip: Rect) -> bool {
    app.mouse.is_some_and(|(x, y)| {
        x >= strip.x && x < strip.right() && y >= strip.y && y < strip.bottom()
    })
}

/// Notification-style popover, anchored below the first row, over the scrim.
fn draw_dropdown(
    app: &App,
    look: &Look,
    buf: &mut Buffer,
    area: Rect,
    header: Rect,
    hits: &mut HitMap,
) {
    let m = &app.ui.manager;
    let w = 68.min(area.width.saturating_sub(2));
    let max_h = 24.min(area.height.saturating_sub(2));
    if w < 12 || max_h < 10 {
        return;
    }
    let mut lines = wrapped(
        &app.manager_dropdown_text(),
        (w - 4) as usize,
        look.st(PRIMARY),
    );
    let mut rows = Vec::new();
    if let Some(card) = m.card.as_ref() {
        lines.push(Vec::new());
        lines.extend(wrapped(
            &default_title(&card.value),
            (w - 4) as usize,
            look.bold(PRIMARY),
        ));
        let offset = lines.len();
        let built = build(app, look, &card.value, (w - 4) as usize);
        lines.extend(built.lines);
        rows = built.rows;
        for row in &mut rows {
            row.line += offset;
        }
    }
    let h = (lines.len() as u16 + 8).clamp(10, max_h);
    let r = Rect::new(
        header
            .right()
            .min(area.right())
            .saturating_sub(w)
            .max(area.x),
        header.bottom(),
        w,
        h,
    )
    .intersection(area);
    buf.set_style(r, look.st(PRIMARY));
    for y in r.y..r.bottom() {
        put(
            buf,
            r.x,
            y,
            &" ".repeat(r.width as usize),
            look.st(PRIMARY),
            r.width,
        );
    }
    hits.add(r, Target::Swallow);
    put(buf, r.x + 2, r.y + 1, "Manager", look.bold(PRIMARY), w - 4);
    let inner = Rect::new(r.x + 2, r.y + 2, w - 4, h - 8);
    let max = lines.len().saturating_sub(inner.height as usize);
    m.details_max.set(max);
    let scroll = m.details_scroll.min(max);
    hits.add(inner, Target::ManagerDetails);
    for (i, line) in lines
        .iter()
        .skip(scroll)
        .take(inner.height as usize)
        .enumerate()
    {
        put_line(buf, inner.x, inner.y + i as u16, line, inner.width);
    }
    for row in rows {
        if row.line >= scroll && row.line < scroll + inner.height as usize {
            hits.add(
                Rect::new(
                    inner.x + row.x as u16,
                    inner.y + (row.line - scroll) as u16,
                    row.w.min(inner.width as usize) as u16,
                    row.count.min(1) as u16,
                ),
                Target::ManagerRow(row.n),
            );
        }
    }
    let input = Rect::new(inner.x, r.bottom() - 6, inner.width, 3);
    let input_style = look.t.style(PRIMARY, PANEL);
    for y in input.y..input.bottom() {
        put(
            buf,
            input.x,
            y,
            &" ".repeat(input.width as usize),
            input_style,
            input.width,
        );
    }
    let text = m.field.as_ref().map(|f| f.text()).unwrap_or_default();
    put(
        buf,
        input.x + 1,
        input.y + 1,
        if text.is_empty() { PLACEHOLDER } else { &text },
        look.t
            .style(if text.is_empty() { SECONDARY } else { PRIMARY }, PANEL),
        input.width.saturating_sub(2),
    );
    hits.add(input, Target::ManagerInput);
    if m.dropdown_interactive {
        m.cursor.set(Some((
            input.x + 1 + width(&text).min(input.width.saturating_sub(3) as usize) as u16,
            input.y + 1,
        )));
    }
    let footer = Rect::new(inner.x, r.bottom() - 2, inner.width, 1);
    let voice = Rect::new(footer.x, footer.y, 13.min(footer.width), 1);
    put(
        buf,
        voice.x,
        voice.y,
        if m.voice_enabled {
            "Voice: on"
        } else {
            "Voice: off"
        },
        look.bold(PRIMARY),
        voice.width,
    );
    hits.add(voice, Target::ManagerVoice);
    let button = Rect::new(
        footer.right().saturating_sub(20).max(voice.right()),
        footer.y,
        20.min(footer.width.saturating_sub(voice.width)),
        1,
    );
    put(
        buf,
        button.x,
        button.y,
        "Open Manager session",
        look.bold(PRIMARY),
        button.width,
    );
    hits.add(button, Target::ManagerDebugSession);
}

fn fill(look: &Look, buf: &mut Buffer, r: Rect) -> Vec<Color> {
    let base = look.st(PRIMARY);
    let mut under = Vec::with_capacity(usize::from(r.width) * usize::from(r.height));
    for y in r.y..r.bottom() {
        for x in r.x..r.right() {
            let cell = &mut buf[(x, y)];
            under.push(cell.bg);
            cell.reset();
            cell.set_style(base);
        }
    }
    under
}

/// 80% of the raised surface over the colours `fill` remembered, in true colour.
/// Other tiers keep the solid surface.
fn finish(look: &Look, buf: &mut Buffer, r: Rect, under: &[Color], opacity: f32) {
    let Some(raised) = look.t.color(RAISED).filter(|_| look.blended()) else {
        return;
    };
    let mut at = 0;
    for y in r.y..r.bottom() {
        for x in r.x..r.right() {
            let cell = &mut buf[(x, y)];
            if cell.bg == raised {
                cell.bg = blend(under[at], raised, opacity);
            }
            at += 1;
        }
    }
}

// ---- the card ---------------------------------------------------------------------------------

/// A pickable row: its first line, how many lines, its number, and the cells it spans.
struct RowHit {
    line: usize,
    count: usize,
    n: usize,
    x: usize,
    w: usize,
}

struct Built {
    lines: Vec<Line>,
    rows: Vec<RowHit>,
}

fn glyph(app: &App, look: &Look, kind: RowKind) -> Span<'static> {
    let t = look.t;
    let (g, tok) = match kind {
        RowKind::Needs => t.attention("waiting"),
        RowKind::Failed => t.attention("error"),
        RowKind::Running => {
            let (g, _, tok) = t.status("running");
            (g, tok)
        }
        RowKind::Done => {
            let (g, _, tok) = t.status("done");
            (g, tok)
        }
        RowKind::Idle => (if t.opts.ascii { "." } else { "\u{b7}" }, MUTED),
    };
    let _ = app;
    Span::styled(g.to_owned(), look.st(tok))
}

fn default_title(card: &Value) -> String {
    if let Some(title) = card["title"].as_str().filter(|t| !t.is_empty()) {
        return title.to_owned();
    }
    match card["type"].as_str().unwrap_or("") {
        "board" => card["project"]
            .as_str()
            .map_or("Sessions".to_owned(), |project| {
                format!("Sessions in {project}")
            }),
        "sessions" => "Matching sessions".to_owned(),
        "approval" => "Approval".to_owned(),
        "question" => "Question".to_owned(),
        "diagram" => "Diagram".to_owned(),
        "excerpt" => "Excerpt".to_owned(),
        "note" => "Note".to_owned(),
        other => other.to_owned(),
    }
}

fn clipped_line(look: &Look) -> Line {
    plain(&format!("{} cut", look.ell()), look.st(MUTED))
}

/// A markdown body as lines of `w` cells. Rows that would be images are left out.
fn markdown_lines(look: &Look, text: &str, w: usize, tint: Option<&str>) -> Vec<Line> {
    let palette = markdown::Palette::from_theme(look.t);
    let base = look.st(PRIMARY);
    let mut out: Vec<Line> = Vec::new();
    for row in markdown::lines(text, &palette, w) {
        if row.blank {
            out.push(Vec::new());
            continue;
        }
        if row.image.is_some() {
            continue;
        }
        let prefix_w: usize = row.prefix.iter().map(|s| width(&s.content)).sum();
        for (i, part) in wrap_spans(&row.spans, w.saturating_sub(prefix_w).max(1))
            .into_iter()
            .enumerate()
        {
            let mut line: Line = if i == 0 {
                row.prefix.clone()
            } else {
                vec![Span::raw(" ".repeat(prefix_w))]
            };
            line.extend(part);
            out.push(line);
        }
    }
    // Spans from the renderer carry their own colours; give the rest the card's.
    for line in &mut out {
        for span in line.iter_mut() {
            let mut style = base.patch(span.style);
            if let Some(tint) = tint {
                if span.style.fg.is_none() || span.style.fg == look.t.color(PRIMARY) {
                    style = style.patch(look.st(tint));
                }
            }
            if span.style.bg.is_none() {
                style.bg = base.bg;
            }
            span.style = style;
        }
    }
    out
}

fn build(app: &App, look: &Look, card: &Value, w: usize) -> Built {
    let mut lines: Vec<Line> = Vec::new();
    let mut rows: Vec<RowHit> = Vec::new();
    let t = look.t;
    let ell = look.ell().to_owned();
    match card["type"].as_str().unwrap_or("") {
        "board" => {
            let all = app.card_rows(card);
            let columns = [
                ("needs_you", "Needs you"),
                ("running", "Running"),
                ("finished", "Finished"),
            ];
            let buckets: Vec<(&str, Vec<&RowView>, u64)> = columns
                .iter()
                .enumerate()
                .map(|(index, (key, label))| {
                    (
                        *label,
                        all.iter().filter(|r| r.column == index).collect::<Vec<_>>(),
                        card["more"][key].as_u64().unwrap_or(0),
                    )
                })
                .collect();
            if w >= 60 {
                let gap = 2usize;
                let cw = (w - gap * 2) / 3;
                let tallest = buckets
                    .iter()
                    .map(|(_, rows, more)| 1 + rows.len().max(1) * 2 + usize::from(*more > 0))
                    .max()
                    .unwrap_or(1);
                let base = lines.len();
                for _ in 0..tallest {
                    lines.push(Vec::new());
                }
                for (c, (label, bucket, more)) in buckets.iter().enumerate() {
                    let x = c * (cw + gap);
                    let put_at = |lines: &mut Vec<Line>, row: usize, text: String, style: Style| {
                        let line = &mut lines[base + row];
                        let have = line_width(line);
                        if have < x {
                            line.push(Span::raw(" ".repeat(x - have)));
                        }
                        line.push(Span::styled(text, style));
                    };
                    put_at(
                        &mut lines,
                        0,
                        truncate(label, cw, &ell),
                        look.bold(SECONDARY),
                    );
                    if bucket.is_empty() {
                        put_at(&mut lines, 1, "none".to_owned(), look.st(MUTED));
                    }
                    let mut at = 1;
                    for r in bucket {
                        let head = format!("{} {}", r.n, r.handle);
                        let head_style = if r.kind == RowKind::Needs {
                            look.bold(PRIMARY)
                        } else {
                            look.st(PRIMARY)
                        };
                        let g = glyph(app, look, r.kind);
                        let head_w = cw.saturating_sub(2);
                        let line = &mut lines[base + at];
                        let have = line_width(line);
                        if have < x {
                            line.push(Span::raw(" ".repeat(x - have)));
                        }
                        let shown = truncate(&head, head_w, &ell);
                        let pad = head_w.saturating_sub(width(&shown));
                        line.push(Span::styled(shown, head_style));
                        line.push(Span::raw(" ".repeat(pad + 1)));
                        line.push(g);
                        put_at(
                            &mut lines,
                            at + 1,
                            format!("  {}", truncate(&r.title, cw.saturating_sub(2), &ell)),
                            look.st(MUTED),
                        );
                        rows.push(RowHit {
                            line: base + at,
                            count: 2,
                            n: r.n,
                            x,
                            w: cw,
                        });
                        at += 2;
                    }
                    if *more > 0 {
                        put_at(&mut lines, at, format!("+{more} more"), look.st(MUTED));
                    }
                }
            } else {
                for (i, (label, bucket, more)) in buckets.iter().enumerate() {
                    if i > 0 {
                        lines.push(Vec::new());
                    }
                    lines.push(plain(label, look.bold(SECONDARY)));
                    if bucket.is_empty() {
                        lines.push(plain("none", look.st(MUTED)));
                    }
                    for r in bucket {
                        let head = format!("{} {}", r.n, r.handle);
                        let room = w.saturating_sub(width(&head) + 4);
                        let mut line = vec![
                            Span::styled(head, look.st(PRIMARY)),
                            Span::raw("  "),
                            Span::styled(truncate(&r.title, room, &ell), look.st(MUTED)),
                        ];
                        let have = line_width(&line);
                        if have + 2 <= w {
                            line.push(Span::raw(" ".repeat(w - have - 1)));
                            line.push(glyph(app, look, r.kind));
                        }
                        rows.push(RowHit {
                            line: lines.len(),
                            count: 1,
                            n: r.n,
                            x: 0,
                            w,
                        });
                        lines.push(line);
                    }
                    if *more > 0 {
                        lines.push(plain(&format!("+{more} more"), look.st(MUTED)));
                    }
                }
            }
        }
        "sessions" => {
            for r in app.card_rows(card) {
                let first = lines.len();
                let head = format!("{} {}", r.n, r.handle);
                let state = r.state.to_string();
                let mut line = vec![Span::styled(head.clone(), look.bold(PRIMARY))];
                if !r.project.is_empty() {
                    line.push(Span::styled(format!("  {}", r.project), look.st(MUTED)));
                }
                let have = line_width(&line);
                let right = width(&state) + 2;
                if have + right < w {
                    line.push(Span::raw(" ".repeat(w - have - right)));
                    line.push(glyph(app, look, r.kind));
                    line.push(Span::styled(format!(" {state}"), look.st(MUTED)));
                }
                lines.push(line);
                lines.push(plain(&truncate(&r.title, w, &ell), look.st(SECONDARY)));
                if !r.why.is_empty() {
                    lines.push(plain(&truncate(&r.why, w, &ell), look.st(MUTED)));
                }
                rows.push(RowHit {
                    line: first,
                    count: lines.len() - first,
                    n: r.n,
                    x: 0,
                    w,
                });
            }
            let more = card["more"].as_u64().unwrap_or(0);
            if more > 0 {
                lines.push(plain(&format!("+{more} more"), look.st(MUTED)));
            }
        }
        "approval" => {
            let handle = card["handle"].as_str().unwrap_or("");
            lines.push(vec![
                Span::styled(handle.to_owned(), look.bold(PRIMARY)),
                Span::raw(" "),
                Span::styled(
                    t.attention("waiting").0.to_owned(),
                    look.st(t.attention("waiting").1),
                ),
            ]);
            if let Some(summary) = card["summary"].as_str().filter(|s| !s.is_empty()) {
                lines.extend(wrapped(summary, w, look.st(SECONDARY)));
            }
            lines.push(Vec::new());
            let code = t.style(PRIMARY, PANEL);
            let detail = card["detail"].as_str().unwrap_or("");
            let mut body: Vec<Line> = detail
                .lines()
                .flat_map(|l| {
                    wrap_spans(
                        &[Span::styled(l.replace('\t', "  "), code)],
                        w.saturating_sub(2).max(1),
                    )
                })
                .collect();
            let local_cut = body.len() > 10;
            body.truncate(10);
            for line in body {
                let pad = w.saturating_sub(line_width(&line) + 2);
                let mut row: Line = vec![Span::styled(" ".to_owned(), code)];
                row.extend(line);
                row.push(Span::styled(" ".repeat(pad + 1), code));
                lines.push(row);
            }
            if card["clipped"].as_bool() == Some(true) || local_cut {
                lines.push(clipped_line(look));
            }
            lines.push(Vec::new());
            lines.push(plain("Say yes or no.", look.st(MUTED)));
        }
        "question" => {
            let handle = card["handle"].as_str().unwrap_or("");
            lines.push(vec![
                Span::styled(handle.to_owned(), look.bold(PRIMARY)),
                Span::raw(" "),
                Span::styled(
                    t.attention("waiting").0.to_owned(),
                    look.st(t.attention("waiting").1),
                ),
            ]);
            lines.extend(wrapped(
                card["question"].as_str().unwrap_or(""),
                w,
                look.st(PRIMARY),
            ));
            lines.push(Vec::new());
            for option in card["options"].as_array().into_iter().flatten() {
                let text = option.as_str().unwrap_or("");
                for (i, part) in wrapped(text, w.saturating_sub(2), look.st(SECONDARY))
                    .into_iter()
                    .enumerate()
                {
                    let mut line = vec![Span::styled(
                        if i == 0 { "\u{2022} " } else { "  " }.to_owned(),
                        look.st(MUTED),
                    )];
                    line.extend(part);
                    lines.push(line);
                }
            }
            lines.push(Vec::new());
            lines.push(plain("Say your answer.", look.st(MUTED)));
        }
        "diagram" => {
            let source = card["source"].as_str().unwrap_or("");
            let palette = markdown::Palette::from_theme(t);
            match markdown::mermaid_cached(source, &palette, w) {
                Some(art) => {
                    let base = look.st(PRIMARY);
                    for line in art.iter() {
                        lines.push(
                            line.spans
                                .iter()
                                .map(|s| {
                                    let mut style = base.patch(s.style);
                                    style.bg = base.bg;
                                    Span::styled(s.content.to_string(), style)
                                })
                                .collect(),
                        );
                    }
                }
                None => {
                    // The renderer rejected it: the source, in a code block, as the timeline does.
                    let fenced = format!("```\n{source}\n```");
                    lines.extend(markdown_lines(look, &fenced, w, None));
                }
            }
            if card["clipped"].as_bool() == Some(true) {
                lines.push(clipped_line(look));
            }
        }
        "excerpt" => {
            let handle = card["handle"].as_str().unwrap_or("a session");
            lines.push(vec![
                Span::styled("from ", look.st(MUTED)),
                Span::styled(handle.to_owned(), look.bold(SECONDARY)),
            ]);
            let rail = t.structure("rail").to_owned();
            for line in markdown_lines(
                look,
                card["markdown"].as_str().unwrap_or(""),
                w.saturating_sub(2),
                Some(SECONDARY),
            ) {
                let mut row = vec![Span::styled(format!("{rail} "), look.st(MUTED))];
                row.extend(line);
                lines.push(row);
            }
            if card["clipped"].as_bool() == Some(true) {
                lines.push(vec![
                    Span::styled(format!("{rail} "), look.st(MUTED)),
                    Span::styled(format!("{} cut", look.ell()), look.st(MUTED)),
                ]);
            }
        }
        "consent" => {
            lines.extend(wrapped(
                card["notice"].as_str().unwrap_or(""),
                w,
                look.st(PRIMARY),
            ));
        }
        "note" => {
            lines.extend(markdown_lines(
                look,
                card["markdown"].as_str().unwrap_or(""),
                w,
                None,
            ));
            if card["clipped"].as_bool() == Some(true) {
                lines.push(clipped_line(look));
            }
        }
        _ => {}
    }
    // Trailing blank lines make no sense in a panel.
    while lines.last().is_some_and(Vec::is_empty) {
        lines.pop();
    }
    Built { lines, rows }
}

/// `1-6 open · space talk · alt+shift+d dismiss`: only the hints that apply, in this order.
fn hints(look: &Look, max_n: usize, focused: bool, consent: bool, dismiss: &str) -> Option<Line> {
    if !focused {
        return None;
    }
    let mut parts: Vec<(String, &str)> = Vec::new();
    if consent {
        parts.push(("enter".to_owned(), "accept"));
        parts.push(("esc".to_owned(), "cancel"));
        return Some(join_hints(look, parts));
    }
    if max_n > 0 {
        let keys = if max_n == 1 {
            "1".to_owned()
        } else {
            format!("1-{}", max_n.min(9))
        };
        parts.push((keys, "open"));
    }
    parts.push(("space".to_owned(), "talk"));
    parts.push((dismiss.to_owned(), "dismiss"));
    Some(join_hints(look, parts))
}

fn join_hints(look: &Look, parts: Vec<(String, &str)>) -> Line {
    let mut line: Line = Vec::new();
    for (i, (key, label)) in parts.into_iter().enumerate() {
        if i > 0 {
            line.push(Span::styled(look.sep(), look.st(MUTED)));
        }
        line.push(Span::styled(key, look.bold(PRIMARY)));
        line.push(Span::styled(format!(" {label}"), look.st(MUTED)));
    }
    line
}

#[allow(clippy::too_many_arguments)]
fn draw_card(
    app: &App,
    look: &Look,
    buf: &mut Buffer,
    area: Rect,
    bounds: Rect,
    top: u16,
    card: &crate::manager::CardState,
    enter: f32,
    hits: &mut HitMap,
    below: bool,
) {
    let value = &card.value;
    let ty = value["type"].as_str().unwrap_or("");
    let preferred: u16 = match ty {
        "board" => 96,
        "diagram" => 92,
        "sessions" | "excerpt" | "approval" => 88,
        _ => 72,
    };
    // Wider than the centre column when it must be: a wrapped card reads worse than an overlapped sidebar.
    let avail_w = area.width.saturating_sub(4);
    let panel_w = preferred.min(avail_w).max(24.min(area.width));
    let inner_w = panel_w.saturating_sub(6) as usize;
    let built = build(app, look, value, inner_w.max(8));
    let dismiss = app
        .manifest
        .command("manager.card_dismiss")
        .and_then(|c| c.tui_key())
        .unwrap_or("alt+shift+d")
        .to_owned();
    let hint = hints(
        look,
        app.card_max_n(),
        card.focused,
        ty == "consent",
        &dismiss,
    );
    // Padding: 1 row top and bottom, 3 cells left and right. A quiet title, a gap, the body, a gap, the hints.
    let chrome = 2 + 2 + if hint.is_some() { 2 } else { 0 };
    let room = if below {
        area.bottom().saturating_sub(top + 1)
    } else {
        top.saturating_sub(area.y + 1)
    }
    .min(area.height * 7 / 10) as usize;
    let max_body = room.saturating_sub(chrome).max(1);
    let mut body = built.lines.clone();
    let cut = body.len() > max_body;
    if cut {
        body.truncate(max_body.saturating_sub(1).max(1));
        body.push(plain(look.ell(), look.st(MUTED)));
    }
    let height = (body.len() + chrome) as u16;
    // Rises a few cells as it fades in.
    let lift = ((1.0 - enter) * 2.0).round() as u16;
    let y = if below {
        top
    } else {
        top.saturating_sub(1)
            .saturating_sub(height)
            .saturating_add(lift)
            .max(area.y)
    };
    let x = (bounds.x + bounds.width / 2)
        .saturating_sub(panel_w / 2)
        .max(area.x);
    let x = x.min(area.right().saturating_sub(panel_w));
    let r = Rect::new(x, y, panel_w, height).intersection(area);
    if r.height < 3 || r.width < 12 {
        return;
    }
    // Remember what is underneath so the surface can be blended over it.
    let under = fill(look, buf, r);
    if look.t.mono() {
        let set = look.t.border_set();
        let st = look.st(MUTED);
        for xx in r.x + 1..r.right().saturating_sub(1) {
            put(buf, xx, r.y, set.horizontal_top, st, 1);
            put(buf, xx, r.bottom() - 1, set.horizontal_bottom, st, 1);
        }
        for yy in r.y + 1..r.bottom().saturating_sub(1) {
            put(buf, r.x, yy, set.vertical_left, st, 1);
            put(buf, r.right() - 1, yy, set.vertical_right, st, 1);
        }
        put(buf, r.x, r.y, set.top_left, st, 1);
        put(buf, r.right() - 1, r.y, set.top_right, st, 1);
        put(buf, r.x, r.bottom() - 1, set.bottom_left, st, 1);
        put(buf, r.right() - 1, r.bottom() - 1, set.bottom_right, st, 1);
    }
    let ix = r.x + 3;
    let iw = r.width.saturating_sub(6);
    let mut row = r.y + 1;
    put(
        buf,
        ix,
        row,
        &truncate(&default_title(value), iw as usize, look.ell()),
        look.st(MUTED),
        iw,
    );
    row += 2;
    let body_top = row;
    for (i, line) in body.iter().enumerate() {
        if row >= r.bottom().saturating_sub(1) {
            break;
        }
        // A code band runs the whole width of the panel body.
        put_line(buf, ix, row, line, iw);
        let _ = i;
        row += 1;
    }
    if let Some(hint) = &hint {
        put_line(buf, ix, r.bottom().saturating_sub(2), hint, iw);
    }
    finish(look, buf, r, &under, PANEL_OPACITY);
    // The card is a surface: clicks stop here. A focused card's rows can be picked.
    if card.focused {
        hits.add(r, Target::Swallow);
        for hit in &built.rows {
            let at = body_top + hit.line as u16;
            if at + hit.count as u16 <= r.bottom().saturating_sub(2) {
                hits.add(
                    Rect::new(
                        ix + hit.x as u16,
                        at,
                        (hit.w as u16).min(iw),
                        hit.count as u16,
                    ),
                    Target::ManagerRow(hit.n),
                );
            }
        }
    } else {
        hits.add(r, Target::ManagerCard);
    }
    // Fade in: the words come up out of the surface.
    if enter < 1.0 && look.blended() {
        for yy in r.y..r.bottom() {
            for xx in r.x..r.right() {
                let cell = &mut buf[(xx, yy)];
                cell.fg = blend(cell.bg, cell.fg, enter);
            }
        }
    }
}

/// The braille wave field in the header strip.
fn draw_indicator(app: &App, buf: &mut Buffer, area: Rect, strip: Rect) {
    indicator::paint_sized(
        buf,
        area,
        Anchor {
            x: strip.x + strip.width / 2,
            bottom: strip.bottom(),
        },
        strip.width,
        1,
        &app.ui.manager.indicator,
        &app.theme,
    );
}

/// The live voice is on: hovering the indicator offers to stop the voice, not the call.
fn draw_stop_label(buf: &mut Buffer, strip: Rect, theme: &Theme) {
    let width = 6.min(strip.width);
    if width < 4 {
        return;
    }
    let x = strip.x + (strip.width - width) / 2;
    let style = theme.style(PRIMARY, RAISED);
    for xx in x..x + width {
        let cell = &mut buf[(xx, strip.y)];
        cell.set_symbol(" ");
        cell.set_style(style);
        cell.modifier = Modifier::empty();
    }
    put(buf, x + 1, strip.y, "Stop", style, width - 1);
}

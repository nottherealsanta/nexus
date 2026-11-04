//! The Manager's stage (docs/manager-design.md 10.1, 10.3, 10.4): the caption, the
//! typed field, the card in its semi-transparent modal, and the orb on top.
//! Everything is painted over the finished frame, so no layout moves.
//!
//! True colour blends: the scrim moves every cell 25% toward the canvas, the
//! panel and the pills are the raised surface blended 80% over whatever is
//! underneath, and the orb is alpha-blended last. The other colour tiers draw
//! solid surfaces and a static glyph.

use std::time::Instant;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;
use serde_json::Value;

use crate::app::App;
use crate::layout::HitMap;
use crate::manager::{RowKind, RowView, CAPTION_FADE, CAPTION_QUIET, CARD_ENTER, PLACEHOLDER};
use crate::markdown::{self, wrap_spans};
use crate::orb::{self, blend, Anchor, OrbState};
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
/// A focused call dims the whole app to about 30%, so the call reads as the
/// thing being used and the session behind never competes with it.
pub const CALL_SCRIM_PERCENT: u8 = 70;
/// How much of the raised surface shows over what is underneath.
pub const PANEL_OPACITY: f32 = 0.8;

fn ease(x: f32) -> f32 {
    let x = x.clamp(0.0, 1.0);
    x * x * (3.0 - 2.0 * x)
}

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
pub fn draw(app: &App, buf: &mut Buffer, area: Rect, composer: Rect, hits: &mut HitMap) {
    let m = &app.ui.manager;
    m.cursor.set(None);
    if !m.orb.visible() || m.consent.is_some() || m.context.is_some() {
        app.orb_sprites.borrow_mut().sync(None);
    }
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
    if m.focused_call() {
        paint::scrim_by(t, buf, area, CALL_SCRIM_PERCENT);
        draw_focused(app, &look, buf, area, now, hits);
        return;
    }
    if m.minimised_call() {
        draw_minimised(app, &look, buf, area, composer, now, hits);
    }
}

/// The focused call: a large orb centred, the caption under it, the hint and
/// End under that, and a card below them all. The app behind is dimmed.
fn draw_focused(
    app: &App,
    look: &Look,
    buf: &mut Buffer,
    area: Rect,
    now: Instant,
    hits: &mut HitMap,
) {
    let m = &app.ui.manager;
    // The call takes the stage: a click outside its controls goes nowhere.
    hits.add(area, Target::Swallow);
    let (ow, oh) = orb::focused_size(area);
    let centre_x = area.x + area.width / 2;
    // A card needs room below the caption, so the orb steps up when one is up.
    let spacer = if m.card.as_ref().is_some_and(|c| !c.collapsed) {
        (area.height.saturating_sub(oh) / 5).max(1)
    } else {
        (area.height.saturating_sub(oh) * 2 / 5).max(1)
    };
    let top = area.y.saturating_add(spacer);
    let orb_rect = Rect::new(centre_x.saturating_sub(ow / 2), top, ow, oh).intersection(area);
    if m.orb.presence > 0.5 && orb_rect.width > 0 && orb_rect.height > 0 {
        hits.add(orb_rect, Target::ManagerOrb);
    }
    let anchor = Anchor {
        x: centre_x,
        bottom: orb_rect.bottom(),
    };
    let mut y = orb_rect.bottom().saturating_add(1);
    if m.field.is_some() {
        y = draw_field_at(app, look, buf, area, y).saturating_add(1);
    }
    let target = app.orb_target(now);
    if let Some((lines, fade)) = caption_lines(app, look, now, target) {
        y = draw_caption_below(look, buf, area, y, &lines, fade).saturating_add(1);
    }
    y = draw_preview(app, look, buf, area, y);
    let hint = app.manager_hint();
    if !hint.is_empty() {
        draw_hint(look, buf, area, y, &hint, false);
        y = y.saturating_add(1);
    }
    if y < area.bottom() {
        y = draw_end(look, buf, area, y, false, hits).saturating_add(1);
    }
    if let Some(card) = m.card.as_ref().filter(|c| !c.collapsed) {
        let enter = if look.t.opts.reduced_motion {
            1.0
        } else {
            ease(now.saturating_duration_since(card.shown).as_secs_f32() / CARD_ENTER.as_secs_f32())
        };
        if card.focused {
            hits.add(area, Target::ManagerScrim);
        }
        draw_card(app, look, buf, area, area, y, card, enter, hits, true);
    }
    draw_orb(app, buf, area, anchor, ow, oh);
}

/// The minimised call: a small orb in the bottom-right corner, its caption
/// under it, then the hint and End. The app keeps its full strength and keys.
fn draw_minimised(
    app: &App,
    look: &Look,
    buf: &mut Buffer,
    area: Rect,
    composer: Rect,
    now: Instant,
    hits: &mut HitMap,
) {
    let m = &app.ui.manager;
    let (ow, oh) = (orb::WIDTH, orb::HEIGHT);
    let right = area.right().saturating_sub(2);
    let bottom = if composer.height > 0 && composer.width > 0 {
        composer.y
    } else {
        area.bottom().saturating_sub(1)
    };
    let end_y = bottom.saturating_sub(1);
    let hint_y = end_y.saturating_sub(1);
    if end_y < area.y || hint_y < area.y {
        return;
    }
    draw_hint(look, buf, area, hint_y, &app.manager_hint(), true);
    let mut orb_bottom = hint_y;
    let target = app.orb_target(now);
    if let Some((lines, fade)) = caption_lines(app, look, now, target) {
        let caption = draw_caption_right(look, buf, area, right, hint_y, &lines, fade);
        orb_bottom = caption.y;
        // The caption is how a waiting card is opened from the corner.
        if m.card.is_some() && caption.height > 0 {
            hits.add(caption, Target::ManagerCard);
        }
    }
    draw_end(look, buf, area, end_y, true, hits);
    let anchor = Anchor {
        x: right.saturating_sub(ow / 2),
        bottom: orb_bottom,
    };
    let orb_rect = orb::bounds_sized(area, anchor, ow, oh);
    draw_orb(app, buf, area, anchor, ow, oh);
    if m.orb.presence > 0.5 && orb_rect.width > 0 && orb_rect.height > 0 {
        hits.add(orb_rect, Target::ManagerOrb);
    }
}

// ---- pills --------------------------------------------------------------------------------

/// What the caption says right now and how faded it is, or nothing.
fn caption_lines(
    app: &App,
    look: &Look,
    now: Instant,
    target: Option<OrbState>,
) -> Option<(Vec<Line>, f32)> {
    let m = &app.ui.manager;
    if app.manager_reconnecting() {
        return Some((vec![plain("Reconnecting", look.st(MUTED))], 1.0));
    }
    let mut lines: Vec<Line> = Vec::new();
    let mut fade = 1.0;
    if let Some(caption) = &m.caption {
        let age = now.saturating_duration_since(caption.since);
        let quiet = matches!(target, None | Some(OrbState::Idle)) && m.field.is_none();
        if quiet && age > CAPTION_QUIET {
            fade = 1.0 - ((age - CAPTION_QUIET).as_secs_f32() / CAPTION_FADE.as_secs_f32());
        }
        if app.theme.opts.reduced_motion || app.theme.mono() {
            fade = 1.0;
        }
        if fade <= 0.0 {
            return None;
        }
        if let Some(user) = &caption.user {
            lines.push(plain(user, look.st(MUTED)));
        }
        if let Some(words) = &caption.manager {
            lines.push(plain(words, look.st(PRIMARY)));
            if let Some(error) = m
                .error
                .as_ref()
                .filter(|e| &e.message == words && !e.fix.is_empty())
            {
                lines.push(plain(&error.fix, look.st(MUTED)));
            }
        }
    }
    if lines.is_empty() {
        // A minimised call keeps a waiting card as its caption; the card itself
        // is out of the way until the user opens it.
        if m.minimised {
            if let Some(line) = m
                .card
                .as_ref()
                .and_then(|c| crate::manager::card_caption(&c.value))
            {
                lines.push(plain(&line, look.st(PRIMARY)));
            }
        }
    }
    if lines.is_empty() {
        if let Some(word) = app.orb_word(now) {
            lines.push(plain(word, look.st(MUTED)));
        }
    }
    (!lines.is_empty()).then_some((lines, fade.clamp(0.0, 1.0)))
}

/// Wrap the caption to at most two lines of `w` cells: the Manager's sentence
/// wins; the user's words drop out when both will not fit.
fn fit_caption(lines: &[Line], w: usize, ell: &str) -> Vec<Line> {
    let text_of = |line: &Line| -> (String, Style) {
        (
            line.iter().map(|s| s.content.as_ref()).collect(),
            line.first().map(|s| s.style).unwrap_or_default(),
        )
    };
    let wrap = |line: &Line| -> Vec<Line> {
        let (text, style) = text_of(line);
        wrap_spans(&[Span::styled(text, style)], w.max(1))
    };
    let clamp = |mut rows: Vec<Line>| -> Vec<Line> {
        if rows.len() > 2 {
            rows.truncate(2);
            if let Some(last) = rows.last_mut() {
                let (text, style) = text_of(last);
                *last = plain(
                    &format!("{}{ell}", truncate(&text, w.saturating_sub(1), "")),
                    style,
                );
            }
        }
        rows
    };
    match lines {
        [] => Vec::new(),
        [only] => clamp(wrap(only)),
        [user, rest @ ..] => {
            let rest_rows: Vec<Line> = rest.iter().flat_map(wrap).collect();
            if rest_rows.len() >= 2 {
                clamp(rest_rows)
            } else {
                let (text, style) = text_of(user);
                let mut out = vec![plain(&truncate(&text, w, ell), style)];
                out.extend(rest_rows);
                out
            }
        }
    }
}

/// Clear `r` to the raised surface and remember the colours that were
/// underneath. The text is drawn on a solid raised background; `finish` then
/// turns that into the raised surface blended over what was there.
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

/// Paint a pill of `lines` with its top at `top`, centred in `area`.
fn paint_pill(look: &Look, buf: &mut Buffer, r: Rect, lines: &[Line], fade: f32, centre: bool) {
    if r.height == 0 || r.width == 0 {
        return;
    }
    let under = fill(look, buf, r);
    for (i, line) in lines.iter().enumerate() {
        let w = line_width(line) as u16;
        let x = if centre {
            r.x + r.width.saturating_sub(w) / 2
        } else {
            r.x + 2
        };
        put_line(buf, x, r.y + i as u16, line, r.width.saturating_sub(2));
    }
    finish(look, buf, r, &under, PANEL_OPACITY * fade);
    if fade < 1.0 && look.blended() {
        for y in r.y..r.bottom() {
            for x in r.x..r.right() {
                let cell = &mut buf[(x, y)];
                cell.fg = blend(cell.bg, cell.fg, fade);
            }
        }
    }
}

/// A centred caption under the orb. Returns the row after it.
fn draw_caption_below(
    look: &Look,
    buf: &mut Buffer,
    area: Rect,
    top: u16,
    lines: &[Line],
    fade: f32,
) -> u16 {
    let limit = area.width.saturating_sub(8).clamp(10, 60) as usize;
    let lines = fit_caption(lines, limit, look.ell());
    let text_w = lines.iter().map(line_width).max().unwrap_or(0);
    let rows = lines.len().max(1) as u16;
    let w = (text_w as u16 + 4).clamp(14, area.width.saturating_sub(4).min(64));
    let x = (area.x + area.width / 2).saturating_sub(w / 2).max(area.x);
    let r = Rect::new(x, top, w, rows).intersection(area);
    if r.height == 0 {
        return top;
    }
    paint_pill(look, buf, r, &lines, fade, true);
    r.bottom()
}

/// A caption in the minimised corner, right-aligned and sitting directly above
/// `bottom`. Returns the (possibly unchanged) row the orb's bottom edge uses.
fn draw_caption_right(
    look: &Look,
    buf: &mut Buffer,
    area: Rect,
    right: u16,
    bottom: u16,
    lines: &[Line],
    fade: f32,
) -> Rect {
    let limit = (area.width / 3).clamp(16, 40) as usize;
    let lines = fit_caption(lines, limit, look.ell());
    let text_w = lines.iter().map(line_width).max().unwrap_or(0);
    let rows = lines.len().max(1) as u16;
    let w = (text_w as u16 + 4).clamp(14, area.width.saturating_sub(4).min(48));
    let x = right.saturating_sub(w).max(area.x);
    let y = bottom.saturating_sub(rows);
    let r = Rect::new(x, y, w, rows).intersection(area);
    if r.height == 0 {
        return Rect::new(area.x, bottom, 0, 0);
    }
    paint_pill(look, buf, r, &lines, fade, false);
    r
}

/// One quiet hint line, centred or right-aligned. It never moves a layout.
fn draw_hint(look: &Look, buf: &mut Buffer, area: Rect, y: u16, text: &str, right: bool) {
    if y >= area.bottom() || text.is_empty() {
        return;
    }
    let room = area.width.saturating_sub(4) as usize;
    let shown = truncate(text, room, look.ell());
    let w = width(&shown) as u16;
    let x = if right {
        area.right().saturating_sub(2 + w).max(area.x)
    } else {
        area.x + area.width.saturating_sub(w) / 2
    };
    put(buf, x, y, &shown, look.st(MUTED), room as u16);
}

/// The explicit End under the orb. Returns the row after it.
fn draw_end(
    look: &Look,
    buf: &mut Buffer,
    area: Rect,
    y: u16,
    right: bool,
    hits: &mut HitMap,
) -> u16 {
    if y >= area.bottom() {
        return y;
    }
    let w = 7u16.min(area.width);
    let x = if right {
        area.right().saturating_sub(2 + w)
    } else {
        area.x + area.width.saturating_sub(w) / 2
    };
    let r = Rect::new(x, y, w, 1).intersection(area);
    if r.width == 0 {
        return y;
    }
    let under = fill(look, buf, r);
    put(
        buf,
        r.x + 2,
        r.y,
        "End",
        look.bold(PRIMARY),
        w.saturating_sub(2),
    );
    finish(look, buf, r, &under, PANEL_OPACITY);
    hits.add(r, Target::ManagerEnd);
    r.bottom()
}

/// The typed field, directly under the focused orb. Returns its row.
fn draw_field_at(app: &App, look: &Look, buf: &mut Buffer, area: Rect, y: u16) -> u16 {
    let Some(field) = app.ui.manager.field.as_ref() else {
        return y;
    };
    let w = (area.width / 2).clamp(20, 60).min(area.width);
    let x = (area.x + area.width / 2).saturating_sub(w / 2).max(area.x);
    let r = Rect::new(x, y, w, 1).intersection(area);
    if r.height == 0 {
        return y;
    }
    let under = fill(look, buf, r);
    let text = field.text();
    // A prompt mark, so the pill reads as somewhere to type.
    let prompt = if look.t.opts.ascii { ">" } else { "\u{203a}" };
    put(buf, r.x + 1, r.y, prompt, look.st(MUTED), 1);
    let (left, inner) = (r.x + 3, r.width.saturating_sub(5) as usize);
    if text.is_empty() {
        put(buf, left, r.y, PLACEHOLDER, look.st(MUTED), inner as u16);
        app.ui.manager.cursor.set(Some((left, r.y)));
    } else {
        // Long lines show their tail, where the typing is.
        let shown = crate::text::truncate_start(&text.replace('\n', " "), inner, look.ell());
        put(buf, left, r.y, &shown, look.st(PRIMARY), inner as u16);
        let col = width(&shown).min(inner) as u16;
        app.ui.manager.cursor.set(Some((left + col, r.y)));
    }
    finish(look, buf, r, &under, PANEL_OPACITY);
    r.y
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
            .map_or("Sessions".to_owned(), str::to_owned),
        "sessions" => "Sessions".to_owned(),
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

/// Use the same smooth, alpha-blended sphere as NSH on graphics terminals.
fn draw_orb(app: &App, buf: &mut Buffer, area: Rect, anchor: Anchor, width: u16, height: u16) {
    let orb = &app.ui.manager.orb;
    let zone = orb::bounds_sized(area, anchor, width, height);
    let picture = app.orb_sprites.borrow().supported()
        && app.theme.opts.tier == crate::theme::Tier::TrueColor
        && !app.theme.opts.ascii;
    if !picture || app.ui.manager.context.is_some() {
        app.orb_sprites.borrow_mut().sync(None);
        orb::paint_sized(buf, area, anchor, width, height, orb, &app.theme);
        return;
    }
    if !orb.visible() || zone.width == 0 || zone.height == 0 {
        app.orb_sprites.borrow_mut().sync(None);
        return;
    }
    let rgb = |token| match app.theme.color(token) {
        Some(Color::Rgb(r, g, b)) => [r, g, b],
        _ => [180, 180, 180],
    };
    app.orb_sprites
        .borrow_mut()
        .sync(Some(&crate::orb_sprite::Request {
            state: orb.to,
            level: orb.level,
            t: orb.t,
            presence: orb.presence,
            color: orb::state_color(&app.theme, orb)
                .map(|c| c.map(|v| v.round().clamp(0.0, 255.0) as u8))
                .unwrap_or_else(|| rgb(orb.to.token())),
            white: rgb("text.primary"),
            light: app.theme.opts.mode == crate::theme::Mode::Light,
            col: zone.x,
            row: zone.y,
            width: zone.width,
            height: zone.height,
        }));
}

/// The current model excerpt and latest tool, without a log or chat surface.
fn draw_preview(app: &App, look: &Look, buf: &mut Buffer, area: Rect, mut y: u16) -> u16 {
    let Some(preview) = &app.ui.manager.preview else {
        return y;
    };
    let w = area.width.saturating_sub(4).min(76);
    let x = area.x + area.width.saturating_sub(w) / 2;
    let source = preview["source"].as_str().unwrap_or("Model");
    let clipped = if preview["clipped"].as_bool() == Some(true) {
        " · excerpt"
    } else {
        ""
    };
    let mut lines = vec![format!("Model context · {source}{clipped}")];
    if let Some(rows) = preview["lines"].as_array() {
        lines.extend(
            rows.iter()
                .take(3)
                .filter_map(|v| v.as_str().map(str::to_owned)),
        );
    }
    if let Some(tool) = preview.get("tool").filter(|v| v.is_object()) {
        lines.push(format!(
            "Tool · {} · {}",
            tool["name"].as_str().unwrap_or("tool"),
            tool["status"].as_str().unwrap_or("running")
        ));
        lines.push(format!(
            "{}{}",
            tool["arguments"].as_str().unwrap_or(""),
            if tool["clipped"].as_bool() == Some(true) {
                " …"
            } else {
                ""
            }
        ));
    }
    let available = area.bottom().saturating_sub(3).saturating_sub(y) as usize;
    if lines.len() > available && available > 0 {
        lines.truncate(available);
        *lines.last_mut().unwrap() = "… more in Manager context".to_owned();
    }
    for line in lines {
        if y >= area.bottom().saturating_sub(3) {
            break;
        }
        for col in x..x + w {
            buf[(col, y)].set_symbol(" ").set_style(look.st(MUTED));
        }
        put(
            buf,
            x,
            y,
            &truncate(&line, w as usize, "…"),
            look.st(MUTED),
            w,
        );
        y += 1;
    }
    y.saturating_add(1)
}

//! Dialog frames, toned panel text, the Settings area list, prompt and logs regions and
//! the completion popup (split out of `render.rs`).
use super::*;

/// Completion is anchored to the actual composer, including a growing editor.
pub fn completion_area(transcript: Rect, count: usize) -> Rect {
    let height = (count.min(10) as u16).min(transcript.height);
    // Same columns as the composer box (2 in from each side); the list also covers the
    // card's blank margin row, so it sits directly on the composer.
    let margin = if transcript.width > 4 { 2 } else { 0 };
    Rect::new(
        transcript.x + margin,
        (transcript.bottom() + 1).saturating_sub(height),
        transcript.width - 2 * margin,
        height,
    )
}
pub fn completion(
    frame: &mut Frame,
    s: &Snapshot,
    transcript: Rect,
    selected: usize,
    hover: &components::Hover,
) {
    let p = Palette::new(s.theme == "nexus-light");
    let area = completion_area(transcript, s.completions.len());
    let start = selected.saturating_sub(9);
    let name_width = s
        .completions
        .iter()
        .map(|v| v.width())
        .max()
        .unwrap_or(0)
        .min(24)
        + 3;
    let rows: Vec<Line> = s
        .completions
        .iter()
        .enumerate()
        .skip(start)
        .take(10)
        .map(|(i, value)| {
            let width = usize::from(area.width.saturating_sub(1));
            let help = s.command_help.get(value).map_or("", String::as_str);
            let text = crate::transcript::truncate(
                &format!("  {value:<name_width$}{help}"),
                width.saturating_sub(1),
            );
            let pad = width.saturating_sub(text.width());
            let selected = i == selected;
            let base = components::selectable(
                &p,
                components::State {
                    selected,
                    hover: hover.amount_id(
                        components::HoverId::CompletionRow(i),
                        std::time::Instant::now(),
                    ),
                    ..Default::default()
                },
            );
            let name = crate::transcript::truncate(&format!("  {value}"), name_width + 1);
            let rest: String = format!("{text}{}", " ".repeat(pad))
                .chars()
                .skip(name.chars().count())
                .collect();
            let muted = if selected { base } else { base.fg(p.quiet) };
            Line::from(vec![
                // The composer's rail, always gray.
                Span::styled("┃", Style::default().fg(p.quiet).bg(p.background)),
                Span::styled(name, base),
                Span::styled(rest, muted),
            ])
        })
        .collect();
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(rows).style(Style::default().bg(p.dialog).fg(p.text)),
        area,
    );
}
/// Where a panel is laid out. Framed modals (Settings, pickers, inspectors) centre over the
/// whole window (the union of every region), like Grok's modal; the bottom drawer and full
/// page layouts stay in the transcript.
pub fn panel_host(r: &super::Regions, s: &Snapshot) -> Rect {
    if matches!(s.panel_layout.as_str(), "drawer" | "page" | "") && s.nav.is_none() {
        return r.transcript;
    }
    let mut union = r.transcript;
    for rect in [r.composer, r.details, r.tabs, r.context] {
        if rect.width > 0 && rect.height > 0 {
            union = union.union(rect);
        }
    }
    union
}
/// A shared rectangle for painting and input; legacy snapshots retain full pages.
pub fn panel_area(transcript: Rect, s: &Snapshot) -> Rect {
    if s.panel_layout == "context" {
        // Card lists (tools, skills, MCP) read best at a page width, not the whole window.
        let width = transcript.width.saturating_sub(4).min(88);
        let height = transcript.height.saturating_sub(2);
        return Rect::new(
            transcript.x + (transcript.width - width) / 2,
            transcript.y + (transcript.height - height) / 2,
            width,
            height,
        );
    }
    if s.panel_layout == "detail" && transcript.width >= 44 {
        // A reading page that fits its text: at most 88 columns, as tall as the body (min 12 rows).
        let width = transcript.width.saturating_sub(4).clamp(44, 88);
        // Wrapped rows plus the spacing Markdown adds around headings, tables and code.
        let room = usize::from(width.saturating_sub(6)).max(1);
        let body: usize = s
            .panel_lines
            .iter()
            .map(|l| l.width().div_ceil(room).max(1) + usize::from(l.starts_with('#')))
            .sum::<usize>()
            .max(1)
            + 8;
        let body = body.min(usize::from(u16::MAX)) as u16;
        let height = body
            .max(12)
            .min(transcript.height.saturating_sub(2))
            .min(transcript.height * 3 / 4 + 2);
        return Rect::new(
            transcript.x + (transcript.width - width) / 2,
            transcript.y + (transcript.height - height) / 2,
            width,
            height,
        );
    }
    if s.panel_layout == "list" && transcript.width >= 44 {
        // Thin one-line-per-row list: widest row plus chrome, clamped, centred. The row counts
        // its inline detail, token column and `[ Restart ]` chip; past 72 columns the label clips.
        let widest = s
            .items
            .iter()
            .map(|i| {
                i.label.width()
                    + if i.detail.is_empty() {
                        0
                    } else {
                        i.detail.width() + 1
                    }
                    + if i.trailing.is_empty() {
                        0
                    } else {
                        i.trailing.width().max(7) + 1
                    }
                    + if i.action_label.is_empty() {
                        0
                    } else {
                        i.action_label.width() + 5
                    }
                    + 11
            })
            .max()
            .unwrap_or(0) as u16;
        let width = (widest + 6).clamp(44, 72).min(
            transcript
                .width
                .saturating_sub(4)
                .max(44.min(transcript.width)),
        );
        let groups = s
            .items
            .iter()
            .enumerate()
            .filter(|(n, i)| !i.group.is_empty() && (*n == 0 || s.items[n - 1].group != i.group))
            .count() as u16;
        let rows: u16 = s.items.iter().map(item_height).sum::<u16>() + groups;
        let height = (rows + 8).min(transcript.height.saturating_sub(2));
        return Rect::new(
            transcript.x + (transcript.width - width) / 2,
            transcript.y + (transcript.height - height) / 2,
            width,
            height,
        );
    }
    if s.nav.is_some() {
        // Grok settings modal: `min(120, area - 8)` wide, `area - 4` tall, centred.
        let width = transcript
            .width
            .saturating_sub(8)
            .min(120)
            .max(transcript.width.min(8));
        let height = transcript
            .height
            .saturating_sub(4)
            .max(transcript.height.min(8));
        return Rect::new(
            transcript.x + (transcript.width - width) / 2,
            transcript.y + (transcript.height - height) / 2,
            width,
            height,
        );
    }
    if s.panel_layout.is_empty() || s.panel_layout == "page" {
        return transcript;
    }
    let width = if s.panel_layout == "drawer" {
        transcript.width
    } else {
        transcript
            .width
            .saturating_sub(4)
            .min(90)
            .max(transcript.width.min(8))
    };
    let height = if s.panel_layout == "drawer" {
        (if s.items.is_empty() {
            s.panel_lines
                .iter()
                .map(|line| {
                    line.width()
                        .div_ceil(width.saturating_sub(4).max(1) as usize)
                        .max(1)
                })
                .sum::<usize>()
                .min(18) as u16
                + 2
        } else {
            s.items.len().min(8) as u16 + 4
        })
        .min(transcript.height)
    } else {
        let content = if !s.items.is_empty() {
            s.items.len().min(12)
        } else {
            s.panel_lines
                .iter()
                .map(|line| {
                    line.width()
                        .div_ceil(usize::from(width.saturating_sub(4)).max(1))
                        .max(1)
                })
                .sum::<usize>()
                .min(18)
        };
        (content as u16 + 8)
            .min(transcript.height.saturating_sub(4).min(26))
            .max(transcript.height.min(8))
    };
    Rect::new(
        transcript.x + (transcript.width - width) / 2,
        if s.panel_layout == "drawer" {
            transcript.bottom() - height
        } else {
            transcript.y + (transcript.height - height) / 2
        },
        width,
        height,
    )
}
/// Filtered item index under the pointer, using the same grouping and scroll as drawing.
pub fn item_height(item: &crate::bridge::Item) -> u16 {
    1 + u16::from(!item.detail.is_empty())
        + u16::from(!item.description.is_empty())
        + item.lines.len() as u16
}
pub fn settings_header_height(s: &Snapshot, width: u16, height: u16) -> u16 {
    let full: usize = s
        .panel_lines
        .iter()
        .map(|text| crate::transcript::wrap(&[Span::raw(text.clone())], width as usize).len())
        .sum();
    (full as u16).min(height.saturating_sub(5).min(8))
}
pub fn panel_item_at(
    s: &Snapshot,
    area: Rect,
    filter: &str,
    selection: usize,
    y: u16,
) -> Option<usize> {
    let inner = panel_inner(area, s.panel_layout == "drawer");
    let header = 2 + if s.nav.is_some() {
        usize::from(settings_header_height(s, inner.width, inner.height))
    } else {
        0
    };
    let room = usize::from(inner.height)
        .saturating_sub(header + if s.panel_hint.is_empty() { 0 } else { 2 });
    let mut body = Vec::new();
    let mut selected_line = 0;
    let mut group = "";
    for (i, item) in s
        .items
        .iter()
        .filter(|item| item.matches(filter))
        .enumerate()
    {
        if !item.group.is_empty() && item.group != group {
            body.push(None);
        }
        group = &item.group;
        if i == selection {
            selected_line = body.len();
        }
        for _ in 0..item_height(item) {
            body.push(Some(i));
        }
    }
    let start = (selected_line + 1).saturating_sub(room.max(1));
    let row = usize::from(y.saturating_sub(inner.y));
    if y < inner.y || row < header || row >= usize::from(inner.height) {
        return None;
    }
    body.get(start + row - header).copied().flatten()
}
/// True when a pointer is in the toggle affordance on an item row.
pub fn panel_toggle_at(
    s: &Snapshot,
    area: Rect,
    filter: &str,
    selection: usize,
    x: u16,
    y: u16,
) -> bool {
    let Some(index) = panel_item_at(s, area, filter, selection, y) else {
        return false;
    };
    let item = s
        .items
        .iter()
        .filter(|item| item.matches(filter))
        .nth(index);
    item.is_some_and(|item| {
        item.toggle_operation.is_some()
            && !item.toggle_locked
            && x >= area.right().saturating_sub(10)
    })
}
/// True when a pointer is on the row button (`[ Restart ]`), which sits left of the toggle.
pub fn panel_action_at(
    s: &Snapshot,
    area: Rect,
    filter: &str,
    selection: usize,
    x: u16,
    y: u16,
) -> bool {
    let Some(index) = panel_item_at(s, area, filter, selection, y) else {
        return false;
    };
    let Some(item) = s
        .items
        .iter()
        .filter(|item| item.matches(filter))
        .nth(index)
    else {
        return false;
    };
    if item.action_operation.is_none() || item.action_label.is_empty() {
        return false;
    }
    // Same geometry as the row painter: toggle flush right in the inner area, button before it.
    let inner = panel_inner(area, s.panel_layout == "drawer");
    let toggle = match (
        item.toggle_operation.is_some(),
        item.toggle_locked,
        item.toggle_enabled,
    ) {
        (false, _, _) => 0,
        (true, true, _) => 10,
        (true, false, Some(true)) => 6,
        (true, false, _) => 7,
    };
    let end = inner.right().saturating_sub(toggle);
    let start = end.saturating_sub((item.action_label.width() + 5) as u16);
    (start..end).contains(&x)
}
/// Share of the way every backdrop cell moves toward the page background under a modal.
pub const BACKDROP_BLEND: f32 = 0.5;

/// Port of Grok's `dim_area` (`xai-grok-pager-render` `render/color.rs`): clear the
/// modifiers of every cell in `area` and blend its foreground and background `factor` of
/// the way toward `blend_bg`. Cells whose colours cannot be blended (indexed, reset)
/// keep their colours and get `DIM`, so the backdrop still recedes.
pub fn dim_area(buf: &mut ratatui::buffer::Buffer, area: Rect, blend_bg: Color, factor: f32) {
    let area = area.intersection(buf.area);
    for y in area.y..area.bottom() {
        for x in area.x..area.right() {
            let Some(cell) = buf.cell_mut((x, y)) else {
                continue;
            };
            cell.modifier = Modifier::empty();
            let (fg, bg) = (cell.fg, cell.bg);
            let blendable = |c: Color| matches!(c, Color::Rgb(..)) || c == Color::Reset;
            if !matches!(blend_bg, Color::Rgb(..)) || !blendable(fg) && !blendable(bg) {
                cell.modifier = Modifier::DIM;
                continue;
            }
            if matches!(fg, Color::Rgb(..)) {
                cell.fg = nexus_widgets::theme::mix(fg, blend_bg, factor);
            } else if fg != Color::Reset {
                cell.modifier = Modifier::DIM;
            }
            if matches!(bg, Color::Rgb(..)) {
                cell.bg = nexus_widgets::theme::mix(bg, blend_bg, factor);
            } else if bg != Color::Reset {
                cell.modifier = Modifier::DIM;
            }
        }
    }
}

/// Dim the whole frame as a modal backdrop. Call it once, after the page is drawn and
/// immediately before the modal (Settings, pickers, inspectors, prompts, confirmations);
/// toasts and the completion popup are drawn afterwards and stay undimmed.
pub fn dim_backdrop(frame: &mut Frame, p: &Palette) {
    let area = frame.area();
    dim_area(frame.buffer_mut(), area, p.background, BACKDROP_BLEND);
}
/// The `[×]` close button on the top-right border of a framed modal (clickable, closes).
pub fn close_rect(area: Rect) -> Rect {
    if area.width < 12 {
        return Rect::default();
    }
    Rect::new(area.right().saturating_sub(5), area.y, 3, 1)
}
/// Grok modal frame (`views/modal_window.rs`): `border` outline, bold title in the top-left
/// border, quiet `[×]` top-right, `dialog` background. Every picker, panel and Settings page
/// goes through here. Returns the padded content area. A borderless drawer keeps the old
/// plain look (title row, no outline).
pub fn dialog_frame(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    p: &Palette,
    borderless: bool,
) -> Rect {
    frame.render_widget(Clear, area);
    if borderless {
        frame.render_widget(
            Block::default()
                .borders(Borders::NONE)
                .style(Style::default().bg(p.dialog).fg(p.text)),
            area,
        );
        let width = usize::from(area.width.saturating_sub(4));
        let title: String = title.chars().take(width).collect();
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                title,
                Style::default().fg(p.text).add_modifier(Modifier::BOLD),
            )))
            .style(Style::default().bg(p.dialog)),
            Rect {
                x: area.x + 2,
                y: area.y + 1,
                width: area.width.saturating_sub(4),
                height: 1.min(area.height),
            },
        );
        return panel_inner(area, borderless);
    }
    let border = Style::default().fg(p.border).bg(p.dialog);
    let room = usize::from(area.width.saturating_sub(10));
    let title: String = title.chars().take(room).collect();
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(ratatui::widgets::BorderType::Rounded)
        .border_style(border)
        .style(Style::default().bg(p.dialog).fg(p.text))
        .title(Line::from(vec![
            Span::styled(" ", border),
            Span::styled(
                title,
                Style::default()
                    .fg(p.text)
                    .bg(p.dialog)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled(" ", border),
        ]));
    frame.render_widget(block, area);
    let close = close_rect(area);
    if close.width > 0 {
        frame.render_widget(
            Paragraph::new("[×]").style(Style::default().fg(p.quiet).bg(p.dialog)),
            close,
        );
    }
    panel_inner(area, borderless)
}
/// Glyphs of a usage bar: used, remaining, and the even-pace marker.
const METER_FILLED: char = '━';
const METER_TRACK: char = '─';
const METER_PACE: char = '┃';

/// One usage-window row: `label  ━━━┃───  38% used · 12% in reserve · resets …`.
/// The label stays in the text colour; used cells take the tone (green, amber,
/// red), the track and summary are dim, the pace marker is plain text, and the
/// reserve phrase is green (spare) or amber (over pace).
fn meter_spans(body: &str, tone: &str, p: &Palette) -> Vec<Span<'static>> {
    let style = |color: Color| Style::default().fg(color);
    let fill = match tone {
        "ok" => p.success,
        "warn" => p.warning,
        "critical" => p.error,
        _ => p.quiet,
    };
    let is_bar = |c: char| matches!(c, METER_FILLED | METER_TRACK | METER_PACE);
    let Some(start) = body.find(is_bar) else {
        return vec![Span::styled(body.to_string(), style(p.muted))];
    };
    let end = body[start..]
        .find(|c: char| !is_bar(c))
        .map_or(body.len(), |i| start + i);
    let mut spans = vec![Span::styled(body[..start].to_string(), style(p.text))];
    let mut run = String::new();
    let mut run_color = None;
    for c in body[start..end].chars() {
        let color = match c {
            METER_FILLED => fill,
            METER_PACE => p.text,
            _ => p.border_strong,
        };
        if run_color.is_some_and(|current| current != color) {
            spans.push(Span::styled(
                std::mem::take(&mut run),
                style(run_color.unwrap()),
            ));
        }
        run.push(c);
        run_color = Some(color);
    }
    if let Some(color) = run_color {
        spans.push(Span::styled(run, style(color)));
    }
    for (i, part) in body[end..].split(" · ").enumerate() {
        let color = if part.ends_with("in reserve") {
            p.success
        } else if part.ends_with("over pace") {
            p.warning
        } else {
            p.muted
        };
        let text = if i == 0 {
            part.to_string()
        } else {
            format!(" · {part}")
        };
        spans.push(Span::styled(text, style(color)));
    }
    spans
}

/// Panel lines coloured by tone like the The terminal tool-details modal: bold section
/// titles, dim labels (`label: ` before a value), green/red/magenta diff lines.
/// Wrapped rows keep the line's leading indent plus two columns.
pub fn toned_lines(
    lines: &[String],
    tones: &[String],
    width: u16,
    p: &Palette,
) -> Vec<Line<'static>> {
    let mut out = Vec::new();
    for (text, tone) in lines.iter().zip(tones) {
        let indent = text.len() - text.trim_start().len();
        let body = &text[indent..];
        let style = |color: Color| Style::default().fg(color);
        let bold = style(p.text).add_modifier(Modifier::BOLD);
        let spans: Vec<Span<'static>> = match tone.as_str() {
            "title" | "header" => vec![Span::styled(body.to_string(), bold)],
            "label" => vec![Span::styled(body.to_string(), style(p.quiet))],
            "add" => vec![Span::styled(body.to_string(), style(p.success))],
            "del" => vec![Span::styled(body.to_string(), style(p.error))],
            "hunk" => vec![Span::styled(body.to_string(), style(p.purple))],
            // Usage windows (`ui_support/usage.py`): only the thin bar takes the tone.
            t if t.starts_with("meter-") => meter_spans(body, &t["meter-".len()..], p),
            "ok" => vec![Span::styled(body.to_string(), style(p.success))],
            "warn" => vec![Span::styled(body.to_string(), style(p.warning))],
            "critical" | "bad" => vec![Span::styled(body.to_string(), style(p.error))],
            "dim" | "unknown" => vec![Span::styled(body.to_string(), style(p.quiet))],
            "kv" => match body.split_once(": ") {
                Some((label, value)) => vec![
                    Span::styled(format!("{label}: "), style(p.quiet)),
                    Span::styled(value.to_string(), style(p.text)),
                ],
                None => vec![Span::styled(body.to_string(), style(p.text))],
            },
            _ => vec![Span::styled(body.to_string(), style(p.muted))],
        };
        let indent = indent.min(usize::from(width) / 2);
        let room = usize::from(width).saturating_sub(indent + 2).max(4);
        for (i, cells) in crate::transcript::wrap(&spans, room)
            .into_iter()
            .enumerate()
        {
            let pad = if i == 0 { indent } else { indent + 2 };
            out.push(crate::transcript::line(
                vec![Span::raw(" ".repeat(pad))],
                cells,
                None,
                Style::default(),
            ));
        }
    }
    out
}
/// The Settings area list inside the dialog (left 24 columns), when a page has one.
pub fn nav_rect(transcript: Rect) -> Rect {
    // The area list is gone: Settings is one root list (`settings_page::root`).
    Rect {
        width: 0,
        ..dialog_inner(transcript)
    }
}
/// Step to the next/previous selectable area (headings are skipped).
pub fn nav_step(nav: &crate::bridge::Nav, forward: bool) -> Option<usize> {
    let count = nav.items.len() as i64;
    let mut at = nav.selected;
    for _ in 0..count {
        at += if forward { 1 } else { -1 };
        if at < 0 || at >= count {
            return None;
        }
        if !nav.items[at as usize].2 {
            return Some(at as usize);
        }
    }
    None
}
pub fn panel_inner(area: Rect, borderless: bool) -> Rect {
    if !borderless {
        return dialog_inner(area);
    }
    Rect {
        x: area.x + 2.min(area.width),
        y: area.y + 2.min(area.height),
        width: area.width.saturating_sub(4),
        height: area.height.saturating_sub(2),
    }
}
pub fn dialog_inner(area: Rect) -> Rect {
    Rect {
        x: area.x + 2,
        y: area.y + 2.min(area.height),
        width: area.width.saturating_sub(4),
        height: area.height.saturating_sub(4),
    }
}
/// The bottom of the transcript reserved for a permission or question panel.
pub fn prompt_area(transcript: Rect, prompt: &crate::bridge::Prompt) -> Rect {
    let structured = prompt_rows(prompt);
    let content = if structured.is_empty() {
        prompt.lines.len().clamp(1, 6)
    } else {
        // A manager launch asks for more `what` rows so its instruction can be read in full.
        structured.len().clamp(1, if prompt.what_rows > 0 { 16 } else { 10 })
    } as u16;
    let extra = if prompt.kind == "question" { 1 } else { 0 };
    let height = (2 + content + prompt.choices.len() as u16 + extra + 1).min(transcript.height);
    Rect {
        x: transcript.x,
        y: transcript.y + transcript.height - height,
        width: transcript.width,
        height,
    }
}
/// One row of a structured permission prompt body (below the title row).
#[derive(Debug, PartialEq)]
pub enum PromptRow {
    /// `   waiting on you · mm:ss`
    Waiting,
    Blank,
    Heading(&'static str),
    Text(String),
    /// `… N more` when `what` is clipped.
    More(usize),
}

/// `what` rows shown before the rest clip to `… N more`.
const PROMPT_WHAT_ROWS: usize = 4;

/// The body of a structured permission prompt, empty for older hosts and questions (which keep
/// drawing `lines`). Section rows appear only when the request carries them.
pub fn prompt_rows(prompt: &crate::bridge::Prompt) -> Vec<PromptRow> {
    if prompt.kind != "permission" || prompt.title.is_empty() {
        return Vec::new();
    }
    let mut rows = vec![PromptRow::Waiting, PromptRow::Blank];
    let limit = if prompt.what_rows > 0 {
        prompt.what_rows
    } else {
        PROMPT_WHAT_ROWS
    };
    if !prompt.what.is_empty() {
        rows.push(PromptRow::Heading("What happens"));
        if prompt.what.len() > limit {
            rows.extend(prompt.what[..limit - 1].iter().cloned().map(PromptRow::Text));
            rows.push(PromptRow::More(prompt.what.len() - (limit - 1)));
        } else {
            rows.extend(prompt.what.iter().cloned().map(PromptRow::Text));
        }
    }
    if !prompt.why.is_empty() {
        rows.push(PromptRow::Heading("Why it asked"));
        // A manager confirmation has no rule behind it: its `why` is a sentence.
        rows.push(PromptRow::Text(if prompt.what_rows > 0 {
            prompt.why.clone()
        } else {
            format!("rule: {}", prompt.why)
        }));
    }
    rows.push(PromptRow::Blank);
    rows
}

/// `waiting on you · mm:ss`, counting up from `since` (unix seconds); no clock when unknown.
pub fn waiting_label(since: f64, now: f64) -> String {
    if since <= 0.0 {
        return "waiting on you".into();
    }
    format!(
        "waiting on you · {}",
        crate::picker::clock(crate::picker::waited(since, now))
    )
}

/// The title row: `◆ Allow git push origin main?`, glyph in the warning colour, text bold.
pub fn prompt_title_line(prompt: &crate::bridge::Prompt, p: &Palette) -> Line<'static> {
    Line::from(vec![
        Span::styled(
            format!("{} ", crate::transcript::look::glyphs().pending),
            Style::default().fg(p.warning),
        ),
        Span::styled(
            prompt.title.clone(),
            Style::default().fg(p.text).add_modifier(Modifier::BOLD),
        ),
    ])
}

/// The styled body rows of a structured prompt at wall-clock `now`.
pub fn prompt_body_lines(
    prompt: &crate::bridge::Prompt,
    p: &Palette,
    now: f64,
) -> Vec<Line<'static>> {
    let muted = Style::default().fg(p.muted);
    prompt_rows(prompt)
        .into_iter()
        .map(|row| match row {
            PromptRow::Waiting => {
                Line::styled(format!("   {}", waiting_label(prompt.since, now)), muted)
            }
            PromptRow::Blank => Line::raw(""),
            PromptRow::Heading(text) => Line::styled(text, muted.add_modifier(Modifier::BOLD)),
            PromptRow::Text(text) => Line::styled(format!("  {text}"), Style::default().fg(p.text)),
            PromptRow::More(n) => Line::styled(format!("  … {n} more"), muted),
        })
        .collect()
}

/// A choice row of a structured prompt: a neutral key chip (bold text on `element`, no colour
/// cue), then the label in `muted`; `▸` marks the keyboard selection.
pub fn prompt_choice_line(
    choice: &crate::bridge::Choice,
    selected: bool,
    p: &Palette,
) -> Vec<Span<'static>> {
    let label = Style::default().fg(if choice.disabled { p.quiet } else { p.muted });
    vec![
        Span::raw(if selected {
            format!(" {} ", crate::transcript::look::glyphs().closed)
        } else {
            "   ".into()
        }),
        Span::styled(
            format!(" {} ", choice.key),
            Style::default()
                .fg(p.text)
                .bg(p.element)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!(
                " {}{}",
                choice.label,
                if choice.disabled {
                    " (unavailable)"
                } else {
                    ""
                }
            ),
            label,
        ),
    ]
}

/// `(content, choices)` inside a prompt panel; the last choice row is the help line.
pub fn prompt_regions(area: Rect, count: usize) -> (Rect, Rect) {
    let inner = Rect {
        x: area.x + 2,
        y: area.y + 1,
        width: area.width.saturating_sub(4),
        height: area.height.saturating_sub(1),
    };
    let height = (count + 2).min(inner.height.saturating_sub(1) as usize) as u16;
    let areas = Layout::vertical([Constraint::Min(1), Constraint::Length(height)]).split(inner);
    (areas[0], areas[1])
}
pub fn logs_region(r: &Regions) -> Rect {
    r.details
}

#[cfg(test)]
mod tests {
    use super::*;

    fn permission(what: usize, choices: &[(&str, &str)]) -> crate::bridge::Prompt {
        serde_json::from_value(serde_json::json!({
            "kind": "permission", "id": "p1", "lines": ["Tool: Bash"],
            "title": "Allow Bash git push origin main?", "since": 1000.0,
            "what": (0..what).map(|i| format!("write: /w/f{i}.txt")).collect::<Vec<_>>(),
            "why": "Bash(git push:*)",
            "choices": choices.iter().map(|(key, label)| serde_json::json!(
                {"label": label, "value": label, "key": key, "disabled": false})).collect::<Vec<_>>(),
        }))
        .unwrap()
    }

    const FOUR: [(&str, &str); 4] = [
        ("y", "Allow once"),
        ("a", "Allow for session"),
        ("n", "Deny once"),
        ("d", "Deny for session"),
    ];

    #[test]
    fn permission_prompt_title_timer_and_sections() {
        let p = Palette::new(false);
        let prompt = permission(2, &FOUR);
        let title = prompt_title_line(&prompt, &p);
        assert_eq!(title.spans[0].content, "◆ ");
        assert_eq!(title.spans[0].style.fg, Some(p.warning));
        assert!(title.spans[1].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(title.spans[1].content, "Allow Bash git push origin main?");
        // The timer counts up from `since`; `now` is injected.
        assert_eq!(waiting_label(1000.0, 1065.0), "waiting on you · 01:05");
        assert_eq!(waiting_label(0.0, 1065.0), "waiting on you");
        let body = prompt_body_lines(&prompt, &p, 1065.0);
        let text: Vec<String> = body
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect();
        assert_eq!(
            text,
            [
                "   waiting on you · 01:05",
                "",
                "What happens",
                "  write: /w/f0.txt",
                "  write: /w/f1.txt",
                "Why it asked",
                "  rule: Bash(git push:*)",
                "",
            ]
        );
        assert_eq!(body[0].style.fg, Some(p.muted));
        assert!(body[2].style.add_modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn permission_prompt_clips_what_rows_and_grows_bounded() {
        let few = permission(1, &FOUR);
        let many = permission(9, &FOUR);
        assert!(prompt_rows(&many).contains(&PromptRow::More(6)));
        assert_eq!(
            prompt_rows(&many).len(),
            2 + 1 + 4 + 2 + 1,
            "What happens shows 3 rows + `… 6 more`"
        );
        let transcript = Rect::new(0, 0, 80, 40);
        let (small, big) = (
            prompt_area(transcript, &few),
            prompt_area(transcript, &many),
        );
        assert!(big.height > small.height && big.height <= 2 + 10 + 4 + 1);
        // A short transcript bounds it.
        assert!(prompt_area(Rect::new(0, 0, 80, 8), &many).height <= 8);
        // Older hosts (no `title`) and questions keep the plain `lines` layout.
        let mut plain = permission(1, &FOUR);
        plain.title.clear();
        assert!(prompt_rows(&plain).is_empty());
        let mut question = permission(1, &FOUR);
        question.kind = "question".into();
        assert!(prompt_rows(&question).is_empty());
    }

    #[test]
    fn manager_launch_confirmation_shows_the_instruction_and_never_offers_always() {
        let mut prompt: crate::bridge::Prompt = serde_json::from_value(serde_json::json!({
            "kind": "permission", "id": "p1", "lines": ["Launch a new session?"],
            "title": "Launch a new session?", "since": 1000.0,
            "what": ["Starts a root session with this instruction ([role] first if named):",
                     "fix the flaky sync test", "then run the suite", "and report back"],
            "why": "Asked every time · this answer is never remembered", "what_rows": 8,
            "choices": [
                {"label": "Launch this session", "value": "allow_once", "key": "y", "disabled": false},
                {"label": "Do not launch", "value": "deny_once", "key": "n", "disabled": false}],
        }))
        .unwrap();
        let rows = prompt_rows(&prompt);
        // Four what rows are all shown (the default limit would clip them to three + `… 1 more`).
        for line in ["fix the flaky sync test", "then run the suite", "and report back"] {
            assert!(rows.contains(&PromptRow::Text(line.into())), "{line}");
        }
        assert!(!rows.iter().any(|row| matches!(row, PromptRow::More(_))));
        // The reason is a sentence, not `rule: …`.
        assert!(rows.contains(&PromptRow::Text(
            "Asked every time · this answer is never remembered".into()
        )));
        let values: Vec<_> = prompt.choices.iter().map(|c| c.value.as_str()).collect();
        assert_eq!(values, ["allow_once", "deny_once"]);
        assert!(!prompt.choices.iter().any(|c| c.key == "a" || c.key == "d"));
        // A long instruction still clips with an announced `… N more`.
        prompt.what = (0..20).map(|i| format!("row {i}")).collect();
        assert!(prompt_rows(&prompt).contains(&PromptRow::More(20 - 7)));
        let area = prompt_area(Rect::new(0, 0, 100, 60), &prompt);
        assert!(area.height <= 2 + 16 + 2 + 1 + 1);
    }

    #[test]
    fn permission_key_chips_are_neutral_and_follow_the_choices() {
        let p = Palette::new(false);
        let prompt = permission(1, &FOUR);
        let loud = [p.success, p.error, p.blue, p.purple];
        for (i, choice) in prompt.choices.iter().enumerate() {
            let spans = prompt_choice_line(choice, i == 0, &p);
            for span in &spans {
                assert!(!loud
                    .iter()
                    .any(|c| Some(*c) == span.style.fg || Some(*c) == span.style.bg));
            }
            assert_eq!(spans[1].content, format!(" {} ", choice.key));
            assert_eq!(spans[1].style.fg, Some(p.text));
            assert_eq!(spans[1].style.bg, Some(p.element));
            assert!(spans[1].style.add_modifier.contains(Modifier::BOLD));
            assert_eq!(spans[2].style.fg, Some(p.muted));
        }
        assert_eq!(
            prompt_choice_line(&prompt.choices[0], true, &p)[0].content,
            " ▸ "
        );
        // No `a` choice from the host, no `a` chip: the lines come from `choices` alone.
        let once = permission(1, &[("y", "Allow once"), ("n", "Deny once")]);
        let keys: Vec<_> = once.choices.iter().map(|c| c.key.as_str()).collect();
        assert_eq!(keys, ["y", "n"]);
    }

    #[test]
    fn an_open_permission_prompt_keeps_the_frame_clock_running() {
        let mut s = Snapshot::default();
        assert!(!animating(&s));
        s.prompt = Some(permission(1, &FOUR));
        assert!(animating(&s), "the waiting timer needs redraws");
        s.prompt.as_mut().unwrap().title.clear();
        assert!(!animating(&s));
    }

    #[test]
    fn dim_area_blends_rgb_halfway_and_clears_modifiers() {
        let mut buf = ratatui::buffer::Buffer::empty(Rect::new(0, 0, 2, 1));
        {
            let cell = buf.cell_mut((0, 0)).unwrap();
            cell.set_fg(Color::Rgb(200, 100, 0));
            cell.set_bg(Color::Rgb(40, 40, 40));
            cell.modifier = Modifier::BOLD;
            let cell = buf.cell_mut((1, 0)).unwrap();
            cell.set_fg(Color::Indexed(4));
            cell.set_bg(Color::Indexed(0));
        }
        dim_area(&mut buf, Rect::new(0, 0, 2, 1), Color::Rgb(20, 20, 20), 0.5);
        let a = buf.cell((0, 0)).unwrap();
        assert_eq!(a.fg, Color::Rgb(110, 60, 10));
        assert_eq!(a.bg, Color::Rgb(30, 30, 30));
        assert_eq!(a.modifier, Modifier::empty());
        let b = buf.cell((1, 0)).unwrap();
        assert_eq!((b.fg, b.modifier), (Color::Indexed(4), Modifier::DIM));
    }
    #[test]
    fn overlays_are_bounded_and_drawer_reaches_composer() {
        for (width, height) in [(120, 40), (60, 20), (8, 6), (1, 1)] {
            let parent = Rect::new(5, 4, width, height);
            let mut s = Snapshot::default();
            s.panel_layout = "modal".into();
            let modal = panel_area(parent, &s);
            assert!(modal.x >= parent.x && modal.y >= parent.y);
            assert!(modal.right() <= parent.right() && modal.bottom() <= parent.bottom());
            if width > 8 && height > 8 {
                assert!(modal.width < width && modal.height < height);
            }
            s.panel_layout = "drawer".into();
            assert_eq!(panel_area(parent, &s).bottom(), parent.bottom());
            assert_eq!(completion_area(parent, 30).bottom(), parent.bottom() + 1);
            assert_eq!(
                completion_area(parent, 30).width,
                parent.width.saturating_sub(4 * u16::from(parent.width > 4))
            );
            assert_eq!(panel_area(parent, &s).width, parent.width);
        }
    }
    #[test]
    fn framed_modals_centre_over_the_whole_window_and_drawers_stay_in_the_transcript() {
        let regions = crate::render::Regions {
            transcript: Rect::new(0, 0, 60, 43),
            composer: Rect::new(0, 43, 60, 7),
            details: Rect::new(60, 0, 40, 50),
            ..Default::default()
        };
        let mut s = Snapshot::default();
        s.panel_layout = "drawer".into();
        assert_eq!(panel_host(&regions, &s), regions.transcript);
        s.panel_layout = "modal".into();
        assert_eq!(panel_host(&regions, &s), Rect::new(0, 0, 100, 50));
        s.nav = Some(crate::bridge::Nav::default());
        assert_eq!(panel_host(&regions, &s), Rect::new(0, 0, 100, 50));
    }

    #[test]
    fn settings_modal_is_large_and_inset_even_for_page_layout() {
        let parent = Rect::new(5, 4, 120, 40);
        let mut s = Snapshot::default();
        s.panel_layout = "page".into();
        s.nav = Some(crate::bridge::Nav::default());
        let area = panel_area(parent, &s);
        assert_eq!(
            area,
            Rect::new(9, 6, 112, 36),
            "min(120, w-8) by h-4, centred"
        );
        assert_eq!(nav_rect(area).width, 0, "no left area column any more");
    }
    #[test]
    fn list_layout_is_thin_clamped_and_falls_back_below_44_columns() {
        let parent = Rect::new(0, 0, 160, 50);
        let mut s = Snapshot::default();
        s.panel_layout = "list".into();
        s.items = vec![crate::bridge::Item {
            label: "bash".into(),
            trailing: "~1.4K".into(),
            ..Default::default()
        }];
        let area = panel_area(parent, &s);
        assert_eq!(area.width, 44);
        assert!(area.height <= 48);
        let narrow = Rect::new(0, 0, 40, 20);
        assert!(panel_area(narrow, &s).width <= 40);
    }
    #[test]
    fn detail_layout_is_bounded_and_centred() {
        let parent = Rect::new(0, 0, 160, 50);
        let mut s = Snapshot::default();
        s.panel_layout = "detail".into();
        s.panel_lines = vec!["x".into(); 200];
        let area = panel_area(parent, &s);
        assert_eq!(area.width, 88);
        assert!(area.height <= 39 && area.x == 36);
        s.panel_lines = vec!["x".into(); 3];
        assert_eq!(panel_area(parent, &s).height, 12);
        s.panel_lines = vec!["x".repeat(160)];
        assert_eq!(panel_area(parent, &s).height, 12, "wrapped rows count");
    }
    #[test]
    fn context_layout_is_an_88_column_centred_page() {
        let parent = Rect::new(4, 6, 160, 52);
        let mut s = Snapshot::default();
        s.panel_layout = "context".into();
        assert_eq!(panel_area(parent, &s), Rect::new(40, 7, 88, 50));
        let narrow = Rect::new(4, 6, 60, 52);
        assert_eq!(panel_area(narrow, &s), Rect::new(6, 7, 56, 50));
        let tiny = Rect::new(3, 2, 4, 2);
        assert_eq!(panel_area(tiny, &s), Rect::new(5, 3, 0, 0));
    }
    #[test]
    fn restart_button_sits_left_of_the_toggle() {
        let mut s = Snapshot::default();
        s.panel_title = "MCP".into();
        s.panel_layout = "context".into();
        s.items = vec![crate::bridge::Item {
            label: "demo".into(),
            toggle_operation: Some(serde_json::json!({"kind":"context_toggle"})),
            toggle_enabled: Some(true),
            action_label: "Restart".into(),
            action_operation: Some(serde_json::json!({"kind":"mcp_restart","name":"demo"})),
            ..Default::default()
        }];
        let area = Rect::new(0, 0, 60, 20);
        let inner = panel_inner(area, false);
        let row = (0..area.height)
            .find(|&y| panel_item_at(&s, area, "", 0, y) == Some(0))
            .expect("item row");
        let toggle_start = inner.right() - 6;
        assert!(panel_action_at(&s, area, "", 0, toggle_start - 1, row));
        assert!(panel_action_at(&s, area, "", 0, toggle_start - 12, row));
        assert!(!panel_action_at(&s, area, "", 0, toggle_start, row));
        assert!(!panel_action_at(&s, area, "", 0, toggle_start - 13, row));
    }
    #[test]
    fn model_picker_rows_are_single_line_and_reference_searchable() {
        use crate::bridge::Item;
        let item = Item {
            label: "Kimi K3 · opencode-go".into(),
            search: "opencode-go/kimi-k3".into(),
            info_operation: Some(serde_json::json!({"kind":"model_details"})),
            group: "Favorites".into(),
            ..Default::default()
        };
        assert_eq!(item_height(&item), 1);
        let mut s = Snapshot::default();
        s.items = vec![item];
        let area = Rect::new(0, 0, 60, 20);
        let y = dialog_inner(area).y;
        assert_eq!(panel_item_at(&s, area, "kimi-k3", 0, y + 3), Some(0));
        assert_eq!(panel_item_at(&s, area, "kimi-k3", 0, y + 4), None);
    }
    #[test]
    fn grouped_picker_mouse_rows_match_display_selection() {
        use crate::bridge::Item;
        let mut s = Snapshot::default();
        s.items = vec![
            Item {
                label: "one".into(),
                group: "First".into(),
                ..Default::default()
            },
            Item {
                label: "two".into(),
                group: "Second".into(),
                ..Default::default()
            },
        ];
        let area = Rect::new(10, 5, 60, 20);
        let y = dialog_inner(area).y;
        assert_eq!(panel_item_at(&s, area, "", 0, y + 2), None);
        assert_eq!(panel_item_at(&s, area, "", 0, y + 3), Some(0));
        assert_eq!(panel_item_at(&s, area, "", 0, y + 5), Some(1));
        assert_eq!(panel_item_at(&s, area, "two", 0, y + 3), Some(0));
    }
}

/// Filtered menu rows share the component states used by other interactive controls.
pub fn draw_menu(
    frame: &mut Frame,
    s: &Snapshot,
    inner: Rect,
    filter: &str,
    selection: usize,
    p: &Palette,
    hover: &super::components::Hover,
) {
    let needle = filter.to_lowercase();
    let rows: Vec<_> = s
        .items
        .iter()
        .filter(|item| item.matches(&needle))
        .collect();
    // Settings pages: the scope path and the area's help sit above the list.
    let mut lines: Vec<Line<'static>> = Vec::new();
    if s.nav.is_some() {
        let note_lines: Vec<_> = s
            .panel_lines
            .iter()
            .flat_map(|text| {
                crate::transcript::wrap(&[Span::raw(text.clone())], inner.width as usize)
            })
            .collect();
        let height = dialogs::settings_header_height(s, inner.width, inner.height) as usize;
        for cells in note_lines.iter().take(height) {
            lines.push(crate::transcript::line(
                vec![],
                cells.clone(),
                None,
                Style::default().fg(p.quiet).bg(p.dialog),
            ));
        }
        if note_lines.len() > height && height > 0 {
            lines[height - 1] = Line::styled(
                "More notes — Tab opens full details",
                Style::default().fg(p.accent),
            );
        }
    }
    lines.push(Line::from(if filter.is_empty() {
        vec![
            Span::styled("Search", Style::default().fg(p.quiet)),
            Span::styled("   Tab inspect details", Style::default().fg(p.quiet)),
        ]
    } else {
        vec![
            Span::styled(format!("{filter}█"), Style::default().fg(p.text)),
            Span::styled("   Tab inspect details", Style::default().fg(p.quiet)),
        ]
    }));
    lines.push(Line::default());
    // Group headings sit above the first item of each group; selection counts items only.
    let mut body: Vec<Line<'static>> = Vec::new();
    let mut selected_line = 0usize;
    let mut group = "";
    for (i, item) in rows.iter().enumerate() {
        if !item.group.is_empty() && item.group != group {
            body.push(Line::styled(
                item.group.clone(),
                Style::default()
                    .fg(p.purple)
                    .bg(p.dialog)
                    .add_modifier(Modifier::BOLD),
            ));
        }
        group = &item.group;
        let on = i == selection;
        // Selected: solid accent bar with dark text. Active choice: `●` + accent name.
        let amount = hover
            .amount(&format!("item:{i}"), std::time::Instant::now())
            .max(hover.amount(&format!("toggle:{i}"), std::time::Instant::now()));
        let base = super::components::selectable(
            p,
            super::components::State {
                selected: on,
                hover: amount,
                ..Default::default()
            },
        );
        let (name_style, detail_style, fill) = if on {
            (base, base.fg(p.muted).remove_modifier(Modifier::BOLD), base)
        } else {
            (
                base.fg(if item.current { p.accent } else { p.text }),
                base.fg(p.quiet),
                base,
            )
        };
        let toggle = item.toggle_operation.as_ref().map(|_| {
            if item.toggle_locked {
                "[ LOCKED ]".to_string()
            } else if item.toggle_enabled.unwrap_or(false) {
                "[ ON ]".to_string()
            } else {
                "[ OFF ]".to_string()
            }
        });
        let marker = if item.current { "●" } else { " " };
        let width = usize::from(inner.width);
        let tail = (!item.trailing.is_empty()).then(|| format!("{:>7} ", item.trailing));
        let button = (item.action_operation.is_some() && !item.action_label.is_empty())
            .then(|| format!("[ {} ] ", item.action_label));
        let available = width.saturating_sub(
            3 + toggle.as_ref().map_or(0, |t| t.width() + 1)
                + tail.as_ref().map_or(0, |t| t.width())
                + button.as_ref().map_or(0, |t| t.width()),
        );
        let structured = if item.name.is_empty() {
            item.label.clone()
        } else {
            format!(
                "{}{}  {}  {} [{}]{}",
                item.name,
                if item.changed { " *" } else { "" },
                item.value,
                item.status,
                item.scope,
                if item.move_up.is_some() || item.move_down.is_some() || item.remove.is_some() {
                    "  ↑ ↓ ×"
                } else {
                    ""
                }
            )
        };
        let label = truncate_width(&structured, available);
        let mut spans = vec![
            Span::styled(
                format!(" {marker} "),
                if on {
                    name_style
                } else {
                    name_style.fg(p.accent)
                },
            ),
            Span::styled(label.clone(), name_style),
        ];
        let mut used = 3 + label.width();
        if !item.detail.is_empty() {
            let detail = truncate_width(&item.detail, available.saturating_sub(label.width() + 1));
            if !detail.is_empty() {
                used += 1 + detail.width();
                spans.push(Span::styled(format!(" {detail}"), detail_style));
            }
        }
        if let Some(tail) = tail {
            // The token column sits left of the `[ Restart ]` chip when there is one.
            let gap = width.saturating_sub(
                used + tail.width()
                    + button.as_ref().map_or(0, |b| b.width())
                    + toggle.as_ref().map_or(0, |t| t.width()),
            );
            spans.push(Span::styled(" ".repeat(gap), fill));
            used += gap + tail.width();
            spans.push(Span::styled(tail, detail_style));
        }
        if let Some(button) = button {
            let gap = width
                .saturating_sub(used + button.width() + toggle.as_ref().map_or(0, |t| t.width()));
            spans.push(Span::styled(" ".repeat(gap), fill));
            used += gap + button.width();
            spans.push(Span::styled(
                button,
                super::components::toggle(
                    p,
                    super::components::State {
                        hover: hover.amount(&format!("action:{i}"), std::time::Instant::now()),
                        ..Default::default()
                    },
                ),
            ));
        }
        if let Some(toggle) = toggle {
            let gap = width.saturating_sub(used + toggle.width());
            spans.push(Span::styled(" ".repeat(gap), fill));
            used += gap + toggle.width();
            spans.push(Span::styled(
                toggle,
                super::components::toggle(
                    p,
                    super::components::State {
                        selected: item.toggle_enabled.unwrap_or(false),
                        disabled: item.toggle_locked,
                        hover: hover.amount(&format!("toggle:{i}"), std::time::Instant::now()),
                        ..Default::default()
                    },
                ),
            ));
        }
        spans.push(Span::styled(" ".repeat(width.saturating_sub(used)), fill));
        if on {
            selected_line = body.len();
        }
        body.push(Line::from(spans));
        if !item.description.is_empty() {
            body.push(Line::styled(
                truncate_width(&format!("   {}", item.description), width),
                base.fg(if on { p.background } else { p.quiet }),
            ));
        }
        for text in &item.lines {
            body.push(Line::styled(
                truncate_width(&format!("   {text}"), width),
                base.fg(if on { p.background } else { p.quiet }),
            ));
        }
        if !item.detail.is_empty() {
            body.push(Line::styled(
                truncate_width(&format!("   {}", item.detail), width),
                base.fg(if on { p.background } else { p.quiet }),
            ));
        }
    }
    let reserved = if s.panel_hint.is_empty() { 0 } else { 2 };
    let room = inner.height.saturating_sub(lines.len() as u16 + reserved) as usize;
    let start = (selected_line + 1).saturating_sub(room.max(1));
    lines.extend(body.into_iter().skip(start).take(room));
    frame.render_widget(
        Paragraph::new(lines).style(Style::default().bg(p.dialog)),
        inner,
    );
    if !s.panel_hint.is_empty() && inner.height > 0 {
        // Key hints: the key text is dim, its action label bright.
        let spans: Vec<Span<'static>> = s
            .panel_hint
            .split("  ")
            .flat_map(|part| {
                let (label, key) = part
                    .rsplit_once(" ctrl+")
                    .map_or((part, String::new()), |(l, k)| (l, format!(" ctrl+{k}")));
                [
                    Span::styled(
                        label.to_string(),
                        Style::default().fg(p.text).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled(format!("{key}  "), Style::default().fg(p.quiet)),
                ]
            })
            .collect();
        frame.render_widget(
            Paragraph::new(Line::from(spans)).style(Style::default().bg(p.dialog)),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
    }
}

/// Shared click/hover geometry excludes the completion help line and clipped rows.
pub fn completion_at(parent: Rect, count: usize, selected: usize, x: u16, y: u16) -> Option<usize> {
    let area = completion_area(parent, count);
    if !area.contains((x, y).into()) {
        return None;
    }
    let index = selected.saturating_sub(9) + usize::from(y - area.y);
    (index < count).then_some(index)
}
pub fn nav_at(s: &Snapshot, area: Rect, x: u16, y: u16) -> Option<usize> {
    let list = nav_rect(area);
    if !list.contains((x, y).into()) {
        return None;
    }
    let i = usize::from(y - list.y);
    s.nav
        .as_ref()?
        .items
        .get(i)
        .filter(|item| !item.2)
        .map(|_| i)
}
pub fn prompt_choice_at(
    parent: Rect,
    prompt: &crate::bridge::Prompt,
    x: u16,
    y: u16,
) -> Option<usize> {
    let (_, choices) = prompt_regions(prompt_area(parent, prompt), prompt.choices.len());
    if !choices.contains((x, y).into()) {
        return None;
    }
    let i = usize::from(y - choices.y);
    prompt.choices.get(i).filter(|c| !c.disabled).map(|_| i)
}

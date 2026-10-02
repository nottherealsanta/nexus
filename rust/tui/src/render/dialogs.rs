//! Dialog frames, toned panel text, the Settings area list, prompt and logs regions and
//! the completion popup (split out of `render.rs`).
use super::*;

pub fn completion(frame: &mut Frame, values: &[String], selected: usize) {
    let height = (values.len().min(8) + 2) as u16;
    let area = Rect {
        x: 2,
        y: frame.area().height.saturating_sub(8 + height),
        width: frame.area().width.saturating_sub(4).min(70),
        height,
    };
    let start = selected.saturating_sub(7);
    let lines: Vec<Line> = values
        .iter()
        .enumerate()
        .skip(start)
        .take(8)
        .map(|(i, value)| {
            Line::styled(
                format!("{} {}", if i == selected { "▸" } else { " " }, value),
                if i == selected {
                    Style::default().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                },
            )
        })
        .collect();
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(lines).block(
            Block::default()
                .title("Completion · ↑↓ choose · Enter insert")
                .borders(Borders::ALL),
        ),
        area,
    );
}
/// Dialog background with an accent title and a rule (Textual modal look);
/// returns the padded content area.
pub fn dialog_frame(frame: &mut Frame, area: Rect, title: &str, p: &Palette) -> Rect {
    frame.render_widget(Clear, area);
    frame.render_widget(Block::default().style(Style::default().bg(p.dialog).fg(p.text)), area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(title.to_string(), Style::default().fg(p.accent).add_modifier(Modifier::BOLD)),
            Line::styled("─".repeat(usize::from(area.width.saturating_sub(4))), Style::default().fg(p.border)),
        ])
        .style(Style::default().bg(p.dialog)),
        Rect { x: area.x + 2, y: area.y + 1, width: area.width.saturating_sub(4), height: 2.min(area.height) },
    );
    dialog_inner(area)
}
/// Panel lines coloured by tone like the Textual tool-details modal: bold section
/// titles, dim labels (`label: ` before a value), green/red/magenta diff lines.
/// Wrapped rows keep the line's leading indent plus two columns.
pub fn toned_lines(lines: &[String], tones: &[String], width: u16, p: &Palette) -> Vec<Line<'static>> {
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
            // Usage windows: the bar's tone colours the whole row.
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
        for (i, cells) in crate::transcript::wrap(&spans, room).into_iter().enumerate() {
            let pad = if i == 0 { indent } else { indent + 2 };
            out.push(crate::transcript::line(vec![Span::raw(" ".repeat(pad))], cells, None, Style::default()));
        }
    }
    out
}
/// The Settings area list inside the dialog (left 24 columns), when a page has one.
pub fn nav_rect(transcript: Rect) -> Rect {
    let inner = dialog_inner(transcript);
    Rect { width: 24.min(inner.width), ..inner }
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
pub fn dialog_inner(area: Rect) -> Rect {
    Rect {
        x: area.x + 2,
        y: area.y + 4.min(area.height),
        width: area.width.saturating_sub(4),
        height: area.height.saturating_sub(5),
    }
}
/// The bottom of the transcript reserved for a permission or question panel.
pub fn prompt_area(transcript: Rect, prompt: &crate::bridge::Prompt) -> Rect {
    let content = prompt.lines.len().clamp(1, 6) as u16;
    let extra = if prompt.kind == "question" { 1 } else { 0 };
    let height = (2 + content + prompt.choices.len() as u16 + extra + 1).min(transcript.height);
    Rect {
        x: transcript.x,
        y: transcript.y + transcript.height - height,
        width: transcript.width,
        height,
    }
}
/// `(content, choices)` inside a prompt panel; the last choice row is the help line.
pub fn prompt_regions(area: Rect, count: usize) -> (Rect, Rect) {
    let inner = Rect { x: area.x + 2, y: area.y + 1, width: area.width.saturating_sub(4), height: area.height.saturating_sub(1) };
    let height = (count + 2).min(inner.height.saturating_sub(1) as usize) as u16;
    let areas = Layout::vertical([Constraint::Min(1), Constraint::Length(height)]).split(inner);
    (areas[0], areas[1])
}
pub fn logs_region(r: &Regions) -> Rect {
    if r.logs.width > 0 {
        return r.logs;
    }
    let transcript = r.transcript;
    Rect::new(
        transcript.x,
        transcript.y + transcript.height / 2,
        transcript.width,
        transcript.height - transcript.height / 2,
    )
}

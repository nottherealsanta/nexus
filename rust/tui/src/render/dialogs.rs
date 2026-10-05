//! Dialog frames, toned panel text, the Settings area list, prompt and logs regions and
//! the completion popup (split out of `render.rs`).
use super::*;

/// Completion is anchored to the actual composer, including a growing editor.
pub fn completion_area(transcript: Rect, count: usize) -> Rect {
    let height = ((count.min(8) + 1) as u16).min(transcript.height);
    Rect::new(
        transcript.x,
        transcript.bottom().saturating_sub(height),
        transcript.width,
        height,
    )
}
pub fn completion(frame: &mut Frame, s: &Snapshot, transcript: Rect, selected: usize) {
    let p = Palette::new(s.theme == "nexus-light");
    let area = completion_area(transcript, s.completions.len());
    let start = selected.saturating_sub(7);
    let lines: Vec<Line> = s
        .completions
        .iter()
        .enumerate()
        .skip(start)
        .take(8)
        .map(|(i, value)| {
            let text = crate::transcript::truncate(
                &format!("  {} {}", if i == selected { "▸" } else { " " }, value),
                usize::from(area.width.saturating_sub(2)),
            );
            let pad = usize::from(area.width.saturating_sub(2)).saturating_sub(text.width());
            Line::styled(
                format!("{text}{}", " ".repeat(pad)),
                Style::default()
                    .fg(if i == selected { p.text } else { p.muted })
                    .bg(if i == selected {
                        p.element_hi
                    } else {
                        p.dialog
                    }),
            )
        })
        .collect();
    frame.render_widget(Clear, area);
    let mut rows = vec![Line::styled(
        "  ↑↓ choose · Enter select · Esc close",
        Style::default().fg(p.quiet),
    )];
    rows.extend(lines);
    frame.render_widget(
        Paragraph::new(rows).style(Style::default().bg(p.dialog).fg(p.text)),
        area,
    );
}
/// A shared rectangle for painting and input; legacy snapshots retain full pages.
pub fn panel_area(transcript: Rect, s: &Snapshot) -> Rect {
    if s.panel_layout == "context" {
        let width = transcript.width.saturating_sub(4);
        let height = transcript.height.saturating_sub(2);
        return Rect::new(
            transcript.x + (transcript.width - width) / 2,
            transcript.y + (transcript.height - height) / 2,
            width,
            height,
        );
    }
    if s.nav.is_some() {
        let width = transcript
            .width
            .saturating_sub(4)
            .max(transcript.width.min(8));
        let height = transcript
            .height
            .saturating_sub(2)
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
    1 + u16::from(!item.detail.is_empty()) + u16::from(!item.description.is_empty())
}
pub fn settings_header_height(s: &Snapshot, width: u16, height: u16) -> u16 {
    let full: usize = s.panel_lines.iter().map(|text| crate::transcript::wrap(&[Span::raw(text.clone())], width as usize).len()).sum();
    (full as u16).min(height.saturating_sub(5).min(8))
}
pub fn panel_item_at(
    s: &Snapshot,
    area: Rect,
    filter: &str,
    selection: usize,
    y: u16,
) -> Option<usize> {
    let mut inner = panel_inner(area, s.panel_layout == "drawer");
    if s.nav.is_some() {
        let taken = nav_rect(area).width + 2;
        inner.x += taken;
        inner.width = inner.width.saturating_sub(taken);
    }
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
        for _ in 0..item_height(item) { body.push(Some(i)); }
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
/// Dialog background with an accent title and a rule (The terminal modal look);
/// returns the padded content area.
pub fn dialog_frame(
    frame: &mut Frame,
    area: Rect,
    title: &str,
    p: &Palette,
    borderless: bool,
) -> Rect {
    frame.render_widget(Clear, area);
    frame.render_widget(
        Block::default()
            .borders(Borders::NONE)
            .style(Style::default().bg(p.dialog).fg(p.text)),
        area,
    );
    let width = usize::from(area.width.saturating_sub(4));
    let esc = if borderless { "" } else { "esc" };
    let title: String = title
        .chars()
        .take(width.saturating_sub(esc.len() + 1))
        .collect();
    let gap = width.saturating_sub(title.chars().count() + esc.len());
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                title,
                Style::default().fg(p.text).add_modifier(Modifier::BOLD),
            ),
            Span::raw(" ".repeat(gap)),
            Span::styled(esc, Style::default().fg(p.quiet)),
        ]))
        .style(Style::default().bg(p.dialog)),
        Rect {
            x: area.x + 2,
            y: area.y + 1,
            width: area.width.saturating_sub(4),
            height: 1.min(area.height),
        },
    );
    panel_inner(area, borderless)
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
    let inner = dialog_inner(transcript);
    Rect {
        width: 24.min(inner.width),
        ..inner
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
            assert_eq!(completion_area(parent, 30).bottom(), parent.bottom());
            assert_eq!(completion_area(parent, 30).width, parent.width);
            assert_eq!(panel_area(parent, &s).width, parent.width);
        }
    }
    #[test]
    fn settings_modal_is_large_and_inset_even_for_page_layout() {
        let parent = Rect::new(5, 4, 120, 40);
        let mut s = Snapshot::default();
        s.panel_layout = "page".into();
        s.nav = Some(crate::bridge::Nav::default());
        let area = panel_area(parent, &s);
        assert_eq!(area, Rect::new(7, 5, 116, 38));
        assert!(nav_rect(area).right() < area.right());
    }
    #[test]
    fn context_layout_is_exact_transcript_inset_without_modal_caps() {
        let parent = Rect::new(4, 6, 160, 52);
        let mut s = Snapshot::default();
        s.panel_layout = "context".into();
        assert_eq!(panel_area(parent, &s), Rect::new(6, 7, 156, 50));
        let tiny = Rect::new(3, 2, 4, 2);
        assert_eq!(panel_area(tiny, &s), Rect::new(5, 3, 0, 0));
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

//! Native shell layout and bounded-width cell cache (feasibility §6–7).
use crate::{bridge::Snapshot, editor::Editor};
use ratatui::{
    layout::{Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
    Frame,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
/// Nexus dark/light tokens, mirroring `nexus/ui/tui/theme.py`.
pub struct Palette {
    pub background: Color,
    pub text: Color,
    pub muted: Color,
    pub quiet: Color,
    pub accent: Color,
    pub panel: Color,
    pub element: Color,
    pub element_hi: Color,
    pub dialog: Color,
    pub border: Color,
    pub border_strong: Color,
    pub blue: Color,
    pub purple: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
    pub cyan: Color,
}
impl Palette {
    pub fn new(light: bool) -> Self {
        let rgb = |r, g, b| Color::Rgb(r, g, b);
        if light {
            Self {
                background: rgb(255, 255, 255),
                text: rgb(27, 27, 27),
                muted: rgb(85, 85, 85),
                quiet: rgb(138, 138, 138),
                accent: rgb(200, 103, 47),
                panel: rgb(245, 245, 244),
                element: rgb(236, 236, 234),
                element_hi: rgb(226, 226, 223),
                dialog: rgb(255, 255, 255),
                border: rgb(220, 220, 216),
                border_strong: rgb(185, 185, 180),
                blue: rgb(47, 111, 214),
                purple: rgb(122, 82, 199),
                success: rgb(38, 128, 68),
                warning: rgb(168, 98, 0),
                error: rgb(194, 58, 74),
                cyan: rgb(14, 116, 144),
            }
        } else {
            Self {
                background: rgb(10, 10, 10),
                text: rgb(238, 238, 238),
                muted: rgb(163, 163, 163),
                quiet: rgb(111, 111, 111),
                accent: rgb(250, 178, 131),
                panel: rgb(20, 20, 20),
                element: rgb(30, 30, 30),
                element_hi: rgb(40, 40, 40),
                dialog: rgb(13, 13, 13),
                border: rgb(44, 44, 44),
                border_strong: rgb(72, 72, 72),
                blue: rgb(92, 156, 245),
                purple: rgb(157, 124, 216),
                success: rgb(127, 216, 143),
                warning: rgb(245, 167, 66),
                error: rgb(224, 108, 117),
                cyan: rgb(86, 212, 221),
            }
        }
    }
}
#[derive(Default, Clone)]
struct Part {
    block: crate::bridge::Content,
    width: u16,
    lines: Vec<Line<'static>>,
    operations: Vec<Option<serde_json::Value>>,
}
#[derive(Default)]
pub struct Cache {
    parts: std::collections::HashMap<String, Part>,
    blocks: Vec<crate::bridge::Content>,
    pub operations: Vec<Option<serde_json::Value>>,
    source: Vec<String>,
    width: u16,
    pub lines: Vec<Line<'static>>,
    /// Animation frame for the running-tool slot (`SPINNER_SLOT`).
    pub spin: usize,
    /// The draft is non-empty: empty-session hints keep their rows but go blank.
    pub typing: bool,
    built_typing: bool,
}
pub const SPINNER_SLOT: char = '\u{e000}';
const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
/// Whether any block has a running-tool slot, i.e. the screen needs ~8 Hz redraws.
pub fn animating(s: &Snapshot) -> bool {
    s.blocks.iter().any(|block| block.text.contains(SPINNER_SLOT))
}
fn with_spinner(line: &Line<'static>, frame: usize) -> Line<'static> {
    if !line.spans.iter().any(|span| span.content.contains(SPINNER_SLOT)) {
        return line.clone();
    }
    let glyph = SPINNER[frame % SPINNER.len()];
    let mut line = line.clone();
    for span in &mut line.spans {
        if span.content.contains(SPINNER_SLOT) {
            span.content = span.content.replace(SPINNER_SLOT, glyph).into();
        }
    }
    line
}
impl Cache {
    pub fn update(&mut self, source: &[String], width: u16, palette: &Palette) {
        if self.source == source && self.width == width {
            return;
        }
        self.source = source.to_vec();
        self.width = width;
        self.lines.clear();
        for block in source {
            for line in block.lines() {
                let style = if line.starts_with('+') {
                    Style::default().fg(Color::Green)
                } else if line.starts_with('-') {
                    Style::default().fg(Color::Red)
                } else if line.starts_with("Turn ")
                    || line.starts_with("assistant")
                    || line.starts_with('#')
                {
                    Style::default()
                        .fg(palette.accent)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(palette.text)
                };
                let mut current = String::new();
                let mut columns = 0;
                for g in line.graphemes(true) {
                    let w = g.width();
                    if columns + w > usize::from(width.max(1)) && !current.is_empty() {
                        self.lines
                            .push(Line::styled(std::mem::take(&mut current), style));
                        columns = 0;
                    }
                    current.push_str(g);
                    columns += w;
                }
                self.lines.push(Line::styled(current, style));
            }
        }
    }
    pub fn update_content(
        &mut self,
        context: &[String],
        blocks: &[crate::bridge::Content],
        width: u16,
        palette: &Palette,
    ) {
        if self.blocks == blocks && self.source == context && self.width == width && self.typing == self.built_typing {
            return;
        }
        if self.typing != self.built_typing {
            self.parts.clear();
        }
        self.built_typing = self.typing;
        self.lines.clear();
        self.operations.clear();
        for row in context {
            for line in row.lines() {
                for cells in crate::transcript::wrap(&[Span::raw(line.to_string())], usize::from(width)) {
                    self.lines.push(crate::transcript::line(vec![], cells, None, Style::default().fg(palette.muted)));
                    self.operations.push(None);
                }
            }
        }
        let mut old = std::mem::take(&mut self.parts);
        for (i, block) in blocks.iter().enumerate() {
            let key = format!("{}:{}", block.id, i);
            let part = if let Some(part) = old
                .remove(&key)
                .filter(|part| part.block == *block && part.width == width)
            {
                part
            } else {
                let mut shown = block.clone();
                if self.typing && shown.kind == "hints" {
                    shown.text = shown.text.lines().map(|_| "\t").collect::<Vec<_>>().join("\n");
                }
                let rows = crate::transcript::build(&shown, width, palette);
                Part {
                    block: block.clone(),
                    width,
                    operations: rows.iter().map(|(_, op)| op.clone()).collect(),
                    lines: rows.into_iter().map(|(line, _)| line).collect(),
                }
            };
            self.lines.extend(part.lines.clone());
            self.operations.extend(part.operations.clone());
            if i + 4096 >= blocks.len() {
                self.parts.insert(key, part);
            }
        }
        self.source = context.to_vec();
        self.blocks = blocks.to_vec();
        self.width = width;
    }
    pub fn reset(&mut self) {
        self.width = 0;
        self.parts.clear();
    }
}
pub struct Regions {
    pub transcript: Rect,
    pub composer: Rect,
    pub sessions: Rect,
    pub details: Rect,
    pub tabs: Rect,
    pub context: Rect,
}
/// Composer height: the editor grows with its wrapped content up to Textual's
/// `max-height: 22` (never below the 8-row resting layout), leaving the
/// transcript at least four rows.
pub fn composer_height(area: Rect, draft: &Editor) -> u16 {
    let width = area.width.saturating_sub(9);
    let rows = editor_view(draft, false, &Palette::new(false), width, u16::MAX).len();
    let wanted = 5 + rows.clamp(3, 22) as u16;
    wanted.min(area.height.saturating_sub(3 + 4)).max(8)
}
pub fn regions(area: Rect, s: &Snapshot, composer_height: u16) -> Regions {
    let main = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(3),
        Constraint::Length(composer_height),
    ])
    .split(area);
    let left = if s.sessions_sidebar && area.width >= 110 {
        30
    } else {
        0
    };
    let right = if s.details_sidebar && area.width >= if left > 0 { 170 } else { 110 } {
        40
    } else {
        0
    };
    let columns = Layout::horizontal([
        Constraint::Length(left),
        Constraint::Min(10),
        Constraint::Length(right),
    ])
    .split(main[1]);
    let center = Layout::vertical([Constraint::Length(0), Constraint::Min(1)]).split(columns[1]);
    Regions {
        tabs: main[0],
        sessions: columns[0],
        details: columns[2],
        context: center[0],
        transcript: center[1],
        composer: main[2],
    }
}
pub fn editor_text(editor: &Editor, secret: bool, palette: &Palette) -> Vec<Line<'static>> {
    let mut lines = vec![Line::default()];
    let selection = editor.selection();
    for (i, g) in editor.text.grapheme_indices(true) {
        if i == editor.cursor {
            lines
                .last_mut()
                .unwrap()
                .spans
                .push(Span::styled("▏", Style::default().fg(palette.accent)));
        }
        if g == "\n" {
            lines.push(Line::default());
            continue;
        }
        let style = if selection.is_some_and(|(a, b)| i >= a && i < b) {
            Style::default().bg(palette.accent).fg(palette.background)
        } else {
            Style::default().fg(palette.text)
        };
        lines.last_mut().unwrap().spans.push(Span::styled(
            if secret {
                "•".to_string()
            } else {
                if g.chars().any(char::is_control) {
                    g.chars().flat_map(char::escape_default).collect()
                } else {
                    g.to_string()
                }
            },
            style,
        ));
    }
    if editor.cursor == editor.text.len() {
        lines
            .last_mut()
            .unwrap()
            .spans
            .push(Span::styled("▏", Style::default().fg(palette.accent)));
    }
    lines
}
pub fn draw(
    frame: &mut Frame,
    s: &Snapshot,
    draft: &Editor,
    form: &Editor,
    answer: &Editor,
    filter: &str,
    selection: usize,
    scroll: usize,
    panel_scroll: usize,
    cache: &mut Cache,
    follow: bool,
    logs_open: bool,
    leader: bool,
    panel_detail: bool,
    sessions_scroll: usize,
    logs_scroll: usize,
    details_scroll: usize,
) -> Regions {
    let p = Palette::new(s.theme == "nexus-light");
    frame.render_widget(
        Block::default().style(Style::default().bg(p.background).fg(p.text)),
        frame.area(),
    );
    let r = regions(frame.area(), s, composer_height(frame.area(), draft));
    draw_top_bar(frame, s, r.tabs, &p);
    if r.sessions.width > 0 {
        let mut lines = vec![Line::styled("SESSIONS", Style::default().fg(p.quiet).add_modifier(Modifier::BOLD))];
        let rows = session_rows(s);
        lines.extend(
            rows.iter()
                .skip(sessions_scroll)
                .take(r.sessions.height.saturating_sub(1) as usize)
                .map(|(text, index)| match index {
                    None => Line::styled(text.clone(), Style::default().fg(p.quiet)),
                    Some(i) if text.starts_with("  ") => Line::styled(
                        text.clone(),
                        Style::default().fg(if s.sessions[*i].state == "running" { p.accent } else { p.quiet }),
                    ),
                    Some(_) => Line::styled(text.clone(), Style::default().fg(p.text)),
                }),
        );
        frame.render_widget(
            Paragraph::new(lines)
                .block(Block::default().borders(Borders::RIGHT).border_style(Style::default().fg(p.border)).padding(ratatui::widgets::Padding::new(1, 1, 1, 0)))
                .style(Style::default().bg(p.panel)),
            r.sessions,
        );
    }
    if r.details.width > 0 {
        frame.render_widget(
            Paragraph::new(details_rows(s, &p, r.details.width.saturating_sub(5)).0.into_iter().skip(details_scroll).collect::<Vec<_>>())
                .block(Block::default().borders(Borders::LEFT).border_style(Style::default().fg(p.border)).padding(ratatui::widgets::Padding::new(2, 2, 1, 0)))
                .style(Style::default().bg(p.panel).fg(p.text)),
            r.details,
        );
    }
    cache.typing = !draft.text.is_empty();
    if s.blocks.is_empty() {
        cache.update(&s.lines, r.transcript.width, &p);
    } else {
        cache.update_content(&[], &s.blocks, r.transcript.width, &p);
    }
    let offset = if follow {
        cache
            .lines
            .len()
            .saturating_sub(r.transcript.height as usize)
    } else {
        scroll.min(cache.lines.len().saturating_sub(1))
    };
    frame.render_widget(
        Paragraph::new(
            cache
                .lines
                .iter()
                .skip(offset)
                .take(r.transcript.height as usize)
                .map(|line| with_spinner(line, cache.spin))
                .collect::<Vec<_>>(),
        ),
        r.transcript,
    );
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(4),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .split(r.composer);
    let inset = |a: Rect, left: u16, right: u16| Rect {
        x: a.x + left,
        width: a.width.saturating_sub(left + right),
        ..a
    };
    // The chat input box: panel background, blue left bar spanning editor and runtime rows.
    let box_area = Rect { x: rows[1].x + 2, width: rows[1].width.saturating_sub(4), y: rows[1].y, height: rows[1].height + 1 };
    frame.render_widget(Block::default().style(Style::default().bg(p.panel)), box_area);
    for y in box_area.y..box_area.y + box_area.height {
        frame.render_widget(
            Paragraph::new("▌").style(Style::default().fg(p.blue).bg(p.panel)),
            Rect { x: box_area.x, y, width: 1, height: 1 },
        );
    }
    let editor_area = Rect {
        x: box_area.x + 3,
        y: rows[1].y + 1,
        width: box_area.width.saturating_sub(5),
        height: rows[1].height.saturating_sub(1),
    };
    frame.render_widget(
        Paragraph::new(editor_view(draft, false, &p, editor_area.width, editor_area.height))
            .style(Style::default().bg(p.panel)),
        editor_area,
    );
    frame.render_widget(
        Paragraph::new(s.attachment_lines.join(" · ")).style(Style::default().fg(p.accent)),
        inset(rows[0], 2, 2),
    );
    let sep = || Span::styled(" · ", Style::default().fg(p.quiet).bg(p.panel));
    let on_panel = Style::default().bg(p.panel);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                {
                    let mut name = s.agent.clone();
                    if let Some(first) = name.get_mut(0..1) {
                        first.make_ascii_uppercase();
                    }
                    name
                },
                on_panel.fg(p.blue).add_modifier(Modifier::BOLD),
            ),
            sep(),
            Span::styled(s.model.clone(), on_panel.fg(p.text)),
            Span::styled(format!(" {}", s.provider), on_panel.fg(p.muted)),
            sep(),
            Span::styled(s.effort.clone(), on_panel.fg(p.muted)),
        ]))
        .style(on_panel),
        Rect { x: box_area.x + 3, y: rows[2].y, width: box_area.width.saturating_sub(4), height: 1 },
    );
    let hint = "ctrl+p commands";
    let usage = s.context_usage.clone();
    let right = format!("{usage}  {hint}");
    let width = usize::from(rows[3].width);
    let cwd: String = s.breadcrumb.chars().take(width.saturating_sub(right.width() + 6)).collect();
    let gap = width.saturating_sub(2 + cwd.width() + right.width() + 2);
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::raw("  "),
            Span::styled(cwd, Style::default().fg(p.quiet)),
            Span::raw(" ".repeat(gap)),
            Span::styled(right, Style::default().fg(p.quiet)),
        ])),
        rows[3],
    );
    if leader {
        frame.render_widget(
            Paragraph::new("  Ctrl+X · M model · V voice · ? hotkeys").style(Style::default().fg(p.accent)),
            rows[4],
        );
    }
    let composer = [rows[1], inset(rows[0], 2, 2)];
    if logs_open {
        let area = logs_region(r.transcript);
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(
                s.logs
                    .iter()
                    .skip(logs_scroll)
                    .take(area.height.saturating_sub(2) as usize)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n"),
            )
            .wrap(ratatui::widgets::Wrap { trim: false })
            .block(
                Block::default()
                    .title("Logs · Ctrl+E close")
                    .borders(Borders::ALL),
            ),
            area,
        );
    }
    if !s.panel_title.is_empty() {
        let inner = dialog_frame(frame, r.transcript, &s.panel_title, &p);
        if let Some(f) = &s.form {
            let areas = Layout::vertical([Constraint::Min(1), Constraint::Length(2)]).split(inner);
            frame.render_widget(
                Paragraph::new(editor_view(form, f.secret, &p, areas[0].width, areas[0].height))
                    .style(Style::default().bg(p.dialog)),
                areas[0],
            );
            frame.render_widget(
                Paragraph::new(format!(
                    "{}\nCtrl+S save · Ctrl+D delete · Escape close",
                    f.status
                ))
                .style(Style::default().fg(p.accent).bg(p.dialog)),
                areas[1],
            );
        } else if !s.items.is_empty() && !panel_detail {
            let needle = filter.to_lowercase();
            let rows: Vec<_> = s
                .items
                .iter()
                .filter(|item| item.label.to_lowercase().contains(&needle))
                .collect();
            let start = selection.saturating_sub(inner.height.saturating_sub(3) as usize);
            let mut lines = vec![Line::from(vec![
                Span::styled("Filter ", Style::default().fg(p.quiet)),
                Span::styled(format!("{filter}▏"), Style::default().fg(p.text)),
                Span::styled("   Tab inspect details", Style::default().fg(p.quiet)),
            ])];
            lines.push(Line::default());
            for (i, item) in rows
                .iter()
                .enumerate()
                .skip(start)
                .take(inner.height.saturating_sub(2) as usize)
            {
                let on = i == selection;
                let style = if on {
                    Style::default().fg(p.text).bg(p.element_hi).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(p.muted).bg(p.dialog)
                };
                let text = format!(" {} {}", if on { "▸" } else { " " }, item.label);
                let pad = usize::from(inner.width).saturating_sub(text.width());
                lines.push(Line::styled(format!("{text}{}", " ".repeat(pad)), style));
            }
            frame.render_widget(Paragraph::new(lines).style(Style::default().bg(p.dialog)), inner);
        } else {
            let mut panel = Cache::default();
            panel.update(&s.panel_lines, inner.width, &p);
            frame.render_widget(
                Paragraph::new(
                    panel
                        .lines
                        .into_iter()
                        .skip(panel_scroll)
                        .take(inner.height as usize)
                        .collect::<Vec<_>>(),
                )
                .style(Style::default().bg(p.dialog)),
                inner,
            );
        }
    }
    if let Some(prompt) = &s.prompt {
        // Textual shows approvals and questions as a short panel above the
        // composer with the transcript still visible.
        let area = prompt_area(r.transcript, prompt);
        frame.render_widget(Clear, area);
        frame.render_widget(Block::default().style(Style::default().bg(p.panel).fg(p.text)), area);
        let title = if prompt.kind == "permission" { "Permission requested" } else { "Question" };
        frame.render_widget(
            Paragraph::new(title).style(Style::default().fg(p.accent).bg(p.panel).add_modifier(Modifier::BOLD)),
            Rect { x: area.x + 2, y: area.y, width: area.width.saturating_sub(4), height: 1 },
        );
        let (content_area, choices_area) = prompt_regions(area, prompt.choices.len());
        let mut content = Cache::default();
        content.update(&prompt.lines, content_area.width, &p);
        frame.render_widget(
            Paragraph::new(
                content
                    .lines
                    .into_iter()
                    .skip(panel_scroll)
                    .take(content_area.height as usize)
                    .collect::<Vec<_>>(),
            )
            .style(Style::default().bg(p.panel)),
            content_area,
        );
        let mut lines: Vec<Line> = prompt
            .choices
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let on = i == selection;
                let text = format!(
                    " {} [{}] {}{}",
                    if on { "▸" } else { " " },
                    c.key,
                    c.label,
                    if c.disabled { " (unavailable)" } else { "" }
                );
                let pad = usize::from(choices_area.width).saturating_sub(text.width());
                let style = if c.disabled {
                    Style::default().fg(p.quiet).bg(p.panel)
                } else if on {
                    Style::default().fg(p.text).bg(p.element_hi).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(p.muted).bg(p.panel)
                };
                Line::styled(format!("{text}{}", " ".repeat(pad)), style)
            })
            .collect();
        if prompt.kind == "question" {
            lines.push(Line::from(vec![
                Span::styled(" Answer ", Style::default().fg(p.quiet)),
                Span::styled(format!("{}▏", answer.text), Style::default().fg(p.text)),
                Span::styled("   Enter submit", Style::default().fg(p.quiet)),
            ]));
        }
        lines.push(Line::styled(
            " ↑/↓ choose · Enter select · keys pick · Esc deny",
            Style::default().fg(p.quiet),
        ));
        frame.render_widget(Paragraph::new(lines).style(Style::default().bg(p.panel)), choices_area);
    }
    if s.voice_phase != "idle" && !s.voice_phase.is_empty() {
        frame.render_widget(
            Paragraph::new(format!(
                "● {} {} · {}",
                s.voice_phase,
                "▂".repeat((s.voice_level * 20.0) as usize),
                s.voice_preview
            ))
            .style(Style::default().fg(p.accent)),
            composer[1],
        );
    }
    r
}
#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};
    #[test]
    fn hints_blank_while_typing_but_keep_their_rows() {
        let palette = Palette::new(false);
        let blocks = vec![crate::bridge::Content {
            id: "empty-hints".into(), kind: "hints".into(), gap: 4,
            text: "esc\tstop\n  /\tcommands".into(), ..Default::default()
        }];
        let mut cache = Cache::default();
        cache.update_content(&[], &blocks, 80, &palette);
        let shown = cache.lines.len();
        assert_eq!(shown, 6);
        assert!(cache.lines[4].spans.iter().any(|span| span.content.contains("stop")));
        cache.typing = true;
        cache.update_content(&[], &blocks, 80, &palette);
        assert_eq!(cache.lines.len(), shown);
        assert!(!cache.lines[4].spans.iter().any(|span| span.content.contains("stop")));
    }
    #[test]
    fn modified_file_rows_expand_with_a_colored_diff_and_map_to_files() {
        use crate::bridge::FileChange;
        let mut s = Snapshot::default();
        s.details_panel.files = vec![
            FileChange { path: "src/a.rs".into(), added: 1, removed: 1, open: true, diff: vec!["@@ -1 +1 @@".into(), "-old".into(), "+new".into()], ..Default::default() },
            FileChange { path: "b.rs".into(), created: true, ..Default::default() },
        ];
        let (lines, files) = details_rows(&s, &Palette::new(false), 40);
        assert_eq!(lines.len(), files.len());
        let first = files.iter().position(|f| *f == Some(0)).unwrap();
        let text: String = lines[first].spans.iter().map(|span| span.content.as_ref()).collect();
        assert!(text.starts_with("▾ M ") && text.contains("a.rs"));
        assert_eq!(lines[first + 2].spans[0].content, "  -old");
        assert_eq!(files[first + 1], None, "diff rows do not toggle");
        let second: String = lines[files.iter().position(|f| *f == Some(1)).unwrap()].spans.iter().map(|span| span.content.as_ref()).collect();
        assert!(second.starts_with("▸ A "));
    }
    #[test]
    fn running_slot_is_replaced_per_frame() {
        let line = Line::from(vec![Span::raw(format!("{SPINNER_SLOT} Bash · ls"))]);
        assert_eq!(with_spinner(&line, 0).spans[0].content, "⠋ Bash · ls");
        assert_eq!(with_spinner(&line, 11).spans[0].content, "⠙ Bash · ls");
        let s = Snapshot::default();
        assert!(!animating(&s));
    }
    #[test]
    fn composer_grows_with_content_and_is_capped() {
        let area = Rect::new(0, 0, 80, 60);
        let mut draft = Editor::default();
        assert_eq!(composer_height(area, &draft), 8);
        draft.insert(&"line\n".repeat(9));
        assert_eq!(composer_height(area, &draft), 5 + 10);
        draft.insert(&"line\n".repeat(60));
        assert_eq!(composer_height(area, &draft), 5 + 22);
        assert_eq!(composer_height(Rect::new(0, 0, 80, 14), &draft), 8, "the transcript keeps its rows");
    }
    #[test]
    fn narrow_layout_and_safe_wrap() {
        let mut terminal = Terminal::new(TestBackend::new(60, 20)).unwrap();
        let s = Snapshot {
            schema: 1,
            title: "Nexus".into(),
            lines: vec!["hello world".into()],
            ..Default::default()
        };
        let mut cache = Cache::default();
        terminal
            .draw(|frame| {
                draw(
                    frame,
                    &s,
                    &Editor::default(),
                    &Editor::default(),
                    &Editor::default(),
                    "",
                    0,
                    0,
                    0,
                    &mut cache,
                    true,
                    false,
                    false,
                    false,
                    0,
                    0,
                    0,
                );
            })
            .unwrap();
        assert!(terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|c| c.symbol() == "h"));
    }
}

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

fn editor_view(
    editor: &Editor,
    secret: bool,
    palette: &Palette,
    width: u16,
    height: u16,
) -> Vec<Line<'static>> {
    let mut rows = vec![Line::default()];
    let mut cursor_row = 0;
    for (i, logical) in editor_text(editor, secret, palette).into_iter().enumerate() {
        if i > 0 {
            rows.push(Line::default());
        }
        let mut columns = 0usize;
        for span in logical.spans {
            for grapheme in span.content.graphemes(true) {
                if columns + grapheme.width() > usize::from(width.max(1)) && columns > 0 {
                    rows.push(Line::default());
                    columns = 0;
                }
                if grapheme == "▏" && span.style.fg == Some(palette.accent) {
                    cursor_row = rows.len() - 1;
                }
                rows.last_mut()
                    .unwrap()
                    .spans
                    .push(Span::styled(grapheme.to_string(), span.style));
                columns += grapheme.width();
            }
        }
    }
    let offset = cursor_row.saturating_sub(height.saturating_sub(1) as usize);
    rows.into_iter()
        .skip(offset)
        .take(height as usize)
        .collect()
}

#[cfg(test)]
mod editor_layout_tests {
    use super::*;
    #[test]
    fn long_line_keeps_cursor_visible_and_graphemes_whole() {
        let mut editor = Editor::default();
        editor.insert(&"🧑🏽‍💻a".repeat(80));
        let rows = editor_view(&editor, false, &Palette::new(false), 12, 3);
        assert!(rows.len() <= 3);
        assert!(rows.iter().all(|row| row.width() <= 12));
        assert!(rows
            .iter()
            .flat_map(|row| row.spans.iter())
            .any(|span| span.content == "▏"));
    }
}

pub fn session_rows(s: &Snapshot) -> Vec<(String, Option<usize>)> {
    let mut rows = Vec::new();
    let mut previous = "";
    for (i, session) in s.sessions.iter().enumerate() {
        if session.group != previous {
            rows.push((session.group.clone(), None));
            previous = &session.group;
        }
        rows.push((session.title.clone(), Some(i)));
        rows.push((format!("  {}", session.state), Some(i)));
    }
    rows
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

pub fn logs_region(transcript: Rect) -> Rect {
    Rect::new(
        transcript.x,
        transcript.y + transcript.height / 2,
        transcript.width,
        transcript.height - transcript.height / 2,
    )
}

#[cfg(test)]
mod history_benchmark {
    use super::*;
    #[test]
    #[ignore = "manual projection/cache microbenchmark; reports timings without a flaky threshold"]
    fn thousand_turns() {
        let mut blocks: Vec<crate::bridge::Content> = (0..1000)
            .map(|i| crate::bridge::Content {
                id: i.to_string(),
                kind: "markdown".into(),
                title: format!("Turn {i}"),
                text: "Result with **formatted** context.\n".repeat(20),
                ..Default::default()
            })
            .collect();
        let mut cache = Cache::default();
        let palette = Palette::new(false);
        let start = std::time::Instant::now();
        cache.update_content(&[], &blocks, 100, &palette);
        let cold = start.elapsed();
        blocks.last_mut().unwrap().text.push_str("stream delta");
        let start = std::time::Instant::now();
        cache.update_content(&[], &blocks, 100, &palette);
        println!(
            "1000-turn cache: cold={cold:?}, one changed={:?}, rows={}",
            start.elapsed(),
            cache.lines.len()
        );
        assert_eq!(cache.operations.len(), cache.lines.len());
        assert!(cache.lines.iter().any(|row| row
            .spans
            .iter()
            .any(|span| span.content.contains("stream delta"))));
    }
}

fn draw_top_bar(frame: &mut Frame, s: &Snapshot, area: Rect, p: &Palette) {
    let width = usize::from(area.width);
    let background = Style::default().bg(p.panel);
    // Row 0: session tabs. Hit-testing in main.rs mirrors this layout:
    // x starts at 2, each tab is `title_width + 4` wide, tabs are two apart.
    let mut tabs = vec![Span::styled("▌", background.fg(p.border_strong)), Span::styled(" ", background)];
    let mut used = 2usize;
    if s.tabs.is_empty() {
        tabs.push(Span::styled(s.title.clone(), background.fg(p.text).add_modifier(Modifier::BOLD)));
        used += s.title.width();
    }
    for row in s.tabs.iter().take(8) {
        let active = s.title.ends_with(&row.id);
        let title: String = row.title.chars().take(24).collect();
        let title_style = if active { background.fg(p.text).add_modifier(Modifier::BOLD) } else { background.fg(p.muted) };
        tabs.push(Span::styled(if active { "▸ " } else { "· " }, background.fg(if active { p.accent } else { p.quiet })));
        tabs.push(Span::styled(title.clone(), title_style));
        tabs.push(Span::styled(" ×", background.fg(p.quiet)));
        tabs.push(Span::styled("  ", background));
        used += title.width() + 6;
    }
    let pad = width.saturating_sub(used + 4);
    tabs.push(Span::styled(" ".repeat(pad), background));
    tabs.push(Span::styled(" + ", background.fg(p.muted)));
    tabs.push(Span::styled("▐", background.fg(p.accent)));
    // Row 1: workspace breadcrumb left, status right.
    let (word, tone) = match s.status.as_str() {
        "running" | "active" | "working" => ("Working".to_string(), p.accent),
        "awaiting_input" | "awaiting_permission" | "input" => ("Needs input".to_string(), p.warning),
        "" | "idle" => ("Idle".to_string(), p.quiet),
        other => {
            let mut word = other.replace('_', " ");
            if let Some(first) = word.get_mut(0..1) {
                first.make_ascii_uppercase();
            }
            (word, p.quiet)
        }
    };
    let crumb: String = s.breadcrumb.chars().take(width.saturating_sub(word.width() + 6)).collect();
    let gap = width.saturating_sub(2 + crumb.width() + word.width() + 2);
    let row1 = Line::from(vec![
        Span::styled("  ", background),
        Span::styled(crumb, background.fg(p.muted)),
        Span::styled(" ".repeat(gap), background),
        Span::styled(word, background.fg(tone)),
        Span::styled("  ", background),
    ]);
    let rule = Line::styled("─".repeat(width), Style::default().fg(p.border).bg(p.background));
    frame.render_widget(Paragraph::new(vec![Line::from(tabs), row1, rule]), area);
}

/// Details sidebar rows, and for each row the modified file it toggles (if any).
pub fn details_rows(s: &Snapshot, p: &Palette, width: u16) -> (Vec<Line<'static>>, Vec<Option<usize>>) {
    let title = |text: &str| Line::styled(text.to_string(), Style::default().fg(p.quiet).add_modifier(Modifier::BOLD));
    let quiet = |text: &str| Line::styled(text.to_string(), Style::default().fg(p.quiet));
    let d = &s.details_panel;
    let mut out = vec![title("SESSION")];
    for (label, value) in &d.session {
        out.push(Line::from(vec![
            Span::styled(format!("{label:<11}"), Style::default().fg(p.quiet)),
            Span::styled(value.chars().take(usize::from(width).saturating_sub(11)).collect::<String>(), Style::default().fg(p.text)),
        ]));
    }
    out.push(Line::default());
    let mut file_rows: Vec<Option<usize>> = vec![None; out.len()];
    out.push(title(&if d.files.is_empty() { "MODIFIED FILES".to_string() } else { format!("MODIFIED FILES  {}", d.files.len()) }));
    out.push(Line::default());
    if d.files.is_empty() {
        out.push(quiet("No files changed yet."));
    }
    file_rows.resize(out.len(), None);
    for (index, file) in d.files.iter().enumerate() {
        let (head, base) = file.path.rsplit_once('/').map_or(("", file.path.as_str()), |(h, b)| (h, b));
        let mut spans = vec![
            Span::styled(if file.open { "▾ " } else { "▸ " }, Style::default().fg(p.quiet)),
            Span::styled(if file.created { "A " } else { "M " }, Style::default().fg(if file.created { p.success } else { p.warning })),
        ];
        if !head.is_empty() {
            spans.push(Span::styled(format!("{head}/"), Style::default().fg(p.quiet)));
        }
        spans.push(Span::raw(base.to_string()));
        spans.push(Span::raw("  "));
        if file.added > 0 || file.removed > 0 {
            spans.push(Span::styled(format!("+{}", file.added), Style::default().fg(p.success)));
            spans.push(Span::styled(format!(" -{}", file.removed), Style::default().fg(p.error)));
        } else if file.created {
            spans.push(Span::styled("new", Style::default().fg(p.success)));
        }
        file_rows.resize(out.len(), None);
        out.push(Line::from(spans));
        file_rows.push(Some(index));
        if file.open {
            for row in &file.diff {
                let tone = match row.chars().next() {
                    Some('+') => p.success,
                    Some('-') => p.error,
                    Some('@') | Some('…') => p.quiet,
                    _ => p.muted,
                };
                out.push(Line::styled(format!("  {row}"), Style::default().fg(tone)));
            }
            if file.diff.is_empty() {
                out.push(quiet(if file.created { "  New file; no diff preview reported." } else { "  No diff preview reported." }));
            }
        }
    }
    file_rows.resize(out.len(), None);
    if !d.files_summary.is_empty() {
        out.push(quiet(&d.files_summary));
    }
    out.push(Line::default());
    out.push(title("MCP SERVERS"));
    out.push(Line::default());
    for (tone, text, note) in &d.mcp {
        let color = |name: &str| match name {
            "success" => p.success,
            "error" | "plain-error" => p.error,
            "warning" => p.warning,
            _ => p.quiet,
        };
        if tone.starts_with("plain-") {
            out.push(Line::styled(text.clone(), Style::default().fg(color(tone))));
        } else {
            out.push(Line::from(vec![
                Span::styled("● ", Style::default().fg(color(tone))),
                Span::raw(text.clone()),
                Span::styled(format!("  {note}"), Style::default().fg(p.quiet)),
            ]));
        }
    }
    file_rows.resize(out.len(), None);
    (out, file_rows)
}

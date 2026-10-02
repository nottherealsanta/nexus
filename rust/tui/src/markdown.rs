//! Markdown to styled cells. HTML is literal; URLs remain visible.
use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
pub fn lines(text: &str) -> Vec<Line<'static>> {
    let mut out = vec![Line::default()];
    let mut bold = 0;
    let mut italic = 0;
    let mut code = 0;
    let mut links = Vec::new();
    for event in Parser::new_ext(
        text,
        Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TABLES | Options::ENABLE_TASKLISTS,
    ) {
        match event {
            Event::Start(Tag::Strong) => bold += 1,
            Event::End(TagEnd::Strong) => bold -= 1,
            Event::Start(Tag::Emphasis) => italic += 1,
            Event::End(TagEnd::Emphasis) => italic -= 1,
            Event::Start(Tag::Heading { .. }) => {
                bold += 1;
            }
            Event::End(TagEnd::Heading(_)) => {
                bold -= 1;
                out.push(Line::default());
            }
            Event::Start(Tag::CodeBlock(_)) => code += 1,
            Event::End(TagEnd::CodeBlock) => {
                code -= 1;
                out.push(Line::default());
            }
            Event::Start(Tag::Link { dest_url, .. })
            | Event::Start(Tag::Image { dest_url, .. }) => links.push(dest_url.to_string()),
            Event::End(TagEnd::Link) | Event::End(TagEnd::Image) => {
                if let Some(url) = links.pop() {
                    out.last_mut().unwrap().spans.push(Span::styled(
                        format!(" ({url})"),
                        Style::default().fg(Color::Blue),
                    ));
                }
            }
            Event::Start(Tag::Item) => out.last_mut().unwrap().spans.push(Span::raw("• ")),
            Event::End(TagEnd::Paragraph) => {
                out.push(Line::default());
                out.push(Line::default());
            }
            Event::End(TagEnd::Item) | Event::End(TagEnd::TableRow) => out.push(Line::default()),
            Event::Text(value)
            | Event::Html(value)
            | Event::InlineHtml(value)
            | Event::Code(value) => {
                let mut style = Style::default();
                if bold > 0 {
                    style = style.add_modifier(Modifier::BOLD);
                }
                if italic > 0 {
                    style = style.add_modifier(Modifier::ITALIC);
                }
                if code > 0 {
                    style = style.fg(Color::Cyan);
                }
                for (i, part) in value.split('\n').enumerate() {
                    if i > 0 {
                        out.push(Line::default());
                    }
                    out.last_mut()
                        .unwrap()
                        .spans
                        .push(Span::styled(part.to_string(), style));
                }
            }
            Event::SoftBreak | Event::HardBreak => out.push(Line::default()),
            Event::Rule => out
                .last_mut()
                .unwrap()
                .spans
                .push(Span::raw("────────────────")),
            Event::TaskListMarker(done) => {
                out.last_mut()
                    .unwrap()
                    .spans
                    .push(Span::raw(if done { "[x] " } else { "[ ] " }))
            }
            _ => {}
        }
    }
    out
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn literal_html_and_link_target() {
        let rows =
            lines("**bold** [link](https://example.com)\n\n<environment>literal</environment>");
        let text = rows
            .iter()
            .flat_map(|r| r.spans.iter())
            .map(|s| s.content.as_ref())
            .collect::<String>();
        assert!(text.contains("https://example.com"));
        assert!(text.contains("<environment>literal</environment>"));
        assert!(rows[0].spans[0].style.add_modifier.contains(Modifier::BOLD));
    }
}

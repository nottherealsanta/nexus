//! Native Markdown layout. HTML is literal; only http/https/mailto links open.
use crate::theme::Theme;
#[cfg(test)]
use core::prelude::v1::test;
use gpui::{prelude::*, *};
use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};
use std::ops::Range;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Span {
    pub range: Range<usize>,
    pub bold: bool,
    pub italic: bool,
    pub struck: bool,
    pub code: bool,
}
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Block {
    pub text: String,
    pub spans: Vec<Span>,
    pub links: Vec<(Range<usize>, String)>,
    pub heading: u8,
    pub code: Option<String>,
    pub quote: bool,
    pub rule: bool,
    pub cells: Vec<Block>,
    pub table_head: bool,
}

pub fn parse(text: &str) -> Vec<Block> {
    let mut blocks = vec![];
    let mut current = Block::default();
    let mut bold = 0;
    let mut italic = 0;
    let mut struck = 0;
    let mut lists: Vec<Option<u64>> = vec![];
    let mut link: Option<(usize, String)> = None;
    let mut table = false;
    let mut table_head = false;
    let mut cells = vec![];
    fn flush(blocks: &mut Vec<Block>, current: &mut Block) {
        if !current.text.is_empty() || current.rule || !current.cells.is_empty() {
            blocks.push(std::mem::take(current));
        }
    }
    for event in Parser::new_ext(
        text,
        Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS,
    ) {
        match event {
            Event::Start(Tag::Paragraph) => {}
            Event::End(TagEnd::Paragraph) if !table => {
                let quote = current.quote;
                flush(&mut blocks, &mut current);
                current.quote = quote;
            }
            Event::Start(Tag::Heading { level, .. }) => current.heading = level as u8,
            Event::End(TagEnd::Heading(_)) => flush(&mut blocks, &mut current),
            Event::Start(Tag::CodeBlock(kind)) => {
                flush(&mut blocks, &mut current);
                current.code = Some(match kind {
                    pulldown_cmark::CodeBlockKind::Fenced(lang) => lang.to_string(),
                    _ => String::new(),
                });
            }
            Event::End(TagEnd::CodeBlock) => flush(&mut blocks, &mut current),
            Event::Start(Tag::Strong) => bold += 1,
            Event::End(TagEnd::Strong) => bold -= 1,
            Event::Start(Tag::Emphasis) => italic += 1,
            Event::End(TagEnd::Emphasis) => italic -= 1,
            Event::Start(Tag::Strikethrough) => struck += 1,
            Event::End(TagEnd::Strikethrough) => struck -= 1,
            Event::Start(Tag::BlockQuote(_)) => current.quote = true,
            Event::End(TagEnd::BlockQuote(_)) => {
                flush(&mut blocks, &mut current);
                current.quote = false;
            }
            Event::Start(Tag::List(start)) => lists.push(start),
            Event::End(TagEnd::List(_)) => {
                lists.pop();
            }
            Event::Start(Tag::Item) => {
                flush(&mut blocks, &mut current);
                current
                    .text
                    .push_str(&"  ".repeat(lists.len().saturating_sub(1)));
                if let Some(Some(number)) = lists.last_mut() {
                    current.text.push_str(&format!("{number}.  "));
                    *number += 1;
                } else {
                    current.text.push_str("•  ");
                }
            }
            Event::End(TagEnd::Item) => flush(&mut blocks, &mut current),
            Event::Start(Tag::Link { dest_url, .. }) => {
                link = Some((current.text.len(), dest_url.to_string()))
            }
            Event::End(TagEnd::Link) => {
                if let Some((start, url)) = link.take() {
                    current.links.push((start..current.text.len(), url));
                }
            }
            Event::Start(Tag::Image { dest_url, .. }) => {
                current.text.push_str(&format!("[Image: {dest_url}] "));
            }
            Event::Start(Tag::Table(_)) => {
                flush(&mut blocks, &mut current);
                table = true;
            }
            Event::End(TagEnd::Table) => table = false,
            Event::Start(Tag::TableHead) => table_head = true,
            Event::End(TagEnd::TableHead) | Event::End(TagEnd::TableRow) => {
                blocks.push(Block {
                    cells: std::mem::take(&mut cells),
                    table_head,
                    ..Block::default()
                });
                table_head = false;
            }
            Event::End(TagEnd::TableCell) => cells.push(std::mem::take(&mut current)),
            Event::Text(s) | Event::Html(s) | Event::InlineHtml(s) => {
                let start = current.text.len();
                current.text.push_str(&s);
                current.spans.push(Span {
                    range: start..current.text.len(),
                    bold: bold > 0,
                    italic: italic > 0,
                    struck: struck > 0,
                    code: false,
                });
            }
            Event::Code(s) => {
                let start = current.text.len();
                current.text.push_str(&s);
                current.spans.push(Span {
                    range: start..current.text.len(),
                    bold: bold > 0,
                    italic: italic > 0,
                    struck: struck > 0,
                    code: true,
                });
            }
            Event::SoftBreak | Event::HardBreak => current.text.push('\n'),
            Event::Rule => {
                flush(&mut blocks, &mut current);
                blocks.push(Block {
                    rule: true,
                    ..Block::default()
                });
            }
            Event::TaskListMarker(done) => {
                current.text.push_str(if done { "☑ " } else { "☐ " })
            }
            _ => {}
        }
    }
    flush(&mut blocks, &mut current);
    blocks
}
pub fn highlights(block: &Block, t: Theme) -> Vec<(Range<usize>, HighlightStyle)> {
    let mut styles: Vec<_> = block
        .spans
        .iter()
        .map(|s| {
            (
                s.range.clone(),
                HighlightStyle {
                    font_weight: s.bold.then_some(FontWeight::SEMIBOLD),
                    font_style: s.italic.then_some(FontStyle::Italic),
                    color: s.code.then_some(t.accent),
                    background_color: s.code.then_some(t.accent_bg),
                    strikethrough: s.struck.then_some(StrikethroughStyle {
                        color: None,
                        thickness: px(1.),
                    }),
                    ..Default::default()
                },
            )
        })
        .collect();
    for (range, _) in &block.links {
        styles.push((
            range.clone(),
            HighlightStyle {
                color: Some(t.accent),
                underline: Some(UnderlineStyle {
                    color: Some(t.accent),
                    thickness: px(1.),
                    wavy: false,
                }),
                ..Default::default()
            },
        ));
    }
    if block.code.as_deref() == Some("diff") {
        let mut start = 0;
        for line in block.text.split_inclusive('\n') {
            if line.starts_with('+') || line.starts_with('-') {
                styles.push((
                    start..start + line.len(),
                    HighlightStyle {
                        color: Some(if line.starts_with('+') {
                            t.green
                        } else {
                            t.red
                        }),
                        ..Default::default()
                    },
                ));
            }
            start += line.len();
        }
    }
    styles
}
pub fn render(
    text: &str,
    t: Theme,
    id: &str,
    mut selectable: impl FnMut(&Block, SharedString, Theme) -> AnyElement,
) -> AnyElement {
    let mut rows = vec![];
    for (i, block) in parse(text).into_iter().enumerate() {
        let key = SharedString::from(format!("md-{id}-{i}"));
        if block.rule {
            rows.push(div().h(px(1.)).my_3().bg(t.border).into_any_element());
            continue;
        }
        if !block.cells.is_empty() {
            rows.push(
                div()
                    .flex()
                    .w_full()
                    .rounded(px(2.))
                    .border_b_1()
                    .border_color(t.border)
                    .bg(if block.table_head {
                        t.surface
                    } else {
                        t.background
                    })
                    .children(block.cells.iter().enumerate().map(|(j, cell)| {
                        div()
                            .flex_1()
                            .min_w_0()
                            .px_3()
                            .py_2()
                            .text_size(px(12.))
                            .font_weight(if block.table_head {
                                FontWeight::SEMIBOLD
                            } else {
                                FontWeight::NORMAL
                            })
                            .child(selectable(cell, format!("{key}-{j}").into(), t))
                    }))
                    .into_any_element(),
            );
            continue;
        }
        if let Some(lang) = &block.code {
            let source = block.text.clone();
            rows.push(
                div()
                    .rounded(px(3.))
                    .border_1()
                    .border_color(t.border)
                    .bg(t.sidebar)
                    .overflow_hidden()
                    .child(
                        div()
                            .flex()
                            .justify_between()
                            .px_4()
                            .py_2()
                            .border_b_1()
                            .border_color(t.border)
                            .text_size(px(10.))
                            .text_color(t.muted)
                            .child(if lang.is_empty() {
                                "CODE".into()
                            } else {
                                lang.to_uppercase()
                            })
                            .child(
                                div()
                                    .id(SharedString::from(format!("{key}-copy")))
                                    .cursor(CursorStyle::Arrow)
                                    .child("Copy")
                                    .on_click(move |_, _, cx| {
                                        cx.write_to_clipboard(ClipboardItem::new_string(
                                            source.clone(),
                                        ))
                                    }),
                            ),
                    )
                    .child(
                        div()
                            .id(key.clone())
                            .overflow_x_scroll()
                            .p_4()
                            .font_family("Menlo")
                            .text_size(px(12.))
                            .line_height(px(21.))
                            .child(selectable(&block, format!("{key}-source").into(), t)),
                    )
                    .into_any_element(),
            );
            continue;
        }
        let mut row = div()
            .w_full()
            .text_size(px(if block.heading > 0 {
                24. - block.heading as f32 * 2.
            } else {
                15.
            }))
            .line_height(px(26.))
            .text_color(t.text)
            .child(selectable(&block, key, t));
        if block.heading > 0 {
            row = row.font_weight(FontWeight::SEMIBOLD).mt_3();
        } else if block.quote {
            row = row
                .pl_4()
                .border_l_2()
                .border_color(t.accent)
                .text_color(t.muted);
        }
        rows.push(row.into_any_element());
    }
    div()
        .flex()
        .flex_col()
        .gap_3()
        .children(rows)
        .into_any_element()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn markdown_preserves_unicode_styles_urls_and_literal_html() {
        let b = parse("**日本語** and [docs](https://example.com) <script>x</script>");
        assert!(b[0]
            .spans
            .iter()
            .any(|s| s.bold && &b[0].text[s.range.clone()] == "日本語"));
        assert_eq!(b[0].links[0].1, "https://example.com");
        assert!(b[0].text.contains("<script>x</script>"));
    }
    #[test]
    fn tables_keep_cells_and_code_keeps_language() {
        let b = parse("| Name | Value |\n|---|---|\n| foo | bar |\n\n```diff\n+ new\n- old\n```\n");
        assert_eq!(b[0].cells.len(), 2);
        assert!(b[0].table_head);
        assert_eq!(b[1].cells[0].text, "foo");
        assert_eq!(b[2].code.as_deref(), Some("diff"));
        assert_eq!(b[2].text, "+ new\n- old\n");
    }
}

//! Transcript turn minimap (§2.4.3).
//!
//! Ticks are derived only from the projection's block kinds — the desktop does
//! not invent counts or timestamps. A user message is a long tick; assistant
//! prose is a short tick.
use crate::bridge::Content;

const LABEL_LIMIT: usize = 80;

#[derive(Clone, Debug, PartialEq)]
pub struct Tick {
    /// Index into the transcript block list, used to scroll on click.
    pub block: usize,
    /// User turns draw a long tick, assistant turns a short one.
    pub long: bool,
    /// First non-empty line, bounded, for the hover hint.
    pub label: String,
}

pub fn ticks(blocks: &[Content]) -> Vec<Tick> {
    let mut ticks = vec![];
    for (index, block) in blocks.iter().enumerate() {
        let long = match block.kind.as_str() {
            "user" => true,
            "markdown" => false,
            _ => continue,
        };
        let source = if block.title.is_empty() {
            &block.text
        } else {
            &block.title
        };
        let label = source
            .lines()
            .find(|line| !line.trim().is_empty())
            .unwrap_or("")
            .trim();
        ticks.push(Tick {
            block: index,
            long,
            label: label.chars().take(LABEL_LIMIT).collect(),
        });
    }
    ticks
}

#[cfg(test)]
mod tests {
    use super::*;

    fn block(kind: &str, text: &str) -> Content {
        Content {
            kind: kind.into(),
            text: text.into(),
            ..Default::default()
        }
    }

    #[test]
    fn ticks_mark_user_turns_long_and_assistant_short() {
        let blocks = vec![
            block("context_header", "ignored"),
            block("user", "Fix the parser"),
            block("tool", "read file"),
            block("markdown", "Done.\nSecond line"),
            block("user", "Now the docs"),
            block("markdown", "Updated."),
        ];
        let ticks = ticks(&blocks);
        assert_eq!(ticks.len(), 4);
        assert_eq!(
            ticks[0],
            Tick {
                block: 1,
                long: true,
                label: "Fix the parser".into()
            }
        );
        assert_eq!(
            ticks[1],
            Tick {
                block: 3,
                long: false,
                label: "Done.".into()
            }
        );
        assert_eq!(ticks[2].block, 4);
        assert!(ticks[2].long);
        assert_eq!(ticks[3].block, 5);
        assert!(!ticks[3].long);
    }

    #[test]
    fn labels_are_bounded_and_skip_blank_lines() {
        let blocks = vec![block("markdown", "\n\nA very long first line\nrest")];
        assert_eq!(ticks(&blocks)[0].label, "A very long first line");
        let long = "x".repeat(200);
        let ticks = ticks(&[block("user", &long)]);
        assert_eq!(ticks[0].label.chars().count(), LABEL_LIMIT);
    }

    #[test]
    fn non_conversational_blocks_produce_no_ticks() {
        assert!(ticks(&[block("tool", "x"), block("thought", "y")]).is_empty());
    }
}

//! Sticky user message: while a turn's content is on screen but its user card has scrolled
//! off the top, the card's first rows stay pinned over the top of the transcript.
//!
//! Pure selection over `(top, rows)` card spans (a card is a padding row, the title and text
//! rows, a padding row); the draw loop in `render.rs` paints the rows and records the hit
//! rectangle so a click scrolls back to the real message.
use ratatui::{
    style::Color,
    text::{Line, Span},
};
use unicode_width::UnicodeWidthStr;

/// Most rows the pinned copy may take.
pub const MAX_ROWS: usize = 3;

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub struct Pin {
    /// Index into the card list of the pinned user message.
    pub card: usize,
    /// The card's first row (its top padding); text rows follow at `top + 1`.
    pub first: usize,
    /// The card's last row (its bottom padding).
    pub last: usize,
    /// Text rows drawn (<= MAX_ROWS), between the two padding rows.
    pub body: usize,
    /// Rows the pinned copy has in full (`body` plus both padding rows) and how many fit
    /// before the next card.
    pub rows: usize,
    pub shown: usize,
    /// More text than fits: the last row ends in an ellipsis.
    pub clipped: bool,
}

/// The pinned card for the viewport starting at `offset`. `cards` are `(top, rows)` in order.
pub fn select(cards: &[(usize, usize)], offset: usize, total: usize) -> Option<Pin> {
    if offset >= total {
        return None;
    }
    // The title row (top + 1) is the first row that matters: once it is above the viewport the
    // real message is gone.
    let card = cards.iter().rposition(|&(top, _)| top + 1 < offset)?;
    let (top, height) = cards[card];
    // Do not overlay a truncated copy while the real card still occupies the viewport.
    // In particular, its attachment chips must never become a detached second band.
    if offset < top.saturating_add(height) {
        return None;
    }
    let body = height.saturating_sub(2).max(1);
    let body_rows = body.min(MAX_ROWS);
    let rows = body_rows + 2;
    // The next card pushes the pin up as it arrives, then replaces it.
    let room = cards
        .get(card + 1)
        .map_or(usize::MAX, |&(next, _)| next.saturating_sub(offset));
    let shown = rows.min(room);
    if shown == 0 {
        return None;
    }
    Some(Pin {
        card,
        first: top,
        last: top + height.saturating_sub(1),
        body: body_rows,
        rows,
        shown,
        clipped: body > MAX_ROWS,
    })
}

/// Replace the last visible character of the row with an ellipsis.
pub fn ellipsize(mut line: Line<'static>) -> Line<'static> {
    for span in line.spans.iter_mut().rev() {
        let text = span.content.to_string();
        let trimmed = text.trim_end();
        if let Some((at, ch)) = trimmed.char_indices().last() {
            let mut out = text[..at].to_string();
            out.push('…');
            let pad = text.width().saturating_sub(out.width());
            out.push_str(&" ".repeat(pad));
            let _ = ch;
            *span = Span::styled(out, span.style);
            break;
        }
    }
    line
}

/// A quiet dotted padding row distinguishes a pinned continuation from the real card.
pub fn continuation_padding(mut line: Line<'static>, colour: Color) -> Line<'static> {
    let mut column = 0;
    for span in &mut line.spans {
        let width = span.content.width();
        let text: String = (column..column + width)
            .map(|x| if x % 4 == 2 { '·' } else { ' ' })
            .collect();
        *span = Span::styled(text, span.style.fg(colour));
        column += width;
    }
    line
}

#[cfg(test)]
mod tests {
    use super::*;

    const CARDS: [(usize, usize); 2] = [(0, 5), (20, 5)];

    #[test]
    fn at_the_top_or_with_the_message_visible_there_is_no_pin() {
        assert_eq!(select(&CARDS, 0, 40), None);
        assert_eq!(select(&CARDS, 1, 40), None, "title row still on screen");
    }

    #[test]
    fn scrolled_within_a_turn_pins_its_user_message() {
        let pin = select(&CARDS, 6, 40).unwrap();
        assert_eq!(
            (pin.card, pin.first, pin.body, pin.rows, pin.shown),
            (0, 0, 3, 5, 5)
        );
        assert_eq!(pin.last, 4);
        assert!(!pin.clipped);
        assert_eq!(select(&CARDS, 2, 40), None, "real card is still visible");
    }

    #[test]
    fn the_next_turn_pushes_out_then_replaces_the_pin() {
        assert_eq!(select(&CARDS, 18, 40).unwrap().shown, 2, "pushed up");
        assert_eq!(
            select(&CARDS, 20, 40),
            None,
            "next card's top row is at the top"
        );
        assert_eq!(select(&CARDS, 21, 40), None);
        assert_eq!(select(&CARDS, 22, 40), None);
        assert_eq!(select(&CARDS, 25, 40).unwrap().card, 1);
    }

    #[test]
    fn long_messages_are_capped_at_three_rows_with_an_ellipsis() {
        assert_eq!(select(&[(0, 12)], 5, 30), None);
        let pin = select(&[(0, 12)], 12, 30).unwrap();
        assert_eq!((pin.body, pin.rows, pin.clipped), (3, 5, true));
        assert_eq!(pin.last, 11, "bottom padding comes from the whole card");
        let line = ellipsize(Line::from(vec![Span::raw("   ❯ hello   ")]));
        assert_eq!(line.spans[0].content, "   ❯ hell…   ");
        let single = select(&[(0, 3)], 5, 30).unwrap();
        assert_eq!((single.body, single.clipped), (1, false));
    }

    #[test]
    fn continuation_padding_keeps_width_and_background() {
        let line = Line::from(Span::styled(
            "            ",
            ratatui::style::Style::default().bg(Color::Black),
        ));
        let line = continuation_padding(line, Color::DarkGray);
        assert_eq!(line.spans[0].content, "  ·   ·   · ");
        assert_eq!(line.spans[0].style.bg, Some(Color::Black));
        assert_eq!(line.spans[0].style.fg, Some(Color::DarkGray));
    }

    #[test]
    fn attachment_tail_is_not_split_from_a_partly_visible_card() {
        let cards = [(0, 12)];
        for offset in 2..12 {
            assert_eq!(select(&cards, offset, 30), None, "offset {offset}");
        }
        assert!(select(&cards, 12, 30).is_some());
    }
}

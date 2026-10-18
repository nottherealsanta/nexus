//! Sticky user message: while a turn's content is on screen but its user card has scrolled
//! off the top, the card's first rows stay pinned over the top of the transcript.
//!
//! Pure selection over `(top, rows)` card spans (a card is a padding row, the title and text
//! rows, a padding row); the draw loop in `render.rs` paints the rows and records the hit
//! rectangle so a click scrolls back to the real message. Only the pinned copy's bottom padding
//! row is replaced, by the STK-3 indicator (`indicator`, step 14 of the design overhaul): a
//! shade with a right-aligned count of the turn's rows above the viewport (`above`).
use ratatui::{
    style::{Color, Style},
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

/// Rows of the pinned turn that are above the viewport, below its own card: the first row of
/// the viewport (`offset`) minus the end of the card (`pin.last`). `select` guarantees the
/// whole card is above the viewport, so this is never negative.
pub fn above(pin: &Pin, offset: usize) -> usize {
    offset.saturating_sub(pin.last + 1)
}

/// `↑ N lines of this turn above` (STK-3), shortened for narrow rows; empty when even the
/// short form leaves fewer than two cells of shade.
fn indicator_label(above: usize, width: usize, ascii: bool) -> String {
    let arrow = if ascii { '^' } else { '↑' };
    let noun = if above == 1 { "line" } else { "lines" };
    [
        format!(" {arrow} {above} {noun} of this turn above "),
        format!(" {arrow} {above} above "),
    ]
    .into_iter()
    .find(|label| label.width() + 2 <= width)
    .unwrap_or_default()
}

/// STK-3: the pinned copy's bottom padding row becomes a shade (`░`, `.` in ASCII) in `shade`
/// with ` ↑ N lines of this turn above ` right-aligned in `label`. Only this row changes; it
/// keeps its width and the card's background, so the real card (whose padding is plain) can
/// never be mistaken for the pin.
pub fn indicator(
    line: Line<'static>,
    above: usize,
    shade: Color,
    label: Color,
    ascii: bool,
) -> Line<'static> {
    let width: usize = line.spans.iter().map(|span| span.content.width()).sum();
    let base = line.spans.first().map_or(Style::default(), |s| s.style);
    let text = indicator_label(above, width, ascii);
    let glyph = if ascii { '.' } else { '░' };
    let mut spans = vec![Span::styled(
        glyph.to_string().repeat(width - text.width()),
        base.fg(shade),
    )];
    if !text.is_empty() {
        spans.push(Span::styled(text, base.fg(label)));
    }
    Line::from(spans)
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

    fn padding(width: usize) -> Line<'static> {
        Line::from(Span::styled(
            " ".repeat(width),
            Style::default().bg(Color::Black),
        ))
    }
    fn text(line: &Line) -> String {
        line.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    #[test]
    fn indicator_is_a_shade_with_a_right_aligned_count_and_keeps_width_and_background() {
        let line = indicator(padding(60), 12, Color::DarkGray, Color::Gray, false);
        let drawn = text(&line);
        assert_eq!(drawn.width(), 60);
        assert!(drawn.starts_with("░░░") && drawn.ends_with(" ↑ 12 lines of this turn above "));
        assert_eq!(line.spans[0].style.fg, Some(Color::DarkGray));
        assert_eq!(line.spans[1].style.fg, Some(Color::Gray));
        assert!(line.spans.iter().all(|s| s.style.bg == Some(Color::Black)));
        assert!(
            text(&indicator(padding(60), 1, Color::Gray, Color::Gray, false))
                .ends_with(" ↑ 1 line of this turn above ")
        );
    }

    #[test]
    fn indicator_fits_widths_20_to_200_and_degrades_gracefully() {
        for width in 20..=200 {
            for above in [0, 1, 7, 12_345] {
                let drawn = text(&indicator(
                    padding(width),
                    above,
                    Color::Gray,
                    Color::Gray,
                    false,
                ));
                assert_eq!(drawn.width(), width, "{width} / {above}");
                assert!(drawn.starts_with("░░"), "{width} / {above}: {drawn:?}");
            }
        }
        let short = text(&indicator(padding(24), 7, Color::Gray, Color::Gray, false));
        assert!(short.ends_with(" ↑ 7 above "), "{short:?}");
        let bare = text(&indicator(padding(8), 7, Color::Gray, Color::Gray, false));
        assert_eq!(bare, "░".repeat(8), "too narrow for any count");
        assert_eq!(
            text(&indicator(padding(0), 7, Color::Gray, Color::Gray, false)),
            ""
        );
    }

    #[test]
    fn indicator_has_an_ascii_form() {
        let drawn = text(&indicator(padding(50), 3, Color::Gray, Color::Gray, true));
        assert!(drawn.starts_with("....") && drawn.ends_with(" ^ 3 lines of this turn above "));
        assert!(drawn.is_ascii());
    }

    #[test]
    fn lines_above_count_the_turn_rows_between_the_card_and_the_viewport() {
        let cards = [(0, 5), (60, 5)];
        for (offset, expected) in [(5, 0), (6, 1), (12, 7), (40, 35), (58, 53)] {
            let pin = select(&cards, offset, 100).unwrap();
            assert_eq!(above(&pin, offset), expected, "offset {offset}");
        }
        // A card clipped to three text rows still counts from its real end.
        let pin = select(&[(0, 12)], 20, 100).unwrap();
        assert_eq!(above(&pin, 20), 8);
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

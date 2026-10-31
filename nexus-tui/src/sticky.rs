//! The sticky user header (DESIGN_PLAN.md D04, from old Nexus `sticky.rs`):
//! once a turn's user band has scrolled off the top while its content is still
//! on screen, a copy of the band's first rows stays pinned under the header.
//!
//! This module is the pure part: which band to pin and how much of it shows
//! (`select`), the `…` for a long message (`ellipsize`), the shade row with its
//! `↑ N lines of this turn above` count (`shade`) and that count (`above`).
//! `render::draw_timeline` paints the rows and records the click target that
//! scrolls back to the real band.

use ratatui::text::Span;

use crate::text::width;

/// Most text rows the pinned copy may take.
pub const MAX_ROWS: usize = 3;

#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub struct Pin {
    /// Index into the band list of the pinned user message.
    pub card: usize,
    /// The band's first row (its top padding); text rows follow at `first + 1`.
    pub first: usize,
    /// The band's last row (its bottom padding).
    pub last: usize,
    /// Text rows drawn (at most `MAX_ROWS`).
    pub body: usize,
    /// Rows the pinned copy has in full (`body` text rows and the shade row) and
    /// how many of them fit before the next band pushes it up.
    pub rows: usize,
    pub shown: usize,
    /// More text than fits: the last text row ends in an ellipsis.
    pub clipped: bool,
}

/// The pinned band for a viewport starting at line `offset`. `cards` are the
/// open user bands as `(top, rows, text rows)`, in order: a padding row, the
/// text rows, any preview rows and a padding row.
pub fn select(cards: &[(usize, usize, usize)], offset: usize, total: usize) -> Option<Pin> {
    if offset >= total {
        return None;
    }
    // The first text row (top + 1) is the row that matters: once it is above the
    // viewport the real message is gone.
    let card = cards.iter().rposition(|&(top, _, _)| top + 1 < offset)?;
    let (top, height, body) = cards[card];
    // Never overlay a copy while the real band still occupies the viewport.
    if offset < top.saturating_add(height) {
        return None;
    }
    let body_rows = body.min(MAX_ROWS);
    let rows = body_rows + 1;
    // The next band pushes the pin up as it arrives, then replaces it.
    let room = cards
        .get(card + 1)
        .map_or(usize::MAX, |&(next, _, _)| next.saturating_sub(offset));
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

/// Rows of the pinned turn that are above the viewport and below its band.
pub fn above(pin: &Pin, offset: usize) -> usize {
    offset.saturating_sub(pin.last + 1)
}

/// Replace the last visible character of a text row with an ellipsis. The
/// spans of a band row are a left margin, the fold marker and the text.
pub fn ellipsize(spans: &mut [Span<'static>], ellipsis: &str) {
    for span in spans.iter_mut().rev() {
        let text = span.content.to_string();
        let trimmed = text.trim_end();
        if let Some((at, _)) = trimmed.char_indices().last() {
            let mut out = text[..at].to_owned();
            out.push_str(ellipsis);
            let pad = width(&text).saturating_sub(width(&out));
            out.push_str(&" ".repeat(pad));
            span.content = out.into();
            break;
        }
    }
}

/// `↑ 34 lines of this turn above`, shortened for a narrow row; empty when even
/// the short form leaves fewer than two cells of shade.
fn label(above: usize, room: usize, ascii: bool) -> String {
    let arrow = if ascii { '^' } else { '↑' };
    let noun = if above == 1 { "line" } else { "lines" };
    [
        format!(" {arrow} {above} {noun} of this turn above "),
        format!(" {arrow} {above} above "),
    ]
    .into_iter()
    .find(|text| width(text) + 2 <= room)
    .unwrap_or_default()
}

/// The shade row of the pinned copy: `░` (`.` in ASCII) over `cells` cells with
/// the count right-aligned. Returns `(shade, label)`; both together fill the row.
pub fn shade(above: usize, cells: usize, ascii: bool) -> (String, String) {
    let text = label(above, cells, ascii);
    let glyph = if ascii { "." } else { "░" };
    (glyph.repeat(cells.saturating_sub(width(&text))), text)
}

#[cfg(test)]
mod tests {
    use super::*;

    const CARDS: [(usize, usize, usize); 2] = [(0, 5, 3), (20, 5, 3)];

    #[test]
    fn nothing_is_pinned_at_the_top_or_while_the_band_is_on_screen() {
        assert_eq!(select(&CARDS, 0, 40), None);
        assert_eq!(
            select(&CARDS, 1, 40),
            None,
            "first text row still on screen"
        );
        assert_eq!(select(&CARDS, 2, 40), None, "real band still visible");
        assert_eq!(select(&CARDS, 40, 40), None, "past the end");
    }

    #[test]
    fn scrolled_within_a_turn_pins_its_band_with_a_shade_row() {
        let pin = select(&CARDS, 6, 40).unwrap();
        assert_eq!((pin.card, pin.first, pin.last), (0, 0, 4));
        assert_eq!((pin.body, pin.rows, pin.shown), (3, 4, 4));
        assert!(!pin.clipped);
        assert_eq!(above(&pin, 6), 1);
        assert_eq!(above(&pin, 19), 14);
    }

    #[test]
    fn the_next_band_pushes_the_pin_up_then_replaces_it() {
        assert_eq!(select(&CARDS, 17, 40).unwrap().shown, 3, "pushed up");
        assert_eq!(select(&CARDS, 19, 40).unwrap().shown, 1);
        assert_eq!(select(&CARDS, 20, 40), None, "next band is at the top");
        assert_eq!(select(&CARDS, 22, 40), None, "next band is on screen");
        assert_eq!(select(&CARDS, 25, 40).unwrap().card, 1);
    }

    #[test]
    fn long_messages_take_three_rows_and_end_in_an_ellipsis() {
        assert_eq!(select(&[(0, 12, 10)], 5, 30), None);
        let pin = select(&[(0, 12, 10)], 12, 30).unwrap();
        assert_eq!((pin.body, pin.rows, pin.clipped), (3, 4, true));
        assert_eq!(pin.last, 11, "the count starts after the whole band");
        let mut spans = vec![Span::raw(" "), Span::raw("▾"), Span::raw(" hello   ")];
        ellipsize(&mut spans, "…");
        assert_eq!(spans[2].content, " hell…   ");
        let single = select(&[(0, 3, 1)], 5, 30).unwrap();
        assert_eq!((single.body, single.clipped), (1, false));
    }

    #[test]
    fn the_shade_row_fills_every_width_and_degrades() {
        for cells in 0..=200 {
            for above in [0, 1, 7, 12_345] {
                let (shade, label) = shade(above, cells, false);
                assert_eq!(width(&shade) + width(&label), cells, "{cells}/{above}");
            }
        }
        let (shade, label) = shade(34, 60, false);
        assert!(shade.starts_with("░░") && label == " ↑ 34 lines of this turn above ");
        assert_eq!(
            super::shade(1, 60, false).1,
            " ↑ 1 line of this turn above "
        );
        assert_eq!(super::shade(7, 24, false).1, " ↑ 7 above ");
        assert_eq!(super::shade(7, 8, false).1, "", "too narrow for a count");
        let (shade, label) = super::shade(3, 50, true);
        assert!(shade.starts_with("....") && label.is_ascii() && label.contains('^'));
    }
}

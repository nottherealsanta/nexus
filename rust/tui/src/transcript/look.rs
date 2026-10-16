//! Shared look of the activity rows: glyph set, time format and the live-run marker.
//!
//! Transcript rows are built once and cached, so they carry no tick. Spans that should
//! pulse while something runs are tagged with [`live`]; the draw loop (which owns the
//! frame clock) recolours exactly those spans with [`animate_row`]. Nothing here reads
//! the colour theme or the animation preference: the caller decides whether to animate.
use nexus_widgets::{anim, theme::mix, Glyphs};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
use std::sync::OnceLock;
use unicode_segmentation::UnicodeSegmentation;

/// The spinner slot the draw loop replaces with the current frame (`render::SPINNER_SLOT`).
pub const SLOT: &str = "\u{e000}";

pub fn glyphs() -> &'static Glyphs {
    static GLYPHS: OnceLock<Glyphs> = OnceLock::new();
    GLYPHS.get_or_init(Glyphs::from_env)
}

/// First character of the end-of-tree glyph: `╰` (`` ` `` in ASCII).
pub fn corner_end() -> &'static str {
    let tree = glyphs().tree_end;
    &tree[..tree.chars().next().map_or(0, char::len_utf8)]
}

/// Top corner of an expanded member: `┌` (`+` in ASCII).
pub fn corner_top() -> &'static str {
    if glyphs().ascii {
        "+"
    } else {
        "┌"
    }
}

/// Grok's `format_time`: `671ms`, `3.2s`, `1m20s`. Mirrors `verb_groups.format_time`.
pub fn format_time(ms: u64) -> String {
    if ms < 1000 {
        format!("{ms}ms")
    } else if ms < 60_000 {
        format!("{:.1}s", ms as f64 / 1000.0)
    } else {
        format!("{}m{:02}s", ms / 60_000, (ms / 1000) % 60)
    }
}

/// Wall clock in epoch milliseconds, the unit of the projection's `started_ms`.
pub fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_millis() as u64)
}

/// Elapsed time of a run that began at `started_ms`, or `None` when the start is unknown.
pub fn live_elapsed(started_ms: u64, now_ms: u64) -> Option<String> {
    (started_ms > 0 && now_ms >= started_ms).then(|| format_time(now_ms - started_ms))
}

/// Tag a style as "animate me". The tag is `sub_modifier(HIDDEN)`: removing a modifier
/// nobody sets is invisible in the terminal, and a plain `Style` never carries it.
pub fn live(style: Style) -> Style {
    style.remove_modifier(Modifier::HIDDEN)
}

pub fn is_live(span: &Span<'_>) -> bool {
    span.style.sub_modifier.contains(Modifier::HIDDEN)
}

/// Recolour the [`live`] spans of one drawn row with the wave: a single-grapheme span
/// (a rail or bullet) follows the row, longer text shimmers per column. Returns the
/// row unchanged when it has nothing live. Pure: the caller passes `tick`.
pub fn animate_row(
    line: &Line<'static>,
    tick: u64,
    row: u16,
    base: Color,
    peak: Color,
) -> Line<'static> {
    if !line.spans.iter().any(is_live) {
        return line.clone();
    }
    let mut spans = Vec::with_capacity(line.spans.len());
    for span in &line.spans {
        if !is_live(span) {
            spans.push(span.clone());
            continue;
        }
        let graphemes: Vec<&str> = span.content.graphemes(true).collect();
        if graphemes.len() <= 1 {
            let amount = anim::wave(tick, row, anim::WAVE_ROWS, anim::WAVE_SPEED);
            spans.push(Span::styled(
                span.content.clone(),
                span.style.fg(mix(base, peak, amount)),
            ));
            continue;
        }
        for (column, grapheme) in graphemes.into_iter().enumerate() {
            let column = (column % anim::WAVE_ROWS as usize) as u16;
            let amount = anim::wave(tick, column, anim::WAVE_ROWS, anim::WAVE_SPEED);
            spans.push(Span::styled(
                grapheme.to_string(),
                span.style.fg(mix(base, peak, amount)),
            ));
        }
    }
    let mut animated = line.clone();
    animated.spans = spans;
    animated
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn time_matches_the_projection_format() {
        assert_eq!(format_time(671), "671ms");
        assert_eq!(format_time(3_200), "3.2s");
        assert_eq!(format_time(80_000), "1m20s");
        assert_eq!(format_time(61_000), "1m01s");
    }

    #[test]
    fn live_elapsed_needs_a_known_start() {
        assert_eq!(live_elapsed(0, 5_000), None);
        assert_eq!(live_elapsed(6_000, 5_000), None);
        assert_eq!(live_elapsed(1_000, 3_400).as_deref(), Some("2.4s"));
    }

    #[test]
    fn only_live_spans_are_recoloured_and_text_is_kept() {
        let still = Span::styled("fixed", Style::default().fg(Color::Rgb(1, 2, 3)));
        let moving = Span::styled("Thinking…", live(Style::default()));
        let rail = Span::styled("┃", live(Style::default()));
        let line = Line::from(vec![still.clone(), moving, rail]);
        let (base, peak) = (Color::Rgb(10, 10, 10), Color::Rgb(250, 250, 250));
        let drawn = animate_row(&line, 7, 3, base, peak);
        assert_eq!(drawn.spans[0], still);
        assert_eq!(drawn.to_string(), line.to_string());
        // One span per grapheme of the shimmering label, one for the rail.
        assert_eq!(
            drawn.spans.len(),
            1 + "Thinking…".graphemes(true).count() + 1
        );
        assert!(drawn.spans[1..].iter().all(|span| span.style.fg.is_some()));
        let plain = Line::from("no motion");
        assert_eq!(animate_row(&plain, 7, 3, base, peak), plain);
    }

    #[test]
    fn corners_are_single_cells() {
        assert_eq!(unicode_width::UnicodeWidthStr::width(corner_end()), 1);
        assert_eq!(unicode_width::UnicodeWidthStr::width(corner_top()), 1);
    }
}

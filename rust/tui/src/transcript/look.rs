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
    } else if ms >= 3_600_000 {
        format!("{}h {}m", ms / 3_600_000, (ms / 60_000) % 60)
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

/// Graphemes of a streaming message that glow (step 4, STR-3).
pub const HEAD_LEN: usize = 12;

/// Tag a style as "write head". Like [`live`] it is a removed modifier nobody sets
/// (`RAPID_BLINK`), so a plain `Style` never carries it. The tag lets the draw loop put
/// the glow back to the base colour when animation is off.
fn head(style: Style) -> Style {
    style.remove_modifier(Modifier::RAPID_BLINK)
}

pub fn is_head(span: &Span<'_>) -> bool {
    span.style.sub_modifier.contains(Modifier::RAPID_BLINK)
}

/// Blend the last `count` graphemes of a streaming message from `base` (the `count`-th
/// from the end) to `peak` (the newest). Rows are visited newest first by the caller:
/// `skip` leading spans (indent, list marker) are left alone, `seen` is how many
/// graphemes the later rows already took, and the new total is returned so a short last
/// row spills into the row above. Only spans coloured `base` (or with no colour) glow, so
/// inline code and links keep their own colours but still use up their share of the head.
/// Glowing graphemes are split into single-grapheme spans and tagged for [`is_head`].
pub fn write_head(
    line: &mut Line<'static>,
    skip: usize,
    mut seen: usize,
    count: usize,
    base: Color,
    peak: Color,
) -> usize {
    if seen >= count || skip >= line.spans.len() {
        return seen;
    }
    let body = line.spans.split_off(skip);
    let mut rebuilt: Vec<Span<'static>> = Vec::new(); // newest first
    for span in body.into_iter().rev() {
        let graphemes: Vec<&str> = span.content.graphemes(true).collect();
        let take = count.saturating_sub(seen).min(graphemes.len());
        if take == 0 {
            rebuilt.push(span);
            continue;
        }
        let plain = span.style.fg.is_none() || span.style.fg == Some(base);
        if !plain {
            // Code and links keep their colour and stay one span; they still spend their share.
            seen += take;
            rebuilt.push(span);
            continue;
        }
        let split = graphemes.len() - take;
        for (index, grapheme) in graphemes.iter().enumerate().skip(split).rev() {
            let rank = seen + (graphemes.len() - 1 - index);
            let amount = if count > 1 {
                1.0 - rank as f32 / (count - 1) as f32
            } else {
                1.0
            };
            rebuilt.push(Span::styled(
                grapheme.to_string(),
                head(span.style.fg(mix(base, peak, amount))),
            ));
        }
        seen += take;
        if split > 0 {
            rebuilt.push(Span::styled(graphemes[..split].concat(), span.style));
        }
    }
    rebuilt.reverse();
    line.spans.extend(rebuilt);
    seen
}

/// Put every [`is_head`] span of a drawn row back to `base` (animation off or monochrome).
pub fn cool_head(line: &Line<'static>, base: Color) -> Line<'static> {
    if !line.spans.iter().any(is_head) {
        return line.clone();
    }
    let mut cooled = line.clone();
    for span in cooled.spans.iter_mut().filter(|span| is_head(span)) {
        span.style.fg = Some(base);
    }
    cooled
}

#[cfg(test)]
mod tests {
    use super::*;

    fn glowing(text: &str, count: usize) -> Line<'static> {
        let mut line = Line::from(vec![
            Span::raw("    "),
            Span::styled(text.to_string(), Style::default().fg(Color::Rgb(0, 0, 0))),
        ]);
        write_head(
            &mut line,
            1,
            0,
            count,
            Color::Rgb(0, 0, 0),
            Color::Rgb(200, 100, 0),
        );
        line
    }

    #[test]
    fn write_head_blends_from_base_to_peak_and_keeps_the_text() {
        let line = glowing("hello world, again", 12);
        assert_eq!(line.to_string(), "    hello world, again");
        let tail: Vec<_> = line.spans.iter().filter(|s| is_head(s)).collect();
        assert_eq!(tail.len(), 12);
        assert_eq!(
            tail[0].style.fg,
            Some(Color::Rgb(0, 0, 0)),
            "12th from the end is base"
        );
        assert_eq!(
            tail[11].style.fg,
            Some(Color::Rgb(200, 100, 0)),
            "the newest is peak"
        );
        // The prefix stays one calm span.
        assert_eq!(line.spans[1].content, "hello ");
        assert!(!is_head(&line.spans[1]));
    }

    #[test]
    fn write_head_spills_into_the_row_above_and_stops_at_the_count() {
        let (base, peak) = (Color::Rgb(0, 0, 0), Color::Rgb(255, 255, 255));
        let mut above = Line::from(vec![Span::styled(
            "one two three",
            Style::default().fg(base),
        )]);
        let mut last = Line::from(vec![Span::styled("four", Style::default().fg(base))]);
        let seen = write_head(&mut last, 0, 0, 12, base, peak);
        assert_eq!(seen, 4);
        let seen = write_head(&mut above, 0, seen, 12, base, peak);
        assert_eq!(seen, 12);
        let glow = |line: &Line<'static>| line.spans.iter().filter(|s| is_head(s)).count();
        assert_eq!((glow(&last), glow(&above)), (4, 8));
        assert_eq!(above.to_string(), "one two three");
        // Done: later rows are left untouched.
        let mut more = Line::from("untouched");
        assert_eq!(write_head(&mut more, 0, 12, 12, base, peak), 12);
        assert_eq!(more.spans.len(), 1);
    }

    #[test]
    fn write_head_is_grapheme_safe_and_leaves_styled_spans_alone() {
        let (base, peak) = (Color::Rgb(0, 0, 0), Color::Rgb(255, 255, 255));
        let code = Style::default().fg(Color::Rgb(9, 9, 9));
        let mut line = Line::from(vec![
            Span::styled("日本語👍🏽e\u{301}", Style::default().fg(base)),
            Span::styled("code", code),
        ]);
        write_head(&mut line, 0, 0, 12, base, peak);
        assert_eq!(line.to_string(), "日本語👍🏽e\u{301}code");
        let heads: Vec<_> = line.spans.iter().filter(|s| is_head(s)).collect();
        // "code" spends 4 of the 12 and keeps its colour; all 5 graphemes before it glow.
        assert_eq!(heads.len(), 5);
        assert!(line
            .spans
            .iter()
            .any(|s| s.content == "code" && s.style == code));
        assert!(heads.iter().all(|s| s.content.graphemes(true).count() == 1));
    }

    #[test]
    fn cool_head_restores_the_base_colour() {
        let base = Color::Rgb(0, 0, 0);
        let line = glowing("abc", 12);
        assert!(line.spans.iter().any(is_head));
        let cooled = cool_head(&line, base);
        assert!(cooled
            .spans
            .iter()
            .filter(|s| is_head(s))
            .all(|s| s.style.fg == Some(base)));
        let plain = Line::from("x");
        assert_eq!(cool_head(&plain, base), plain);
    }

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

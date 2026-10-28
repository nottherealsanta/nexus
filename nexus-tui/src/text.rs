//! Text helpers measured in terminal cells (Unicode display widths).

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

pub fn width(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

pub fn cw(c: char) -> usize {
    UnicodeWidthChar::width(c).unwrap_or(0)
}

/// Uppercase the first grapheme only (agent names: `orchestrate` → `Orchestrate`).
pub fn capitalize(s: &str) -> String {
    match s.graphemes(true).next() {
        Some(first) => first.to_uppercase() + &s[first.len()..],
        None => String::new(),
    }
}

/// Truncate to `max` cells, ending with `ellipsis` when cut (never splits a
/// wide character).
pub fn truncate(s: &str, max: usize, ellipsis: &str) -> String {
    if width(s) <= max {
        return s.to_owned();
    }
    let e = width(ellipsis);
    if max <= e {
        return ellipsis.chars().take(max).collect();
    }
    let mut out = String::new();
    let mut w = 0;
    for grapheme in s.graphemes(true) {
        let cell_width = width(grapheme);
        if w + cell_width > max - e {
            break;
        }
        out.push_str(grapheme);
        w += cell_width;
    }
    out.push_str(ellipsis);
    out
}

/// Truncate the start (for breadcrumbs: `… › child`).
pub fn truncate_start(s: &str, max: usize, ellipsis: &str) -> String {
    if width(s) <= max {
        return s.to_owned();
    }
    let e = width(ellipsis);
    if max <= e {
        return ellipsis.chars().take(max).collect();
    }
    let mut keep: Vec<char> = Vec::new();
    let mut w = 0;
    for c in s.chars().rev() {
        let c_w = cw(c);
        if w + c_w > max - e {
            break;
        }
        keep.push(c);
        w += c_w;
    }
    keep.reverse();
    format!("{ellipsis}{}", keep.into_iter().collect::<String>())
}

/// Truncate the middle (paths).
pub fn truncate_middle(s: &str, max: usize, ellipsis: &str) -> String {
    if width(s) <= max {
        return s.to_owned();
    }
    let e = width(ellipsis);
    if max <= e + 1 {
        return truncate(s, max, ellipsis);
    }
    let head_w = (max - e).div_ceil(2);
    let tail_w = max - e - head_w;
    let mut head = String::new();
    let mut w = 0;
    for c in s.chars() {
        if w + cw(c) > head_w {
            break;
        }
        head.push(c);
        w += cw(c);
    }
    let mut tail: Vec<char> = Vec::new();
    w = 0;
    for c in s.chars().rev() {
        if w + cw(c) > tail_w {
            break;
        }
        tail.push(c);
        w += cw(c);
    }
    tail.reverse();
    format!("{head}{ellipsis}{}", tail.into_iter().collect::<String>())
}

/// Pad with spaces to exactly `w` cells (truncating is the caller's job).
pub fn pad_right(s: &str, w: usize) -> String {
    let cur = width(s);
    format!("{s}{}", " ".repeat(w.saturating_sub(cur)))
}

pub fn pad_left(s: &str, w: usize) -> String {
    let cur = width(s);
    format!("{}{s}", " ".repeat(w.saturating_sub(cur)))
}

/// Hard-wrap one logical line at `w` cells, preferring spaces. Always returns
/// at least one (possibly empty) row. Every row is at most `w` cells.
pub fn wrap_line(line: &str, w: usize) -> Vec<String> {
    let w = w.max(1);
    if width(line) <= w {
        return vec![line.to_owned()];
    }
    let mut rows = Vec::new();
    let mut cur = String::new();
    let mut cur_w = 0;
    let mut last_space: Option<(usize, usize)> = None; // (byte index in cur, width before)
    for grapheme in line.graphemes(true) {
        let c_w = width(grapheme);
        if cur_w + c_w > w {
            if grapheme == " " {
                // the line is exactly full: break here and drop the space
                rows.push(std::mem::take(&mut cur));
                cur_w = 0;
                last_space = None;
                continue;
            }
            if let Some((at, _)) = last_space {
                let rest = cur[at..].trim_start_matches(' ').to_owned();
                cur.truncate(at);
                rows.push(std::mem::take(&mut cur));
                cur = rest;
                cur_w = width(&cur);
            } else {
                rows.push(std::mem::take(&mut cur));
                cur_w = 0;
            }
            last_space = None;
            if grapheme == " " && cur.is_empty() {
                continue;
            }
        }
        if grapheme == " " && !cur.is_empty() {
            last_space = Some((cur.len(), cur_w));
        }
        cur.push_str(grapheme);
        cur_w += c_w;
    }
    rows.push(cur);
    rows
}

/// Wrap text with embedded newlines.
pub fn wrap(text: &str, w: usize) -> Vec<String> {
    text.split('\n')
        .flat_map(|l| wrap_line(l.trim_end_matches('\r'), w))
        .collect()
}

/// `1.2k` style counts.
pub fn count(n: u64) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1_000_000.0)
    } else if n >= 1000 {
        format!("{:.1}k", n as f64 / 1000.0)
    } else {
        n.to_string()
    }
}

/// Compact relative age: `now`, `3m`, `1h`, `2d`.
pub fn age(now: f64, then: f64) -> String {
    let s = (now - then).max(0.0) as u64;
    match s {
        0..=44 => "now".into(),
        45..=3599 => format!("{}m", (s / 60).max(1)),
        3600..=86_399 => format!("{}h", s / 3600),
        _ => format!("{}d", s / 86_400),
    }
}

/// `just now`, `3m ago`, `2h ago`.
pub fn ago(now: f64, then: f64) -> String {
    match age(now, then).as_str() {
        "now" => "just now".into(),
        a => format!("{a} ago"),
    }
}

pub fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let n = match chunk.len() {
            3 => (chunk[0] as u32) << 16 | (chunk[1] as u32) << 8 | chunk[2] as u32,
            2 => (chunk[0] as u32) << 16 | (chunk[1] as u32) << 8,
            _ => (chunk[0] as u32) << 16,
        };
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            T[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            T[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn widths_count_cells() {
        assert_eq!(width("abc"), 3);
        assert_eq!(width("日本"), 4);
        assert_eq!(width("a◧b"), 3);
    }

    #[test]
    fn truncation_never_splits_wide_chars() {
        assert_eq!(truncate("日本語テキスト", 7, "…"), "日本語…");
        assert_eq!(width(&truncate("日本語テキスト", 6, "…")), 5);
        assert_eq!(truncate("short", 10, "…"), "short");
        assert_eq!(truncate("abcdef", 4, "..."), "a...");
        assert_eq!(truncate_start("root › child", 7, "…"), "… child");
        assert_eq!(
            truncate_middle("/work/nexus/src/lib.rs", 12, "…"),
            "/work/…ib.rs"
        );
    }

    #[test]
    fn wrapping_respects_width_and_words() {
        let rows = wrap("hello wide world 日本語", 10);
        assert!(rows.iter().all(|r| width(r) <= 10), "{rows:?}");
        assert_eq!(rows[0], "hello wide");
        assert_eq!(wrap("a\n\nb", 5), ["a", "", "b"]);
        assert_eq!(wrap_line("abcdefghij", 4), ["abcd", "efgh", "ij"]);
    }

    #[test]
    fn ages_and_counts() {
        assert_eq!(age(1000.0, 990.0), "now");
        assert_eq!(age(1000.0, 1000.0 - 180.0), "3m");
        assert_eq!(age(100_000.0, 100_000.0 - 7200.0), "2h");
        assert_eq!(count(1200), "1.2k");
        assert_eq!(count(1_200_000), "1.2M");
        assert_eq!(capitalize("orchestrate"), "Orchestrate");
        assert_eq!(capitalize("éclair"), "Éclair");
        assert_eq!(capitalize(""), "");
        assert_eq!(base64(b"Man"), "TWFu");
        assert_eq!(base64(b"Ma"), "TWE=");
    }
}

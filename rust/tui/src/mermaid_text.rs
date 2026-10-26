//! Legible text rendering of the Mermaid chart types a cell raster cannot show.
//!
//! A pie / Gantt / timeline SVG is hundreds of pixels wide, but a half-block
//! raster has about one pixel per terminal column, so its labels shrink below a
//! pixel and turn to noise. These types are instead rebuilt from the source as
//! text rows: a pie as a bar list with values and shares, a Gantt as a task grid
//! with a bar track and dates, a timeline as period/event rows. `None` means the
//! source is not understood or does not fit, and the caller shows the source.
use unicode_width::UnicodeWidthStr;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tone {
    Title,
    Heading,
    Label,
    Bar,
    Quiet,
}

pub type Segment = (String, Tone);
pub type Row = Vec<Segment>;

const MIN_BAR: usize = 8;

pub fn render(source: &str, width: usize) -> Option<Vec<Row>> {
    let mut lines = source
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with("%%"));
    let head = lines.next()?;
    let rest: Vec<&str> = lines.collect();
    let rows = match head.split_whitespace().next()? {
        "pie" => pie(head, &rest, width)?,
        "gantt" => gantt(&rest, width)?,
        "timeline" => timeline(&rest, width)?,
        _ => return None,
    };
    rows.iter()
        .all(|row| row_width(row) <= width)
        .then_some(rows)
}

pub fn row_width(row: &Row) -> usize {
    row.iter().map(|(s, _)| s.width()).sum()
}

fn fit(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_owned();
    }
    let mut out = String::new();
    for ch in text.chars() {
        if out.width() + ch.to_string().width() + 1 > width {
            break;
        }
        out.push(ch);
    }
    out.push('…');
    out
}

fn pad(text: &str, width: usize) -> String {
    let text = fit(text, width);
    let fill = width.saturating_sub(text.width());
    format!("{text}{}", " ".repeat(fill))
}

fn unquote(s: &str) -> &str {
    s.trim().trim_matches('"').trim()
}

fn pie(head: &str, rest: &[&str], width: usize) -> Option<Vec<Row>> {
    let mut title = head
        .split_once("title")
        .map(|(_, t)| t.trim().to_owned())
        .unwrap_or_default();
    let mut slices: Vec<(String, f64)> = Vec::new();
    for line in rest {
        if let Some(t) = line.strip_prefix("title ") {
            title = t.trim().to_owned();
        } else if let Some((label, value)) = line.rsplit_once(':') {
            let value: f64 = value
                .trim()
                .parse()
                .ok()
                .filter(|v: &f64| v.is_finite() && *v >= 0.0)?;
            slices.push((unquote(label).to_owned(), value));
        } else if !line.starts_with("showData") {
            return None;
        }
    }
    let total: f64 = slices.iter().map(|(_, v)| v).sum();
    if slices.is_empty() || total <= 0.0 {
        return None;
    }
    let values: Vec<String> = slices.iter().map(|(_, v)| format!("{v}")).collect();
    let vw = values.iter().map(|v| v.width()).max()?;
    let label_w = slices
        .iter()
        .map(|(l, _)| l.width())
        .max()?
        .min(width.saturating_sub(MIN_BAR + vw + 10).max(4));
    // label + gap + bar + gap + "100.0%" + gap + value
    let bar_w = width.checked_sub(label_w + vw + 10)?.min(30);
    if bar_w < MIN_BAR {
        return None;
    }
    let mut rows = Vec::new();
    if !title.is_empty() {
        rows.push(vec![(fit(&title, width), Tone::Title)]);
    }
    for ((label, value), text) in slices.iter().zip(&values) {
        let share = value / total;
        let filled = ((share * bar_w as f64).round() as usize).min(bar_w);
        rows.push(vec![
            (pad(label, label_w), Tone::Label),
            ("  ".into(), Tone::Quiet),
            ("█".repeat(filled), Tone::Bar),
            ("░".repeat(bar_w - filled), Tone::Quiet),
            (format!(" {:>5.1}%", share * 100.0), Tone::Label),
            (format!(" {text:>vw$}"), Tone::Quiet),
        ]);
    }
    Some(rows)
}

fn timeline(rest: &[&str], width: usize) -> Option<Vec<Row>> {
    let mut rows = Vec::new();
    let mut events = 0;
    for line in rest {
        if let Some(t) = line.strip_prefix("title ") {
            rows.push(vec![(fit(t.trim(), width), Tone::Title)]);
        } else if let Some(s) = line.strip_prefix("section ") {
            rows.push(vec![(fit(s.trim(), width), Tone::Heading)]);
        } else {
            let mut parts = line.split(':').map(str::trim);
            let period = parts.next().filter(|p| !p.is_empty())?;
            let period_w = period.width().min(width / 3).max(4);
            let mut first = true;
            for event in parts.filter(|e| !e.is_empty()) {
                let lead = if first {
                    pad(period, period_w)
                } else {
                    " ".repeat(period_w)
                };
                rows.push(vec![
                    (lead, Tone::Label),
                    (" │ ".into(), Tone::Quiet),
                    (fit(event, width.saturating_sub(period_w + 3)), Tone::Label),
                ]);
                first = false;
                events += 1;
            }
            if first {
                rows.push(vec![(fit(period, width), Tone::Label)]);
            }
        }
    }
    (events > 0 || rows.len() > 1).then_some(rows)
}

fn days(date: &str) -> Option<i64> {
    let mut it = date.split('-');
    let (y, m, d): (i64, i64, i64) = (
        it.next()?.parse().ok()?,
        it.next()?.parse().ok()?,
        it.next()?.parse().ok()?,
    );
    if it.next().is_some()
        || !(1..=9999).contains(&y)
        || !(1..=12).contains(&m)
        || !(1..=31).contains(&d)
    {
        return None;
    }
    // Howard Hinnant's days_from_civil.
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some(era * 146_097 + doe - 719_468)
}

fn civil(z: i64) -> String {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}")
}

fn duration(token: &str) -> Option<i64> {
    let unit = token.chars().last()?;
    // Bounded so date arithmetic below cannot overflow.
    let n: i64 = token[..token.len() - unit.len_utf8()]
        .parse()
        .ok()
        .filter(|n| (0..=1_000_000).contains(n))?;
    match unit {
        'w' => Some(n * 7),
        'd' => Some(n),
        _ => None,
    }
}

struct Task {
    name: String,
    start: i64,
    end: i64,
}

fn gantt(rest: &[&str], width: usize) -> Option<Vec<Row>> {
    let mut title = String::new();
    let mut tasks: Vec<Task> = Vec::new();
    // Section heading rows are placed before the first task that follows them.
    let mut sections: Vec<(usize, String)> = Vec::new();
    let mut ids: Vec<(String, i64)> = Vec::new();
    let mut last_end: Option<i64> = None;
    for line in rest {
        let keyword = line.split_whitespace().next().unwrap_or("");
        match keyword {
            "title" => title = line[5..].trim().to_owned(),
            "section" => sections.push((tasks.len(), line[7..].trim().to_owned())),
            "dateFormat" => {
                if line[10..].trim() != "YYYY-MM-DD" {
                    return None;
                }
            }
            "axisFormat" | "tickInterval" | "excludes" | "includes" | "todayMarker" | "weekday"
            | "weekend" | "inclusiveEndDates" | "topAxis" | "accTitle" | "accDescr" | "click" => {}
            _ => {
                let (name, spec) = line.split_once(':')?;
                let mut tokens: Vec<&str> = spec
                    .split(',')
                    .map(str::trim)
                    .filter(|t| !t.is_empty())
                    .collect();
                while tokens
                    .first()
                    .is_some_and(|t| matches!(*t, "done" | "active" | "crit" | "milestone"))
                {
                    tokens.remove(0);
                }
                let is_when = |t: &str| t.starts_with("after ") || days(t).is_some();
                let id =
                    (tokens.len() >= 2 && !is_when(tokens[0]) && duration(tokens[0]).is_none())
                        .then(|| tokens.remove(0).to_owned());
                let (start_tok, end_tok) = match tokens.as_slice() {
                    [end] => (None, *end),
                    [start, end] => (Some(*start), *end),
                    _ => return None,
                };
                let start = match start_tok {
                    Some(t) => match t.strip_prefix("after ") {
                        Some(dep) => ids.iter().rev().find(|(i, _)| i == dep.trim())?.1,
                        None => days(t)?,
                    },
                    None => last_end?,
                };
                let end = match days(end_tok) {
                    Some(d) => d,
                    None => start + duration(end_tok)?,
                };
                if end < start {
                    return None;
                }
                if let Some(id) = id {
                    ids.push((id, end));
                }
                last_end = Some(end);
                tasks.push(Task {
                    name: name.trim().to_owned(),
                    start,
                    end,
                });
            }
        }
    }
    let first = tasks.iter().map(|t| t.start).min()?;
    let last = tasks.iter().map(|t| t.end).max()?;
    let span = (last - first).max(1);
    let name_w = tasks
        .iter()
        .map(|t| t.name.width())
        .max()?
        .min((width / 3).max(6));
    let long_tail = 23; // "2024-01-01 → 2024-01-06"
    let bar_w = |tail: usize| width.checked_sub(name_w + 2 + tail + 2);
    let (tail_w, bar_w) = match bar_w(long_tail) {
        Some(b) if b >= 16 => (long_tail, b),
        _ => (5, bar_w(5).filter(|b| *b >= MIN_BAR)?),
    };
    let col = |d: i64| ((d - first) as f64 / span as f64 * bar_w as f64).round() as usize;
    let mut rows = Vec::new();
    if !title.is_empty() {
        rows.push(vec![(fit(&title, width), Tone::Title)]);
    }
    let axis =
        |text: String| -> Row { vec![(" ".repeat(name_w + 2), Tone::Quiet), (text, Tone::Quiet)] };
    let (a, b) = (civil(first), civil(last));
    if bar_w >= a.width() + b.width() + 1 {
        let gap = bar_w - a.width() - b.width();
        rows.push(axis(format!("{a}{}{b}", " ".repeat(gap))));
    }
    for (i, task) in tasks.iter().enumerate() {
        for (_, name) in sections.iter().filter(|(at, _)| *at == i) {
            rows.push(vec![(fit(name, width), Tone::Heading)]);
        }
        let x0 = col(task.start).min(bar_w - 1);
        let x1 = col(task.end).clamp(x0 + 1, bar_w);
        let tail = if tail_w == long_tail {
            format!("{} → {}", civil(task.start), civil(task.end))
        } else {
            format!("{}d", task.end - task.start)
        };
        rows.push(vec![
            (pad(&task.name, name_w), Tone::Label),
            ("  ".into(), Tone::Quiet),
            ("·".repeat(x0), Tone::Quiet),
            ("█".repeat(x1 - x0), Tone::Bar),
            ("·".repeat(bar_w - x1), Tone::Quiet),
            (format!("  {}", pad(&tail, tail_w)), Tone::Quiet),
        ]);
    }
    Some(rows)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(rows: &[Row]) -> Vec<String> {
        rows.iter()
            .map(|r| r.iter().map(|(s, _)| s.as_str()).collect())
            .collect()
    }

    #[test]
    fn pie_lists_labels_values_and_shares() {
        let rows = render(
            "pie title Pets\n\"Dogs\" : 386\n\"Cats\" : 85\n\"Rats\" : 15",
            60,
        )
        .unwrap();
        let t = text(&rows);
        assert_eq!(t[0], "Pets");
        assert!(
            t[1].contains("Dogs") && t[1].contains("386") && t[1].contains("79.4%"),
            "{t:?}"
        );
        assert!(t[3].contains("Rats") && t[3].contains("3.1%"), "{t:?}");
        assert!(t.iter().all(|l| l.width() <= 60));
    }

    #[test]
    fn gantt_keeps_every_label_date_and_dependency() {
        let src = "gantt\n dateFormat YYYY-MM-DD\n title Plan\n section Build\n Parse :a, 2024-01-01, 5d\n Render :b, after a, 6d\n section Ship\n Test :c, after b, 4d\n Release :d, after c, 2d";
        let t = text(&render(src, 80).unwrap());
        let all = t.join("\n");
        for needle in [
            "Plan",
            "Build",
            "Ship",
            "Parse",
            "Render",
            "Test",
            "Release",
            "2024-01-01 → 2024-01-06",
            "2024-01-06 → 2024-01-12",
        ] {
            assert!(all.contains(needle), "missing {needle}: {all}");
        }
        assert!(t.iter().all(|l| l.width() <= 80), "{t:?}");
        // Dependent tasks start where their predecessor ended, so bars step right.
        let bar_at = |name: &str| {
            let line = t.iter().find(|l| l.starts_with(name)).unwrap();
            line.chars().position(|c| c == '█').unwrap()
        };
        assert!(bar_at("Parse") < bar_at("Render") && bar_at("Render") < bar_at("Test"));
    }

    #[test]
    fn gantt_narrow_uses_durations_and_tiny_widths_decline() {
        let src =
            "gantt\n dateFormat YYYY-MM-DD\n Parse :a, 2024-01-01, 5d\n Render :b, after a, 6d";
        let t = text(&render(src, 36).unwrap());
        assert!(t.iter().any(|l| l.contains("6d")), "{t:?}");
        assert!(render(src, 14).is_none());
    }

    #[test]
    fn timeline_rows_pair_periods_with_events() {
        let t = text(
            &render(
                "timeline\n title History\n 2024 : Shipped : Scaled\n 2025 : Rewrote",
                50,
            )
            .unwrap(),
        );
        assert_eq!(t[0], "History");
        assert!(t[1].contains("2024") && t[1].contains("Shipped"));
        assert!(!t[2].contains("2024") && t[2].contains("Scaled"));
        assert!(t[3].contains("2025") && t[3].contains("Rewrote"));
    }

    #[test]
    fn unknown_or_invalid_sources_decline() {
        assert!(render("graph TD\nA-->B", 80).is_none());
        assert!(render("pie\ninvalid ???", 80).is_none());
        assert!(render("gantt\n dateFormat DD/MM\n A :a, 2024-01-01, 1d", 80).is_none());
        assert!(render("%% c\npie\n\"a\":1", 60).is_some() && render("mindmap\n a", 60).is_none());
    }

    #[test]
    fn date_roundtrip() {
        for d in ["1970-01-01", "2024-02-29", "2024-12-31", "1999-03-01"] {
            assert_eq!(civil(days(d).unwrap()), d);
        }
        assert_eq!(days("2024-01-02").unwrap() - days("2024-01-01").unwrap(), 1);
    }
}

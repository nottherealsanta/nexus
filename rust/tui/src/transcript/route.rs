//! Quota-aware routing on a sub-agent row (`agent.routed`).
//!
//! One labelled line under the row's model line: what was asked for (`low`), the candidate
//! chosen (after the arrow), and each candidate skipped below its plan-quota threshold with
//! the figures that decided it (`monthly 1.5% left ≤ 2%, resets Nov 1`). When every candidate
//! was below its threshold the line is a warning and still names the chosen one. Reset dates
//! are shown in UTC. No routing decision draws nothing.
use super::{indented, Rows};
use crate::{
    bridge::{SkippedCandidate, SubagentRoute},
    render::Palette,
};
use ratatui::{style::Style, text::Span};
use serde_json::Value;

/// Columns the route line is indented: the task row's model-line lead.
const INDENT: usize = 6;

/// Push the route line (wrapped at words, never cut) for a routed sub-agent.
pub fn draw(out: &mut Rows, route: &SubagentRoute, width: usize, p: &Palette, op: &Option<Value>) {
    let (text, style) = if route.all_blocked {
        let chosen = or_dash(&route.chosen);
        (
            format!("all candidates below usage threshold · using {chosen}"),
            Style::default().fg(p.warning),
        )
    } else {
        let left = route
            .requested
            .as_deref()
            .filter(|requested| !requested.is_empty())
            .unwrap_or("routed");
        (
            format!("{left} → {}", or_dash(&route.chosen)),
            Style::default().fg(p.quiet),
        )
    };
    let mut text = text;
    for skipped in &route.skipped {
        text.push_str(" · skipped ");
        text.push_str(&skipped_text(skipped));
    }
    indented(out, vec![Span::styled(text, style)], INDENT, width, op);
}

fn or_dash(text: &str) -> &str {
    if text.is_empty() {
        "–"
    } else {
        text
    }
}

/// `github-copilot/gpt-6-luna (monthly 1.5% left ≤ 2%, resets Nov 1)`.
fn skipped_text(skipped: &SkippedCandidate) -> String {
    let reference = or_dash(&skipped.reference);
    let kind = match skipped.kind.as_str() {
        "" => skipped.label.as_str(),
        "five_hour" => "5-hour",
        other => other,
    };
    let mut detail = format!(
        "{}% left ≤ {}%",
        percent(skipped.remaining_percent),
        percent(skipped.threshold_percent)
    );
    if !kind.is_empty() {
        detail = format!("{kind} {detail}");
    }
    if let Some(at) = skipped.resets_at {
        detail.push_str(&format!(", resets {}", short_date(at)));
    }
    format!("{reference} ({detail})")
}

/// A percentage without needless decimals: `2`, `1.5`, `10.25`.
pub fn percent(value: f64) -> String {
    if !value.is_finite() {
        return "?".to_string();
    }
    let text = format!("{value:.2}");
    text.trim_end_matches('0').trim_end_matches('.').to_string()
}

/// `Nov 1` for epoch seconds, in UTC. Out-of-range values read `?`.
pub fn short_date(epoch: i64) -> String {
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let days = epoch.div_euclid(86_400);
    // Civil date from days since 1970-01-01 (proleptic Gregorian).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    match usize::try_from(month - 1) {
        Ok(index) if index < 12 => format!("{} {day}", MONTHS[index]),
        _ => "?".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bridge::Content;
    use crate::transcript::build;
    use unicode_width::UnicodeWidthStr;

    const NOV_1: i64 = 1_793_491_200;

    fn luna_skip() -> SkippedCandidate {
        SkippedCandidate {
            reference: "github-copilot/gpt-6-luna".into(),
            quota: "copilot".into(),
            kind: "monthly".into(),
            label: "Monthly".into(),
            remaining_percent: 1.5,
            threshold_percent: 2.0,
            resets_at: Some(NOV_1),
        }
    }

    fn rows_text(rows: &Rows) -> Vec<String> {
        rows.iter()
            .map(|(line, _)| line.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect()
    }

    fn task_with(route: Option<SubagentRoute>) -> Content {
        Content {
            kind: "task".into(),
            agent_name: "explorer".into(),
            task: "search".into(),
            model: "gpt-6-luna".into(),
            tier: "low".into(),
            elapsed_ms: 3200,
            status: "done".into(),
            color: "#ff0000".into(),
            route,
            ..Default::default()
        }
    }

    #[test]
    fn rerouted_line_names_requested_chosen_and_each_skipped_candidate() {
        let p = Palette::new(false);
        let route = SubagentRoute {
            requested: Some("low".into()),
            first: "github-copilot/gpt-6-luna".into(),
            chosen: "openai-codex/gpt-6-luna".into(),
            all_blocked: false,
            skipped: vec![luna_skip()],
        };
        let mut out: Rows = Vec::new();
        draw(&mut out, &route, 120, &p, &None);
        let text = rows_text(&out);
        assert_eq!(
            text,
            ["      low → openai-codex/gpt-6-luna · skipped github-copilot/gpt-6-luna (monthly 1.5% left ≤ 2%, resets Nov 1)"]
        );
        assert_eq!(out[0].0.spans[1].style.fg, Some(p.quiet));

        // One parenthetical per skipped candidate, and no requested means "routed".
        let two = SubagentRoute {
            requested: None,
            skipped: vec![
                luna_skip(),
                SkippedCandidate {
                    reference: "anthropic/claude-x".into(),
                    kind: "five_hour".into(),
                    remaining_percent: 10.25,
                    threshold_percent: 10.0,
                    resets_at: None,
                    ..Default::default()
                },
            ],
            ..route
        };
        let mut out: Rows = Vec::new();
        draw(&mut out, &two, 240, &p, &None);
        assert_eq!(
            rows_text(&out),
            ["      routed → openai-codex/gpt-6-luna · skipped github-copilot/gpt-6-luna (monthly 1.5% left ≤ 2%, resets Nov 1) · skipped anthropic/claude-x (5-hour 10.25% left ≤ 10%)"]
        );
    }

    #[test]
    fn all_blocked_line_is_a_warning_and_keeps_the_skipped_details() {
        let p = Palette::new(false);
        let route = SubagentRoute {
            requested: Some("low".into()),
            first: "github-copilot/gpt-6-luna".into(),
            chosen: "openai-codex/gpt-6-luna".into(),
            all_blocked: true,
            skipped: vec![luna_skip()],
        };
        let mut out: Rows = Vec::new();
        draw(&mut out, &route, 200, &p, &None);
        let text = rows_text(&out).concat();
        assert_eq!(
            text,
            "      all candidates below usage threshold · using openai-codex/gpt-6-luna · skipped github-copilot/gpt-6-luna (monthly 1.5% left ≤ 2%, resets Nov 1)"
        );
        assert_eq!(out[0].0.spans[1].style.fg, Some(p.warning));
    }

    #[test]
    fn routed_line_wraps_at_words_and_never_drops_text() {
        let p = Palette::new(false);
        let route = SubagentRoute {
            requested: Some("low".into()),
            chosen: "openai-codex/gpt-6-luna".into(),
            skipped: vec![luna_skip()],
            ..Default::default()
        };
        let mut out: Rows = Vec::new();
        draw(&mut out, &route, 44, &p, &None);
        let rows = rows_text(&out);
        assert!(rows.len() > 1, "{rows:?}");
        assert!(rows.iter().all(|row| row.width() <= 44), "{rows:?}");
        let joined = rows
            .iter()
            .map(|row| row.trim())
            .collect::<Vec<_>>()
            .join(" ");
        assert!(joined.contains("resets Nov 1)"), "{joined:?}");
        assert!(joined.contains("1.5% left ≤ 2%"), "{joined:?}");
    }

    #[test]
    fn percent_and_date_formatting() {
        assert_eq!(percent(2.0), "2");
        assert_eq!(percent(1.5), "1.5");
        assert_eq!(percent(10.25), "10.25");
        assert_eq!(percent(0.0), "0");
        assert_eq!(percent(f64::NAN), "?");
        assert_eq!(short_date(0), "Jan 1");
        assert_eq!(short_date(NOV_1), "Nov 1");
        assert_eq!(short_date(1_799_971_200), "Jan 15");
    }

    #[test]
    fn task_row_gains_one_route_line_and_absent_route_adds_nothing() {
        let p = Palette::new(false);
        let plain = build(&task_with(None), 100, &p);
        assert_eq!(plain.len(), 2);
        let plain_text = rows_text(&plain).concat();
        assert!(!plain_text.contains('→') && !plain_text.contains("skipped"));

        let routed = build(
            &task_with(Some(SubagentRoute {
                requested: Some("low".into()),
                chosen: "openai-codex/gpt-6-luna".into(),
                skipped: vec![luna_skip()],
                ..Default::default()
            })),
            140,
            &p,
        );
        let text = rows_text(&routed);
        assert_eq!(text.len(), 3, "{text:?}");
        assert!(
            text[2].contains("low → openai-codex/gpt-6-luna"),
            "{text:?}"
        );
        // The route line sits after the model line and before any detail.
        assert!(text[1].contains("gpt-6-luna"), "{text:?}");
    }
}

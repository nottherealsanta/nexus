//! Provider usage table for `GET /api/usage` (K11).

use serde_json::Value;

const WEEK: f64 = 604_800.0;

pub fn gauge_cells(used: Option<f64>) -> [u8; 10] {
    let eighths = used
        .filter(|v| v.is_finite())
        .unwrap_or(0.0)
        .clamp(0.0, 100.0);
    let eighths = (eighths * 0.8).round_ties_even() as i32;
    std::array::from_fn(|i| (eighths - 8 * i as i32).clamp(0, 8) as u8)
}

pub fn percent_left(used: Option<f64>) -> String {
    let Some(used) = used.filter(|v| v.is_finite()) else {
        return "—".into();
    };
    let left = 100.0 - used.clamp(0.0, 100.0);
    if left.fract() != 0.0 && !(10.0..=90.0).contains(&left) {
        format!("{left:.1}%")
    } else {
        format!("{left:.0}%")
    }
}

pub fn reserve(
    used: Option<f64>,
    reset: Option<f64>,
    period: Option<f64>,
    now: f64,
) -> Option<f64> {
    let (used, reset, period) = (used?, reset?, period?);
    if !used.is_finite()
        || !reset.is_finite()
        || !period.is_finite()
        || period < WEEK
        || reset <= now
    {
        return None;
    }
    Some(100.0 * (1.0 - (reset - now) / period) - used.clamp(0.0, 100.0))
}

pub fn reserve_text(value: Option<f64>) -> String {
    match value.filter(|v| v.is_finite()).map(f64::round_ties_even) {
        Some(v) if v > 0.0 => format!("+{v:.0}%"),
        Some(v) if v < 0.0 => format!("{v:.0}%"),
        Some(_) => "0%".into(),
        None => "—".into(),
    }
}

/// Duration to a reset, at minute precision, with the two largest units.
pub fn duration(at: Option<f64>, now: f64) -> String {
    let Some(at) = at.filter(|v| v.is_finite()) else {
        return "—".into();
    };
    let secs = (at - now).max(0.0) as u64;
    let (d, h, m) = (secs / 86_400, secs % 86_400 / 3600, secs % 3600 / 60);
    match (d, h, m) {
        (0, 0, 0) => "now".into(),
        (0, 0, m) => format!("{m}m"),
        (0, h, m) => format!("{h}h {m}m"),
        (d, h, _) => format!("{d}d {h}h"),
    }
}

fn age(at: f64, now: f64) -> String {
    let secs = (now - at).max(0.0) as u64;
    if secs < 60 {
        format!("{secs}s")
    } else {
        duration(Some(now), at)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Row {
    Header,
    Provider {
        name: String,
        plan: String,
    },
    Window {
        label: String,
        used: Option<u8>,
        cells: [u8; 10],
        kind: String,
        left: String,
        reserve: String,
        expires: String,
    },
    Note(String),
    Gap,
}

pub const EMPTY: &str = "No provider is connected. Connect one in Settings.";

pub fn rows(usage: &Value, now: f64) -> Vec<Row> {
    if usage.get("fetched").is_some_and(Value::is_null) {
        return vec![Row::Note(
            "No usage snapshot yet. Press r to refresh.".into(),
        )];
    }
    let mut out = Vec::new();
    for p in usage["providers"].as_array().into_iter().flatten() {
        let id = p["id"].as_str().unwrap_or_default();
        if out.is_empty() {
            out.push(Row::Header);
        }
        out.push(Row::Provider {
            name: p["label"]
                .as_str()
                .or(p["id"].as_str())
                .unwrap_or("provider")
                .to_owned(),
            plan: p["plan"].as_str().unwrap_or_default().to_owned(),
        });
        if let Some(error) = p["error"].as_str().filter(|s| !s.is_empty()) {
            out.push(Row::Note(format!("Could not read the limits: {error}")));
            continue;
        }
        let windows: Vec<_> = p["windows"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|w| {
                let label = w["label"].as_str().unwrap_or_default();
                id != "copilot" || !(label == "Chat (monthly)" || label == "Completions (monthly)")
            })
            .collect();
        for w in &windows {
            let used = w["used_percent"].as_f64().map(|u| u.clamp(0.0, 100.0));
            let reset = w["resets_at"].as_f64();
            out.push(Row::Window {
                label: w["label"].as_str().unwrap_or("window").to_owned(),
                used: used.map(|u| u as u8),
                cells: gauge_cells(used),
                kind: w["kind"].as_str().unwrap_or_default().to_owned(),
                left: percent_left(used),
                reserve: reserve_text(reserve(used, reset, w["period_seconds"].as_f64(), now)),
                expires: duration(reset, now),
            });
        }
        let notes: Vec<_> = p["notes"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .filter(|n| !n.is_empty())
            .filter(|n| !(id == "codex" && n.starts_with("Limit reached:")))
            .filter(|n| {
                !(id == "grok" && (n.starts_with("Prepaid credits:") || n.ends_with(" of credits")))
            })
            .filter(|n| {
                !(id == "copilot"
                    && (n.starts_with("AI credits used this period:")
                        || n.starts_with("Chat: ")
                        || n.starts_with("Completions: ")))
            })
            .collect();
        if windows.is_empty() && notes.is_empty() {
            out.push(Row::Note("No limit windows reported.".into()));
        }
        out.extend(notes.into_iter().map(|n| Row::Note(n.to_owned())));
    }
    if out.is_empty() {
        out.push(Row::Note(EMPTY.into()));
    }
    if let Some(fetched) = usage["fetched"].as_f64() {
        out.push(Row::Gap);
        out.push(Row::Note(format!("Updated {} ago", age(fetched, now))));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn usage_gauge_and_left() {
        for (used, eighths) in [
            (0., 0),
            (1., 1),
            (9., 7),
            (10., 8),
            (24., 19),
            (36., 29),
            (47., 38),
            (68., 54),
            (90., 72),
            (100., 80),
        ] {
            assert_eq!(
                gauge_cells(Some(used))
                    .iter()
                    .map(|v| u32::from(*v))
                    .sum::<u32>(),
                eighths
            );
        }
        assert_eq!(gauge_cells(None), [0; 10]);
        assert_eq!(gauge_cells(Some(10.625))[1], 0); // 8.5 eighths rounds to eight
        assert_eq!(gauge_cells(Some(11.875))[1], 2); // 9.5 rounds to ten
        assert_eq!(gauge_cells(Some(24.0)), [8, 8, 3, 0, 0, 0, 0, 0, 0, 0]);
        assert_eq!(gauge_cells(Some(36.0)), [8, 8, 8, 5, 0, 0, 0, 0, 0, 0]);
        assert_eq!(percent_left(Some(24.0)), "76%");
        assert_eq!(percent_left(Some(100.0)), "0%");
        assert_eq!(percent_left(Some(0.2)), "99.8%");
        assert_eq!(percent_left(Some(99.8)), "0.2%");
        assert_eq!(percent_left(None), "—");
    }
    #[test]
    fn usage_reserve_and_duration() {
        assert_eq!(reserve_text(Some(8.5)), "+8%");
        assert_eq!(
            reserve_text(reserve(Some(41.5), Some(WEEK / 2.0), Some(WEEK), 0.0)),
            "+8%"
        );
        assert_eq!(reserve_text(Some(-8.5)), "-8%");
        assert_eq!(reserve_text(Some(0.0)), "0%");
        assert_eq!(reserve_text(Some(8.6)), "+9%");
        assert_eq!(
            reserve(Some(10.), Some(302_400.), Some(WEEK), 0.),
            Some(40.)
        );
        assert!(reserve(Some(10.), Some(100.), Some(18_000.), 0.).is_none());
        assert!(reserve(Some(10.), Some(100.), Some(WEEK - 1.), 0.).is_none());
        assert!(reserve(Some(10.), Some(100.), None, 0.).is_none());
        assert!(reserve(None, Some(100.), Some(WEEK), 0.).is_none());
        assert!(reserve(Some(10.), Some(0.), Some(WEEK), 0.).is_none());
        assert!(reserve(Some(10.), Some(100.), Some(30. * 86400.), 0.).is_some());
        assert_eq!(duration(None, 0.), "—");
        assert_eq!(duration(Some(-1.), 0.), "now");
        assert_eq!(duration(Some(59.), 0.), "now");
        assert_eq!(duration(Some(11_400.), 0.), "3h 10m");
        assert_eq!(duration(Some(187_200.), 0.), "2d 4h");
    }
    #[test]
    fn usage_cached_notes_and_details_are_hidden() {
        let data: Value = serde_json::from_str(include_str!(
            "../../interface/scenarios/provider-usage.json"
        ))
        .unwrap();
        let text = format!("{:?}", rows(&data, 1.0));
        for hidden in [
            "Limit reached",
            "of credits",
            "of 300 left",
            "Prepaid credits: $2.50",
            "AI credits used this period",
        ] {
            assert!(!text.contains(hidden), "{text}");
        }
        assert!(text.contains("Other xAI note"));
        assert!(text.contains("Prepaid credits: $1.00"));
        assert!(matches!(rows(&data, 1.0)[0], Row::Header));
    }
}

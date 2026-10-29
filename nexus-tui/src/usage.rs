//! The plan-limits text for `GET /api/usage` (K11): one block per provider, one line per window.

use serde_json::Value;

/// `3h 10m`, `2d 4h`, `45s`: how long until `at`, in the two largest units.
pub fn until(at: f64, now: f64) -> String {
    let secs = (at - now).max(0.0) as u64;
    let (d, h, m) = (secs / 86_400, secs % 86_400 / 3600, secs % 3600 / 60);
    match (d, h, m) {
        (0, 0, 0) => format!("{secs}s"),
        (0, 0, m) => format!("{m}m"),
        (0, h, m) => format!("{h}h {m}m"),
        (d, h, _) => format!("{d}d {h}h"),
    }
}

/// One line of the usage modal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Row {
    /// A provider heading: name, then its plan.
    Provider { name: String, plan: String },
    /// A limit window: whole percent used (`None` when the provider did not say) and when it resets.
    Window {
        label: String,
        used: Option<u8>,
        reset: String,
    },
    /// A reason or remark under a provider, in plain text.
    Note(String),
    /// Space between providers.
    Gap,
}

pub const EMPTY: &str = "No signed-in provider reports plan limits (codex and copilot do).";

/// The modal's rows for `GET /api/usage`: a heading per provider, one row per window.
/// Reserve, pace marks and absolute reset times are left out; the bar and "resets in" say it.
pub fn rows(usage: &Value, now: f64) -> Vec<Row> {
    let mut out = Vec::new();
    for p in usage["providers"].as_array().into_iter().flatten() {
        if !out.is_empty() {
            out.push(Row::Gap);
        }
        let text = |key: &str| p[key].as_str().unwrap_or_default().to_owned();
        out.push(Row::Provider {
            name: p["label"]
                .as_str()
                .or(p["id"].as_str())
                .unwrap_or("provider")
                .to_owned(),
            plan: text("plan"),
        });
        if let Some(error) = p["error"].as_str().filter(|s| !s.is_empty()) {
            out.push(Row::Note(format!("Could not read the limits: {error}")));
            continue;
        }
        for w in p["windows"].as_array().into_iter().flatten() {
            let detail = w["detail"].as_str().filter(|s| !s.is_empty());
            let reset = match (w["resets_at"].as_f64(), detail) {
                (Some(at), Some(d)) => format!("resets in {} · {d}", until(at, now)),
                (Some(at), None) => format!("resets in {}", until(at, now)),
                (None, d) => d.unwrap_or_default().to_owned(),
            };
            out.push(Row::Window {
                label: w["label"].as_str().unwrap_or("window").to_owned(),
                used: w["used_percent"]
                    .as_f64()
                    .map(|u| u.round().clamp(0.0, 100.0) as u8),
                reset,
            });
        }
    }
    if out.is_empty() {
        out.push(Row::Note(EMPTY.into()));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_read_as_percent_and_time_left() {
        let usage = serde_json::json!({"providers": [
            {"id": "codex", "label": "ChatGPT (Codex)", "plan": "plus", "notes": [], "windows": [
                {"label": "5-hour", "used_percent": 42.4, "resets_at": 10_800.0 + 600.0}]},
            {"id": "copilot", "label": "GitHub Copilot", "error": "timed out", "windows": []}]});
        assert_eq!(
            rows(&usage, 0.0),
            vec![
                Row::Provider {
                    name: "ChatGPT (Codex)".into(),
                    plan: "plus".into()
                },
                Row::Window {
                    label: "5-hour".into(),
                    used: Some(42),
                    reset: "resets in 3h 10m".into()
                },
                Row::Gap,
                Row::Provider {
                    name: "GitHub Copilot".into(),
                    plan: String::new()
                },
                Row::Note("Could not read the limits: timed out".into()),
            ]
        );
        assert_eq!(until(90_000.0, 0.0), "1d 1h");
        assert_eq!(
            rows(&serde_json::json!({"providers": []}), 0.0),
            vec![Row::Note(EMPTY.into())]
        );
    }
}

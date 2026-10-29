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

pub fn text(usage: &Value, now: f64) -> String {
    let providers = usage["providers"].as_array().cloned().unwrap_or_default();
    if providers.is_empty() {
        return "No signed-in provider reports plan limits (codex and copilot do).".into();
    }
    let mut out = Vec::new();
    for p in &providers {
        let plan = p["plan"].as_str().filter(|s| !s.is_empty());
        out.push(format!(
            "{}{}",
            p["label"].as_str().or(p["id"].as_str()).unwrap_or("provider"),
            plan.map(|s| format!(" · {s}")).unwrap_or_default()
        ));
        if let Some(error) = p["error"].as_str().filter(|s| !s.is_empty()) {
            out.push(format!("  could not read the limits: {error}"));
            out.push(String::new());
            continue;
        }
        for w in p["windows"].as_array().cloned().unwrap_or_default() {
            let used = w["used_percent"].as_f64().map(|u| format!("{u:.0}% used"));
            let reset = w["resets_at"].as_f64().map(|r| format!("resets in {}", until(r, now)));
            let detail = w["detail"].as_str().filter(|s| !s.is_empty()).map(str::to_owned);
            let parts: Vec<String> = [used, reset, detail].into_iter().flatten().collect();
            out.push(format!(
                "  {}  {}",
                w["label"].as_str().unwrap_or("window"),
                parts.join(" · ")
            ));
        }
        for note in p["notes"].as_array().cloned().unwrap_or_default() {
            if let Some(n) = note.as_str() {
                out.push(format!("  {n}"));
            }
        }
        out.push(String::new());
    }
    out.join("\n").trim_end().to_owned()
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
        let text = text(&usage, 0.0);
        assert!(text.contains("ChatGPT (Codex) · plus"));
        assert!(text.contains("5-hour  42% used · resets in 3h 10m"), "{text}");
        assert!(text.contains("could not read the limits: timed out"));
        assert_eq!(until(90_000.0, 0.0), "1d 1h");
        assert!(super::text(&serde_json::json!({"providers": []}), 0.0).contains("No signed-in"));
    }
}

use serde_json::Value;
use std::sync::atomic::{AtomicU64, Ordering};

pub const VIEWS: [&str; 5] = ["Timing", "Conversation", "Steps", "Requests", "Raw"];
static GENERATION: AtomicU64 = AtomicU64::new(0);
pub fn generation() -> u64 {
    GENERATION.fetch_add(1, Ordering::Relaxed) + 1
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Inspector {
    pub session: String,
    pub view: usize,
    pub page: usize,
    pub generation: u64,
    pub data: Value,
    pub loading: bool,
    pub error: String,
    pub top: usize,
    pub cursor: usize,
    pub details: bool,
    pub info: bool,
    pub zoom: u32,
    pub pan: u32,
}
impl Inspector {
    pub fn new(session: String) -> Self {
        Self {
            session,
            view: 0,
            page: 0,
            generation: generation(),
            data: Value::Null,
            loading: true,
            error: String::new(),
            top: 0,
            cursor: 0,
            details: false,
            info: false,
            zoom: 1,
            pan: 0,
        }
    }
    pub fn text(&self, width: usize) -> String {
        let mut out = format!(
            "{}\n1 Timing · 2 Conversation · 3 Steps · 4 Requests · 5 Raw\n{} · Page {}\n",
            self.session,
            VIEWS[self.view],
            self.page + 1
        );
        let cache = &self.data["cache"];
        let n = |key: &str| {
            cache[key].as_u64().map_or_else(
                || "unknown".into(),
                |n| {
                    let digits = n.to_string();
                    let mut out = String::new();
                    for (i, ch) in digits.chars().enumerate() {
                        if i > 0 && (digits.len() - i).is_multiple_of(3) {
                            out.push(',');
                        }
                        out.push(ch);
                    }
                    out
                },
            )
        };
        let hit = cache["hit_percent"].as_f64().map_or_else(
            || "unknown".into(),
            |v| format!("{}%", format!("{v:.1}").trim_end_matches(".0")),
        );
        out.push_str(&format!(
            "Input {} · Cached {} · Uncached {} · Hit {hit} · Writes unknown\n",
            n("input"),
            n("cached"),
            n("uncached")
        ));
        if self.loading {
            out.push_str("Loading inspector…\n");
        }
        if !self.error.is_empty() {
            out.push_str(&format!("{}\n", self.error));
        }
        if let Some(notes) = self.data["notes"].as_array() {
            for note in notes {
                let note = note.as_str().unwrap_or("");
                if !self.info && note.starts_with("Cache") {
                    continue;
                }
                out.push_str(note);
                out.push('\n');
            }
        }
        out.push_str("\nr refresh · n/p older/newer · +/- zoom · h/l pan\nj/k select · enter details · c copy · i cache info\n\n");
        if self.view != 0 {
            out.push_str(self.data["text"].as_str().unwrap_or(""));
            return out;
        }
        let Some(lanes) = self.data["lanes"].as_array().filter(|l| !l.is_empty()) else {
            out.push_str("No retained execution timings.");
            return out;
        };
        let origin = lanes
            .iter()
            .filter_map(|l| l["start"].as_f64())
            .fold(f64::INFINITY, f64::min);
        let end = lanes
            .iter()
            .filter_map(|l| l["end"].as_f64())
            .fold(origin, f64::max);
        let total = (end - origin).max(0.001);
        let span = total / self.zoom as f64;
        let start = origin + (total - span) * self.pan as f64 / 10.0;
        let label_width = (width / 3).clamp(10, 30);
        let columns = width.saturating_sub(label_width + 18).max(8);
        out.push_str(&format!(
            "{}× · +{:.2}s — +{:.2}s — +{:.2}s\n",
            self.zoom,
            start - origin,
            start + span / 2.0 - origin,
            start + span - origin
        ));
        out.push_str("# model · = tool · - turn/attempt · > live · ! failed\n");
        for (i, lane) in lanes.iter().enumerate() {
            let a = lane["start"].as_f64().unwrap_or(origin);
            let b = lane["end"].as_f64().unwrap_or(a);
            let left = (((a - start) / span * columns as f64).floor() as isize)
                .clamp(0, columns as isize - 1) as usize;
            let right = (((b - start) / span * columns as f64).ceil() as isize)
                .clamp(0, columns as isize) as usize;
            let status = lane["status"].as_str().unwrap_or("");
            let glyph =
                if status.contains("fail") || status.contains("error") || status == "interrupted" {
                    '!'
                } else if lane["running"] == true {
                    '>'
                } else if lane["kind"] == "model" {
                    '#'
                } else if lane["kind"] == "tool" {
                    '='
                } else {
                    '-'
                };
            let mut bar = vec![' '; columns];
            if b >= start && a <= start + span {
                for cell in bar.iter_mut().take(right.max(left + 1)).skip(left) {
                    *cell = glyph;
                }
            }
            let label = format!(
                "{} · {} · {}",
                lane["session"].as_str().unwrap_or(""),
                lane["kind"].as_str().unwrap_or(""),
                lane["label"].as_str().unwrap_or("")
            );
            let label: String = label.chars().take(label_width).collect();
            let duration = if lane["end_unknown"] == true {
                "end unknown".into()
            } else {
                format!("{:.2}s", b - a)
            };
            out.push_str(&format!(
                "{} {:label_width$} |{}| {duration}\n",
                if i == self.cursor { '▸' } else { ' ' },
                label,
                bar.iter().collect::<String>()
            ));
        }
        if self.details {
            if let Some(lane) = lanes.get(self.cursor) {
                out.push_str("\nSelected operation\n");
                out.push_str(lane["detail"].as_str().unwrap_or(""));
            }
        }
        out
    }
}

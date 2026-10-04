//! Opt-in, bounded terminal timings (RATATUI_REDESIGN_PLAN §2.1).
use std::{
    collections::{BTreeMap, VecDeque},
    time::{Duration, Instant},
};
/// Phases slower than this are appended to `~/.nexus/tui-stalls.log` (always on, bounded).
const STALL: Duration = Duration::from_millis(50);
const STALL_LOG_LIMIT: u64 = 256 * 1024;
/// Records one slow phase of the terminal loop so a multi-second freeze names its cause.
pub fn stall(phase: &str, elapsed: Duration) {
    if elapsed < STALL {
        return;
    }
    let Some(home) = std::env::var_os("HOME") else {
        return;
    };
    let path = std::path::PathBuf::from(home).join(".nexus/tui-stalls.log");
    let oversized = std::fs::metadata(&path).is_ok_and(|meta| meta.len() > STALL_LOG_LIMIT);
    let at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64());
    use std::io::Write;
    let mut options = std::fs::OpenOptions::new();
    options.create(true).write(true);
    if oversized {
        options.truncate(true);
    } else {
        options.append(true);
    }
    if let Ok(mut file) = options.open(path) {
        let _ = writeln!(
            file,
            "{at:.3} pid {} {phase}: {:.0} ms",
            std::process::id(),
            elapsed.as_secs_f64() * 1000.0
        );
    }
}
#[derive(Default)]
pub struct Trace {
    enabled: bool,
    samples: VecDeque<(&'static str, f64)>,
    published: Option<Instant>,
}
impl Trace {
    pub fn new() -> Self {
        Self {
            enabled: std::env::var("NEXUS_TUI_TRACE").as_deref() == Ok("1"),
            ..Self::default()
        }
    }
    pub fn record(&mut self, name: &'static str, elapsed: Duration) {
        if !self.enabled {
            return;
        }
        if self.samples.len() >= 2000 * 8 {
            self.samples.pop_front();
        }
        self.samples
            .push_back((name, elapsed.as_secs_f64() * 1000.0));
    }
    pub fn record_count(&mut self, name: &'static str, value: usize) {
        if self.enabled {
            if self.samples.len() >= 16000 {
                self.samples.pop_front();
            }
            self.samples.push_back((name, value as f64));
        }
    }
    pub fn summary(&self) -> Vec<String> {
        let mut groups: BTreeMap<&str, Vec<f64>> = BTreeMap::new();
        for (name, elapsed) in &self.samples {
            groups.entry(name).or_default().push(*elapsed);
        }
        let raw = std::env::var("NEXUS_TUI_TRACE_RAW").as_deref() == Ok("1");
        let mut lines: Vec<String> = Vec::new();
        if raw {
            let ordered: Vec<String> = self
                .samples
                .iter()
                .filter(|(name, _)| *name == "event→frame")
                .map(|(_, ms)| format!("{ms:.0}"))
                .collect();
            lines.push(format!("event→frame in order (ms): {}", ordered.join(" ")));
        }
        lines.extend(groups.into_iter().map(|(name, mut times)| {
            times.sort_by(f64::total_cmp);
            let percentile =
                |fraction: f64| times[((times.len() - 1) as f64 * fraction).ceil() as usize];
            format!(
                "{name}: p50 {:.3} · p95 {:.3} · max {:.3} {} (n={})",
                percentile(0.50),
                percentile(0.95),
                times[times.len() - 1],
                if name == "layout_blocks" {
                    "blocks"
                } else if name == "layout_reset" {
                    "resets"
                } else {
                    "ms"
                },
                times.len()
            )
        }));
        lines
    }
    pub fn publish(&mut self) -> Option<Vec<String>> {
        if !self.enabled
            || self
                .published
                .is_some_and(|at| at.elapsed() < Duration::from_secs(2))
        {
            return None;
        }
        self.published = Some(Instant::now());
        Some(self.summary())
    }
    pub fn finish(&self) {
        if !self.enabled {
            return;
        }
        let path = std::env::var_os("NEXUS_TUI_TRACE_FILE")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| {
                std::env::temp_dir().join(format!("nexus-tui-trace-{}.log", std::process::id()))
            });
        match std::fs::write(&path, self.summary().join("\n")) {
            Ok(()) => eprintln!("Native timing trace: {}", path.display()),
            Err(error) => eprintln!("Native timing trace could not be written: {error}"),
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_percentiles() {
        let mut trace = Trace {
            enabled: true,
            ..Default::default()
        };
        for i in 0..20000 {
            trace.record("draw", Duration::from_micros(i));
        }
        assert_eq!(trace.samples.len(), 16000);
        assert!(trace.summary()[0].contains("p95"));
        assert!(Trace::default().summary().is_empty());
    }
}

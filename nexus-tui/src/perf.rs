//! Client-side timings (docs/api.md#performance): each Core request carries a
//! trace id (`x-nexus-trace`), and requests that take at least [`SLOW_MS`] plus
//! every session-history load are appended as JSON lines to
//! `<home>/logs/tui-perf.jsonl`. A request line holds the client's total time and
//! Core's own handler time (`server-timing: app;dur=`); the gap between them is
//! time spent queued behind other work in Core, on the wire, or reading the body.
//! Only routes, sizes and timings are written, never bodies or query strings.

use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

/// Requests at least this slow on the client are written down.
pub const SLOW_MS: f64 = 50.0;
/// The log is started over past this size, keeping one previous file.
const MAX_BYTES: u64 = 4 * 1024 * 1024;

static LOG: OnceLock<PathBuf> = OnceLock::new();
static NEXT: AtomicU64 = AtomicU64::new(1);

/// Where timings go; without this call (tests, `--once`) nothing is written.
pub fn init(home: &std::path::Path) {
    let _ = LOG.set(home.join("logs").join("tui-perf.jsonl"));
}

/// `tui-<pid>-<n>`: unique per process, so a Core log line can be matched to it.
pub fn trace_id() -> String {
    format!(
        "tui-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    )
}

/// Core's handler time from a `server-timing` header (`app;dur=12.3, …`).
pub fn server_ms(header: Option<&str>) -> Option<f64> {
    header?.split(',').find_map(|metric| {
        let mut parts = metric.trim().split(';');
        (parts.next()? == "app")
            .then(|| parts.find_map(|p| p.trim().strip_prefix("dur=")?.parse().ok()))
            .flatten()
    })
}

pub fn ms(d: Duration) -> f64 {
    (d.as_secs_f64() * 1_000_000.0).round() / 1000.0
}

/// One Core request; written only when slow.
pub fn request(
    method: &str,
    path: &str,
    trace: &str,
    total: Duration,
    status: Option<u16>,
    server: Option<f64>,
    bytes: Option<usize>,
) {
    let total = ms(total);
    if total < SLOW_MS {
        return;
    }
    write(json!({
        "kind": "request",
        "method": method,
        "route": route(path),
        "trace": trace,
        "status": status,
        "ms": total,
        "core_ms": server,
        "outside_core_ms": server.map(|s| ((total - s) * 10.0).round() / 10.0),
        "bytes": bytes,
    }));
}

/// A finished measurement of something larger (a history load).
pub fn event(kind: &str, mut fields: Value) {
    if let Some(map) = fields.as_object_mut() {
        map.insert("kind".into(), kind.into());
    }
    write(fields);
}

/// The path with ids replaced (`/api/session/:id/turn`), so lines group by route.
pub fn route(path: &str) -> String {
    path.split('/')
        .map(|seg| {
            let id_like = seg.len() >= 16 && seg.chars().any(|c| c.is_ascii_digit());
            if id_like {
                ":id"
            } else {
                seg
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}

fn write(mut line: Value) {
    let Some(path) = LOG.get() else { return };
    let at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or_default();
    if let Some(map) = line.as_object_mut() {
        map.insert("at".into(), ((at * 1000.0).round() / 1000.0).into());
        map.insert("pid".into(), std::process::id().into());
    }
    if std::fs::metadata(path).is_ok_and(|m| m.len() > MAX_BYTES) {
        let _ = std::fs::rename(path, path.with_extension("jsonl.1"));
    }
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(f, "{line}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_timing_reads_the_app_metric() {
        assert_eq!(server_ms(Some("app;dur=12.5, cpu;dur=3")), Some(12.5));
        assert_eq!(server_ms(Some("cpu;dur=3, app;dur=7")), Some(7.0));
        assert_eq!(server_ms(Some("db;dur=3")), None);
        assert_eq!(server_ms(None), None);
    }

    #[test]
    fn routes_hide_ids() {
        assert_eq!(
            route("/api/session/ses_01JABCDEF0123456789/turn"),
            "/api/session/:id/turn"
        );
        assert_eq!(route("/api/info"), "/api/info");
    }

    #[test]
    fn trace_ids_are_unique() {
        assert_ne!(trace_id(), trace_id());
    }
}

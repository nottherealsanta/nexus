//! The only module with target-specific code (`cfg(target_arch)`): how the app
//! learns where the daemon is and how it authenticates. Everything else is
//! target-neutral; `tests/layering.rs` fails if `cfg(` appears elsewhere
//! (except `src/main.rs`).
//!
//! * web: the page is served by the daemon at `<prefix>/app/`, so the API is
//!   `../v1/web/` (relative, same origin) and the browser owns the HttpOnly
//!   cookie and the `Origin` header. No ticket is needed.
//! * native (macOS, ...): `NEXUS_URL` (`http://127.0.0.1:PORT/w/KEY/`) and
//!   `NEXUS_TICKET` come from the environment (`scripts/nsh-app.sh run macos`
//!   or `nexus app`). The app redeems the ticket itself and sends the cookie
//!   and `Origin` explicitly.

#[cfg(not(target_arch = "wasm32"))]
mod native;
#[cfg(target_arch = "wasm32")]
mod web;

#[cfg(not(target_arch = "wasm32"))]
pub use native::launch;
#[cfg(target_arch = "wasm32")]
pub use web::launch;

/// Shown on native targets when the launch environment is missing.
pub const MISSING_ENV: &str =
    "Not connected: start with `scripts/nsh-app.sh run macos` or `nexus app` (NEXUS_URL and NEXUS_TICKET are not set)";

/// How to reach and authenticate to the daemon.
#[derive(Clone, Debug, PartialEq)]
pub struct Launch {
    /// Prefix of every API path, ending in `/` (`../v1/web/` on the web).
    pub base: String,
    /// Exact `Origin` to send on native targets (the browser sets its own).
    pub origin: Option<String>,
    /// One-use ticket to redeem before the first request (native only).
    pub ticket: Option<String>,
}

/// Builds a native launch from `NEXUS_URL` and an optional ticket.
pub fn launch_from_url(url: &str, ticket: Option<String>) -> Result<Launch, String> {
    let url = url.trim();
    let rest = url
        .strip_prefix("http://")
        .or_else(|| url.strip_prefix("https://"))
        .ok_or_else(|| format!("NEXUS_URL must start with http:// (got `{url}`)"))?;
    let scheme_len = url.len() - rest.len();
    let host_len = rest.find('/').unwrap_or(rest.len());
    if host_len == 0 {
        return Err("NEXUS_URL has no host".to_owned());
    }
    let origin = url[..scheme_len + host_len].to_owned();
    let mut base = url.trim_end_matches('/').to_owned();
    base.push_str("/v1/web/");
    Ok(Launch { base, origin: Some(origin), ticket: ticket.filter(|t| !t.trim().is_empty()) })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn derives_base_and_origin_from_url() {
        let l = launch_from_url("http://127.0.0.1:8123/w/KEY/", Some("T".into())).unwrap();
        assert_eq!(l.base, "http://127.0.0.1:8123/w/KEY/v1/web/");
        assert_eq!(l.origin.as_deref(), Some("http://127.0.0.1:8123"));
        assert_eq!(l.ticket.as_deref(), Some("T"));
        let l = launch_from_url("http://127.0.0.1:9", Some(" ".into())).unwrap();
        assert_eq!(l.base, "http://127.0.0.1:9/v1/web/");
        assert_eq!(l.ticket, None);
        assert!(launch_from_url("ftp://x", None).is_err());
        assert!(launch_from_url("http:///x", None).is_err());
    }
}

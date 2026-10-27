//! HTTP access to the daemon's `/v1/web/*` host contract.
//!
//! [`Api`] is a `Copy` handle (its auth state lives in a signal). Paths are
//! relative to the launch base (`platform::Launch::base`): relative on the
//! web, absolute on native, where the app redeems its ticket itself and sends
//! `Cookie`, `Origin` and `X-CSRF-Token` explicitly. Writes are sent exactly
//! once; this module never retries.

use nsh_core::platform::HttpRequest;
use nsh_core::prelude::*;
use serde_json::Value;

use crate::platform::Launch;

/// A failed request: HTTP `status` (0 = no response) and a readable message.
#[derive(Clone, Debug, PartialEq)]
pub struct ApiError {
    pub status: u16,
    pub message: String,
}

impl ApiError {
    pub fn unauthorized(&self) -> bool {
        self.status == 401 || self.status == 403
    }
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl From<ApiError> for String {
    fn from(e: ApiError) -> String {
        e.message
    }
}

/// Result of connecting: the workspace the daemon serves.
#[derive(Clone, Debug, PartialEq)]
pub struct Bootstrap {
    pub workspace: String,
}

#[derive(Clone, Debug, PartialEq)]
struct Auth {
    launch: Launch,
    csrf: Option<String>,
    cookie: Option<String>,
}

/// Copy-able API context.
#[derive(Clone, Copy)]
pub struct Api {
    auth: Signal<Auth>,
}

/// `name=value` from a `Set-Cookie` header (attributes dropped).
pub fn cookie_pair(set_cookie: &str) -> Option<String> {
    let pair = set_cookie.split(';').next()?.trim();
    pair.contains('=').then(|| pair.to_owned())
}

/// `{csrf, workspace}` from a bootstrap or redeem response.
pub fn parse_bootstrap(v: &Value) -> Option<(String, String)> {
    let csrf = v.get("csrf")?.as_str()?.to_owned();
    Some((csrf, v.get("workspace").and_then(Value::as_str).unwrap_or("").to_owned()))
}

impl Api {
    pub fn new(launch: Launch) -> Self {
        Api { auth: signal(Auth { launch, csrf: None, cookie: None }) }
    }

    fn snapshot(&self) -> Auth {
        self.auth.get_untracked()
    }

    fn decorate(&self, mut req: HttpRequest, auth: &Auth, write: bool) -> HttpRequest {
        if let Some(c) = &auth.cookie {
            req = req.header("Cookie", c.clone());
        }
        if let Some(o) = &auth.launch.origin {
            req = req.header("Origin", o.clone());
        }
        if write {
            req = req.header("Content-Type", "application/json");
            if let Some(t) = &auth.csrf {
                req = req.header("X-CSRF-Token", t.clone());
            }
        }
        req
    }

    async fn send(req: HttpRequest) -> Result<(Value, Option<String>), ApiError> {
        let resp = fetch(req).await.map_err(|e| ApiError { status: 0, message: format!("network: {e}") })?;
        let status = resp.status();
        let cookie = resp.header("set-cookie").and_then(cookie_pair);
        let body = resp.text();
        if !resp.ok() {
            let shown: String = body.chars().take(300).collect();
            return Err(ApiError { status, message: format!("HTTP {status}: {shown}") });
        }
        serde_json::from_str(&body)
            .map(|v| (v, cookie))
            .map_err(|e| ApiError { status, message: format!("bad JSON: {e}") })
    }

    /// Redeems the launch ticket (native) and learns the CSRF token and workspace.
    pub async fn connect(&self) -> Result<Bootstrap, ApiError> {
        let auth = self.snapshot();
        let (value, cookie) = if let Some(ticket) = auth.launch.ticket.clone() {
            let body = serde_json::json!({ "ticket": ticket }).to_string();
            let req = HttpRequest::post(format!("{}ticket/redeem", auth.launch.base), body);
            let req = self.decorate(req, &Auth { csrf: None, ..auth.clone() }, true);
            Self::send(req).await?
        } else {
            let req = self.decorate(HttpRequest::get(format!("{}bootstrap", auth.launch.base)), &auth, false);
            Self::send(req).await?
        };
        let (csrf, workspace) = parse_bootstrap(&value)
            .ok_or(ApiError { status: 0, message: "bootstrap response had no csrf token".into() })?;
        self.auth.update(|a| {
            a.csrf = Some(csrf);
            // Only the native launch stores the cookie itself; the browser keeps its own.
            if a.launch.ticket.is_some() && cookie.is_some() {
                a.cookie = cookie;
            }
        });
        Ok(Bootstrap { workspace })
    }

    /// `GET <base><path>` (path like `session-view?session=ID`).
    pub async fn get_json(&self, path: &str) -> Result<Value, ApiError> {
        let auth = self.snapshot();
        let req = self.decorate(HttpRequest::get(format!("{}{path}", auth.launch.base)), &auth, false);
        Self::send(req).await.map(|(v, _)| v)
    }

    /// `POST <base>command` with a host command object. Sent once, never retried.
    pub async fn command(&self, command: Value) -> Result<Value, ApiError> {
        let auth = self.snapshot();
        let req = HttpRequest::post(format!("{}command", auth.launch.base), command.to_string());
        let req = self.decorate(req, &auth, true);
        Self::send(req).await.map(|(v, _)| v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cookie_pair_drops_attributes() {
        assert_eq!(cookie_pair("nexus_web=abc; HttpOnly; Path=/").as_deref(), Some("nexus_web=abc"));
        assert_eq!(cookie_pair("junk"), None);
    }

    #[test]
    fn bootstrap_needs_csrf() {
        let v = serde_json::json!({"csrf":"t","workspace":"/w"});
        assert_eq!(parse_bootstrap(&v), Some(("t".into(), "/w".into())));
        assert_eq!(parse_bootstrap(&serde_json::json!({})), None);
    }
}

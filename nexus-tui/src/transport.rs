//! HTTP and SSE transport for the Core API.
//!
//! This layer knows bearer auth, JSON bodies, `Idempotency-Key` headers, error
//! classification and the SSE wire format. It knows nothing about projects,
//! sessions or the client model (see `api` and `reduce`).
//!
//! It mirrors `nexus-core/nexus/client.py`: keyed mutations are retried on a
//! transport failure with the same key (three attempts), an HTTP error is never
//! retried here, and the event stream is opened with `Last-Event-ID`.

use std::pin::Pin;
use std::time::Duration;

use bytes::Bytes;
use futures_util::{Stream, StreamExt};
use reqwest::Method;
use serde_json::Value;

/// Error codes the server sends when an event cursor cannot be resumed.
pub const RESYNC_CODES: [&str; 4] = [
    "invalid_cursor",
    "cursor_generation",
    "cursor_ahead",
    "history_unavailable",
];

/// An HTTP error response (status >= 400) with the server's JSON body, if any.
#[derive(Debug, Clone, PartialEq)]
pub struct ApiError {
    pub status: u16,
    pub message: String,
    pub code: Option<String>,
    pub body: Value,
}

impl ApiError {
    pub fn from_body(status: u16, text: &str) -> Self {
        match serde_json::from_str::<Value>(text) {
            Ok(body) if body.is_object() => {
                let message = body
                    .get("error")
                    .and_then(Value::as_str)
                    .map_or_else(|| clip(text), str::to_owned);
                let code = body.get("code").and_then(Value::as_str).map(str::to_owned);
                Self {
                    status,
                    message,
                    code,
                    body,
                }
            }
            _ => Self {
                status,
                message: clip(text),
                code: None,
                body: Value::Null,
            },
        }
    }

    pub fn is_auth(&self) -> bool {
        self.status == 401
    }

    /// The event cursor cannot be resumed: reload state from the snapshot.
    pub fn is_resync(&self) -> bool {
        self.body
            .get("resync")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            || self
                .code
                .as_deref()
                .is_some_and(|c| RESYNC_CODES.contains(&c))
    }

    /// 409 `revision_conflict`: the current revision, if the server reported it.
    pub fn revision_conflict(&self) -> Option<Option<i64>> {
        (self.status == 409 && self.code.as_deref() == Some("revision_conflict")).then(|| {
            self.body
                .get("current_revision")
                .or_else(|| self.body.get("revision"))
                .and_then(Value::as_i64)
        })
    }

    /// 409 `idempotency_key_reused`: the key was used for a different request.
    pub fn key_reused(&self) -> bool {
        self.status == 409 && self.code.as_deref() == Some("idempotency_key_reused")
    }
}

fn clip(text: &str) -> String {
    text.chars().take(500).collect()
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "HTTP {}: {}", self.status, self.message)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Error {
    /// Connection refused, reset, timed out, TLS, or a broken stream.
    Network(String),
    /// The server answered with an error status.
    Api(ApiError),
    /// A success response that could not be understood.
    Decode(String),
}

impl Error {
    pub fn api(&self) -> Option<&ApiError> {
        match self {
            Error::Api(e) => Some(e),
            _ => None,
        }
    }
    pub fn is_auth(&self) -> bool {
        self.api().is_some_and(ApiError::is_auth)
    }
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Network(m) => write!(f, "network error: {m}"),
            Error::Api(e) => e.fmt(f),
            Error::Decode(m) => write!(f, "unexpected response: {m}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<reqwest::Error> for Error {
    fn from(e: reqwest::Error) -> Self {
        Error::Network(e.without_url().to_string())
    }
}

/// A successful JSON reply.
#[derive(Debug, Clone, PartialEq)]
pub struct Reply {
    pub status: u16,
    /// `Idempotent-Replayed: true`: the server replayed an earlier acceptance.
    pub replayed: bool,
    /// `Value::Null` for an empty body (204).
    pub body: Value,
}

/// Bounded reconnect delays; the last entry repeats. Mirrors the Python client.
#[derive(Debug, Clone)]
pub struct Backoff {
    steps: Vec<Duration>,
    failures: usize,
}

impl Backoff {
    pub fn standard() -> Self {
        Self::new(&[0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0])
    }
    pub fn new(seconds: &[f64]) -> Self {
        Self {
            steps: seconds
                .iter()
                .map(|s| Duration::from_secs_f64(*s))
                .collect(),
            failures: 0,
        }
    }
    /// The delay before the next attempt; counts a failure.
    pub fn next_delay(&mut self) -> Duration {
        let index = self.failures.min(self.steps.len().saturating_sub(1));
        self.failures += 1;
        self.steps.get(index).copied().unwrap_or_default()
    }
    pub fn reset(&mut self) {
        self.failures = 0;
    }
    pub fn failures(&self) -> usize {
        self.failures
    }
}

#[derive(Clone)]
pub struct Http {
    base: String,
    token: String,
    calls: reqwest::Client,
    streams: reqwest::Client,
}

impl Http {
    pub fn new(base: &str, token: &str) -> Result<Self, Error> {
        let calls = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(60))
            .build()?;
        let streams = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .build()?;
        Ok(Self {
            base: base.trim_end_matches('/').to_owned(),
            token: token.to_owned(),
            calls,
            streams,
        })
    }

    pub fn base(&self) -> &str {
        &self.base
    }

    /// One JSON API call. With a key, a transport failure is retried (three
    /// attempts) with the *same* key so the server can replay the acceptance.
    pub async fn call(
        &self,
        method: Method,
        path: &str,
        query: &[(&str, String)],
        body: Option<&Value>,
        key: Option<&str>,
        if_match: Option<i64>,
    ) -> Result<Reply, Error> {
        let attempts = if key.is_some() { 3 } else { 1 };
        let mut last = None;
        for attempt in 1..=attempts {
            let mut req = self
                .calls
                .request(method.clone(), format!("{}{}", self.base, path))
                .bearer_auth(&self.token)
                .query(query);
            if let Some(key) = key {
                req = req.header("idempotency-key", key);
            }
            if let Some(rev) = if_match {
                req = req.header("if-match", rev.to_string());
            }
            if let Some(body) = body {
                req = req.json(body);
            }
            match req.send().await {
                Ok(resp) => return read_reply(resp).await,
                Err(e) => {
                    last = Some(Error::from(e));
                    if attempt < attempts {
                        tokio::time::sleep(Duration::from_millis(250 * attempt)).await;
                    }
                }
            }
        }
        Err(last.unwrap_or_else(|| Error::Network("no attempt made".into())))
    }

    pub async fn get(&self, path: &str, query: &[(&str, String)]) -> Result<Value, Error> {
        Ok(self
            .call(Method::GET, path, query, None, None, None)
            .await?
            .body)
    }

    /// A GET whose body is not JSON (a session export). Returns the body and
    /// the suggested filename from `content-disposition`, when present.
    pub async fn get_text(
        &self,
        path: &str,
        query: &[(&str, String)],
    ) -> Result<(String, Option<String>), Error> {
        let resp = self
            .calls
            .get(format!("{}{}", self.base, path))
            .bearer_auth(&self.token)
            .query(query)
            .send()
            .await?;
        let status = resp.status().as_u16();
        let name = resp
            .headers()
            .get("content-disposition")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| {
                v.split("filename=")
                    .nth(1)
                    .map(|f| f.trim_matches('"').to_owned())
            });
        let text = resp.text().await?;
        if status >= 400 {
            return Err(Error::Api(ApiError::from_body(status, &text)));
        }
        Ok((text, name))
    }

    /// Open `GET /api/event`. The cursor is sent as `Last-Event-ID` like the
    /// Python client. A cursor failure arrives as an `Error::Api` with
    /// `is_resync()`; no stream is returned.
    pub async fn events(
        &self,
        session: Option<&str>,
        cursor: Option<&str>,
    ) -> Result<EventStream, Error> {
        let mut req = self
            .streams
            .get(format!("{}/api/event", self.base))
            .bearer_auth(&self.token)
            .header("accept", "text/event-stream");
        if let Some(s) = session {
            req = req.query(&[("session", s)]);
        }
        if let Some(c) = cursor {
            req = req.header("last-event-id", c);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        if status >= 400 {
            let text = resp.text().await.unwrap_or_default();
            return Err(Error::Api(ApiError::from_body(status, &text)));
        }
        let bytes = resp.bytes_stream().map(|r| r.map_err(Error::from));
        Ok(EventStream::new(bytes))
    }
}

async fn read_reply(resp: reqwest::Response) -> Result<Reply, Error> {
    let status = resp.status().as_u16();
    let replayed = resp
        .headers()
        .get("idempotent-replayed")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("true"));
    let text = resp.text().await?;
    if status >= 400 {
        return Err(Error::Api(ApiError::from_body(status, &text)));
    }
    let body = if text.trim().is_empty() {
        Value::Null
    } else {
        serde_json::from_str(&text).map_err(|e| Error::Decode(e.to_string()))?
    };
    Ok(Reply {
        status,
        replayed,
        body,
    })
}

/// One SSE frame as it appeared on the wire.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct SseFrame {
    pub event: String,
    pub id: Option<String>,
    pub data: String,
}

/// Incremental SSE parser: feed bytes in any chunking, get complete frames.
/// Handles LF/CRLF/CR line ends, comments, multi-line `data:` and a leading
/// BOM. A frame without data is dropped, as browsers do.
#[derive(Debug, Default)]
pub struct SseParser {
    buf: Vec<u8>,
    event: Option<String>,
    id: Option<String>,
    data: Vec<String>,
    skip_lf: bool,
    started: bool,
}

impl SseParser {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn feed(&mut self, chunk: &[u8]) -> Vec<SseFrame> {
        let mut out = Vec::new();
        for &b in chunk {
            if self.skip_lf {
                self.skip_lf = false;
                if b == b'\n' {
                    continue;
                }
            }
            match b {
                b'\n' => self.line(&mut out),
                b'\r' => {
                    self.skip_lf = true;
                    self.line(&mut out);
                }
                _ => self.buf.push(b),
            }
        }
        out
    }

    fn line(&mut self, out: &mut Vec<SseFrame>) {
        let raw = std::mem::take(&mut self.buf);
        let mut line = String::from_utf8_lossy(&raw).into_owned();
        if !self.started {
            self.started = true;
            if let Some(rest) = line.strip_prefix('\u{feff}') {
                line = rest.to_owned();
            }
        }
        if line.is_empty() {
            if !self.data.is_empty() {
                out.push(SseFrame {
                    event: self.event.take().unwrap_or_else(|| "message".into()),
                    id: self.id.take(),
                    data: self.data.join("\n"),
                });
            }
            self.event = None;
            self.id = None;
            self.data.clear();
            return;
        }
        if line.starts_with(':') {
            return;
        }
        let (field, value) = match line.split_once(':') {
            Some((f, v)) => (f, v.strip_prefix(' ').unwrap_or(v)),
            None => (line.as_str(), ""),
        };
        match field {
            "event" => self.event = Some(value.to_owned()),
            "id" => self.id = Some(value.to_owned()),
            "data" => self.data.push(value.to_owned()),
            _ => {}
        }
    }
}

type ByteStream = Pin<Box<dyn Stream<Item = Result<Bytes, Error>> + Send>>;

/// A parsed SSE response. `next` yields frames until the server ends the
/// stream (`Ok(None)`) or the connection breaks (`Err(Network)`).
pub struct EventStream {
    inner: ByteStream,
    parser: SseParser,
    ready: std::collections::VecDeque<SseFrame>,
}

impl EventStream {
    pub fn new(stream: impl Stream<Item = Result<Bytes, Error>> + Send + 'static) -> Self {
        Self {
            inner: Box::pin(stream),
            parser: SseParser::new(),
            ready: Default::default(),
        }
    }

    pub async fn next(&mut self) -> Result<Option<SseFrame>, Error> {
        loop {
            if let Some(frame) = self.ready.pop_front() {
                return Ok(Some(frame));
            }
            match self.inner.next().await {
                Some(Ok(chunk)) => self.ready.extend(self.parser.feed(&chunk)),
                Some(Err(e)) => return Err(e),
                None => return Ok(None),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frames(chunks: &[&str]) -> Vec<SseFrame> {
        let mut p = SseParser::new();
        chunks.iter().flat_map(|c| p.feed(c.as_bytes())).collect()
    }

    #[test]
    fn parses_frames_split_at_any_byte() {
        let text = "event: a\nid: g:1\ndata: {\"x\":1}\n\n: keep\nevent: b\ndata: l1\ndata: l2\n\n";
        let whole = frames(&[text]);
        assert_eq!(whole.len(), 2);
        assert_eq!(
            whole[0],
            SseFrame {
                event: "a".into(),
                id: Some("g:1".into()),
                data: "{\"x\":1}".into()
            }
        );
        assert_eq!(whole[1].data, "l1\nl2");
        let mut p = SseParser::new();
        let mut split = Vec::new();
        for b in text.as_bytes() {
            split.extend(p.feed(&[*b]));
        }
        assert_eq!(split, whole);
    }

    #[test]
    fn handles_crlf_cr_and_bom() {
        let f = frames(&[
            "\u{feff}event: a\r\ndata: 1\r\n\r\n",
            "event: b\rdata: 2\r\r",
        ]);
        assert_eq!(
            f.iter()
                .map(|f| (f.event.as_str(), f.data.as_str()))
                .collect::<Vec<_>>(),
            [("a", "1"), ("b", "2")]
        );
    }

    #[test]
    fn crlf_split_between_chunks_is_one_line_end() {
        let f = frames(&["event: a\r", "\ndata: 1\r", "\n\r", "\n"]);
        assert_eq!(f.len(), 1);
        assert_eq!(f[0].data, "1");
    }

    #[test]
    fn drops_data_less_frames_and_defaults_event_name() {
        let f = frames(&["event: ping\n\ndata: x\n\n"]);
        assert_eq!(f.len(), 1);
        assert_eq!(f[0].event, "message");
        assert_eq!(f[0].id, None);
    }

    #[test]
    fn error_classification() {
        let e = ApiError::from_body(
            409,
            r#"{"error":"stale","code":"revision_conflict","revision":4,"current_revision":4}"#,
        );
        assert_eq!(e.revision_conflict(), Some(Some(4)));
        assert!(!e.is_resync());
        let r = ApiError::from_body(
            410,
            r#"{"error":"gone","code":"history_unavailable","resync":true}"#,
        );
        assert!(r.is_resync());
        let r = ApiError::from_body(400, r#"{"error":"bad","code":"invalid_cursor"}"#);
        assert!(r.is_resync());
        assert!(ApiError::from_body(401, "nope").is_auth());
        assert_eq!(ApiError::from_body(500, "plain text").message, "plain text");
        assert!(
            ApiError::from_body(409, r#"{"code":"idempotency_key_reused","error":"x"}"#)
                .key_reused()
        );
    }

    #[test]
    fn backoff_is_bounded_and_resets() {
        let mut b = Backoff::new(&[1.0, 2.0]);
        let d: Vec<_> = (0..4).map(|_| b.next_delay().as_secs()).collect();
        assert_eq!(d, [1, 2, 2, 2]);
        b.reset();
        assert_eq!(b.next_delay().as_secs(), 1);
    }

    #[tokio::test]
    async fn event_stream_yields_frames_then_ends() {
        let chunks: Vec<Result<Bytes, Error>> = vec![
            Ok(Bytes::from(
                "event: server.connected\ndata: {}\n\nid: g:1\nev",
            )),
            Ok(Bytes::from("ent: x\ndata: {}\n\n")),
            Err(Error::Network("reset".into())),
        ];
        let mut s = EventStream::new(futures_util::stream::iter(chunks));
        assert_eq!(s.next().await.unwrap().unwrap().event, "server.connected");
        let f = s.next().await.unwrap().unwrap();
        assert_eq!((f.event.as_str(), f.id.as_deref()), ("x", Some("g:1")));
        assert!(matches!(s.next().await, Err(Error::Network(_))));
    }
}

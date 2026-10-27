//! Keyed mutations and revision conflicts against a scripted loopback server.

use std::sync::{Arc, Mutex};

use nexus_tui::api::{CoreApi, Outcome, RequestId};
use nexus_tui::transport::{Error, Http};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const REVISION_CONFLICT: &str = include_str!("../fixtures/real/revision_conflict.json");

/// `None` closes the connection without answering (a lost response).
type Canned = Option<(u16, Vec<(&'static str, &'static str)>, String)>;

#[derive(Debug, Clone)]
struct Seen {
    request_line: String,
    head: String,
    body: String,
}

impl Seen {
    fn header(&self, name: &str) -> Option<String> {
        self.head.lines().find_map(|l| {
            let (k, v) = l.split_once(':')?;
            k.eq_ignore_ascii_case(name).then(|| v.trim().to_owned())
        })
    }
}

async fn server(script: Vec<Canned>) -> (String, Arc<Mutex<Vec<Seen>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let script = Arc::new(Mutex::new(
        script
            .into_iter()
            .collect::<std::collections::VecDeque<_>>(),
    ));
    let log = seen.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = listener.accept().await else {
                return;
            };
            let (script, log) = (script.clone(), log.clone());
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                let (head, body) = loop {
                    let n = sock.read(&mut chunk).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&buf[..i]).to_string();
                        let want: usize = head
                            .lines()
                            .find_map(|l| {
                                l.to_ascii_lowercase()
                                    .strip_prefix("content-length:")
                                    .map(|v| v.trim().parse().unwrap())
                            })
                            .unwrap_or(0);
                        if buf.len() >= i + 4 + want {
                            break (
                                head,
                                String::from_utf8_lossy(&buf[i + 4..i + 4 + want]).to_string(),
                            );
                        }
                    }
                };
                log.lock().unwrap().push(Seen {
                    request_line: head.lines().next().unwrap().into(),
                    head,
                    body,
                });
                let next = script.lock().unwrap().pop_front().flatten();
                if let Some((status, headers, body)) = next {
                    let extra: String = headers
                        .iter()
                        .map(|(k, v)| format!("{k}: {v}\r\n"))
                        .collect();
                    let resp = format!(
                        "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n{extra}\r\n{body}",
                        body.len()
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                }
                // dropping the socket closes it
            });
        }
    });
    (url, seen)
}

fn api(url: &str) -> CoreApi {
    CoreApi::new(Http::new(url, "tok").unwrap())
}

const SESSION: &str = r#"{"id":"ses_1","project_id":"prj_1","title":"T","revision":2,"status":"idle","archived":false}"#;

#[tokio::test]
async fn a_lost_response_is_retried_with_the_same_key_and_replay_is_reported() {
    let (url, seen) = server(vec![
        None, // response lost
        Some((
            202,
            vec![("Idempotent-Replayed", "true")],
            r#"{"status":"busy","turn_id":"trn_1","origin":"prompt"}"#.into(),
        )),
    ])
    .await;
    let key = RequestId::new();
    let out = api(&url).prompt("ses_1", "hi", &key).await.unwrap();
    let Outcome::Done { value, replayed } = out else {
        panic!("{out:?}")
    };
    assert!(replayed);
    assert_eq!(value["turn_id"], "trn_1");
    let seen = seen.lock().unwrap();
    assert_eq!(seen.len(), 2);
    assert!(seen
        .iter()
        .all(|s| s.header("idempotency-key").as_deref() == Some(key.as_str())));
    assert!(seen
        .iter()
        .all(|s| s.header("authorization").as_deref() == Some("Bearer tok")));
    assert!(seen[0]
        .request_line
        .starts_with("POST /api/session/ses_1/prompt"));
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&seen[1].body).unwrap(),
        json!({"text": "hi"})
    );
}

#[tokio::test]
async fn model_and_agent_changes_carry_the_expected_revision() {
    let (url, seen) = server(vec![
        Some((200, vec![], SESSION.into())),
        Some((200, vec![], SESSION.into())),
    ])
    .await;
    let api = api(&url);
    api.set_model_rev("ses_1", "codex/gpt-6", Some(2), &RequestId::new())
        .await
        .unwrap();
    api.set_agent_rev("ses_1", "scout", Some(2), &RequestId::new())
        .await
        .unwrap();
    let seen = seen.lock().unwrap();
    assert!(seen[0]
        .request_line
        .starts_with("POST /api/session/ses_1/model"));
    assert!(seen[1]
        .request_line
        .starts_with("POST /api/session/ses_1/agent"));
    assert!(seen
        .iter()
        .all(|s| s.header("if-match").as_deref() == Some("2")));
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&seen[0].body).unwrap(),
        json!({"model": "codex/gpt-6"})
    );
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&seen[1].body).unwrap(),
        json!({"agent": "scout"})
    );
}

#[tokio::test]
async fn an_export_keeps_the_markdown_body_and_suggested_name() {
    let (url, seen) = server(vec![Some((
        200,
        vec![(
            "content-disposition",
            "attachment; filename=\"nexus-ses_1-session.md\"",
        )],
        "# Transcript\n".into(),
    ))])
    .await;
    let (text, name) = api(&url).export("ses_1", "markdown", "tree").await.unwrap();
    assert_eq!(text, "# Transcript\n");
    assert_eq!(name.as_deref(), Some("nexus-ses_1-session.md"));
    let seen = seen.lock().unwrap();
    assert!(seen[0]
        .request_line
        .starts_with("GET /api/session/ses_1/export"));
    assert!(seen[0].request_line.contains("format=markdown"));
    assert!(seen[0].request_line.contains("scope=tree"));
}

#[tokio::test]
async fn an_http_error_is_never_retried() {
    let (url, seen) = server(vec![Some((
        409,
        vec![],
        r#"{"error":"a turn is already running"}"#.into(),
    ))])
    .await;
    let err = api(&url)
        .prompt("ses_1", "hi", &RequestId::new())
        .await
        .unwrap_err();
    assert_eq!(err.api().unwrap().status, 409);
    assert_eq!(seen.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn a_stale_revision_is_a_conflict_with_the_current_revision() {
    let (url, seen) = server(vec![Some((409, vec![], REVISION_CONFLICT.into()))]).await;
    let out = api(&url)
        .rename_project("prj_1", "x", Some(99), &RequestId::new())
        .await
        .unwrap();
    match out {
        Outcome::Conflict {
            current_revision,
            message,
        } => {
            assert_eq!(current_revision, Some(2));
            assert!(message.contains("revision 2"));
        }
        other => panic!("{other:?}"),
    }
    let seen = seen.lock().unwrap();
    assert_eq!(seen[0].header("if-match").as_deref(), Some("99"));
    assert!(seen[0].request_line.starts_with("PATCH /api/project/prj_1"));
}

#[tokio::test]
async fn a_successful_keyed_update_decodes_the_session() {
    let (url, _) = server(vec![Some((200, vec![], SESSION.into()))]).await;
    let out = api(&url)
        .patch_session("ses_1", json!({"title": "T"}), Some(1), &RequestId::new())
        .await
        .unwrap();
    let Outcome::Done { value, replayed } = out else {
        panic!()
    };
    assert!(!replayed);
    assert_eq!((value.title.as_str(), value.revision), ("T", 2));
}

#[tokio::test]
async fn a_reused_key_is_a_distinct_error_not_a_conflict() {
    let body = r#"{"error":"key reused","code":"idempotency_key_reused"}"#;
    let (url, _) = server(vec![Some((409, vec![], body.into()))]).await;
    let err = api(&url)
        .interrupt("ses_1", &RequestId::new())
        .await
        .unwrap_err();
    assert!(matches!(&err, Error::Api(e) if e.key_reused()));
}

#[tokio::test]
async fn an_empty_204_is_success() {
    let (url, _) = server(vec![Some((204, vec![], String::new()))]).await;
    let out = api(&url)
        .delete_session("ses_1", &RequestId::new())
        .await
        .unwrap();
    assert!(matches!(
        out,
        Outcome::Done {
            replayed: false,
            ..
        }
    ));
}

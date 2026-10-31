//! Supervisor integration tests against a scripted mock Core (a tiny HTTP/1.1
//! server on loopback). Bodies come from `fixtures/` (real daemon captures and
//! documented shapes).

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use nexus_tui::api::{Compat, UpdateSide};
use nexus_tui::state::{AppState, ConnectionState};
use nexus_tui::sync::{self, Command, ResyncCause, SyncConfig, SyncHandle, SyncMsg};
use nexus_tui::transport::Backoff;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const INFO: &str = include_str!("../fixtures/real/info.json");
const SNAPSHOT: &str = include_str!("../fixtures/real/snapshot.json");
const CURSOR_GENERATION: &str = include_str!("../fixtures/real/cursor_generation.json");
const GEN: &str = "log_c915bfc9b6d826c4";

enum Script {
    /// Send these frames, then close the connection.
    Close(String),
    /// Send these frames and keep the stream open.
    Hold(String),
    /// Answer with an error status and JSON body.
    Status(u16, String),
}

#[derive(Default)]
struct Inner {
    token: String,
    info: String,
    snapshot: String,
    scripts: VecDeque<Script>,
    /// JSON bodies for read endpoints the test scripts, keyed by path.
    routes: HashMap<String, String>,
    /// (path, last-event-id) of every request received.
    requests: Vec<(String, Option<String>)>,
}

#[derive(Clone)]
struct Mock {
    url: String,
    inner: Arc<Mutex<Inner>>,
}

impl Mock {
    async fn start(token: &str) -> Mock {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let inner = Arc::new(Mutex::new(Inner {
            token: token.into(),
            info: INFO.into(),
            snapshot: SNAPSHOT.into(),
            ..Default::default()
        }));
        let shared = inner.clone();
        tokio::spawn(async move {
            loop {
                let Ok((sock, _)) = listener.accept().await else {
                    return;
                };
                tokio::spawn(serve(sock, shared.clone()));
            }
        });
        Mock { url, inner }
    }
    fn script(&self, s: Script) {
        self.inner.lock().unwrap().scripts.push_back(s);
    }
    fn set_info(&self, v: Value) {
        self.inner.lock().unwrap().info = v.to_string();
    }
    fn set_token(&self, t: &str) {
        self.inner.lock().unwrap().token = t.into();
    }
    fn route(&self, path: &str, body: Value) {
        self.inner
            .lock()
            .unwrap()
            .routes
            .insert(path.into(), body.to_string());
    }
    fn count(&self, path: &str) -> usize {
        self.inner
            .lock()
            .unwrap()
            .requests
            .iter()
            .filter(|(p, _)| p == path)
            .count()
    }
    fn event_cursors(&self) -> Vec<Option<String>> {
        let g = self.inner.lock().unwrap();
        g.requests
            .iter()
            .filter(|(p, _)| p == "/api/event")
            .map(|(_, c)| c.clone())
            .collect()
    }
    fn paths(&self) -> Vec<String> {
        self.inner
            .lock()
            .unwrap()
            .requests
            .iter()
            .map(|(p, _)| p.clone())
            .collect()
    }
}

async fn serve(mut sock: tokio::net::TcpStream, inner: Arc<Mutex<Inner>>) {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 2048];
    while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
        match sock.read(&mut chunk).await {
            Ok(0) | Err(_) => return,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
        }
    }
    let head = String::from_utf8_lossy(&buf).to_string();
    let mut lines = head.lines();
    let path = lines
        .next()
        .unwrap_or_default()
        .split_whitespace()
        .nth(1)
        .unwrap_or("/")
        .to_owned();
    let path = path.split('?').next().unwrap().to_owned();
    let header = |name: &str| {
        head.lines().find_map(|l| {
            let (k, v) = l.split_once(':')?;
            k.eq_ignore_ascii_case(name).then(|| v.trim().to_owned())
        })
    };
    let auth = header("authorization");
    let last_id = header("last-event-id");
    let (status, ctype, body, hold): (u16, &str, String, bool) = {
        let mut g = inner.lock().unwrap();
        g.requests.push((path.clone(), last_id));
        if auth.as_deref() != Some(&format!("Bearer {}", g.token)) {
            (
                401,
                "application/json",
                r#"{"error":"unauthorized"}"#.into(),
                false,
            )
        } else if let Some(body) = g.routes.get(&path) {
            (200, "application/json", body.clone(), false)
        } else {
            match path.as_str() {
                "/api/info" => (200, "application/json", g.info.clone(), false),
                "/api/snapshot" => (200, "application/json", g.snapshot.clone(), false),
                p if p.starts_with("/api/session/") && p.ends_with("/message") => (
                    200,
                    "application/json",
                    r#"{"messages":[],"more":false}"#.into(),
                    false,
                ),
                "/api/event" => match g.scripts.pop_front() {
                    Some(Script::Close(b)) => (200, "text/event-stream", b, false),
                    Some(Script::Hold(b)) => (200, "text/event-stream", b, true),
                    Some(Script::Status(s, b)) => (s, "application/json", b, false),
                    None => (200, "text/event-stream", String::new(), true),
                },
                _ => (
                    404,
                    "application/json",
                    r#"{"error":"not found"}"#.into(),
                    false,
                ),
            }
        }
    };
    let head = format!("HTTP/1.1 {status} X\r\ncontent-type: {ctype}\r\nconnection: close\r\n\r\n");
    let _ = sock.write_all(head.as_bytes()).await;
    let _ = sock.write_all(body.as_bytes()).await;
    let _ = sock.flush().await;
    if hold {
        tokio::time::sleep(Duration::from_secs(30)).await;
    }
}

fn connected(head: i64, resumed: Option<i64>) -> String {
    let d = json!({"version": "0.10.0.dev0", "generation": GEN, "head": head, "head_cursor": format!("{GEN}:{head}"),
                   "resumed_from": resumed, "session": null, "floor": 0});
    format!("event: server.connected\ndata: {d}\n\n")
}

fn project_updated(id: i64, name: &str, revision: i64) -> String {
    let d = json!({"id": id, "seq": id, "time": 1.0, "type": "project.updated", "session_id": null,
                   "cursor": format!("{GEN}:{id}"),
                   "data": {"id": "prj_1a17de160d3c070dfda", "path": "/work", "name": name, "revision": revision}});
    format!("id: {GEN}:{id}\nevent: project.updated\ndata: {d}\n\n")
}

fn write_daemon(home: &std::path::Path, url: &str, pid: i64, token: &str) {
    let tmp = home.join("daemon.json.tmp");
    std::fs::write(
        &tmp,
        json!({"url": url, "pid": pid, "token": token, "started": 1.0}).to_string(),
    )
    .unwrap();
    std::fs::rename(tmp, home.join("daemon.json")).unwrap();
}

fn temp_home(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("nexus-tui-test-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn config(home: &std::path::Path) -> SyncConfig {
    let mut cfg = SyncConfig::new(home.to_owned());
    cfg.bootstrap = false;
    cfg.backoff = Backoff::new(&[0.02]);
    cfg.park_interval = Duration::from_millis(30);
    cfg
}

/// Drive the supervisor into an `AppState` until `done` holds (10 s limit).
async fn drive(
    h: &mut SyncHandle,
    state: &mut AppState,
    log: &mut Vec<SyncMsg>,
    done: impl Fn(&AppState, &[SyncMsg]) -> bool,
) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    while !done(state, log) {
        let msg = tokio::time::timeout_at(deadline, h.messages.recv())
            .await
            .expect("timed out")
            .expect("channel closed");
        log.push(msg.clone());
        state.apply(msg);
    }
}

fn states(log: &[SyncMsg]) -> Vec<String> {
    log.iter()
        .filter_map(|m| match m {
            SyncMsg::Connection(c) => Some(c.label().to_owned()),
            _ => None,
        })
        .collect()
}

#[tokio::test]
async fn connects_snapshots_then_streams_after_the_watermark() {
    let mock = Mock::start("tok").await;
    let home = temp_home("live");
    write_daemon(&home, &mock.url, 10, "tok");
    mock.script(Script::Hold(format!(
        "{}{}",
        connected(4, Some(3)),
        project_updated(4, "renamed", 2)
    )));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| s.events_applied == 1).await;

    assert_eq!(states(&log), ["connecting", "live"]);
    assert!(matches!(
        &log[1],
        SyncMsg::Handshake {
            compat: Compat::Unreported,
            ..
        }
    ));
    assert!(matches!(
        &log[2],
        SyncMsg::Snapshot {
            cause: ResyncCause::Initial,
            ..
        }
    ));
    assert!(st.connection.is_live());
    assert_eq!(st.model.projects["prj_1a17de160d3c070dfda"].name, "renamed");
    // The stream resumed strictly after the snapshot watermark.
    assert_eq!(mock.event_cursors(), [Some(format!("{GEN}:3"))]);
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn reconnect_resumes_from_the_last_applied_cursor_without_a_snapshot() {
    let mock = Mock::start("tok").await;
    let home = temp_home("reconnect");
    write_daemon(&home, &mock.url, 10, "tok");
    // First connection delivers event 4 and drops; the second replays 4 (dedup) and 5.
    mock.script(Script::Close(format!(
        "{}{}",
        connected(4, Some(3)),
        project_updated(4, "a", 2)
    )));
    mock.script(Script::Hold(format!(
        "{}{}{}",
        connected(5, Some(4)),
        project_updated(4, "a", 2),
        project_updated(5, "b", 3)
    )));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| s.events_applied == 2).await;

    assert_eq!(
        mock.count("/api/snapshot"),
        1,
        "a plain reconnect needs no resync"
    );
    assert_eq!(
        mock.event_cursors(),
        [Some(format!("{GEN}:3")), Some(format!("{GEN}:4"))]
    );
    assert_eq!(st.duplicates, 1);
    assert_eq!(st.model.projects["prj_1a17de160d3c070dfda"].name, "b");
    assert!(states(&log).contains(&"reconnecting".to_owned()));
    assert!(st.connection.is_live());
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn a_rejected_cursor_resyncs_from_a_fresh_snapshot() {
    let mock = Mock::start("tok").await;
    let home = temp_home("cursor");
    write_daemon(&home, &mock.url, 10, "tok");
    mock.script(Script::Status(409, CURSOR_GENERATION.into()));
    mock.script(Script::Hold(connected(3, Some(3))));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| {
        s.connection.is_live() && s.snapshots_applied == 2
    })
    .await;

    assert_eq!(mock.count("/api/snapshot"), 2);
    let causes: Vec<_> = log
        .iter()
        .filter_map(|m| {
            if let SyncMsg::Snapshot { cause, .. } = m {
                Some(cause.clone())
            } else {
                None
            }
        })
        .collect();
    assert_eq!(
        causes,
        [
            ResyncCause::Initial,
            ResyncCause::Cursor("cursor_generation".into())
        ]
    );
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn overflow_triggers_a_full_resync() {
    let mock = Mock::start("tok").await;
    let home = temp_home("overflow");
    write_daemon(&home, &mock.url, 10, "tok");
    let overflow = include_str!("../fixtures/documented/overflow.sse");
    mock.script(Script::Hold(format!(
        "{}{}",
        connected(3, Some(3)),
        overflow
    )));
    mock.script(Script::Hold(connected(3, Some(3))));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| {
        s.snapshots_applied == 2 && s.connection.is_live()
    })
    .await;
    assert!(matches!(
        &log.iter()
            .rev()
            .find_map(|m| if let SyncMsg::Snapshot { cause, .. } = m {
                Some(cause)
            } else {
                None
            }),
        Some(ResyncCause::Overflow)
    ));
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn a_restarted_daemon_rereads_the_token_and_resyncs() {
    let mock = Mock::start("tok-1").await;
    let home = temp_home("restart");
    write_daemon(&home, &mock.url, 10, "tok-1");
    mock.script(Script::Close(connected(3, Some(3))));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| s.connection.is_live()).await;

    // The daemon restarts: new pid and token, same address. The old token now fails.
    mock.set_token("tok-2");
    write_daemon(&home, &mock.url, 11, "tok-2");
    mock.script(Script::Hold(connected(3, Some(3))));
    drive(&mut h, &mut st, &mut log, |s, _| {
        s.snapshots_applied == 2 && s.connection.is_live()
    })
    .await;

    let last = log.iter().rev().find_map(|m| {
        if let SyncMsg::Snapshot { cause, .. } = m {
            Some(cause.clone())
        } else {
            None
        }
    });
    assert_eq!(last, Some(ResyncCause::DaemonRestarted));
    assert!(st
        .banners
        .contains(&nexus_tui::state::Banner::DaemonRestarted));
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn auth_failure_waits_for_a_new_token() {
    let mock = Mock::start("right").await;
    let home = temp_home("auth");
    write_daemon(&home, &mock.url, 10, "wrong");
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| {
        matches!(s.connection, ConnectionState::AuthFailed { .. })
    })
    .await;
    assert_eq!(
        mock.count("/api/snapshot"),
        0,
        "nothing but the handshake is sent while unauthenticated"
    );

    mock.script(Script::Hold(connected(3, Some(3))));
    write_daemon(&home, &mock.url, 10, "right");
    drive(&mut h, &mut st, &mut log, |s, _| s.connection.is_live()).await;
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn a_different_api_major_halts_before_any_other_request() {
    let mock = Mock::start("tok").await;
    let home = temp_home("major");
    write_daemon(&home, &mock.url, 10, "tok");
    let mut info: Value = serde_json::from_str(INFO).unwrap();
    info["api_version"] = json!("2.0");
    mock.set_info(info);
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| {
        matches!(s.connection, ConnectionState::Incompatible { .. })
    })
    .await;
    assert_eq!(
        st.connection,
        ConnectionState::Incompatible {
            core: "2.0".into(),
            update: UpdateSide::Client
        }
    );
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(mock.paths(), ["/api/info"]);
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn a_newer_minor_goes_live_with_a_banner() {
    let mock = Mock::start("tok").await;
    let home = temp_home("minor");
    write_daemon(&home, &mock.url, 10, "tok");
    let mut info: Value = serde_json::from_str(INFO).unwrap();
    info["api_version"] = json!("1.7");
    mock.set_info(info);
    mock.script(Script::Hold(connected(3, Some(3))));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| s.connection.is_live()).await;
    assert_eq!(
        st.banners,
        [nexus_tui::state::Banner::NewerCore { core: "1.7".into() }]
    );
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn no_daemon_json_is_offline_not_a_crash() {
    let home = temp_home("offline");
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| {
        matches!(s.connection, ConnectionState::Offline { .. })
    })
    .await;
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn selecting_a_session_loads_its_history_without_a_resync() {
    let mock = Mock::start("tok").await;
    let home = temp_home("select");
    write_daemon(&home, &mock.url, 10, "tok");
    mock.script(Script::Hold(connected(3, Some(3))));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| s.connection.is_live()).await;
    let sid = "ses_1a17de160eccc428a38";
    st.model.select(Some(sid));
    h.commands.send(Command::Select(Some(sid.into()))).unwrap();
    drive(&mut h, &mut st, &mut log, |_, log| {
        log.iter()
            .any(|m| matches!(m, SyncMsg::History(h) if h.session_id == "ses_1a17de160eccc428a38"))
    })
    .await;
    // The stream stays open and no global snapshot is read again.
    assert_eq!(mock.count("/api/snapshot"), 1);
    assert_eq!(mock.count("/api/event"), 1);
    assert_eq!(mock.count(&format!("/api/session/{sid}/message")), 1);
    assert!(st.connection.is_live());
    h.commands.send(Command::Shutdown).unwrap();
}

#[tokio::test]
async fn mcp_inventory_reads_the_global_list_and_the_exact_project_endpoint() {
    let mock = Mock::start("tok").await;
    let home = temp_home("mcp-inventory");
    write_daemon(&home, &mock.url, 10, "tok");
    let project = "prj_1a17de160d3c070dfda";
    mock.route("/api/skill", json!({"skills": [], "problems": []}));
    mock.route(
        "/api/mcp",
        json!({"servers": [
            {"server": "docs", "state": "connected", "tools": 2, "enabled": true},
            {"server": "browser-use", "state": "disabled", "tools": 0, "enabled": false}
        ]}),
    );
    mock.route(
        &format!("/api/project/{project}/mcp"),
        json!({
            "project_id": project,
            "file": "/repo/.mcp.json",
            "exists": true,
            "revision": "rev",
            "error": null,
            "warnings": [],
            "servers": [{
                "server": "browser-use", "state": "idle", "error": null,
                "tools": 0, "enabled": true, "source": "project", "project_id": project,
                "file": "/repo/.mcp.json", "transport": "stdio", "command": "npx",
                "arg_count": 2, "cwd": "/repo", "env_names": ["TOKEN"]
            }]
        }),
    );
    mock.script(Script::Hold(connected(3, Some(3))));
    let mut h = sync::spawn(config(&home));
    let (mut st, mut log) = (AppState::new(), vec![]);
    drive(&mut h, &mut st, &mut log, |s, _| s.connection.is_live()).await;
    h.commands.send(Command::SkillMcp(project.into())).unwrap();
    drive(&mut h, &mut st, &mut log, |_, log| {
        log.iter().any(|m| matches!(m, SyncMsg::SkillMcp { .. }))
    })
    .await;

    assert_eq!(mock.count(&format!("/api/project/{project}/mcp")), 1);
    assert!(mock
        .paths()
        .contains(&format!("/api/project/{project}/mcp")));
    let msg = log
        .iter()
        .find_map(|m| match m {
            SyncMsg::SkillMcp {
                project: answered,
                mcp,
                project_mcp,
                ..
            } => Some((answered, mcp, project_mcp)),
            _ => None,
        })
        .unwrap();
    assert_eq!(msg.0, project, "the preview's exact project id is read");
    assert_eq!(msg.1.as_ref().unwrap()["servers"][0]["server"], "docs");
    assert_eq!(msg.1.as_ref().unwrap()["servers"][1]["state"], "disabled");
    let project_rows = msg.2.as_ref().unwrap();
    assert_eq!(project_rows["project_id"], project);
    assert_eq!(project_rows["servers"][0]["server"], "browser-use");
    assert_eq!(project_rows["servers"][0]["state"], "idle");
    assert_eq!(project_rows["servers"][0]["source"], "project");
    h.commands.send(Command::Shutdown).unwrap();
}

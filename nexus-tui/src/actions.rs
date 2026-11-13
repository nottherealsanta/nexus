//! The mutations the shell can make through Core's HTTP API, and how they run.
//! Each user action is one `ApiCall` with its own `RequestId`; the result comes
//! back as an `ActionDone` that the app turns into a toast (and, for a failed
//! send, a restored draft).

use std::path::Path;

use serde_json::{json, Value};

use crate::api::{CoreApi, Outcome, RequestId};
use crate::discovery;
use crate::transport::Http;

pub fn skill_detail(body: &Value) -> String {
    let resources = body["children"]
        .as_array()
        .or_else(|| body["files"].as_array());
    let resources = resources.filter(|rows| !rows.is_empty()).map_or_else(
        || "No other files or folders.".to_owned(),
        |rows| {
            rows.iter()
                .filter_map(|row| row.as_str())
                .map(|name| format!("- `{}`", name.replace('`', "\\`")))
                .collect::<Vec<_>>()
                .join("\n")
        },
    );
    let content = body["content"]
        .as_str()
        .or_else(|| body["body"].as_str())
        .unwrap_or("")
        .replace("\r\n", "\n");
    let markdown = content
        .strip_prefix("---\n")
        .and_then(|rest| rest.find("\n---\n").map(|end| end + 8))
        .map_or_else(
            || content.to_owned(),
            |end| format!("```yaml\n{}\n```\n{}", &content[..end], &content[end..]),
        );
    format!("## Description\n\n{}\n\n## Supporting resources\n\n{resources}\n\n## SKILL.md\n\n{markdown}", body["description"].as_str().unwrap_or(""))
}

#[derive(Clone, PartialEq)]
pub struct Secret(pub String);
impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[redacted]")
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum ApiCall {
    ProjectGit {
        project: String,
        worktree: Option<String>,
    },
    ProjectGitDiff {
        project: String,
        worktree: Option<String>,
        path: String,
    },
    ProjectGitLive {
        project: String,
        worktree: Option<String>,
        action: String,
        value: String,
        paths: Vec<String>,
    },
    ProjectGitAction {
        project: String,
        worktree: Option<String>,
        action: String,
        value: String,
        paths: Vec<String>,
        confirmed_live: Vec<String>,
    },
    FileWatch {
        project: String,
        session: Option<String>,
        client: String,
        release: bool,
    },
    FileMutation {
        project: String,
        session: Option<String>,
        action: String,
        path: String,
        to: Option<String>,
    },
    FileCount {
        project: String,
        session: Option<String>,
        path: String,
    },
    TerminalControl {
        id: String,
        attachment: String,
        generation: u64,
        acquire: bool,
    },
    TerminalInput {
        id: String,
        attachment: String,
        generation: u64,
        seq: u64,
        bytes: Vec<u8>,
    },
    TerminalResize {
        id: String,
        attachment: String,
        generation: u64,
        rows: u16,
        cols: u16,
    },
    TerminalList {
        project: String,
    },
    TerminalCreate {
        project: String,
        workspace: String,
        session: Option<String>,
        client: String,
    },
    TerminalRename {
        id: String,
        name: String,
    },
    /// Stop (kill) a background bash job from the Shells modal.
    StopJob {
        id: String,
    },
    TerminalClose {
        id: String,
    },
    /// Connect (`method`: `device`, `local`, `api_key` with `key`) or, with `sign_out`, disconnect or cancel.
    Provider {
        id: String,
        sign_out: bool,
        method: String,
        key: Option<Secret>,
    },
    /// Send a prompt to an existing idle session.
    Prompt {
        session: String,
        /// Root tree to unarchive first, after the user's confirmation.
        unarchive: Option<String>,
        text: String,
        images: Vec<crate::images::Image>,
        pastes: Vec<(String, String)>,
        mode: String,
        /// The composer as it was before the send, to put back whole if it fails.
        draft: Option<Box<crate::composer::Composer>>,
    },
    /// Composer `!` mode: run a command in the workspace; Core adds it and its output to context.
    Shell {
        session: String,
        command: String,
    },
    Input {
        session: String,
        id: String,
        action: String,
        before: Option<String>,
        parts: Vec<Value>,
    },
    /// Create a session; the app opens it and dispatches Prompt when creation completes.
    NewAndPrompt {
        project: String,
        text: String,
        images: Vec<crate::images::Image>,
        pastes: Vec<(String, String)>,
        /// Agent, model and effort chosen before the session existed.
        fields: serde_json::Value,
        /// The composer as it was before the send, to put back whole if it fails.
        draft: Option<Box<crate::composer::Composer>>,
    },
    Continue {
        session: String,
    },
    Speak {
        session: String,
    },
    /// Summarise the session and start a continued one (`/handoff`).
    Handoff {
        session: String,
    },
    /// A side question answered from the context so far (`/btw`).
    Btw {
        session: String,
        question: String,
        request_id: String,
    },
    BtwState {
        session: String,
        request_id: String,
    },
    /// The full text of a skill or an MCP server for the context modal.
    ContextEntry {
        kind: &'static str,
        name: String,
        project: Option<String>,
    },
    SpeakStop,
    VoiceStatus {
        generation: u64,
    },
    VoicePrepare {
        generation: u64,
    },
    /// Settings refresh, separate from the generation-guarded dictation check.
    VoiceSettingsStatus {
        generation: u64,
    },
    VoiceDownload {
        generation: u64,
    },
    /// The local search service's status (Settings › Tools).
    SearchServiceStatus,
    /// Start the local search service and wait until it answers.
    SearchServiceStart,
    /// What the browser tools need on Core's machine (Settings › Browser).
    BrowserStatus,
    VoiceModel {
        target: VoiceTarget,
        operation: VoiceOperation,
    },
    VoiceTranscribe {
        wav: Vec<u8>,
        partial: bool,
        generation: u64,
    },
    /// The Manager: is speech recognition ready? (`done.voice` is `/api/voice`.)
    ManagerVoice {
        token: u64,
    },
    /// The Manager: what the user said (`done.transcript`).
    ManagerTranscribe {
        wav: Vec<u8>,
        token: u64,
    },
    /// The Manager: stop the sentence being spoken here.
    ManagerSpeakStop,
    /// GPT-Live: Core opens its call, then this client opens the call's audio.
    ManagerLive {
        stream_id: String,
    },
    /// The Manager: speak one sentence, reporting what is really playing.
    ManagerSpeak {
        text: String,
        token: u64,
        playback: crate::speech::Playback,
    },
    Interrupt {
        session: String,
    },
    /// This client is showing the session: Core tells every client to clear its unread mark.
    Seen {
        session: String,
    },
    Archive {
        session: String,
        archived: bool,
    },
    Rename {
        session: String,
        title: String,
        revision: i64,
    },
    Delete {
        session: String,
    },
    /// `scope_session` also grants the same call for the rest of the session
    /// (allow only); `note` is added to a refusal the model reads (deny only).
    Reply {
        session: String,
        id: String,
        allow: bool,
        scope_session: bool,
        note: Option<String>,
    },
    /// Answer a pending `ask` request: one `{question_id, selected, text}` per
    /// question (see `docs/api.md`), submitted once with its own key.
    Answer {
        session: String,
        id: String,
        answers: Vec<Value>,
    },
    /// Apply from the next model call; an active call keeps its configuration.
    SetModel {
        session: String,
        model: String,
        revision: i64,
    },
    SetAgent {
        session: String,
        agent: String,
        revision: i64,
    },
    /// Empty clears the session effort (the agent's or provider's applies).
    SetEffort {
        session: String,
        effort: String,
        revision: i64,
    },
    /// Fetch and inspect candidates for `/learn` before any install is made.
    LearnPreview {
        project: String,
        source: String,
        scope: String,
    },
    /// Install the selected skills from one short-lived preview fetch.
    LearnInstall {
        project: String,
        scope: String,
        fetch_id: String,
        skills: Vec<String>,
        overwrite: bool,
        replacing: Vec<String>,
    },
    /// Update the session-local tool/skill/MCP switch list.
    SetDisabledTools {
        session: String,
        disabled_tools: Vec<String>,
        revision: i64,
    },
    /// Write a Markdown transcript under `<Nexus home>/exports`.
    Export {
        session: String,
        format: String,
        scope: String,
    },
    ChangeDirectory {
        session: String,
        directory: String,
    },
    /// Add (or find) the project at a directory.
    AddProject {
        path: String,
    },
    /// Remove (unregister) a project; Core keeps its sessions and never touches files.
    DeleteProject {
        project: String,
    },
    /// Move a project in the saved order: before/after a neighbour, or to an end.
    MoveProject {
        project: String,
        before: Option<String>,
        after: Option<String>,
    },
    /// Set the project's three-tier status.
    SetProjectStatus {
        project: String,
        status: String,
    },
    /// Read the project's prompt history (Up in the composer); never toasts.
    Prompts {
        project: String,
    },
    /// Write the editor's text (`PUT …/file`, K10) if the file is still at `revision`.
    SaveFile {
        project: String,
        session: Option<String>,
        path: String,
        content: String,
        revision: String,
    },
    /// Usage of every connected provider, with plan windows where reported (`GET /api/usage`).
    Usage,
    /// Refresh the stored provider usage snapshot.
    RefreshUsage,
    /// One page of the native inspector (`GET …/inspect`, API v2). `query` carries
    /// view, limit, before, session and the filters; `generation` orders responses.
    Inspector {
        session: String,
        query: Vec<(String, String)>,
        generation: u64,
    },
    /// The cheap change check (`GET …/inspect/head`).
    InspectorHead {
        session: String,
        target: String,
        generation: u64,
    },
    /// One full record whose detail the page clipped (`GET …/inspect/record`).
    InspectorRecord {
        session: String,
        target: String,
        view: usize,
        kind: String,
        id: String,
        row: String,
    },
    /// Would switching to `model` or `agent` lose the prompt cache? (`GET …/switch-cost`)
    SwitchCost {
        session: String,
        model: Option<String>,
        agent: Option<String>,
        revision: i64,
    },
    /// The page of messages before sequence `before` (`turn.load_older`).
    OlderMessages {
        session: String,
        before: i64,
    },
    /// Core's session search (`GET /api/search`, titles and user messages); never toasts.
    Search {
        query: String,
        project: Option<String>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum VoiceTarget {
    Stt,
    Tts,
}

impl VoiceTarget {
    pub fn id(self) -> &'static str {
        match self {
            Self::Stt => "stt",
            Self::Tts => "tts",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Stt => "speech-to-text",
            Self::Tts => "text-to-speech",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VoiceOperation {
    Download,
    Remove,
}

impl ApiCall {
    /// Past-tense label for the success toast.
    pub fn done_label(&self) -> &'static str {
        match self {
            ApiCall::FileWatch { .. } => "",
            ApiCall::ProjectGit { .. }
            | ApiCall::ProjectGitDiff { .. }
            | ApiCall::ProjectGitLive { .. } => "",
            ApiCall::ProjectGitAction { .. } => "Git updated",
            ApiCall::FileMutation { .. } => "File updated",
            ApiCall::FileCount { .. } => "",
            ApiCall::TerminalList { .. }
            | ApiCall::TerminalCreate { .. }
            | ApiCall::TerminalRename { .. }
            | ApiCall::TerminalClose { .. }
            | ApiCall::TerminalControl { .. }
            | ApiCall::TerminalInput { .. }
            | ApiCall::TerminalResize { .. } => "",
            ApiCall::Shell { .. } => "",
            ApiCall::Input { .. } => "Pending input updated",
            ApiCall::Provider { sign_out: true, .. } => "Signed out",
            ApiCall::Provider { .. } => "Sign-in started",
            ApiCall::Prompt { .. } | ApiCall::NewAndPrompt { .. } => "Sent",
            ApiCall::Continue { .. } => "Continuing",
            ApiCall::Speak { .. } => "Finished speaking",
            ApiCall::Handoff { .. } => "Handed off to a new session",
            ApiCall::Btw { .. } | ApiCall::BtwState { .. } | ApiCall::ContextEntry { .. } => "",
            ApiCall::SpeakStop => "Stopped speaking",
            ApiCall::VoiceStatus { .. }
            | ApiCall::VoicePrepare { .. }
            | ApiCall::VoiceSettingsStatus { .. }
            | ApiCall::VoiceTranscribe { .. }
            | ApiCall::ManagerVoice { .. }
            | ApiCall::ManagerTranscribe { .. }
            | ApiCall::ManagerSpeak { .. }
            | ApiCall::ManagerLive { .. }
            | ApiCall::ManagerSpeakStop
            | ApiCall::Seen { .. } => "",
            ApiCall::SearchServiceStatus | ApiCall::BrowserStatus => "",
            ApiCall::SearchServiceStart => "Web search is running",
            ApiCall::VoiceDownload { .. } => "Voice model download started",
            ApiCall::VoiceModel {
                operation: VoiceOperation::Download,
                ..
            } => "Voice model download started",
            ApiCall::VoiceModel {
                operation: VoiceOperation::Remove,
                ..
            } => "Voice model removed from cache",
            ApiCall::Interrupt { .. } => "Interrupt sent",
            ApiCall::StopJob { .. } => "Shell stopped",
            ApiCall::Archive { archived: true, .. } => "Archived",
            ApiCall::Archive { .. } => "Unarchived",
            ApiCall::Rename { .. } => "Renamed",
            ApiCall::Delete { .. } => "Deleted",
            ApiCall::Reply {
                allow: true,
                scope_session: true,
                ..
            } => "Allowed for this session",
            ApiCall::Reply { allow: true, .. } => "Allowed",
            ApiCall::Reply { .. } => "Denied",
            ApiCall::Answer { .. } => "",
            ApiCall::SetModel { .. } => "Model changed from the next call",
            ApiCall::SetAgent { .. } => "Agent changed from the next call",
            ApiCall::SetEffort { .. } => "Effort changed from the next call",
            ApiCall::Export { .. } => "Exported",
            ApiCall::LearnPreview { .. } => "",
            ApiCall::LearnInstall { .. } => "Skills installed",
            ApiCall::SetDisabledTools { .. } => "Session switches updated",
            ApiCall::ChangeDirectory { .. } => "Directory change requested",
            ApiCall::AddProject { .. } => "Project added",
            ApiCall::DeleteProject { .. } => "Project removed",
            ApiCall::MoveProject { .. } => "Project moved",
            ApiCall::SetProjectStatus { .. } => "Project status changed",
            ApiCall::Prompts { .. }
            | ApiCall::Search { .. }
            | ApiCall::OlderMessages { .. }
            | ApiCall::SwitchCost { .. }
            | ApiCall::Usage
            | ApiCall::Inspector { .. }
            | ApiCall::InspectorHead { .. }
            | ApiCall::InspectorRecord { .. }
            | ApiCall::RefreshUsage => "",
            ApiCall::SaveFile { .. } => "Saved",
        }
    }

    /// Text to put back in the composer if the call fails.
    pub fn draft(&self) -> Option<(Option<&str>, &str)> {
        match self {
            ApiCall::Prompt { session, text, .. } => Some((Some(session), text)),
            ApiCall::NewAndPrompt { text, .. } => Some((None, text)),
            ApiCall::Shell { session, command } => Some((Some(session), command)),
            _ => None,
        }
    }

    /// The composer to put back after a failed send, marker chips included.
    pub fn saved_draft(&self) -> Option<&crate::composer::Composer> {
        match self {
            ApiCall::Prompt { draft, .. } | ApiCall::NewAndPrompt { draft, .. } => draft.as_deref(),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ActionDone {
    pub call: ApiCall,
    pub result: Result<Done, String>,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Done {
    pub project_git: Option<Value>,
    pub git_live: Option<Value>,
    pub file_watch_status: Option<String>,
    pub file_count: Option<u64>,
    pub terminals: Option<Vec<crate::terminal_api::TerminalResource>>,
    pub terminal: Option<crate::terminal_api::TerminalResource>,
    /// A session the app should open (new sessions).
    pub open: Option<String>,
    /// The created session itself, so its tab can render before its event arrives.
    pub session: Option<crate::api::Session>,
    /// Set when the write lost a revision race; the app reports it.
    pub conflict: Option<String>,
    /// A local file the action produced (exports), for the success toast.
    pub path: Option<String>,
    /// A finished sentence for the success toast (`/learn`).
    pub message: Option<String>,
    /// A project the app should make current (added projects).
    pub project: Option<String>,
    pub input_parts: Option<Vec<Value>>,
    /// The project's prompts, oldest first (history recall).
    pub prompts: Option<Vec<String>>,
    /// The save's answer: the file's new revision.
    pub saved: Option<Value>,
    /// Core's `/api/usage`.
    pub usage: Option<Value>,
    pub inspection: Option<Value>,
    /// Core's answer to `…/switch-cost`.
    pub switch_cost: Option<Value>,
    /// A page of older messages and whether more remain.
    pub older: Option<(Vec<crate::api::Message>, bool)>,
    /// Core's search hits (`/api/search`).
    pub search: Option<Value>,
    /// `/api/skill/preview` result, shown by the app as a checklist or install.
    pub learn_preview: Option<Value>,
    /// A Core voice status/download response.
    pub voice: Option<Value>,
    /// Core's `/api/search/service` answer.
    pub search_service: Option<Value>,
    /// Core's `/api/browser` answer.
    pub browser_status: Option<Value>,
    /// A transcription result. It remains a response until the app inserts it.
    pub transcript: Option<String>,
    /// The answer to a `/btw` question.
    pub answer: Option<String>,
    pub btw_context: Option<String>,
    pub btw_state: Option<Value>,
}

/// An authenticated API for the daemon in `home`, from `daemon.json`.
pub fn api_for(home: &Path) -> Option<CoreApi> {
    let info = discovery::read_info(home)?;
    Http::new(&info.url, &info.token).ok().map(CoreApi::new)
}

fn conflict<T>(o: Outcome<T>) -> Result<Option<T>, String> {
    match o {
        Outcome::Done { value, .. } => Ok(Some(value)),
        Outcome::Conflict { message, .. } => Err(format!(
            "Changed elsewhere ({message}); the view is current, try again"
        )),
    }
}

pub async fn run(api: Option<CoreApi>, call: ApiCall) -> ActionDone {
    let result = match api {
        None => Err("Core is not reachable".to_owned()),
        Some(api) => execute(&api, &call).await,
    };
    ActionDone { call, result }
}

/// A history entry as the composer shows it: images are mentioned, not re-attached.
fn prompt_entry(text: &str, images: u64) -> String {
    match (text.is_empty(), images) {
        (_, 0) => text.to_owned(),
        (true, 1) => "[1 image]".to_owned(),
        (true, n) => format!("[{n} images]"),
        (false, 1) => format!("{text}\n[1 image]"),
        (false, n) => format!("{text}\n[{n} images]"),
    }
}

async fn execute(api: &CoreApi, call: &ApiCall) -> Result<Done, String> {
    let e = |e: crate::transport::Error| e.to_string();
    let key = RequestId::new();
    let mut done = Done::default();
    match call {
        ApiCall::ProjectGitDiff {
            project,
            worktree,
            path,
        } => {
            let mut query = vec![("path", path.clone())];
            if let Some(worktree) = worktree {
                query.push(("worktree", worktree.clone()));
            }
            done.project_git = Some(
                api.http
                    .get(&format!("/api/project/{project}/git/diff"), &query)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::ProjectGitLive {
            project, worktree, ..
        } => {
            done.git_live = Some(
                api.http
                    .get(
                        &format!("/api/project/{project}/git/live"),
                        &worktree
                            .as_ref()
                            .map(|w| vec![("worktree", w.clone())])
                            .unwrap_or_default(),
                    )
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::ProjectGitAction {
            project,
            worktree,
            action,
            value,
            paths,
            confirmed_live,
        } => {
            let body = json!({"action": action, "value": value, "paths": paths, "confirmed_live": confirmed_live, "force_confirmed": action == "branch_delete_force", "worktree": worktree});
            match api
                .http
                .call(
                    reqwest::Method::POST,
                    &format!("/api/project/{project}/git/action"),
                    &[],
                    Some(&body),
                    Some(key.as_str()),
                    None,
                )
                .await
            {
                Ok(reply) => {
                    done.project_git = Some(
                        reply
                            .body
                            .get("status")
                            .filter(|s| s.is_object())
                            .cloned()
                            .unwrap_or(reply.body),
                    )
                }
                Err(crate::transport::Error::Api(error))
                    if error.code.as_deref() == Some("worktree_live") =>
                {
                    done.git_live = Some(error.body)
                }
                Err(error) => return Err(e(error)),
            }
        }
        ApiCall::ProjectGit { project, worktree } => {
            done.project_git = Some(
                api.http
                    .get(
                        &format!("/api/project/{project}/git"),
                        &worktree
                            .as_ref()
                            .map(|w| vec![("worktree", w.clone())])
                            .unwrap_or_default(),
                    )
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::FileWatch {
            project,
            session,
            client,
            release,
        } => {
            let query = session
                .as_ref()
                .map(|s| vec![("session", s.clone())])
                .unwrap_or_default();
            let body =
                json!({"client_id": client, "action": if *release { "release" } else { "renew" }});
            let result = api
                .http
                .call(
                    reqwest::Method::POST,
                    &format!("/api/project/{project}/watch"),
                    &query,
                    Some(&body),
                    None,
                    None,
                )
                .await
                .map_err(e)?;
            done.file_watch_status = result.body["status"].as_str().map(str::to_owned);
        }
        ApiCall::FileMutation {
            project,
            session,
            action,
            path,
            to,
        } => {
            let query = session
                .as_ref()
                .map(|s| vec![("session", s.clone())])
                .unwrap_or_default();
            let body = json!({"action": action, "path": path, "to": to});
            api.http
                .call(
                    reqwest::Method::POST,
                    &format!("/api/project/{project}/files"),
                    &query,
                    Some(&body),
                    Some(key.as_str()),
                    None,
                )
                .await
                .map_err(e)?;
        }
        ApiCall::FileCount {
            project,
            session,
            path,
        } => {
            let mut query = vec![("path", path.clone()), ("count", "true".into())];
            if let Some(s) = session {
                query.push(("session", s.clone()));
            }
            let result = api
                .http
                .get(&format!("/api/project/{project}/files"), &query)
                .await
                .map_err(e)?;
            done.file_count = Some(
                result["count"]
                    .as_u64()
                    .ok_or("Core did not return a folder count")?,
            );
        }
        ApiCall::TerminalControl {
            id,
            attachment,
            acquire,
            ..
        } => {
            crate::terminal_api::TerminalApi(api.http.clone())
                .control(id, attachment, *acquire)
                .await
                .map_err(e)?;
        }
        ApiCall::TerminalInput {
            id,
            attachment,
            seq,
            bytes,
            ..
        } => {
            crate::terminal_api::TerminalApi(api.http.clone())
                .input(id, attachment, *seq, bytes)
                .await
                .map_err(e)?;
        }
        ApiCall::TerminalResize {
            id,
            attachment,
            rows,
            cols,
            ..
        } => {
            crate::terminal_api::TerminalApi(api.http.clone())
                .resize(id, attachment, *rows, *cols)
                .await
                .map_err(e)?;
        }
        ApiCall::TerminalList { project } => {
            done.terminals = Some(
                crate::terminal_api::TerminalApi(api.http.clone())
                    .list(project)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::TerminalCreate {
            project,
            workspace,
            session,
            client,
        } => {
            done.terminal = Some(
                crate::terminal_api::TerminalApi(api.http.clone())
                    .create(project, workspace, session.as_deref(), client, 24, 80)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::TerminalRename { id, name } => {
            done.terminal = Some(
                crate::terminal_api::TerminalApi(api.http.clone())
                    .rename(id, name)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::StopJob { id } => {
            api.stop_bash_job(id).await.map_err(e)?;
        }
        ApiCall::TerminalClose { id } => {
            done.terminal = Some(
                crate::terminal_api::TerminalApi(api.http.clone())
                    .close(id)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::Provider {
            id,
            sign_out,
            method: how,
            key,
        } => {
            let method = if *sign_out {
                reqwest::Method::DELETE
            } else {
                reqwest::Method::POST
            };
            let body = if let Some(key) = key {
                json!({"method": "api_key", "api_key": key.0})
            } else {
                json!({"method": how})
            };
            api.http
                .call(
                    method,
                    &format!("/api/provider/{id}/login"),
                    &[],
                    if *sign_out { None } else { Some(&body) },
                    None,
                    None,
                )
                .await
                .map_err(e)?;
        }
        ApiCall::SaveFile {
            project,
            session,
            path,
            content,
            revision,
        } => {
            let mut query = vec![("path", path.clone())];
            if let Some(session) = session {
                query.push(("session", session.clone()));
            }
            let body = json!({ "content": content, "expected_revision": revision });
            match api
                .http
                .call(
                    reqwest::Method::PUT,
                    &format!("/api/project/{project}/file"),
                    &query,
                    Some(&body),
                    None,
                    None,
                )
                .await
            {
                Ok(r) => done.saved = Some(r.body),
                // The file moved on since it was read: not a failure to retry, a state to show.
                Err(crate::transport::Error::Api(error))
                    if error.code.as_deref() == Some("stale_file") =>
                {
                    done.conflict = Some(error.message)
                }
                Err(error) => return Err(e(error)),
            }
        }
        ApiCall::Usage => {
            done.usage = Some(api.http.get("/api/usage", &[]).await.map_err(e)?);
        }
        ApiCall::Inspector { session, query, .. } => {
            done.inspection = Some(
                api.http
                    .get(
                        &format!("/api/session/{session}/inspect"),
                        &query
                            .iter()
                            .map(|(k, v)| (k.as_str(), v.clone()))
                            .collect::<Vec<_>>(),
                    )
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::InspectorHead {
            session, target, ..
        } => {
            let query = if target == session {
                vec![]
            } else {
                vec![("session", target.clone())]
            };
            done.inspection = Some(
                api.http
                    .get(&format!("/api/session/{session}/inspect/head"), &query)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::InspectorRecord {
            session, kind, id, ..
        } => {
            done.inspection = Some(
                api.http
                    .get(
                        &format!("/api/session/{session}/inspect/record"),
                        &[("kind", kind.clone()), ("id", id.clone())],
                    )
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::RefreshUsage => {
            done.usage = Some(
                api.http
                    .call(reqwest::Method::POST, "/api/usage", &[], None, None, None)
                    .await
                    .map_err(e)?
                    .body,
            );
        }
        ApiCall::SwitchCost {
            session,
            model,
            agent,
            ..
        } => {
            let mut params = Vec::new();
            if let Some(model) = model {
                params.push(("model", model.clone()));
            }
            if let Some(agent) = agent {
                params.push(("agent", agent.clone()));
            }
            done.switch_cost = Some(
                api.http
                    .get(&format!("/api/session/{session}/switch-cost"), &params)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::OlderMessages { session, before } => {
            done.older = Some(
                api.messages_page(session, Some(*before), crate::sync::MESSAGE_PAGE)
                    .await
                    .map_err(e)?,
            );
        }
        ApiCall::Search { query, project } => {
            let mut params = vec![("q", query.clone()), ("limit", "30".to_owned())];
            if let Some(project) = project {
                params.push(("project", project.clone()));
            }
            done.search = Some(api.http.get("/api/search", &params).await.map_err(e)?);
        }
        ApiCall::Prompts { project } => {
            let value = api
                .http
                .get(
                    &format!("/api/project/{project}/prompts"),
                    &[("limit", "200".to_owned())],
                )
                .await
                .map_err(e)?;
            let mut prompts: Vec<String> = value["prompts"]
                .as_array()
                .map(|list| {
                    list.iter()
                        .map(|p| {
                            prompt_entry(
                                p["text"].as_str().unwrap_or(""),
                                p["image_count"].as_u64().unwrap_or(0),
                            )
                        })
                        .filter(|text| !text.is_empty())
                        .collect()
                })
                .unwrap_or_default();
            prompts.reverse();
            done.prompts = Some(prompts);
        }
        ApiCall::Prompt {
            session,
            unarchive,
            text,
            images,
            pastes,
            mode,
            ..
        } => {
            if let Some(root) = unarchive {
                conflict(
                    api.set_archived(root, false, &crate::api::RequestId::new())
                        .await
                        .map_err(e)?,
                )?;
            }
            conflict(
                api.prompt_images(session, text, images, pastes, mode, &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::Shell { session, command } => {
            api.http
                .call(
                    reqwest::Method::POST,
                    &format!("/api/session/{session}/shell"),
                    &[],
                    Some(&json!({ "command": command })),
                    None,
                    None,
                )
                .await
                .map_err(e)?;
        }
        ApiCall::Input {
            session,
            id,
            action,
            before,
            ..
        } => {
            let base = format!("/api/session/{session}/input");
            let (method, path, body) = match action.as_str() {
                "remove" | "delete" => (reqwest::Method::DELETE, format!("{base}/{id}"), None),
                "steer" => (
                    reqwest::Method::PATCH,
                    format!("{base}/{id}"),
                    Some(json!({"mode":"steer"})),
                ),
                "move" => (
                    reqwest::Method::POST,
                    format!("{base}/{id}/move"),
                    Some(json!({"before":before})),
                ),
                _ => (
                    reqwest::Method::POST,
                    format!("{base}/next"),
                    Some(json!({})),
                ),
            };
            let reply = api
                .http
                .call(method, &path, &[], body.as_ref(), None, None)
                .await
                .map_err(e)?;
            if action == "remove" {
                done.input_parts = reply.body["parts"].as_array().cloned();
            }
        }
        ApiCall::NewAndPrompt {
            project, fields, ..
        } => {
            let s = conflict(
                api.create_session(project, fields.clone(), &key)
                    .await
                    .map_err(e)?,
            )?
            .ok_or("no session returned")?;
            done.open = Some(s.id.clone());
            done.session = Some(s.clone());
        }
        ApiCall::Continue { session } => {
            conflict(api.continue_turn(session, &key).await.map_err(e)?)?;
        }
        ApiCall::Speak { session } => {
            crate::speech::speak(&api.http, session).await?;
        }
        ApiCall::Handoff { session } => {
            let body = api
                .http
                .call(
                    reqwest::Method::POST,
                    &format!("/api/session/{session}/handoff"),
                    &[],
                    Some(&json!({})),
                    None,
                    None,
                )
                .await
                .map_err(e)?
                .body;
            done.open = body["to"].as_str().map(str::to_owned);
        }
        ApiCall::BtwState { session, .. } => {
            done.btw_state = Some(
                api.http
                    .call(
                        reqwest::Method::GET,
                        &format!("/api/session/{session}/btw"),
                        &[],
                        None,
                        None,
                        None,
                    )
                    .await
                    .map_err(e)?
                    .body,
            );
        }
        ApiCall::Btw {
            session,
            question,
            request_id,
        } => {
            let body = api
                .http
                .call(
                    reqwest::Method::POST,
                    &format!("/api/session/{session}/btw"),
                    &[],
                    Some(&json!({ "question": question, "request_id": request_id })),
                    None,
                    None,
                )
                .await
                .map_err(e)?
                .body;
            done.answer = body["session"].as_str().map(str::to_owned);
            done.btw_context = body["inherited_context"].as_str().map(str::to_owned);
        }
        ApiCall::ContextEntry {
            kind,
            name,
            project,
        } => {
            let path = match (*kind, project) {
                ("skill", _) => format!("/api/skill/{name}"),
                _ => format!("/api/mcp/{name}"),
            };
            let query = match (*kind, project) {
                ("skill", Some(project)) => vec![("project", project.clone())],
                _ => Vec::new(),
            };
            let mut reply = api
                .http
                .call(reqwest::Method::GET, &path, &query, None, None, None)
                .await;
            // A project's own server is not in the global list.
            if let (true, Some(project), true) = (*kind == "mcp", project, reply.is_err()) {
                reply = api
                    .http
                    .call(
                        reqwest::Method::GET,
                        &format!("/api/project/{project}/mcp/{name}"),
                        &[],
                        None,
                        None,
                        None,
                    )
                    .await;
            }
            let body = reply.map_err(e)?.body;
            done.answer = Some(if *kind == "skill" {
                skill_detail(&body)
            } else {
                serde_json::to_string_pretty(&body).unwrap_or_default()
            });
        }
        ApiCall::SpeakStop => {
            if !crate::speech::stop_local() {
                api.http
                    .call(
                        reqwest::Method::POST,
                        "/api/voice/speak/stop",
                        &[],
                        Some(&serde_json::json!({})),
                        None,
                        None,
                    )
                    .await
                    .map_err(e)?;
            }
        }
        ApiCall::VoiceStatus { .. } => {
            done.voice = Some(api.http.get("/api/voice", &[]).await.map_err(e)?);
        }
        ApiCall::VoicePrepare { .. } => {
            api.http
                .call(
                    reqwest::Method::POST,
                    "/api/voice/stt/prepare",
                    &[],
                    None,
                    None,
                    None,
                )
                .await
                .map_err(e)?;
        }
        ApiCall::VoiceSettingsStatus { .. } => {
            done.voice = Some(api.http.get("/api/voice", &[]).await.map_err(e)?);
        }
        ApiCall::SearchServiceStatus => {
            done.search_service = Some(api.http.get("/api/search/service", &[]).await.map_err(e)?);
        }
        ApiCall::BrowserStatus => {
            done.browser_status = Some(api.http.get("/api/browser", &[]).await.map_err(e)?);
        }
        ApiCall::SearchServiceStart => {
            done.search_service = Some(
                api.http
                    .call(
                        reqwest::Method::POST,
                        "/api/search/service/start",
                        &[],
                        None,
                        None,
                        None,
                    )
                    .await
                    .map_err(e)?
                    .body,
            );
        }
        ApiCall::VoiceDownload { .. } => {
            done.voice = Some(
                api.http
                    .call(
                        reqwest::Method::POST,
                        "/api/voice/stt/download",
                        &[],
                        None,
                        None,
                        None,
                    )
                    .await
                    .map_err(e)?
                    .body,
            );
        }
        ApiCall::VoiceModel { target, operation } => {
            let path = match operation {
                VoiceOperation::Download => format!("/api/voice/{}/download", target.id()),
                VoiceOperation::Remove => format!("/api/voice/{}", target.id()),
            };
            let method = match operation {
                VoiceOperation::Download => reqwest::Method::POST,
                VoiceOperation::Remove => reqwest::Method::DELETE,
            };
            done.voice = Some(
                api.http
                    .call(method, &path, &[], None, None, None)
                    .await
                    .map_err(e)?
                    .body,
            );
        }
        ApiCall::ManagerSpeakStop => {
            crate::speech::stop_local();
        }
        ApiCall::ManagerLive { stream_id } => {
            crate::live::open(&api.http, stream_id).await?;
        }
        ApiCall::ManagerVoice { .. } => {
            done.voice = Some(api.http.get("/api/voice", &[]).await.map_err(e)?);
        }
        ApiCall::ManagerTranscribe { wav, .. } => {
            let reply = api
                .http
                .post_bytes("/api/voice/transcribe", &[], "audio/wav", wav.clone())
                .await
                .map_err(e)?;
            done.transcript = Some(reply.body["text"].as_str().unwrap_or("").to_owned());
        }
        ApiCall::ManagerSpeak { text, playback, .. } => {
            crate::speech::speak_text(&api.http, text, playback).await?;
        }
        ApiCall::VoiceTranscribe { wav, partial, .. } => {
            let query = if *partial {
                vec![("partial", "1".to_owned())]
            } else {
                Vec::new()
            };
            let reply =
                api.http
                    .post_bytes("/api/voice/transcribe", &query, "audio/wav", wav.clone())
                    .await
                    .map_err(|error| {
                        if error.api().is_some_and(|api_error| {
                            api_error.code.as_deref() == Some("voice_busy")
                        }) {
                            "voice_busy".to_owned()
                        } else {
                            e(error)
                        }
                    })?;
            done.transcript = reply.body["text"].as_str().map(str::to_owned);
        }
        ApiCall::Interrupt { session } => {
            let parts = conflict(api.interrupt_and_reclaim(session, &key).await.map_err(e)?)?;
            if let Some(parts) = parts {
                if !parts.is_empty() {
                    done.input_parts = Some(parts);
                }
            }
        }
        ApiCall::Seen { session } => {
            api.mark_seen(session).await.map_err(e)?;
        }
        ApiCall::Archive { session, archived } => {
            conflict(
                api.set_archived(session, *archived, &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::Rename {
            session,
            title,
            revision,
        } => {
            match api
                .patch_session(session, json!({ "title": title }), Some(*revision), &key)
                .await
                .map_err(e)?
            {
                Outcome::Done { .. } => {}
                Outcome::Conflict { message, .. } => done.conflict = Some(message),
            }
        }
        ApiCall::Delete { session } => {
            conflict(api.delete_session(session, &key).await.map_err(e)?)?;
        }
        ApiCall::Reply {
            session,
            id,
            allow,
            scope_session,
            note,
        } => {
            conflict(
                api.reply_permission(session, id, *allow, *scope_session, note.as_deref(), &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::Answer {
            session,
            id,
            answers,
        } => {
            conflict(
                api.reply_question(session, id, Value::Array(answers.clone()), &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::SetModel {
            session,
            model,
            revision,
        } => match api
            .set_model_rev(session, model, Some(*revision), &key)
            .await
            .map_err(e)?
        {
            Outcome::Done { .. } => {}
            Outcome::Conflict { message, .. } => done.conflict = Some(message),
        },
        ApiCall::SetAgent {
            session,
            agent,
            revision,
        } => match api
            .set_agent_rev(session, agent, Some(*revision), &key)
            .await
            .map_err(e)?
        {
            Outcome::Done { .. } => {}
            Outcome::Conflict { message, .. } => done.conflict = Some(message),
        },
        ApiCall::ChangeDirectory { session, directory } => {
            conflict(
                api.change_directory(session, directory, &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::AddProject { path } => {
            let project = conflict(api.create_project(path, None, &key).await.map_err(e)?)?
                .ok_or("no project returned")?;
            done.project = Some(project.id);
        }
        ApiCall::DeleteProject { project } => {
            conflict(api.delete_project(project, &key).await.map_err(e)?)?;
        }
        ApiCall::MoveProject {
            project,
            before,
            after,
        } => {
            conflict(
                api.move_project(project, before.as_deref(), after.as_deref(), &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::SetProjectStatus { project, status } => {
            conflict(
                api.set_project_status(project, status, &key)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::LearnPreview {
            project,
            source,
            scope,
        } => {
            let value = api
                .http
                .call(
                    reqwest::Method::POST,
                    "/api/skill/preview",
                    &[],
                    Some(&json!({ "source": source, "scope": scope, "project": project })),
                    None,
                    None,
                )
                .await
                .map_err(e)?
                .body;
            done.learn_preview = Some(value);
        }
        ApiCall::LearnInstall {
            project,
            scope,
            fetch_id,
            skills,
            overwrite,
            replacing,
        } => {
            let value = api
                .http
                .call(
                    reqwest::Method::POST,
                    "/api/skill/install",
                    &[],
                    Some(&json!({
                        "fetch_id": fetch_id,
                        "skills": skills,
                        "scope": scope,
                        "project": project,
                        "overwrite": overwrite,
                    })),
                    None,
                    None,
                )
                .await
                .map_err(e)?
                .body;
            let installed: Vec<&str> = value["installed"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|i| i["name"].as_str())
                .collect();
            let returned_replaced: Vec<&str> = value["installed"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|item| item["replaced"].as_bool().unwrap_or(false))
                .filter_map(|item| item["name"].as_str())
                .collect();
            let replaced = if returned_replaced.is_empty() {
                replacing
                    .iter()
                    .filter(|name| installed.contains(&name.as_str()))
                    .map(String::as_str)
                    .collect::<Vec<_>>()
            } else {
                returned_replaced
            };
            let place = if scope == "global" {
                "for every project"
            } else {
                "for this project"
            };
            done.message = Some(format!(
                "Learned {} {place}{}. Available in new sessions.",
                installed.join(", "),
                if replaced.is_empty() {
                    String::new()
                } else {
                    format!(" (replaced {})", replaced.join(", "))
                }
            ));
        }
        ApiCall::SetDisabledTools {
            session,
            disabled_tools,
            revision,
        } => {
            conflict(
                api.patch_session(
                    session,
                    json!({ "disabled_tools": disabled_tools }),
                    Some(*revision),
                    &key,
                )
                .await
                .map_err(e)?,
            )?;
        }
        ApiCall::Export {
            session,
            format,
            scope,
        } => {
            let ext = if format == "json" { "json" } else { "md" };
            let (text, _name) = api.export(session, format, scope).await.map_err(e)?;
            let dir = crate::discovery::home()
                .unwrap_or_else(|_| std::env::temp_dir())
                .join("exports");
            std::fs::create_dir_all(&dir)
                .map_err(|err| format!("could not create {}: {err}", dir.display()))?;
            let stamp = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |d| d.as_secs());
            let path = dir.join(format!("{session}-{scope}-{stamp}.{ext}"));
            std::fs::write(&path, text)
                .map_err(|err| format!("could not write {}: {err}", path.display()))?;
            done.path = Some(path.display().to_string());
        }
        ApiCall::SetEffort {
            session,
            effort,
            revision,
        } => match api
            .patch_session(session, json!({ "effort": effort }), Some(*revision), &key)
            .await
            .map_err(e)?
        {
            Outcome::Done { .. } => {}
            Outcome::Conflict { message, .. } => done.conflict = Some(message),
        },
    }
    Ok(done)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn archived_prompt_unarchives_first_and_stops_on_failure() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        for succeeds in [true, false] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move {
                let mut paths = Vec::new();
                for n in 0..if succeeds { 2 } else { 1 } {
                    let (mut socket, _) = listener.accept().await.unwrap();
                    let mut bytes = vec![0; 8192];
                    let len = socket.read(&mut bytes).await.unwrap();
                    paths.push(
                        String::from_utf8_lossy(&bytes[..len])
                            .lines()
                            .next()
                            .unwrap()
                            .to_owned(),
                    );
                    let status = if succeeds { "200 OK" } else { "409 Conflict" };
                    let body = if !succeeds {
                        r#"{"error":"Tree is busy"}"#
                    } else if n == 0 {
                        r#"{"id":"root","archived":false}"#
                    } else {
                        "{}"
                    };
                    let response = format!("HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}", body.len());
                    socket.write_all(response.as_bytes()).await.unwrap();
                }
                paths
            });
            let call = ApiCall::Prompt {
                session: "child".into(),
                unarchive: Some("root".into()),
                text: "Continue".into(),
                images: Vec::new(),
                pastes: Vec::new(),
                mode: "send".into(),
                draft: None,
            };
            let api = Http::new(&url, "tok").ok().map(CoreApi::new);
            let done = run(api, call).await;
            assert_eq!(done.result.is_ok(), succeeds, "{done:?}");
            let paths = server.await.unwrap();
            assert_eq!(paths[0], "POST /api/session/root/unarchive HTTP/1.1");
            if succeeds {
                assert_eq!(paths[1], "POST /api/session/child/prompt HTTP/1.1");
            } else {
                assert_eq!(paths.len(), 1);
                assert_eq!(done.call.draft(), Some((Some("child"), "Continue")));
            }
        }
    }

    #[test]
    fn failed_sends_hand_the_draft_back() {
        let c = ApiCall::Prompt {
            unarchive: None,
            mode: "send".into(),
            images: Vec::new(),
            pastes: Vec::new(),
            session: "ses_1".into(),
            text: "hello".into(),
            draft: None,
        };
        assert_eq!(c.draft(), Some((Some("ses_1"), "hello")));
        assert_eq!(
            ApiCall::Interrupt {
                session: "s".into()
            }
            .draft(),
            None
        );
    }

    #[tokio::test]
    async fn an_answer_is_one_keyed_post_with_every_question() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            let (mut sock, _) = listener.accept().await.unwrap();
            let mut buf = Vec::new();
            let mut chunk = [0u8; 4096];
            loop {
                let n = sock.read(&mut chunk).await.unwrap();
                buf.extend_from_slice(&chunk[..n]);
                let text = String::from_utf8_lossy(&buf).to_string();
                if let Some((head, body)) = text.split_once("\r\n\r\n") {
                    let want: usize = head
                        .to_ascii_lowercase()
                        .lines()
                        .find_map(|l| {
                            l.strip_prefix("content-length:")
                                .map(|v| v.trim().parse().unwrap())
                        })
                        .unwrap_or(0);
                    if body.len() >= want {
                        break;
                    }
                }
            }
            sock.write_all(b"HTTP/1.1 204 No Content\r\nconnection: close\r\n\r\n")
                .await
                .unwrap();
            String::from_utf8_lossy(&buf).to_string()
        });
        let answers = vec![
            json!({"question_id": "db", "selected": ["pg"], "text": null}),
            json!({"question_id": "notes", "selected": [], "text": "small"}),
        ];
        let call = ApiCall::Answer {
            session: "ses_1".into(),
            id: "qst_1".into(),
            answers: answers.clone(),
        };
        assert_eq!(call.done_label(), "");
        assert_eq!(call.draft(), None);
        let api = Http::new(&url, "tok").ok().map(CoreApi::new);
        let done = run(api, call).await;
        assert!(done.result.is_ok(), "{done:?}");
        let seen = server.await.unwrap();
        assert!(
            seen.starts_with("POST /api/session/ses_1/question/qst_1 "),
            "{seen}"
        );
        assert!(seen.to_ascii_lowercase().contains("idempotency-key:"));
        let body = seen.split_once("\r\n\r\n").unwrap().1;
        let sent: Value = serde_json::from_str(body).unwrap();
        assert_eq!(sent, json!({ "answers": answers }));
    }

    #[tokio::test]
    async fn learn_preview_install_and_session_switch_use_the_two_step_contract() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            let mut requests = Vec::new();
            for _ in 0..3 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let n = socket.read(&mut chunk).await.unwrap();
                    request.extend_from_slice(&chunk[..n]);
                    let text = String::from_utf8_lossy(&request);
                    if let Some((head, body)) = text.split_once("\r\n\r\n") {
                        let length = head
                            .lines()
                            .find_map(|line| {
                                let (key, value) = line.split_once(':')?;
                                key.eq_ignore_ascii_case("content-length")
                                    .then(|| value.trim().parse::<usize>().unwrap())
                            })
                            .unwrap_or(0);
                        if body.len() >= length {
                            break;
                        }
                    }
                }
                let text = String::from_utf8_lossy(&request).to_string();
                let path = text
                    .lines()
                    .next()
                    .unwrap()
                    .split_whitespace()
                    .nth(1)
                    .unwrap()
                    .to_owned();
                let reply = match path.as_str() {
                    "/api/skill/preview" => json!({
                        "fetch_id": "fetch-1",
                        "requested": ["alpha", "beta"],
                        "problems": [],
                        "found": []
                    }),
                    "/api/skill/install" => json!({
                        "installed": [
                            {"name": "alpha", "replaced": false},
                            {"name": "beta", "replaced": true}
                        ],
                        "skipped": []
                    }),
                    "/api/session/ses_1" => json!({"id": "ses_1", "revision": 8}),
                    other => panic!("unexpected request path: {other}"),
                }
                .to_string();
                let response = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{reply}",
                    reply.len()
                );
                socket.write_all(response.as_bytes()).await.unwrap();
                requests.push(text);
            }
            requests
        });
        let api = Http::new(&url, "tok").ok().map(CoreApi::new);

        let preview = run(
            api.clone(),
            ApiCall::LearnPreview {
                project: "prj_1".into(),
                source: "owner/repo".into(),
                scope: "project".into(),
            },
        )
        .await;
        assert_eq!(
            preview.result.unwrap().learn_preview,
            Some(json!({
                "fetch_id": "fetch-1",
                "requested": ["alpha", "beta"],
                "problems": [],
                "found": []
            }))
        );

        let install = run(
            api.clone(),
            ApiCall::LearnInstall {
                project: "prj_1".into(),
                scope: "global".into(),
                fetch_id: "fetch-1".into(),
                skills: vec!["alpha".into(), "beta".into()],
                overwrite: true,
                replacing: vec!["beta".into()],
            },
        )
        .await;
        assert_eq!(
            install.result.unwrap().message.as_deref(),
            Some(
                "Learned alpha, beta for every project (replaced beta). Available in new sessions."
            )
        );

        let switched = run(
            api,
            ApiCall::SetDisabledTools {
                session: "ses_1".into(),
                disabled_tools: vec!["bash".into(), "skill:alpha".into(), "mcp:docs".into()],
                revision: 7,
            },
        )
        .await;
        assert!(switched.result.is_ok());
        let requests = server.await.unwrap();
        assert!(requests[0].starts_with("POST /api/skill/preview "));
        assert_eq!(
            serde_json::from_str::<Value>(requests[0].split_once("\r\n\r\n").unwrap().1).unwrap(),
            json!({"source":"owner/repo", "scope":"project", "project":"prj_1"})
        );
        assert!(requests[1].starts_with("POST /api/skill/install "));
        assert_eq!(
            serde_json::from_str::<Value>(requests[1].split_once("\r\n\r\n").unwrap().1).unwrap(),
            json!({
                "fetch_id":"fetch-1",
                "skills":["alpha", "beta"],
                "scope":"global",
                "project":"prj_1",
                "overwrite":true
            })
        );
        assert!(requests[2].starts_with("PATCH /api/session/ses_1 "));
        assert!(requests[2].to_ascii_lowercase().contains("if-match: 7"));
        assert_eq!(
            serde_json::from_str::<Value>(requests[2].split_once("\r\n\r\n").unwrap().1).unwrap(),
            json!({"disabled_tools":["bash", "skill:alpha", "mcp:docs"]})
        );
    }

    #[tokio::test]
    async fn voice_actions_use_status_download_and_raw_wav_contracts() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            let mut requests = Vec::new();
            for _ in 0..3 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let n = socket.read(&mut chunk).await.unwrap();
                    request.extend_from_slice(&chunk[..n]);
                    let Some(split) = request.windows(4).position(|w| w == b"\r\n\r\n") else {
                        continue;
                    };
                    let headers = String::from_utf8_lossy(&request[..split]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            let (key, value) = line.split_once(':')?;
                            key.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if request.len() >= split + 4 + length {
                        break;
                    }
                }
                let split = request.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
                let headers = String::from_utf8_lossy(&request[..split]);
                let path = headers
                    .lines()
                    .next()
                    .unwrap()
                    .split_whitespace()
                    .nth(1)
                    .unwrap();
                let (status, body) = match path {
                    "/api/voice" => (
                        "200 OK",
                        json!({"stt":{"state":"ready","repo":"org/stt","size":179_005_408}}),
                    ),
                    "/api/voice/stt/download" => ("202 Accepted", json!({"state":"downloading"})),
                    "/api/voice/transcribe?partial=1" => ("200 OK", json!({"text":"hello there"})),
                    other => panic!("unexpected request {other}"),
                };
                let body = body.to_string();
                let response = format!(
                    "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
                socket.write_all(response.as_bytes()).await.unwrap();
                requests.push(request);
            }
            requests
        });
        let api = Http::new(&url, "token").ok().map(CoreApi::new);

        let status = run(api.clone(), ApiCall::VoiceStatus { generation: 7 }).await;
        assert_eq!(
            status.result.unwrap().voice.unwrap()["stt"]["state"],
            "ready"
        );
        let downloaded = run(api.clone(), ApiCall::VoiceDownload { generation: 7 }).await;
        assert_eq!(
            downloaded.result.unwrap().voice.unwrap()["state"],
            "downloading"
        );
        let wav = b"RIFFsample".to_vec();
        let transcript = run(
            api,
            ApiCall::VoiceTranscribe {
                wav: wav.clone(),
                partial: true,
                generation: 7,
            },
        )
        .await;
        assert_eq!(
            transcript.result.unwrap().transcript.as_deref(),
            Some("hello there")
        );

        let requests = server.await.unwrap();
        assert!(String::from_utf8_lossy(&requests[0]).starts_with("GET /api/voice "));
        assert!(String::from_utf8_lossy(&requests[1]).starts_with("POST /api/voice/stt/download "));
        let request = &requests[2];
        let split = request.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
        let headers = String::from_utf8_lossy(&request[..split]).to_ascii_lowercase();
        assert!(headers.starts_with("post /api/voice/transcribe?partial=1 "));
        assert!(headers
            .lines()
            .any(|line| line == "content-type: audio/wav"));
        assert_eq!(&request[split + 4..], wav.as_slice());
    }

    #[tokio::test]
    async fn settings_voice_actions_use_tts_download_and_cache_delete_routes() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            let mut requests = Vec::new();
            for _ in 0..4 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let n = socket.read(&mut chunk).await.unwrap();
                    request.extend_from_slice(&chunk[..n]);
                    let Some(split) = request.windows(4).position(|w| w == b"\r\n\r\n") else {
                        continue;
                    };
                    let headers = String::from_utf8_lossy(&request[..split]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            let (key, value) = line.split_once(':')?;
                            key.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if request.len() >= split + 4 + length {
                        break;
                    }
                }
                let split = request.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
                let headers = String::from_utf8_lossy(&request[..split]);
                let path = headers
                    .lines()
                    .next()
                    .unwrap()
                    .split_whitespace()
                    .nth(1)
                    .unwrap();
                let (status, body) = match path {
                    "/api/voice" => (
                        "200 OK",
                        json!({
                            "stt":{"state":"absent","repo":"org/stt"},
                            "tts":{"state":"absent","repo":"org/tts"}
                        }),
                    ),
                    "/api/voice/tts/download" => ("202 Accepted", json!({"state":"downloading"})),
                    "/api/voice/stt" => (
                        "200 OK",
                        json!({"repo":"org/stt","state":"absent","freed":1234}),
                    ),
                    "/api/voice/tts" => (
                        "200 OK",
                        json!({"repo":"org/tts","state":"absent","freed":5678}),
                    ),
                    other => panic!("unexpected request {other}"),
                };
                let body = body.to_string();
                let response = format!(
                    "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
                socket.write_all(response.as_bytes()).await.unwrap();
                requests.push(request);
            }
            requests
        });
        let api = Http::new(&url, "token").ok().map(CoreApi::new);

        let status = run(api.clone(), ApiCall::VoiceSettingsStatus { generation: 1 }).await;
        assert_eq!(
            status.result.unwrap().voice.unwrap()["tts"]["repo"],
            "org/tts"
        );
        let downloaded = run(
            api.clone(),
            ApiCall::VoiceModel {
                target: VoiceTarget::Tts,
                operation: VoiceOperation::Download,
            },
        )
        .await;
        assert_eq!(
            downloaded.result.unwrap().voice.unwrap()["state"],
            "downloading"
        );
        for target in [VoiceTarget::Stt, VoiceTarget::Tts] {
            let removed = run(
                api.clone(),
                ApiCall::VoiceModel {
                    target,
                    operation: VoiceOperation::Remove,
                },
            )
            .await;
            assert_eq!(
                removed.result.unwrap().voice.unwrap()["freed"],
                if target == VoiceTarget::Stt {
                    1234
                } else {
                    5678
                }
            );
        }

        let requests = server.await.unwrap();
        let starts: Vec<_> = requests
            .iter()
            .map(|request| {
                String::from_utf8_lossy(request)
                    .lines()
                    .next()
                    .unwrap()
                    .to_owned()
            })
            .collect();
        assert!(starts[0].starts_with("GET /api/voice "));
        assert!(starts[1].starts_with("POST /api/voice/tts/download "));
        assert!(starts[2].starts_with("DELETE /api/voice/stt "));
        assert!(starts[3].starts_with("DELETE /api/voice/tts "));
    }

    #[tokio::test]
    async fn no_daemon_is_an_error_not_a_panic() {
        let done = run(
            None,
            ApiCall::Continue {
                session: "s".into(),
            },
        )
        .await;
        assert!(done.result.is_err());
    }
}

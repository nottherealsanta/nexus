//! The mutations the shell can make through Core's HTTP API, and how they run.
//! Each user action is one `ApiCall` with its own `RequestId`; the result comes
//! back as an `ActionDone` that the app turns into a toast (and, for a failed
//! send, a restored draft).

use std::path::Path;

use serde_json::{json, Value};

use crate::api::{CoreApi, Outcome, RequestId};
use crate::discovery;
use crate::transport::Http;

#[derive(Clone, PartialEq)]
pub struct Secret(pub String);
impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("[redacted]")
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum ApiCall {
    Provider {
        id: String,
        sign_out: bool,
        key: Option<Secret>,
    },
    /// Send a prompt to an existing idle session.
    Prompt {
        session: String,
        text: String,
        images: Vec<crate::images::Image>,
        mode: String,
    },
    Input {
        session: String,
        id: String,
        action: String,
        before: Option<String>,
        parts: Vec<Value>,
    },
    /// Create a session in a project, then prompt it.
    NewAndPrompt {
        project: String,
        text: String,
        images: Vec<crate::images::Image>,
        /// Agent, model and effort chosen before the session existed.
        fields: serde_json::Value,
    },
    NewSession {
        project: String,
        fields: serde_json::Value,
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
    },
    SpeakStop,
    VoiceStatus {
        generation: u64,
    },
    /// Settings refresh, separate from the generation-guarded dictation check.
    VoiceSettingsStatus {
        generation: u64,
    },
    VoiceDownload {
        generation: u64,
    },
    VoiceModel {
        target: VoiceTarget,
        operation: VoiceOperation,
    },
    VoiceTranscribe {
        wav: Vec<u8>,
        partial: bool,
        generation: u64,
    },
    Interrupt {
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
    /// Remove a project; its sessions go with it.
    DeleteProject {
        project: String,
    },
    /// Read the project's prompt history (Up in the composer); never toasts.
    Prompts {
        project: String,
    },
    /// Plan limits of the providers that report them (`GET /api/usage`).
    Usage,
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
            ApiCall::Input { .. } => "Pending input updated",
            ApiCall::Provider { sign_out: true, .. } => "Signed out",
            ApiCall::Provider { .. } => "Sign-in started",
            ApiCall::Prompt { .. } | ApiCall::NewAndPrompt { .. } => "Sent",
            ApiCall::NewSession { .. } => "Session created",
            ApiCall::Continue { .. } => "Continuing",
            ApiCall::Speak { .. } => "Finished speaking",
            ApiCall::Handoff { .. } => "Handed off to a new session",
            ApiCall::Btw { .. } => "",
            ApiCall::SpeakStop => "Stopped speaking",
            ApiCall::VoiceStatus { .. }
            | ApiCall::VoiceSettingsStatus { .. }
            | ApiCall::VoiceTranscribe { .. } => "",
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
            ApiCall::Answer { .. } => "Answered",
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
            ApiCall::Prompts { .. }
            | ApiCall::Search { .. }
            | ApiCall::OlderMessages { .. }
            | ApiCall::SwitchCost { .. }
            | ApiCall::Usage => "",
        }
    }

    /// Text to put back in the composer if the call fails.
    pub fn draft(&self) -> Option<(Option<&str>, &str)> {
        match self {
            ApiCall::Prompt { session, text, .. } => Some((Some(session), text)),
            ApiCall::NewAndPrompt { text, .. } => Some((None, text)),
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
    /// A session the app should open (new sessions).
    pub open: Option<String>,
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
    /// Core's `/api/usage`.
    pub usage: Option<Value>,
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
    /// A transcription result. It remains a response until the app inserts it.
    pub transcript: Option<String>,
    /// The answer to a `/btw` question.
    pub answer: Option<String>,
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
        ApiCall::Provider { id, sign_out, key } => {
            let method = if *sign_out {
                reqwest::Method::DELETE
            } else {
                reqwest::Method::POST
            };
            let body = if let Some(key) = key {
                json!({"api_key": key.0})
            } else {
                json!({"method": "device"})
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
        ApiCall::Usage => {
            done.usage = Some(api.http.get("/api/usage", &[]).await.map_err(e)?);
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
            text,
            images,
            mode,
        } => {
            conflict(
                api.prompt_images(session, text, images, mode, &key)
                    .await
                    .map_err(e)?,
            )?;
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
                "remove" => (reqwest::Method::DELETE, format!("{base}/{id}"), None),
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
            project,
            text,
            images,
            fields,
        } => {
            let s = conflict(
                api.create_session(project, fields.clone(), &key)
                    .await
                    .map_err(e)?,
            )?
            .ok_or("no session returned")?;
            done.open = Some(s.id.clone());
            let key2 = RequestId::new();
            conflict(
                api.prompt_images(&s.id, text, images, "send", &key2)
                    .await
                    .map_err(e)?,
            )?;
        }
        ApiCall::NewSession { project, fields } => {
            let s = conflict(
                api.create_session(project, fields.clone(), &key)
                    .await
                    .map_err(e)?,
            )?
            .ok_or("no session returned")?;
            done.open = Some(s.id);
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
        ApiCall::Btw { session, question } => {
            let body = api
                .http
                .call(
                    reqwest::Method::POST,
                    &format!("/api/session/{session}/btw"),
                    &[],
                    Some(&json!({ "question": question })),
                    None,
                    None,
                )
                .await
                .map_err(e)?
                .body;
            done.answer = body["answer"].as_str().map(str::to_owned);
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
        ApiCall::VoiceSettingsStatus { .. } => {
            done.voice = Some(api.http.get("/api/voice", &[]).await.map_err(e)?);
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
            conflict(api.interrupt(session, &key).await.map_err(e)?)?;
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

    #[test]
    fn failed_sends_hand_the_draft_back() {
        let c = ApiCall::Prompt {
            mode: "send".into(),
            images: Vec::new(),
            session: "ses_1".into(),
            text: "hello".into(),
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
        assert_eq!(call.done_label(), "Answered");
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
                        json!({"stt":{"state":"ready","repo":"org/stt","size":181_000_000}}),
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
